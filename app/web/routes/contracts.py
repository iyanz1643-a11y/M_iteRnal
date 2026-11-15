"""실적 묶음의 계약 원장 /perf/contracts. 범위 안 계약의 목록, 상세, 내려받기를 한 주소에서 한다. 모든 요청이 guard 를 거친다.

| 요청 | 하는 일 | 누가 |
| --- | --- | --- |
| GET /perf/contracts | 목록. 거르기와 쪽 나누기. 계약자와 피보험자 이름은 가린다 | 전원. 메뉴 범위 안 계약만 |
| GET /perf/contracts?id=번호 | 계약 상세와 상태 이력. 이름은 아래 규칙대로 | 그 계약이 범위 안인 사람 |
| POST /perf/contracts action=reveal | 메인 관리자가 사유를 적고 이름을 본다 | 메인 관리자 |
| GET /perf/contracts?export=csv | 지금 거른 목록 전부를 CSV 로. 이름은 가린다 | 전원. 범위 안만 |

D-05 계약자와 피보험자 이름

| 누가 | 목록과 CSV | 상세 |
| --- | --- | --- |
| 담당 설계사(contract.agent_id) | 가림 | 봄 |
| 직속 관리자. 업적 귀속 월에 담당 설계사가 속한 조직의 조직장과 그 기간의 대리 | 가림 | 봄 |
| 위 조직의 관리자, 다른 관리자 | 가림 | 범위 안이면 가린 채로, 범위 밖이면 403 |
| 메인 관리자 | 가림 | 사유를 적은 뒤 봄. docs/09 2.3 원칙 3 |

이름을 본 일은 역할과 상관없이 변경 기록에 contract_name_view 로 남긴다. 메인 관리자의 사유는 그 줄의 after_json 에 둔다.
내려받기는 contract_export 로 남긴다. 범위 밖 계약을 연 시도는 access_denied 로 남긴다.

D-17: 수수료와 시상 금액 칸은 싣지 않는다. 목록에 인정보험료 합계를 내지 않는다. 철회와 청약이 섞인 합계는
업적 집계와 다른 숫자가 되기 때문이다. 같은 사람의 실적 숫자는 기간별 업적에서 본다.

이름 칸을 읽는 곳은 이 파일의 _holder_plain 과 _rows 둘뿐이다. _rows 는 읽자마자 가린다.
tests/test_s1_decisions.py 의 D-05 시험이 이것을 본다.
"""
from __future__ import annotations

import calendar
import csv
import io
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app import db
from app.engine import classify
from app.web import auth
from app.web.deps import NO_STORE, _in, _q, get_con, guard, page
from app.web.routes.perf import fresh, pick_org, scope_ids, scope_orgs

router = APIRouter()
PATH = "/perf/contracts"
PER_PAGE = 50
REASON_MIN = 5                       # 메인 관리자의 조회 사유 최소 글자 수
REASON_MAX = 200

STATUS_KO = {"applied": "청약", "inforce": "정상", "withdrawn": "철회", "declined": "반송", "lapsed": "실효", "cancelled": "해지", "matured": "만기"}
STATUS_CLASS = {"inforce": "good", "applied": "blue", "withdrawn": "bad", "declined": "bad", "lapsed": "warn", "cancelled": "warn"}
SECTOR_KO = {"life": "생보", "nonlife": "손보"}
MONTH = "COALESCE(c.perf_ym, REPLACE(SUBSTR(COALESCE(c.contract_date, c.application_date), 1, 7), '-', ''))"   # 업적 귀속 월


def mask(name) -> str:
    """이름 가리기. 홍길동은 홍*동, 남궁민수는 남**수, 두 글자는 홍*, 한 글자는 * 다. 비어 있으면 -."""
    s = re.sub(r"\s+", "", str(name or ""))
    if not s or s == "-":
        return "-"
    if len(s) == 1:
        return "*"
    if len(s) == 2:
        return s[0] + "*"
    return s[0] + "*" * (len(s) - 2) + s[-1]


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _deny(con, ctx, what: str, message: str):
    auth.log(con, ctx["login_id"], "access_denied", f"{PATH} {what}")
    raise HTTPException(403, message)


