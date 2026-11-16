"""데이터 묶음의 인별실적: 사람별 기간 실적 파일을 미리보기한 뒤 적재한다. 메인 관리자만 쓴다(menu.yaml data-person-perf).

| 단계 | 하는 일 |
| --- | --- |
| 미리보기 | 머리글을 검사하고, DB 를 메모리에 복사한 사본에서 실제 적재기를 돌려 건수, 사번 매칭, 막힌 줄, 구분 분포를 낸다. 사본은 버린다 |
| 적재 | 미리보기한 파일과 같은지 해시로 확인하고 연결 흐름(Pipeline, 종류 person_perf)으로 원본을 보관한 뒤 적재한다 |

- 적재기는 app/loader/person_perf.py 다. 계약 원장과 섞지 않고 agent_period_perf 에만 넣는다. 그래서 업적 run 은 다시 돌리지 않는다.
- 기간은 시작월과 끝월이다. 비우면 파일 이름의 '202603-05' 꼴에서 읽는다.
- 화면에는 건수, 비율, 사원번호, 줄 번호만 보인다. 사람 이름과 금액은 미리보기와 결과에 싣지 않는다.
  적재기의 품질 문제 글은 이름과 금액을 넣지 않게 만들어져 있어 그대로 보인다.
- 올린 파일은 화면 업로드 구역(APP_UPLOAD_DIR)에 두었다가 적재하거나 취소하면 지운다. 같은 파일(해시)은 미리보기 전에 막는다.
"""
import hashlib
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.connect.base import DUPLICATE, LOADED, VIA_UPLOAD, RawFile
from app.connect.pipeline import Pipeline
from app.loader import person_perf as pp
from app.loader.sheet import HeaderMismatch
from app.web import auth
from app.web import person_perf_view as ppv
from app.web.deps import get_con, guard, page
from app.web.routes.data import BATCH_STATUS_KO, _read_upload, issue_view, scratch, upload_dir

router = APIRouter()
SESSION_KEY = "person_perf_upload"
KIND = "person_perf"
TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
YM = re.compile(r"^20\d\d(0[1-9]|1[0-2])$")
MAX_BYTES = 30 * 1024 * 1024


def _file(saved):
    tok = (saved or {}).get("token")
    if not tok or not TOKEN.match(tok):
        return None
    p = upload_dir() / f"person_perf_{tok}.xlsx"
    return p if p.is_file() else None


def _drop(request) -> None:
    p = _file(request.session.get(SESSION_KEY))
    if p:
        p.unlink(missing_ok=True)
    request.session.pop(SESSION_KEY, None)


def _period(form, file_name) -> tuple:
    """(시작월, 끝월, 오류). 칸이 비면 파일 이름에서 읽는다."""
    start, end = str(form.get("start_ym") or "").strip(), str(form.get("end_ym") or "").strip()
    if not start and not end:
        g = pp.guess_period(file_name or "")
        if not g:
            return None, None, "기간을 정하세요. 파일 이름에 202603-05 꼴이 없어 기간을 읽지 못했습니다."
        return g[0], g[1], None
    if not (YM.match(start) and YM.match(end)):
        return None, None, "시작월과 끝월을 YYYYMM 으로 넣으세요."
    if start > end:
        return None, None, "시작월이 끝월보다 늦습니다."
    return start, end, None


def _absent(con, start, end) -> int:
    """재직 설계사 가운데 이 기간 파일에 없는 사람 수. 사본에서 센다."""
    return con.execute("""SELECT COUNT(*) FROM agent a WHERE a.status='active' AND NOT EXISTS
                          (SELECT 1 FROM v_agent_period_perf p WHERE p.agent_id=a.agent_id AND p.start_ym=? AND p.end_ym=?)""",
                       (start, end)).fetchone()[0]


def preview_file(con, path, start, end, by) -> dict:
    """사본에서 적재기를 돌린다. 원래 DB 는 바꾸지 않는다. 돌려주는 것에 이름과 금액은 없다."""
    head = pp.preview(path, start, end)
    if not head["ok"]:
        return {"ok": False, "reasons": head["reasons"]}
    mem = scratch(con)
    try:
        s = pp.load_person_perf(mem, path, start, end, by=by)
        issues = [dict(r) for r in mem.execute(
            "SELECT severity, check_code, row_no, message FROM quality_issue WHERE batch_id=? ORDER BY severity, row_no", (s["batch_id"],))]
        status = mem.execute("SELECT status FROM import_batch WHERE batch_id=?", (s["batch_id"],)).fetchone()[0]
        keep = ("file_rows", "loaded", "skipped", "summary_rows", "summary_matches", "sums_match", "matched_agents", "unmapped_agents",
                "name_mismatch", "insurer_sum_mismatch", "insurers", "unmapped_insurers", "tiers", "dropped_columns", "superseded",
                "block", "warn")
        out = {k: s.get(k) for k in keep}
        out.update(ok=True, warnings=head["warnings"], batch_status=status, absent=_absent(mem, start, end),
                   blocked_rows=[i for i in issue_view(issues, True) if i["severity"] == "block" and i["row_no"]],
                   issues=issue_view(issues, True))
        return out
    finally:
        mem.close()


