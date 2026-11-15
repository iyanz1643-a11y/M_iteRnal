"""수수료·점검 묶음 다섯 화면과 데이터 묶음의 정산서와 유지율. S3 파트 G.

금액은 app/engine/settle.py 가 준 값 그대로다. 이 파일은 범위 안 설계사 목록을 넘기고 화면 꼴로 묶을 뿐 계산하지 않는다.
모든 주소는 guard 를 거친다. 범위는 config/menu.yaml 의 roles 를 따른다.

| 주소 | 메인 관리자 | 관리자 | 설계사 | 쓰기 |
| --- | --- | --- | --- | --- |
| /settle/commission | 전체, 사람 고르기 | 본인 | 본인 | 없음 |
| /settle/clawback | 전체 | 본인 | 본인 | 없음 |
| /settle/recon | 전체 | 없음 | 없음 | 대사 다시 돌리기 |
| /settle/adjust | 전체 | 없음 | 없음 | 차이 처리. 메모 필수. settle.set_diff_status |
| /settle/close | 전체 | 없음 | 없음 | 월 마감과 해제. 마감은 경고와 확인 칸, 해제는 사유 필수 |
| /data/statement | 전체 | 없음 | 없음 | 정산서와 유지율 현황 올리기. 미리보기 뒤 넣기 |

D-17. 관리자와 설계사는 메뉴 범위가 self 라 본인 한 사람의 목록만 넘긴다. 다른 사람을 고르는 agent 값은 메인 관리자만 쓴다.
관리자가 범위 밖 사람을 고르면 403 이고 변경 기록에 범위 밖 접근 시도로 남는다.

D-11 가정. 정산서가 들어온 보험사는 정산서 기준, 정산서가 없고 요율표가 있는 보험사는 자체 계산이라 예상이다. 금액마다 둘 중 하나를 붙인다.
D-23 초년도 지급 한도 점검은 규정 확인 전이라 넣지 않았다.
D-05. 계약은 증권번호 끝 네 자리로만 가리킨다. 고객 이름은 엔진도 주지 않는다.

마감. 조건과 쓰기는 엔진 settle.close_check, close_month, reopen_month 한 곳이다. 기존 도구도 같은 조건을 쓴다. 마감 조건은 가정이다.
이 파일은 확인 칸과 오류 문구를 화면에 맞게 먼저 보고, 엔진이 메인 관리자와 막는 조건을 한 번 더 본다.
- 대사 차이는 웹의 처리 상태(recon_diff_action)로 본다. 미처리가 하나라도 있으면 막는다. 보험사 문의는 막지 않고 경고한다.
- 기존 도구의 reconciliation_diff.status 는 마감 판단에 쓰지 않고 참고 건수로만 보여 준다.
- 그 달 정산서가 들어왔는데 대사를 돌리지 않았으면 막는다. 차단 등급 품질 문제와 검토 대기 시상 공문도 막는다. 기존 도구와 같다.
- 정산서가 없는 보험사가 있으면 막지 않고 경고한다. 그 보험사 금액은 예상으로 남는다.
- 조정(adjustment)은 그 달 지급에 넣지 않는다. 마감은 상태만 잠그고 금액 묶음(snapshot)은 만들지 않는다.

정산서와 유지율 현황 올리기. 적재는 기존 적재기 excel_loader.load_statement, load_persistency 를 부른다.
INLINE_BYTES 이하 파일은 미리보기 뒤 요청 안에서 바로 넣는다. 그보다 크고 MAX_BYTES 이하인 파일은 미리보기 없이
작업 워커의 큰 적재(ingest) 작업으로 넣고 /data/jobs 로 보낸다. 상태와 건수는 그 화면에서 본다. 적재기와 마감 확인은 같다.
정산서를 넣으면 어느 쪽이든 그 달 대사 다시 돌리기(recon_run) 작업이 뒤따른다. 마감한 달이면 넣지 않는다. 규칙은 app/ops/jobs.py 다.
미리보기는 DB 를 메모리에 복사해 같은 적재기를 돌리고 버린다. 그래서 미리보기와 넣기의 건수가 같다.
미리보기와 결과에는 건수와 문제 종류, 줄 번호만 보인다. 적재기 메시지에는 금액이 섞일 수 있어 열 없음 말고는 싣지 않는다.
"""
from __future__ import annotations

