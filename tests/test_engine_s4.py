"""S4 계산: 제도(파트 I)의 웹용 함수. 커리어, 인증과 MDRT, 임원제도, 영업우수, 기관별 기준 버전.

시험하는 것: 범위, 목표 역산, 기준 버전, 승인과 선발은 메인 관리자만, 예상 표시.
D-33 은 열린 결정이라 셈 방식은 그대로 두고 기준 설명 칸만 붙였다. 무르기는 열린 결정이라 사유를 적고 무르게 둔 가정이다.

자료는 run_demo 의 샘플을 임시 폴더에 한 번 만들고 시험마다 복사해 쓴다. data/demo.db 는 건드리지 않는다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import json
import math
import pathlib
import shutil
import tempfile

import pytest

from app import db
from app.engine import career, results, standards
from app.engine import certification as ce
from app.periods import ym_add

YM = "202610"
MAIN = {"role": "main"}


@pytest.fixture(scope="module")
def base():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="engine-s4-"))
    run_demo.main(tmp / "base.db", tmp / "raw")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def con(base, tmp_path):
    path = tmp_path / "t.db"
    shutil.copy(base / "base.db", path)
    c = db.connect(path)
    yield c
    c.close()


def everyone(con):
    return [r[0] for r in con.execute("SELECT agent_id FROM agent ORDER BY agent_id")]


def main_login(con):
    return con.execute("SELECT login_id FROM user_account WHERE is_main_admin=1 AND status='active' LIMIT 1").fetchone()[0]


def non_main_login(con):
    return con.execute("SELECT login_id FROM user_account WHERE is_main_admin=0 AND status='active' LIMIT 1").fetchone()[0]


def branch_scope(con):
    """한 지점과 그 아래에 그 달 소속인 사람. 관리자 한 명의 범위와 같은 꼴이다."""
    org = con.execute("SELECT org_id FROM org_unit WHERE level='branch' ORDER BY org_id LIMIT 1").fetchone()[0]
    from app.engine import orgs
    sub = sorted(orgs.descendants(con, org))
    q = ",".join("?" * len(sub))
    return sorted({r[0] for r in con.execute(
        f"SELECT agent_id FROM agent_org_history WHERE org_id IN ({q}) AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)",
        (*sub, YM, YM))})


def eligible_member(con, detail):
    return next(m for m in detail["members"] if m["eligible"])


# ---------------------------------------------------------------- 범위
def test_scope_limits_every_reader(con):
    mine = branch_scope(con)
    assert mine and len(mine) < len(everyone(con))
    allowed = set(mine)
    readers = [
        [r["agent_id"] for r in career.career_status(con, mine, YM, viewer=MAIN)],
        [r["agent_id"] for r in career.career_candidates(con, mine, YM, viewer=MAIN)],
        [r["agent_id"] for r in career.goal_backsolve(con, mine, YM, viewer=MAIN)],
        [r["agent_id"] for r in ce.cert_progress(con, mine, viewer=MAIN)["rows"]],
        [r["agent_id"] for r in ce.executive_status(con, mine, viewer=MAIN)["rows"]],
        [r["agent_id"] for r in ce.excellence(con, mine, viewer=MAIN)["rows"]],
        [r["agent_id"] for r in ce.award_history(con, mine)],
    ]
    for got in readers:
        assert set(got) <= allowed
    assert any(readers[3]), "범위 안 인증 줄이 있어야 합니다"
    for fn in (lambda ids: career.career_status(con, ids, YM), lambda ids: career.goal_backsolve(con, ids, YM),
               lambda ids: ce.cert_progress(con, ids)["rows"], lambda ids: ce.award_history(con, ids),
               lambda ids: career.career_results(con, ids), lambda ids: ce.selections(con, ids)):
        assert fn([]) == [] and fn(None) == []


def test_round_and_results_follow_scope(con):
    detail = career.open_round(con, YM, main_login(con))
    rid = detail["round"]["round_id"]
    mine = branch_scope(con)
    part = career.round_detail(con, rid, mine)
    assert {m["agent_id"] for m in part["members"]} <= set(mine)
    full = career.rounds(con, everyone(con))[0]
    small = career.rounds(con, mine)[0]
    assert small["n_members"] <= full["n_members"] and full["n_members"] == len(detail["members"])
    m = eligible_member(con, detail)
    career.approve_promotion(con, rid, m["agent_id"], main_login(con))
    outside = [a for a in everyone(con) if a != m["agent_id"]]
    assert not career.career_results(con, outside)
    assert [r["agent_id"] for r in career.career_results(con, [m["agent_id"]])] == [m["agent_id"]]


def test_manager_programs_need_manager_in_scope(con):
    ex = ce.executive_status(con, everyone(con), viewer=MAIN)
    mgr = ex["manager"][0]
    only_fc = [a for a in everyone(con) if a != mgr["agent_id"]]
    assert all(r["agent_id"] != mgr["agent_id"] for r in ce.executive_status(con, only_fc, viewer=MAIN)["manager"])
    assert mgr["org_id"] and mgr["org_name"]


# ---------------------------------------------------------------- 목표 역산
def test_backsolve_uses_same_numbers_as_results(con):
    rows = career.career_status(con, everyone(con), YM, viewer=MAIN)
    target = next(r for r in rows for c in r["criteria"] if c["metric"] == "converted_premium" and c["backsolve"])
    c = next(c for c in target["criteria"] if c["metric"] == "converted_premium")
    b = c["backsolve"]
    assert b["period_key"] == "2026Q4" and b["start_ym"] == "202610" and b["end_ym"] == "202612" and b["months_left"] == 2
    summary = results.period_summary(con, [target["agent_id"]], "quarter", YM)
    same = summary["rows"][0]["converted_premium"] if summary["rows"] else 0
    assert b["so_far"] == same, "목표 역산의 실적이 실적 요약과 달라졌습니다"
    assert b["remaining"] == max(0, c["target"] - b["so_far"])
    assert b["monthly_needed"] == (math.ceil(b["remaining"] / 2) if b["remaining"] else 0)
    assert b["monthly_needed"] * b["months_left"] >= b["remaining"]


def test_backsolve_at_period_end_targets_next_period(con):
    agent = con.execute("SELECT agent_id FROM agent WHERE job_type='fc' ORDER BY agent_id LIMIT 1").fetchone()[0]
    b = career.backsolve(con, agent, "converted_premium", "quarter", ">=", 1_000_000, "202609")
    assert (b["period_key"], b["start_ym"], b["end_ym"], b["months_left"], b["so_far"]) == ("2026Q4", "202610", "202612", 3, 0)
    assert b["monthly_needed"] == math.ceil(1_000_000 / 3)
    assert career.backsolve(con, agent, "persistency_13", "month", ">=", 0.9, YM) is None
    assert career.backsolve(con, agent, "converted_premium", "quarter", "<=", 1, YM) is None


def test_tenure_reach_month_and_met_criteria_have_no_backsolve(con):
    rows = career.career_status(con, everyone(con), YM, viewer=MAIN)
    for r in rows:
        for c in r["criteria"]:
            if c["met"]:
                assert c["backsolve"] is None and c["remaining"] == 0
            if c["metric"] == "tenure_months" and not c["met"]:
                assert c["reach_ym"] == ym_add(YM, math.ceil(c["remaining"]))
    # 판정은 기존 승격 후보와 같다
    old = career.promotion_candidates(con, YM)
    assert int(old["승격후보"].sum()) == sum(r["eligible"] for r in rows)


def test_goal_backsolve_matches_tally(con):
    goals = career.goal_backsolve(con, everyone(con), YM, viewer=MAIN)
    assert goals and all(g["period_key"] in ("2026Q4", "2026") for g in goals)
    q = next(g for g in goals if g["period_key"] == "2026Q4")
    same = results.period_summary(con, [q["agent_id"]], "quarter", YM)["rows"]
    assert q["so_far"] == (same[0][q["metric"]] if same else 0)
    assert q["months_left"] == 2 and not q["last_month"]
    if q["remaining"]:
        assert q["monthly_needed"] == math.ceil(q["remaining"] / 2)
    y = next(g for g in goals if g["period_key"] == "2026")
    assert y["months_left"] == 2 and y["supported"]


def test_cert_monthly_need_for_cumulative_metric(con):
    rows = ce.cert_progress(con, everyone(con), viewer=MAIN)["rows"]
    m = next(m for r in rows for m in r["metrics"] if m["metric"] == "annual_recognized_premium" and not m["met"])
    assert m["months_left"] == 2
    assert m["monthly_needed"] == math.ceil(m["remaining"] / 2)


# ---------------------------------------------------------------- 기준 버전
def test_cert_run_keeps_version_and_new_version_on_change(con):
    run1 = ce.latest_run(con)
    v1 = standards.used_by(con, "cert_run", run1)["MDRT"]
    assert v1["version_no"] == 1 and v1["issuer"] == "MDRT" and v1["criteria"]
    run2, _ = ce.run(con, 2026, YM, "test")
    assert standards.used_by(con, "cert_run", run2)["MDRT"]["version_id"] == v1["version_id"], "같은 기준이면 같은 판이어야 합니다"
    con.execute("UPDATE certification_criteria SET value=value+1 WHERE program_id=(SELECT program_id FROM certification_program WHERE code='MDRT')")
    run3, _ = ce.run(con, 2026, YM, "test")
    v2 = standards.used_by(con, "cert_run", run3)["MDRT"]
    assert v2["version_no"] == 2 and v2["version_id"] != v1["version_id"]
    assert standards.used_by(con, "cert_run", run1)["MDRT"]["version_id"] == v1["version_id"], "앞의 판정은 앞의 판을 가리켜야 합니다"
    assert standards.used_by(con, "cert_run", run3)["SAM_ACE"]["version_no"] == 1
    row = next(r for r in ce.cert_progress(con, everyone(con), viewer=MAIN)["rows"] if r["program_code"] == "MDRT")
    assert row["version"]["label"] == "2026년 기준 2판"
    labels = [v["label"] for v in standards.versions(con, std_key="MDRT")]
    assert labels == ["2026년 기준 1판", "2026년 기준 2판"]


def test_career_round_records_version_and_old_round_keeps_it(con):
    by = main_login(con)
    first = career.open_round(con, YM, by)
    v1 = {v["std_key"]: v for v in first["versions"]}
    assert set(v1) == {"FC>SFC", "SFC>SM"} and all(v["issuer"] == standards.CAREER_ISSUER for v in v1.values())
    con.execute("""UPDATE career_promotion_criteria SET value=value+1 WHERE metric='converted_premium'
                   AND from_level_id=(SELECT level_id FROM career_level WHERE code='FC')""")
    second = career.open_round(con, YM, by)
    v2 = {v["std_key"]: v for v in second["versions"]}
    assert v2["FC>SFC"]["version_no"] == 2 and v2["SFC>SM"]["version_id"] == v1["SFC>SM"]["version_id"]
    old = career.round_detail(con, first["round"]["round_id"], everyone(con))
    assert old["round"]["status"] == "replaced"
    assert {v["version_id"] for v in old["versions"]} == {v["version_id"] for v in v1.values()}
    st = {s["issuer"]: s for s in standards.standards(con, 2026, YM)}
    fc = next(i for i in st[standards.CAREER_ISSUER]["items"] if i["std_key"] == "FC>SFC")
    assert fc["version"]["version_no"] == 2


def test_old_db_gets_tables(con):
    for t in ("career_decision", "career_round_member", "career_round", "excellence_selection", "standard_use", "standard_version"):
        con.execute(f"DROP TABLE {t}")
    standards.ensure(con)
    standards.ensure(con)
    assert con.execute("SELECT COUNT(*) FROM standard_version").fetchone()[0] == 0
    ce.run(con, 2026, YM, "test")
    assert standards.versions(con, scope="cert")


# ---------------------------------------------------------------- 승인과 선발은 메인 관리자만
def test_only_main_admin_decides(con):
    other = non_main_login(con)
    mgr_ctx = {"role": "manager", "login_id": other}
    fake_main = {"role": "main", "login_id": other}          # 역할 글자만 main 이어도 계정 표에서 다시 본다
    for by in (other, mgr_ctx, fake_main, None, "없는계정"):
        with pytest.raises(PermissionError):
            career.open_round(con, YM, by)
    detail = career.open_round(con, YM, main_login(con))
    rid, m = detail["round"]["round_id"], eligible_member(con, detail)
    for by in (other, mgr_ctx, fake_main):
        with pytest.raises(PermissionError):
            career.approve_promotion(con, rid, m["agent_id"], by)
        with pytest.raises(PermissionError):
            career.reject_promotion(con, rid, m["agent_id"], by)
        with pytest.raises(PermissionError):
            ce.select_excellence(con, "SALES_EXCELLENT", 2026, m["agent_id"], by)
    d = career.approve_promotion(con, rid, m["agent_id"], {"role": "main", "login_id": main_login(con)})
    with pytest.raises(PermissionError):
        career.revoke_decision(con, d["decision_id"], other, "사유를 적었습니다")
    assert not con.execute("SELECT 1 FROM audit_log WHERE actor=? AND action LIKE 'career_%'", (other,)).fetchone()


def test_approve_moves_level_and_revoke_restores(con):
    by = main_login(con)
    detail = career.open_round(con, YM, by)
    rid, m = detail["round"]["round_id"], eligible_member(con, detail)
    aid = m["agent_id"]
    n_hist = con.execute("SELECT COUNT(*) FROM agent_career_history").fetchone()[0]
    d = career.approve_promotion(con, rid, aid, by, note="분기 기준 충족")
    assert d["effective_ym"] == "202611"
    assert career.current_level(con, aid, YM)["level_id"] == m["from_level"]["level_id"]
    assert career.current_level(con, aid, "202611")["level_id"] == m["to_level"]["level_id"]
    with pytest.raises(ValueError):
        career.approve_promotion(con, rid, aid, by)                  # 같은 회차 두 번
    with pytest.raises(ValueError):
        career.revoke_decision(con, d["decision_id"], by, "짧음")     # 사유가 짧다
    career.revoke_decision(con, d["decision_id"], by, "기준 자료를 다시 확인합니다")
    assert career.current_level(con, aid, "202611")["level_id"] == m["from_level"]["level_id"]
    assert con.execute("SELECT COUNT(*) FROM agent_career_history").fetchone()[0] == n_hist + 1, "이력 줄은 지우지 않습니다"
    live = con.execute("SELECT COUNT(*) FROM agent_career_history WHERE agent_id=? AND effective_to IS NULL", (aid,)).fetchone()[0]
    assert live == 1
    res = career.career_results(con, [aid])
    assert res[0]["voided"] and res[0]["void_reason"]
    assert not career.career_results(con, [aid], include_voided=False)
    career.reject_promotion(con, rid, aid, by)                       # 무른 뒤 다시 결정할 수 있다
    actions = [r[0] for r in con.execute("SELECT action FROM audit_log WHERE actor=? AND action LIKE 'career_%' ORDER BY id", (by,))]
    assert actions == ["career_round_open", "career_approve", "career_revoke", "career_reject"]
    revoke = con.execute("SELECT after_json FROM audit_log WHERE action='career_revoke'").fetchone()[0]
    assert "reason_len" in json.loads(revoke) and "기준" not in revoke


def test_cannot_approve_non_eligible_or_reopen_decided_round(con):
    by = main_login(con)
    detail = career.open_round(con, YM, by)
    rid = detail["round"]["round_id"]
    weak = next(m for m in detail["members"] if not m["eligible"])
    with pytest.raises(ValueError):
        career.approve_promotion(con, rid, weak["agent_id"], by)
    career.reject_promotion(con, rid, weak["agent_id"], by)
    with pytest.raises(ValueError):
        career.open_round(con, YM, by)


def test_excellence_selection_and_revoke(con):
    by = main_login(con)
    xl = ce.excellence(con, everyone(con), viewer=MAIN)
    cand = next(r for r in xl["candidates"] if r["program_code"] == "SALES_EXCELLENT")
    loser = next(r for r in xl["rows"] if r["program_code"] == "SALES_EXCELLENT" and not r["achieved"])
    with pytest.raises(ValueError):
        ce.select_excellence(con, "SALES_EXCELLENT", 2026, loser["agent_id"], by)
    with pytest.raises(ValueError):
        ce.select_excellence(con, "MDRT", 2026, cand["agent_id"], by)
    s = ce.select_excellence(con, "SALES_EXCELLENT", 2026, cand["agent_id"], by, note="연간 선발")
    assert s["version_label"] == "2026년 기준 1판" and s["status_run_id"] == xl["run_id"]
    with pytest.raises(ValueError):
        ce.select_excellence(con, "SALES_EXCELLENT", 2026, cand["agent_id"], by)
    again = ce.excellence(con, everyone(con), viewer=MAIN)
    assert all(r["agent_id"] != cand["agent_id"] or r["program_code"] != "SALES_EXCELLENT" for r in again["candidates"])
    hist = next(h for h in again["history"] if h["agent_id"] == cand["agent_id"] and h["program_code"] == "SALES_EXCELLENT")
    assert {"year": 2026, "source": "selection"} in hist["years"]
    with pytest.raises(PermissionError):
        ce.revoke_selection(con, s["selection_id"], non_main_login(con), "사유를 적었습니다")
    ce.revoke_selection(con, s["selection_id"], by, "선발 인원을 다시 봅니다")
    assert not ce.selections(con, [cand["agent_id"]], 2026)
    assert ce.selections(con, [cand["agent_id"]], 2026, include_voided=True)[0]["voided"]
    actions = [r[0] for r in con.execute("SELECT action FROM audit_log WHERE action LIKE 'excellence_%' ORDER BY id")]
    assert actions == ["excellence_select", "excellence_revoke"]


def test_hall_of_fame_counts_live_selection(con):
    """앱에서 선발한 지난 해도 명예의전당 누적 횟수에 든다. 무르면 빠진다."""
    prog = con.execute("SELECT program_id FROM certification_program WHERE code='SALES_EXCELLENT'").fetchone()[0]
    aid = next(a for a in everyone(con) if not con.execute(
        "SELECT 1 FROM agent_certification_award WHERE agent_id=?", (a,)).fetchone() and con.execute(
        "SELECT 1 FROM agent WHERE agent_id=? AND job_type='fc'", (a,)).fetchone())

    def count():
        ce.run(con, 2026, YM, "test")
        row = next(r for r in ce.latest_status(con, [aid], with_detail=True) if r["code"] == "HALL_OF_FAME")
        return next(v["실적"] for k, v in row["detail"].items() if k.startswith("award_count"))

    before = count()
    standards.ensure(con)
    sid = con.execute("""INSERT INTO excellence_selection(program_id, year, agent_id, selected_by, selected_at)
                         VALUES(?,2025,?,'admin','2026-01-10 00:00:00')""", (prog, aid)).lastrowid
    assert count() == before + 1
    con.execute("UPDATE excellence_selection SET voided_at='2026-01-11 00:00:00', voided_by='admin', void_reason='시험' WHERE selection_id=?", (sid,))
    assert count() == before


# ---------------------------------------------------------------- 예상 표시와 D-33 기준 설명
def test_estimates_are_labelled(con):
    res = ce.cert_progress(con, everyone(con), viewer=MAIN)
    assert res["run_at"] and res["as_of_ym"] == YM and res["projected_note"]
    for r in res["rows"]:
        assert r["projected"]["is_estimate"] is True and r["projected"]["label"] == "예상"
        for m in r["metrics"]:
            if m["cumulative"]:
                assert m["year_end"]["is_estimate"] is True and m["year_end"]["label"] == standards.ESTIMATE_LABEL
            else:
                assert m["year_end"] is None and m["monthly_needed"] is None
    proj = {(r["agent_id"], r["program_code"]): r["projected"]["value"] for r in res["rows"]}
    stored = {(r["agent_id"], r["code"]): r["projected_achieved"] for r in ce.latest_status(con, everyone(con))}
    assert {k: proj[k] for k in stored if k in proj} == {k: v for k, v in stored.items() if k in proj}


def test_d33_basis_note_only_on_contract_date_metrics(con):
    res = ce.cert_progress(con, everyone(con), viewer=MAIN)
    assert res["basis"] == {"note": standards.BASIS_NOTE, "decision": "D-33", "assumed": True}
    for r in res["rows"]:
        for m in r["metrics"]:
            assert (m["basis"] is not None) == (m["metric"] in standards.D33_METRICS)
    sam = next(r for r in res["rows"] if r["program_code"] == "SAM_ACE")
    assert sam["basis"]["decision"] == "D-33"


def test_commission_hidden_except_self_and_main(con):
    rows = ce.cert_progress(con, everyone(con), viewer=None)["rows"]
    mdrt = [r for r in rows if r["program_code"] == "MDRT"]
    me, other = mdrt[0], mdrt[1]

    def comm(viewer, aid):
        r = next(x for x in ce.cert_progress(con, [aid], viewer=viewer)["rows"] if x["program_code"] == "MDRT")
        return next(m for m in r["metrics"] if m["metric"] == "annual_commission")

    assert comm(None, me["agent_id"])["hidden"] and comm(None, me["agent_id"])["actual"] is None
    assert not comm(MAIN, other["agent_id"])["hidden"]
    viewer = {"role": "manager", "agent_id": me["agent_id"]}
    assert not comm(viewer, me["agent_id"])["hidden"]
    hid = comm(viewer, other["agent_id"])
    assert hid["hidden"] and hid["actual"] is None and hid["remaining"] is None and hid["year_end"]["value"] is None
    prem = next(m for m in next(x for x in ce.cert_progress(con, [other["agent_id"]], viewer=viewer)["rows"]
                                if x["program_code"] == "MDRT")["metrics"] if m["metric"] == "annual_first_premium")
    assert not prem["hidden"] and prem["actual"] is not None
