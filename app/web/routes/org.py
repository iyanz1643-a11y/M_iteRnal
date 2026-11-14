"""조직과 계정 묶음: 조직 계층, 조직장과 대리, 계정, 변경 기록."""
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.org import heads as hd
from app.org import levels
from app.org.sync import org_texts, termination_candidates
from app.web import auth
from app.web.deps import _in, _q, get_con, guard, page

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}


def _active_members(con, ym, org_ids) -> dict:
    """{org_id: 그 조직에 바로 속한 재직 설계사 수}"""
    q, ids = _in(org_ids)
    return {r["org_id"]: r["n"] for r in con.execute(
        f"""SELECT h.org_id, COUNT(DISTINCT h.agent_id) n FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id AND a.status='active'
            WHERE h.org_id IN ({q}) AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?) GROUP BY h.org_id""", (*ids, ym, ym))}


def _heads_by_org(con, ym, org_ids) -> dict:
    """{org_id: [{name, code, role}]}. 조직장은 자료 기준 달, 대리는 오늘 날짜로 따진다."""
    q, ids = _in(org_ids)
    today = date.today().isoformat()
    out = {}
    for r in con.execute(f"""SELECT m.org_id, m.role, a.name, a.internal_code code, NULL date_to FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                             WHERE m.org_id IN ({q}) AND m.effective_from<=? AND (m.effective_to IS NULL OR m.effective_to>=?)
                             UNION ALL
                             SELECT d.org_id, 'deputy', a.name, a.internal_code, d.date_to FROM org_deputy d JOIN agent a ON a.agent_id=d.agent_id
                             WHERE d.org_id IN ({q}) AND d.ended_at IS NULL AND d.date_from<=? AND d.date_to>=?""", (*ids, ym, ym, *ids, today, today)):
        out.setdefault(r["org_id"], []).append(dict(r))
    return out