import json
import pathlib
import re
import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.engine import reconcile
from app.engine import settle as st
from app.loader import excel_loader
from app.loader.mapping import MAPPING_DIR
from app.ops import jobs
from app.web import auth
from app.web.deps import get_con, guard, page
from app.web.routes.perf import scope_ids
from app.web.routes.staff import upload_dir

router = APIRouter()

YM = re.compile(r"^20\d\d(0[1-9]|1[0-2])$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
LINES_SHOW = 200                    # 한 화면에 그리는 줄의 상한. 넘으면 건수만 더 적는다
DIFF_SHOW = 200
NOTE_MAX = 500
UPLOAD_KEY = "settle_upload"        # 세션에는 파일 표지, 종류, 보험사 코드, 귀속월, 파일 이름만 둔다
INLINE_BYTES = 20 * 1024 * 1024     # 이하면 미리보기 뒤 바로 넣는다
MAX_BYTES = 100 * 1024 * 1024       # INLINE_BYTES 보다 크면 작업으로 넣는다. 이보다 크면 받지 않는다
CHUNK = 1024 * 1024
KIND_KO = {"statement": "원수사 정산서", "persistency": "유지율 현황"}
BATCH_STATUS_KO = {"received": "받음", "active": "반영", "loaded": "반영", "failed": "실패", "superseded": "바뀜"}
ISSUE_KO = {"missing_column": "필수 열 없음", "bad_value": "읽을 수 없는 값", "required_blank": "필수 값 없음", "total_mismatch": "파일 합계와 다름",
            "unmapped_agent": "설계사 코드 매핑 없음", "unmapped_product": "상품 코드가 마스터에 없음", "negative_amount": "환수가 아닌데 음수 금액",
            "duplicate_key": "같은 계약 줄 겹침", "unmapped_insurer": "보험사 이름 매핑 없음", "bad_ratio": "유지 건수가 대상보다 많음"}
SOURCE_CHIP = {"statement": ("good", "정산서 기준"), "calc": ("warn", "예상")}
STATUS_CHIP = {"open": "bad", "checked": "blue", "asked": "warn", "resolved": "good"}
LEGACY_KO = {"open": "미처리", "confirmed": "확인", "adjusted": "조정", "ignored": "무시"}
CLOSE_ACTIONS = ("close", "close_reopen")


# ---------------------------------------------------------------- 공통
def months(con, ctx) -> list:
    """고를 수 있는 달. 수수료 계산이나 대사가 끝났거나 정산서가 들어온 달과 로그인 문맥의 기준 월. 최근 달부터."""
    got = {r[0] for r in con.execute("SELECT DISTINCT period_ym FROM calc_run WHERE run_type IN ('commission','reconciliation') AND status='done'")}
    got |= {r[0] for r in con.execute(
        f"SELECT DISTINCT period_ym FROM import_batch WHERE data_type='statement' AND status IN ({','.join('?' * len(st.LIVE_BATCH))})", st.LIVE_BATCH)}
    got.add(ctx["ym"])
    return sorted((m for m in got if m and YM.match(m)), reverse=True)


def pick_ym(con, ctx, ym) -> tuple:
    opts = months(con, ctx)
    return (ym if ym in opts else ctx["ym"]), opts


def closed_row(con, ym) -> dict | None:
    r = con.execute("SELECT period_ym, status, closed_at, closed_by FROM close_period WHERE period_ym=?", (ym,)).fetchone()
    return dict(r) if r else None


def is_closed(con, ym) -> bool:
    r = closed_row(con, ym)
    return bool(r and r["status"] == "closed")


def stamps(con, ym) -> dict:
    """기준 시각. 정산서 적재, 자체 계산, 대사가 끝난 시각."""
    loaded = st.statement_insurers(con, ym)

    def run(t):
        r = con.execute("SELECT finished_at FROM calc_run WHERE run_type=? AND period_ym=? AND status='done' ORDER BY run_id DESC LIMIT 1", (t, ym)).fetchone()
        return r["finished_at"] if r else None
    return {"statement": max(loaded.values()) if loaded else None, "statement_n": len(loaded), "calc": run("commission"), "recon": run("reconciliation")}


def _deny(con, ctx, request, what: str = ""):
    auth.log(con, ctx["login_id"], "access_denied", f"{request.url.path}{' ' + what if what else ''}")
    raise HTTPException(403, "이 사람의 수수료는 볼 수 없습니다. 수수료는 본인과 메인 관리자만 봅니다.")


def _who(con, ids) -> dict:
    """사람 번호에서 사번과 이름. 메인 관리자 화면에서만 부른다."""
    ids = sorted({a for a in ids if a is not None})
    if not ids:
        return {}
    return {r["agent_id"]: dict(r) for r in con.execute(
        f"SELECT agent_id, internal_code code, name FROM agent WHERE agent_id IN ({','.join('?' * len(ids))})", ids)}


def _pick_agent(con, ctx, request, agent):
    """고른 사람. 메인 관리자만 다른 사람을 고른다. 관리자와 설계사는 본인 말고는 403 이다 (D-17)."""
    ids = scope_ids(con, ctx)
    if agent is None:
        return None, ids
    if ctx["scope"] != "all" and agent != ctx.get("agent_id"):
        _deny(con, ctx, request, f"agent:{agent}")
    if agent not in ids:
        _deny(con, ctx, request, f"agent:{agent}")
    if ctx["scope"] != "all":
        return None, ids
    return _who(con, [agent]).get(agent) or {"agent_id": agent, "code": None, "name": None}, [agent]


def _chip(line) -> tuple:
    return SOURCE_CHIP["calc" if line.get("estimated") else "statement"]


# ---------------------------------------------------------------- 수수료 내역
@router.get("/settle/commission", response_class=HTMLResponse)
def commission(request: Request, ym: str | None = None, agent: int | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    ym, yms = pick_ym(con, ctx, ym)
    who, ids = _pick_agent(con, ctx, request, agent)
    m = st.agent_month(con, ids, ym)
    everyone = ctx["scope"] == "all" and who is None
    people = st.by_agent(con, ids, ym) if everyone else []
    lines = [] if everyone else m["lines"]
    by_type = [{**t, "basis_ko": "예상 포함" if t["estimated"] else "정산서 기준"} for t in m["by_type"]]
    return page(request, ctx, "settle_commission.html", m=m, by_type=by_type, people=people, lines=lines[:LINES_SHOW], more=max(len(lines) - LINES_SHOW, 0),
                everyone=everyone, who=who, has_agent=bool(ids), yms=yms, ym=ym, stamps=stamps(con, ym), closed=is_closed(con, ym),
                SOURCE_CHIP=SOURCE_CHIP, chip=_chip, f={"ym": ym, "agent": agent or ""})


# ---------------------------------------------------------------- 환수
@router.get("/settle/clawback", response_class=HTMLResponse)
def clawback(request: Request, ym: str | None = None, agent: int | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    ym, yms = pick_ym(con, ctx, ym)
    who, ids = _pick_agent(con, ctx, request, agent)
    c = st.clawbacks(con, ids, ym)
    everyone = ctx["scope"] == "all" and who is None
    names = _who(con, [x["agent_id"] for x in c["lines"]]) if ctx["scope"] == "all" else {}
    rows = sorted(c["lines"], key=lambda x: (x["amount"], x["insurer"] or "", x["policy_tail"] or ""))
    return page(request, ctx, "settle_clawback.html", c=c, rows=rows[:LINES_SHOW], more=max(len(rows) - LINES_SHOW, 0), names=names,
                people=len({x["agent_id"] for x in rows}), everyone=everyone, who=who, has_agent=bool(ids), yms=yms, ym=ym,
                stamps=stamps(con, ym), chip=_chip, f={"ym": ym, "agent": agent or ""})


# ---------------------------------------------------------------- 대사
def _recon_page(request, ctx, con, ym, *, insurer=None, kind=None, status=None, error=None, done=None, status_code=200):
    ym, yms = pick_ym(con, ctx, ym)
    ids = scope_ids(con, ctx)
    summary = st.statement_summary(con, ids, ym, unmapped=True)
    insurer = insurer if insurer in {r["insurer_id"] for r in summary["rows"]} else None
    kind = kind if kind in st.DIFF_KO else None
    status = status if status in st.STATUS_KO else None
    diffs = st.diff_list(con, ids, ym, unmapped=True, insurer_id=insurer, diff_type=kind, status=status)
    names = _who(con, [d["agent_id"] for d in diffs["rows"]])
    legacy_open = sum(1 for d in diffs["rows"] if d["legacy_status"] == "open")
    return page(request, ctx, "settle_recon.html", status_code=status_code, s=summary, d=diffs, rows=diffs["rows"][:DIFF_SHOW],
                more=max(len(diffs["rows"]) - DIFF_SHOW, 0), names=names, legacy_open=legacy_open, yms=yms, ym=ym, stamps=stamps(con, ym),
                closed=is_closed(con, ym), error=error, done=done, STATUS_KO=st.STATUS_KO, DIFF_KO=st.DIFF_KO, STATUS_CHIP=STATUS_CHIP,
                LEGACY_KO=LEGACY_KO, f={"ym": ym, "insurer": insurer or "", "kind": kind or "", "status": status or ""})


@router.get("/settle/recon", response_class=HTMLResponse)
def recon(request: Request, ym: str | None = None, insurer: int | None = None, kind: str = "", status: str = "", done: str = "",
          ctx: dict = Depends(guard), con=Depends(get_con)):
    return _recon_page(request, ctx, con, ym, insurer=insurer, kind=kind, status=status, done=done == "rerun")


@router.post("/settle/recon", response_class=HTMLResponse)
async def recon_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    """대사 다시 돌리기. 정산서를 새로 넣은 뒤에 쓴다. 마감한 달은 돌리지 않는다. 처리 기록은 같은 차이로 이어진다."""
    form = await request.form()
    ym = str(form.get("ym") or "")
    if form.get("action") != "rerun" or ym not in months(con, ctx):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    if is_closed(con, ym):
        return _recon_page(request, ctx, con, ym, error="마감한 달은 대사를 다시 돌릴 수 없습니다. 마감 화면에서 해제한 뒤 돌리세요.", status_code=400)
    try:
        _, n = reconcile.run(con, ym, ctx["login_id"])
    except RuntimeError:
        con.rollback()
        return _recon_page(request, ctx, con, ym, error="그 달 수수료 자체 계산이 아직 없어 대사를 돌릴 수 없습니다.", status_code=400)
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (db.now(), ctx["login_id"], "recon_run", ym, json.dumps({"diffs": n}, ensure_ascii=False)))
    con.commit()
    return RedirectResponse(f"/settle/recon?ym={ym}&done=rerun", status_code=303)


# ---------------------------------------------------------------- 조정
def _adjust_page(request, ctx, con, ym, *, diff=None, status=None, error=None, form=None, done=False, status_code=200):
    ym, yms = pick_ym(con, ctx, ym)
    ids = scope_ids(con, ctx)
    status = status if status in st.STATUS_KO else None
    all_rows = st.diff_list(con, ids, ym, unmapped=True)
    rows = [r for r in all_rows["rows"] if not status or r["status"] == status]
    sel = next((r for r in all_rows["rows"] if r["diff_id"] == diff), None) if diff is not None else None
    history = list(reversed(st.diff_history(con, sel["diff_id"]))) if sel else []
    actors = {r["login_id"]: r["display_name"] for r in con.execute("SELECT login_id, display_name FROM user_account WHERE is_main_admin=1")}
    acted = sorted((r for r in all_rows["rows"] if r["acted_at"]), key=lambda r: r["acted_at"], reverse=True)
    return page(request, ctx, "settle_adjust.html", status_code=status_code, d=all_rows, rows=rows[:DIFF_SHOW], more=max(len(rows) - DIFF_SHOW, 0),
                sel=sel, history=history, acted=acted[:30], actors=actors, yms=yms, ym=ym, stamps=stamps(con, ym), closed=is_closed(con, ym),
                error=error, form=form or {}, done=done, STATUS_KO=st.STATUS_KO, ACTIONS=st.ACTIONS, STATUS_CHIP=STATUS_CHIP,
                LEGACY_KO=LEGACY_KO, NOTE_MAX=NOTE_MAX, f={"ym": ym, "status": status or "", "diff": sel["diff_id"] if sel else ""})


@router.get("/settle/adjust", response_class=HTMLResponse)
def adjust(request: Request, ym: str | None = None, diff: int | None = None, status: str = "", done: str = "",
           ctx: dict = Depends(guard), con=Depends(get_con)):
    return _adjust_page(request, ctx, con, ym, diff=diff, status=status, done=done == "1")


@router.post("/settle/adjust", response_class=HTMLResponse)
async def adjust_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    """차이 하나의 처리. 확인함, 보험사 문의, 해결 가운데 하나와 메모가 있어야 한다. 규칙은 settle.set_diff_status 한 곳이다."""
    form = await request.form()
    ym, raw, action = str(form.get("ym") or ""), str(form.get("diff_id") or ""), str(form.get("status") or "")
    note = str(form.get("note") or "")
    if not raw.isdigit():
        raise HTTPException(400, "처리할 차이를 고르세요.")
    diff_id = int(raw)
    kept = {"status": action, "note": note[:NOTE_MAX]}
    if len(note.strip()) > NOTE_MAX:
        return _adjust_page(request, ctx, con, ym, diff=diff_id, error=f"메모는 {NOTE_MAX}자까지 적습니다.", form=kept, status_code=400)
    try:
        st.set_diff_status(con, diff_id, action, note, ctx)
    except PermissionError:
        con.rollback()
        _deny(con, ctx, request, "diff")
    except (ValueError, LookupError) as e:
        con.rollback()
        return _adjust_page(request, ctx, con, ym, diff=diff_id, error=str(e), form=kept, status_code=400)
    return RedirectResponse(f"/settle/adjust?ym={ym}&diff={diff_id}&done=1", status_code=303)


# ---------------------------------------------------------------- 마감
def _close_history(con) -> list:
    rows = [dict(r) for r in con.execute(
        f"""SELECT l.at, l.actor, l.action, l.target, l.after_json, u.display_name FROM audit_log l LEFT JOIN user_account u ON u.login_id=l.actor
            WHERE l.action IN ({','.join('?' * len(CLOSE_ACTIONS))}) ORDER BY l.id DESC LIMIT 40""", CLOSE_ACTIONS)]
    for r in rows:
        try:
            r["reason"] = (json.loads(r["after_json"] or "{}") or {}).get("reason")
        except (ValueError, AttributeError):
            r["reason"] = None
        r["via"] = "기존 도구" if r["actor"] == "ui" else "웹"
    return rows


def _close_page(request, ctx, con, ym, *, ask=None, error=None, form=None, done=None, status_code=200):
    ym, yms = pick_ym(con, ctx, ym)
    row = closed_row(con, ym)
    closed = bool(row and row["status"] == "closed")
    check = st.close_check(con, ym)
    ask = ask if ask in ("close", "reopen") else None
    if (ask == "close" and (closed or check["block"])) or (ask == "reopen" and not closed):
        ask = None
    states = {r["period_ym"]: r["status"] for r in con.execute("SELECT period_ym, status FROM close_period")}
    return page(request, ctx, "settle_close.html", status_code=status_code, row=row, closed=closed, check=check, ask=ask, error=error,
                form=form or {}, done=done, history=_close_history(con), yms=yms, ym=ym, states=states, stamps=stamps(con, ym),
                NOTE_MAX=NOTE_MAX, f={"ym": ym})


@router.get("/settle/close", response_class=HTMLResponse)
def close(request: Request, ym: str | None = None, ask: str = "", done: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    return _close_page(request, ctx, con, ym, ask=ask, done=done if done in ("close", "reopen") else None)


@router.post("/settle/close", response_class=HTMLResponse)
async def close_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    """마감은 확인 칸(agree=yes)이 있어야 하고 막는 조건이 없어야 한다. 해제는 사유가 있어야 한다. 둘 다 메인 관리자만이다."""
    form = await request.form()
    action, ym = str(form.get("action") or ""), str(form.get("ym") or "")
    if action not in ("close", "reopen") or ym not in months(con, ctx):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    if ctx["role"] != "main":                        # guard 가 이미 막는다. 쓰기는 한 번 더 본다
        _deny(con, ctx, request, action)
    if action == "close":
        if is_closed(con, ym):
            return _close_page(request, ctx, con, ym, error="이미 마감한 달입니다.", status_code=400)
        if form.get("agree") != "yes":
            return _close_page(request, ctx, con, ym, ask="close", error="되돌리기 어렵다는 것을 확인하는 칸에 표시해야 마감합니다.", status_code=400)
        if st.close_check(con, ym)["block"]:
            return _close_page(request, ctx, con, ym, error="마감을 막는 일이 남아 있습니다. 아래 목록을 먼저 처리하세요.", status_code=400)
        try:
            st.close_month(con, ym, ctx)
        except PermissionError:
            con.rollback()
            _deny(con, ctx, request, action)
        except ValueError as e:
            con.rollback()
            return _close_page(request, ctx, con, ym, error=str(e), status_code=400)
        return RedirectResponse(f"/settle/close?ym={ym}&done=close", status_code=303)
    reason = str(form.get("reason") or "").strip()
    if not is_closed(con, ym):
        return _close_page(request, ctx, con, ym, error="마감하지 않은 달입니다.", status_code=400)
    if not reason:
        return _close_page(request, ctx, con, ym, ask="reopen", error="해제 사유를 적어 주세요.", status_code=400)
    if len(reason) > NOTE_MAX:
        return _close_page(request, ctx, con, ym, ask="reopen", error=f"해제 사유는 {NOTE_MAX}자까지 적습니다.", form={"reason": reason[:NOTE_MAX]},
                           status_code=400)
    try:
        st.reopen_month(con, ym, ctx, reason)
    except PermissionError:
        con.rollback()
        _deny(con, ctx, request, action)
    except ValueError as e:
        con.rollback()
        return _close_page(request, ctx, con, ym, ask="reopen", error=str(e), form={"reason": reason[:NOTE_MAX]}, status_code=400)
    return RedirectResponse(f"/settle/close?ym={ym}&done=reopen", status_code=303)


# ---------------------------------------------------------------- 정산서와 유지율 올리기
def statement_insurer_options(con) -> list:
    """정산서 매핑 파일이 있는 보험사. 매핑이 없으면 적재기가 읽지 못한다."""
    return [dict(r) for r in con.execute("SELECT insurer_id, code, name FROM insurer ORDER BY name")
            if (MAPPING_DIR / f"{r['code']}_statement.yaml").exists()]


def _batches(con, limit=30) -> list:
    rows = [dict(r) for r in con.execute(
        """SELECT b.batch_id, b.data_type, b.period_ym, b.version, b.row_count, b.status, b.loaded_at, b.received_at, i.name insurer,
                  SUM(q.severity='block') block, SUM(q.severity='warn') warn
           FROM import_batch b LEFT JOIN insurer i ON i.insurer_id=b.insurer_id LEFT JOIN quality_issue q ON q.batch_id=b.batch_id
           WHERE b.data_type IN ('statement','persistency') GROUP BY b.batch_id ORDER BY b.batch_id DESC LIMIT ?""", (limit,))]
    for r in rows:
        r["kind_ko"] = KIND_KO.get(r["data_type"], r["data_type"])
        r["status_ko"] = BATCH_STATUS_KO.get(r["status"], r["status"])
    return rows


def _issues(con, batch_id) -> list:
    """문제를 종류별로 묶는다. 건수와 줄 번호 여덟 개까지. 메시지는 열 없음만 싣는다(열 이름뿐이다)."""
    groups: dict = {}
    for r in con.execute("SELECT severity, check_code, row_no, message FROM quality_issue WHERE batch_id=? ORDER BY id", (batch_id,)):
        g = groups.setdefault((r["severity"], r["check_code"]), {"severity": r["severity"], "code": r["check_code"],
                                                                 "label": ISSUE_KO.get(r["check_code"], r["check_code"]), "count": 0, "rows": [], "detail": None})
        g["count"] += 1
        if r["row_no"] is not None and len(g["rows"]) < 8:
            g["rows"].append(r["row_no"])
        if r["check_code"] == "missing_column":
            g["detail"] = r["message"]
    return sorted(groups.values(), key=lambda g: (g["severity"] != "block", -g["count"]))


def _run_loader(con, path, kind, code, ym, by) -> dict:
    if kind == "statement":
        return excel_loader.load_statement(con, path, code, ym, by=by)
    return excel_loader.load_persistency(con, path, ym, by=by)


def _result(con, res) -> dict:
    b = con.execute("SELECT status FROM import_batch WHERE batch_id=?", (res["batch_id"],)).fetchone()
    return {"rows": res.get("rows", 0), "block": res.get("block", 0), "warn": res.get("warn", 0), "batch_id": res["batch_id"],
            "status": b["status"] if b else None, "status_ko": BATCH_STATUS_KO.get(b["status"], b["status"]) if b else "",
            "issues": _issues(con, res["batch_id"])}


def dry_run(con, path, kind, code, ym, by) -> dict:
    """미리보기. DB 를 메모리에 복사해 같은 적재기를 돌리고 버린다. 원래 DB 에는 아무것도 쓰지 않는다."""
    mem = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        con.commit()
        con.backup(mem)
        mem.row_factory = sqlite3.Row
        mem.execute("PRAGMA foreign_keys = ON")
        replaces = 0
        if kind == "statement":
            replaces = mem.execute("""SELECT COUNT(*) FROM import_batch b JOIN insurer i ON i.insurer_id=b.insurer_id
                                      WHERE b.data_type='statement' AND b.period_ym=? AND i.code=? AND b.status='active'""", (ym, code)).fetchone()[0]
        out = _result(mem, _run_loader(mem, path, kind, code, ym, by))
        out["replaces"] = replaces
        return out
    finally:
        mem.close()


def _upload_path(token) -> pathlib.Path | None:
    if not token or not TOKEN.match(token):
        return None
    d = upload_dir() / f"settle_{token}"
    files = [p for p in d.iterdir() if p.is_file()] if d.is_dir() else []
    return files[0] if len(files) == 1 else None


def _drop_upload(token) -> None:
    p = _upload_path(token)
    if p is not None:
        p.unlink(missing_ok=True)
        try:
            p.parent.rmdir()
        except OSError:
            pass


def _safe_name(name: str) -> str:
    base = pathlib.Path(str(name or "")).name.strip() or "statement.xlsx"
    stem, suffix = base.rsplit(".", 1) if "." in base else (base, "")
    stem = re.sub(r"[^0-9A-Za-z가-힣._ -]", "_", stem)[:80].strip(" .") or "statement"
    return f"{stem}.{suffix.lower()}"


def _statement_page(request, ctx, con, *, preview=None, result=None, error=None, form=None, status_code=200):
    saved = request.session.get(UPLOAD_KEY) or {}
    resp = page(request, ctx, "settle_statement.html", status_code=status_code, insurers=statement_insurer_options(con), batches=_batches(con),
                preview=preview, result=result, error=error, form=form or {"kind": "statement", "ym": ctx["ym"]}, saved=saved if preview else {},
                KIND_KO=KIND_KO, yms=months(con, ctx), closed_months={r[0] for r in con.execute("SELECT period_ym FROM close_period WHERE status='closed'")})
    return resp


@router.get("/data/statement", response_class=HTMLResponse)
def statement(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return _statement_page(request, ctx, con)


async def _statement_preview(request, ctx, con, form):
    kind, code, ym = str(form.get("kind") or ""), str(form.get("insurer") or ""), str(form.get("ym") or "")
    kept = {"kind": kind, "insurer": code, "ym": ym}
    fail = lambda msg: _statement_page(request, ctx, con, error=msg, form=kept, status_code=400)      # noqa: E731
    if kind not in KIND_KO:
        return fail("올릴 자료의 종류를 고르세요.")
    if not YM.match(ym):
        return fail("귀속월은 202610 처럼 여섯 자리로 적어 주세요.")
    if kind == "statement" and code not in {i["code"] for i in statement_insurer_options(con)}:
        return fail("정산서의 보험사를 고르세요.")
    if is_closed(con, ym):
        return fail("마감한 달에는 자료를 넣을 수 없습니다. 마감 화면에서 해제한 뒤 넣으세요.")
    up = form.get("file")
    if up is None or not getattr(up, "filename", ""):
        return fail("올릴 엑셀 파일을 고르세요.")
    name = _safe_name(up.filename)
    if not name.endswith(".xlsx"):
        return fail("엑셀(xlsx) 파일만 받습니다.")
    token = secrets.token_urlsafe(24)
    d = upload_dir() / f"settle_{token}"
    d.mkdir(parents=True, exist_ok=True)
    path, size = d / name, 0
    with open(path, "wb") as f:                         # 조각으로 받아 큰 파일도 메모리에 다 올리지 않는다
        while chunk := await up.read(CHUNK):
            size += len(chunk)
            if size > MAX_BYTES:
                break
            f.write(chunk)
    if size > MAX_BYTES:
        _drop_upload(token)
        return fail(f"파일이 너무 큽니다. {MAX_BYTES // 1024 // 1024}MB 까지 받습니다.")
    _drop_upload((request.session.get(UPLOAD_KEY) or {}).get("token"))
    request.session.pop(UPLOAD_KEY, None)
    if size > INLINE_BYTES:
        return _statement_enqueue(request, ctx, con, token, name, kind, code, ym, fail)
    try:
        pv = dry_run(con, path, kind, code if kind == "statement" else None, ym, ctx["login_id"])
    except excel_loader.DuplicateFile:
        _drop_upload(token)
        return fail("이미 넣은 파일입니다.")
    except (FileNotFoundError, LookupError):
        _drop_upload(token)
        return fail("이 달에 맞는 열 매핑이 없습니다. 매핑 파일을 먼저 준비해야 합니다.")
    except Exception:                                   # 엑셀이 아니거나 깨진 파일
        _drop_upload(token)
        return fail("파일을 읽지 못했습니다. 보험사 정산서나 유지율 현황 엑셀인지 확인하세요.")
    pv.update(file=name, kind=kind, kind_ko=KIND_KO[kind], ym=ym, insurer=next((i["name"] for i in statement_insurer_options(con) if i["code"] == code), None))
    if pv["status"] == "failed":
        _drop_upload(token)
        return _statement_page(request, ctx, con, preview=pv, form=kept, status_code=400)
    request.session[UPLOAD_KEY] = {"token": token, "kind": kind, "code": code if kind == "statement" else "", "ym": ym, "file": name}
    return _statement_page(request, ctx, con, preview=pv, form=kept)


def _statement_load(request, ctx, con):
    saved = request.session.get(UPLOAD_KEY) or {}
    path = _upload_path(saved.get("token"))
    if path is None or saved.get("kind") not in KIND_KO:
        return _statement_page(request, ctx, con, error="미리보기한 파일이 없습니다. 파일을 다시 올려 미리보기부터 하세요.", status_code=400)
    kind, code, ym = saved["kind"], saved.get("code") or None, saved["ym"]
    if is_closed(con, ym):
        return _statement_page(request, ctx, con, error="마감한 달에는 자료를 넣을 수 없습니다.", status_code=400)
    try:
        res = _run_loader(con, path, kind, code, ym, ctx["login_id"])
    except excel_loader.DuplicateFile:
        con.rollback()
        request.session.pop(UPLOAD_KEY, None)
        _drop_upload(saved.get("token"))
        return _statement_page(request, ctx, con, error="이미 넣은 파일입니다.", status_code=400)
    request.session.pop(UPLOAD_KEY, None)               # 넣은 파일은 원본으로 남긴다. 세션의 표지만 지운다
    out = _result(con, res)
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (db.now(), ctx["login_id"], "statement_load", f"batch:{res['batch_id']}",
                 json.dumps({"kind": kind, "insurer": code, "ym": ym, "rows": out["rows"], "block": out["block"], "warn": out["warn"]}, ensure_ascii=False)))
    con.commit()
    if kind == "statement" and out["status"] != "failed":    # 작업으로 넣을 때와 같이 그 달 대사를 다시 돌리게 한다
        jobs.enqueue(con, "recon_run", {"ym": ym}, by=ctx["login_id"])
    out.update(file=saved.get("file"), kind=kind, kind_ko=KIND_KO[kind], ym=ym)
    return _statement_page(request, ctx, con, result=out)


def _statement_enqueue(request, ctx, con, token, name, kind, code, ym, fail):
    """큰 파일. 미리보기 없이 큰 적재 작업으로 넣고 /data/jobs 로 보낸다. 파일은 업로드 구역에 남겨 워커가 읽는다."""
    params = {"area": "uploads", "key": f"settle_{token}/{name}", "kind": kind, "ym": ym, "by": ctx["login_id"]}
    if kind == "statement":
        params["insurer"] = code
    try:
        jid = jobs.enqueue(con, "ingest", params, by=ctx["login_id"])
    except jobs.JobError:
        _drop_upload(token)
        return fail("작업으로 넣지 못했습니다. 파일을 다시 올려 주세요.")
    auth.log(con, ctx["login_id"], "job_enqueue", f"ingest:{jid}")
    return RedirectResponse("/data/jobs?added=1", status_code=303)


@router.post("/data/statement", response_class=HTMLResponse)
async def statement_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    action = form.get("action")
    if action == "preview":
        return await _statement_preview(request, ctx, con, form)
    if action == "load":
        return _statement_load(request, ctx, con)
    if action == "cancel":
        _drop_upload((request.session.pop(UPLOAD_KEY, None) or {}).get("token"))
        return RedirectResponse("/data/statement", status_code=303)
    raise HTTPException(400, "처리할 수 없는 요청입니다.")
