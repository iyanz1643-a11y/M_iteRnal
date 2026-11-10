"""업적 집계: 월/분기/반기/연 설계사별 실적."""
import json
from collections import defaultdict

from app import db
from app.periods import month_bounds, period_key, period_start

ACTIVE_STATUSES = ("inforce", "lapsed", "cancelled", "matured", "withdrawn")  # 한 번 성립한 계약


def org_at(con, agent_id, ym):
    row = con.execute(
        """SELECT org_id FROM agent_org_history WHERE agent_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)
           ORDER BY effective_from DESC LIMIT 1""",
        (agent_id, ym, ym),
    ).fetchone()
    return row["org_id"] if row else None


def compute_range(con, start_ym, end_ym):
    """start_ym~end_ym 사이 설계사별 집계 (딕셔너리)."""
    d_from, _ = month_bounds(start_ym)
    _, d_to = month_bounds(end_ym)
    agg = defaultdict(lambda: dict(applied_count=0, new_count=0, withdrawn_count=0, monthly_premium=0, converted_premium=0,
                                   commission_amount=0, clawback_amount=0))
    # 청약 (예정 업적): 아직 applied 상태인 건
    for r in con.execute(
        "SELECT agent_id, SUM(policy_count) n FROM contract WHERE status='applied' AND application_date>=? AND application_date<? GROUP BY agent_id",
        (d_from, d_to),
    ):
        agg[r["agent_id"]]["applied_count"] += r["n"]
    # 성립 건 (이후 철회된 건도 성립 월에는 포함)
    for r in con.execute(
        f"""SELECT agent_id, SUM(policy_count) n, SUM(premium_monthly) mp, SUM(converted_premium) cp FROM contract
            WHERE status IN {ACTIVE_STATUSES} AND contract_date>=? AND contract_date<? GROUP BY agent_id""",
        (d_from, d_to),
    ):
        a = agg[r["agent_id"]]
        a["new_count"] += r["n"]
        a["monthly_premium"] += r["mp"] or 0
        a["converted_premium"] += r["cp"] or 0
    # 철회 건: 철회 월에 차감
    for r in con.execute(
        """SELECT agent_id, SUM(policy_count) n, SUM(premium_monthly) mp, SUM(converted_premium) cp FROM contract
           WHERE status='withdrawn' AND withdrawal_date>=? AND withdrawal_date<? GROUP BY agent_id""",
        (d_from, d_to),
    ):
        a = agg[r["agent_id"]]
        a["withdrawn_count"] += r["n"]
        a["monthly_premium"] -= r["mp"] or 0
        a["converted_premium"] -= r["cp"] or 0
    # 수수료 (완료 run 기준)
    for r in con.execute(
        """SELECT c.agent_id,
                  SUM(CASE WHEN c.commission_type<>'clawback' THEN c.amount ELSE 0 END) paid,
                  SUM(CASE WHEN c.commission_type='clawback' THEN c.amount ELSE 0 END) cb
           FROM commission_calc c JOIN calc_run r ON r.run_id=c.run_id AND r.status='done'
           WHERE c.period_ym>=? AND c.period_ym<=? GROUP BY c.agent_id""",
        (start_ym, end_ym),
    ):
        agg[r["agent_id"]]["commission_amount"] += r["paid"] or 0
        agg[r["agent_id"]]["clawback_amount"] += r["cb"] or 0
    for a in agg.values():
        a["net_count"] = a["new_count"] - a["withdrawn_count"]
    return agg


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
