"""업적 집계: 월/분기/반기/연 설계사별 실적.

규칙: 청약은 예정 업적, 성립한 달에 더하고 철회한 달에 뺀다. 다른 엔진(랭킹, 조직 부문 순위)도 이 규칙을 따른다.
설계사가 비어 있는 계약(모집사원번호가 마스터에 없는 실제 계약)은 사람에게 붙일 수 없어 설계사별 집계에서 빠진다.
그 몫은 unassigned() 로 따로 본다. 설계사별 합과 unassigned() 를 더하면 원장 전체와 맞는다.
조직 집계는 org_rollup() 이다. 조직과 그 아래 전부(센터, 팀 포함)의 설계사를 더한다.
"""
import json
from collections import defaultdict

from app import db
from app.engine import orgs
from app.periods import month_bounds, period_key, period_start

ACTIVE_STATUSES = ("inforce", "lapsed", "cancelled", "matured", "withdrawn")  # 한 번 성립한 계약


def org_at(con, agent_id, ym):
    row = con.execute(
        """SELECT org_id FROM agent_org_history WHERE agent_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)
           ORDER BY effective_from DESC LIMIT 1""",
        (agent_id, ym, ym),
    ).fetchone()
    return row["org_id"] if row else None


def compute_range(con, start_ym, end_ym, unassigned_only=False):
    """start_ym~end_ym 사이 설계사별 집계 (딕셔너리). unassigned_only 면 설계사가 빈 계약만 None 키 하나로 모은다."""
    who = "agent_id IS NULL" if unassigned_only else "agent_id IS NOT NULL"
    d_from, _ = month_bounds(start_ym)
    _, d_to = month_bounds(end_ym)
    agg = defaultdict(lambda: dict(applied_count=0, new_count=0, withdrawn_count=0, monthly_premium=0, converted_premium=0,
                                   commission_amount=0, clawback_amount=0))
    # 청약 (예정 업적): 아직 applied 상태인 건
    for r in con.execute(
        f"SELECT agent_id, SUM(policy_count) n FROM contract WHERE {who} AND status='applied' AND application_date>=? AND application_date<? GROUP BY agent_id",
        (d_from, d_to),
    ):
        agg[r["agent_id"]]["applied_count"] += r["n"]
    # 성립 건 (이후 철회된 건도 성립 월에는 포함)
    for r in con.execute(
        f"""SELECT agent_id, SUM(policy_count) n, SUM(premium_monthly) mp, SUM(converted_premium) cp FROM contract
            WHERE {who} AND status IN {ACTIVE_STATUSES} AND contract_date>=? AND contract_date<? GROUP BY agent_id""",
        (d_from, d_to),
    ):
        a = agg[r["agent_id"]]
        a["new_count"] += r["n"]
        a["monthly_premium"] += r["mp"] or 0
        a["converted_premium"] += r["cp"] or 0
    # 철회 건: 철회 월에 차감
    for r in con.execute(
        f"""SELECT agent_id, SUM(policy_count) n, SUM(premium_monthly) mp, SUM(converted_premium) cp FROM contract
           WHERE {who} AND status='withdrawn' AND withdrawal_date>=? AND withdrawal_date<? GROUP BY agent_id""",
        (d_from, d_to),
    ):
        a = agg[r["agent_id"]]
        a["withdrawn_count"] += r["n"]
        a["monthly_premium"] -= r["mp"] or 0
        a["converted_premium"] -= r["cp"] or 0
    # 수수료 (완료 run 기준)
    for r in [] if unassigned_only else con.execute(
        """SELECT c.agent_id,
                  SUM(CASE WHEN c.commission_type<>'clawback' THEN c.amount ELSE 0 END) paid,
                  SUM(CASE WHEN c.commission_type='clawback' THEN c.amount ELSE 0 END) cb
           FROM commission_calc c JOIN calc_run r ON r.run_id=c.run_id AND r.status='done'
           WHERE c.agent_id IS NOT NULL AND c.period_ym>=? AND c.period_ym<=? GROUP BY c.agent_id""",
        (start_ym, end_ym),
    ):
        agg[r["agent_id"]]["commission_amount"] += r["paid"] or 0
        agg[r["agent_id"]]["clawback_amount"] += r["cb"] or 0
    for a in agg.values():
        a["net_count"] = a["new_count"] - a["withdrawn_count"]
    return agg


def unassigned(con, start_ym, end_ym) -> dict:
    """설계사가 빈 계약의 집계. 설계사별 집계와 같은 규칙이다. 원장 대조에 쓴다."""
    agg = compute_range(con, start_ym, end_ym, unassigned_only=True)
    return agg.get(None) or dict(applied_count=0, new_count=0, withdrawn_count=0, net_count=0, monthly_premium=0, converted_premium=0)


SUM_COLS = ("applied_count", "new_count", "withdrawn_count", "net_count", "monthly_premium", "converted_premium", "commission_amount", "clawback_amount")


def org_rollup(con, org_id, period_ym, period_type="month") -> dict:
    """조직과 그 아래 전부의 집계. period_ym 의 완료된 업적 run 을 읽는다.
    소속은 그 run 이 남긴 기간 기준일 소속(agent_performance_summary.org_id)이다. 뷰 v_org_performance_rollup 과 같은 값이다."""
    ids = sorted(orgs.descendants(con, org_id))
    q = ",".join("?" * len(ids))
    r = con.execute(
        f"""SELECT COUNT(*) agents, {', '.join(f'COALESCE(SUM(s.{c}),0) {c}' for c in SUM_COLS)}
            FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.run_type='performance' AND r.period_ym=?
            WHERE s.period_type=? AND s.period_key=? AND s.org_id IN ({q})""",
        [period_ym, period_type, period_key(period_type, period_ym)] + ids).fetchone()
    return dict(r)


def run(con, period_ym, run_by="system"):
    """period_ym 기준 월·분기(누적)·반기(누적)·연(누적) 집계를 한 run으로 저장."""
    run_id = db.new_run(con, "performance", period_ym, run_by, json.dumps({"as_of": period_ym}))
    n = 0
    for ptype in ("month", "quarter", "half", "year"):
        start = period_start(ptype, period_ym)
        key = period_key(ptype, period_ym)
        agg = compute_range(con, start, period_ym)
        for fc in con.execute("SELECT agent_id FROM agent WHERE job_type='fc'").fetchall():
            agg[fc["agent_id"]]  # 실적이 없는 설계사도 0 으로 한 줄 남긴다
        for agent_id, a in agg.items():
            a.setdefault("net_count", a["new_count"] - a["withdrawn_count"])
            con.execute(
                """INSERT INTO agent_performance_summary(run_id, agent_id, org_id, period_type, period_key, applied_count, new_count,
                   withdrawn_count, net_count, monthly_premium, converted_premium, commission_amount, clawback_amount)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, agent_id, org_at(con, agent_id, period_ym), ptype, key, a["applied_count"], a["new_count"],
                 a["withdrawn_count"], a["net_count"], a["monthly_premium"], a["converted_premium"],
                 a["commission_amount"], a["clawback_amount"]),
            )
            n += 1
    db.finish_run(con, run_id, f"{n} rows")
    return run_id, n
