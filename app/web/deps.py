"""라우터들이 함께 쓰는 것: DB 연결, 로그인 확인, 권한 검사 guard, CSRF, 화면 그리기, 조회 도우미.

권한 검사는 여기 있는 guard 한 곳에서만 한다. 화면 주소를 만들 때는 반드시 Depends(guard) 를 단다.
"""
import os
import pathlib
import secrets
from datetime import date

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app import db
from app.engine import promotion as pm
from app.web import auth, menu

HERE = pathlib.Path(__file__).resolve().parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["won"] = lambda v: f"{int(v):,}" if v is not None else "-"
templates.env.filters["n"] = lambda v: f"{(v or 0):g}"
templates.env.filters["md"] = lambda d: f"{int(d[5:7])}월 {int(d[8:10])}일" if d else ""


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
    uid = request.session.get("uid")
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
    if ctx["role"] not in item["roles"]:
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


def page(request, ctx, name, status_code=200, **kw):
    cfg = menu.load()
    return templates.TemplateResponse(request, name, status_code=status_code, context={
        "ctx": ctx, "groups": menu.for_role(ctx["role"]), "mobile_tabs": cfg["mobile_tabs"], "item": ctx.get("item"),
        "path": request.url.path, "SCOPES": menu.SCOPES, "PHASES": menu.PHASES, "STATUS": menu.STATUS,
        "LEVEL_KO": auth.LEVEL_KO, "HEAD_KO": auth.HEAD_KO, "pm": pm, **kw})


# ---------------------------------------------------------------- 조회 도우미
def _q(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def _in(ids):
    ids = list(ids) or [-1]
    return ",".join("?" * len(ids)), ids


def _perf(con, ym, agent_ids):
    q, ids = _in(agent_ids)
    return _q(con, f"""SELECT a.agent_id, a.internal_code code, a.name, a.hire_date, o.name org, s.applied_count, s.new_count, s.withdrawn_count,
                              s.net_count, s.converted_premium
                       FROM agent a
                       LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
                       LEFT JOIN org_unit o ON o.org_id=h.org_id
                       LEFT JOIN (SELECT s.* FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                                  WHERE s.period_type='month' AND s.period_key=?) s ON s.agent_id=a.agent_id
                       WHERE a.agent_id IN ({q}) AND a.status='active' ORDER BY COALESCE(s.converted_premium,0) DESC, a.internal_code""",
              (ym, ym, ym, ym, *ids))


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
