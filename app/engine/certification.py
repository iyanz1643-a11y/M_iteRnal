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
import math

from app import db
from app.engine import classify, orgs, persist, standards
from app.periods import month_bounds, months_between, ym_to_tuple

CUMULATIVE = {"annual_commission", "annual_first_premium", "annual_recognized_premium", "org_recognized_premium"}
CONCLUDED = "('inforce','lapsed','cancelled','matured')"


def _subtree(con, org_id):
    return sorted(orgs.descendants(con, org_id))


def org_members(con, org_id, ym):
    """조직과 아래 전부(센터, 팀 포함)에 그 달 소속인 재직 설계사 agent_id 목록.
    조직 유지율의 목록과 같아야 하므로 persist.org_members 를 그대로 쓴다. 기존 도구의 조직 화면도 이 함수를 부른다."""
    return persist.org_members(con, org_id, ym)


def _recognized(con, agent_ids, year, as_of_ym, prog):
    if not agent_ids:
        return 0
    d_from, _ = month_bounds(f"{year}01")
    _, d_to = month_bounds(as_of_ym)
    marks = ",".join("?" * len(agent_ids))
    sql = f"""SELECT COALESCE(SUM(c.converted_premium),0) v FROM contract c {classify.JOINS}
              WHERE c.agent_id IN ({marks}) AND c.status IN {CONCLUDED} AND c.contract_date>=? AND c.contract_date<?"""
    params = list(agent_ids) + [d_from, d_to]
    if prog["insurer_id"]:
        sql += " AND c.insurer_id=?"
        params.append(prog["insurer_id"])
    elif prog["sector"] != "all":
        sql += f" AND {classify.SECTOR}=?"
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
        # 앱에서 선발한 해(excellence_selection, 무르지 않은 것)도 센다. 같은 해의 수상 기록이 이미 있으면 한 번만 센다
        past += con.execute("""SELECT COUNT(DISTINCT s.year) n FROM excellence_selection s
                               WHERE s.agent_id=? AND s.program_id=? AND s.year<? AND s.voided_at IS NULL
                                 AND NOT EXISTS (SELECT 1 FROM agent_certification_award w WHERE w.agent_id=s.agent_id AND w.program_id=s.program_id AND w.year=s.year)""",
                            (aid, pid["program_id"], year)).fetchone()["n"]
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
    standards.ensure(con)
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
        # 판정에 쓴 기준의 판을 남긴다. 같은 내용이면 같은 판을 다시 쓴다
        standards.record(con, "cert", prog["code"], year, standards.cert_criteria(con, prog["program_id"], year),
                         issuer=prog["issuer"], by=run_by, used_in="cert_run", ref_id=run_id)
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


# ---------------------------------------------------------------- 읽기
# 다른 곳은 인증 표를 직접 읽지 않고 이 함수를 부른다. 범위 안 설계사 목록 agent_ids 를 받아서만 읽는다.
# 이름과 실적 값은 담지 않는다. 실적 값이 든 metric_json 은 with_detail 이 참일 때만 풀어 detail 로 넣는다.
def _scope(agent_ids):
    ids = []
    for x in agent_ids or ():
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v not in ids:
            ids.append(v)
    return ids


def awards(con, agent_ids, year=None):
    """범위 안 설계사의 인증 확정 기록 agent_certification_award. year 를 주면 그 해만.

    돌려주는 것: dict 목록. agent_id, program_id, code, name, category, year, awarded_at, certificate_no.
    차례는 설계사, 연도, 프로그램 코드다."""
    ids = _scope(agent_ids)
    if not ids:
        return []
    sql = f"""SELECT w.agent_id, w.program_id, p.code, p.name, p.category, w.year, w.awarded_at, w.certificate_no
              FROM agent_certification_award w JOIN certification_program p ON p.program_id=w.program_id
              WHERE w.agent_id IN ({','.join('?' * len(ids))})"""
    params = list(ids)
    if year is not None:
        sql += " AND w.year=?"
        params.append(int(year))
    return [dict(r) for r in con.execute(sql + " ORDER BY w.agent_id, w.year, p.code, w.id", params)]


