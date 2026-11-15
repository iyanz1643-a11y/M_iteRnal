"""커리어패스: 승격 후보 추출, 목표 진척, 웹 화면용 단계 진척과 목표 역산, 회차와 승인."""
import json
import math

import pandas as pd

from app import db
from app.engine import performance, persist, standards
from app.periods import month_bounds, months_between, period_end, period_key, period_start, ym_add, ym_diff, ym_to_tuple


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
        return persist.rate(con, [agent["agent_id"]], "p13", ym)[0]
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
    for agent in con.execute("SELECT * FROM agent WHERE status='active' AND job_type='fc'").fetchall():
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


# ---------------------------------------------------------------- 웹 화면용 (S4)
# 커리어패스 운영(/program/career)과 내 커리어패스(/my/career)가 부른다. 모두 범위 안 설계사 목록 agent_ids 를 받는다.
# 판정 값은 promotion_candidates 와 같은 _metric 에서 온다. 분기 보험료 지표는 최근 끝난 분기의 저장된 업적 run 값이다.
# 목표 역산은 지금 진행 중인 기간의 실적을 performance.tally 로 읽는다. 실적 요약 화면과 같은 규칙이다.
# 성립한 달에 더하고 철회한 달에 뺀다. 기준월(as_of_ym)까지를 이미 지난 달로 보고 그 뒤의 달을 남은 달로 센다.
# 기준월이 기간의 마지막 달이면 다음 기간을 목표로 잡는다. 다음 회차가 그 기간을 보기 때문이다. 가정이다.
# 승인은 메인 관리자만 한다. 승인하면 단계 이력에 새 줄을 넣고 앞 줄을 닫는다. 무르기는 열린 결정이라 사유를 적고 무르게 둔 가정이다.
# 무르면 새 줄의 effective_to 를 시작 달 앞으로 당겨 어느 달에도 걸리지 않게 하고 앞 줄을 되돌린다. 줄은 지우지 않는다.
# 금액 가리기 (D-17). 수수료 지표(commission_amount)는 본인과 메인 관리자만 본다. viewer 가 없으면 가린다.
CAREER_METRIC_KO = {"converted_premium": "인정보험료", "new_count": "순건수", "net_count": "순건수", "monthly_premium": "보험료",
                    "commission_amount": "수수료", "persistency_13": "13회차 유지율", "tenure_months": "재직 개월", "team_size": "팀 인원"}
CAREER_METRIC_UNIT = {"converted_premium": "won", "monthly_premium": "won", "commission_amount": "won", "persistency_13": "rate",
                      "tenure_months": "month", "team_size": "person"}
PERIOD_COL = {"converted_premium": "converted_premium", "new_count": "net_count", "net_count": "net_count",
              "monthly_premium": "monthly_premium"}                       # _metric 이 저장된 run 에서 읽는 칸과 같다
PLANNED_COL = {"converted_premium": "applied_converted", "monthly_premium": "applied_premium", "new_count": "applied_count",
               "net_count": "applied_count"}
CAREER_MONEY_HIDDEN = ("commission_amount",)
BACKSOLVE_NOTE = "남은 달에 같은 양을 채운다고 보고 나눈 값입니다. 청약은 성립해야 실적이 됩니다."


def _can_see_money(viewer, agent_id) -> bool:
    if not viewer:
        return False
    if viewer.get("role") == "main":
        return True
    return viewer.get("agent_id") is not None and int(viewer["agent_id"]) == int(agent_id)


def _pair_key(con, from_level_id, to_level_id) -> str:
    f = con.execute("SELECT code FROM career_level WHERE level_id=?", (from_level_id,)).fetchone()
    t = con.execute("SELECT code FROM career_level WHERE level_id=?", (to_level_id,)).fetchone()
    return f"{f['code']}>{t['code']}"


def _level(row) -> dict | None:
    return {"level_id": row["level_id"], "code": row["code"], "name": row["name"], "rank_order": row["rank_order"]} if row else None


