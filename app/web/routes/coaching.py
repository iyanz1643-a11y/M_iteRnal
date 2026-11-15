"""팀 관리 묶음: 면담 기록 /team/coaching, 활동 깔때기 /team/activity. 저장과 숫자는 app/sales 의 coaching 과 stats 가 한다.

모든 주소가 guard 를 거친다. 범위는 guard 가 준 ctx 그대로다.

| 주소 | 하는 일 | 누가 |
| --- | --- | --- |
| GET /team/coaching | 팀원별 마지막 면담, 약속 사항, 다음 면담. ?agent= 로 한 사람의 이력과 기록 폼 | 관리자 자기 조직. 본인은 뺀다 |
| POST /team/coaching | 면담 한 건 남기기 | 같음. 범위 밖 팀원은 403 |
| GET /team/activity | 한 달의 통화, 상담, 제안, 청약, 체결 건수와 전환율. 사람별 표 | 메인 관리자 전체, 관리자 자기 조직 |

금액은 없다(D-17). 활동 깔때기의 청약은 설계사가 적은 활동이라 업적 집계의 청약과 다를 수 있다. 화면에 그 안내를 둔다.
"""
from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.org.sync import org_texts
from app.sales import coaching as co
from app.sales import core, stats
from app.web import auth
from app.web.deps import _in, _q, get_con, guard, page

router = APIRouter()
FLASH = "coach_flash"


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _members(con, ctx) -> list:
    """범위 안 재직 팀원. 본인은 뺀다. 소속 조직 이름을 붙인다."""
    ids = [a for a in sorted(ctx["agent_ids"]) if a != ctx.get("agent_id")] if ctx["scope"] != "all" else \
        [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent")]
    q, params = _in(ids)
    rows = _q(con, f"SELECT agent_id, internal_code code, name, hire_date FROM agent WHERE agent_id IN ({q}) AND status='active' ORDER BY internal_code", params)
    texts = org_texts(con)
    for r in rows:
        r["org"] = texts.get(core.home_org(con, r["agent_id"], ctx["ym"]), "")
    return rows


# ================================================================ 면담 기록
@router.get("/team/coaching", response_class=HTMLResponse)
def team_coaching(request: Request, agent: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    core.ensure(con)
    day = core.today()
    members = _members(con, ctx)
    last = co.latest(con, [m["agent_id"] for m in members])
    for m in members:
        r = last.get(m["agent_id"])
        m["last"] = r
        m["state"], m["left"] = co.state(r["next_on"] if r else None, day)
    order = {"over": 0, "soon": 1, "none": 2, "later": 3}
    members.sort(key=lambda m: (order[m["state"]], (m["last"] or {}).get("next_on") or "", m["code"]))
    picked = None
    if agent:
        aid = _int(agent)
        picked = next((m for m in members if m["agent_id"] == aid), None)
        if picked is None:
            auth.log(con, ctx["login_id"], "access_denied", f"/team/coaching agent:{agent}")
            raise HTTPException(404, "없는 팀원입니다.")
        picked = {**picked, "history": co.history(con, aid)}
    resp = page(request, ctx, "team_coaching.html", members=members, picked=picked, day=day.isoformat(), DUE_DAYS=co.DUE_DAYS,
                n_over=sum(1 for m in members if m["state"] == "over"), n_soon=sum(1 for m in members if m["state"] == "soon"),
                n_none=sum(1 for m in members if m["state"] == "none"), flash=request.session.pop(FLASH, None), PROMISE_MAX=co.PROMISE_MAX)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/team/coaching", response_class=HTMLResponse)
async def team_coaching_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    core.ensure(con)
    f = await request.form()
    if f.get("action") != "add":
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    aid = _int(f.get("agent_id"))
    if aid not in {m["agent_id"] for m in _members(con, ctx)} or not ctx.get("agent_id"):
        auth.log(con, ctx["login_id"], "access_denied", f"/team/coaching add:{aid}")
        raise HTTPException(403, "범위 안 팀원의 면담만 남깁니다.")
    try:
        co.add(con, ctx["agent_id"], aid, f.get("met_on"), f.get("promises"), f.get("next_on"), ctx["login_id"])
    except ValueError as e:
        con.rollback()
        request.session[FLASH] = {"kind": "bad", "text": str(e)}
        return RedirectResponse(f"/team/coaching?agent={aid}#form", status_code=303)
    con.commit()
    request.session[FLASH] = {"kind": "ok", "text": "면담을 남겼습니다."}
    return RedirectResponse(f"/team/coaching?agent={aid}", status_code=303)


# ================================================================ 활동 깔때기
def _month(m: str, day: date) -> tuple:
    """(YYYY-MM, 첫날, 끝날). 모르는 값이면 이번 달."""
    if not re.fullmatch(r"\d{4}-\d{2}", m or "") or not 1 <= int(m[5:]) <= 12:
        m = day.strftime("%Y-%m")
    y, mo = int(m[:4]), int(m[5:])
    first = date(y, mo, 1)
    nxt = date(y + (mo == 12), mo % 12 + 1, 1)
    return m, first.isoformat(), date.fromordinal(nxt.toordinal() - 1).isoformat()


def _months(day: date, n=6) -> list:
    out, y, m = [], day.year, day.month
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


@router.get("/team/activity", response_class=HTMLResponse)
def team_activity(request: Request, m: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    core.ensure(con)
    day = core.today()
    month, first, last = _month(m, day)
    ids = sorted(ctx["agent_ids"]) if ctx["scope"] == "org" else [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent")]
    fn = stats.funnel(con, ids, first, last)
    q, params = _in(list(fn["agents"]))
    names = {r["agent_id"]: r for r in _q(con, f"SELECT agent_id, internal_code code, name FROM agent WHERE agent_id IN ({q})", params)}
    texts = org_texts(con)
    rows = []
    for aid, v in fn["agents"].items():
        a = names.get(aid) or {"code": "-", "name": "-"}
        rows.append({"agent_id": aid, "code": a["code"], "name": a["name"], "org": texts.get(core.home_org(con, aid, ctx["ym"]), ""), **v,
                     "rate": stats.rate(v["won"], v["call"])})
    rows.sort(key=lambda r: (-r["won"], -r["application"], -r["call"], r["code"]))
    steps = [{"key": k, "label": core.KIND_KO.get(k, "체결"), "n": fn["total"][k], "sub": ""} for k in (*core.KINDS, "won")]
    for s, (_, _, rt) in zip(steps[1:], fn["rates"]):
        s["sub"] = f"앞 단계의 {rt:g}%" if rt is not None else "앞 단계 없음"
    return page(request, ctx, "team_activity.html", month=month, months=_months(day), steps=steps, rows=rows, total=fn["total"],
                overall=stats.rate(fn["total"]["won"], fn["total"]["call"]), now=db.now(), first=first, last=last, KIND_KO=core.KIND_KO)
