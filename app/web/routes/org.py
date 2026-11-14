"""조직과 계정 묶음: 조직 계층, 계정, 변경 기록."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.web import auth
from app.web.deps import _in, _q, get_con, guard, page

router = APIRouter()


@router.get("/org/tree", response_class=HTMLResponse)
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


@router.get("/org/accounts", response_class=HTMLResponse)
def org_accounts(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    rows = _q(con, """SELECT u.user_id, u.login_id, u.display_name, u.is_main_admin, u.status, u.must_change_password, u.locked_until, u.last_login_at,
                             (SELECT COUNT(*) FROM org_manager m WHERE m.agent_id=u.agent_id AND m.effective_to IS NULL) heads
                      FROM user_account u ORDER BY u.is_main_admin DESC, u.login_id""")
    return page(request, ctx, "org_accounts.html", rows=rows, now=db.now())


@router.post("/org/accounts")
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


@router.get("/org/audit", response_class=HTMLResponse)
def org_audit(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return page(request, ctx, "org_audit.html", rows=_q(con, "SELECT at, actor, action, target FROM audit_log ORDER BY id DESC LIMIT 200"))
