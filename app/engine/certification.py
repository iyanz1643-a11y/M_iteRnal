"""인증·임원제도·어워드 진척 계산.

대상 프로그램 (certification_program.category)
  mdrt               MDRT
  association_cert   협회 우수인증설계사 (생명 / 손해)
  insurer_cert       원수사 인증 (원수사별 실적·유지율)
  executive_fc       설계사 임원제도
  executive_manager  관리자 임원제도 (지점장·본부장, 조직 실적 기준)
  award              영업우수설계사 (순위), 명예의전당 (누적 수상)
기준은 그룹 안에서 AND, 그룹 사이는 OR 이다.
"""
import json

from app import db
from app.engine import persist
from app.periods import month_bounds, months_between, ym_to_tuple

CUMULATIVE = {"annual_commission", "annual_first_premium", "annual_recognized_premium", "org_recognized_premium"}
CONCLUDED = "('inforce','lapsed','cancelled','matured')"


def _subtree(con, org_id):
    ids = [org_id]
    for r in con.execute("SELECT org_id FROM org_unit WHERE parent_id=?", (org_id,)).fetchall():
        ids += _subtree(con, r["org_id"])
    return ids


def org_members(con, org_id, ym):
    """조직(하위 포함)의 현재 소속 설계사 agent_id 목록."""
    ids = _subtree(con, org_id)
    marks = ",".join("?" * len(ids))
    return [r["agent_id"] for r in con.execute(
        f"""SELECT h.agent_id FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id
            WHERE a.job_type='fc' AND a.status='active' AND h.org_id IN ({marks}) AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)""",
        ids + [ym, ym])]


def _recognized(con, agent_ids, year, as_of_ym, prog):
    if not agent_ids:
        return 0
    d_from, _ = month_bounds(f"{year}01")
    _, d_to = month_bounds(as_of_ym)
    marks = ",".join("?" * len(agent_ids))
    sql = f"""SELECT COALESCE(SUM(c.converted_premium),0) v FROM contract c JOIN insurer i ON i.insurer_id=c.insurer_id
              WHERE c.agent_id IN ({marks}) AND c.status IN {CONCLUDED} AND c.contract_date>=? AND c.contract_date<?"""
    params = list(agent_ids) + [d_from, d_to]
    if prog["insurer_id"]:
        sql += " AND c.insurer_id=?"
        params.append(prog["insurer_id"])
    elif prog["sector"] != "all":
        sql += " AND i.sector=?"
        params.append(prog["sector"])
    return con.execute(sql, params).fetchone()["v"]


def _metric(con, ctx, crit):
    agent, prog, year, ym = ctx["agent"], ctx["prog"], ctx["year"], ctx["ym"]
    metric, aid = crit["metric"], agent["agent_id"]
    if metric == "annual_commission":
        return con.execute("""SELECT COALESCE(SUM(c.amount),0) v FROM commission_calc c JOIN calc_run r ON r.run_id=c.run_id AND r.status='done'
                              WHERE c.agent_id=? AND c.period_ym BETWEEN ? AND ?""", (aid, f"{year}01", ym)).fetchone()["v"]
    if metric == "annual_first_premium":
        d_from, _ = month_bounds(f"{year}01")
        _, d_to = month_bounds(ym)
        return con.execute(f"SELECT COALESCE(SUM(premium_monthly*12),0) v FROM contract WHERE agent_id=? AND status IN {CONCLUDED} AND contract_date>=? AND contract_date<?",
                           (aid, d_from, d_to)).fetchone()["v"]
    if metric == "annual_recognized_premium":
        return _recognized(con, [aid], year, ym, prog)
    if metric in ("persistency_13", "persistency_25"):
        return persist.rate(con, [aid], "p13" if metric.endswith("13") else "p25", ym, prog["insurer_id"], prog["sector"])[0]
    if metric == "tenure_months":
        return months_between(agent["hire_date"], month_bounds(ym)[1]) if agent["hire_date"] else 0
    if metric == "rank_recognized_premium":
        return ctx["ranks"].get(aid, 9999)
    if metric == "award_count":
        pid = con.execute("SELECT program_id FROM certification_program WHERE code=?", (crit["metric_param"],)).fetchone()
        if not pid:
            return 0
        past = con.execute("SELECT COUNT(*) n FROM agent_certification_award WHERE agent_id=? AND program_id=? AND year<?", (aid, pid["program_id"], year)).fetchone()["n"]
        return past + (1 if ctx["achieved_now"].get((crit["metric_param"], aid)) else 0)
    if metric.startswith("org_"):
        members = ctx["members"]
        if metric == "org_recognized_premium":
            return _recognized(con, members, year, ym, prog)
        if metric == "org_persistency_13":
            return persist.rate(con, members, "p13", ym)[0]
        if metric == "org_headcount":
            return len(members)
        if metric == "org_active_ratio":
            if not members:
                return 0
            d_from, d_to = month_bounds(ym)
            marks = ",".join("?" * len(members))
            act = con.execute(f"SELECT COUNT(DISTINCT agent_id) n FROM contract WHERE agent_id IN ({marks}) AND status IN {CONCLUDED} AND contract_date>=? AND contract_date<?",
                              members + [d_from, d_to]).fetchone()["n"]
            return round(act / len(members), 3)
    return con.execute("SELECT COALESCE(SUM(value),0) v FROM agent_external_metric WHERE agent_id=? AND metric=? AND period_ym BETWEEN ? AND ?",
                       (aid, metric, f"{year}01", ym)).fetchone()["v"]


