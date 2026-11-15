"""S3 계산: 수수료·점검(G)의 웹용 함수와 조직 유지율의 설계사 목록.

수수료·점검은 D-11 제안대로(가정) 정산서 기준이다. 정산서가 없고 요율표만 있는 보험사의 금액은 예상이다.
조직 유지율은 인증과 같은 재직자 기준이라는 제안대로(가정) persist.org_members 한 곳이 목록을 정한다.
교육은 조직별 설정의 기한 임박 날짜 수와 필수 과정 추가를 소속 조직의 값으로 읽는다. 인증 표는 certification 의 읽기 함수로만 읽는다.

자료는 run_demo 의 샘플을 임시 폴더에 한 번 만들고 시험마다 복사해 쓴다. data/demo.db 는 건드리지 않는다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import json
import pathlib
import shutil
import tempfile
from datetime import date, timedelta

import pytest

from app import db
from app.engine import certification, orgs, persist, reconcile
from app.engine import education as edu
from app.engine import settle as st
from app.org import settings as org_settings

YM = "202610"


@pytest.fixture(scope="module")
def base():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="engine-s3-"))
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
    return [r[0] for r in con.execute("SELECT agent_id FROM agent")]


def raw_statement(con, ym=YM, where="1=1", params=()):
    """정산서 원장을 그대로 더한다. 살아 있는 배치만."""
    return con.execute(f"""SELECT COUNT(*) n, COALESCE(SUM(s.amount),0) net,
                                  COALESCE(SUM(CASE WHEN s.commission_type='clawback' THEN s.amount ELSE 0 END),0) claw
                           FROM commission_statement_line s JOIN import_batch b ON b.batch_id=s.batch_id
                           WHERE s.period_ym=? AND b.status IN ('active','loaded') AND {where}""", (ym, *params)).fetchone()


def non_main_login(con):
    return con.execute("SELECT login_id FROM user_account WHERE is_main_admin=0 AND status='active' LIMIT 1").fetchone()[0]


# ---------------------------------------------------------------- 정산 요약의 합
def test_statement_summary_sums_match_statement_lines(con):
    s = st.statement_summary(con, everyone(con), YM, unmapped=True)
    assert s["rows"], "정산서가 있는 보험사가 없다"
    for r in s["rows"]:
        raw = raw_statement(con, where="s.insurer_id=?", params=(r["insurer_id"],))
        assert r["has_statement"] and r["statement"]["lines"] == raw["n"], r["insurer_id"]
        assert r["statement"]["net"] == raw["net"], r["insurer_id"]
        assert r["statement"]["clawback"] == raw["claw"], r["insurer_id"]
        assert r["statement"]["paid"] + r["statement"]["clawback"] == r["statement"]["net"]
    raw = raw_statement(con)
    assert (s["total"]["lines"], s["total"]["net"]) == (raw["n"], raw["net"])
    assert s["total"]["net"] == sum(r["statement"]["net"] for r in s["rows"])
    # 자체 계산은 요율표가 있는 보험사에만 있다
    rates = st.rate_insurers(con, YM)
    for r in s["rows"]:
        assert r["has_rates"] == (r["insurer_id"] in rates)
        if not r["has_rates"]:
            assert r["calc"] is None, r["insurer_id"]


def test_summary_and_person_screens_give_same_total(con):
    """정산 요약(매핑된 줄), 사람별 합, 범위 전체의 월 수수료가 같은 숫자다."""
    ids = everyone(con)
    s = st.statement_summary(con, ids, YM)
    people = st.by_agent(con, ids, YM)
    whole = st.agent_month(con, ids, YM)
    assert s["total"]["net"] == whole["total"]["net"] == sum(p["net"] for p in people)
    assert s["total"]["lines"] == whole["total"]["lines"] == sum(p["lines"] for p in people)
    one = people[0]
    assert st.agent_month(con, [one["agent_id"]], YM)["total"]["net"] == one["net"]


def test_superseded_statement_is_not_counted_and_falls_back_to_estimate(con):
    ins = con.execute("SELECT insurer_id FROM import_batch WHERE data_type='statement' AND period_ym=? ORDER BY insurer_id LIMIT 1", (YM,)).fetchone()[0]
    who = con.execute("SELECT agent_id FROM commission_statement_line WHERE insurer_id=? AND period_ym=? AND agent_id IS NOT NULL LIMIT 1",
                      (ins, YM)).fetchone()[0]
    before = st.agent_month(con, [who], YM)
    assert not before["estimated"]
    con.execute("UPDATE import_batch SET status='superseded' WHERE data_type='statement' AND period_ym=? AND insurer_id=?", (YM, ins))
    con.commit()
    s = st.statement_summary(con, everyone(con), YM, unmapped=True)
    row = next((r for r in s["rows"] if r["insurer_id"] == ins), None)
    assert row is not None and not row["has_statement"] and row["statement"] is None
    assert s["total"]["net"] == raw_statement(con)["net"]
    after = st.agent_month(con, [who], YM)
    gone = [b for b in after["by_insurer"] if b["insurer_id"] == ins]
    assert gone and gone[0]["source"] == "calc" and gone[0]["estimated"]
    assert after["estimated"] and after["expected"]["lines"] > 0


def test_month_without_statement_is_estimate(con):
    """9월은 정산서가 없고 요율표로 계산한 줄만 있다. 전부 예상이다."""
    ids = everyone(con)
    m = st.agent_month(con, ids, "202609")
    assert m["lines"] and m["estimated"]
    assert all(r["source"] == "calc" and r["estimated"] for r in m["lines"])
    assert m["confirmed"]["lines"] == 0 and m["expected"]["net"] == m["total"]["net"]
    calc = con.execute("""SELECT COALESCE(SUM(c.amount),0) FROM commission_calc c WHERE c.run_id=? AND c.agent_id IS NOT NULL""",
                       (db.latest_run(con, "commission", "202609"),)).fetchone()[0]
    assert m["total"]["net"] == calc
    claw = st.clawbacks(con, ids, "202609")
    assert claw["total"] == m["total"]["clawback"] and all(r["commission_type"] == "clawback" for r in claw["lines"])


# ---------------------------------------------------------------- 대사 차이
def test_diff_list_matches_latest_reconciliation(con):
    d = st.diff_list(con, everyone(con), YM, unmapped=True)
    run_id = db.latest_run(con, "reconciliation", YM)
    assert d["run_id"] == run_id
    raw = con.execute("SELECT diff_id, diff_type, diff_amount, statement_amount, internal_amount, policy_no FROM reconciliation_diff WHERE run_id=?",
                      (run_id,)).fetchall()
    assert len(d["rows"]) == len(raw) > 0
    by_id = {r["diff_id"]: r for r in raw}
    for r in d["rows"]:
        src = by_id[r["diff_id"]]
        assert r["diff_type"] == src["diff_type"] and r["diff_amount"] == src["diff_amount"]
        assert r["diff_type_ko"] in st.DIFF_KO.values()
        assert r["policy_tail"] == src["policy_no"][-4:] and "policy_no" not in r
        assert r["diff_amount"] == (r["statement_amount"] or 0) - (r["internal_amount"] or 0)
        assert r["status"] == "open" and r["status_ko"] == "미처리"
    amounts = [abs(r["diff_amount"]) for r in d["rows"]]
    assert amounts == sorted(amounts, reverse=True)
    assert d["counts"]["open"] == len(raw)
    s = st.statement_summary(con, everyone(con), YM, unmapped=True)
    assert s["open_count"] == len(raw) == sum(r["diff_count"] for r in s["rows"])
    one = d["rows"][0]
    only = st.diff_list(con, everyone(con), YM, unmapped=True, insurer_id=one["insurer_id"], diff_type=one["diff_type"])
    assert only["rows"] and all(r["insurer_id"] == one["insurer_id"] and r["diff_type"] == one["diff_type"] for r in only["rows"])
    assert only["counts"] == d["counts"]


# ---------------------------------------------------------------- 범위
def test_scope_limits_every_function(con):
    ids = everyone(con)
    assert st.statement_summary(con, [], YM)["rows"] == []
    assert st.diff_list(con, [], YM)["rows"] == []
    assert st.agent_month(con, [], YM)["lines"] == []
    assert st.by_agent(con, [], YM) == []
    d = st.diff_list(con, ids, YM)
    who = next(r["agent_id"] for r in d["rows"] if r["agent_id"])
    mine = st.diff_list(con, [who], YM)["rows"]
    assert mine and all(r["agent_id"] == who for r in mine)
    others = [a for a in ids if a != who]
    assert all(r["agent_id"] != who for r in st.diff_list(con, others, YM)["rows"])
    m = st.agent_month(con, [who], YM)
    assert m["lines"] and all(r["agent_id"] == who for r in m["lines"])
    assert all("name" not in r and "policy_no" not in r for r in m["lines"])


def test_unmapped_lines_only_with_unmapped(con):
    line = con.execute("SELECT line_id, insurer_id, amount FROM commission_statement_line WHERE period_ym=? AND agent_id IS NOT NULL LIMIT 1",
                       (YM,)).fetchone()
    con.execute("UPDATE commission_statement_line SET agent_id=NULL WHERE line_id=?", (line["line_id"],))
    con.commit()
    ids = everyone(con)
    with_u = st.statement_summary(con, ids, YM, unmapped=True)
    without = st.statement_summary(con, ids, YM)
    assert with_u["total"]["lines"] - without["total"]["lines"] == raw_statement(con, where="s.agent_id IS NULL")["n"] >= 1
    assert without["total"]["net"] == raw_statement(con, where="s.agent_id IS NOT NULL")["net"]
    assert st.agent_month(con, ids, YM)["total"]["lines"] == without["total"]["lines"]


# ---------------------------------------------------------------- 차이 처리 이력
def test_diff_status_history_and_rules(con):
    ids = everyone(con)
    d = st.diff_list(con, ids, YM, unmapped=True)["rows"][0]
    with pytest.raises(PermissionError):
        st.set_diff_status(con, d["diff_id"], "checked", "메모", non_main_login(con))
    with pytest.raises(PermissionError):
        st.set_diff_status(con, d["diff_id"], "checked", "메모", {"role": "manager", "login_id": "admin"})
    with pytest.raises(ValueError):
        st.set_diff_status(con, d["diff_id"], "checked", "  ", "admin")
    with pytest.raises(ValueError):
        st.set_diff_status(con, d["diff_id"], "confirmed", "메모", "admin")
    assert st.diff_history(con, d["diff_id"]) == []

    st.set_diff_status(con, d["diff_id"], "checked", "정산서 확인", "admin")
    st.set_diff_status(con, d["diff_id"], "asked", "보험사에 메일 문의", {"role": "main", "login_id": "admin"})
    h = st.diff_history(con, d["diff_id"])
    assert [x["status"] for x in h] == ["checked", "asked"] and h[-1]["status_ko"] == "보험사 문의"
    now = next(r for r in st.diff_list(con, ids, YM, unmapped=True)["rows"] if r["diff_id"] == d["diff_id"])
    assert now["status"] == "asked" and now["note"] == "보험사에 메일 문의" and now["acted_by"] == "admin" and not now["changed"]
    assert now["legacy_status"] == "open"                       # 기존 도구의 상태는 건드리지 않는다
    counts = st.diff_list(con, ids, YM, unmapped=True)["counts"]
    assert counts["asked"] == 1 and counts["open"] == len(st.diff_list(con, ids, YM, unmapped=True)["rows"]) - 1
    assert len(st.diff_list(con, ids, YM, unmapped=True, status="asked")["rows"]) == 1
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='diff_update' AND target=?", (f"diff:{d['diff_id']}",)).fetchone()[0] == 2


def test_diff_status_survives_rerun_and_flags_changed_amount(con):
    ids = everyone(con)
    d = next(r for r in st.diff_list(con, ids, YM, unmapped=True)["rows"] if r["statement_amount"] is not None and r["internal_amount"] is not None)
    st.set_diff_status(con, d["diff_id"], "resolved", "요율 공문 확인", "admin")
    line_id = con.execute("SELECT statement_line_id FROM reconciliation_diff WHERE diff_id=?", (d["diff_id"],)).fetchone()[0]
    con.execute("UPDATE commission_statement_line SET amount=amount+1000 WHERE line_id=?", (line_id,))
    con.commit()
    reconcile.run(con, YM)
    with pytest.raises(ValueError):                              # 지난 대사의 차이는 받지 않는다
        st.set_diff_status(con, d["diff_id"], "checked", "메모", "admin")
    new = next(r for r in st.diff_list(con, ids, YM, unmapped=True)["rows"]
               if (r["insurer_id"], r["policy_tail"], r["commission_type"], r["installment_no"])
               == (d["insurer_id"], d["policy_tail"], d["commission_type"], d["installment_no"]))
    assert new["diff_id"] != d["diff_id"] and new["status"] == "resolved" and new["changed"]
    assert [x["diff_id"] for x in st.diff_history(con, new["diff_id"])] == [d["diff_id"]]
    st.set_diff_status(con, new["diff_id"], "checked", "바뀐 금액 다시 확인", "admin")
    again = next(r for r in st.diff_list(con, ids, YM, unmapped=True)["rows"] if r["diff_id"] == new["diff_id"])
    assert again["status"] == "checked" and not again["changed"]
    assert len(st.diff_history(con, d["diff_id"])) == 2


def test_closed_month_refuses_status(con):
    d = st.diff_list(con, everyone(con), YM, unmapped=True)["rows"][0]
    con.execute("INSERT INTO close_period(period_ym, status, closed_at, closed_by) VALUES(?, 'closed', ?, 'admin')", (YM, db.now()))
    con.commit()
    with pytest.raises(ValueError):
        st.set_diff_status(con, d["diff_id"], "checked", "메모", "admin")


def test_ensure_creates_table_on_old_db(con):
    con.execute("DROP TABLE recon_diff_action")
    con.commit()
    d = st.diff_list(con, everyone(con), YM, unmapped=True)
    assert d["rows"] and con.execute("SELECT name FROM sqlite_master WHERE name='recon_diff_action'").fetchone()


# ---------------------------------------------------------------- 유지율 목록 규칙
def _leave_mid_month(con, agent_id, ym=YM):
    """사원현황 동기화처럼 그 달 중간에 퇴사시키고 소속 이력을 그 달에서 닫는다."""
    con.execute("UPDATE agent SET status='terminated', termination_date=? WHERE agent_id=?", (f"{ym[:4]}-{ym[4:]}-15", agent_id))
    con.execute("UPDATE agent_org_history SET effective_to=? WHERE agent_id=? AND effective_to IS NULL", (ym, agent_id))
    con.commit()


def _history_people(con, org_id, ym):
    under = sorted(orgs.descendants(con, org_id))
    q = ",".join("?" * len(under))
    return {r[0] for r in con.execute(f"""SELECT DISTINCT agent_id FROM agent_org_history WHERE org_id IN ({q})
                                           AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)""", (*under, ym, ym))}


def test_org_persistency_members_exclude_mid_month_leaver(con):
    org = con.execute("SELECT org_id FROM org_unit WHERE level='branch' ORDER BY org_id LIMIT 1").fetchone()[0]
    before = persist.org_members(con, org, YM)
    leaver = next(a for a in before if persist.rate(con, [a], "p13", YM)[2])
    _leave_mid_month(con, leaver)
    got = persist.org_members(con, org, YM)
    assert leaver not in got and leaver in _history_people(con, org, YM)    # 소속 이력에는 있지만 유지율 목록에서는 빠진다
    assert got == orgs.members(con, org, YM) == certification.org_members(con, org, YM)
    jobs = {r[0]: (r[1], r[2]) for r in con.execute("SELECT agent_id, job_type, status FROM agent")}
    assert all(jobs[a] == ("fc", "active") for a in got)
    legacy = persist.rate(con, orgs.members(con, org, YM), "p13", YM)
    assert persist.rate(con, got, "p13", YM) == legacy


def test_org_persistency_members_respect_scope(con):
    org = con.execute("SELECT org_id FROM org_unit WHERE level='hq' ORDER BY org_id LIMIT 1").fetchone()[0]
    full = persist.org_members(con, org, YM)
    assert len(full) > 2
    part = full[: len(full) // 2]
    outsider = con.execute("SELECT agent_id FROM agent WHERE agent_id NOT IN (%s) LIMIT 1" % ",".join("?" * len(full)), full).fetchone()[0]
    assert persist.org_members(con, org, YM, agent_ids=part + [outsider]) == part
    assert persist.org_members(con, org, YM, agent_ids=[]) == []


# ---------------------------------------------------------------- 교육과 조직별 설정
EDU_DAY = date(2026, 10, 15)


def _put_setting(con, org_id, key, value, from_ym="202001"):
    """엔진 시험이라 권한 검사 없이 설정 줄을 바로 넣는다. 줄은 더하기만 한다."""
    org_settings.ensure(con)
    con.execute("INSERT INTO org_setting(org_id, key, value, from_ym, set_by, set_at, note) VALUES(?,?,?,?,?,?,?)",
                (org_id, key, json.dumps(value), from_ym, "test", org_settings._stamp(), None))
    con.commit()


def _new_course(con, code, required, deadline_date=None):
    return edu.save_course(con, by="test", code=code, name=f"시험 과정 {code}", kind="product", required=required, target_role="all",
                           deadline_rule="fixed" if deadline_date else "none", deadline_date=deadline_date)


def _up(con, org_id, level):
    info = {r["org_id"]: (r["level"], r["parent_id"]) for r in con.execute("SELECT org_id, level, parent_id FROM org_unit")}
    while org_id is not None and info[org_id][0] != level:
        org_id = info[org_id][1]
    return org_id


def _people_in(con, ref=EDU_DAY):
    """재직 중이고 소속이 있는 사람. agent_id 와 기준일 소속."""
    return [p for p in edu._people(con, everyone(con), ref) if not p["left"] and p["org_id"] is not None]


def _two_branches(con):
    """같은 본부 아래 서로 다른 지점의 두 사람과, 다른 본부의 한 사람. 없으면 None 자리."""
    people = _people_in(con)
    by = {}
    for p in people:
        by.setdefault((_up(con, p["org_id"], "hq"), _up(con, p["org_id"], "branch")), p)
    keys = [k for k in by if k[0] is not None and k[1] is not None]
    for k1 in keys:
        for k2 in keys:
            if k1[0] == k2[0] and k1[1] != k2[1]:
                other = next((by[k] for k in by if k[0] not in (None, k1[0])), None)
                return k1, by[k1], k2, by[k2], other
    pytest.skip("샘플에 같은 본부 아래 지점이 둘인 사람이 없다")


def _cell(rows, agent_id, course_id):
    p = next(r for r in rows if r["agent_id"] == agent_id)
    return next((x for x in p["courses"] if x["course_id"] == course_id), None)


def test_soon_days_follow_each_persons_org(con):
    (hq, br_a), a, (_, br_b), b, _ = _two_branches(con)
    cid = _new_course(con, "T-SOON", True, (EDU_DAY + timedelta(days=10)).isoformat())
    ids = [a["agent_id"], b["agent_id"]]
    _put_setting(con, br_a, edu.SOON_KEY, 14)                       # 지점 A 만 14일. 지점 B 는 기본값 7일
    rows = edu.person_status(con, ids, EDU_DAY)
    ca, cb = _cell(rows, a["agent_id"], cid), _cell(rows, b["agent_id"], cid)
    assert (ca["state"], ca["soon_days"]) == ("soon", 14)
    assert (cb["state"], cb["soon_days"]) == ("open", edu.SOON_DAYS)
    assert [r["agent_id"] for r in edu.due_soon(con, ids, EDU_DAY) if r["course_id"] == cid] == [a["agent_id"]]
    p = edu.org_progress(con, ids, EDU_DAY)
    assert next(c for c in p["courses"] if c["course_id"] == cid)["soon"] == 1
    assert edu.my_education(con, a["agent_id"], [a["agent_id"]], EDU_DAY)["soon_days"] == 14
    # 위 조직의 값은 아래로 내려가고, 넘긴 soon_days 는 모두에게 그 값이다
    _put_setting(con, hq, edu.SOON_KEY, 12)
    assert _cell(edu.person_status(con, ids, EDU_DAY), b["agent_id"], cid)["state"] == "soon"
    fixed = edu.person_status(con, ids, EDU_DAY, soon_days=5)
    assert {_cell(fixed, i, cid)["state"] for i in ids} == {"open"}
    # 설정이 시작하기 전 달을 계산하면 그때 값을 쓴다
    _put_setting(con, br_b, edu.SOON_KEY, 3, from_ym="203001")
    assert _cell(edu.person_status(con, ids, EDU_DAY), b["agent_id"], cid)["soon_days"] == 12


def test_old_db_without_settings_table_keeps_old_behavior(con):
    (_, br_a), a, _, b, _ = _two_branches(con)
    cid = _new_course(con, "T-OLD", True, (EDU_DAY + timedelta(days=10)).isoformat())
    extra = _new_course(con, "T-OLDX", False)
    ids = [a["agent_id"], b["agent_id"]]
    before = edu.person_status(con, ids, EDU_DAY, soon_days=edu.SOON_DAYS)
    _put_setting(con, br_a, edu.SOON_KEY, 14)
    _put_setting(con, br_a, edu.EXTRA_KEY, [extra])
    assert _cell(edu.person_status(con, ids, EDU_DAY), a["agent_id"], extra) is not None
    con.execute("DROP TABLE org_setting")
    con.execute("DROP TABLE org_setting_lock")
    con.commit()
    after = edu.person_status(con, ids, EDU_DAY)
    assert after == before
    assert _cell(after, a["agent_id"], cid)["soon_days"] == edu.SOON_DAYS
    assert all(_cell(after, i, extra) is None for i in ids)


def test_extra_required_is_inherited_and_cannot_be_removed(con):
    (hq, br_a), a, (_, br_b), b, other = _two_branches(con)
    c_hq, c_br, c_none = _new_course(con, "T-XHQ", False), _new_course(con, "T-XBR", False), _new_course(con, "T-XNO", False)
    ids = [a["agent_id"], b["agent_id"]] + ([other["agent_id"]] if other else [])
    _put_setting(con, hq, edu.EXTRA_KEY, [c_hq])
    _put_setting(con, br_a, edu.EXTRA_KEY, [c_br])
    _put_setting(con, br_b, edu.EXTRA_KEY, [])                        # 아래에서 비워도 위의 것은 남는다
    rows = edu.person_status(con, ids, EDU_DAY)
    got = {i: {x["course_id"] for x in next(r for r in rows if r["agent_id"] == i)["courses"]} & {c_hq, c_br, c_none} for i in ids}
    assert got[a["agent_id"]] == {c_hq, c_br}
    assert got[b["agent_id"]] == {c_hq}
    if other:
        assert got[other["agent_id"]] == set()
    x = _cell(rows, a["agent_id"], c_br)
    assert x["required"] is True and x["required_by"] == "org"
    base = edu.person_status(con, [b["agent_id"]], EDU_DAY)[0]
    assert base["required_total"] == sum(1 for y in base["courses"] if y["required"])
    # 조직 진척도와 미수료자 목록도 같은 판정이다. 과정마다 대상은 그 과정을 더한 조직 아래 사람만이다
    p = edu.org_progress(con, ids, EDU_DAY)
    targets = {c["course_id"]: c["targets"] for c in p["courses"]}
    assert targets[c_hq] == len([i for i in ids if c_hq in got[i]]) and targets[c_br] == 1 and c_none not in targets
    nd = {(r["agent_id"], r["course_id"]) for r in edu.not_done(con, ids, EDU_DAY)}
    assert (a["agent_id"], c_br) in nd and (b["agent_id"], c_br) not in nd
    # 수료하면 진척에 든다
    edu.record_completion(con, agent_id=a["agent_id"], course_id=c_br, by="test", agent_ids=None, completed_on=EDU_DAY.isoformat())
    assert _cell(edu.person_status(con, ids, EDU_DAY), a["agent_id"], c_br)["state"] == "done"
    # 필수가 아닌 과정까지 볼 때 더하지 않은 사람에게는 선택 과정이다
    allc = edu.person_status(con, [b["agent_id"]], EDU_DAY, required_only=False)
    y = _cell(allc, b["agent_id"], c_br)
    assert y is not None and y["required"] is False and y["required_by"] is None


# ---------------------------------------------------------------- 인증 읽기 함수
def test_certification_readers_stay_in_scope(con):
    holders = [r[0] for r in con.execute("SELECT DISTINCT agent_id FROM agent_certification_award ORDER BY agent_id")]
    assert holders, "샘플에 인증 확정 기록이 없다"
    one = holders[0]
    got = certification.awards(con, [one])
    assert got and {r["agent_id"] for r in got} == {one}
    n = con.execute("SELECT COUNT(*) FROM agent_certification_award WHERE agent_id=?", (one,)).fetchone()[0]
    assert len(got) == n
    assert {"code", "name", "category", "year", "awarded_at"} <= set(got[0])
    assert certification.awards(con, []) == [] and certification.awards(con, None) == []
    y = got[0]["year"]
    assert all(r["year"] == y for r in certification.awards(con, holders, year=y))
    assert len(certification.awards(con, everyone(con))) == con.execute("SELECT COUNT(*) FROM agent_certification_award").fetchone()[0]

    run_id = certification.latest_run(con)
    assert run_id is not None, "샘플에 인증 계산이 없다"
    scope = everyone(con)[:5]
    rows = certification.latest_status(con, scope)
    assert {r["agent_id"] for r in rows} <= set(scope) and {r["run_id"] for r in rows} <= {run_id}
    n = con.execute(f"SELECT COUNT(*) FROM agent_certification_status WHERE run_id=? AND agent_id IN ({','.join('?' * len(scope))})",
                    (run_id, *scope)).fetchone()[0]
    assert len(rows) == n
    assert all("detail" not in r for r in rows)
    ach = certification.latest_status(con, everyone(con), achieved_only=True)
    assert all(r["achieved"] is True for r in ach)
    assert certification.latest_status(con, []) == []
    assert certification.latest_status(con, scope, as_of_ym="190001") == []


# ---------------------------------------------------------------- 월 마감
def _close_state(con, ym=YM):
    r = con.execute("SELECT status, closed_at, closed_by FROM close_period WHERE period_ym=?", (ym,)).fetchone()
    return dict(r) if r else None


def _clear_blocks(con, keep_open=0, asked=0):
    """막는 조건을 푼다. 품질 문제와 시상 공문은 기존 도구에서 처리했다고 보고, 대사 차이는 웹 처리 상태로 처리한다.
    keep_open 건은 미처리로 두고, 그다음 asked 건은 보험사 문의로 둔다."""
    con.execute("UPDATE quality_issue SET resolved=1 WHERE severity='block'")
    con.execute("UPDATE incentive_source_doc SET extraction_status='reviewed' WHERE extraction_status='extracted'")
    con.commit()
    rows = st.diff_list(con, everyone(con), YM, unmapped=True)["rows"]
    assert len(rows) > keep_open + asked, "샘플에 대사 차이가 모자라다"
    for i, r in enumerate(rows):
        if i < keep_open:
            continue
        st.set_diff_status(con, r["diff_id"], "asked" if i < keep_open + asked else "checked", "확인", "admin")


def test_close_blocks(con):
    check = st.close_check(con, YM)
    assert any("처리하지 않은 대사 차이" in b for b in check["block"])
    with pytest.raises(ValueError):
        st.close_month(con, YM, "admin")
    assert _close_state(con) is None and con.execute("SELECT COUNT(*) FROM audit_log WHERE action='close'").fetchone()[0] == 0

    # 미처리 차이가 하나만 남아도 막는다
    _clear_blocks(con, keep_open=1)
    check = st.close_check(con, YM)
    assert len(check["block"]) == 1 and check["counts"]["open"] == 1
    with pytest.raises(ValueError):
        st.close_month(con, YM, "admin")

    # 차단 등급 품질 문제와 검토 대기 시상 공문도 막는다
    _clear_blocks(con)
    assert st.close_check(con, YM)["block"] == []
    batch = con.execute("SELECT batch_id FROM import_batch WHERE period_ym=? AND status<>'superseded' LIMIT 1", (YM,)).fetchone()[0]
    con.execute("INSERT INTO quality_issue(batch_id, severity, check_code, message) VALUES(?, 'block', 'missing_column', '시험')", (batch,))
    con.commit()
    assert [b for b in st.close_check(con, YM)["block"] if "품질 문제 1건" in b]
    with pytest.raises(ValueError):
        st.close_month(con, YM, "admin")
    con.execute("UPDATE quality_issue SET resolved=1")
    con.execute("""INSERT INTO incentive_source_doc(period_ym, title, file_path, file_hash, received_at, extraction_status)
                   VALUES(?, '시험', 'raw/test.pdf', 'test-close-hash', ?, 'extracted')""", (YM, db.now()))
    con.commit()
    assert [b for b in st.close_check(con, YM)["block"] if "시상 공문 1건" in b]
    with pytest.raises(ValueError):
        st.close_month(con, YM, "admin")
    con.execute("UPDATE incentive_source_doc SET extraction_status='reviewed' WHERE file_hash='test-close-hash'")
    con.commit()
    assert st.close_check(con, YM)["block"] == []

    # 대사를 돌린 뒤에 들어온 정산서가 있으면 막는다
    ran_at = st.close_check(con, YM)["ran_at"]
    con.execute("""UPDATE import_batch SET loaded_at=? WHERE batch_id=(SELECT MIN(batch_id) FROM import_batch
                   WHERE data_type='statement' AND period_ym=? AND status IN ('active','loaded'))""", (ran_at + "Z", YM))
    con.commit()
    assert any("대사를 돌린 뒤에 들어온 정산서" in b for b in st.close_check(con, YM)["block"])
    with pytest.raises(ValueError):
        st.close_month(con, YM, "admin")
    assert _close_state(con) is None


def test_close_warns_but_does_not_block(con):
    _clear_blocks(con, asked=1)
    check = st.close_check(con, YM)
    assert check["block"] == [] and check["counts"]["asked"] == 1
    assert any("보험사에 문의 중인 차이" in w for w in check["warn"])
    # 기존 도구의 reconciliation_diff.status 는 막지 않고 참고 건수로만 센다
    assert check["legacy_open"] == check["diff_total"] > 0
    done = st.close_month(con, YM, "admin")
    assert done["status"] == "closed" and done["closed_by"] == "admin" and done["warn"] == check["warn"]
    assert _close_state(con)["status"] == "closed"
    log = con.execute("SELECT actor, after_json FROM audit_log WHERE action='close' AND target=?", (YM,)).fetchall()
    assert len(log) == 1 and log[0]["actor"] == "admin" and json.loads(log[0]["after_json"]) == {"via": "web"}
    with pytest.raises(ValueError):                              # 두 번 마감하지 않는다
        st.close_month(con, YM, "admin")
    # 정산서가 없는 보험사는 예상으로 남는다고 알린다
    missing = st.rate_insurers(con, YM) - set(st.statement_insurers(con, YM))
    assert bool(missing) == any("예상 금액" in w for w in check["warn"])


def test_reopen_needs_reason(con):
    with pytest.raises(ValueError):                              # 마감하지 않은 달
        st.reopen_month(con, YM, "admin", "사유")
    _clear_blocks(con)
    st.close_month(con, YM, "admin")
    before = _close_state(con)
    for bad in ("", "   ", None, "가" * (st.REASON_MAX + 1)):
        with pytest.raises(ValueError):
            st.reopen_month(con, YM, "admin", bad)
    assert _close_state(con)["status"] == "closed"
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='close_reopen'").fetchone()[0] == 0
    out = st.reopen_month(con, YM, {"role": "main", "login_id": "admin"}, "  보험사 정산서 재수신 ")
    assert out["status"] == "open" and out["reason"] == "보험사 정산서 재수신"
    after = _close_state(con)
    assert after["status"] == "open" and (after["closed_at"], after["closed_by"]) == (before["closed_at"], before["closed_by"])
    row = con.execute("SELECT actor, after_json FROM audit_log WHERE action='close_reopen' AND target=?", (YM,)).fetchone()
    assert row["actor"] == "admin" and json.loads(row["after_json"])["reason"] == "보험사 정산서 재수신"
    st.close_month(con, YM, "admin")                             # 해제한 달은 다시 마감할 수 있다
    assert _close_state(con)["status"] == "closed"


def test_close_and_reopen_are_main_only(con):
    _clear_blocks(con)
    other = non_main_login(con)
    for by in (other, {"role": "manager", "login_id": "admin"}, {"role": "main", "login_id": other}, None, "no-such-login"):
        with pytest.raises(PermissionError):
            st.close_month(con, YM, by)
    assert _close_state(con) is None
    st.close_month(con, YM, "admin")
    for by in (other, {"role": "manager", "login_id": "admin"}, "ui"):
        with pytest.raises(PermissionError):
            st.reopen_month(con, YM, by, "사유")
    assert _close_state(con)["status"] == "closed"
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('close','close_reopen')").fetchone()[0] == 1


def test_legacy_tool_does_not_close(con, tmp_path, monkeypatch):
    """기존 도구(app/ui.py)의 마감 메뉴는 막는 조건이 없어도 close_period 와 audit_log 에 쓰지 않는다. 마감은 웹에서만 한다(총괄 결정, 가정)."""
    from streamlit.testing.v1 import AppTest
    _clear_blocks(con)
    assert st.close_check(con, YM)["block"] == [], "조건이 풀린 상태에서 막혀야 시험이 된다"
    real = db.connect
    path = tmp_path / "t.db"
    monkeypatch.setattr(db, "connect", lambda *a, **k: real(path))
    ui = pathlib.Path(__file__).resolve().parent.parent / "app" / "ui.py"
    at = AppTest.from_file(str(ui), default_timeout=60)
    at.run()
    at.sidebar.radio[0].set_value("마감").run()
    at.sidebar.selectbox[0].set_value(YM).run()
    assert not at.exception, "기존 도구의 마감 메뉴가 열리지 않는다"
    assert any("/settle/close" in w.value and "메인 관리자" in w.value for w in at.warning)
    btn = next(b for b in at.button if b.label == "마감 실행")
    assert btn.disabled, "기존 도구의 마감 단추가 눌린다"     # 눌리지 않는 단추라 브라우저에서도 누를 수 없다
    at.run()
    assert not at.exception
    assert con.execute("SELECT COUNT(*) FROM close_period").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('close','close_reopen')").fetchone()[0] == 0
    import streamlit
    streamlit.cache_resource.clear()
