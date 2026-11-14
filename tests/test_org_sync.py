"""여섯 단계 조직, 사원현황과 계약현황에서 조직 만들기, 조직장과 대리의 범위를 본다. 가짜 이터널 파일로 돈다.
사람 이름은 비교에도 출력에도 쓰지 않는다. 사번과 건수로만 확인한다."""
import copy
import pathlib
import tempfile
from datetime import date, timedelta

import pytest

from app import db, masters
from app.loader import eternal_contract as ec
from app.loader import eternal_employee as ee
from app.loader import fake_eternal as fe
from app.loader import org_path
from app.org import heads as hd
from app.org import levels, sync
from app.web import auth

YM = "202602"
TABLES = ("org_unit", "agent", "agent_org_history", "org_manager", "org_deputy", "user_account")


@pytest.fixture()
def env():
    tmp = pathlib.Path(tempfile.mkdtemp())
    con = db.init_db(tmp / "t.db")
    roster = fe.fake_roster()
    emp_path, _ = fe.make_employee_file(tmp / "emp.xlsx", roster)
    rows = ee.parse_employee_file(emp_path)["rows"]
    yield {"con": con, "tmp": tmp, "roster": roster, "rows": rows}
    con.close()


def counts(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def org_of(con, code, ym="209912"):
    r = con.execute("""SELECT h.org_id FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id WHERE a.internal_code=?
                       AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)""", (code, ym, ym)).fetchone()
    return r[0] if r else None


def aid(con, code):
    return con.execute("SELECT agent_id FROM agent WHERE internal_code=?", (code,)).fetchone()[0]


def ctx_of(con, code):
    """계정을 만들어 그 사람의 범위를 돌려준다."""
    a = aid(con, code)
    if not con.execute("SELECT 1 FROM user_account WHERE agent_id=?", (a,)).fetchone():
        auth.issue_account(con, a)
        con.commit()
    uid = con.execute("SELECT user_id FROM user_account WHERE agent_id=?", (a,)).fetchone()[0]
    return auth.context(con, uid)


def subtree(con, org_id):
    return auth._descendants(con, [org_id])


def members(con, org_ids, ym="209912"):
    q = ",".join("?" * len(org_ids))
    return {r[0] for r in con.execute(f"""SELECT h.agent_id FROM agent_org_history h WHERE h.org_id IN ({q})
                                         AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)""", (*org_ids, ym, ym))}


# ---------------------------------------------------------------- 계층 설정
def test_levels_come_from_config():
    assert levels.codes() == ["ga", "hq", "branch", "center", "team"]
    assert auth.LEVEL_KO["ga"] == "GA" and auth.LEVEL_KO["center"] == "센터"
    assert {"hq_head", "branch_head", "center_head", "team_head", "deputy"} <= set(auth.HEAD_KO)
    assert levels.head_role("ga") is None and levels.head_role("team") == "team_head"
    assert levels.position_role("직할지점장") == "center_head" and levels.position_role("FC") is None


# ---------------------------------------------------------------- 사원현황
def test_staff_sync_builds_six_levels_by_path(env):
    con, rows = env["con"], env["rows"]
    res = sync.sync_staff(con, rows, YM)
    st = res["stats"]
    assert st["rows"] == len(rows) == 40 and st["new_agents"] == 40 and st["terminated"] == 1
    assert st["heads_new"] == sum(1 for r in rows if r["position"] == "지점장" and not r["termination_date"])
    by_level = {r[0]: r[1] for r in con.execute("SELECT level, COUNT(*) FROM org_unit GROUP BY level")}
    assert by_level["ga"] == 1 and by_level["hq"] == 4 and by_level["branch"] == 8 and by_level["center"] >= 9 and by_level["team"] >= 8
    # 이름이 같은 센터와 팀이 상위마다 따로 있다
    assert con.execute("SELECT COUNT(DISTINCT parent_id) FROM org_unit WHERE level='center' AND name=?", (fe.SHARED_CENTER,)).fetchone()[0] == 2
    assert con.execute("SELECT COUNT(DISTINCT parent_id) FROM org_unit WHERE level='team' AND name=?", (fe.SHARED_TEAM,)).fetchone()[0] == 2
    # 계층은 위에서 아래로만 내려간다. 팀의 부모는 센터, 센터의 부모는 지점
    pairs = {tuple(r) for r in con.execute("SELECT c.level, p.level FROM org_unit c JOIN org_unit p ON p.org_id=c.parent_id")}
    assert pairs == {("hq", "ga"), ("branch", "hq"), ("center", "branch"), ("team", "center")}
    # 사람은 경로의 맨 아래 조직에 붙는다
    for a in env["roster"]:
        if a["path"][4]:
            assert con.execute("SELECT level FROM org_unit WHERE org_id=?", (org_of(con, a["code"], YM),)).fetchone()[0] == "team"
    # 이름은 돌려주는 changes 에만 있고, 그 밖의 건수에는 없다
    assert all(set(c) == {"kind", "code", "name", "before", "after", "note"} for c in res["changes"])


def test_staff_sync_is_idempotent_and_preview_matches(env):
    con, rows = env["con"], env["rows"]
    before = counts(con)
    dry = sync.sync_staff(con, rows, YM, dry_run=True)
    assert counts(con) == before                                         # 미리보기는 아무것도 남기지 않는다
    real = sync.sync_staff(con, rows, YM)
    assert dry["stats"] == real["stats"] and len(dry["changes"]) == len(real["changes"])
    after = counts(con)
    again = sync.sync_staff(con, rows, YM)
    assert counts(con) == after
    assert {k: v for k, v in again["stats"].items() if k not in ("rows", "missing", "head_without_org")} == {
        k: 0 for k in sync.STAT_KEYS if k not in ("rows", "missing", "head_without_org")}
    assert again["changes"] == []


def test_staff_sync_moves_terminations_and_heads(env):
    con, rows = env["con"], env["rows"]
    sync.sync_staff(con, rows, YM)
    later = copy.deepcopy(rows)
    fc = next(r for r in later if r["position"] == "FC" and not r["termination_date"] and r["org"]["팀"])
    leaver = next(r for r in later if r["position"] == "FC" and not r["termination_date"] and r is not fc and not r["org"]["팀"])
    head = next(r for r in later if r["position"] == "지점장")
    other = next(r for r in later if r["org"]["지점"] != fc["org"]["지점"])
    old_org = org_of(con, fc["employee_no"], YM)
    fc["org"] = dict(other["org"])
    leaver["termination_date"] = "2026-04-30"
    head["position"] = "FC"
    # 퇴사 전에 계정을 만들어 둔다. 퇴사해도 자동으로 중지하지 않는다
    auth.issue_account(con, aid(con, leaver["employee_no"]))
    con.commit()
    st = sync.sync_staff(con, later, "202605")["stats"]
    assert st["moved"] == 1 and st["terminated"] == 1 and st["heads_end"] == 1 and st["new_orgs"] == 0
    hist = con.execute("SELECT org_id, effective_from, effective_to FROM agent_org_history WHERE agent_id=? ORDER BY effective_from",
                       (aid(con, fc["employee_no"]),)).fetchall()
    assert [tuple(h) for h in hist][-2:] == [(old_org, hist[-2][1], "202604"), (org_of(con, fc["employee_no"]), "202605", None)]
    assert org_of(con, fc["employee_no"], "202604") == old_org                # 지난달 실적은 그때 소속에 남는다
    lv = con.execute("SELECT status, termination_date FROM agent WHERE internal_code=?", (leaver["employee_no"],)).fetchone()
    assert tuple(lv) == ("terminated", "2026-04-30") and org_of(con, leaver["employee_no"], "202605") is None
    assert [c["internal_code"] for c in sync.termination_candidates(con)] == [leaver["employee_no"]]
    assert con.execute("SELECT status FROM user_account WHERE login_id=?", (leaver["employee_no"],)).fetchone()[0] == "active"
    assert con.execute("SELECT effective_to FROM org_manager WHERE agent_id=?", (aid(con, head["employee_no"]),)).fetchone()[0] == "202604"
    # 퇴사일이 빠진 파일이 다시 오면 복직으로 본다
    leaver["termination_date"] = None
    st = sync.sync_staff(con, later, "202607")["stats"]
    assert st["reinstated"] == 1 and st["moved"] == 0
    assert org_of(con, leaver["employee_no"], "202607") is not None and sync.termination_candidates(con) == []


def test_manual_heads_survive_staff_sync(env):
    con, rows = env["con"], env["rows"]
    sync.sync_staff(con, rows, YM)
    fc = next(r for r in rows if r["position"] == "FC" and not r["termination_date"])
    team = org_of(con, fc["employee_no"], YM)
    hd.assign_head(con, team, aid(con, fc["employee_no"]), YM, by="t")
    con.commit()
    sync.sync_staff(con, rows, "202603")
    assert con.execute("SELECT effective_to FROM org_manager WHERE source='manual'").fetchone()[0] is None


# ---------------------------------------------------------------- 범위
def test_scope_on_six_levels(env):
    con, rows = env["con"], env["rows"]
    sync.sync_staff(con, rows, YM)
    # 이름이 같은 팀 둘 가운데 하나의 팀원에게 팀장을, 그 팀의 센터 사람에게 센터장을 맡긴다
    teams = [r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE level='team' AND name=? ORDER BY org_id", (fe.SHARED_TEAM,))]
    team, twin = teams
    center = con.execute("SELECT parent_id FROM org_unit WHERE org_id=?", (team,)).fetchone()[0]
    in_team = sorted(members(con, [team]))
    in_center = sorted(members(con, [center]))
    assert in_team and in_center and members(con, [twin])
    t_code = con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (in_team[0],)).fetchone()[0]
    c_code = con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (in_center[0],)).fetchone()[0]
    hd.assign_head(con, team, in_team[0], YM, by="t")
    hd.assign_head(con, center, in_center[0], YM, by="t")
    con.commit()

    t = ctx_of(con, t_code)
    assert t["role"] == "manager" and t["org_ids"] == {team}
    assert t["agent_ids"] == members(con, [team]) and not (t["agent_ids"] & members(con, [twin]))

    c = ctx_of(con, c_code)
    assert c["org_ids"] == subtree(con, center) and team in c["org_ids"] and twin not in c["org_ids"]
    assert {lv for (lv,) in con.execute(f"SELECT level FROM org_unit WHERE org_id IN ({','.join('?' * len(c['org_ids']))})", tuple(c["org_ids"]))} <= {"center", "team"}
    assert c["agent_ids"] == members(con, c["org_ids"]) | {c["agent_id"]}
    assert not (c["agent_ids"] & members(con, [twin]))

    # 사원현황의 지점장은 지점과 그 아래 센터와 팀까지 본다
    br_head = next(r for r in rows if r["position"] == "지점장" and not r["termination_date"])
    b = ctx_of(con, br_head["employee_no"])
    branch = con.execute("SELECT org_id FROM org_manager WHERE agent_id=?", (b["agent_id"],)).fetchone()[0]
    assert b["org_ids"] == subtree(con, branch) and len(b["org_ids"]) >= 1
    everyone = {r[0] for r in con.execute("SELECT agent_id FROM agent")}
    assert b["agent_ids"] == members(con, b["org_ids"]) | {b["agent_id"]} and everyone - b["agent_ids"]

    # 지정이 아닌 설계사는 본인만
    plain = next(r for r in rows if r["position"] == "FC" and not r["termination_date"] and r["employee_no"] not in (t_code, c_code))
    p = ctx_of(con, plain["employee_no"])
    assert p["role"] == "agent" and p["org_ids"] == set() and p["agent_ids"] == {p["agent_id"]}