# ---------------------------------------------------------------- 범위와 거르기
def _scope_where(con, ctx, org: int | None) -> tuple:
    """(조건 글, 값, 고른 조직). 메뉴 범위 안 설계사의 계약만이다. 메인 관리자는 담당이 비어 있는 계약도 본다."""
    sel, ids = pick_org(con, ctx, org, scope_ids(con, ctx))
    q, params = _in(ids)
    where = f"c.agent_id IN ({q})"
    if ctx["scope"] == "all" and sel is None:
        where = f"({where} OR c.agent_id IS NULL)"
    return where, list(params), sel


def _filters(con, ctx, args: dict) -> dict:
    """주소의 거르기 값을 확인한다. 모르는 값은 버린다."""
    f = {"ym": args.get("ym") or "", "insurer": _int(args.get("insurer")), "cat": args.get("cat") or "", "status": args.get("status") or "",
         "sector": args.get("sector") or "", "q": re.sub(r"[^0-9A-Za-z-]", "", args.get("q") or "")[:30], "org": _int(args.get("org"))}
    if f["status"] not in STATUS_KO:
        f["status"] = ""
    if f["sector"] not in SECTOR_KO:
        f["sector"] = ""
    return f


def _where(con, ctx, f) -> tuple:
    where, params, sel = _scope_where(con, ctx, f["org"])
    conds = [where]
    if f["ym"] and f["ym"] != "all":
        conds.append(f"{MONTH}=?")
        params.append(f["ym"])
    if f["insurer"]:
        conds.append("c.insurer_id=?")
        params.append(f["insurer"])
    if f["cat"]:
        conds.append(f"{classify.CATEGORY}=?")
        params.append(f["cat"])
    if f["status"]:
        conds.append("c.status=?")
        params.append(f["status"])
    if f["sector"]:
        conds.append(f"{classify.SECTOR}=?")
        params.append(f["sector"])
    if f["q"]:
        conds.append("(c.policy_no LIKE ? OR c.application_no LIKE ?)")
        params += [f"%{f['q']}%", f"%{f['q']}%"]
    return " AND ".join(conds), params, sel


BASE = f"""FROM contract c {classify.JOINS}
           LEFT JOIN product_category pc ON pc.code={classify.CATEGORY}
           LEFT JOIN agent a ON a.agent_id=c.agent_id"""


def _rows(con, where, params, limit=None, offset=0) -> list:
    """목록과 CSV 의 줄. 계약자와 피보험자 이름은 읽자마자 가린다. 원래 이름은 이 함수 밖으로 나가지 않는다."""
    tail = f" LIMIT {int(limit)} OFFSET {int(offset)}" if limit else ""
    rows = _q(con, f"""SELECT c.contract_id, c.policy_no, c.application_no, i.name insurer, COALESCE(pr.product_name, c.product_code_raw, c.insurer_product_code) product,
                              COALESCE(pc.name, '미분류') category, {classify.SECTOR} sector, c.contract_date, c.application_date, c.status, c.withdrawal_date,
                              c.converted_premium, c.policy_count, {MONTH} ym, a.internal_code agent_code, a.name agent_name,
                              c.policyholder_name, c.insured_name
                       {BASE} WHERE {where}
                       ORDER BY COALESCE(c.contract_date, c.application_date) DESC, c.contract_id DESC{tail}""", params)
    for r in rows:
        r["holder"], r["insured"] = mask(r.pop("policyholder_name")), mask(r.pop("insured_name"))
    return rows


def _decorate(r: dict) -> dict:
    r["status_ko"] = STATUS_KO.get(r["status"], r["status"])
    r["status_class"] = STATUS_CLASS.get(r["status"], "")
    r["sector_ko"] = SECTOR_KO.get(r["sector"], "-")
    r["no"] = r["policy_no"] or r["application_no"] or "-"
    r["date"] = r["contract_date"] or r["application_date"]
    return r


def _months(con, ctx) -> list:
    where, params, _ = _scope_where(con, ctx, None)
    return [r["ym"] for r in _q(con, f"SELECT DISTINCT {MONTH} ym FROM contract c WHERE {where} AND {MONTH} IS NOT NULL ORDER BY ym DESC", params)]


def _options(con) -> dict:
    return {"insurers": _q(con, "SELECT insurer_id, name FROM insurer ORDER BY name"),
            "cats": _q(con, "SELECT code, name FROM product_category ORDER BY sort_order")}


