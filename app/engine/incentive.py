"""구간시상 엔진: 자격 판정, 구간 결정, 시상액, 시상 환수."""
import json

from app import db
from app.engine import persist
from app.periods import month_bounds, months_between, period_end, period_key, period_start, ym_diff

ACTIVE = ("inforce", "lapsed", "cancelled", "matured")  # 시상 측정 시점 기준 유효 계약 (철회 제외)


def _metric_contracts(con, rule, start_ym, end_ym):
    d_from, _ = month_bounds(start_ym)
    _, d_to = month_bounds(end_ym)
    sql = f"""SELECT c.contract_id, c.agent_id, c.converted_premium, c.premium_monthly, c.policy_count, pr.product_category
              FROM contract c LEFT JOIN product pr ON pr.product_id=c.product_id
              JOIN insurer i ON i.insurer_id=c.insurer_id
              WHERE c.status IN {ACTIVE} AND c.contract_date>=? AND c.contract_date<?"""
    params = [d_from, d_to]
    if rule["scope"] == "insurer":
        sql += " AND i.code=?"
        params.append(rule["scope_value"])
    elif rule["scope"] == "product_category":
        sql += " AND pr.product_category=?"
        params.append(rule["scope_value"])
    excluded = [c["value"] for c in con.execute(
        "SELECT value FROM incentive_condition WHERE rule_id=? AND condition_type='exclude_product_category'", (rule["rule_id"],))]
    # 규칙별 대상 구분 (예: 종신·건강만). 지정이 없으면 전 구분 대상
    included = [c["product_category"] for c in con.execute("SELECT product_category FROM incentive_rule_category WHERE rule_id=?", (rule["rule_id"],))]
    rows = con.execute(sql, params).fetchall()
    return [r for r in rows if r["product_category"] not in excluded and (not included or r["product_category"] in included)]


def _metric_value(rule, rows):
    if rule["metric"] == "count":
        return sum(r["policy_count"] or 0 for r in rows)
    if rule["metric"] == "converted_premium":
        return sum(r["converted_premium"] or 0 for r in rows)
    if rule["metric"] == "monthly_premium":
        return sum(r["premium_monthly"] or 0 for r in rows)
    if rule["metric"] == "annual_premium":
        return sum((r["premium_monthly"] or 0) * 12 for r in rows)
    raise ValueError(rule["metric"])


def _cmp(op, a, b):
    return {"<=": a <= b, ">=": a >= b, "=": a == b, "<": a < b, ">": a > b}[op]


def _check_conditions(con, rule, agent, rows, period_ym):
    reasons = []
    for cond in con.execute("SELECT * FROM incentive_condition WHERE rule_id=?", (rule["rule_id"],)):
        ctype = cond["condition_type"]
        if ctype == "exclude_product_category":
            continue
        if ctype in ("persistency_13", "persistency_25"):
            kind = "p13" if ctype == "persistency_13" else "p25"
            actual = persist.rate(con, [agent["agent_id"]], kind, period_ym)[0]
        elif ctype == "min_count":
            actual = sum(r["policy_count"] or 0 for r in rows)
        elif ctype == "tenure_months":
            actual = months_between(agent["hire_date"], month_bounds(period_ym)[1]) if agent["hire_date"] else 0
        else:
            continue
        if not _cmp(cond["operator"], actual, float(cond["value"])):
            reasons.append(f"{ctype} {actual:.2f} {cond['operator']} {cond['value']} 불충족")
    return reasons


def _tier(con, rule, value):
    return con.execute(
        "SELECT * FROM incentive_tier WHERE rule_id=? AND min_value<=? AND (max_value IS NULL OR max_value>?) ORDER BY tier_no DESC LIMIT 1",
        (rule["rule_id"], value, value)).fetchone()


def _rules_for(con, period_ym):
    """해당 월에 유효한 active 규칙. 분기/반기/연 규칙은 기간 종료 월에만 확정 계산한다."""
    out = []
    for r in con.execute(
        "SELECT * FROM incentive_rule WHERE status='active' AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)",
        (period_ym, period_ym)):
        if r["period_type"] != "month" and period_end(r["period_type"], period_ym) != period_ym:
            continue
        out.append(r)
    return out