def _cmp(op, a, b):
    return {"<=": a <= b, ">=": a >= b, "=": a == b}[op]


def _progress(op, actual, target, ok):
    if ok:
        return 1.0
    if op == ">=" and target:
        return max(0.0, min(actual / target, 1.0))
    if op == "<=" and actual:
        return max(0.0, min(target / actual, 1.0))
    return 0.0


def run(con, year, as_of_ym, run_by="system"):
    run_id = db.new_run(con, "certification", as_of_ym, run_by, json.dumps({"year": year}))
    elapsed = ym_to_tuple(as_of_ym)[1]
    ranks = {r["agent_id"]: r["rank_no"] for r in con.execute(
        """SELECT s.agent_id, s.rank_no FROM ranking_snapshot s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
           WHERE s.period_type='year' AND s.metric='recognized_premium' AND s.scope='all'""", (as_of_ym,))}
    achieved_now, n = {}, 0
    for prog in con.execute("SELECT * FROM certification_program WHERE active=1 ORDER BY sort_order").fetchall():
        crits = con.execute("SELECT * FROM certification_criteria WHERE program_id=? AND year=?", (prog["program_id"], year)).fetchall()
        if not crits:
            continue
        groups = {}
        for c in crits:
            groups.setdefault(c["criteria_group"], []).append(c)
        if prog["target"] == "manager":
            subjects = [(a, m["org_id"]) for m in con.execute(
                "SELECT * FROM org_manager WHERE role=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)", (prog["target_role"], as_of_ym, as_of_ym)).fetchall()
                for a in [con.execute("SELECT * FROM agent WHERE agent_id=?", (m["agent_id"],)).fetchone()]]
        else:
            subjects = [(a, None) for a in con.execute("SELECT * FROM agent WHERE status='active' AND job_type='fc'").fetchall()]
        for agent, org_id in subjects:
            ctx = dict(agent=agent, prog=prog, year=year, ym=as_of_ym, ranks=ranks, achieved_now=achieved_now,
                       members=org_members(con, org_id, as_of_ym) if org_id else [])
            best, achieved, projected, detail = 0.0, False, False, {}
            for gname, gcrits in groups.items():
                rates, ok_all, proj_all = [], True, True
                for c in gcrits:
                    actual = _metric(con, ctx, c)
                    proj_val = actual * 12 / elapsed if c["metric"] in CUMULATIVE and elapsed else actual
                    ok, ok_proj = _cmp(c["operator"], actual, c["value"]), _cmp(c["operator"], proj_val, c["value"])
                    rates.append(_progress(c["operator"], actual, c["value"], ok))
                    ok_all &= ok
                    proj_all &= ok_proj
                    label = c["metric"] + (f"({c['metric_param']})" if c["metric_param"] else "")
                    detail[f"{gname}:{label}" if len(groups) > 1 else label] = dict(
                        실적=round(actual, 3), 기준=f"{c['operator']} {c['value']:g}", 충족=bool(ok), 연말예상=round(proj_val, 3))
                best = max(best, min(rates) if rates else 0)
                achieved |= ok_all
                projected |= proj_all
            achieved_now[(prog["code"], agent["agent_id"])] = achieved
            con.execute(
                """INSERT INTO agent_certification_status(run_id, agent_id, org_id, program_id, year, as_of_ym, metric_json, progress_rate, achieved, projected_achieved)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (run_id, agent["agent_id"], org_id, prog["program_id"], year, as_of_ym, json.dumps(detail, ensure_ascii=False),
                 round(best, 3), int(achieved), int(projected)))
            n += 1
    db.finish_run(con, run_id, f"{n} rows")
    return run_id, n