def latest_run(con, as_of_ym=None):
    """가장 늦은 기준월의 완료된 인증 계산 run 번호. as_of_ym 을 주면 그 달의 것. 없으면 None."""
    sql, params = "SELECT run_id FROM calc_run WHERE run_type='certification' AND status='done'", []
    if as_of_ym:
        sql += " AND period_ym=?"
        params.append(str(as_of_ym))
    r = con.execute(sql + " ORDER BY period_ym DESC, run_id DESC LIMIT 1", params).fetchone()
    return r[0] if r else None


def latest_status(con, agent_ids, as_of_ym=None, achieved_only=False, with_detail=False):
    """범위 안 설계사의 가장 늦은 완료 인증 계산 결과 agent_certification_status. 계산하지 않고 읽기만 한다.

    run 은 latest_run 이 고른 하나다. achieved_only 가 참이면 달성한 줄만.
    돌려주는 것: dict 목록. run_id, as_of_ym, agent_id, org_id, program_id, code, name, category, year,
    progress_rate, achieved, projected_achieved, projected_ym. projected 는 연말 달성 예상이다. with_detail 이면 detail 도 넣는다."""
    ids = _scope(agent_ids)
    if not ids:
        return []
    run_id = latest_run(con, as_of_ym)
    if run_id is None:
        return []
    sql = f"""SELECT s.run_id, s.as_of_ym, s.agent_id, s.org_id, s.program_id, p.code, p.name, p.category, s.year,
                     s.progress_rate, s.achieved, s.projected_achieved, s.projected_ym, s.metric_json
              FROM agent_certification_status s JOIN certification_program p ON p.program_id=s.program_id
              WHERE s.run_id=? AND s.agent_id IN ({','.join('?' * len(ids))})"""
    if achieved_only:
        sql += " AND s.achieved=1"
    out = []
    for r in con.execute(sql + " ORDER BY s.agent_id, p.sort_order, p.code, s.year, s.id", [run_id, *ids]):
        d = dict(r)
        raw = d.pop("metric_json")
        d["achieved"], d["projected_achieved"] = bool(d["achieved"]), (None if d["projected_achieved"] is None else bool(d["projected_achieved"]))
        if with_detail:
            d["detail"] = json.loads(raw) if raw else {}
        out.append(d)
    return out


# ---------------------------------------------------------------- 웹 화면용 (S4)
# 제도 화면(인증과 MDRT, 임원제도, 영업우수)과 내 진척의 인증 화면이 부른다. 범위 안 설계사 목록 agent_ids 를 받아서만 읽는다.
# 계산하지 않고 latest_status 가 고른 완료 run 하나를 읽는다. 기준 시각은 그 run 의 완료 시각(run_at)이다.
# 연말 값과 연말 달성은 예상이라 standards.estimate() 꼴로 넘긴다. 화면은 label('예상')을 붙인다.
# 연 보험료 지표에는 D-33 기준 설명(basis)을 붙인다. 셈 방식은 바꾸지 않았다.
# 금액 가리기 (D-17). 연 수수료(annual_commission)는 수수료라 본인과 메인 관리자만 본다. viewer 는 auth.context() 의 결과다.
# viewer 가 없으면 모두 가린다. 가린 칸은 None 이고 hidden 이 참이다. 달성 여부와 진척률은 가리지 않는다.
CERT_CATEGORIES = ("mdrt", "association_cert", "insurer_cert")
EXEC_CATEGORIES = ("executive_fc", "executive_manager")
AWARD_CATEGORIES = ("award",)
CATEGORY_KO = {"mdrt": "MDRT", "association_cert": "협회 인증", "insurer_cert": "원수사 인증", "executive_fc": "설계사 임원제도",
               "executive_manager": "관리자 임원제도", "award": "영업우수와 명예의전당"}
METRIC_KO = {"annual_commission": "연 수수료", "annual_first_premium": "연 초회보험료", "annual_recognized_premium": "연 인정보험료",
             "persistency_13": "13회차 유지율", "persistency_25": "25회차 유지율", "tenure_months": "재직 개월",
             "rank_recognized_premium": "연 인정보험료 순위", "award_count": "누적 수상 횟수", "org_recognized_premium": "조직 연 인정보험료",
             "org_persistency_13": "조직 13회차 유지율", "org_headcount": "조직 인원", "org_active_ratio": "조직 가동률",
             "mis_selling_count": "불완전판매 건수", "complaint_count": "민원 건수"}
