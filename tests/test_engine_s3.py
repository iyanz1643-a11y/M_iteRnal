"""S3 계산: 수수료·점검(G)의 웹용 함수와 조직 유지율의 설계사 목록.

수수료·점검은 D-11 제안대로(가정) 정산서 기준이다. 정산서가 없고 요율표만 있는 보험사의 금액은 예상이다.
조직 유지율은 인증과 같은 재직자 기준이라는 제안대로(가정) persist.org_members 한 곳이 목록을 정한다.

자료는 run_demo 의 샘플을 임시 폴더에 한 번 만들고 시험마다 복사해 쓴다. data/demo.db 는 건드리지 않는다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import pathlib
import shutil
import tempfile

import pytest

from app import db
from app.engine import certification, orgs, persist, reconcile
from app.engine import settle as st

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