# ---------------------------------------------------------------- 이름을 보는 규칙
def _month_bounds(ym: str) -> tuple:
    y, m = int(ym[:4]), int(ym[4:6])
    return f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def direct_heads(con, c: dict) -> set:
    """그 계약의 직속 관리자. 업적 귀속 월에 담당 설계사가 속한 조직의 조직장과, 그 달과 기간이 겹치는 대리다."""
    ym = c.get("ym")
    if not c.get("agent_id") or not ym or not re.fullmatch(r"\d{6}", ym):
        return set()
    orgs = [r["org_id"] for r in con.execute(
        "SELECT org_id FROM agent_org_history WHERE agent_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)",
        (c["agent_id"], ym, ym))]
    if not orgs:
        return set()
    q, params = _in(orgs)
    heads = {r["agent_id"] for r in con.execute(
        f"SELECT agent_id FROM org_manager WHERE org_id IN ({q}) AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)",
        (*params, ym, ym))}
    first, last = _month_bounds(ym)
    heads |= {r["agent_id"] for r in con.execute(
        f"""SELECT agent_id FROM org_deputy WHERE org_id IN ({q}) AND date_from<=? AND date_to>=?
            AND (ended_at IS NULL OR SUBSTR(ended_at, 1, 10)>=?)""", (*params, last, first, first))}
    return heads


def name_access(con, ctx, c: dict) -> str:
    """이 사람이 이 계약의 이름을 어떻게 보는가. 'own' 담당 설계사, 'head' 직속 관리자, 'reason' 메인 관리자는 사유를 적고, 'masked' 가림."""
    me = ctx.get("agent_id")
    if me and c.get("agent_id") == me:
        return "own"
    if me and ctx["role"] == "manager" and me in direct_heads(con, c):
        return "head"
    if ctx["role"] == "main":
        return "reason"
    return "masked"


def _holder_plain(con, contract_id: int) -> dict:
    """원래 이름. name_access 가 own, head 이거나 메인 관리자가 사유를 적었을 때만 부른다."""
    r = con.execute("SELECT policyholder_name, insured_name FROM contract WHERE contract_id=?", (contract_id,)).fetchone()
    return {"holder": (r["policyholder_name"] or "-") if r else "-", "insured": (r["insured_name"] or "-") if r else "-"}


def _log_view(con, ctx, contract_id: int, why: str, reason: str | None = None):
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (db.now(), ctx["login_id"], "contract_name_view", f"contract:{contract_id}",
                 json.dumps({"why": why, **({"reason": reason} if reason else {})}, ensure_ascii=False)))
    con.commit()


# ---------------------------------------------------------------- 상세
def _contract(con, ctx, contract_id) -> dict:
    """범위 안 계약 하나. 없으면 404, 범위 밖이면 403 이고 시도를 남긴다."""
    exists = con.execute("SELECT 1 FROM contract WHERE contract_id=?", (contract_id,)).fetchone() if contract_id else None
    if not exists:
        raise HTTPException(404, "없는 계약입니다.")
    where, params, _ = _scope_where(con, ctx, None)
    rows = _rows(con, f"c.contract_id=? AND {where}", [contract_id, *params])
    if not rows:
        _deny(con, ctx, f"contract:{contract_id}", "이 계약을 볼 권한이 없습니다.")
    c = _decorate(rows[0])
    extra = con.execute("""SELECT c.agent_id, c.premium, c.premium_monthly, c.payment_mode, c.payment_term, c.payment_term_name, c.paid_installments,
                                  c.coverage_start, c.coverage_end, c.status_date, c.receipt_date, c.org_path
                           FROM contract c WHERE c.contract_id=?""", (contract_id,)).fetchone()
    c.update(dict(extra))
    c["history"] = _q(con, "SELECT status, status_date, created_at FROM contract_status_history WHERE contract_id=? ORDER BY status_date, id", (contract_id,))
    for h in c["history"]:
        h["status_ko"] = STATUS_KO.get(h["status"], h["status"])
    org = con.execute("""SELECT o.name FROM agent_org_history h JOIN org_unit o ON o.org_id=h.org_id
                         WHERE h.agent_id=? AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)""",
                      (c["agent_id"], c["ym"], c["ym"])).fetchone() if c["agent_id"] and c["ym"] else None
    c["org"] = org["name"] if org else (c["org_path"] or "-")
    return c