def test_head_term_ends_by_itself_and_deputy_is_dated(env):
    con, rows = env["con"], env["rows"]
    sync.sync_staff(con, rows, YM)
    fc = next(r for r in rows if r["position"] == "FC" and not r["termination_date"])
    a, team = aid(con, fc["employee_no"]), org_of(con, fc["employee_no"], YM)
    ym = auth.current_ym(con)
    hd.assign_head(con, team, a, "202601", "202602", by="t")              # 끝난 지정
    con.commit()
    assert ctx_of(con, fc["employee_no"])["role"] == "agent"
    today = date.today()
    hd.assign_deputy(con, team, a, (today + timedelta(days=3)).isoformat(), (today + timedelta(days=5)).isoformat(), "휴가", by="t")
    con.commit()
    assert ctx_of(con, fc["employee_no"])["role"] == "agent"               # 아직 시작 전
    did = hd.assign_deputy(con, team, a, today.isoformat(), (today + timedelta(days=2)).isoformat(), "휴가", by="t")
    con.commit()
    d = ctx_of(con, fc["employee_no"])
    assert d["role"] == "manager" and d["org_ids"] == subtree(con, team) and any(h["role"] == "deputy" for h in d["heads"])
    hd.end_deputy(con, did, by="t")
    con.commit()
    assert ctx_of(con, fc["employee_no"])["role"] == "agent"
    mid = hd.assign_head(con, team, a, ym, by="t")
    con.commit()
    assert ctx_of(con, fc["employee_no"])["role"] == "manager"
    hd.end_head(con, mid, ym)
    con.commit()
    assert ctx_of(con, fc["employee_no"])["role"] == "agent"
    with pytest.raises(hd.HeadError):
        hd.assign_head(con, sync.root_id(con), a, ym, by="t")              # GA 에는 조직장이 없다
    with pytest.raises(hd.HeadError):
        hd.assign_deputy(con, team, a, today.isoformat(), (today + timedelta(days=200)).isoformat(), "", by="t")