def run(con, period_ym, run_by="system"):
    rules = _rules_for(con, period_ym)
    run_id = db.new_run(con, "incentive", period_ym, run_by, json.dumps({"incentive_rule": [r["rule_id"] for r in rules]}))
    agents = con.execute("SELECT * FROM agent WHERE job_type='fc' AND (status='active' OR termination_date>=?)", (month_bounds(period_ym)[0],)).fetchall()
    n = 0
    from app.engine.performance import org_at
    for rule in rules:
        start = period_start(rule["period_type"], period_ym)
        rows_all = _metric_contracts(con, rule, start, period_ym)
        by_agent = {}
        for r in rows_all:
            by_agent.setdefault(r["agent_id"], []).append(r)
        for agent in agents:
            rows = by_agent.get(agent["agent_id"], [])
            value = _metric_value(rule, rows)
            reasons = _check_conditions(con, rule, agent, rows, period_ym)
            tier = _tier(con, rule, value) if not reasons else None
            if not reasons and tier is None:
                reasons = ["최소 구간 미달"]
            reward = 0
            if tier:
                reward = round(value * tier["reward_value"]) if tier["reward_type"] == "rate" else tier["reward_value"]
            detail = dict(period_key=period_key(rule["period_type"], period_ym),
                          contracts=[(r["contract_id"], r["converted_premium"]) for r in rows])
            con.execute(
                """INSERT INTO incentive_result(run_id, period_ym, agent_id, org_id, rule_id, metric_value, qualified,
                   disqualify_reason, tier_no, reward_amount, detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, period_ym, agent["agent_id"], org_at(con, agent["agent_id"], period_ym), rule["rule_id"], value,
                 1 if tier else 0, "; ".join(reasons) or None, tier["tier_no"] if tier else None, reward, json.dumps(detail)),
            )
            n += 1
    cb = _clawbacks(con, period_ym)
    db.finish_run(con, run_id, f"{n} results, {cb} clawbacks")
    return run_id, n, cb


def _clawbacks(con, period_ym):
    """감시 기간 내 해지/실효/철회된 계약에 대한 시상 환수 (이번 달 귀속)."""
    d_from, d_to = month_bounds(period_ym)
    ended = {r["contract_id"]: r for r in con.execute(
        "SELECT contract_id, status, converted_premium FROM contract WHERE status IN ('lapsed','cancelled','withdrawn') AND status_date>=? AND status_date<?",
        (d_from, d_to))}
    if not ended:
        return 0
    n = 0
    results = con.execute(
        """SELECT ir.*, r.clawback_months FROM incentive_result ir
           JOIN incentive_rule r ON r.rule_id=ir.rule_id
           JOIN calc_run cr ON cr.run_id=ir.run_id AND cr.status='done'
           WHERE ir.qualified=1 AND ir.reward_amount>0 AND r.clawback_months>0 AND ir.period_ym<?""", (period_ym,)).fetchall()
    for res in results:
        if ym_diff(period_ym, res["period_ym"]) > res["clawback_months"]:
            continue
        detail = json.loads(res["detail_json"] or "{}")
        for cid, cp in detail.get("contracts", []):
            if cid in ended and res["metric_value"]:
                exists = con.execute("SELECT 1 FROM incentive_clawback WHERE result_id=? AND contract_id=?", (res["result_id"], cid)).fetchone()
                if exists:
                    continue
                amt = round(res["reward_amount"] * (cp or 0) / res["metric_value"])
                if amt <= 0:
                    continue
                con.execute(
                    "INSERT INTO incentive_clawback(result_id, contract_id, period_ym, amount, reason) VALUES(?,?,?,?,?)",
                    (res["result_id"], cid, period_ym, -amt, f"{ended[cid]['status']} ({res['period_ym']} 시상분 비례 환수)"))
                n += 1
    return n
