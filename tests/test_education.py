"""교육: 진척도, 기한 임박, 범위, 같은 수료를 두 번 넣지 않기, 퇴사자, 명함 수료 코드 기록."""
import pathlib
import sqlite3
import tempfile

import pytest

from app import db, masters, samples_edu
from app.engine import education as edu

REF = "2026-10-31"


@pytest.fixture()
def env():
    con = db.init_db(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    masters.seed(con)
    hq = con.execute("SELECT org_id FROM org_unit WHERE level='hq' ORDER BY org_id").fetchone()[0]
    b1, b2 = [r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE parent_id=? ORDER BY org_id", (hq,))][:2]
    members = lambda org: [r[0] for r in con.execute(
        """SELECT h.agent_id FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id
           WHERE h.org_id=? AND h.effective_to IS NULL AND a.job_type='fc' ORDER BY a.internal_code""", (org,))]
    course = lambda code, **kw: edu.save_course(con, by="t", **({"code": code, "name": f"{code} 과정", "kind": "compliance", "required": True} | kw))
    session = lambda cid, day, **kw: edu.save_session(con, by="t", course_id=cid, date_from=day, online=True, **kw)
    done = lambda agent, cid, day, **kw: edu.record_completion(con, agent_id=agent, course_id=cid, completed_on=day, by="t", agent_ids=None, **kw)
    yield {"con": con, "b1": b1, "b2": b2, "m1": members(b1), "m2": members(b2), "course": course, "session": session, "done": done}
    con.close()


def _by_code(progress):
    return {c["course_code"]: c for c in progress["courses"]}


def test_progress_counts_required_courses_per_person_and_per_course(env):
    con, m1 = env["con"], env["m1"]
    a, b, c = m1[:3]
    req = env["course"]("CP", deadline_rule="fixed", deadline_date="2026-12-31")
    opt = env["course"]("OPT", required=False)
    s1 = env["session"](req, "2026-09-01")
    env["done"](a, req, None, session_id=s1)                 # 차수의 종료일이 수료일이 된다
    env["done"](b, req, "2026-10-15")
    env["done"](c, opt, "2026-10-15")                        # 필수가 아니면 진척도에 들지 않는다
    env["done"](c, req, "2026-11-02")                        # 기준일 뒤의 수료는 아직 세지 않는다

    p = edu.org_progress(con, m1, REF)
    cp = _by_code(p)["CP"]
    assert "OPT" not in _by_code(p)
    assert (cp["targets"], cp["done"], cp["not_done"]) == (len(m1), 2, len(m1) - 2)
    assert cp["rate"] == round(2 / len(m1), 4)
    assert p["total"]["targets"] == len(m1) and p["total"]["people_all_done"] == 2

    rows = {r["agent_id"]: r for r in edu.person_status(con, m1, REF)}
    assert rows[a]["required_done"] == 1 and rows[a]["rate"] == 1.0
    assert rows[a]["courses"][0]["completed_on"] == "2026-09-01" and rows[a]["courses"][0]["session_no"] == 1
    assert rows[c]["required_done"] == 0 and rows[c]["courses"][0]["state"] == "open"
    # 같은 날짜로 다시 부르면 같은 값이 나온다. 표에 남기지 않고 계산하기 때문이다
    assert edu.org_progress(con, m1, REF) == p


def test_by_org_groups_people_under_the_level_and_stays_in_scope(env):
    con, m1, m2 = env["con"], env["m1"], env["m2"]
    req = env["course"]("CP")
    for x in m1[:2] + m2[:1]:
        env["done"](x, req, "2026-10-01")
    rows = {r["org_id"]: r for r in edu.progress_by_org(con, m1 + m2, "branch", REF)}
    assert set(rows) == {env["b1"], env["b2"]}
    assert (rows[env["b1"]]["targets"], rows[env["b1"]]["done"]) == (len(m1), 2)
    assert (rows[env["b2"]]["targets"], rows[env["b2"]]["done"]) == (len(m2), 1)


def test_deadline_soon_overdue_and_hire_based(env):
    con, m1 = env["con"], env["m1"]
    a, b = m1[:2]
    seven = env["course"]("D7", deadline_rule="fixed", deadline_date="2026-11-07")    # 7일 남음: 임박
    env["course"]("D8", deadline_rule="fixed", deadline_date="2026-11-08")    # 8일 남음: 여유
    env["course"]("D0", deadline_rule="fixed", deadline_date="2026-10-30")     # 하루 지남
    env["done"](b, seven, "2026-10-20")
    soon = edu.due_soon(con, [a, b], REF)
    assert [(r["agent_id"], r["course_code"], r["days_left"]) for r in soon] == [(a, "D7", 7)]
    rows = {(r["agent_id"], r["course_code"]): r["state"] for r in edu.not_done(con, [a, b], REF)}
    assert rows[(a, "D0")] == "overdue" and rows[(a, "D8")] == "open" and (b, "D7") not in rows
    assert edu.not_done(con, [a, b], REF)[0]["state"] == "overdue"          # 기한 지남이 먼저 나온다
    assert edu.org_progress(con, [a, b], REF)["total"]["soon"] == 1

    # 위촉 뒤 N일: 위촉일에 따라 사람마다 기한이 다르다. 늦게 수료하면 수료지만 late 가 참이다
    con.execute("UPDATE agent SET hire_date='2026-10-01' WHERE agent_id=?", (a,))
    con.execute("UPDATE agent SET hire_date='2026-01-01' WHERE agent_id=?", (b,))
    nb = env["course"]("NB", kind="newbie", deadline_rule="hire_days", deadline_days=30, target_hired_from="2026-01-01")
    env["done"](b, nb, "2026-03-15")
    cells = {r["agent_id"]: next(x for x in r["courses"] if x["course_code"] == "NB") for r in edu.person_status(con, [a, b], REF)}
    assert cells[a]["due"] == "2026-10-31" and cells[a]["days_left"] == 0 and cells[a]["state"] == "soon"
    assert cells[b]["state"] == "done" and cells[b]["late"] is True


def test_yearly_course_counts_only_this_years_completion(env):
    con, a = env["con"], env["m1"][0]
    y = env["course"]("YR", deadline_rule="yearly", deadline_md="11-06")
    env["done"](a, y, "2025-10-01")
    cell = edu.person_status(con, [a], REF)[0]["courses"][0]
    assert cell["state"] == "soon" and cell["due"] == "2026-11-06"
    r = env["done"](a, y, "2026-10-01")                    # 다른 해의 수료는 다른 주기라 새로 들어간다
    assert r["created"]
    assert edu.person_status(con, [a], REF)[0]["courses"][0]["state"] == "done"


def test_scope_never_mixes_people_outside_the_list(env):
    con, m1, m2 = env["con"], env["m1"], env["m2"]
    req = env["course"]("CP", deadline_rule="fixed", deadline_date="2026-11-03")
    s = env["session"](req, "2026-10-10")
    for x in m1[:1] + m2:
        env["done"](x, req, None, session_id=s)
    p = _by_code(edu.org_progress(con, m1, REF))["CP"]
    assert (p["targets"], p["done"]) == (len(m1), 1)
    assert {r["agent_id"] for r in edu.completers(con, req, m1)} == {m1[0]}
    assert edu.sessions(con, req, m1)[0]["completers"] == 1
    assert {r["agent_id"] for r in edu.person_status(con, m1, REF)} == set(m1)
    assert {r["agent_id"] for r in edu.not_done(con, m1, REF)} <= set(m1)
    assert edu.org_progress(con, [], REF)["total"]["targets"] == 0          # 빈 범위는 아무도 보지 않는다
    with pytest.raises(PermissionError):
        edu.my_education(con, m2[0], m1, REF)
    with pytest.raises(PermissionError):
        edu.record_completion(con, agent_id=m2[1], course_id=req, completed_on="2026-10-10", by="mgr", agent_ids=m1)


def test_course_targets_role_level_and_owner_org(env):
    con, m1, m2 = env["con"], env["m1"], env["m2"]
    mine = env["course"]("ORG", owner_org_id=env["b1"])
    mgr = env["course"]("MGR", target_role="manager")
    level = con.execute("""SELECT l.code FROM agent_career_history h JOIN career_level l ON l.level_id=h.level_id
                           WHERE h.agent_id=?""", (m1[0],)).fetchone()[0]
    lv = env["course"]("LV", target_levels=level)
    rows = {r["agent_id"]: {x["course_id"] for x in r["courses"]} for r in edu.person_status(con, m1 + m2, REF)}
    assert all(mine in rows[x] for x in m1) and not any(mine in rows[x] for x in m2)
    assert not any(mgr in v for v in rows.values())
    assert lv in rows[m1[0]]
    head = con.execute("SELECT agent_id FROM org_manager WHERE org_id=? AND effective_to IS NULL", (env["b1"],)).fetchone()[0]
    assert {x["course_id"] for x in edu.person_status(con, [head], REF)[0]["courses"]} == {mgr}     # ORG 는 설계사 대상이다
    both = env["course"]("ALL", owner_org_id=env["b1"], target_role="all")
    assert {x["course_id"] for x in edu.person_status(con, [head], REF)[0]["courses"]} == {mgr, both}
    assert [c["code"] for c in edu.list_courses(con, [env["b2"]])] == ["LV", "MGR"]          # 다른 지점이 연 과정은 안 보인다
    assert "ORG" in [c["code"] for c in edu.list_courses(con, [env["b1"]])]


def test_same_completion_is_not_recorded_twice(env):
    con, a = env["con"], env["m1"][0]
    req = env["course"]("CP")
    s1, s2 = env["session"](req, "2026-09-01"), env["session"](req, "2026-10-01")
    first = env["done"](a, req, None, session_id=s1)
    again = env["done"](a, req, None, session_id=s1)
    other = env["done"](a, req, None, session_id=s2)          # 다른 차수를 또 들어도 같은 과정의 같은 수료다
    assert first["created"] and not again["created"] and not other["created"]
    assert again["duplicate_of"] == first["completion_id"] == other["completion_id"]
    assert con.execute("SELECT COUNT(*) FROM edu_completion WHERE agent_id=?", (a,)).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):              # 엔진을 거치지 않아도 표가 막는다
        con.execute("""INSERT INTO edu_completion(agent_id, course_id, completed_on, cycle_key, source, recorded_by, recorded_at)
                       VALUES(?,?,?,?,?,?,?)""", (a, req, "2026-10-02", "once", "manual", "t", db.now()))
    # 뺀 줄은 지우지 않고 남으며, 빼고 나면 다시 넣을 수 있다
    edu.void_completion(con, first["completion_id"], by="t", reason="잘못 넣음", agent_ids=None)
    assert env["done"](a, req, "2026-10-01")["created"]
    assert con.execute("SELECT COUNT(*) FROM edu_completion WHERE agent_id=?", (a,)).fetchone()[0] == 2


def test_roster_batch_can_be_voided_and_rebuilt(env):
    con, m1 = env["con"], env["m1"]
    req = env["course"]("CP")
    mk = lambda h: con.execute("""INSERT INTO import_batch(data_type, period_ym, file_name, file_path, file_hash, received_at)
                                  VALUES('edu_roster','202610','명부.xlsx','edu/명부.xlsx',?,?)""", (h, db.now())).lastrowid
    b1 = mk("h1")
    for i, x in enumerate(m1[:3], start=2):
        env["done"](x, req, "2026-10-01", source="roster", batch_id=b1, row_no=i)
    assert edu.void_batch(con, b1, by="t", reason="명부 고쳐 다시 올림") == 3
    assert _by_code(edu.org_progress(con, m1, REF))["CP"]["done"] == 0
    b2 = mk("h2")
    for i, x in enumerate(m1[:2], start=2):
        env["done"](x, req, "2026-10-01", source="roster", batch_id=b2, row_no=i)
    assert _by_code(edu.org_progress(con, m1, REF))["CP"]["done"] == 2
    assert con.execute("SELECT COUNT(*) FROM edu_completion WHERE batch_id=?", (b1,)).fetchone()[0] == 3     # 앞 배치의 줄도 남는다


def test_left_agent_is_out_of_the_denominator_but_the_record_stays(env):
    con, m1 = env["con"], env["m1"]
    gone, later = m1[0], m1[1]
    req = env["course"]("CP")
    env["done"](gone, req, "2026-09-01")
    con.execute("UPDATE agent SET status='terminated', termination_date='2026-10-15' WHERE agent_id=?", (gone,))
    con.execute("UPDATE agent SET status='terminated', termination_date='2026-11-20' WHERE agent_id=?", (later,))   # 기준일 뒤 퇴사
    p = edu.org_progress(con, m1, REF)
    cp = _by_code(p)["CP"]
    assert (cp["targets"], cp["done"], cp["left_excluded"]) == (len(m1) - 1, 0, 1)
    assert p["total"]["left_excluded"] == 1 and p["total"]["people"] == len(m1) - 1
    assert gone not in {r["agent_id"] for r in edu.not_done(con, m1, REF)}
    assert later in {r["agent_id"] for r in edu.not_done(con, m1, REF)}
    rows = {r["agent_id"]: r for r in edu.person_status(con, m1, REF)}
    assert rows[gone]["left"] and not rows[later]["left"]
    kept = edu.completers(con, req, m1)
    assert [(r["agent_id"], bool(r["left"])) for r in kept] == [(gone, True)]
    # 퇴사 전 날짜로 보면 그때는 모수에 들어간다
    assert _by_code(edu.org_progress(con, m1, "2026-10-01"))["CP"]["targets"] == len(m1)


def test_card_code_keeps_only_serial_and_hash(env):
    con, m1 = env["con"], env["m1"]
    abc = env["course"]("ABC", kind="product", required=False, card_course_code="ABC")
    with pytest.raises(ValueError):
        env["course"]("XYZ", card_course_code="XYZ")
    c = env["done"](m1[0], abc, "2026-10-17")["completion_id"]
    env["done"](m1[1], abc, "2026-10-17")
    assert {r["agent_id"] for r in edu.card_code_candidates(con, m1[:1], "ABC")} == {m1[0]}
    iid = edu.request_card_code(con, c, by="admin")
    with pytest.raises(ValueError):
        edu.request_card_code(con, c, by="admin")
    assert m1[0] not in {r["agent_id"] for r in edu.card_code_candidates(con, m1)}
    code = "abc-7k2q 9xzp"
    edu.record_card_code_issued(con, iid, serial="ABC2610-001", code=code, by="admin")
    row = dict(con.execute("SELECT * FROM edu_card_code WHERE issue_id=?", (iid,)).fetchone())
    assert row["status"] == "issued" and row["completed_ym"] == "2026-10" and row["serial"] == "ABC2610-001"
    assert row["code_sha256"] == edu.card_code_hash("ABC-7K2Q-9XZP") and len(row["code_sha256"]) == 64
    assert not any("7K2Q" in str(v).upper() for v in row.values())
    edu.revoke_card_code(con, iid, by="admin", reason="시험")
    assert edu.card_codes(con, m1) == [] and len(edu.card_codes(con, m1, include_revoked=True)) == 1
    assert edu.request_card_code(con, c, by="admin")                    # 내린 뒤에는 새로 요청할 수 있다


def test_ensure_builds_tables_on_an_old_db(env):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    text = db.SCHEMA.read_text(encoding="utf-8")
    con.executescript(text[:text.find(edu.BEGIN)])
    edu.ensure(con)
    edu.ensure(con)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(edu.TABLES) <= names


def test_sample_seed_is_reproducible(env):
    con = env["con"]
    n = samples_edu.seed(con)
    assert (n["courses"], n["required"]) == (4, 3) and n["completions"] > 0
    assert con.execute("SELECT COUNT(*) FROM edu_course WHERE card_course_code='ABC'").fetchone()[0] == 1
    first = samples_edu.summary_lines(con)
    assert samples_edu.summary_lines(con) == first
    assert first[0].startswith(f"[education] {samples_edu.REF_DATE} 기준")
