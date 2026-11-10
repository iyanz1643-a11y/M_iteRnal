"""인증(MDRT, 우수인증설계사) 진척 계산."""
import json

from app import db
from app.periods import month_bounds, months_between, ym_to_tuple


def _metric(con, agent, metric, year, as_of_ym):
    y_from, y_to = f"{year}01", f"{year}12"
    if metric == "annual_commission":
        row = con.execute(
            """SELECT SUM(c.amount) v FROM commission_calc c JOIN calc_run r ON r.run_id=c.run_id AND r.status='done'
               WHERE c.agent_id=? AND c.period_ym BETWEEN ? AND ?""", (agent["agent_id"], y_from, as_of_ym)).fetchone()
        return row["v"] or 0
    if metric == "annual_first_premium":
        d_from, _ = month_bounds(y_from)
        _, d_to = month_bounds(as_of_ym)
        row = con.execute(
            """SELECT SUM(premium_monthly*12) v FROM contract WHERE agent_id=? AND status IN ('inforce','lapsed','cancelled','matured')
               AND contract_date>=? AND contract_date<?""", (agent["agent_id"], d_from, d_to)).fetchone()
        return row["v"] or 0
    if metric in ("persistency_13", "persistency_25"):
        kind = "p13" if metric.endswith("13") else "p25"
        row = con.execute("SELECT rate FROM persistency WHERE agent_id=? AND kind=? AND period_ym<=? ORDER BY period_ym DESC LIMIT 1",
                          (agent["agent_id"], kind, as_of_ym)).fetchone()
        return row["rate"] if row else 0
    if metric == "tenure_months":
        return months_between(agent["hire_date"], month_bounds(as_of_ym)[1]) if agent["hire_date"] else 0
    row = con.execute("SELECT SUM(value) v FROM agent_external_metric WHERE agent_id=? AND metric=? AND period_ym BETWEEN ? AND ?",
                      (agent["agent_id"], metric, y_from, as_of_ym)).fetchone()
    return row["v"] or 0


def _cmp(op, a, b):
    return {"<=": a <= b, ">=": a >= b, "=": a == b}[op]


def run(con, year, as_of_ym, run_by="system"):
    run_id = db.new_run(con, "certification", as_of_ym, run_by, json.dumps({"year": year}))
    _, m = ym_to_tuple(as_of_ym)
    elapsed = m  # 연초부터 경과 개월
    n = 0
    for prog in con.execute("SELECT * FROM certification_program WHERE active=1"):
        crits = con.execute("SELECT * FROM certification_criteria WHERE program_id=? AND year=?", (prog["program_id"], year)).fetchall()
        if not crits:
            continue
        groups = {}
        for c in crits:
            groups.setdefault(c["criteria_group"], []).append(c)
        for agent in con.execute("SELECT * FROM agent WHERE status='active'"):
            best_rate, achieved, projected, detail = 0.0, False, False, {}
            for gname, gcrits in groups.items():
                rates, ok_all, proj_all = [], True, True
                for c in gcrits:
                    actual = _metric(con, agent, c["metric"], year, as_of_ym)
                    cumulative = c["metric"] in ("annual_commission", "annual_first_premium")
                    proj_val = actual * 12 / elapsed if cumulative and elapsed else actual
                    ok = _cmp(c["operator"], actual, c["value"])
                    ok_proj = _cmp(c["operator"], proj_val, c["value"])
                    if c["operator"] == ">=" and c["value"]:
                        rates.append(min(actual / c["value"], 1.0))
                    else:
                        rates.append(1.0 if ok else 0.0)
                    ok_all &= ok
                    proj_all &= ok_proj
                    detail[f"{gname}:{c['metric']}"] = dict(actual=round(actual, 2), target=c["value"], op=c["operator"], ok=ok,
                                                           projected=round(proj_val, 2))
                g_rate = min(rates) if rates else 0
                best_rate = max(best_rate, g_rate)
                achieved |= ok_all
                projected |= proj_all
            con.execute(
                """INSERT INTO agent_certification_status(run_id, agent_id, program_id, year, as_of_ym, metric_json, progress_rate,
                   achieved, projected_achieved) VALUES(?,?,?,?,?,?,?,?,?)""",
                (run_id, agent["agent_id"], prog["program_id"], year, as_of_ym, json.dumps(detail, ensure_ascii=False),
                 round(best_rate, 3), int(achieved), int(projected)))
            n += 1
    db.finish_run(con, run_id, f"{n} rows")
    return run_id, n
