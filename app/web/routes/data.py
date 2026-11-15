"""데이터 묶음의 계약 엑셀, 시상안, 품질검증, 적재 이력. 메인 관리자만 쓴다. 기존 운영 도구(app/ui.py)의 데이터 수집 화면을 옮겼다.

| 주소 | 하는 일 |
| --- | --- |
| /data/contracts | 계약현황 엑셀을 미리보기한 뒤 적재한다. 실제 양식(eternal_contract)과 샘플 양식(internal_org_contract)을 받는다 |
| /data/plan | 보험사별 시상안을 미리보기한 뒤 원장에 쌓고, 원장 줄을 보고 승인하거나 반려한다 |
| /data/quality | quality_issue 의 차단과 경고 목록. 처리 표시와 그 기록 |
| /data/batches | import_batch 의 파일별 적재 결과와 계약현황 재적재 |

- 미리보기는 DB 를 메모리에 복사한 사본에서 실제 적재기를 돌리고 사본을 버린다. 그래서 미리보기와 적재의 건수가 같고, 원래 DB 에는 쓰지 않는다.
- 올린 파일은 화면 업로드 구역(APP_UPLOAD_DIR)에 두었다가 적재하면 지운다. 적재하는 파일은 원본 구역(data/raw)에 보관하고 지우지 않는다.
- 같은 파일(해시)은 미리보기 전에 막는다. 실제 양식의 큰 파일(BIG_ROWS 줄 넘음)은 작업 워커의 큰 적재(ingest)로 넣고 /data/jobs 에서 본다.
- 적재 뒤에는 업적 run 과 랭킹 run 을 작업으로 넣는다. 계산은 워커가 한다.
- 화면에는 건수와 합계만 보인다. 계약 한 줄의 이름과 금액은 보이지 않는다. 품질 문제 글은 이름과 금액이 없게 만든 적재기
  (SAFE_SOURCES)의 것만 그대로 보이고, 나머지는 검사 이름만 보인다.
- 재적재는 계약현황만 하고 확인 단계를 거친다. 이전 배치는 superseded 로 두고 지우지 않는다. 원본 파일, 원본 행 보관 표, 품질 기록도 그대로다.
  import_batch.file_hash 가 UNIQUE 라 이전 배치의 해시 칸 뒤에 '~superseded~<배치 번호>' 를 붙여 같은 파일 막기를 비켜 간다(가정).
  앞 파일로 원장을 되돌리지 않도록 그 뒤에 들어온 계약현황 배치가 있으면 재적재하지 않는다.
- 시상안 해석 보조(AI)는 S4 다. 지금은 사람이 줄을 확인하고 승인한다. 금액이 원문 그림에만 있는 줄은 '원문 확인' 으로 둔다.
"""
import hashlib
import pathlib
import re
import secrets
import sqlite3

import openpyxl
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import db
from app.connect.base import DUPLICATE, LOADED, VIA_UPLOAD, RawFile
from app.connect.pipeline import Pipeline
from app.connect.raw_store import default_data_dir, store_raw
from app.loader import eternal_contract as ec
from app.loader import excel_loader, mapping
from app.loader import incentive_plan as ip
from app.loader.sheet import HeaderMismatch
from app.ops import jobs
from app.ops.storage import get_storage
from app.web import auth
from app.web.deps import get_con, guard, page

