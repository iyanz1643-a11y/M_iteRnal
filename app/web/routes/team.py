"""팀 관리 묶음: 팀원 현황, 신인 정착.

신인 정착 /team/onboarding 은 새 계산을 만들지 않고 엔진 함수 둘을 조합한다.

| 체크 | 원본 | 판단 |
| --- | --- | --- |
| 필수 교육 | education.person_status | 그 사람에게 걸린 필수 과정을 모두 수료했는가. 걸린 필수 과정이 없으면 대상 없음으로 보고 마친 것으로 센다 |
| 첫 계약 | performance.tally(by='month') | 위촉한 달부터 자료 기준 달까지 성립 건이 있는 첫 달. 청약만 있으면 예정으로 표시한다 |
| 첫 가동 | performance.tally(by='month') | 같은 기간에 순건수가 1건 이상인 첫 달. 업적 집계 규칙대로 성립한 달에 더하고 철회한 달에 뺀다 |

- 대상은 범위 안 재직 설계사 가운데 오늘 기준 위촉한 지 ONBOARD_DAYS 일 안인 사람이다. 보는 사람 본인은 뺀다.
- 범위는 guard 가 준 ctx 를 그대로 쓴다. 메인 관리자는 전체, 관리자는 ctx['agent_ids'] 다.
- 금액은 읽지도 보여 주지도 않는다. 건수와 여부, 달만 보인다(D-17).
- 교육은 교육 화면과 같이 오늘 기준, 계약은 자료 기준 달까지다. 화면에 두 기준을 함께 적는다.
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.engine import education as edu
from app.engine import performance
from app.org.sync import org_texts
from app.web import org_values
from app.web.deps import _in, _last_load, _perf, _q, edu_today, get_con, guard, page

router = APIRouter()
ONBOARD_DAYS = 90


@router.get("/team/members", response_class=HTMLResponse)
def team_members(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    ids = ctx["agent_ids"] if ctx["scope"] == "org" else [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent")]
    return page(request, ctx, "team_members.html", rows=_perf(con, ctx["ym"], ids), last_load=_last_load(con))


# ---------------------------------------------------------------- 신인 정착
def _today() -> date:
    """신인 정착은 교육 기한을 함께 보므로 교육 화면과 같은 기준일 deps.edu_today 를 쓴다."""
    return edu_today()


def _scope(con, ctx) -> list:
    if ctx["scope"] == "all":
        ids = [r[0] for r in con.execute("SELECT agent_id FROM agent")]
    else:
        ids = sorted(ctx["agent_ids"])
    return [a for a in ids if a != ctx.get("agent_id")]


def _ym(d: str) -> str:
    return d[:4] + d[5:7]


def onboarding_rows(con, ctx, today: date) -> list:
    """범위 안 신인의 체크리스트 줄. 금액 칸은 없다."""
    scope = _scope(con, ctx)
    if not scope:
        return []
    q, ids = _in(scope)
    since = (today - timedelta(days=ONBOARD_DAYS)).isoformat()
    people = _q(con, f"""SELECT agent_id, internal_code code, name, hire_date FROM agent
                         WHERE agent_id IN ({q}) AND status='active' AND hire_date IS NOT NULL
                           AND substr(hire_date,1,10)>=? AND substr(hire_date,1,10)<=?
                         ORDER BY hire_date, internal_code""", (*ids, since, today.isoformat()))
    if not people:
        return []
    new_ids = [p["agent_id"] for p in people]
    edu.ensure(con)
    status = {r["agent_id"]: r for r in edu.person_status(con, new_ids, today)}
    end = ctx["ym"]
    start = min(_ym(p["hire_date"]) for p in people)
    months = performance.tally(con, start, end, agent_ids=new_ids, by="month") if start <= end else {}
    texts = org_texts(con)
    out = []
    for p in people:
        aid, hire_ym = p["agent_id"], _ym(p["hire_date"])
        st = status.get(aid) or {"required_total": 0, "required_done": 0, "overdue": 0, "soon": 0, "courses": [], "org_id": None}
        m = {k: r for k, r in (months.get(aid) or {}).items() if k and hire_ym <= k <= end}
        made = sorted(k for k, r in m.items() if r["new_count"] > 0)
        active = sorted(k for k, r in m.items() if r["net_count"] >= 1)
        todo = [x for x in st["courses"] if x["required"] and x["state"] != "done"]
        edu_ok = st["required_done"] == st["required_total"]
        row = {"agent_id": aid, "code": p["code"], "name": p["name"], "hire_date": p["hire_date"][:10],
               "days": (today - date.fromisoformat(p["hire_date"][:10])).days, "org": texts.get(st["org_id"], ""),
               "edu_total": st["required_total"], "edu_done": st["required_done"], "edu_ok": edu_ok, "edu_todo": todo,
               "edu_overdue": st["overdue"], "edu_soon": st["soon"], "edu_soon_days": st.get("soon_days"),
               "first_contract": made[0] if made else None, "new_count": sum(r["new_count"] for r in m.values()),
               "applied": sum(r["applied_count"] for r in m.values()),
               "first_active": active[0] if active else None, "no_data": hire_ym > end}
        row["steps"] = sum((edu_ok, row["first_contract"] is not None, row["first_active"] is not None))
        out.append(row)
    out.sort(key=lambda r: (r["steps"], r["hire_date"], r["code"]))      # 덜 마친 사람, 위촉이 오래된 사람이 위로
    return out


@router.get("/team/onboarding", response_class=HTMLResponse)
def team_onboarding(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    today = _today()
    rows = onboarding_rows(con, ctx, today)
    return page(request, ctx, "team_onboarding.html", rows=rows, today=today.isoformat(), days=ONBOARD_DAYS,
                last_load=_last_load(con), soon_days=org_values.days_text([r["edu_soon_days"] for r in rows],
                                                                          org_values.for_ctx(con, ctx, org_values.EDU_DAYS_KEY)))