def _recent(con) -> list:
    if not ppv.ready(con):
        return []
    rows = [dict(r) for r in con.execute(
        """SELECT b.batch_id, b.status, b.row_count, b.loaded_at, b.received_via,
                  (SELECT MIN(start_ym) FROM agent_period_perf p WHERE p.batch_id=b.batch_id) start_ym,
                  (SELECT MAX(end_ym) FROM agent_period_perf p WHERE p.batch_id=b.batch_id) end_ym,
                  (SELECT COUNT(*) FROM agent_period_perf p WHERE p.batch_id=b.batch_id AND p.agent_id IS NULL) unmapped,
                  (SELECT COUNT(*) FROM quality_issue q WHERE q.batch_id=b.batch_id AND q.severity='block') block,
                  (SELECT COUNT(*) FROM quality_issue q WHERE q.batch_id=b.batch_id AND q.severity='warn') warn
           FROM import_batch b WHERE b.data_type=? ORDER BY b.batch_id DESC LIMIT 10""", (ppv.DATA_TYPE,))]
    for r in rows:
        r["period"] = ppv.period_label(r["start_ym"], r["end_ym"]) if r["start_ym"] else "-"
        r["status_ko"] = BATCH_STATUS_KO.get(r["status"], r["status"])
    return rows


def _page(request, ctx, con, status_code=200, **kw):
    kw.setdefault("preview", None)
    kw.setdefault("result", None)
    kw.setdefault("err", None)
    kw.setdefault("form", {"start_ym": "", "end_ym": ""})
    return page(request, ctx, "data_person_perf.html", status_code=status_code, recent=_recent(con), latest=ppv.latest(con),
                TIER_KO=ppv.TIER_KO, TIERS=ppv.TIERS, TIER_CHIP=ppv.TIER_CHIP, period_label=ppv.period_label, **kw)


@router.get("/data/person-perf", response_class=HTMLResponse)
def person_perf_page(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return _page(request, ctx, con)


@router.post("/data/person-perf", response_class=HTMLResponse)
async def person_perf_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    action = form.get("action")
    if action == "cancel":
        _drop(request)
        return _page(request, ctx, con)
    if action == "preview":
        return await _preview(request, ctx, con, form)
    if action == "apply":
        return _apply(request, ctx, con)
    raise HTTPException(400, "처리할 수 없는 요청입니다.")


async def _preview(request, ctx, con, form):
    keep = {"start_ym": str(form.get("start_ym") or "").strip(), "end_ym": str(form.get("end_ym") or "").strip()}
    name, data, err = await _read_upload(form, MAX_BYTES)
    if err:
        return _page(request, ctx, con, err=err, form=keep, status_code=400)
    start, end, err = _period(form, name)
    if err:
        return _page(request, ctx, con, err=err, form=keep, status_code=400)
    digest = hashlib.sha256(data).hexdigest()
    dup = con.execute("SELECT batch_id FROM import_batch WHERE file_hash=?", (digest,)).fetchone()
    if dup:
        return _page(request, ctx, con, err=f"이미 적재한 파일입니다. 배치 {dup[0]}번입니다.", form=keep, status_code=409)
    _drop(request)
    token = secrets.token_urlsafe(24)
    path = upload_dir() / f"person_perf_{token}.xlsx"
    path.write_bytes(data)
    try:
        pv = preview_file(con, path, start, end, ctx["login_id"])
    except HeaderMismatch as e:
        pv = {"ok": False, "reasons": e.reasons}
    except Exception:
        pv = {"ok": False, "reasons": ["파일을 끝까지 읽지 못했습니다. 인별실적 엑셀 파일인지 확인하세요."]}
    if not pv["ok"]:
        path.unlink(missing_ok=True)
        msg = "인별실적 양식이 아닙니다. " + " / ".join(pv["reasons"][:3])
        return _page(request, ctx, con, err=msg, form=keep, status_code=400)
    request.session[SESSION_KEY] = {"token": token, "start": start, "end": end, "file": name, "hash": digest}
    return _page(request, ctx, con, preview={**pv, "file": name, "start": start, "end": end}, form={"start_ym": start, "end_ym": end})


def _apply(request, ctx, con):
    saved = request.session.get(SESSION_KEY) or {}
    path = _file(saved)
    if not path:
        return _page(request, ctx, con, err="미리보기한 파일이 없습니다. 파일을 다시 올려 미리보기부터 하세요.", status_code=400)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != saved.get("hash"):
        _drop(request)
        return _page(request, ctx, con, err="미리보기한 파일과 다릅니다. 다시 올려 주세요.", status_code=400)
    by = ctx["login_id"]
    start, end = saved["start"], saved["end"]
    raw = RawFile(name=saved.get("file") or "person_perf.xlsx", data=data, via=VIA_UPLOAD, kind=KIND, period_ym=end,
                  meta={"by": by, "start_ym": start, "end_ym": end})
    res = Pipeline(con).ingest(raw)
    batch_id = (res.detail or {}).get("batch_id")
    if res.status == DUPLICATE:
        _drop(request)
        return _page(request, ctx, con, err="이미 적재한 파일입니다.", status_code=409)
    if not batch_id:
        return _page(request, ctx, con, err=f"적재하지 못했습니다. {res.reason}", status_code=400)
    _drop(request)
    d = res.detail
    auth.log(con, by, "person_perf_upload", f"batch:{batch_id} {start}-{end} rows:{d.get('loaded')}")
    return _page(request, ctx, con, result={"batch_id": batch_id, "loaded": res.status == LOADED, "start": start, "end": end,
                                            "file": raw.name, "reason": res.reason, **{k: v for k, v in d.items() if k != "batch_id"}})