router = APIRouter()
YM = re.compile(r"^20\d\d(0[1-9]|1[0-2])$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
MAX_BYTES = 50 * 1024 * 1024
BIG_ROWS = 3000                     # 실제 양식에서 이 줄 수를 넘으면 화면에서 적재하지 않고 작업 워커로 넣는다
CONTRACT_KEY, PLAN_KEY = "contract_upload", "plan_upload"
PLAN_SUFFIX = (".xlsx", ".xlsm", ".json", ".txt")

FORMATS = {"real": "실제 양식", "sample": "샘플 양식"}
FORMAT_SOURCE = {"real": "eternal_contract@", "sample": "internal_org_contract@"}
# 품질 문제 글에 이름과 금액을 넣지 않게 만든 적재기. 이 적재기의 배치는 글을 그대로 보여 준다
SAFE_SOURCES = ("eternal_contract@", "eternal_employee@", "edu_roster@")
# 그 밖의 적재기에서도 글에 이름과 금액이 들지 않는 검사
SAFE_CODES = {"missing_column", "required_blank", "unknown_category", "unmapped_product", "unmapped_insurer", "bad_status",
              "missing_contract_date", "duplicate_key", "org_mismatch", "category_mismatch", "sign_aligned", "dropped_column", "bad_ratio"}
CODE_KO = {
    "missing_column": "필수 열 없음", "required_blank": "필수 값 없음", "bad_value": "읽을 수 없는 값", "bad_row": "넣지 않은 줄",
    "total_mismatch": "합계 대조 다름", "duplicate_key": "겹친 키", "unmapped_agent": "마스터에 없는 사번", "name_mismatch": "이름 불일치",
    "org_mismatch": "업로드 본부 밖 소속", "unmapped_insurer": "마스터에 없는 보험사", "bad_status": "상태 값 오류",
    "unknown_category": "구분 미확인", "unmapped_product": "마스터에 없는 상품", "category_mismatch": "구분 불일치",
    "recognized_mismatch": "인정보험료 확인", "missing_contract_date": "계약일 없음", "negative_amount": "음수 금액",
    "sign_aligned": "철회 부호 맞춤", "dropped_column": "버린 민감 열", "org_blank": "조직 빈칸", "org_gap": "조직 경로 빈칸",
    "unknown_sector": "업권 미확인", "category_sector_mismatch": "분류표와 업권 다름", "unknown_payment_mode": "납입주기 미확인",
    "blank_agent": "사번 빈칸", "unmapped_collector": "수금사원 미확인", "stale_row": "원장보다 이전 값", "bad_ratio": "유지율 비율 오류",
    "volume_jump": "줄 수 급변", "header": "머리글", "duplicate_in_file": "파일 안 중복", "duplicate_existing": "이미 있는 수료",
}
HIDDEN_TEXT = "이 검사의 원래 글에는 이름이나 금액이 있어 가렸습니다."
SEV_KO = {"block": "차단", "warn": "경고"}
TYPE_KO = {"org_contract": "계약현황", "statement": "정산서", "persistency": "유지율", "employee": "사원현황", "edu_roster": "수료자 명부"}
TYPE_LINK = {"statement": "/data/statement", "employee": "/data/staff", "edu_roster": "/edu/completions"}
BATCH_STATUS_KO = {"active": "적재함", "failed": "막힘", "superseded": "대체됨", "received": "받음", "loaded": "적재함"}
CONTRACT_STATUS_KO = {"applied": "청약", "inforce": "정상", "withdrawn": "철회", "declined": "반송", "lapsed": "실효",
                      "cancelled": "해지", "matured": "만기"}
BASIS_KO = {"initial_premium": "월초", "converted_premium": "월납환산", "monthly_premium": "월납", "paid_premium": "입금보험료",
            "base_premium": "기본보험료", "premium": "보험료", "unspecified": "기준 미표기"}
REVIEW_KO = {"draft": "검토 전", "approved": "승인", "rejected": "반려"}
SUPERSEDED_MARK = "~superseded~"


# ---------------------------------------------------------------- 공통 도우미
def _rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def upload_dir() -> pathlib.Path:
    d = get_storage().root("uploads")                    # APP_UPLOAD_DIR, 없으면 data/raw/uploads
    d.mkdir(parents=True, exist_ok=True)
    return d


def _upload(prefix: str, saved: dict | None) -> pathlib.Path | None:
    saved = saved or {}
    tok, suffix = saved.get("token"), saved.get("suffix") or ".xlsx"
    if not tok or not TOKEN.match(tok) or suffix not in PLAN_SUFFIX:
        return None
    p = upload_dir() / f"{prefix}_{tok}{suffix}"
    return p if p.is_file() else None


def _drop(prefix: str, request: Request, key: str) -> None:
    p = _upload(prefix, request.session.get(key))
    if p:
        p.unlink(missing_ok=True)
    request.session.pop(key, None)


def scratch(con) -> sqlite3.Connection:
    """미리보기용 사본. 메모리에 DB 를 통째로 복사한다. 쓰고 나면 닫아 버린다."""
    mem = sqlite3.connect(":memory:", check_same_thread=False)
    con.backup(mem)
    mem.row_factory = sqlite3.Row
    mem.execute("PRAGMA foreign_keys = ON")
    return mem


def is_closed(con, ym) -> bool:
    r = con.execute("SELECT status FROM close_period WHERE period_ym=?", (ym,)).fetchone()
    return bool(r and r["status"] == "closed")


def safe_source(mapping_version) -> bool:
    return bool(mapping_version) and str(mapping_version).startswith(SAFE_SOURCES)


def issue_view(issues, safe: bool) -> list:
    """품질 문제를 화면에 보일 꼴로. safe 가 아니면 SAFE_CODES 밖의 글은 가린다."""
    out = []
    for i in issues:
        code = i.get("check_code")
        show = safe or code in SAFE_CODES
        out.append({**i, "code_ko": CODE_KO.get(code, code), "sev_ko": SEV_KO.get(i.get("severity"), i.get("severity")),
                    "text": i.get("message") if show else HIDDEN_TEXT, "hidden": not show})
    return out


async def _read_upload(form, max_bytes=MAX_BYTES):
    f = form.get("file")
    if f is None or not getattr(f, "filename", ""):
        return None, None, "파일을 고르세요."
    data = await f.read(max_bytes + 1)
    if len(data) > max_bytes:
        return None, None, "파일이 너무 큽니다."
    if not data:
        return None, None, "빈 파일입니다."
    return pathlib.Path(f.filename).name[:120], data, None


def _months(con) -> list:
    return [r[0] for r in con.execute("SELECT DISTINCT period_ym FROM import_batch WHERE data_type='org_contract' ORDER BY 1 DESC")]


def _hqs(con) -> list:
    return _rows(con, "SELECT org_id, name FROM org_unit WHERE level='hq' ORDER BY name, org_id")


# ---------------------------------------------------------------- 계약 엑셀: 양식 알아보기와 미리보기
def detect_format(path, ym) -> tuple:
    """(양식, 실제 양식이 아니라면 그 사유 목록). 양식은 real, sample, None."""
    reasons = []
    try:
        pv = ec.preview(path, ym)
        if pv["ok"]:
            return "real", []
        reasons = pv["reasons"]
    except HeaderMismatch as e:
        reasons = e.reasons
    except (LookupError, FileNotFoundError) as e:
        reasons = [str(e)]
    except Exception:
        return None, ["파일을 읽지 못했습니다. 엑셀 파일인지 확인하세요."]
    if _is_sample(path, ym):
        return "sample", []
    return None, reasons


def _is_sample(path, ym) -> bool:
    try:
        m = mapping.load("internal", "org_contract", ym)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return False
    try:
        sheet = m["file"].get("sheet")
        ws = wb[sheet] if sheet in wb.sheetnames else wb.worksheets[0]
        hrow = m["file"].get("header_row", 1)
        row = next(ws.iter_rows(min_row=hrow, max_row=hrow, values_only=True), ())
        heads = {str(v).strip() for v in row if v is not None}
        return all(c in heads for c in m["columns"])
    finally:
        wb.close()


def _status_counts(con, batch_id) -> dict:
    return {r[0]: r[1] for r in con.execute(
        "SELECT status, COUNT(*) FROM contract WHERE last_batch_id=? OR (last_batch_id IS NULL AND source_batch_id=?) GROUP BY 1", (batch_id, batch_id))}


def preview_contract(con, path, ym, fmt, hq, by) -> dict:
    """사본에서 적재기를 돌려 건수, 합계 대조, 문제를 낸다. 원래 DB 는 바꾸지 않는다."""
    mem = scratch(con)
    try:
        if fmt == "real":
            s = ec.load_contract(mem, path, ym, by=by)
            out = {"file_rows": s["file_rows"], "loaded": s["loaded"], "skipped": s["skipped"], "stale": s["stale"],
                   "inserted": s["inserted"], "updated": s["updated"], "refreshed": s["refreshed"],
                   "file_sum": s["file_recognized_sum"], "aligned_sum": s["file_recognized_sum_aligned"], "loaded_sum": s["loaded_recognized_sum"],
                   "sign_fixed": s["sign_fixed"], "dropped": s["dropped_columns"], "category_map": s["category_map"]}
        else:
            s = excel_loader.load_org_contract(mem, path, ym, int(hq), by=by)
            new_sum = mem.execute("SELECT COALESCE(SUM(converted_premium),0) FROM contract WHERE source_batch_id=?", (s["batch_id"],)).fetchone()[0]
            out = {"file_rows": s["rows"], "loaded": s["rows"] - s["skipped"], "skipped": s["skipped"], "stale": 0,
                   "inserted": s["inserted"], "updated": s["updated"], "refreshed": None, "new_sum": new_sum, "dropped": []}
        bid = s["batch_id"]
        issues = [dict(r) for r in mem.execute(
            "SELECT severity, check_code, row_no, message FROM quality_issue WHERE batch_id=? ORDER BY severity, row_no", (bid,))]
        out.update(block=s["block"], warn=s["warn"], fmt=fmt, issues=issue_view(issues, fmt == "real"),
                   total_ok=not any(i["check_code"] == "total_mismatch" for i in issues),
                   batch_status=mem.execute("SELECT status FROM import_batch WHERE batch_id=?", (bid,)).fetchone()[0],
                   by_status=_status_counts(mem, bid))
        if fmt == "real":
            out["sum_diff"] = out["loaded_sum"] - out["aligned_sum"]
        return out
    finally:
        mem.close()


def _contracts_page(request, ctx, con, status_code=200, **kw):
    saved = request.session.get(CONTRACT_KEY) or {}
    recent = _rows(con, """SELECT b.batch_id, b.period_ym, b.version, b.file_name, b.status, b.row_count, b.loaded_at, b.mapping_version, o.name org,
                                  (SELECT COUNT(*) FROM quality_issue q WHERE q.batch_id=b.batch_id AND q.severity='block') block,
                                  (SELECT COUNT(*) FROM quality_issue q WHERE q.batch_id=b.batch_id AND q.severity='warn') warn
                           FROM import_batch b LEFT JOIN org_unit o ON o.org_id=b.upload_org_id
                           WHERE b.data_type='org_contract' ORDER BY b.batch_id DESC LIMIT 8""")
    for r in recent:
        r["fmt_ko"] = next((FORMATS[k] for k, v in FORMAT_SOURCE.items() if str(r["mapping_version"] or "").startswith(v)), "-")
    last = con.execute("SELECT MAX(loaded_at) FROM import_batch WHERE data_type='org_contract' AND status!='superseded'").fetchone()[0]
    kw.setdefault("preview", None)
    kw.setdefault("result", None)
    kw.setdefault("err", None)
    kw.setdefault("form", {"ym": saved.get("ym") or auth.current_ym(con), "fmt": saved.get("fmt") or "auto", "hq": saved.get("hq")})
    return page(request, ctx, "data_contracts.html", status_code=status_code, recent=recent, last_load=last, hqs=_hqs(con),
                FORMATS=FORMATS, BIG_ROWS=BIG_ROWS, STATUS_KO=CONTRACT_STATUS_KO, BATCH_STATUS_KO=BATCH_STATUS_KO, saved=saved, **kw)


@router.get("/data/contracts", response_class=HTMLResponse)
def contracts_page(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return _contracts_page(request, ctx, con)


@router.post("/data/contracts", response_class=HTMLResponse)
async def contracts_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    action = form.get("action")
    by = ctx["login_id"]
    if action == "cancel":
        _drop("contract", request, CONTRACT_KEY)
        return _contracts_page(request, ctx, con)
    if action == "preview":
        return await _contract_preview(request, ctx, con, form, by)
    if action == "apply":
        return _contract_apply(request, ctx, con, by)
    raise HTTPException(400, "처리할 수 없는 요청입니다.")


async def _contract_preview(request, ctx, con, form, by):
    ym = str(form.get("ym") or "").strip()
    fmt_pick = str(form.get("fmt") or "auto")
    hq = str(form.get("hq") or "").strip()
    keep = {"ym": ym, "fmt": fmt_pick, "hq": hq}
    if not YM.match(ym):
        return _contracts_page(request, ctx, con, err="귀속월을 YYYYMM 으로 넣으세요.", form=keep, status_code=400)
    name, data, err = await _read_upload(form)
    if err:
        return _contracts_page(request, ctx, con, err=err, form=keep, status_code=400)
    digest = hashlib.sha256(data).hexdigest()
    dup = con.execute("SELECT batch_id FROM import_batch WHERE file_hash=?", (digest,)).fetchone()
    if dup:
        return _contracts_page(request, ctx, con, err=f"이미 적재한 파일입니다. 배치 {dup[0]}번입니다. 다시 넣으려면 적재 이력에서 재적재하세요.",
                               dup_batch=dup[0], form=keep, status_code=409)
    _drop("contract", request, CONTRACT_KEY)
    token = secrets.token_urlsafe(24)
    path = upload_dir() / f"contract_{token}.xlsx"
    path.write_bytes(data)
    fmt, reasons = detect_format(path, ym)
    if fmt_pick in FORMATS and fmt and fmt != fmt_pick:
        reasons, fmt = [f"고른 양식과 달리 {FORMATS[fmt]} 파일로 보입니다."], None
    if fmt is None:
        path.unlink(missing_ok=True)
        msg = "계약현황 양식이 아닙니다. " + " / ".join(reasons[:3])
        return _contracts_page(request, ctx, con, err=msg, form=keep, status_code=400)
    if fmt == "sample":
        hq_ok = hq.isdigit() and con.execute("SELECT 1 FROM org_unit WHERE org_id=? AND level='hq'", (int(hq),)).fetchone()
        if not hq_ok:
            path.unlink(missing_ok=True)
            return _contracts_page(request, ctx, con, err="샘플 양식은 업로드 본부를 골라야 합니다.", form={**keep, "fmt": "sample"}, status_code=400)
    try:
        pv = preview_contract(con, path, ym, fmt, hq, by)
    except Exception:
        path.unlink(missing_ok=True)
        return _contracts_page(request, ctx, con, err="파일을 끝까지 읽지 못했습니다. 양식과 값을 확인하세요.", form=keep, status_code=400)
    big = fmt == "real" and pv["file_rows"] > BIG_ROWS
    request.session[CONTRACT_KEY] = {"token": token, "suffix": ".xlsx", "ym": ym, "fmt": fmt, "hq": hq if fmt == "sample" else "",
                                     "file": name, "hash": digest, "rows": pv["file_rows"], "big": big}
    return _contracts_page(request, ctx, con, preview={**pv, "file": name, "ym": ym, "big": big, "closed": is_closed(con, ym)},
                           form={**keep, "fmt": fmt})


def _contract_apply(request, ctx, con, by):
    saved = request.session.get(CONTRACT_KEY) or {}
    path = _upload("contract", saved)
    if not path:
        return _contracts_page(request, ctx, con, err="미리보기한 파일이 없습니다. 파일을 다시 올려 미리보기부터 하세요.", status_code=400)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    ym, fmt = saved["ym"], saved["fmt"]
    if digest != saved.get("hash"):
        _drop("contract", request, CONTRACT_KEY)
        return _contracts_page(request, ctx, con, err="미리보기한 파일과 다릅니다. 다시 올려 주세요.", status_code=400)
    dup = con.execute("SELECT batch_id FROM import_batch WHERE file_hash=?", (digest,)).fetchone()
    if dup:
        _drop("contract", request, CONTRACT_KEY)
        return _contracts_page(request, ctx, con, err=f"이미 적재한 파일입니다. 배치 {dup[0]}번입니다.", status_code=409)
    if is_closed(con, ym):
        return _contracts_page(request, ctx, con, err="마감한 달이라 적재하지 않습니다. 마감을 풀고 다시 하세요.", status_code=400)
    raw = RawFile(name=saved.get("file") or "contract.xlsx", data=data, via=VIA_UPLOAD, kind="contract", period_ym=ym, meta={"by": by})
    result = {"file": raw.name, "ym": ym, "fmt": fmt}
    if fmt == "real" and saved.get("big"):
        _, rel = store_raw(raw)
        try:
            job_id = jobs.enqueue(con, "ingest", {"area": "raw", "key": rel.split("/", 1)[1], "kind": "contract", "ym": ym, "by": by}, by=by)
        except jobs.JobError as e:
            return _contracts_page(request, ctx, con, err=str(e), status_code=400)
        auth.log(con, by, "contract_upload_job", f"job:{job_id} rows:{saved.get('rows')}")
        result["job_id"] = job_id
    elif fmt == "real":
        res = Pipeline(con).ingest(raw)
        batch_id = (res.detail or {}).get("batch_id")
        if res.status == DUPLICATE or not batch_id:
            msg = "이미 적재한 파일입니다." if res.status == DUPLICATE else f"적재하지 못했습니다. {res.reason}"
            return _contracts_page(request, ctx, con, err=msg, status_code=400)
        result.update(batch_id=batch_id, loaded=res.status == LOADED)
    else:
        stored, rel = store_raw(raw)
        try:
            s = excel_loader.load_org_contract(con, stored, ym, int(saved["hq"]), by=by)
        except excel_loader.DuplicateFile:
            return _contracts_page(request, ctx, con, err="이미 적재한 파일입니다.", status_code=409)
        con.execute("UPDATE import_batch SET received_via=?, file_path=? WHERE batch_id=?", (VIA_UPLOAD, rel, s["batch_id"]))
        con.commit()
        result.update(batch_id=s["batch_id"])
    if result.get("batch_id"):
        b = batch_row(con, result["batch_id"])
        result.update(batch=b, loaded=b["status"] == "active")
        result["follow"] = jobs.enqueue_refresh(con, ym, by=by)
        auth.log(con, by, "contract_upload", f"batch:{result['batch_id']} rows:{b['row_count']}")
    path.unlink(missing_ok=True)
    request.session.pop(CONTRACT_KEY, None)
    return _contracts_page(request, ctx, con, result=result)


# ---------------------------------------------------------------- 적재 이력과 재적재
def batch_row(con, batch_id) -> dict | None:
    r = con.execute("""SELECT b.*, i.name insurer, o.name org,
                              (SELECT COUNT(*) FROM quality_issue q WHERE q.batch_id=b.batch_id AND q.severity='block') block,
                              (SELECT COUNT(*) FROM quality_issue q WHERE q.batch_id=b.batch_id AND q.severity='warn') warn,
                              (SELECT COUNT(*) FROM quality_issue q WHERE q.batch_id=b.batch_id AND q.resolved=0) open
                       FROM import_batch b LEFT JOIN insurer i ON i.insurer_id=b.insurer_id LEFT JOIN org_unit o ON o.org_id=b.upload_org_id
                       WHERE b.batch_id=?""", (batch_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["type_ko"] = TYPE_KO.get(d["data_type"], d["data_type"])
    d["status_ko"] = BATCH_STATUS_KO.get(d["status"], d["status"])
    d["fmt"] = next((k for k, v in FORMAT_SOURCE.items() if str(d["mapping_version"] or "").startswith(v)), None)
    d["source"] = d["insurer"] or d["org"] or "내부"
    return d


def _raw_roots() -> list:
    roots = {default_data_dir() / "raw", get_storage().root("raw"), db.ROOT / "data" / "raw"}
    return [r.resolve() for r in roots]


def source_path(file_path) -> pathlib.Path | None:
    """배치의 원본 파일. 원본 구역 안에 있는 파일만 돌려준다. 그 밖의 경로는 열지 않는다."""
    if not file_path:
        return None
    p = pathlib.Path(file_path)
    if not p.is_absolute():
        p = default_data_dir() / p
    try:
        p = p.resolve()
    except OSError:
        return None
    if not any(p.is_relative_to(r) for r in _raw_roots()):
        return None
    return p if p.is_file() else None


def reload_check(con, b) -> tuple:
    """(재적재할 수 있는가, 할 수 없다면 그 까닭)"""
    if b["data_type"] != "org_contract":
        return False, "이 화면에서는 계약현황만 다시 적재합니다."
    if b["status"] == "superseded":
        return False, "이미 다른 배치로 대체된 배치입니다."
    if b["fmt"] is None:
        return False, "어느 양식으로 적재했는지 알 수 없어 다시 적재하지 않습니다."
    later = con.execute("""SELECT MIN(batch_id) FROM import_batch WHERE data_type='org_contract' AND status!='superseded'
                           AND batch_id>? AND upload_org_id IS ?""", (b["batch_id"], b["upload_org_id"])).fetchone()[0]
    if later:
        return False, f"그 뒤에 들어온 계약현황 배치 {later}번이 있습니다. 앞 파일로 원장을 되돌리지 않도록 다시 적재하지 않습니다."
    if is_closed(con, b["period_ym"]):
        return False, "마감한 달입니다."
    if source_path(b["file_path"]) is None:
        return False, "보관한 원본 파일을 찾지 못했습니다."
    return True, ""


def reload_batch(con, b, by) -> dict:
    """계약현황 배치 하나를 보관한 원본으로 다시 적재한다. 이전 배치는 대체됨으로 두고 지우지 않는다.

    이전 배치의 표시는 커밋하기 전에 적재기를 부른다. 적재기가 배치를 만들기 전에 막히면 되돌려 이전 배치가 그대로 남는다.
    """
    path = source_path(b["file_path"])
    bid = b["batch_id"]
    con.execute("UPDATE import_batch SET status='superseded', file_hash=?, note=? WHERE batch_id=?",
                (f"{b['file_hash']}{SUPERSEDED_MARK}{bid}", f"재적재로 대체함 {db.now()} {by}", bid))
    try:
        if b["fmt"] == "real":
            s = ec.load_contract(con, path, b["period_ym"], upload_org_id=b["upload_org_id"], by=by)
        else:
            s = excel_loader.load_org_contract(con, path, b["period_ym"], b["upload_org_id"], by=by)
    except Exception:
        con.rollback()
        raise
    new = s["batch_id"]
    con.execute("UPDATE import_batch SET received_via=?, file_path=? WHERE batch_id=?", ("reload", b["file_path"], new))
    con.execute("UPDATE import_batch SET note=? WHERE batch_id=?", (f"재적재로 대체함 {db.now()} {by}. 새 배치 {new}", bid))
    con.commit()
    return {"old": bid, "new": new}


@router.get("/data/batches", response_class=HTMLResponse)
def batches_page(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    qp = request.query_params
    f = {"type": qp.get("type") or "", "status": qp.get("status") or ""}
    sel = None
    if (qp.get("id") or "").isdigit():
        sel = batch_row(con, int(qp["id"]))
        if sel is None:
            raise HTTPException(404, "없는 배치입니다.")
    return _batches_page(request, ctx, con, f, sel, confirm=qp.get("confirm") == "reload", done=qp.get("done"))


def _batches_page(request, ctx, con, f, sel=None, status_code=200, **kw):
    where, args = ["1=1"], []
    if f["type"] in TYPE_KO:
        where.append("b.data_type=?")
        args.append(f["type"])
    if f["status"] in BATCH_STATUS_KO:
        where.append("b.status=?")
        args.append(f["status"])
    rows = _rows(con, f"""SELECT b.batch_id, b.data_type, b.period_ym, b.version, b.file_name, b.status, b.row_count, b.loaded_at, b.loaded_by,
                                 COALESCE(i.name, o.name, '내부') source,
                                 (SELECT COUNT(*) FROM quality_issue q WHERE q.batch_id=b.batch_id AND q.severity='block') block,
                                 (SELECT COUNT(*) FROM quality_issue q WHERE q.batch_id=b.batch_id AND q.severity='warn') warn
                          FROM import_batch b LEFT JOIN insurer i ON i.insurer_id=b.insurer_id LEFT JOIN org_unit o ON o.org_id=b.upload_org_id
                          WHERE {' AND '.join(where)} ORDER BY b.batch_id DESC LIMIT 300""", args)
    counts = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM import_batch GROUP BY 1")}
    detail = None
    if sel:
        ok, why = reload_check(con, sel)
        detail = {"can_reload": ok, "why": why, "has_file": source_path(sel["file_path"]) is not None,
                  "contracts": con.execute("SELECT COUNT(*) FROM contract WHERE last_batch_id=? OR source_batch_id=?",
                                           (sel["batch_id"], sel["batch_id"])).fetchone()[0],
                  "by_code": issue_view(_rows(con, """SELECT severity, check_code, COUNT(*) n, SUM(resolved=0) open FROM quality_issue WHERE batch_id=?
                                                     GROUP BY 1,2 ORDER BY severity, n DESC""", (sel["batch_id"],)), True),
                  "link": TYPE_LINK.get(sel["data_type"]),
                  "replaced_by": _replaced_by(sel)}
    return page(request, ctx, "data_batches.html", status_code=status_code, rows=rows, counts=counts, f=f, sel=sel, detail=detail,
                TYPE_KO=TYPE_KO, BATCH_STATUS_KO=BATCH_STATUS_KO, FORMATS=FORMATS, **{"confirm": False, "done": None, "err": None, **kw})


def _replaced_by(b):
    m = re.search(r"새 배치 (\d+)", b.get("note") or "")
    return int(m.group(1)) if m else None


@router.post("/data/batches", response_class=HTMLResponse)
async def batches_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    if form.get("action") != "reload" or not str(form.get("batch_id") or "").isdigit():
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    b = batch_row(con, int(form["batch_id"]))
    if b is None:
        raise HTTPException(404, "없는 배치입니다.")
    f = {"type": "", "status": ""}
    if form.get("confirm") != "yes":
        return _batches_page(request, ctx, con, f, b, confirm=True, err="재적재를 확인하는 칸을 고르세요. 아직 아무것도 바뀌지 않았습니다.", status_code=400)
    ok, why = reload_check(con, b)
    if not ok:
        return _batches_page(request, ctx, con, f, b, err=why, status_code=400)
    try:
        out = reload_batch(con, b, ctx["login_id"])
    except Exception:
        return _batches_page(request, ctx, con, f, batch_row(con, b["batch_id"]),
                             err="다시 적재하지 못했습니다. 매핑과 원본 양식을 확인하세요. 이전 배치는 그대로 두었습니다.", status_code=400)
    jobs.enqueue_refresh(con, b["period_ym"], by=ctx["login_id"])
    auth.log(con, ctx["login_id"], "contract_reload", f"batch:{out['old']} new:{out['new']}")
    return _batches_page(request, ctx, con, f, batch_row(con, out["new"]), done=f"배치 {out['old']}번을 다시 적재해 배치 {out['new']}번을 만들었습니다. "
                         f"이전 배치는 대체됨으로 남겼습니다. 업적 run 과 랭킹 run 을 작업으로 넣었습니다.")


# ---------------------------------------------------------------- 품질검증
@router.get("/data/quality", response_class=HTMLResponse)
def quality_page(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    qp = request.query_params
    f = {"sev": qp.get("sev") or "", "state": qp.get("state") or "open", "batch": qp.get("batch") or "", "old": qp.get("old") or ""}
    return _quality_page(request, ctx, con, f, msg=qp.get("msg"))


def _quality_page(request, ctx, con, f, status_code=200, **kw):
    where, args = ["1=1"], []
    if f["sev"] in SEV_KO:
        where.append("q.severity=?")
        args.append(f["sev"])
    if f["state"] == "open":
        where.append("q.resolved=0")
    elif f["state"] == "done":
        where.append("q.resolved=1")
    if f["batch"].isdigit():
        where.append("q.batch_id=?")
        args.append(int(f["batch"]))
    elif f["old"] != "1":
        where.append("b.status!='superseded'")
    w = " AND ".join(where)
    rows = _rows(con, f"""SELECT q.id, q.batch_id, q.row_no, q.severity, q.check_code, q.message, q.resolved,
                                 b.file_name, b.data_type, b.period_ym, b.status batch_status, b.mapping_version
                          FROM quality_issue q JOIN import_batch b ON b.batch_id=q.batch_id
                          WHERE {w} ORDER BY q.resolved, q.severity, q.batch_id DESC, q.row_no LIMIT 300""", args)
    total = con.execute(f"SELECT COUNT(*) FROM quality_issue q JOIN import_batch b ON b.batch_id=q.batch_id WHERE {w}", args).fetchone()[0]
    rows = [{**r, **issue_view([r], safe_source(r["mapping_version"]))[0], "type_ko": TYPE_KO.get(r["data_type"], r["data_type"])} for r in rows]
    summary = {k: con.execute(f"""SELECT COUNT(*) FROM quality_issue q JOIN import_batch b ON b.batch_id=q.batch_id
                                  WHERE b.status!='superseded' AND {cond}""").fetchone()[0]
               for k, cond in (("block", "q.severity='block' AND q.resolved=0"), ("warn", "q.severity='warn' AND q.resolved=0"), ("done", "q.resolved=1"))}
    log = _rows(con, "SELECT at, actor, action, target FROM audit_log WHERE action IN ('quality_resolve','quality_reopen') ORDER BY id DESC LIMIT 20")
    for r in log:
        m = re.search(r"n=(\d+)", r["target"] or "")
        r["n"] = int(m.group(1)) if m else None
    return page(request, ctx, "data_quality.html", status_code=status_code, rows=rows, total=total, summary=summary, f=f, log=log,
                SEV_KO=SEV_KO, **{"msg": None, "err": None, **kw})


@router.post("/data/quality", response_class=HTMLResponse)
async def quality_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    action = form.get("action")
    if action not in ("resolve", "reopen"):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    ids = sorted({int(x) for x in form.getlist("ids") if str(x).isdigit()})
    f = {"sev": str(form.get("sev") or ""), "state": str(form.get("state") or "open"), "batch": str(form.get("batch") or ""), "old": str(form.get("old") or "")}
    if not ids:
        return _quality_page(request, ctx, con, f, err="처리할 줄을 고르세요.", status_code=400)
    q = ",".join("?" * len(ids))
    want = 0 if action == "resolve" else 1
    found = [r[0] for r in con.execute(f"SELECT id FROM quality_issue WHERE id IN ({q}) AND resolved=?", (*ids, want))]
    if found:
        q2 = ",".join("?" * len(found))
        con.execute(f"UPDATE quality_issue SET resolved=? WHERE id IN ({q2})", (1 - want, *found))
        con.commit()
        shown = ",".join(map(str, found[:40])) + ("…" if len(found) > 40 else "")
        auth.log(con, ctx["login_id"], "quality_" + action, f"n={len(found)} ids={shown}")
    verb = "처리함으로 표시했습니다" if action == "resolve" else "다시 열었습니다"
    return _quality_page(request, ctx, con, f, msg=f"{len(found)}건을 {verb}.")


# ---------------------------------------------------------------- 시상안
def _plan_docs(con) -> list:
    return _rows(con, """SELECT d.plan_doc_id, d.period_ym, d.title, d.file_name, d.source_kind, d.row_count, d.status, d.loaded_at, d.loaded_by,
                                (SELECT COUNT(*) FROM incentive_plan_line l WHERE l.plan_doc_id=d.plan_doc_id AND l.review_status='approved') approved
                         FROM incentive_plan_doc d ORDER BY d.plan_doc_id DESC LIMIT 60""")


def _components(con, line_ids) -> dict:
    out = {}
    if not line_ids:
        return out
    q = ",".join("?" * len(line_ids))
    for c in con.execute(f"SELECT * FROM incentive_plan_component WHERE line_id IN ({q}) ORDER BY line_id, seq", list(line_ids)):
        rate = float(c["rate"]) * 100
        text = f"{(c['option_label'] + ') ') if c['option_label'] else ''}{BASIS_KO.get(c['basis'], c['basis'])} × {rate:g}%"
        timing = ip.TIMING_KO.get(c["pay_timing"], c["pay_timing"])
        if c["pay_timing"] != "unspecified":
            text += f" · {timing}"
        out.setdefault(c["line_id"], []).append(text)
    return out


def needs_original(line, comps) -> bool:
    """금액이나 지급률이 원문(표나 그림)에만 있는 줄. 숫자를 지어내지 않고 '원문 확인' 으로 둔다."""
    if line["recipient_level"] == "note":
        return False
    return not comps or "그림" in (line.get("parse_note") or "") or not (line.get("reward_raw") or "").strip()


def plan_summary(con, doc_id) -> dict:
    sm = ip.summary(con, doc_id)
    n = lambda st: sum(x["n"] for x in sm["by_kind"] if x["parse_status"] == st)
    return {"doc": sm["doc"], "skipped": sm["skipped"], "auto": n("auto"), "manual": n("manual"), "note": n("note"),
            "insurers": sm["matched_insurer"]["total"], "matched": sm["matched_insurer"]["matched"],
            "by_kind": [{"kind": ip.KIND_KO.get(x["line_kind"], x["line_kind"]), "status": ip.STATUS_KO.get(x["parse_status"], x["parse_status"]), "n": x["n"]}
                        for x in sm["by_kind"]],
            "by_note": sm["by_note"]}


def _plan_page(request, ctx, con, status_code=200, **kw):
    qp = request.query_params
    docs = _plan_docs(con)
    pick = kw.pop("doc_id", None) or (int(qp["doc"]) if (qp.get("doc") or "").isdigit() else None)
    doc = next((d for d in docs if d["plan_doc_id"] == pick), None) or next((d for d in docs if d["status"] != "superseded"), docs[0] if docs else None)
    f = {k: qp.get(k) or "" for k in ("ins", "lv", "kind", "ps", "rv")}
    lines, sm, opts = [], None, {}
    if doc:
        sm = plan_summary(con, doc["plan_doc_id"])
        allrows = _rows(con, """SELECT line_id, sheet, row_no, insurer_name, recipient_level, category_raw, period_raw, criteria_raw, reward_raw,
                                       pay_timing, line_kind, parse_status, parse_note, review_status, reviewed_by, reviewed_at
                                FROM incentive_plan_line WHERE plan_doc_id=? ORDER BY line_id""", (doc["plan_doc_id"],))
        opts = {"ins": sorted({r["insurer_name"] for r in allrows}), "lv": sorted({r["recipient_level"] for r in allrows}),
                "kind": sorted({r["line_kind"] for r in allrows}), "ps": sorted({r["parse_status"] for r in allrows})}
        cmap = {"ins": "insurer_name", "lv": "recipient_level", "kind": "line_kind", "ps": "parse_status", "rv": "review_status"}
        lines = [r for r in allrows if all(not f[k] or r[col] == f[k] for k, col in cmap.items())]
        comps = _components(con, [r["line_id"] for r in lines])
        for r in lines:
            r["comps"] = comps.get(r["line_id"], [])
            r["original"] = needs_original(r, r["comps"])
        sm["approved"] = sum(1 for r in allrows if r["review_status"] == "approved")
        sm["rejected"] = sum(1 for r in allrows if r["review_status"] == "rejected")
    kw.setdefault("preview", None)
    kw.setdefault("msg", None)
    kw.setdefault("err", None)
    saved = request.session.get(PLAN_KEY) or {}
    return page(request, ctx, "data_plan.html", status_code=status_code, docs=docs, doc=doc, sm=sm, lines=lines, f=f, opts=opts,
                form_ym=saved.get("ym") or auth.current_ym(con), KIND_KO=ip.KIND_KO, PS_KO=ip.STATUS_KO, LEVEL_KO=ip.LEVEL_KO,
                REVIEW_KO=REVIEW_KO, **kw)


@router.get("/data/plan", response_class=HTMLResponse)
def plan_page(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return _plan_page(request, ctx, con, msg=request.query_params.get("msg"))


@router.post("/data/plan", response_class=HTMLResponse)
async def plan_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    action = form.get("action")
    by = ctx["login_id"]
    if action == "cancel":
        _drop("plan", request, PLAN_KEY)
        return _plan_page(request, ctx, con)
    if action == "preview":
        return await _plan_preview(request, ctx, con, form, by)
    if action == "apply":
        return _plan_apply(request, ctx, con, by)
    if action in ("approve", "reject"):
        if not str(form.get("doc") or "").isdigit():
            raise HTTPException(400, "처리할 수 없는 요청입니다.")
        doc_id = int(form["doc"])
        ids = sorted({int(x) for x in form.getlist("ids") if str(x).isdigit()})
        if not ids:
            return _plan_page(request, ctx, con, doc_id=doc_id, err="승인하거나 반려할 줄을 고르세요.", status_code=400)
        q = ",".join("?" * len(ids))
        mine = [r[0] for r in con.execute(f"SELECT line_id FROM incentive_plan_line WHERE plan_doc_id=? AND line_id IN ({q})", (doc_id, *ids))]
        for i in mine:
            ip.review(con, i, action == "approve", by)
        if mine:
            auth.log(con, by, f"plan_line_{action}", f"doc:{doc_id} n={len(mine)}")
        verb = "승인했습니다" if action == "approve" else "반려했습니다"
        return _plan_page(request, ctx, con, doc_id=doc_id, msg=f"{len(mine)}줄을 {verb}.")
    raise HTTPException(400, "처리할 수 없는 요청입니다.")


async def _plan_preview(request, ctx, con, form, by):
    ym = str(form.get("ym") or "").strip()
    title = str(form.get("title") or "").strip()[:80]
    if not YM.match(ym):
        return _plan_page(request, ctx, con, err="귀속월을 YYYYMM 으로 넣으세요.", status_code=400)
    name, data, err = await _read_upload(form, 20 * 1024 * 1024)
    if err:
        return _plan_page(request, ctx, con, err=err, status_code=400)
    suffix = pathlib.Path(name).suffix.lower()
    if suffix not in PLAN_SUFFIX:
        return _plan_page(request, ctx, con, err="시상안은 엑셀(.xlsx)이나 드라이브 추출본(.json, .txt)만 받습니다.", status_code=400)
    digest = hashlib.sha256(data).hexdigest()
    dup = con.execute("SELECT plan_doc_id FROM incentive_plan_doc WHERE file_hash=?", (digest,)).fetchone()
    if dup:
        return _plan_page(request, ctx, con, doc_id=dup[0], err=f"이미 적재한 시상안입니다. 시상안 {dup[0]}번입니다.", status_code=409)
    _drop("plan", request, PLAN_KEY)
    token = secrets.token_urlsafe(24)
    path = upload_dir() / f"plan_{token}{suffix}"
    path.write_bytes(data)
    mem = scratch(con)
    try:
        res = ip.load(mem, path, ym, title=title or None, loaded_by=by)
        pv = plan_summary(mem, res["plan_doc_id"])
    except Exception:
        path.unlink(missing_ok=True)
        return _plan_page(request, ctx, con, err="시상안을 읽지 못했습니다. 표준 6열(보험사, 시상대상, 분류, 기간, 달성기준, 시상내용) 양식인지 확인하세요.",
                          status_code=400)
    finally:
        mem.close()
    replaces = con.execute("SELECT COUNT(*) FROM incentive_plan_doc WHERE period_ym=? AND status!='superseded'", (ym,)).fetchone()[0]
    request.session[PLAN_KEY] = {"token": token, "suffix": suffix, "ym": ym, "title": title, "file": name, "hash": digest}
    return _plan_page(request, ctx, con, preview={**pv, "file": name, "ym": ym, "lines": res["lines"], "replaces": replaces})


def _plan_apply(request, ctx, con, by):
    saved = request.session.get(PLAN_KEY) or {}
    path = _upload("plan", saved)
    if not path:
        return _plan_page(request, ctx, con, err="미리보기한 파일이 없습니다. 파일을 다시 올려 미리보기부터 하세요.", status_code=400)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != saved.get("hash"):
        _drop("plan", request, PLAN_KEY)
        return _plan_page(request, ctx, con, err="미리보기한 파일과 다릅니다. 다시 올려 주세요.", status_code=400)
    meta = {"by": by, **({"title": saved["title"]} if saved.get("title") else {})}
    raw = RawFile(name=saved.get("file") or path.name, data=data, via=VIA_UPLOAD, kind="plan", period_ym=saved["ym"], meta=meta)
    res = Pipeline(con).ingest(raw)
    doc_id = (res.detail or {}).get("plan_doc_id")
    if res.status != LOADED or not doc_id:
        msg = "이미 적재한 시상안입니다." if res.status == DUPLICATE else f"적재하지 못했습니다. {res.reason}"
        return _plan_page(request, ctx, con, err=msg, status_code=400)
    path.unlink(missing_ok=True)
    request.session.pop(PLAN_KEY, None)
    auth.log(con, by, "plan_upload", f"doc:{doc_id} lines:{res.detail.get('lines')}")
    return _plan_page(request, ctx, con, doc_id=doc_id, msg=f"시상안 {doc_id}번으로 원장 {res.detail.get('lines')}줄을 쌓았습니다. 줄을 확인하고 승인하세요.")
