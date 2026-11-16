"""라우터들이 함께 쓰는 것: DB 연결, 로그인 확인, 권한 검사 guard, CSRF, 화면 그리기, 조회 도우미.

권한 검사는 여기 있는 guard 한 곳에서만 한다. 화면 주소를 만들 때는 반드시 Depends(guard) 를 단다.
"""
import os
import pathlib
import secrets
from datetime import date
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app import db
from app.engine import promotion as pm
from app.ops import sessions
from app.web import auth, menu

HERE = pathlib.Path(__file__).resolve().parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["won"] = lambda v: f"{int(v):,}" if v is not None else "-"
templates.env.filters["n"] = lambda v: f"{(v or 0):g}"
templates.env.filters["md"] = lambda d: f"{int(d[5:7])}월 {int(d[8:10])}일" if d else ""
templates.env.filters["when"] = lambda t: f"{int(t[5:7])}월 {int(t[8:10])}일 {int(t[11:13])}시 {t[14:16]}분" if t and len(t) >= 16 else ""
templates.env.filters["ymko"] = lambda ym: f"{ym[:4]}년 {int(ym[4:6])}월" if ym and len(ym) >= 6 else ""


def _qs(params: dict, **kw) -> str:
    """화면의 거르기 단추가 쓰는 주소 뒤 꼬리. params 에 kw 를 덮어쓰고 빈 값은 뺀다."""
    merged = {**params, **kw}
    return "?" + urlencode({k: v for k, v in merged.items() if v not in (None, "")})


templates.env.globals["qs"] = _qs


BASIS_LEAD = "보험사 기준이라 업적 집계와 다를 수 있습니다."


def basis_note(p: dict, r: dict | None, ref) -> str:
    """프로모션 달성 현황의 기준 안내. 달성 현황(팀원과 내 현황)과 내 시상 진척이 함께 쓴다. D-08 제안대로 둔 가정이다.

    calc-engine 이 progress() 결과에 basis 설명 칸을 더하면 그 글을 쓴다. 글이거나 text 또는 note 열쇠를 가진 dict 를 받는다.
    칸이 없으면 프로모션의 기준일 종류와 화면의 기준일로 글을 만든다. 철회 제외는 엔진의 COUNTED 와 같은 뜻이다.
    """
    eng = (r or {}).get("basis") if isinstance(r, dict) else None
    if isinstance(eng, dict):
        eng = eng.get("text") or eng.get("note") or eng.get("ko")
    day = date.fromisoformat(ref) if isinstance(ref, str) and ref else (ref or None)
    until = f"{day.month}월 {day.day}일까지 셉니다." if day else ""
    if isinstance(eng, str) and eng.strip():
        eng = eng.strip()
        lead = "" if "업적 집계" in eng else BASIS_LEAD + " "          # 엔진 글에 같은 말이 있으면 두 번 적지 않는다
        return f"{lead}{eng} {until}".strip()
    kind = pm.BASIS_KO.get(p.get("date_basis"), p.get("date_basis") or "계약일자")
    return f"{BASIS_LEAD} 기준일은 {kind}이고 {until} 성립한 계약만 세고 철회한 계약은 뺍니다."


templates.env.globals["basis_note"] = basis_note
templates.env.globals["group_entry"] = menu.entry


def get_con():
    con = db.connect(os.environ.get("APP_DB", db.DEFAULT_DB))
    try:
        yield con
    finally:
        con.close()


PASSWORD_PATH = "/me"


class NeedLogin(Exception):
    pass


class NeedPasswordChange(Exception):
    """초기 비밀번호로 들어온 사람은 비밀번호를 바꾸기 전까지 내 정보 화면만 쓴다."""


def current(request: Request, con=Depends(get_con)) -> dict:
    """로그인한 사람. 쿠키의 세션 버전이 계정의 버전과 다르면 로그아웃한 쿠키로 보고 로그인 전으로 돌린다(app/ops/sessions.py)."""
    uid = request.session.get("uid")
    if uid and not sessions.matches(request, con, uid):
        uid = None
    ctx = auth.context(con, uid) if uid else None
    if not ctx:
        clear_login(request)
        raise NeedLogin()
    return ctx