def _next_pair(con, level_id, ym):
    """그 단계에서 갈 수 있는 다음 단계 가운데 가장 낮은 것과 그 기준 줄."""
    to = con.execute(
        """SELECT DISTINCT t.* FROM career_promotion_criteria c JOIN career_level t ON t.level_id=c.to_level_id
           WHERE c.from_level_id=? AND c.effective_from<=? AND (c.effective_to IS NULL OR c.effective_to>=?)
           ORDER BY t.rank_order, t.level_id LIMIT 1""", (level_id, ym, ym)).fetchone()
    if not to:
        return None, []
    crits = con.execute(
        """SELECT * FROM career_promotion_criteria WHERE from_level_id=? AND to_level_id=? AND effective_from<=?
           AND (effective_to IS NULL OR effective_to>=?) ORDER BY metric, period_type, id""", (level_id, to["level_id"], ym, ym)).fetchall()
    return to, crits


def _remaining(op, actual, target, met):
    if met:
        return 0
    if actual is None:
        return None
    if op == ">=":
        return max(0, target - actual)
    if op == "<=":
        return max(0, actual - target)
    return None


def _basis_key(ptype, ym):
    try:
        return last_full_quarter_key(ym) if ptype == "quarter" else period_key(ptype, ym)
    except ValueError:
        return None


def backsolve(con, agent_id, metric, period_type, operator, target, as_of_ym) -> dict | None:
    """목표 역산 하나. 기간 합계 지표(보험료, 건수)와 '>=' 기준만 한다. 그 밖은 None.

    돌려주는 것: {period_type, period_key, start_ym, end_ym, so_far, planned, target, remaining, months_left, monthly_needed, note}
    so_far 는 진행 중 기간의 확정 실적, planned 는 아직 청약인 예정 실적이다. monthly_needed 는 올림한 정수다."""
    col = PERIOD_COL.get(metric)
    if not col or operator != ">=" or period_type not in ("month", "quarter", "half", "year"):
        return None
    if period_end(period_type, as_of_ym) == as_of_ym:          # 이번 달로 끝난 기간이면 다음 기간을 목표로 한다
        nxt = ym_add(as_of_ym, 1)
        start, end, so_far, planned = period_start(period_type, nxt), period_end(period_type, nxt), 0, 0
    else:
        start, end = period_start(period_type, as_of_ym), period_end(period_type, as_of_ym)
        row = performance.tally(con, start, as_of_ym, agent_ids=[agent_id]).get(agent_id) or performance.blank()
        so_far, planned = row[col], row[PLANNED_COL[metric]]
    months_left = ym_diff(end, as_of_ym)
    remaining = max(0, target - so_far)
    monthly = math.ceil(remaining / months_left) if remaining > 0 and months_left > 0 else 0
    return {"period_type": period_type, "period_key": period_key(period_type, start), "start_ym": start, "end_ym": end,
            "so_far": so_far, "planned": planned, "target": target, "remaining": remaining, "months_left": months_left,
            "monthly_needed": monthly, "note": BACKSOLVE_NOTE}


def _judge(con, agent, level, crits, ym, viewer):
    rows, met = [], 0
    for c in crits:
        try:
            actual = _metric(con, agent, c["metric"], c["period_type"], ym)
        except ValueError:                                       # rolling_12m 처럼 아직 셀 수 없는 기간
            actual = None
        ok = actual is not None and _cmp(c["operator"], actual, c["value"])
        met += ok
        rem = _remaining(c["operator"], actual, c["value"], ok)
        row = {"metric": c["metric"], "metric_ko": CAREER_METRIC_KO.get(c["metric"], c["metric"]),
               "unit": CAREER_METRIC_UNIT.get(c["metric"], "count"), "period_type": c["period_type"],
               "basis_key": _basis_key(c["period_type"], ym), "op": c["operator"], "target": c["value"],
               "actual": actual, "met": bool(ok), "remaining": rem, "reach_ym": None, "hidden": False,
               "backsolve": None if ok else backsolve(con, agent["agent_id"], c["metric"], c["period_type"], c["operator"], c["value"], ym)}
        if c["metric"] == "tenure_months" and not ok and rem is not None:
            row["reach_ym"] = ym_add(ym, int(math.ceil(rem)))
        if c["metric"] in CAREER_MONEY_HIDDEN and not _can_see_money(viewer, agent["agent_id"]):
            row.update(actual=None, remaining=None, backsolve=None, hidden=True)
        rows.append(row)
    return rows, met