# ---------------------------------------------------------------- 계약현황의 조직 경로
def test_contract_paths_make_the_same_tree_as_staff(env):
    con = env["con"]
    for k, (name, sector) in enumerate(fe.MASTER_INSURERS.items()):
        con.execute("INSERT INTO insurer(code, name, sector) VALUES(?,?,?)", (f"T{k:02d}", name, sector))
    for i, (code, name, elig) in enumerate(masters.CATEGORIES):
        con.execute("INSERT INTO product_category(code, name, incentive_eligible, sort_order) VALUES(?,?,?,?)", (code, name, elig, i))
    con.commit()
    path, _ = fe.make_contract_file(env["tmp"] / "contract.xlsx", period_ym=YM, roster=env["roster"])
    ec.load_contract(con, path, YM)
    nodes = org_path.org_nodes(con)
    before = counts(con)
    dry = sync.sync_contract_orgs(con, dry_run=True)
    assert counts(con) == before and dry["nodes"] == len(nodes)
    res = sync.sync_contract_orgs(con)
    assert res == dry and res["new_orgs"] == len(nodes)                     # 본사 마디가 뿌리가 된다
    assert con.execute("SELECT COUNT(*) FROM org_unit WHERE level='center' AND name=?", (fe.SHARED_CENTER,)).fetchone()[0] == 2
    assert sync.sync_contract_orgs(con)["new_orgs"] == 0
    # 같은 사람들의 사원현황을 넣어도 새 조직이 생기지 않는다. 두 파일의 경로가 같은 조직을 가리킨다
    assert sync.sync_staff(con, env["rows"], YM)["stats"]["new_orgs"] == 0