def _detail(request, ctx, con, contract_id, reason=None, error=None, status_code=200):
    c = _contract(con, ctx, contract_id)
    access = name_access(con, ctx, c)
    names = None
    if access in ("own", "head"):
        names = _holder_plain(con, contract_id)
        _log_view(con, ctx, contract_id, access)
    elif access == "reason" and reason:
        names = _holder_plain(con, contract_id)
        _log_view(con, ctx, contract_id, "main", reason)
    return page(request, ctx, "perf_contract.html", c=c, access=access, names=names, reason=reason, error=error,
                fresh=fresh(con, ctx["ym"]), REASON_MIN=REASON_MIN, REASON_MAX=REASON_MAX, status_code=status_code)


# ---------------------------------------------------------------- 내려받기
CSV_HEAD = ["증권번호", "청약번호", "보험사", "상품", "구분", "업권", "계약일", "업적 귀속 월", "상태", "인정보험료", "건수", "담당 사번", "담당 설계사", "계약자", "피보험자"]


def _cell(v) -> str:
    """엑셀이 식으로 읽지 않게 =, +, -, @ 로 시작하는 글 앞에 ' 를 붙인다. 숫자는 그대로 둔다."""
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _export(con, ctx, f, where, params) -> Response:
    rows = [_decorate(r) for r in _rows(con, where, params)]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEAD)
    for r in rows:
        w.writerow([_cell(x) for x in (r["policy_no"], r["application_no"], r["insurer"], r["product"], r["category"], r["sector_ko"], r["date"],
                                       r["ym"], r["status_ko"], r["converted_premium"], r["policy_count"], r["agent_code"], r["agent_name"],
                                       r["holder"], r["insured"])])
    kept = {k: v for k, v in f.items() if v not in (None, "")}
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (db.now(), ctx["login_id"], "contract_export", f"rows:{len(rows)}", json.dumps({"filters": kept, "masked": True}, ensure_ascii=False)))
    con.commit()
    name = f"contracts_{ctx['ym']}.csv"
    return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": NO_STORE, "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------- 주소
@router.get("/perf/contracts", response_class=HTMLResponse)
def contracts(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    args = dict(request.query_params)
    if "id" in args:
        return _detail(request, ctx, con, _int(args["id"]))
    f = _filters(con, ctx, args)
    months = _months(con, ctx)
    if f["ym"] != "all" and f["ym"] not in months:
        f["ym"] = ctx["ym"] if ctx["ym"] in months else (months[0] if months else "all")
    where, params, sel = _where(con, ctx, f)
    if args.get("export") == "csv":
        return _export(con, ctx, f, where, params)
    total = con.execute(f"SELECT COUNT(*) {BASE} WHERE {where}", params).fetchone()[0]
    by_status = {r["status"]: r["n"] for r in _q(con, f"SELECT c.status, COUNT(*) n {BASE} WHERE {where} GROUP BY c.status", params)}
    pages = max(1, -(-total // PER_PAGE))
    pg = min(max(_int(args.get("page")) or 1, 1), pages)
    rows = [_decorate(r) for r in _rows(con, where, params, PER_PAGE, (pg - 1) * PER_PAGE)]
    keep = {k: ("" if v is None else v) for k, v in f.items()}
    return page(request, ctx, "perf_contracts.html", rows=rows, total=total, by_status=by_status, pg=pg, pages=pages, f=keep, months=months,
                sel=sel, orgs=scope_orgs(con, ctx), opts=_options(con), STATUS_KO=STATUS_KO, SECTOR_KO=SECTOR_KO, fresh=fresh(con, ctx["ym"]))


@router.post("/perf/contracts", response_class=HTMLResponse)
async def contracts_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    action, cid = form.get("action"), _int(form.get("id"))
    if action != "reveal" or not cid:
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    if ctx["role"] != "main":
        _deny(con, ctx, f"contract:{cid}", "메인 관리자만 사유를 적고 이름을 봅니다.")
    reason = re.sub(r"\s+", " ", str(form.get("reason") or "")).strip()
    if len(reason) < REASON_MIN or len(reason) > REASON_MAX:
        return _detail(request, ctx, con, cid, error=f"조회 사유를 {REASON_MIN}자에서 {REASON_MAX}자 사이로 적어 주세요.", status_code=400)
    return _detail(request, ctx, con, cid, reason=reason)