def career_status(con, agent_ids, as_of_ym, *, viewer=None) -> list:
    """사람별 현재 단계, 다음 단계의 기준, 남은 양, 목표 역산. 범위 안 재직자만.

    돌려주는 것: [{agent_id, code, name, job_type, level, next_level, criteria, n_met, n_total, eligible, version, std_key, as_of_ym}]
    - level, next_level: {level_id, code, name, rank_order} 또는 None. 단계 이력이 없거나 맨 위 단계면 None 이다
    - criteria: [{metric, metric_ko, unit, period_type, basis_key, op, target, actual, met, remaining, reach_ym, hidden, backsolve}]
      basis_key 는 판정에 쓴 기간이다. 분기는 최근 끝난 분기다. backsolve 는 backsolve() 의 모양이고 채운 기준은 None 이다.
    - version: 지금 기준과 내용이 같은 판 {version_id, version_no, label}. 아직 회차에 쓰지 않았으면 None 이다
    """
    ids = standards.scope_ids(agent_ids)
    if not ids:
        return []
    out = []
    for agent in con.execute(f"SELECT * FROM agent WHERE status='active' AND agent_id IN ({','.join('?' * len(ids))}) ORDER BY internal_code", ids):
        lvl = current_level(con, agent["agent_id"], as_of_ym)
        to, crits = _next_pair(con, lvl["level_id"], as_of_ym) if lvl else (None, [])
        criteria, met = _judge(con, agent, lvl, crits, as_of_ym, viewer)
        key = f"{lvl['code']}>{to['code']}" if to else None
        ver = standards.find(con, "career", key, int(as_of_ym[:4]), standards.career_criteria(con, lvl["level_id"], to["level_id"], as_of_ym)) if to else None
        out.append({"agent_id": agent["agent_id"], "code": agent["internal_code"], "name": agent["name"], "job_type": agent["job_type"],
                    "as_of_ym": as_of_ym, "level": _level(lvl), "next_level": _level(to), "std_key": key, "criteria": criteria,
                    "n_met": int(met), "n_total": len(criteria), "eligible": bool(criteria) and met == len(criteria),
                    "version": {"version_id": ver["version_id"], "version_no": ver["version_no"], "label": ver["label"]} if ver else None})
    return out


def career_candidates(con, agent_ids, as_of_ym, *, viewer=None) -> list:
    """승격 후보. career_status 가운데 다음 단계가 있는 사람을 채운 기준 수가 많은 순으로. eligible 이 참이면 기준을 모두 채웠다."""
    rows = [r for r in career_status(con, agent_ids, as_of_ym, viewer=viewer) if r["next_level"]]
    rows.sort(key=lambda r: (not r["eligible"], -(r["n_met"] / r["n_total"] if r["n_total"] else 0), r["code"]))
    return rows


