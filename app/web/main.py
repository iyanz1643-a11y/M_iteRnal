"""웹 화면의 진입점. 로그인, 역할별 메뉴, 조직 범위 검사를 여기서 강제한다.

    uvicorn app.web.main:app --port 8000        # APP_DB 로 DB 파일, APP_SECRET 으로 세션 키를 바꾼다
"""
import os
import pathlib
import secrets
from datetime import date, datetime

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.periods import period_key
from app.web import auth, menu

HERE = pathlib.Path(__file__).resolve().parent
app = FastAPI(title="GA 업무 앱", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("APP_SECRET") or secrets.token_hex(32),
                   same_site="lax", https_only=os.environ.get("APP_HTTPS") == "1", max_age=60 * 60 * 8)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["won"] = lambda v: f"{int(v):,}" if v is not None else "-"
templates.env.filters["n"] = lambda v: f"{(v or 0):g}"


def get_con():
    con = db.connect(os.environ.get("APP_DB", db.DEFAULT_DB))
    try:
        yield con
    finally:
        con.close()


class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    return RedirectResponse("/login", status_code=303)


def current(request: Request, con=Depends(get_con)) -> dict:
    uid = request.session.get("uid")
    ctx = auth.context(con, uid) if uid else None
    if not ctx:
        request.session.clear()
        raise NeedLogin()
    return ctx


def guard(request: Request, ctx: dict = Depends(current), con=Depends(get_con)) -> dict:
    """요청한 주소가 메뉴에 있고 그 역할에게 열려 있는지 본다. 화면이 아니라 여기서 막는다."""
    item = menu.find(request.url.path)
    if item is None:
        raise HTTPException(404, "없는 화면입니다.")
    if ctx["role"] not in item["roles"]:
        auth.log(con, ctx["login_id"], "access_denied", request.url.path)
        raise HTTPException(403, "이 화면을 볼 권한이 없습니다.")
    return {**ctx, "item": item, "scope": item["roles"][ctx["role"]]}


def page(request, ctx, name, **kw):
    cfg = menu.load()
    return templates.TemplateResponse(request, name, {
        "ctx": ctx, "groups": menu.for_role(ctx["role"]), "mobile_tabs": cfg["mobile_tabs"], "item": ctx.get("item"),
        "path": request.url.path, "SCOPES": menu.SCOPES, "PHASES": menu.PHASES, "STATUS": menu.STATUS,
        "LEVEL_KO": auth.LEVEL_KO, "HEAD_KO": auth.HEAD_KO, **kw})


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "error.html", {"code": exc.status_code, "message": exc.detail}, status_code=exc.status_code)