def clear_login(request: Request) -> None:
    """로그인 정보와 그에 딸린 값을 비운다. CSRF 토큰만 남긴다.

    로그인 전 세션에는 로그인 폼의 토큰이 들어 있다. 브라우저가 스스로 부르는 주소가 여기로 와서
    토큰까지 지우면 이어지는 로그인 POST 가 막힌다. 로그인에 성공하면 renew_csrf 로 토큰을 바꾼다.
    """
    tok = request.session.get(CSRF_KEY)
    request.session.clear()
    if tok:
        request.session[CSRF_KEY] = tok


def guard(request: Request, ctx: dict = Depends(current), con=Depends(get_con)) -> dict:
    """요청한 주소가 메뉴에 있고 그 역할에게 열려 있는지 본다. 화면이 아니라 여기서 막는다."""
    item = menu.find(request.url.path)
    if item is None:
        raise HTTPException(404, "없는 화면입니다.")
    if not menu.allowed(item, ctx):                     # 역할과, 항목에 requires 가 있으면 그 권한 플래그까지 본다
        auth.log(con, ctx["login_id"], "access_denied", request.url.path)
        raise HTTPException(403, "이 화면을 볼 권한이 없습니다.")
    if ctx["must_change_password"] and request.url.path != PASSWORD_PATH:
        raise NeedPasswordChange()
    return {**ctx, "item": item, "scope": item["roles"][ctx["role"]]}


# ---------------------------------------------------------------- CSRF
CSRF_KEY = "csrf_token"                 # 세션의 키이자 폼의 숨은 칸 이름
CSRF_HEADER = "x-csrf-token"            # 나중에 화면에서 fetch 로 보낼 때 쓰는 머리글
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def csrf_token(request: Request) -> str:
    """세션의 토큰을 돌려준다. 없으면 만든다. 로그인 전 세션에도 만들어 로그인 폼이 쓴다."""
    tok = request.session.get(CSRF_KEY)
    if not tok:
        tok = request.session[CSRF_KEY] = secrets.token_urlsafe(32)
    return tok


def renew_csrf(request: Request) -> str:
    """로그인 성공 뒤처럼 세션이 바뀔 때 토큰을 새로 만든다."""
    request.session[CSRF_KEY] = secrets.token_urlsafe(32)
    return request.session[CSRF_KEY]


async def csrf_protect(request: Request):
    """앱 전체에 걸린 의존성. 상태를 바꾸는 요청은 세션의 토큰과 같은 값을 보내야 한다."""
    if request.method not in UNSAFE:
        return
    form = await request.form()                         # FastAPI 가 같은 요청 객체에 담아 두므로 화면 함수가 다시 읽어도 된다
    sent = form.get(CSRF_KEY) or request.headers.get(CSRF_HEADER) or ""
    want = request.session.get(CSRF_KEY) or ""
    if not want or not isinstance(sent, str) or not secrets.compare_digest(sent.encode(), want.encode()):
        raise HTTPException(403, "요청을 확인하지 못했습니다. 화면을 새로 고친 뒤 다시 시도하세요.")


templates.env.globals["csrf_token"] = csrf_token


NO_STORE = "no-store"


def page(request, ctx, name, status_code=200, **kw):
    """로그인한 화면 하나를 그린다. 로그인한 모든 HTML 은 여기를 지난다.

    Cache-Control: no-store 를 붙여 브라우저와 중간 캐시가 화면을 남기지 않게 한다. 공용 기기에서 뒤로 가기나 캐시로
    다른 사람의 실적이 보이지 않게 하려는 것이다. 로그인 화면과 정적 파일은 여기를 지나지 않는다.
    """
    cfg = menu.load()
    resp = templates.TemplateResponse(request, name, status_code=status_code, context={
        "ctx": ctx, "groups": menu.for_ctx(ctx), "mobile_tabs": cfg["mobile_tabs"].get(ctx["role"], []), "item": ctx.get("item"),
        "path": request.url.path, "SCOPES": menu.SCOPES, "PHASES": menu.PHASES, "STATUS": menu.STATUS,
        "LEVEL_KO": auth.LEVEL_KO, "HEAD_KO": auth.HEAD_KO, "pm": pm, **kw})
    resp.headers["Cache-Control"] = NO_STORE
    return resp


