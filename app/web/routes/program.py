"""제도 묶음 네 화면과 내 진척의 커리어패스, 인증. S4 파트 I.

계산은 app/engine/career.py, certification.py, standards.py 가 한다. 이 파일은 범위 안 설계사 목록을 넘기고 화면 꼴로 묶을 뿐이다.
모든 주소는 guard 를 거친다. 범위는 config/menu.yaml 의 roles 를 따른다. viewer 에는 로그인 문맥 ctx 를 넘긴다.

| 주소 | 메인 관리자 | 관리자 | 설계사 | 쓰기 |
| --- | --- | --- | --- | --- |
| /program/career | 전체 | 자기 조직 | 없음 | 회차 열기, 승인, 반려, 무르기. 메인 관리자만, 확인 칸과 사유 |
| /program/cert | 전체 | 자기 조직 | 없음 | 없음 |
| /program/executive | 전체 | 자기 조직 | 없음 | 없음 |
| /program/excellence | 전체 | 자기 조직 | 없음 | 선발과 무르기. 메인 관리자만, 확인 칸과 사유 |
| /my/career | 없음 | 본인 | 본인 | 없음 |
| /my/cert | 없음 | 본인 | 본인 | 없음 |

D-17. 수수료 지표(커리어의 commission_amount, 인증의 annual_commission)는 엔진이 본인과 메인 관리자 말고는 가린다.
화면은 가린 칸(hidden)에 가림 표지만 붙이고 값을 다시 넣지 않는다.
D-33 가정. 인증의 연 보험료 지표는 계약일로 세어 실적 화면과 다를 수 있다. 엔진의 기준 설명(basis)을 화면에 그대로 보인다.
예상. 연말 값과 연말 달성은 엔진이 standards.estimate() 꼴로 준다. 화면은 label('예상')을 값 옆에 붙인다.
기준 버전. 판정 화면마다 '이 판정에 쓴 기준: 기관 연도 판'을 보인다. 판을 남기기 전의 판정이면 그렇다고 적는다.

가정. 회차 열기는 확인 칸만 받는다. 엔진이 회차에 사유 칸을 두지 않았기 때문이다. 승인과 반려와 선발은 확인 칸과 사유를 받는다.
무르기는 열린 결정이라 사유를 standards.REVOKE_MIN 자 이상 적고 무르게 둔 엔진의 가정을 따른다.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.engine import career, standards
from app.engine import certification as ce
from app.engine import results as rs
from app.web import auth
from app.web.deps import get_con, guard, page, templates
from app.web.routes.perf import scope_ids

router = APIRouter()

NOTE_MAX = 500
YM = re.compile(r"^20\d\d(0[1-9]|1[0-2])$")
PERIOD_KO = {"month": "월", "quarter": "분기", "half": "반기", "year": "연", "rolling_12m": "최근 12개월"}
OP_KO = {">=": "이상", "<=": "이하", "=": ""}             # 같음 기준은 "기준 0건" 처럼 값만 적는다
DECISION_KO = {"approved": ("good", "승인"), "rejected": ("bad", "반려")}
ROUND_KO = {"open": ("blue", "열림"), "replaced": ("", "다시 엶"), "closed": ("", "닫힘")}
CAREER_VIEWS = {"candidates": "후보", "rounds": "회차와 승인", "results": "결과", "standards": "기관별 기준"}
CERT_SECTIONS = (("mdrt", "MDRT"), ("association_cert", "협회 인증"), ("insurer_cert", "원수사 인증"))
EXEC_SECTIONS = (("executive_manager", "관리자 임원제도"), ("executive_fc", "설계사 임원제도"))
COUNT_UNIT = {"award_count": "회", "org_headcount": "명"}
CRIT_GROUP_KO = {"all": "", "commission": "수수료 기준", "premium": "보험료 기준"}


def pkey(key) -> str:
    """기간 열쇠를 말로. 202610, 2026Q3, 2026H2, 2026."""
    k = str(key or "")
    if re.fullmatch(r"20\d\d(0[1-9]|1[0-2])", k):
        return f"{k[:4]}년 {int(k[4:])}월"
    m = re.fullmatch(r"(20\d\d)Q([1-4])", k)
    if m:
        return f"{m.group(1)}년 {m.group(2)}분기"
    m = re.fullmatch(r"(20\d\d)H([12])", k)
    if m:
        return f"{m.group(1)}년 {'상' if m.group(2) == '1' else '하'}반기"
    return f"{k}년" if re.fullmatch(r"20\d\d", k) else k


templates.env.filters["pkey"] = pkey


def _deny(con, ctx, request, what: str):
    auth.log(con, ctx["login_id"], "access_denied", f"{request.url.path} {what}")
    raise HTTPException(403, "메인 관리자만 할 수 있습니다.")


def _me(ctx) -> list:
    return [ctx["agent_id"]] if ctx.get("agent_id") else []


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _base(ctx) -> dict:
    return {"PERIOD_KO": PERIOD_KO, "OP_KO": OP_KO, "COUNT_UNIT": COUNT_UNIT, "CRIT_GROUP_KO": CRIT_GROUP_KO, "NOTE_MAX": NOTE_MAX,
            "REVOKE_MIN": standards.REVOKE_MIN, "CAREER_ISSUER": standards.CAREER_ISSUER, "is_main": ctx["role"] == "main",
            "CAREER_KO": career.CAREER_METRIC_KO, "CERT_KO": ce.METRIC_KO, "CERT_UNIT": ce.METRIC_UNIT, "CAREER_UNIT": career.CAREER_METRIC_UNIT}


# ---------------------------------------------------------------- 커리어패스 운영
def _career_page(request, ctx, con, *, view="candidates", round_id=None, ask=None, agent=None, decision=None, error=None,
                 form=None, done=None, status_code=200):
    ym, ids = ctx["ym"], scope_ids(con, ctx)
    view = view if view in CAREER_VIEWS else "candidates"
    cands = career.career_candidates(con, ids, ym, viewer=ctx)
    rounds = career.rounds(con, ids)
    detail = None
    if round_id is not None:
        try:
            detail = career.round_detail(con, round_id, ids, viewer=ctx)
        except ValueError:
            raise HTTPException(404, "없는 회차입니다.") from None
    elif view == "rounds" and rounds:
        detail = career.round_detail(con, rounds[0]["round_id"], ids, viewer=ctx)
    results = career.career_results(con, ids)
    target = None                                    # 확인 단계에 올린 사람이나 결정
    if ctx["role"] != "main":
        ask = None
    if ask in ("approve", "reject"):
        target = next((m for m in (detail or {}).get("members", []) if m["agent_id"] == agent), None)
        if not target or target["decision"] or detail["round"]["status"] != "open" or (ask == "approve" and not target["eligible"]):
            ask, target = None, None
    elif ask == "revoke":
        target = next((r for r in results if r["decision_id"] == decision and not r["voided"]), None)
        if not target:
            ask = None
    elif ask != "open":
        ask = None
    live = next((r for r in rounds if r["status"] == "open" and r["round_key"] == career.last_full_quarter_key(ym)), None)
    return page(request, ctx, "program_career.html", status_code=status_code, view=view, VIEWS=CAREER_VIEWS, cands=cands,
                n_eligible=sum(1 for c in cands if c["eligible"]), rounds=rounds, detail=detail, results=results,
                stds=standards.standards(con, int(ym[:4]), ym) if view == "standards" else [],
                versions=standards.versions(con) if view == "standards" else [],
                ask=ask, target=target, error=error, form=form or {}, done=done, ym=ym, round_key=career.last_full_quarter_key(ym),
                live=live, fresh=rs.freshness(con, ym), DECISION_KO=DECISION_KO, ROUND_KO=ROUND_KO, **_base(ctx))


@router.get("/program/career", response_class=HTMLResponse)
def program_career(request: Request, view: str = "candidates", round: int | None = None, ask: str = "", agent: int | None = None,
                   decision: int | None = None, done: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    return _career_page(request, ctx, con, view=view, round_id=round, ask=ask, agent=agent, decision=decision,
                        done=done if done in ("open", "approve", "reject", "revoke") else None)


@router.post("/program/career", response_class=HTMLResponse)
async def program_career_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    """회차 열기, 승인, 반려, 무르기. 메인 관리자만이고 확인 칸(agree=yes)이 있어야 한다. 엔진이 메인 관리자를 한 번 더 본다."""
    form = await request.form()
    action = str(form.get("action") or "")
    if action not in ("open", "approve", "reject", "revoke"):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    if ctx["role"] != "main":
        _deny(con, ctx, request, f"career_{action}")
    rid, aid, did = _int(form.get("round")), _int(form.get("agent")), _int(form.get("decision"))
    note = _clean(form.get("note"))[:NOTE_MAX + 1]
    eff = _clean(form.get("effective_ym"))
    view = "results" if action == "revoke" and form.get("from") == "results" else "rounds"
    kept = {"note": note[:NOTE_MAX], "effective_ym": eff}
    again = dict(view=view, round_id=rid, ask=action, agent=aid, decision=did, form=kept, status_code=400)
    if form.get("agree") != "yes":
        return _career_page(request, ctx, con, error="확인 칸에 표시해야 처리합니다.", **again)
    if action in ("approve", "reject", "revoke"):
        if not note:
            return _career_page(request, ctx, con, error="사유를 적어 주세요.", **again)
        if len(note) > NOTE_MAX:
            return _career_page(request, ctx, con, error=f"사유는 {NOTE_MAX}자까지 적습니다.", **again)
    if action in ("approve", "reject") and (rid is None or aid is None or aid not in scope_ids(con, ctx)):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    if action == "approve" and eff and not YM.match(eff):
        return _career_page(request, ctx, con, error="시작 달은 202611 처럼 여섯 자리로 적습니다.", **again)
    try:
        if action == "open":
            res = career.open_round(con, ctx["ym"], ctx)
            rid = res["round"]["round_id"]
        elif action == "approve":
            career.approve_promotion(con, rid, aid, ctx, effective_ym=eff or None, note=note)
        elif action == "reject":
            career.reject_promotion(con, rid, aid, ctx, note=note)
        else:
            if did is None or not any(r["decision_id"] == did for r in career.career_results(con, scope_ids(con, ctx))):
                raise HTTPException(400, "처리할 수 없는 요청입니다.")
            career.revoke_decision(con, did, ctx, note)
            rid = next((r["round_id"] for r in career.career_results(con, scope_ids(con, ctx)) if r["decision_id"] == did), rid)
    except PermissionError:
        con.rollback()
        _deny(con, ctx, request, f"career_{action}")
    except ValueError as e:
        con.rollback()
        return _career_page(request, ctx, con, error=str(e), **again)
    tail = f"&round={rid}" if rid is not None and view == "rounds" else ""
    return RedirectResponse(f"/program/career?view={view}{tail}&done={action}#done", status_code=303)


# ---------------------------------------------------------------- 인증과 MDRT, 임원제도
def _programs(res, sections) -> list:
    """분류마다 프로그램 요약. [{category, title, programs: [summary + issuer, version]}]"""
    first = {}
    for r in res["rows"]:
        first.setdefault(r["program_code"], r)
    out = []
    for cat, title in sections:
        progs = []
        for s in res["summary"]:
            if s["category"] != cat:
                continue
            r = first.get(s["program_code"]) or {}
            progs.append({**s, "issuer": r.get("issuer"), "version": r.get("version"), "target": r.get("target")})
        if progs:
            out.append({"category": cat, "title": title, "programs": progs})
    return out


def _status_page(request, ctx, con, res, sections, *, kind, prog, who):
    groups = _programs(res, sections)
    codes = [p["program_code"] for g in groups for p in g["programs"]]
    prog = prog if prog in codes else (codes[0] if codes else None)
    rows = [r for r in res["rows"] if r["program_code"] == prog]
    rows.sort(key=lambda r: (not r["achieved"], -(r["progress_rate"] or 0), r["code"] or ""))
    sel = next((p for g in groups for p in g["programs"] if p["program_code"] == prog), None)
    person = None
    if who is not None:
        if who not in scope_ids(con, ctx):
            auth.log(con, ctx["login_id"], "access_denied", f"{request.url.path} agent:{who}")
            raise HTTPException(403, "이 사람의 진척을 볼 수 없습니다.")
        person = next((r for r in rows if r["agent_id"] == who), None)
    return page(request, ctx, "program_status.html", kind=kind, res=res, pgroups=groups, prog=prog, sel=sel, rows=rows, person=person,
                **_base(ctx))


@router.get("/program/cert", response_class=HTMLResponse)
def program_cert(request: Request, prog: str = "", who: int | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    res = ce.cert_progress(con, scope_ids(con, ctx), viewer=ctx)
    return _status_page(request, ctx, con, res, CERT_SECTIONS, kind="cert", prog=prog, who=who)


@router.get("/program/executive", response_class=HTMLResponse)
def program_executive(request: Request, prog: str = "", who: int | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    res = ce.executive_status(con, scope_ids(con, ctx), viewer=ctx)
    return _status_page(request, ctx, con, res, EXEC_SECTIONS, kind="executive", prog=prog, who=who)


# ---------------------------------------------------------------- 영업우수
def _excellence_page(request, ctx, con, *, ask=None, agent=None, prog=None, sel_id=None, error=None, form=None, done=None, status_code=200):
    ids = scope_ids(con, ctx)
    x = ce.excellence(con, ids, viewer=ctx)
    voided = [s for s in ce.selections(con, ids, x["year"], include_voided=True) if s["voided"]] if x["year"] else []
    target = None
    if ctx["role"] != "main":
        ask = None
    if ask == "select":
        target = next((r for r in x["candidates"] if r["agent_id"] == agent and r["program_code"] == prog), None)
    elif ask == "revoke":
        target = next((s for s in x["selections"] if s["selection_id"] == sel_id), None)
    if not target:
        ask = None
    versions = {}
    for r in x["rows"]:
        versions.setdefault(r["program_code"], r)
    return page(request, ctx, "program_excellence.html", status_code=status_code, x=x, voided=voided, ask=ask, target=target, error=error,
                form=form or {}, done=done, firsts=list(versions.values()), **_base(ctx))


@router.get("/program/excellence", response_class=HTMLResponse)
def program_excellence(request: Request, ask: str = "", agent: int | None = None, prog: str = "", sel: int | None = None, done: str = "",
                       ctx: dict = Depends(guard), con=Depends(get_con)):
    return _excellence_page(request, ctx, con, ask=ask, agent=agent, prog=prog, sel_id=sel,
                            done=done if done in ("select", "revoke") else None)


@router.post("/program/excellence", response_class=HTMLResponse)
async def program_excellence_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    """선발과 무르기. 메인 관리자만이고 확인 칸과 사유가 있어야 한다. 엔진이 메인 관리자와 기준 충족을 한 번 더 본다."""
    form = await request.form()
    action = str(form.get("action") or "")
    if action not in ("select", "revoke"):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    if ctx["role"] != "main":
        _deny(con, ctx, request, f"excellence_{action}")
    aid, sid, year = _int(form.get("agent")), _int(form.get("sel")), _int(form.get("year"))
    prog = _clean(form.get("prog"))
    note = _clean(form.get("note"))
    again = dict(ask=action, agent=aid, prog=prog, sel_id=sid, form={"note": note[:NOTE_MAX]}, status_code=400)
    if form.get("agree") != "yes":
        return _excellence_page(request, ctx, con, error="확인 칸에 표시해야 처리합니다.", **again)
    if not note:
        return _excellence_page(request, ctx, con, error="사유를 적어 주세요.", **again)
    if len(note) > NOTE_MAX:
        return _excellence_page(request, ctx, con, error=f"사유는 {NOTE_MAX}자까지 적습니다.", **again)
    ids = scope_ids(con, ctx)
    try:
        if action == "select":
            if aid is None or year is None or not prog or aid not in ids:
                raise HTTPException(400, "처리할 수 없는 요청입니다.")
            ce.select_excellence(con, prog, year, aid, ctx, note=note)
        else:
            if sid is None or not any(s["selection_id"] == sid for s in ce.selections(con, ids)):
                raise HTTPException(400, "처리할 수 없는 요청입니다.")
            ce.revoke_selection(con, sid, ctx, note)
    except PermissionError:
        con.rollback()
        _deny(con, ctx, request, f"excellence_{action}")
    except ValueError as e:
        con.rollback()
        return _excellence_page(request, ctx, con, error=str(e), **again)
    return RedirectResponse(f"/program/excellence?done={action}#done", status_code=303)


# ---------------------------------------------------------------- 내 진척: 커리어패스와 인증
@router.get("/my/career", response_class=HTMLResponse)
def my_career(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    ym, me = ctx["ym"], _me(ctx)
    st = (career.career_status(con, me, ym, viewer=ctx) or [None])[0]
    goals = career.goal_backsolve(con, me, ym, viewer=ctx)
    return page(request, ctx, "my_career.html", st=st, goals=goals, history=[r for r in career.career_results(con, me, include_voided=False) if r["decision"] == "approved"],
                has_agent=bool(me), ym=ym, fresh=rs.freshness(con, ym), **_base(ctx))


@router.get("/my/cert", response_class=HTMLResponse)
def my_cert(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    res = ce.cert_progress(con, _me(ctx), viewer=ctx)
    groups = []
    for cat, title in CERT_SECTIONS:
        rows = [r for r in res["rows"] if r["category"] == cat]
        if rows:
            groups.append({"title": title, "rows": rows})
    return page(request, ctx, "my_cert.html", res=res, pgroups=groups, has_agent=bool(_me(ctx)),
                n_achieved=sum(1 for r in res["rows"] if r["achieved"]), n_projected=sum(1 for r in res["rows"] if r["projected"]["value"]),
                **_base(ctx))
