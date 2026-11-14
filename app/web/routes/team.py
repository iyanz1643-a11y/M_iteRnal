"""팀 관리 묶음."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.web.deps import _last_load, _perf, get_con, guard, page

router = APIRouter()


@router.get("/team/members", response_class=HTMLResponse)
def team_members(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    ids = ctx["agent_ids"] if ctx["scope"] == "org" else [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent")]
    return page(request, ctx, "team_members.html", rows=_perf(con, ctx["ym"], ids), last_load=_last_load(con))
