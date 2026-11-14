"""내 진척 묶음 가운데 실적 요약과 시상 진척. 관리자와 설계사의 본인 화면이다. 메뉴 범위는 self 다.

본인 한 사람(ctx['agent_id'])으로만 읽는다. 계산은 엔진이 한다.
- 실적 요약: results.period_summary, results.breakdown. 기간별 업적과 같은 숫자다.
- 시상 진척: 조직 부문 순위(org_award.standing), 구간시상(incentive_result 의 본인 줄), 프로모션 달성(promotion.progress).
  금액은 본인 것만 싣고 모두 예상이라고 적는다. 확정은 보험사 정산과 월 마감을 따른다.
"""
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.engine import org_award as oa
from app.engine import promotion as pm
from app.engine import results as rs
from app.web.deps import _cards, _q, get_con, guard, page
from app.web.routes.perf import SECTORS, filters, fresh, label_months, pick, pick_ym

router = APIRouter()


@router.get("/my/summary", response_class=HTMLResponse)
def my_summary(request: Request, pt: str = "month", ym: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    pt = pick(pt, rs.PERIOD_KO, "month")
    ym, yms = pick_ym(con, ctx, ym)
    me = [ctx["agent_id"]] if ctx.get("agent_id") else []
    s = rs.period_summary(con, me, pt, ym)
    label_months(s["months"])
    cats = rs.breakdown(con, me, pt, ym, by="category")
    secs = rs.breakdown(con, me, pt, ym, by="sector")
    cats["shown"] = [c for c in cats["rows"] if c["new_count"] or c["withdrawn_count"] or c["applied_count"] or c["converted_premium"]]
    return page(request, ctx, "my_summary.html", s=s, cats=cats, secs=secs, has_agent=bool(me), yms=yms, ym=ym, pt=pt,
                PERIODS=rs.PERIOD_KO, SECTORS=SECTORS, fresh=fresh(con, ym), f=filters(pt, "", ym))


def _divisions(con, ctx, ym) -> list:
    """볼 수 있는 조직 부문마다 본인의 이번 달 순위. 순위에 없으면 me 가 None 이다."""
    out = []
    for d in oa.visible(con, ctx, ym):
        rows = oa.standing(con, d, ym)
        me = next((r for r in rows if r["agent_id"] == ctx.get("agent_id")), None)
        winners = [r for r in rows if r["winner"]]
        gap = None
        if me and not me["winner"] and winners:
            gap = winners[-1]["value"] - me["value"]
        elif not me and len(winners) >= d["top_n"]:
            gap = winners[-1]["value"]                     # 아직 실적이 없으면 수상권 끝 사람의 값이 남은 양이다
        out.append({**d, "me": me, "total": len(rows), "gap": gap,
                    "confirmed": bool(con.execute("SELECT 1 FROM org_award_result WHERE division_id=? AND period_ym=? LIMIT 1",
                                                  (d["division_id"], ym)).fetchone())})
    return out


COND_KO = {"persistency_13": "13회차 유지율", "persistency_25": "25회차 유지율", "min_count": "최소 건수"}
COND = re.compile(r"(\w+) ([\d.]+) >= ([\d.]+) 불충족")


def reason_ko(text):
    """구간시상 엔진의 미달 사유를 말로 옮긴다. 모르는 꼴은 그대로 둔다."""
    if not text:
        return None
    parts = []
    for part in text.split(";"):
        m = COND.search(part.strip())
        if m and m.group(1) in COND_KO:
            parts.append(f"{COND_KO[m.group(1)]} {float(m.group(2)):g}, 기준 {float(m.group(3)):g} 이상에 못 미칩니다")
        else:
            parts.append(part.strip())
    return " · ".join(parts)


def _tiers(con, ctx, ym) -> list:
    """구간시상 엔진이 그 달에 계산한 본인 줄. 완료된 시상 run 만 읽는다."""
    if not ctx.get("agent_id"):
        return []
    rows = _q(con, """SELECT r.name, r.rule_kind, r.metric, r.period_type, i.metric_value, i.qualified, i.disqualify_reason, i.tier_no, i.reward_amount
                      FROM incentive_result i JOIN calc_run c ON c.run_id=i.run_id AND c.status='done' AND c.run_type='incentive'
                      JOIN incentive_rule r ON r.rule_id=i.rule_id
                      WHERE i.agent_id=? AND i.period_ym=? AND c.run_id=(SELECT MAX(run_id) FROM calc_run WHERE run_type='incentive' AND status='done' AND period_ym=?)
                      ORDER BY r.rule_kind, r.rule_id""", (ctx["agent_id"], ym, ym))
    for r in rows:
        r["reason"] = reason_ko(r["disqualify_reason"])
        r["unit"] = "건" if r["metric"] == "count" else "원"
    return rows


def _promos(con, ctx) -> tuple:
    cards, ref = _cards(con, ctx)
    cards = [c for c in cards if c["recipient_level"] == "fc" and c["phase"] != "upcoming"]
    if not ctx.get("agent_id"):
        return [], ref
    return [{**c, "r": pm.progress(con, c, [ctx["agent_id"]], ref)[ctx["agent_id"]]} for c in cards], ref


@router.get("/my/awards", response_class=HTMLResponse)
def my_awards(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    ym = ctx["ym"]
    promos, ref = _promos(con, ctx)
    divisions = _divisions(con, ctx, ym)
    return page(request, ctx, "my_awards.html", divisions=divisions, tiers=_tiers(con, ctx, ym), promos=promos, ref=ref.isoformat(),
                has_agent=bool(ctx.get("agent_id")), fresh=fresh(con, ym),
                hits=sum(1 for d in divisions if d["me"] and d["me"]["winner"]) + sum(1 for p in promos if p["r"]["tier"] or (p["kind"] == "rate" and p["r"]["value"] > 0)))