@router.get("/org/tree", response_class=HTMLResponse)
def org_tree(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    ym = ctx["ym"]
    q, ids = _in(ctx["org_ids"])
    orgs = _q(con, f"SELECT org_id, parent_id, name, level FROM org_unit WHERE org_id IN ({q}) AND active=1", tuple(ids))
    direct, heads = _active_members(con, ym, ids), _heads_by_org(con, ym, ids)
    by_parent, known = {}, {o["org_id"] for o in orgs}
    for o in sorted(orgs, key=lambda o: (levels.depth(o["level"]), o["name"], o["org_id"])):
        by_parent.setdefault(o["parent_id"] if o["parent_id"] in known else None, []).append(o)
    flat = []

    def walk(parent, depth):
        total = 0
        for o in by_parent.get(parent, []):
            row = {**o, "depth": depth, "direct": direct.get(o["org_id"], 0), "heads": heads.get(o["org_id"], [])}
            flat.append(row)
            row["below"] = walk(o["org_id"], depth + 1)
            total += row["direct"] + row["below"]
        return total
    walk(None, 0)
    counts = {lv: sum(1 for o in orgs if o["level"] == lv) for lv in levels.codes()}
    return page(request, ctx, "org_tree.html", orgs=flat, counts=counts, levels=levels.load()["levels"])


# ---------------------------------------------------------------- 조직장과 대리
DONE = {"assign": "조직장을 지정했습니다.", "end": "조직장 지정을 끝냈습니다. 이번 달부터 설계사로 돌아갑니다.",
        "deputy": "대리를 지정했습니다.", "deputy_end": "대리 지정을 끝냈습니다."}


def _heads_page(request, ctx, con, err=None, msg=None, status_code=200):
    ym, today = ctx["ym"], date.today().isoformat()
    texts = org_texts(con)
    rows = _q(con, """SELECT m.id, m.org_id, m.role, m.effective_from, m.effective_to, m.source, a.internal_code code, a.name, o.level
                      FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id
                      WHERE m.effective_to IS NULL OR m.effective_to>=?""", (ym,))
    for r in rows:
        r["org"] = texts.get(r["org_id"], "")
        r["now"] = r["effective_from"] <= ym
    rows.sort(key=lambda r: (levels.depth(r["level"]), r["org"]))
    deputies = _q(con, """SELECT d.id, d.org_id, d.date_from, d.date_to, d.reason, a.internal_code code, a.name FROM org_deputy d
                          JOIN agent a ON a.agent_id=d.agent_id WHERE d.ended_at IS NULL AND d.date_to>=? ORDER BY d.date_from""", (today,))
    for d in deputies:
        d["org"], d["now"] = texts.get(d["org_id"], ""), d["date_from"] <= today
    headed = {r["org_id"] for r in rows if r["now"]}
    choices = [{"org_id": o["org_id"], "text": texts.get(o["org_id"], o["name"]), "level": o["level"]}
               for o in con.execute("SELECT org_id, name, level FROM org_unit WHERE active=1") if levels.head_role(o["level"])]
    choices.sort(key=lambda c: c["text"])
    vacant = [c for c in choices if c["org_id"] not in headed]
    return page(request, ctx, "org_heads.html", rows=rows, deputies=deputies, choices=choices, vacant=vacant, err=err, msg=msg,
                today=today, status_code=status_code)


@router.get("/org/heads", response_class=HTMLResponse)
def org_heads(request: Request, done: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    return _heads_page(request, ctx, con, msg=DONE.get(done))


@router.post("/org/heads", response_class=HTMLResponse)
def org_heads_action(request: Request, action: str = Form(...), org_id: int | None = Form(None), code: str = Form(""), from_ym: str = Form(""),
                     to_ym: str = Form(""), date_from: str = Form(""), date_to: str = Form(""), reason: str = Form(""),
                     item_id: int | None = Form(None), ctx: dict = Depends(guard), con=Depends(get_con)):
    by = ctx["login_id"]
    try:
        if action in ("assign", "deputy"):
            a = con.execute("SELECT agent_id FROM agent WHERE internal_code=?", (code.strip().upper(),)).fetchone()
            if not a or org_id is None:
                raise hd.HeadError("조직과 사번을 바르게 넣으세요.")
            if action == "assign":
                hd.assign_head(con, org_id, a["agent_id"], from_ym.strip(), to_ym.strip() or None, by=by)
                auth.log(con, by, "org_head_assign", f"org={org_id} agent={code.strip().upper()} {from_ym.strip()}~{to_ym.strip()}")
            else:
                hd.assign_deputy(con, org_id, a["agent_id"], date_from, date_to, reason, by=by)
                auth.log(con, by, "org_deputy_assign", f"org={org_id} agent={code.strip().upper()} {date_from}~{date_to}")
        elif action == "end" and item_id is not None:
            m = hd.end_head(con, item_id, ctx["ym"])
            auth.log(con, by, "org_head_end", f"org={m['org_id']} agent_id={m['agent_id']} role={m['role']}")
        elif action == "deputy_end" and item_id is not None:
            d = hd.end_deputy(con, item_id, by=by)
            auth.log(con, by, "org_deputy_end", f"org={d['org_id']} agent_id={d['agent_id']}")
        else:
            raise HTTPException(400, "처리할 수 없는 요청입니다.")
    except hd.HeadError as e:
        con.rollback()
        return _heads_page(request, ctx, con, err=str(e), status_code=400)
    return RedirectResponse(f"/org/heads?done={action}", status_code=303)


# ---------------------------------------------------------------- 계정
def _accounts_page(request, ctx, con, issued=None, err=None, status_code=200):
    rows = _q(con, """SELECT u.user_id, u.login_id, u.display_name, u.is_main_admin, u.status, u.must_change_password, u.locked_until, u.last_login_at,
                             (SELECT COUNT(*) FROM org_manager m WHERE m.agent_id=u.agent_id AND m.effective_to IS NULL) heads
                      FROM user_account u ORDER BY u.is_main_admin DESC, u.login_id""")
    texts = org_texts(con)
    ym = ctx["ym"]
    no_account = _q(con, """SELECT a.agent_id, a.internal_code code, a.name, a.hire_date,
                                   (SELECT h.org_id FROM agent_org_history h WHERE h.agent_id=a.agent_id AND h.effective_from<=?
                                     AND (h.effective_to IS NULL OR h.effective_to>=?) ORDER BY h.effective_from DESC LIMIT 1) org_id
                            FROM agent a WHERE a.status='active' AND NOT EXISTS (SELECT 1 FROM user_account u WHERE u.agent_id=a.agent_id)
                            ORDER BY a.internal_code""", (ym, ym))
    for r in no_account:
        r["org"] = texts.get(r["org_id"], "")
    resp = page(request, ctx, "org_accounts.html", rows=rows, now=db.now(), no_account=no_account, leavers=termination_candidates(con),
                issued=issued or [], err=err, status_code=status_code)
    resp.headers.update(NO_STORE)
    return resp


@router.get("/org/accounts", response_class=HTMLResponse)
def org_accounts(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return _accounts_page(request, ctx, con)


@router.post("/org/accounts", response_class=HTMLResponse)
def org_accounts_action(request: Request, action: str = Form(...), user_id: int | None = Form(None), agent_id: int | None = Form(None),
                        ctx: dict = Depends(guard), con=Depends(get_con)):
    by = ctx["login_id"]
    if action in ("issue", "issue_all"):
        if action == "issue":
            if agent_id is None:
                raise HTTPException(400, "처리할 수 없는 요청입니다.")
            targets = [agent_id]
        else:
            targets = [r[0] for r in con.execute("""SELECT agent_id FROM agent a WHERE status='active'
                                                    AND NOT EXISTS (SELECT 1 FROM user_account u WHERE u.agent_id=a.agent_id) ORDER BY internal_code""")]
        issued = []
        for aid in targets:
            try:
                login_id, pw = auth.issue_account(con, aid)
            except ValueError as e:
                con.rollback()
                return _accounts_page(request, ctx, con, err=str(e), status_code=400)
            name = con.execute("SELECT display_name FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0]
            issued.append({"login_id": login_id, "name": name, "password": pw})
        con.commit()
        for x in issued:
            auth.log(con, by, "account_issue", x["login_id"])
        return _accounts_page(request, ctx, con, issued=issued)

    target = con.execute("SELECT * FROM user_account WHERE user_id=?", (user_id,)).fetchone() if user_id is not None else None
    if not target or action not in ("suspend", "activate", "unlock", "reset") or (action == "suspend" and target["user_id"] == ctx["user_id"]):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    if action == "reset":
        pw = auth.reset_password(con, target["user_id"])
        auth.log(con, by, "account_reset", target["login_id"])
        return _accounts_page(request, ctx, con, issued=[{"login_id": target["login_id"], "name": target["display_name"], "password": pw}])
    if action == "unlock":
        con.execute("UPDATE user_account SET failed_count=0, locked_until=NULL WHERE user_id=?", (target["user_id"],))
    else:
        con.execute("UPDATE user_account SET status=? WHERE user_id=?", ("suspended" if action == "suspend" else "active", target["user_id"]))
    auth.log(con, by, f"account_{action}", target["login_id"])
    return RedirectResponse("/org/accounts", status_code=303)


@router.get("/org/audit", response_class=HTMLResponse)
def org_audit(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return page(request, ctx, "org_audit.html", rows=_q(con, "SELECT at, actor, action, target FROM audit_log ORDER BY id DESC LIMIT 200"))
