"""인별실적(사람별 기간 실적 집계) 적재기 시험. 가짜 자료로만 돈다."""
import json
import pathlib
import tempfile

import openpyxl
import pytest

from app import db, masters
from app.engine import performance
from app.loader import eternal_contract as ec
from app.loader import fake_eternal as fe
from app.loader import fake_person_perf as fp
from app.loader import person_perf as pp
from app.loader.excel_loader import DuplicateFile
from app.loader.sheet import HeaderMismatch

START, END = "202603", "202605"


@pytest.fixture()
def env():
    tmp = pathlib.Path(tempfile.mkdtemp())
    con = db.init_db(tmp / "t.db")
    for i, (code, name, elig) in enumerate(masters.CATEGORIES):
        con.execute("INSERT INTO product_category(code, name, incentive_eligible, sort_order) VALUES(?,?,?,?)", (code, name, elig, i))
    masters.seed_real_insurers(con)
    roster = fe.fake_roster()
    for a in roster[:-3]:                                  # 뒤의 셋은 설계사 마스터에 없는 사람이다
        con.execute("INSERT INTO agent(internal_code, name) VALUES(?,?)", (a["code"], a["name"]))
    con.commit()
    people = fp.fake_people(roster)
    path = fp.write_person_perf(tmp / "202603-05_인별실적.xlsx", people)
    yield {"con": con, "tmp": tmp, "roster": roster, "people": people, "path": path}
    con.close()


def _ids(con):
    return [r[0] for r in con.execute("SELECT agent_id FROM agent")]


def _codes(con, batch_id):
    return {r[0] for r in con.execute("SELECT check_code FROM quality_issue WHERE batch_id=?", (batch_id,))}


# ------------------------------------------------------------------ 기간
def test_guess_period_from_file_name():
    assert pp.guess_period("data/real/202603-05_인별실적.xlsx") == ("202603", "202605")
    assert pp.guess_period("202606-08_인별실적.xlsx") == ("202606", "202608")
    assert pp.guess_period("x_202611-202702.xlsx") == ("202611", "202702")
    assert pp.guess_period("인별실적.xlsx") is None


# ------------------------------------------------------------------ 건수와 합계
def test_counts_and_sums_match_source_and_total_row_is_not_a_person(env):
    con, people = env["con"], env["people"]
    s = pp.load_person_perf(con, env["path"], START, END)
    assert s["block"] == 0 and s["sums_match"] is True
    assert s["file_rows"] == s["loaded"] == len(people) and s["summary_rows"] == 1
    assert s["summary_matches"] == {"total_count": True, "total_monthly_premium_amt": True}
    n, c, a = con.execute("SELECT COUNT(*), SUM(total_count), SUM(total_monthly_premium_amt) FROM agent_period_perf").fetchone()
    assert n == len(people)
    assert c == sum(p["values"][("total", "건수")] for p in people)
    assert a == sum(p["values"][("total", "월납보험료")] for p in people)
    ins = con.execute("SELECT SUM(policy_count), SUM(monthly_premium_amt) FROM agent_period_perf_insurer").fetchone()
    assert tuple(ins) == (c, a)
    assert s["insurer_sum_mismatch"] == 0
    b = con.execute("SELECT data_type, status, period_ym, row_count FROM import_batch WHERE batch_id=?", (s["batch_id"],)).fetchone()
    assert tuple(b) == ("person_perf", "active", END, len(people))
    # 원본 행은 합계 행까지 남긴다
    assert con.execute("SELECT COUNT(*) FROM raw_row WHERE batch_id=?", (s["batch_id"],)).fetchone()[0] == len(people) + 1


def test_tiers_and_count_amount_columns(env):
    con, people = env["con"], env["people"]
    s = pp.load_person_perf(con, env["path"], START, END)
    want = {"none": 0, "some": 0, "active_10": 0, "elite_50": 0}
    for p in people:
        v = p["values"]
        t = "elite_50" if v[("정예(50만이상)", "건수")] else "active_10" if v[("유실적(10만이상)", "건수")] else "some" if v[("total", "건수")] else "none"
        want[t] += 1
    assert {k: s["tiers"].get(k, 0) for k in want} == want
    cols = [r[1] for r in con.execute("PRAGMA table_info(agent_period_perf)")]
    measures = [c for c in cols if c.startswith(("total_", "active_", "elite_", "single_", "auto_", "general_"))]
    assert measures and all(c.endswith(("_count", "_amt")) for c in measures)          # 건수인지 금액인지 칸 이름에 드러난다
    r = con.execute("SELECT active_small_count, auto_global_perf_amt FROM agent_period_perf WHERE employee_no=?", (people[0]["사원번호"],)).fetchone()
    assert r[0] is None                                    # 실제 파일처럼 빈 묶음은 0 이 아니라 빈 값이다
    assert r[1] == people[0]["values"][("자동차", "글로벌성적")]