def goal_backsolve(con, agent_ids, as_of_ym, *, viewer=None) -> list:
    """사람이 정한 목표(agent_goal)의 역산. as_of_ym 이 든 기간의 목표만 본다.

    돌려주는 것: [{goal_id, agent_id, code, name, period_type, period_key, metric, metric_ko, target, set_by, so_far, planned,
    remaining, progress, months_left, monthly_needed, last_month, supported, hidden, note}]
    so_far 는 기간 첫 달부터 as_of_ym 까지의 확정 실적이다. 실적 요약 화면과 같은 값이다. 업적 집계에 없는 지표는 supported 가 거짓이다.
    last_month 가 참이면 기준월이 기간의 마지막 달이라 남은 달이 없다."""
    ids = standards.scope_ids(agent_ids)
    if not ids:
        return []
    out = []
    goals = con.execute(f"""SELECT g.*, a.internal_code code, a.name FROM agent_goal g JOIN agent a ON a.agent_id=g.agent_id
                            WHERE g.agent_id IN ({','.join('?' * len(ids))}) ORDER BY a.internal_code, g.period_type, g.metric""", ids).fetchall()
    cache = {}
    for g in goals:
        try:
            if g["period_key"] != period_key(g["period_type"], as_of_ym):
                continue
            start, end = period_start(g["period_type"], as_of_ym), period_end(g["period_type"], as_of_ym)
        except ValueError:
            continue
        if (start, as_of_ym) not in cache:
            cache[(start, as_of_ym)] = performance.tally(con, start, as_of_ym, agent_ids=ids)
        row = cache[(start, as_of_ym)].get(g["agent_id"]) or performance.blank()
        metric = g["metric"]
        supported = metric in performance.ROW_KEYS
        so_far = row[metric] if supported else None
        planned = row.get(PLANNED_COL.get(metric, ""), None) if supported else None
        months_left = ym_diff(end, as_of_ym)
        remaining = max(0, g["target_value"] - so_far) if supported else None
        monthly = (math.ceil(remaining / months_left) if remaining and months_left > 0 else (0 if remaining == 0 else None)) if supported else None
        hidden = metric in CAREER_MONEY_HIDDEN and not _can_see_money(viewer, g["agent_id"])
        item = {"goal_id": g["id"], "agent_id": g["agent_id"], "code": g["code"], "name": g["name"], "period_type": g["period_type"],
                "period_key": g["period_key"], "metric": metric, "metric_ko": CAREER_METRIC_KO.get(metric, metric), "target": g["target_value"],
                "set_by": g["set_by"], "so_far": so_far, "planned": planned, "remaining": remaining,
                "progress": round(min(so_far / g["target_value"], 1.0), 3) if supported and g["target_value"] else None,
                "months_left": months_left, "monthly_needed": monthly, "last_month": months_left == 0, "supported": supported,
                "hidden": hidden, "note": BACKSOLVE_NOTE}
        if hidden:
            item.update(target=None, so_far=None, planned=None, remaining=None, monthly_needed=None)
        out.append(item)
    return out


# ---------------------------------------------------------------- 회차, 승인, 결과
def _round(con, round_id):
    r = con.execute("SELECT * FROM career_round WHERE round_id=?", (int(round_id),)).fetchone()
    if not r:
        raise ValueError("없는 회차입니다.")
    return r


