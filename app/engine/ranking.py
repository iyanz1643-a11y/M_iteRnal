"""랭킹: 전체 설계사 순위. 당월은 집계 중이므로 공유는 전월 확정분으로 한다."""
import json

from app import db
from app.engine import classify
from app.engine.performance import ACTIVE_STATUSES, org_at
from app.periods import month_bounds, period_key, period_start


def _values(con, start_ym, end_ym, eligible_only):
    d_from, _ = month_bounds(start_ym)
    _, d_to = month_bounds(end_ym)
    elig = "AND pc.incentive_eligible=1" if eligible_only else ""
    made = f"c.status IN {ACTIVE_STATUSES} AND c.contract_date>=:f AND c.contract_date<:t"   # 업적 집계와 같은 규칙
    sql = f"""
        SELECT a.agent_id,
               COALESCE(SUM(CASE WHEN {made} THEN c.converted_premium END),0)
             - COALESCE(SUM(CASE WHEN c.status='withdrawn' AND c.withdrawal_date>=:f AND c.withdrawal_date<:t THEN c.converted_premium END),0) AS rp,
               COALESCE(SUM(CASE WHEN {made} THEN c.policy_count END),0)
             - COALESCE(SUM(CASE WHEN c.status='withdrawn' AND c.withdrawal_date>=:f AND c.withdrawal_date<:t THEN c.policy_count END),0) AS cnt
        FROM agent a
        LEFT JOIN contract c ON c.agent_id=a.agent_id
        LEFT JOIN product p ON p.product_id=c.product_id
        LEFT JOIN product_category pc ON pc.code={classify.category('c', 'p')}
        WHERE a.job_type='fc' AND a.status='active' AND (c.contract_id IS NULL OR 1=1 {elig})
        GROUP BY a.agent_id"""
    return con.execute(sql, dict(f=d_from, t=d_to)).fetchall()


def _rank(rows, key):
    ordered = sorted(rows, key=lambda r: -r[key])
    out, prev, rank = [], None, 0
    for i, r in enumerate(ordered, start=1):
        if r[key] != prev:
            rank, prev = i, r[key]
        out.append((rank, r["agent_id"], r[key]))
    return out


def run(con, period_ym, run_by="system"):
    run_id = db.new_run(con, "ranking", period_ym, run_by, json.dumps({"as_of": period_ym}))
    n = 0
    for ptype in ("month", "year"):
        start, key = period_start(ptype, period_ym), period_key(ptype, period_ym)
        for scope, eligible in (("all", False), ("eligible", True)):
            rows = _values(con, start, period_ym, eligible)
            for metric, col in (("recognized_premium", "rp"), ("net_count", "cnt")):
                for rank, agent_id, value in _rank(rows, col):
                    con.execute(
                        "INSERT INTO ranking_snapshot(run_id, period_type, period_key, metric, scope, rank_no, agent_id, org_id, value) VALUES(?,?,?,?,?,?,?,?,?)",
                        (run_id, ptype, key, metric, scope, rank, agent_id, org_at(con, agent_id, period_ym), value))
                    n += 1
    db.finish_run(con, run_id, f"{n} rows")
    return run_id, n
