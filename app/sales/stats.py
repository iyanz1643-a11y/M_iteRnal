"""체결률과 활동 깔때기. 화면은 숫자를 받아 그리기만 한다.

| 숫자 | 뜻 |
| --- | --- |
| 배정 | 지금 그 사람에게 있는 DB 가운데 미배정이 아닌 것. 회수한 DB 는 회수 뒤 받은 사람에게 센다 |
| 체결 | 그 가운데 고객으로 바꾼 것(won) |
| 체결률 | 체결 / 배정. 배정이 0 이면 비운다 |
| 활동 | 기간 안 done_on 의 활동 건수. 통화, 상담, 제안, 청약 |
| 전환율 | 다음 단계 건수 / 앞 단계 건수. 같은 DB 의 단계 이동이 아니라 기간 안 건수의 비다 |

조직 단위는 자료 기준 달에 그 사람이 속한 조직과 그 위를 따른다. 업적 집계와 같은 소속 이력이다.
청약은 설계사가 적은 활동이라 업적 집계의 청약 건수와 다를 수 있다. 화면에 그 안내를 둔다. 금액은 없다.
"""
from __future__ import annotations

from app.engine import orgs
from app.sales import core


def _in(ids):
    ids = list(ids) or [-1]
    return ",".join("?" * len(ids)), ids


def rate(won, total):
    return round(won / total * 100, 1) if total else None


def by_agent(con, agent_ids) -> dict:
    """{agent_id: {total, won, open, lost}}."""
    q, ids = _in(agent_ids)
    out = {}
    for r in con.execute(f"""SELECT agent_id, COUNT(*) total, SUM(status='won') won, SUM(status IN ('assigned','working')) open, SUM(status='lost') lost
                             FROM sales_lead WHERE agent_id IN ({q}) GROUP BY agent_id""", ids):
        out[r["agent_id"]] = {"total": r["total"], "won": r["won"] or 0, "open": r["open"] or 0, "lost": r["lost"] or 0}
    return out


def conversion(con, agent_ids, ym, level: str, org_ids=None) -> list:
    """조직 단위(level: team, branch, hq) 체결률. org_ids 를 주면 그 조직만 낸다(관리자의 맡은 범위)."""
    per = by_agent(con, agent_ids)
    if not per:
        return []
    lv = {r["org_id"]: (r["level"], r["name"]) for r in con.execute("SELECT org_id, level, name FROM org_unit")}
    acc: dict = {}
    for aid, v in per.items():
        home = core.home_org(con, aid, ym) or core.last_org(con, aid)
        if home is None:
            continue
        hit = [o for o in orgs.ancestors(con, home) if lv.get(o, ("",))[0] == level]
        for o in hit:
            if org_ids is not None and o not in org_ids:
                continue
            a = acc.setdefault(o, {"org_id": o, "name": lv[o][1], "total": 0, "won": 0, "open": 0, "people": 0})
            a["total"] += v["total"]
            a["won"] += v["won"]
            a["open"] += v["open"]
            a["people"] += 1
    rows = list(acc.values())
    for r in rows:
        r["rate"] = rate(r["won"], r["total"])
    rows.sort(key=lambda r: (-(r["rate"] or -1), r["name"]))
    return rows


def funnel(con, agent_ids, d_from: str, d_to: str) -> dict:
    """기간 안 활동 건수. {'total': {kind: n, won: n}, 'agents': {agent_id: {...}}, 'rates': [(앞, 뒤, 율)]}."""
    q, ids = _in(agent_ids)
    agents: dict = {}
    for r in con.execute(f"""SELECT agent_id, kind, COUNT(*) n FROM sales_activity WHERE agent_id IN ({q}) AND done_on>=? AND done_on<=?
                             GROUP BY agent_id, kind""", (*ids, d_from, d_to)):
        agents.setdefault(r["agent_id"], dict.fromkeys([*core.KINDS, "won"], 0))[r["kind"]] = r["n"]
    for r in con.execute(f"""SELECT agent_id, COUNT(*) n FROM sales_customer WHERE agent_id IN ({q}) AND lead_id IS NOT NULL
                             AND handed_from_agent_id IS NULL AND substr(created_at,1,10)>=? AND substr(created_at,1,10)<=? GROUP BY agent_id""",
                         (*ids, d_from, d_to)):
        agents.setdefault(r["agent_id"], dict.fromkeys([*core.KINDS, "won"], 0))["won"] = r["n"]
    total = dict.fromkeys([*core.KINDS, "won"], 0)
    for v in agents.values():
        for k in total:
            total[k] += v[k]
    steps = [*core.KINDS, "won"]
    rates = [(a, b, rate(total[b], total[a])) for a, b in zip(steps, steps[1:])]
    return {"total": total, "agents": agents, "rates": rates}