# ---------------------------------------------------------------- 조회 도우미
def _q(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def _in(ids):
    ids = list(ids) or [-1]
    return ",".join("?" * len(ids)), ids


def _perf(con, ym, agent_ids):
    """범위 안 설계사의 그 달 실적. agent_ids 는 guard 가 준 범위 목록이고, 그 밖의 사람은 넣지 않는다.

    재직자와, 퇴사했어도 그 달에 소속 이력이 있는 사람을 센다. 조직 집계(v_org_performance_rollup, org_rollup)와 같은 기준이다.
    그 달 중간에 퇴사한 사람의 퇴사 전 실적은 조직 합계와 팀원 현황에 넣는다. 랭킹, 조직 부문 시상, 프로모션 달성 대상에서는 뺀다.
    퇴사자 줄은 left 가 참이고 termination_date 에 퇴사일이 있다.
    """
    q, ids = _in(agent_ids)
    return _q(con, f"""SELECT a.agent_id, a.internal_code code, a.name, a.hire_date, a.termination_date, a.status='terminated' AS "left",
                              o.name org, s.applied_count, s.new_count, s.withdrawn_count,
                              s.net_count, s.converted_premium
                       FROM agent a
                       LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
                       LEFT JOIN org_unit o ON o.org_id=h.org_id
                       LEFT JOIN (SELECT s.* FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                                  WHERE s.period_type='month' AND s.period_key=?) s ON s.agent_id=a.agent_id
                       WHERE a.agent_id IN ({q}) AND (a.status='active' OR h.org_id IS NOT NULL) ORDER BY COALESCE(s.converted_premium,0) DESC, a.internal_code""",
              (ym, ym, ym, ym, *ids))


def edu_today() -> date:
    """교육 기한의 기준일. 교육은 실적 자료와 상관없는 날짜 일이라 모든 교육 화면이 오늘을 쓴다.

    팀 교육 진척도, 내 교육, 홈의 할 일, 신인 정착, 알림함의 교육 기한, 일정의 교육 기한이 모두 이 함수를 부른다.
    프로모션의 기준일은 자료 기준 달의 pm.ref_date 이고 이 함수와 따로다.
    """
    return date.today()


def _last_load(con):
    r = con.execute("SELECT MAX(loaded_at) t FROM import_batch WHERE data_type='org_contract'").fetchone()
    return r["t"]


def _period_text(p):
    md = lambda d: f"{int(d[5:7])}월 {int(d[8:10])}일"
    if len(p["windows"]) == 1:
        return f"{md(p['date_from'])} ~ {md(p['date_to'])}"
    return " · ".join(f"{w['label']} {md(w['date_from'])}~{md(w['date_to'])}" for w in p["windows"])


def _cards(con, ctx, include_unpublished=False):
    """프로모션 카드. 홈과 프로모션 화면이 함께 쓴다."""
    ref = pm.ref_date(ctx["ym"])
    out = []
    for p in pm.listing(con, ctx["ym"], ctx["role"], include_unpublished):
        due, due_class = pm.due_label(p, ref)
        f, t = date.fromisoformat(p["date_from"]), date.fromisoformat(p["date_to"])
        phase = "upcoming" if ref < f else ("ended" if ref > t else "active")
        out.append({**p, "due": due, "due_class": due_class, "phase": phase, "head": pm.headline(p), "period_text": _period_text(p)})
    order = {"active": 0, "upcoming": 1, "ended": 2}
    out.sort(key=lambda c: (order[c["phase"]], c["date_to"], c["promo_id"]))
    return out, ref