def open_round(con, as_of_ym, by) -> dict:
    """커리어 회차를 연다. 메인 관리자만 한다. 회차는 최근 끝난 분기(round_key)마다 하나다.
    재직 설계사 전원을 판정해 후보 줄을 남기고, 판정에 쓴 기준의 판을 standard_version 에 남긴다.
    같은 분기에 열린 회차가 있으면 결정이 없을 때만 그 회차를 replaced 로 두고 새로 연다. 돌려주는 것: round_detail() 의 모양."""
    by = standards.require_main(con, by)
    standards.ensure(con)
    key = last_full_quarter_key(as_of_ym)
    live = con.execute("SELECT round_id FROM career_round WHERE round_key=? AND status='open'", (key,)).fetchone()
    if live:
        if con.execute("SELECT 1 FROM career_decision WHERE round_id=? AND voided_at IS NULL", (live["round_id"],)).fetchone():
            raise ValueError("승인이나 반려가 있는 회차는 다시 열 수 없습니다. 결정을 무른 뒤 여십시오.")
        con.execute("UPDATE career_round SET status='replaced' WHERE round_id=?", (live["round_id"],))
    everyone = [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' AND job_type='fc' ORDER BY agent_id")]
    rows = [r for r in career_status(con, everyone, as_of_ym, viewer={"role": "main"}) if r["next_level"]]
    at = db.now()
    rid = con.execute("""INSERT INTO career_round(round_key, as_of_ym, n_members, n_eligible, status, opened_by, opened_at)
                         VALUES(?,?,?,?,'open',?,?)""", (key, as_of_ym, len(rows), sum(r["eligible"] for r in rows), by, at)).lastrowid
    vers = {}
    for r in rows:
        k = r["std_key"]
        if k not in vers:
            crit = standards.career_criteria(con, r["level"]["level_id"], r["next_level"]["level_id"], as_of_ym)
            vers[k] = standards.record(con, "career", k, int(as_of_ym[:4]), crit, issuer=standards.CAREER_ISSUER, by=by,
                                       used_in="career_round", ref_id=rid)
        detail = [{x: c[x] for x in ("metric", "period_type", "basis_key", "op", "target", "actual", "met")} for c in r["criteria"]]
        con.execute("""INSERT INTO career_round_member(round_id, agent_id, from_level_id, to_level_id, n_met, n_total, eligible, detail_json, version_id)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (rid, r["agent_id"], r["level"]["level_id"], r["next_level"]["level_id"], r["n_met"], r["n_total"], int(r["eligible"]),
                     json.dumps(detail, ensure_ascii=False), vers[k]))
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (at, by, "career_round_open", f"round:{rid}", json.dumps({"round_key": key, "as_of_ym": as_of_ym, "n": len(rows)})))
    con.commit()
    return round_detail(con, rid, everyone, viewer={"role": "main"})


def rounds(con, agent_ids) -> list:
    """회차 목록. 수는 범위 안 사람으로 센다. [{round_id, round_key, as_of_ym, status, opened_by, opened_at,
    n_members, n_eligible, n_approved, n_rejected}] 늦은 회차부터."""
    standards.ensure(con)
    ids = standards.scope_ids(agent_ids)
    marks = ",".join("?" * len(ids)) or "NULL"
    out = []
    for r in con.execute("SELECT * FROM career_round ORDER BY round_key DESC, round_id DESC"):
        n = con.execute(f"""SELECT COUNT(*) n, COALESCE(SUM(m.eligible),0) e,
                                   COALESCE(SUM(d.decision='approved'),0) a, COALESCE(SUM(d.decision='rejected'),0) j
                            FROM career_round_member m
                            LEFT JOIN career_decision d ON d.round_id=m.round_id AND d.agent_id=m.agent_id AND d.voided_at IS NULL
                            WHERE m.round_id=? AND m.agent_id IN ({marks})""", (r["round_id"], *ids)).fetchone()
        out.append({"round_id": r["round_id"], "round_key": r["round_key"], "as_of_ym": r["as_of_ym"], "status": r["status"],
                    "opened_by": r["opened_by"], "opened_at": r["opened_at"], "n_members": n["n"], "n_eligible": n["e"],
                    "n_approved": n["a"], "n_rejected": n["j"]})
    return out


def _levels(con) -> dict:
    return {r["level_id"]: _level(r) for r in con.execute("SELECT * FROM career_level")}


def round_detail(con, round_id, agent_ids, *, viewer=None) -> dict:
    """회차 하나의 후보와 결정. 범위 안 사람만 싣는다.

    돌려주는 것: {round: {round_id, round_key, as_of_ym, status, opened_by, opened_at}, versions: [판], members: [{agent_id, code, name,
    from_level, to_level, n_met, n_total, eligible, criteria, version_label, decision}]}
    decision 은 무르지 않은 결정 {decision_id, decision, effective_ym, note, decided_by, decided_at} 이거나 None 이다."""
    standards.ensure(con)
    r = _round(con, round_id)
    ids = standards.scope_ids(agent_ids)
    lv = _levels(con)
    members = []
    if ids:
        for m in con.execute(f"""SELECT m.*, a.internal_code code, a.name, v.year v_year, v.version_no FROM career_round_member m
                                 JOIN agent a ON a.agent_id=m.agent_id LEFT JOIN standard_version v ON v.version_id=m.version_id
                                 WHERE m.round_id=? AND m.agent_id IN ({','.join('?' * len(ids))})
                                 ORDER BY m.eligible DESC, m.n_met DESC, a.internal_code""", (r["round_id"], *ids)):
            d = con.execute("""SELECT decision_id, decision, effective_ym, note, decided_by, decided_at FROM career_decision
                               WHERE round_id=? AND agent_id=? AND voided_at IS NULL""", (r["round_id"], m["agent_id"])).fetchone()
            crit = json.loads(m["detail_json"])
            for c in crit:
                c["metric_ko"] = CAREER_METRIC_KO.get(c["metric"], c["metric"])
                c["hidden"] = c["metric"] in CAREER_MONEY_HIDDEN and not _can_see_money(viewer, m["agent_id"])
                if c["hidden"]:
                    c["actual"] = None
            members.append({"agent_id": m["agent_id"], "code": m["code"], "name": m["name"], "from_level": lv.get(m["from_level_id"]),
                            "to_level": lv.get(m["to_level_id"]), "n_met": m["n_met"], "n_total": m["n_total"], "eligible": bool(m["eligible"]),
                            "criteria": crit, "version_id": m["version_id"],
                            "version_label": standards._label(m["v_year"], m["version_no"]) if m["version_no"] else None,
                            "decision": dict(d) if d else None})
    return {"round": {k: r[k] for k in ("round_id", "round_key", "as_of_ym", "status", "opened_by", "opened_at")},
            "versions": list(standards.used_by(con, "career_round", r["round_id"]).values()), "members": members}


def _decide_check(con, round_id, agent_id):
    r = _round(con, round_id)
    if r["status"] != "open":
        raise ValueError("열린 회차에서만 결정합니다.")
    m = con.execute("SELECT * FROM career_round_member WHERE round_id=? AND agent_id=?", (r["round_id"], int(agent_id))).fetchone()
    if not m:
        raise ValueError("회차에 없는 사람입니다.")
    if con.execute("SELECT 1 FROM career_decision WHERE round_id=? AND agent_id=? AND voided_at IS NULL", (r["round_id"], m["agent_id"])).fetchone():
        raise ValueError("이미 결정했습니다. 무른 뒤 다시 결정하십시오.")
    return r, m


def approve_promotion(con, round_id, agent_id, by, *, effective_ym=None, note=None) -> dict:
    """승격 승인. 메인 관리자만 하고, 기준을 모두 채운 후보만 승인한다. 기준 밖 승인은 열린 결정이다.
    새 단계는 effective_ym 부터다. 비우면 회차 기준월의 다음 달이다. 앞 단계 이력은 그 앞 달로 닫는다.
    돌려주는 것: {decision_id, round_id, agent_id, decision, from_level, to_level, effective_ym, history_id}"""
    by = standards.require_main(con, by)
    standards.ensure(con)
    r, m = _decide_check(con, round_id, agent_id)
    if not m["eligible"]:
        raise ValueError("기준을 모두 채운 후보만 승인합니다.")
    eff = str(effective_ym or ym_add(r["as_of_ym"], 1))
    if len(eff) != 6 or not eff.isdigit() or not 1 <= int(eff[4:]) <= 12:
        raise ValueError("시작 달은 YYYYMM 입니다.")
    prev = con.execute("""SELECT * FROM agent_career_history WHERE agent_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)
                          ORDER BY effective_from DESC, id DESC LIMIT 1""", (m["agent_id"], eff, eff)).fetchone()
    if not prev or prev["level_id"] != m["from_level_id"]:
        raise ValueError("회차를 연 뒤 단계가 바뀌었습니다. 회차를 다시 여십시오.")
    if prev["effective_from"] >= eff:
        raise ValueError("시작 달이 지금 단계의 시작보다 늦어야 합니다.")
    if con.execute("""SELECT 1 FROM agent_career_history WHERE agent_id=? AND id<>? AND effective_from>?
                      AND (effective_to IS NULL OR effective_to>=effective_from)""", (m["agent_id"], prev["id"], prev["effective_from"])).fetchone():
        raise ValueError("뒤에 잡힌 단계 이력이 있습니다.")
    at = db.now()
    con.execute("UPDATE agent_career_history SET effective_to=? WHERE id=?", (ym_add(eff, -1), prev["id"]))
    hid = con.execute("""INSERT INTO agent_career_history(agent_id, level_id, effective_from, effective_to, reason, approved_by)
                         VALUES(?,?,?,?,'promotion',?)""", (m["agent_id"], m["to_level_id"], eff, prev["effective_to"], by)).lastrowid
    did = con.execute("""INSERT INTO career_decision(round_id, agent_id, from_level_id, to_level_id, decision, effective_ym, note, history_id,
                                                     prev_history_id, prev_effective_to, version_id, decided_by, decided_at)
                         VALUES(?,?,?,?,'approved',?,?,?,?,?,?,?,?)""",
                      (r["round_id"], m["agent_id"], m["from_level_id"], m["to_level_id"], eff, " ".join((note or "").split()) or None,
                       hid, prev["id"], prev["effective_to"], m["version_id"], by, at)).lastrowid
    lv = _levels(con)
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (at, by, "career_approve", f"round:{r['round_id']}:agent:{m['agent_id']}",
                 json.dumps({"level": lv[m["from_level_id"]]["code"], "history_id": prev["id"], "effective_to": prev["effective_to"]}),
                 json.dumps({"decision_id": did, "level": lv[m["to_level_id"]]["code"], "history_id": hid, "effective_ym": eff})))
    con.commit()
    return {"decision_id": did, "round_id": r["round_id"], "agent_id": m["agent_id"], "decision": "approved",
            "from_level": lv[m["from_level_id"]], "to_level": lv[m["to_level_id"]], "effective_ym": eff, "history_id": hid}


def reject_promotion(con, round_id, agent_id, by, *, note=None) -> dict:
    """승격 반려. 메인 관리자만 한다. 단계 이력은 건드리지 않는다. 돌려주는 것: {decision_id, round_id, agent_id, decision}"""
    by = standards.require_main(con, by)
    standards.ensure(con)
    r, m = _decide_check(con, round_id, agent_id)
    at = db.now()
    did = con.execute("""INSERT INTO career_decision(round_id, agent_id, from_level_id, to_level_id, decision, note, version_id, decided_by, decided_at)
                         VALUES(?,?,?,?,'rejected',?,?,?,?)""",
                      (r["round_id"], m["agent_id"], m["from_level_id"], m["to_level_id"], " ".join((note or "").split()) or None,
                       m["version_id"], by, at)).lastrowid
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (at, by, "career_reject", f"round:{r['round_id']}:agent:{m['agent_id']}", json.dumps({"decision_id": did})))
    con.commit()
    return {"decision_id": did, "round_id": r["round_id"], "agent_id": m["agent_id"], "decision": "rejected"}


def revoke_decision(con, decision_id, by, reason) -> dict:
    """승인이나 반려를 무른다. 메인 관리자만 하고 사유를 standards.REVOKE_MIN 자 이상 적는다. 무르기 여부는 열린 결정이라 가정이다.
    승인을 무르면 새 단계 이력의 effective_to 를 시작 달 앞으로 당기고 앞 이력의 effective_to 를 되돌린다. 줄은 지우지 않는다.
    그 뒤에 다른 단계 이력이 잡혔으면 막는다. 변경 기록에는 사유의 길이만 남긴다.
    돌려주는 것: {decision_id, decision, revoked_by, revoked_at, reason}"""
    by = standards.require_main(con, by)
    reason = standards.clean_reason(reason)
    standards.ensure(con)
    d = con.execute("SELECT * FROM career_decision WHERE decision_id=?", (int(decision_id),)).fetchone()
    if not d:
        raise ValueError("없는 결정입니다.")
    if d["voided_at"]:
        raise ValueError("이미 무른 결정입니다.")
    at = db.now()
    if d["decision"] == "approved":
        h = con.execute("SELECT * FROM agent_career_history WHERE id=?", (d["history_id"],)).fetchone()
        if con.execute("""SELECT 1 FROM agent_career_history WHERE agent_id=? AND id<>? AND effective_from>?
                          AND (effective_to IS NULL OR effective_to>=effective_from)""", (d["agent_id"], h["id"], h["effective_from"])).fetchone():
            raise ValueError("승인 뒤에 다른 단계 이력이 있어 무를 수 없습니다.")
        con.execute("UPDATE agent_career_history SET effective_to=?, reason='promotion_revoked' WHERE id=?", (ym_add(h["effective_from"], -1), h["id"]))
        con.execute("UPDATE agent_career_history SET effective_to=? WHERE id=?", (d["prev_effective_to"], d["prev_history_id"]))
    con.execute("UPDATE career_decision SET voided_by=?, voided_at=?, void_reason=? WHERE decision_id=? AND voided_at IS NULL",
                (by, at, reason, d["decision_id"]))
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (at, by, "career_revoke", f"decision:{d['decision_id']}", json.dumps({"decision": d["decision"], "reason_len": len(reason)})))
    con.commit()
    return {"decision_id": d["decision_id"], "decision": d["decision"], "revoked_by": by, "revoked_at": at, "reason": reason}


def career_results(con, agent_ids, *, year=None, include_voided=True) -> list:
    """승인과 반려의 결과. 범위 안 사람만. year 를 주면 그 해의 회차만(round_key 앞 네 자리).
    [{decision_id, round_id, round_key, agent_id, code, name, decision, from_level, to_level, effective_ym, note, version_label,
    decided_by, decided_at, voided, voided_by, voided_at, void_reason}] 늦은 결정부터."""
    standards.ensure(con)
    ids = standards.scope_ids(agent_ids)
    if not ids:
        return []
    sql = f"""SELECT d.*, r.round_key, a.internal_code code, a.name, v.year v_year, v.version_no FROM career_decision d
              JOIN career_round r ON r.round_id=d.round_id JOIN agent a ON a.agent_id=d.agent_id
              LEFT JOIN standard_version v ON v.version_id=d.version_id
              WHERE d.agent_id IN ({','.join('?' * len(ids))})"""
    params = list(ids)
    if year is not None:
        sql += " AND substr(r.round_key,1,4)=?"
        params.append(str(year))
    if not include_voided:
        sql += " AND d.voided_at IS NULL"
    lv = _levels(con)
    out = []
    for d in con.execute(sql + " ORDER BY d.decided_at DESC, d.decision_id DESC", params):
        out.append({"decision_id": d["decision_id"], "round_id": d["round_id"], "round_key": d["round_key"], "agent_id": d["agent_id"],
                    "code": d["code"], "name": d["name"], "decision": d["decision"], "from_level": lv.get(d["from_level_id"]),
                    "to_level": lv.get(d["to_level_id"]), "effective_ym": d["effective_ym"], "note": d["note"],
                    "version_label": standards._label(d["v_year"], d["version_no"]) if d["version_no"] else None,
                    "decided_by": d["decided_by"], "decided_at": d["decided_at"], "voided": d["voided_at"] is not None,
                    "voided_by": d["voided_by"], "voided_at": d["voided_at"], "void_reason": d["void_reason"]})
    return out