METRIC_UNIT = {"annual_commission": "won", "annual_first_premium": "won", "annual_recognized_premium": "won", "org_recognized_premium": "won",
               "persistency_13": "rate", "persistency_25": "rate", "org_persistency_13": "rate", "org_active_ratio": "rate",
               "tenure_months": "month", "rank_recognized_premium": "rank", "org_headcount": "person"}
MONEY_HIDDEN = ("annual_commission",)
PROJECTED_NOTE = "연말 예상은 지금까지의 월평균이 연말까지 이어진다고 본 값입니다. 확정이 아닙니다."


def _parse_key(key):
    group, label = key.split(":", 1) if ":" in key else ("all", key)
    metric, _, rest = label.partition("(")
    return group, metric, (rest[:-1] if rest.endswith(")") else None)


def _parse_rule(text):
    op, _, val = (text or "").partition(" ")
    try:
        return op, float(val)
    except ValueError:
        return op, None


def _remaining(op, actual, target, met):
    if met or target is None or actual is None:
        return 0 if met else None
    if op == ">=":
        return max(0.0, target - actual)
    if op == "<=":
        return max(0.0, actual - target)
    return None                                     # '=' 은 남은 양으로 말할 수 없다


def _can_see_money(viewer, agent_id) -> bool:
    if not viewer:
        return False
    if viewer.get("role") == "main":
        return True
    return viewer.get("agent_id") is not None and int(viewer["agent_id"]) == int(agent_id)


def _metric_rows(detail, as_of_ym, agent_id, viewer):
    months_left = 12 - int(str(as_of_ym)[4:6])
    out = []
    for key, v in detail.items():
        group, metric, param = _parse_key(key)
        op, target = _parse_rule(v.get("기준"))
        actual, met = v.get("실적"), bool(v.get("충족"))
        cumulative = metric in CUMULATIVE
        remaining = _remaining(op, actual, target, met)
        monthly = None
        if cumulative and op == ">=" and remaining and months_left > 0:
            monthly = math.ceil(remaining / months_left)
        hidden = metric in MONEY_HIDDEN and not _can_see_money(viewer, agent_id)
        row = {"key": key, "group": group, "metric": metric, "metric_ko": METRIC_KO.get(metric, metric), "param": param,
               "unit": METRIC_UNIT.get(metric, "count"), "op": op, "target": target, "met": met, "cumulative": cumulative,
               "actual": actual, "remaining": remaining,
               "year_end": standards.estimate(v.get("연말예상")) if cumulative else None,
               "monthly_needed": monthly, "months_left": months_left if cumulative else None,
               "hidden": hidden, "basis": standards.basis(metric)}
        if hidden:
            row.update(actual=None, remaining=None, monthly_needed=None, year_end=standards.estimate(None) if cumulative else None)
        out.append(row)
    return out