def test_names_are_not_stored(env):
    con, people = env["con"], env["people"]
    s = pp.load_person_perf(con, env["path"], START, END)
    cols = [r[1] for r in con.execute("PRAGMA table_info(agent_period_perf)")]
    assert not any("name" in c for c in cols)
    text = "\n".join(json.dumps(list(r)) for r in con.execute("SELECT * FROM agent_period_perf"))
    text += "\n".join(r[0] for r in con.execute("SELECT raw_json FROM raw_row WHERE batch_id=?", (s["batch_id"],)))
    text += "\n".join(r[0] for r in con.execute("SELECT message FROM quality_issue WHERE batch_id=?", (s["batch_id"],)))
    for p in people:
        assert p["사원명"] not in text
    assert "사원명" not in text


def test_unknown_agent_and_name_mismatch_warn_without_names(env):
    con, roster = env["con"], env["roster"]
    con.execute("UPDATE agent SET name='바뀐이름' WHERE internal_code=?", (roster[0]["code"],))
    con.commit()
    s = pp.load_person_perf(con, env["path"], START, END)
    assert s["unmapped_agents"] == 3 and s["matched_agents"] == len(roster) - 3 and s["name_mismatch"] == 1
    assert con.execute("SELECT COUNT(*) FROM agent_period_perf WHERE agent_id IS NULL").fetchone()[0] == 3
    msgs = [r[0] for r in con.execute("SELECT message FROM quality_issue WHERE batch_id=?", (s["batch_id"],))]
    assert any(roster[-1]["code"] in m for m in msgs)
    for a in roster:
        assert not any(a["name"] in m for m in msgs)


# ------------------------------------------------------------------ 같은 파일, 다시 넣기
def test_same_file_twice_is_blocked_and_new_file_supersedes(env):
    con, people = env["con"], env["people"]
    s1 = pp.load_person_perf(con, env["path"], START, END)
    with pytest.raises(DuplicateFile):
        pp.load_person_perf(con, env["path"], START, END)
    changed = [dict(p) for p in people[:-1]]
    p2 = fp.write_person_perf(env["tmp"] / "202603-05_인별실적_v2.xlsx", changed)
    s2 = pp.load_person_perf(con, p2, START, END)
    assert s2["superseded"] == 1
    st = {r[0]: r[1] for r in con.execute("SELECT batch_id, status FROM import_batch")}
    assert st == {s1["batch_id"]: "superseded", s2["batch_id"]: "active"}
    assert con.execute("SELECT COUNT(*) FROM v_agent_period_perf").fetchone()[0] == len(changed)
    assert con.execute("SELECT COUNT(*) FROM agent_period_perf").fetchone()[0] == len(people) + len(changed)   # 이전 배치는 지우지 않는다
    assert pp.periods(con) == [(START, END)]


# ------------------------------------------------------------------ 머리글 검사
@pytest.mark.parametrize("kw, expect", [
    ({"rename": {"자동차": "자동차보험"}}, "자동차"),
    ({"rename": {"사원번호": "사번"}}, "필수 열 없음"),
    ({"rename": {"직할지점": "센터"}}, "매핑에 없는 열"),
    ({"extra_groups": [("월간Total2", ["건수"])]}, "Total"),
    ({"extra_groups": [("특별묶음", ["건수", "환산"])]}, "모르는 묶음"),
    ({"sheet": "인별2"}, "시트 '인별'"),
])
def test_changed_header_is_blocked_before_load(env, kw, expect):
    con = env["con"]
    path = fp.write_person_perf(env["tmp"] / "bad.xlsx", env["people"], **kw)
    assert pp.preview(path, START, END)["ok"] is False
    with pytest.raises(HeaderMismatch) as e:
        pp.load_person_perf(con, path, START, END)
    assert expect in str(e.value)
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM agent_period_perf").fetchone()[0] == 0


def test_extra_sub_in_total_group_is_blocked(env):
    path = fp.write_person_perf(env["tmp"] / "bad_sub.xlsx", env["people"])
    wb = openpyxl.load_workbook(path)
    ws = wb["인별"]
    col = [c.value for c in ws[2]].index("글로벌성적") + 1          # 첫 글로벌성적은 Total 묶음의 칸이다
    ws.cell(row=2, column=col, value="환산성적")
    wb.save(path)
    with pytest.raises(HeaderMismatch) as e:
        pp.load_person_perf(env["con"], path, START, END)
    assert "환산성적" in str(e.value) and "글로벌성적" in str(e.value)


def test_column_order_does_not_matter_and_three_month_label_is_read(env):
    """정리 시트처럼 사람 칸 순서가 다르고 Total 묶음 이름에 기간이 붙어도 읽는다."""
    path = fp.write_person_perf(env["tmp"] / "order.xlsx", env["people"], total_label="2026년03~05월간Total",
                                person_cols=["No.", "사원번호", "사원명", "직위", "총괄", "본부", "지점", "직할지점"], work_sheets=False)
    s = pp.load_person_perf(env["con"], path, START, END)
    assert s["block"] == 0 and s["loaded"] == len(env["people"])
    assert env["con"].execute("SELECT COUNT(*) FROM agent_period_perf WHERE org_team IS NOT NULL").fetchone()[0] == 0


