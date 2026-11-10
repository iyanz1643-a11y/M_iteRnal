"""커리어패스: 승격 후보 추출, 목표 진척."""
import pandas as pd

from app import db
from app.periods import month_bounds, months_between, period_key, ym_add, ym_to_tuple


def current_level(con, agent_id, ym):
    return con.execute(
        """SELECT l.* FROM agent_career_history h JOIN career_level l ON l.level_id=h.level_id
           WHERE h.agent_id=? AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
           ORDER BY h.effective_from DESC LIMIT 1""", (agent_id, ym, ym)).fetchone()


def last_full_quarter_key(ym):
    y, m = ym_to_tuple(ym)
    if m % 3 == 0:
        return period_key("quarter", ym)
    return period_key("quarter", ym_add(ym, -(m % 3)))


def _metric(con, agent, metric, ptype, ym):
    if metric == "tenure_months":
        return months_between(agent["hire_date"], month_bounds(ym)[1]) if agent["hire_date"] else 0
    if metric == "persistency_13":
        row = con.execute("SELECT rate FROM persistency WHERE agent_id=? AND kind='p13' AND period_ym<=? ORDER BY period_ym DESC LIMIT 1",
                          (agent["agent_id"], ym)).fetchone()
        return row["rate"] if row else 0
    if metric == "team_size":
        return 0
    key = last_full_quarter_key(ym) if ptype == "quarter" else period_key(ptype, ym)
    col = {"converted_premium": "converted_premium", "new_count": "net_count", "net_count": "net_count",
           "monthly_premium": "monthly_premium", "commission_amount": "commission_amount"}.get(metric, metric)
    row = con.execute(
        f"""SELECT s.{col} v FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done'
            WHERE s.agent_id=? AND s.period_type=? AND s.period_key=? ORDER BY s.run_id DESC LIMIT 1""",
        (agent["agent_id"], ptype, key)).fetchone()
    return row["v"] if row else 0


def _cmp(op, a, b):
    return {"<=": a <= b, ">=": a >= b, "=": a == b}[op]


def promotion_candidates(con, ym) -> pd.DataFrame:
    out = []
    for agent in con.execute("SELECT * FROM agent WHERE status='active'"):
        lvl = current_level(con, agent["agent_id"], ym)
        if not lvl:
            continue
        crits = con.execute(
            """SELECT c.*, t.code to_code, t.name to_name FROM career_promotion_criteria c JOIN career_level t ON t.level_id=c.to_level_id
               WHERE c.from_level_id=? AND c.effective_from<=? AND (c.effective_to IS NULL OR c.effective_to>=?)""",
            (lvl["level_id"], ym, ym)).fetchall()
        if not crits:
            continue
        gaps, met = [], 0
        for c in crits:
            actual = _metric(con, agent, c["metric"], c["period_type"], ym)
            ok = _cmp(c["operator"], actual, c["value"])
            met += ok
            if not ok:
                gaps.append(f"{c['metric']}({c['period_type']}) {actual:,.2f} / {c['value']:,.2f}")
        out.append(dict(사번=agent["internal_code"], 이름=agent["name"], 현재직급=lvl["name"], 목표직급=crits[0]["to_name"],
                        충족=f"{met}/{len(crits)}", 승격후보=met == len(crits), 미충족=", ".join(gaps)))
    return pd.DataFrame(out).sort_values(["승격후보", "충족"], ascending=False) if out else pd.DataFrame()


def goal_progress(con, ym) -> pd.DataFrame:
    rows = db.df(con, """
        SELECT a.internal_code 사번, a.name 이름, g.period_type 기간유형, g.period_key 기간, g.metric 지표, g.target_value 목표,
               g.set_by 설정자, s.converted_premium, s.net_count, s.commission_amount
        FROM agent_goal g JOIN agent a ON a.agent_id=g.agent_id
        LEFT JOIN (SELECT s.* FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done'
                   WHERE s.run_id=(SELECT MAX(run_id) FROM calc_run WHERE run_type='performance' AND status='done')) s
          ON s.agent_id=g.agent_id AND s.period_type=g.period_type AND s.period_key=g.period_key
        ORDER BY a.internal_code""")
    if rows.empty:
        return rows
    rows["실적"] = rows.apply(lambda r: r.get(r["지표"], 0) or 0, axis=1)
    rows["진척률"] = (rows["실적"] / rows["목표"]).round(3)
    return rows[["사번", "이름", "기간유형", "기간", "지표", "목표", "실적", "진척률", "설정자"]]