def progress(con, agent_ids, *, categories=None, as_of_ym=None, viewer=None, achieved_only=False) -> dict:
    """범위 안 설계사의 제도 판정 결과. categories 로 분류를 고른다. 비면 전부.

    돌려주는 것: {run_id, as_of_ym, run_at, year, rows, summary, basis, projected_note}
    - rows: [{run_id, as_of_ym, year, agent_id, code, name, org_id, org_name, program_id, program_code, program_name, category,
              category_ko, issuer, sector, target, progress_rate, achieved, projected, version, metrics, basis}]
      projected 는 {value: 연말 달성 예상 참거짓, is_estimate: True, label: '예상'} 이다.
      version 은 판정에 쓴 기준의 판 {version_id, version_no, label}. 판을 남기기 전의 run 이면 None 이다.
      metrics 는 지표마다 {key, group, metric, metric_ko, param, unit, op, target, actual, met, remaining, cumulative,
      year_end(예상), monthly_needed, months_left, hidden, basis}. monthly_needed 는 남은 달에 월마다 더 필요한 양이다.
    - summary: [{program_code, program_name, category, n, n_achieved, n_projected}] 범위 안 인원으로 센다
    - basis: 연 보험료 지표가 하나라도 있으면 D-33 기준 설명, 없으면 None
    """
    cats = tuple(categories) if categories else None
    rows = [r for r in latest_status(con, agent_ids, as_of_ym, achieved_only=achieved_only, with_detail=True)
            if cats is None or r["category"] in cats]
    run_id = rows[0]["run_id"] if rows else latest_run(con, as_of_ym)
    run = con.execute("SELECT period_ym, finished_at FROM calc_run WHERE run_id=?", (run_id,)).fetchone() if run_id else None
    head = {"run_id": run_id, "as_of_ym": run["period_ym"] if run else None, "run_at": run["finished_at"] if run else None,
            "year": rows[0]["year"] if rows else None, "projected_note": PROJECTED_NOTE}
    if not rows:
        return {**head, "rows": [], "summary": [], "basis": None}
    progs = {r["program_id"]: r for r in con.execute("SELECT program_id, issuer, sector, target FROM certification_program")}
    ids = sorted({r["agent_id"] for r in rows})
    people = {r["agent_id"]: r for r in con.execute(
        f"SELECT agent_id, internal_code, name FROM agent WHERE agent_id IN ({','.join('?' * len(ids))})", ids)}
    org_ids = sorted({r["org_id"] for r in rows if r["org_id"]})
    org_names = {r["org_id"]: r["name"] for r in con.execute(
        f"SELECT org_id, name FROM org_unit WHERE org_id IN ({','.join('?' * len(org_ids))})", org_ids)} if org_ids else {}
    vers = standards.used_by(con, "cert_run", run_id)
    out, summary, any_basis = [], {}, False
    for r in rows:
        pg, pe = progs.get(r["program_id"]), people.get(r["agent_id"])
        v = vers.get(r["code"])
        metrics = _metric_rows(r.get("detail") or {}, r["as_of_ym"], r["agent_id"], viewer)
        b = next((m["basis"] for m in metrics if m["basis"]), None)
        any_basis |= bool(b)
        out.append({"run_id": r["run_id"], "as_of_ym": r["as_of_ym"], "year": r["year"], "agent_id": r["agent_id"],
                    "code": pe["internal_code"] if pe else None, "name": pe["name"] if pe else None,
                    "org_id": r["org_id"], "org_name": org_names.get(r["org_id"]),
                    "program_id": r["program_id"], "program_code": r["code"], "program_name": r["name"], "category": r["category"],
                    "category_ko": CATEGORY_KO.get(r["category"], r["category"]), "issuer": pg["issuer"] if pg else None,
                    "sector": pg["sector"] if pg else None, "target": pg["target"] if pg else None,
                    "progress_rate": r["progress_rate"], "achieved": r["achieved"],
                    "projected": standards.estimate(r["projected_achieved"]),
                    "version": {"version_id": v["version_id"], "version_no": v["version_no"], "label": v["label"]} if v else None,
                    "metrics": metrics, "basis": b})
        s = summary.setdefault(r["code"], {"program_code": r["code"], "program_name": r["name"], "category": r["category"],
                                           "n": 0, "n_achieved": 0, "n_projected": 0})
        s["n"] += 1
        s["n_achieved"] += int(r["achieved"])
        s["n_projected"] += int(bool(r["projected_achieved"]))
    return {**head, "rows": out, "summary": list(summary.values()),
            "basis": {"note": standards.BASIS_NOTE, "decision": standards.BASIS_DECISION, "assumed": True} if any_basis else None}


def cert_progress(con, agent_ids, *, as_of_ym=None, viewer=None) -> dict:
    """생명보험과 손해보험 인증, 원수사 인증, MDRT 의 진척과 연말 예상. 모양은 progress() 와 같다."""
    return progress(con, agent_ids, categories=CERT_CATEGORIES, as_of_ym=as_of_ym, viewer=viewer)


def executive_status(con, agent_ids, *, as_of_ym=None, viewer=None) -> dict:
    """관리자 임원제도와 설계사 임원제도의 판정. progress() 의 모양에 fc 와 manager 목록을 더한다.
    관리자 임원제도는 조직장 본인이 범위 안에 있을 때만 보이고, org_id 와 org_name 이 평가 조직이다."""
    res = progress(con, agent_ids, categories=EXEC_CATEGORIES, as_of_ym=as_of_ym, viewer=viewer)
    res["fc"] = [r for r in res["rows"] if r["category"] == "executive_fc"]
    res["manager"] = [r for r in res["rows"] if r["category"] == "executive_manager"]
    return res


