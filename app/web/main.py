"""웹 화면의 진입점. 로그인, 역할별 메뉴, 조직 범위 검사를 여기서 강제한다.

    uvicorn app.web.main:app --port 8000        # APP_DB 로 DB 파일, APP_SECRET 으로 세션 키를 바꾼다
"""
import os
import pathlib
import secrets
from datetime import date, datetime
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.engine import org_award as oa
from app.engine import promotion as pm
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
templates.env.filters["md"] = lambda d: f"{int(d[5:7])}월 {int(d[8:10])}일" if d else ""


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
        "LEVEL_KO": auth.LEVEL_KO, "HEAD_KO": auth.HEAD_KO, "pm": pm, **kw})


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
    cards, _ = _cards(con, ctx)
    data["cards"] = [c for c in cards if c["recipient_level"] == "fc" and c["phase"] != "ended"][:6] or [c for c in cards if c["recipient_level"] == "fc"][:6]
    data["promos"], data["promo_total"] = ([], 0) if data["cards"] else _promos(con, ym)
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


# ---------------------------------------------------------------- 프로모션
def _period_text(p):
    md = lambda d: f"{int(d[5:7])}월 {int(d[8:10])}일"
    if len(p["windows"]) == 1:
        return f"{md(p['date_from'])} ~ {md(p['date_to'])}"
    return " · ".join(f"{w['label']} {md(w['date_from'])}~{md(w['date_to'])}" for w in p["windows"])


def _cards(con, ctx, include_unpublished=False):
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


def _scope_agents(con, ctx):
    ids = ctx["agent_ids"] if ctx["role"] != "main" else {r["agent_id"] for r in con.execute("SELECT agent_id FROM agent WHERE status='active'")}
    q, lst = _in(ids)
    ym = ctx["ym"]
    return _q(con, f"""SELECT a.agent_id, a.internal_code code, a.name, o.name org FROM agent a
                       LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
                       LEFT JOIN org_unit o ON o.org_id=h.org_id WHERE a.agent_id IN ({q}) AND a.status='active' ORDER BY a.internal_code""", (ym, ym, *lst))