def test_new_and_missing_insurer_warn_but_load(env):
    ins = [x for x in fp.INSURERS if x != "동양생명"] + ["가상신생명"]
    people = [dict(p, insurers={("가상신생명" if k == "동양생명" else k): v for k, v in p["insurers"].items()}) for p in env["people"]]
    path = fp.write_person_perf(env["tmp"] / "ins.xlsx", people, insurers=ins, work_sheets=False)
    s = pp.load_person_perf(env["con"], path, START, END)
    assert s["block"] == 0 and s["sums_match"]
    msgs = " ".join(r[0] for r in env["con"].execute("SELECT message FROM quality_issue WHERE batch_id=?", (s["batch_id"],)))
    assert "가상신생명" in msgs and "매핑에 없던 보험사" in msgs
    # IM라이프, 농협생명 은 별칭으로 마스터에서 찾는다. 가상신생명만 마스터에 없다
    assert s["unmapped_insurers"] == 1
    got = {r[0]: r[1] for r in env["con"].execute("SELECT DISTINCT insurer_name, insurer_id IS NOT NULL FROM agent_period_perf_insurer")}
    assert got.get("IM라이프", 1) == 1 and got.get("농협생명", 1) == 1


def test_bad_number_blocks_only_that_row(env):
    path = fp.write_person_perf(env["tmp"] / "badnum.xlsx", env["people"], summary=False, work_sheets=False)
    wb = openpyxl.load_workbook(path)
    ws = wb["인별"]
    col = [c.value for c in ws[2]].index("건수") + 1
    ws.cell(row=3, column=col, value="열두건")
    wb.save(path)
    s = pp.load_person_perf(env["con"], path, START, END)
    assert s["block"] == 1 and s["skipped"] == 1 and s["loaded"] == len(env["people"]) - 1 and s["sums_match"]


def test_sensitive_group_is_dropped(env):
    people = [dict(p, values={**p["values"], ("소득", "금액"): 987654321 + i}) for i, p in enumerate(env["people"])]
    path = fp.write_person_perf(env["tmp"] / "sens.xlsx", people, extra_groups=[("소득", ["금액"])], work_sheets=False)
    s = pp.load_person_perf(env["con"], path, START, END)
    assert s["block"] == 0 and s["dropped_columns"] == ["소득/금액"]
    dump = "\n".join(env["con"].iterdump())
    assert "987654321" not in dump and "98765432" not in dump


# ------------------------------------------------------------------ 계약 원장과 섞지 않는다, 읽기 도우미
def test_contract_ledger_is_untouched_and_helpers_are_scoped(env):
    con, roster = env["con"], env["roster"]
    cpath, _ = fe.make_contract_file(env["tmp"] / "c.xlsx", period_ym="202602", roster=roster)
    ec.load_contract(con, cpath, "202602")
    before = (con.execute("SELECT COUNT(*), SUM(converted_premium) FROM contract").fetchone()[:], performance.tally(con, "202602", "202602"))
    pp.load_person_perf(con, env["path"], START, END)
    after = (con.execute("SELECT COUNT(*), SUM(converted_premium) FROM contract").fetchone()[:], performance.tally(con, "202602", "202602"))
    assert before == after
    ids = _ids(con)
    some = ids[:5]
    assert set(pp.period_rows(con, START, END, some)) == set(some)
    assert pp.period_rows(con, START, END, []) == {}
    np_ = pp.no_perf(con, START, END, ids)
    zero = {r[0] for r in con.execute("SELECT agent_id FROM agent_period_perf WHERE total_count=0 AND agent_id IS NOT NULL")}
    assert set(np_["none"]) == zero and np_["absent"] == []
    cmp_ = pp.compare_with_ledger(con, START, END, some)
    assert cmp_["ledger"] is False and {r["agent_id"] for r in cmp_["rows"]} == set(some)


def test_compare_with_ledger_uses_the_same_tally(env):
    con, roster = env["con"], env["roster"]
    cpath, _ = fe.make_contract_file(env["tmp"] / "c.xlsx", period_ym="202602", roster=roster)
    ec.load_contract(con, cpath, "202602")
    path = fp.write_person_perf(env["tmp"] / "202602-02_인별실적.xlsx", env["people"], work_sheets=False)
    pp.load_person_perf(con, path, "202602", "202602")
    ids = _ids(con)
    cmp_ = pp.compare_with_ledger(con, "202602", "202602", ids)
    tally = performance.tally(con, "202602", "202602", agent_ids=ids)
    assert cmp_["ledger"] is True
    for r in cmp_["rows"]:
        t = tally.get(r["agent_id"]) or performance.blank()
        assert r["ledger_net_count"] == t["net_count"] and r["ledger_monthly_premium"] == t["monthly_premium"]
        assert r["count_same"] == (r["in_file"] and r["file_count"] == t["net_count"])