# ---------------------------------------------------------------- 영업우수 선발과 수상 이력
# 선발은 메인 관리자만 한다. 그 해 가장 늦은 완료 인증 run 에서 기준을 채운 사람만 고른다. 기준 밖 선발과 선발 인원은 열린 결정이다.
# 선발은 excellence_selection 에 남고 agent_certification_award 는 건드리지 않는다. 명예의전당의 누적 수상 횟수는 둘을 함께 센다.
# 무르기는 열린 결정이라 사유를 적고 무르게 둔 가정이다. 줄은 지우지 않는다. 변경 기록에는 사유의 길이만 남긴다.
def _program(con, code):
    p = con.execute("SELECT * FROM certification_program WHERE code=?", (code,)).fetchone()
    if not p:
        raise ValueError("없는 프로그램입니다.")
    return p


def _year_status(con, program_id, year, agent_id):
    """그 해 그 프로그램의 가장 늦은 완료 run 의 판정 줄."""
    return con.execute(
        """SELECT s.run_id, s.achieved FROM agent_certification_status s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done'
           WHERE s.program_id=? AND s.year=? AND s.agent_id=? ORDER BY r.period_ym DESC, r.run_id DESC LIMIT 1""",
        (program_id, int(year), agent_id)).fetchone()


def selections(con, agent_ids, year=None, *, include_voided=False) -> list:
    """범위 안 설계사의 영업우수 선발 줄. [{selection_id, program_id, program_code, program_name, year, agent_id, code, name,
    status_run_id, version_id, version_label, note, selected_by, selected_at, voided, voided_by, voided_at, void_reason}]"""
    standards.ensure(con)
    ids = _scope(agent_ids)
    if not ids:
        return []
    sql = f"""SELECT s.*, p.code program_code, p.name program_name, a.internal_code code, a.name,
                     v.year v_year, v.version_no
              FROM excellence_selection s JOIN certification_program p ON p.program_id=s.program_id JOIN agent a ON a.agent_id=s.agent_id
              LEFT JOIN standard_version v ON v.version_id=s.version_id
              WHERE s.agent_id IN ({','.join('?' * len(ids))})"""
    params = list(ids)
    if year is not None:
        sql += " AND s.year=?"
        params.append(int(year))
    if not include_voided:
        sql += " AND s.voided_at IS NULL"
    out = []
    for r in con.execute(sql + " ORDER BY s.year DESC, p.sort_order, a.internal_code, s.selection_id", params):
        d = dict(r)
        vy, vn = d.pop("v_year"), d.pop("version_no")
        d["version_label"] = standards._label(vy, vn) if vn else None
        d["voided"] = d["voided_at"] is not None
        out.append(d)
    return out


def award_history(con, agent_ids, *, categories=AWARD_CATEGORIES) -> list:
    """범위 안 설계사의 수상 이력. 수상 기록(agent_certification_award)과 무르지 않은 앱 선발(excellence_selection)을 합친다.
    [{agent_id, code, name, program_code, program_name, category, years: [{year, source}], count}] 횟수 많은 순.
    source 는 record(수상 기록) 또는 selection(앱 선발)이다. 같은 해가 둘 다 있으면 record 하나로 센다."""
    ids = _scope(agent_ids)
    if not ids:
        return []
    cats = set(categories or ())
    per = {}
    for w in awards(con, ids):
        if cats and w["category"] not in cats:
            continue
        per.setdefault((w["agent_id"], w["code"]), {"program_name": w["name"], "category": w["category"], "years": {}})["years"][w["year"]] = "record"
    for s in selections(con, ids):
        p = con.execute("SELECT category FROM certification_program WHERE program_id=?", (s["program_id"],)).fetchone()
        if cats and p["category"] not in cats:
            continue
        e = per.setdefault((s["agent_id"], s["program_code"]), {"program_name": s["program_name"], "category": p["category"], "years": {}})
        e["years"].setdefault(s["year"], "selection")
    if not per:
        return []
    aids = sorted({a for a, _ in per})
    people = {r["agent_id"]: r for r in con.execute(
        f"SELECT agent_id, internal_code, name FROM agent WHERE agent_id IN ({','.join('?' * len(aids))})", aids)}
    out = [{"agent_id": a, "code": people[a]["internal_code"], "name": people[a]["name"], "program_code": code,
            "program_name": e["program_name"], "category": e["category"],
            "years": [{"year": y, "source": src} for y, src in sorted(e["years"].items())], "count": len(e["years"])}
           for (a, code), e in per.items()]
    out.sort(key=lambda x: (-x["count"], x["program_code"], x["code"]))
    return out