# ---------------------------------------------------------------- 로그인
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, login_id: str = Form(...), password: str = Form(...), con=Depends(get_con)):
    user, error = auth.login(con, login_id, password)
    if not user:
        return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=401)
    request.session.clear()
    request.session["uid"] = user["user_id"]
    return RedirectResponse("/me" if user["must_change_password"] else "/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


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


TIMING_KO = {"next_month": "익월", "inst13": "13회차", "next_month+inst13": "익월과 13회차", "year1": "1차년", "year2": "2차년"}


def _pct(rate):
    return f"{rate * 100:g}%"


def _promos(con, ym, limit=6):
    """게시 기능을 만들기 전까지는 시상안 원장의 설계사 몫 줄을 미리보기로 보여 준다."""
    doc = con.execute("SELECT plan_doc_id FROM incentive_plan_doc WHERE status!='superseded' ORDER BY period_ym=? DESC, plan_doc_id DESC LIMIT 1", (ym,)).fetchone()
    if not doc:
        return [], 0
    rows = _q(con, """SELECT l.line_id, l.insurer_name, l.category_raw, l.period_raw, l.period_to, l.reward_raw, l.pay_timing, l.parse_status,
                             (SELECT GROUP_CONCAT(c.rate, '|') FROM incentive_plan_component c WHERE c.line_id=l.line_id) rates
                      FROM incentive_plan_line l WHERE l.plan_doc_id=? AND l.recipient_level='fc'
                      ORDER BY l.period_to IS NULL, l.period_to, l.line_id""", (doc["plan_doc_id"],))
    today = date.today()
    for r in rows[:limit]:
        rates = [float(x) for x in (r["rates"] or "").split("|") if x]
        if len(rates) == 1 and r["parse_status"] == "auto":
            r["rate_kind"], r["rate_label"] = "one", _pct(rates[0])
        elif r["reward_raw"] and rates:
            r["rate_kind"], r["rate_label"] = "text", r["reward_raw"]        # 조건이 여럿이면 숫자 하나로 줄이지 않는다
        else:
            r["rate_kind"], r["rate_label"] = "none", None
        r["timing"] = TIMING_KO.get(r["pay_timing"])
        if r["period_to"]:
            end = date.fromisoformat(r["period_to"])
            left = (end - today).days
            r["due"] = "오늘 마감" if left == 0 else (f"D-{left}" if left > 0 else f"{end.month}월 {end.day}일 종료")
            r["due_class"] = "bad" if 0 <= left <= 3 else ("blue" if left > 3 else "")
        else:
            r["due"], r["due_class"] = "기간 확인", "warn"
    return rows[:limit], len(rows)


def _goal(con, ym, agent_id):
    """분기 인정보험료 목표가 있으면 달성률을 낸다."""
    key = period_key("quarter", ym)
    g = con.execute("SELECT target_value FROM agent_goal WHERE agent_id=? AND period_type='quarter' AND period_key=? AND metric='converted_premium'", (agent_id, key)).fetchone()
    if not g or not g["target_value"]:
        return None
    v = con.execute("""SELECT s.converted_premium v FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                       WHERE s.period_type='quarter' AND s.period_key=? AND s.agent_id=?""", (ym, key, agent_id)).fetchone()
    value = (v["v"] if v else 0) or 0
    return {"label": f"{key[:4]}년 {key[-1]}분기", "target": g["target_value"], "value": value,
            "pct": value / g["target_value"] * 100, "left": max(g["target_value"] - value, 0)}


# ---------------------------------------------------------------- 화면
@app.get("/", response_class=HTMLResponse)
def home(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    ym, role = ctx["ym"], ctx["role"]
    data = {"last_load": _last_load(con)}
    if data["last_load"]:
        t = datetime.strptime(data["last_load"], "%Y-%m-%d %H:%M:%S")
        data["last_load_label"] = f"{t.month}월 {t.day}일 {t.hour}시 {t.minute:02d}분"
    data["promos"], data["promo_total"] = _promos(con, ym)
    if ctx["agent_id"]:
        mine = _perf(con, ym, [ctx["agent_id"]])
        data["mine"] = mine[0] if mine else None
        r = con.execute("""SELECT s.rank_no FROM ranking_snapshot s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                           WHERE s.period_type='month' AND s.period_key=? AND s.metric='recognized_premium' AND s.scope='all' AND s.agent_id=?""",
                        (ym, ym, ctx["agent_id"])).fetchone()
        data["my_rank"] = r["rank_no"] if r else None
        data["rank_total"] = con.execute("""SELECT COUNT(*) FROM ranking_snapshot s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                                            WHERE s.period_type='month' AND s.period_key=? AND s.metric='recognized_premium' AND s.scope='all'""", (ym, ym)).fetchone()[0]
        data["goal"] = _goal(con, ym, ctx["agent_id"])
    if role in ("manager", "main"):
        team = _perf(con, ym, ctx["agent_ids"] if role == "manager" else [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent")])
        data["team"] = {"n": len(team), "active": sum(1 for t in team if (t["net_count"] or 0) > 0),
                        "premium": sum(t["converted_premium"] or 0 for t in team), "count": sum(t["net_count"] or 0 for t in team), "top": team[:5]}
    if role == "main":
        one = lambda sql, p=(): con.execute(sql, p).fetchone()[0]
        data["ops"] = {
            "batches": one("SELECT COUNT(*) FROM import_batch WHERE period_ym=?", (ym,)),
            "block": one("SELECT COUNT(*) FROM quality_issue q JOIN import_batch b ON b.batch_id=q.batch_id WHERE b.period_ym=? AND q.severity='block'", (ym,)),
            "warn": one("SELECT COUNT(*) FROM quality_issue q JOIN import_batch b ON b.batch_id=q.batch_id WHERE b.period_ym=? AND q.severity='warn'", (ym,)),
            "plan_manual": one("SELECT COUNT(*) FROM incentive_plan_line l JOIN incentive_plan_doc d ON d.plan_doc_id=l.plan_doc_id WHERE d.status!='superseded' AND l.parse_status='manual' AND l.review_status='draft'"),
            "accounts": one("SELECT COUNT(*) FROM user_account WHERE status='active'"),
            "closed": bool(con.execute("SELECT 1 FROM close_period WHERE period_ym=? AND status='closed'", (ym,)).fetchone()),
        }
    return page(request, ctx, "home.html", d=data)


@app.get("/team/members", response_class=HTMLResponse)
def team_members(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    ids = ctx["agent_ids"] if ctx["scope"] == "org" else [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent")]
    return page(request, ctx, "team_members.html", rows=_perf(con, ctx["ym"], ids), last_load=_last_load(con))


@app.get("/org/tree", response_class=HTMLResponse)
def org_tree(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    ym = ctx["ym"]
    q, ids = _in(ctx["org_ids"])
    orgs = _q(con, f"""SELECT o.org_id, o.parent_id, o.name, o.level,
                         (SELECT GROUP_CONCAT(a.name, ', ') FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                           WHERE m.org_id=o.org_id AND m.effective_from<=? AND (m.effective_to IS NULL OR m.effective_to>=?)) head,
                         (SELECT COUNT(*) FROM agent_org_history h WHERE h.org_id=o.org_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)) members
                       FROM org_unit o WHERE o.org_id IN ({q}) AND o.active=1 ORDER BY o.org_id""", (ym, ym, ym, ym, *ids))
    by_parent, known = {}, {o["org_id"] for o in orgs}
    for o in orgs:
        by_parent.setdefault(o["parent_id"] if o["parent_id"] in known else None, []).append(o)
    flat = []

    def walk(parent, depth):
        for o in by_parent.get(parent, []):
            flat.append({**o, "depth": depth})
            walk(o["org_id"], depth + 1)
    walk(None, 0)
    return page(request, ctx, "org_tree.html", orgs=flat)


@app.get("/org/accounts", response_class=HTMLResponse)
def org_accounts(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    rows = _q(con, """SELECT u.user_id, u.login_id, u.display_name, u.is_main_admin, u.status, u.must_change_password, u.locked_until, u.last_login_at,
                             (SELECT COUNT(*) FROM org_manager m WHERE m.agent_id=u.agent_id AND m.effective_to IS NULL) heads
                      FROM user_account u ORDER BY u.is_main_admin DESC, u.login_id""")
    return page(request, ctx, "org_accounts.html", rows=rows, now=db.now())


@app.post("/org/accounts")
def org_accounts_action(request: Request, user_id: int = Form(...), action: str = Form(...), ctx: dict = Depends(guard), con=Depends(get_con)):
    target = con.execute("SELECT * FROM user_account WHERE user_id=?", (user_id,)).fetchone()
    if not target or action not in ("suspend", "activate", "unlock") or (action == "suspend" and target["user_id"] == ctx["user_id"]):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    if action == "unlock":
        con.execute("UPDATE user_account SET failed_count=0, locked_until=NULL WHERE user_id=?", (user_id,))
    else:
        con.execute("UPDATE user_account SET status=? WHERE user_id=?", ("suspended" if action == "suspend" else "active", user_id))
    auth.log(con, ctx["login_id"], f"account_{action}", target["login_id"])
    return RedirectResponse("/org/accounts", status_code=303)


@app.get("/org/audit", response_class=HTMLResponse)
def org_audit(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return page(request, ctx, "org_audit.html", rows=_q(con, "SELECT at, actor, action, target FROM audit_log ORDER BY id DESC LIMIT 200"))


@app.get("/me", response_class=HTMLResponse)
def me(request: Request, ctx: dict = Depends(guard)):
    return page(request, ctx, "me.html", message=None, error=None)


@app.post("/me", response_class=HTMLResponse)
def me_password(request: Request, old: str = Form(...), new: str = Form(...), new2: str = Form(...), ctx: dict = Depends(guard), con=Depends(get_con)):
    u = con.execute("SELECT password_hash FROM user_account WHERE user_id=?", (ctx["user_id"],)).fetchone()
    error = None
    if not auth.verify_password(old, u["password_hash"]):
        error = "지금 비밀번호가 맞지 않습니다."
    elif new != new2:
        error = "새 비밀번호 두 칸이 서로 다릅니다."
    elif len(new) < 8 or new == old:
        error = "새 비밀번호는 8자 이상이고 지금 것과 달라야 합니다."
    if error:
        return page(request, ctx, "me.html", message=None, error=error)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0 WHERE user_id=?", (auth.hash_password(new), ctx["user_id"]))
    auth.log(con, ctx["login_id"], "password_change")
    return page(request, {**ctx, "must_change_password": False}, "me.html", message="비밀번호를 바꿨습니다.", error=None)


@app.get("/{rest:path}", response_class=HTMLResponse)
def planned(request: Request, ctx: dict = Depends(guard)):
    """아직 새 화면이 없는 메뉴: 무엇이 들어오는지, 어느 단계인지, 지금 어디서 쓰는지를 보여 준다."""
    return page(request, ctx, "planned.html")