def test_backfill_keeps_old_trees_working():
    tmp = pathlib.Path(tempfile.mkdtemp())
    con = db.init_db(tmp / "t.db")
    hq = con.execute("INSERT INTO org_unit(parent_id, name, level) VALUES(NULL,'옛본부','hq')").lastrowid
    br = con.execute("INSERT INTO org_unit(parent_id, name, level) VALUES(?,'옛지점','branch')", (hq,)).lastrowid
    assert sync.backfill(con) == 2
    root = sync.root_id(con)
    assert con.execute("SELECT parent_id FROM org_unit WHERE org_id=?", (hq,)).fetchone()[0] == root
    assert sync.ensure_path(con, [("hq", "옛본부"), ("branch", "옛지점")]) == br
    assert sync.backfill(con) == 0
    con.close()


# ---------------------------------------------------------------- 원수사
def test_real_insurers_seed():
    tmp = pathlib.Path(tempfile.mkdtemp())
    con = db.init_db(tmp / "t.db")
    for code, name, sector in masters.INSURERS:
        con.execute("INSERT INTO insurer(code, name, sector) VALUES(?,?,?)", (code, name, sector))
    con.execute("INSERT INTO insurer(code, name, sector, origin) VALUES('AUTO_X','흥국화재','life','loader')")
    assert len(masters.REAL_INSURERS) == len(set(masters.REAL_INSURERS)) == 32
    assert masters.seed_real_insurers(con) == 32 and masters.seed_real_insurers(con) == 0
    rows = {r["name"]: (r["sector"], r["origin"]) for r in con.execute("SELECT name, sector, origin FROM insurer")}
    assert len(rows) == 36 and all(n in rows for _, n, _ in masters.INSURERS)
    assert rows["흥국화재"] == ("nonlife", "master")
    for name in ("IBK연금", "메트라이프", "처브라이프생명", "iM라이프", "푸본현대생명"):
        assert rows[name][0] == "life", name
    for name in ("현대해상", "라이나손보", "AIG손보", "메리츠화재"):
        assert rows[name][0] == "nonlife", name
    assert sum(1 for n in masters.REAL_INSURERS if rows[n][0] == "life") == 20
    with pytest.raises(ValueError):
        masters.insurer_sector("가상보험")
    con.close()