def excellence(con, agent_ids, year=None, *, as_of_ym=None, viewer=None) -> dict:
    """영업우수 화면. progress() 의 모양(분류 award)에 candidates, selections, history 를 더한다.
    candidates 는 기준을 채웠고 그 해 수상 기록도 선발도 없는 줄이다. 선발할 수 있는 사람이다.
    year 를 주지 않으면 판정 run 의 해다."""
    res = progress(con, agent_ids, categories=AWARD_CATEGORIES, as_of_ym=as_of_ym, viewer=viewer)
    year = int(year if year is not None else (res["year"] or 0)) or None
    sel = selections(con, agent_ids, year) if year else []
    taken = {(s["agent_id"], s["program_code"]) for s in sel}
    taken |= {(w["agent_id"], w["code"]) for w in awards(con, agent_ids, year)} if year else set()
    res["year"] = year
    res["candidates"] = [r for r in res["rows"] if r["achieved"] and r["year"] == year and (r["agent_id"], r["program_code"]) not in taken]
    res["selections"] = sel
    res["history"] = award_history(con, agent_ids)
    return res


def select_excellence(con, program_code, year, agent_id, by, note=None) -> dict:
    """영업우수 선발. 메인 관리자만 한다. by 는 login_id 이거나 auth.context() 의 결과다.
    분류가 award 인 프로그램만, 그 해 가장 늦은 완료 인증 run 에서 기준을 채운 사람만 고른다.
    이미 그 해 수상 기록이나 무르지 않은 선발이 있으면 막는다. 돌려주는 것: 선발 줄 하나(selections 의 모양)."""
    by = standards.require_main(con, by)
    standards.ensure(con)
    p = _program(con, program_code)
    if p["category"] not in AWARD_CATEGORIES:
        raise ValueError("영업우수 프로그램만 선발합니다.")
    year, agent_id = int(year), int(agent_id)
    st = _year_status(con, p["program_id"], year, agent_id)
    if not st or not st["achieved"]:
        raise ValueError("그 해 기준을 채운 사람만 선발합니다.")
    if con.execute("SELECT 1 FROM agent_certification_award WHERE agent_id=? AND program_id=? AND year=?", (agent_id, p["program_id"], year)).fetchone():
        raise ValueError("이미 그 해 수상 기록이 있습니다.")
    if con.execute("SELECT 1 FROM excellence_selection WHERE agent_id=? AND program_id=? AND year=? AND voided_at IS NULL",
                   (agent_id, p["program_id"], year)).fetchone():
        raise ValueError("이미 선발했습니다.")
    ver = standards.used_by(con, "cert_run", st["run_id"]).get(p["code"])
    at = db.now()
    sid = con.execute("""INSERT INTO excellence_selection(program_id, year, agent_id, status_run_id, version_id, note, selected_by, selected_at)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (p["program_id"], year, agent_id, st["run_id"], ver["version_id"] if ver else None,
                       " ".join((note or "").split()) or None, by, at)).lastrowid
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (at, by, "excellence_select", f"{p['code']}:{year}:{agent_id}", json.dumps({"selection_id": sid, "run_id": st["run_id"]})))
    con.commit()
    return next(s for s in selections(con, [agent_id], year) if s["selection_id"] == sid)


def revoke_selection(con, selection_id, by, reason) -> dict:
    """선발을 무른다. 메인 관리자만 하고 사유를 standards.REVOKE_MIN 자 이상 적는다. 줄은 지우지 않고 voided 칸만 채운다.
    돌려주는 것: {selection_id, revoked_by, revoked_at, reason}"""
    by = standards.require_main(con, by)
    reason = standards.clean_reason(reason)
    standards.ensure(con)
    s = con.execute("SELECT * FROM excellence_selection WHERE selection_id=?", (int(selection_id),)).fetchone()
    if not s:
        raise ValueError("없는 선발입니다.")
    if s["voided_at"]:
        raise ValueError("이미 무른 선발입니다.")
    at = db.now()
    con.execute("UPDATE excellence_selection SET voided_by=?, voided_at=?, void_reason=? WHERE selection_id=? AND voided_at IS NULL",
                (by, at, reason, s["selection_id"]))
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (at, by, "excellence_revoke", f"selection:{s['selection_id']}", json.dumps({"reason_len": len(reason)})))
    con.commit()
    return {"selection_id": s["selection_id"], "revoked_by": by, "revoked_at": at, "reason": reason}