@app.get("/promo/list", response_class=HTMLResponse)
def promo_list(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    cards, ref = _cards(con, ctx)
    return page(request, ctx, "promo_list.html", cards=cards, ref=ref.isoformat())


@app.get("/promo/progress", response_class=HTMLResponse)
def promo_progress(request: Request, p: int | None = None, view: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    cards, ref = _cards(con, ctx)
    cards = [c for c in cards if c["recipient_level"] == "fc"]
    team_view = ctx["role"] == "main" or (ctx["role"] == "manager" and view != "me")
    if not team_view:
        mine = [{**c, "r": pm.progress(con, c, [ctx["agent_id"]], ref)[ctx["agent_id"]]} for c in cards] if ctx["agent_id"] else []
        return page(request, ctx, "promo_progress_me.html", items=mine, ref=ref.isoformat())
    sel = next((c for c in cards if c["promo_id"] == p), cards[0] if cards else None)
    rows, summary = [], None
    if sel:
        agents = _scope_agents(con, ctx)
        res = pm.progress(con, sel, [a["agent_id"] for a in agents], ref)
        rows = [{**a, "r": res[a["agent_id"]]} for a in agents]
        rows.sort(key=lambda x: -x["r"]["value"])
        near = [x for x in rows if x["r"]["next"] and x["r"]["value"] > 0 and x["r"]["next"]["gap"] <= x["r"]["next"]["min_value"] * 0.3]
        summary = {"n": len(rows), "achieved": sum(1 for x in rows if x["r"]["tier"] or (sel["kind"] == "rate" and x["r"]["value"] > 0)),
                   "near": len(near), "missed": sum(1 for x in rows if x["r"]["broken"]), "top": max((x["r"]["value"] for x in rows), default=0)}
    return page(request, ctx, "promo_progress_team.html", cards=cards, sel=sel, rows=rows, summary=summary, ref=ref.isoformat(), show_amount=ctx["role"] == "main")


@app.get("/promo/publish", response_class=HTMLResponse)
def promo_publish(request: Request, msg: str | None = None, err: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    doc = con.execute("SELECT plan_doc_id, file_name, period_ym FROM incentive_plan_doc WHERE status!='superseded' ORDER BY period_ym=? DESC, plan_doc_id DESC LIMIT 1", (ctx["ym"],)).fetchone()
    lines = _q(con, """SELECT l.line_id, l.insurer_name, l.category_raw, l.period_raw, l.period_from, l.criteria_raw, l.reward_raw, l.line_kind, l.parse_status, l.parse_note,
                              (SELECT p.promo_id FROM promotion p WHERE p.source_line_id=l.line_id AND p.status!='withdrawn' LIMIT 1) promo_id
                       FROM incentive_plan_line l WHERE l.plan_doc_id=? AND l.recipient_level='fc' ORDER BY l.line_id""", (doc["plan_doc_id"],)) if doc else []
    for ln in lines:
        ln["direct"] = ln["line_kind"] == "rate_promo" and ln["parse_status"] == "auto" and bool(ln["period_from"])
    cards, _ = _cards(con, ctx, include_unpublished=True)
    cats = _q(con, "SELECT code, name FROM product_category ORDER BY sort_order")
    return page(request, ctx, "promo_publish.html", doc=dict(doc) if doc else None, lines=lines, cards=cards, cats=cats, msg=msg, err=err,
                waiting=sum(1 for ln in lines if not ln["promo_id"]))


@app.post("/promo/publish")
async def promo_publish_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    f = await request.form()
    action, by = f.get("action"), ctx["login_id"]
    try:
        if action == "publish_line":
            pm.publish_from_line(con, int(f["line_id"]), by, categories=[f["category"]] if f.get("category") else None)
            msg = "승인하고 게시했습니다."
        elif action in ("published", "withdrawn"):
            pm.set_status(con, int(f["promo_id"]), action, by)
            msg = "게시했습니다." if action == "published" else "회수했습니다."
        elif action == "delete":
            pm.delete_draft(con, int(f["promo_id"]))
            msg = "지웠습니다."
        else:
            raise ValueError("처리할 수 없는 요청입니다.")
    except (ValueError, KeyError) as e:
        return RedirectResponse(f"/promo/publish?err={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/promo/publish?msg={quote(msg)}", status_code=303)


def _tier_form_defaults(con, ctx, line_id=None, promo_id=None):
    d = {"promo_id": "", "name": "", "kind": "weekly", "insurer_id": None, "insurer_name": "", "sector": "", "caution": "", "target_note": "",
         "metric": "premium_monthly", "date_basis": "contract_date", "cap": "", "categories": [], "combine_rule": "", "pay_timing": "",
         "windows": [("", "", "")] * 3, "tiers": [("", "fixed", "", "")] * 6, "rates": [], "source_line_id": "", "origin": None, "status": None}
    num = lambda v: "" if v is None else f"{v:g}"
    if promo_id:
        p = pm._load(con, promo_id)
        if not p:
            raise HTTPException(404, "없는 프로모션입니다.")
        tiers = [(num(t["min_value"]), t["reward_kind"], num(t["reward_value"] * 100 if t["reward_kind"] == "rate" and t["reward_value"] is not None else t["reward_value"]),
                  t["reward_label"] or "") for t in p["tiers"]]
        d.update(promo_id=str(promo_id), name=p["name"], kind=p["kind"], insurer_id=p["insurer_id"], insurer_name=p["insurer_name"] or "", sector=p["sector"] or "",
                 caution=p["caution"] or "", target_note=p["target_note"] or "", metric=p["metric"], date_basis=p["date_basis"], cap=num(p["per_contract_cap"]),
                 categories=p["category_list"], combine_rule=p["combine_rule"] if len(p["windows"]) > 1 else "", pay_timing=p["pay_timing"] or "",
                 windows=([(w["label"], w["date_from"], w["date_to"]) for w in p["windows"]] + [("", "", "")] * 3)[:3],
                 tiers=(tiers + [("", "fixed", "", "")] * 6)[:6], rates=p["rates"], status=p["status"])
    elif line_id:
        ln = con.execute("SELECT * FROM incentive_plan_line WHERE line_id=? AND recipient_level='fc'", (line_id,)).fetchone()
        if ln:
            origin = " / ".join(x for x in (ln["period_raw"], ln["criteria_raw"], ln["reward_raw"]) if x)
            d.update(name=ln["category_raw"] or "", kind=pm.FROM_LINE_KIND.get(ln["line_kind"], "weekly") if ln["line_kind"] != "rate_promo" else "weekly",
                     insurer_name=ln["insurer_name"], sector=ln["sector"] or "", caution=ln["reward_raw"] or "", source_line_id=str(line_id), origin=origin,
                     windows=[("", ln["period_from"] or "", ln["period_to"] or ""), ("", "", ""), ("", "", "")])
    return d


@app.get("/promo/tiers", response_class=HTMLResponse)
def promo_tiers(request: Request, line: int | None = None, edit: int | None = None, err: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    return page(request, ctx, "promo_tiers.html", d=_tier_form_defaults(con, ctx, line, edit), err=err,
                cats=_q(con, "SELECT code, name FROM product_category ORDER BY sort_order"),
                insurers=_q(con, "SELECT insurer_id, name, sector FROM insurer WHERE active=1 ORDER BY name"))


@app.post("/promo/tiers")
async def promo_tiers_save(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    f = await request.form()
    num = lambda v: float(str(v).replace(",", "").strip()) if str(v or "").strip() else None
    promo_id = int(f["promo_id"]) if f.get("promo_id") else None
    try:
        windows = [(f.get(f"w_label_{i}", "").strip(), f.get(f"w_from_{i}", ""), f.get(f"w_to_{i}", "")) for i in range(1, 4)]
        tiers = [(num(f.get(f"t_min_{i}")), f.get(f"t_kind_{i}", "fixed"), num(f.get(f"t_value_{i}")), f.get(f"t_label_{i}", "").strip())
                 for i in range(1, 7) if num(f.get(f"t_min_{i}")) is not None]
        for m, k, v, lab in tiers:
            if k in ("fixed", "rate") and v is None:
                raise ValueError("정액과 비율 보상에는 값을 넣어야 합니다.")
            if k in ("travel", "goods") and not lab:
                raise ValueError("여행과 물품 보상에는 이름을 넣어야 합니다.")
        tiers = [(m, k, (v / 100 if k == "rate" else v), lab) for m, k, v, lab in tiers]
        ins = con.execute("SELECT insurer_id, name FROM insurer WHERE insurer_id=?", (int(f["insurer_id"]),)).fetchone() if f.get("insurer_id") else None
        if not f.get("name", "").strip():
            raise ValueError("이름을 넣어야 합니다.")
        common = dict(name=f["name"], windows=windows, tiers=tiers, by=ctx["login_id"], kind=f.get("kind", "weekly"),
                      insurer_id=ins["insurer_id"] if ins else None, insurer_name=(ins["name"] if ins else f.get("insurer_name", "").strip() or None),
                      sector=f.get("sector") or None, categories=f.getlist("categories"), target_note=f.get("target_note", "").strip() or None,
                      metric=f.get("metric", "premium_monthly"), date_basis=f.get("date_basis", "contract_date"), per_contract_cap=num(f.get("cap")),
                      combine_rule=f.get("combine_rule") or None, pay_timing=f.get("pay_timing") or None, caution=f.get("caution", "").strip() or None)
        if promo_id:
            res = pm.update(con, promo_id, **common)
            msg = "고쳤습니다. 바뀐 항목 " + str(len(res["changed"])) + "개를 변경 기록에 남겼습니다." if res["changed"] else "바뀐 것이 없습니다."
        else:
            pm.create(con, period_ym=ctx["ym"], source_line_id=int(f["source_line_id"]) if f.get("source_line_id") else None,
                      publish=f.get("action") == "publish", **common)
            if f.get("source_line_id"):
                con.execute("UPDATE incentive_plan_line SET review_status='approved', reviewed_by=?, reviewed_at=? WHERE line_id=?", (ctx["login_id"], db.now(), int(f["source_line_id"])))
                con.commit()
            msg = "프로모션을 " + ("게시했습니다." if f.get("action") == "publish" else "초안으로 저장했습니다.")
    except (ValueError, KeyError) as e:
        back = f"/promo/tiers?err={quote(str(e))}" + (f"&edit={promo_id}" if promo_id else (f"&line={f.get('source_line_id')}" if f.get("source_line_id") else ""))
        return RedirectResponse(back, status_code=303)
    return RedirectResponse(f"/promo/publish?msg={quote(msg)}", status_code=303)


# ---------------------------------------------------------------- 조직 부문 시상
def _manageable_orgs(con, ctx):
    q, ids = _in(ctx["org_ids"])
    return _q(con, f"SELECT org_id, name, level FROM org_unit WHERE org_id IN ({q}) AND active=1 ORDER BY org_id", tuple(ids)) if ctx["role"] in ("main", "manager") else []


@app.get("/promo/org-awards", response_class=HTMLResponse)
def org_awards(request: Request, edit: int | None = None, new: int | None = None, msg: str | None = None, err: str | None = None,
               ctx: dict = Depends(guard), con=Depends(get_con)):
    ym = ctx["ym"]
    manage = ctx["role"] in ("main", "manager")
    blocks = []
    for d in oa.visible(con, ctx, ym, include_inactive=manage):
        mine = oa.can_manage(ctx, d["org_id"])
        if d["status"] != "active" and not mine:
            continue
        rows = oa.standing(con, d, ym)
        done = oa.confirmed(con, d["division_id"], ym)
        me = next((r for r in rows if r["agent_id"] == ctx["agent_id"]), None) if ctx["agent_id"] else None
        blocks.append({**d, "rows": oa.view_rows(rows, ctx), "total": len(rows), "podium": (done or [r for r in rows if r["winner"]])[:3], "confirmed": done,
                       "me": me, "top": rows[0]["value"] if rows else 0, "can_manage": mine})
    form = None
    if manage and (edit or new):
        d = oa.load(con, edit) if edit else None
        if edit and (not d or not oa.can_manage(ctx, d["org_id"])):
            raise HTTPException(403, "이 부문을 고칠 권한이 없습니다.")
        form = d or {"division_id": "", "org_id": None, "name": "", "metric": "converted_premium", "sector": None, "category_list": [], "top_n": 3,
                     "min_value": None, "effective_from": ym, "effective_to": None, "note": "", "rewards": {}}
    return page(request, ctx, "org_awards.html", blocks=blocks, form=form, manage=manage, msg=msg, err=err, oa=oa,
                orgs=_manageable_orgs(con, ctx), cats=_q(con, "SELECT code, name FROM product_category ORDER BY sort_order"))


@app.post("/promo/org-awards")
async def org_awards_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    f = await request.form()
    action, by = f.get("action"), ctx["login_id"]

    def deny():
        auth.log(con, by, "access_denied", f"/promo/org-awards {action}")
        raise HTTPException(403, "이 부문을 다룰 권한이 없습니다.")
    try:
        if action == "save":
            org_id = int(f["org_id"])
            division_id = int(f["division_id"]) if f.get("division_id") else None
            old = oa.load(con, division_id) if division_id else None
            if not oa.can_manage(ctx, org_id) or (old and not oa.can_manage(ctx, old["org_id"])):
                deny()
            oa.save(con, by=by, org_id=org_id, name=f.get("name", ""), metric=f.get("metric", ""), sector=f.get("sector") or None, categories=f.getlist("categories"),
                    top_n=f.get("top_n") or 0, min_value=f.get("min_value") or None, effective_from=f.get("effective_from", ""), effective_to=f.get("effective_to") or None,
                    note=f.get("note", "").strip() or None, rewards=[(i, f.get(f"reward_{i}", "")) for i in range(1, 6)], division_id=division_id)
            msg = "부문을 고쳤습니다." if division_id else "부문을 만들었습니다."
        elif action in ("active", "inactive", "confirm"):
            d = oa.load(con, int(f["division_id"]))
            if not d or (action == "confirm" and ctx["role"] != "main") or not oa.can_manage(ctx, d["org_id"]):
                deny()
            if action == "confirm":
                msg = f"{d['name']}의 수상자 {oa.confirm(con, d['division_id'], ctx['ym'], by)}명을 확정했습니다."
            else:
                oa.set_status(con, d["division_id"], action, by)
                msg = "부문을 다시 열었습니다." if action == "active" else "부문을 닫았습니다."
        else:
            raise ValueError("처리할 수 없는 요청입니다.")
    except (ValueError, KeyError) as e:
        back = "&edit=" + f["division_id"] if f.get("division_id") and action == "save" else ("&new=1" if action == "save" else "")
        return RedirectResponse(f"/promo/org-awards?err={quote(str(e))}{back}", status_code=303)
    return RedirectResponse(f"/promo/org-awards?msg={quote(msg)}", status_code=303)


@app.get("/{rest:path}", response_class=HTMLResponse)
def planned(request: Request, ctx: dict = Depends(guard)):
    """아직 새 화면이 없는 메뉴: 무엇이 들어오는지, 어느 단계인지, 지금 어디서 쓰는지를 보여 준다."""
    return page(request, ctx, "planned.html")
