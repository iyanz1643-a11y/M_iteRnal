"""조직 부문 시상: 순위, 범위, 확정, 누가 무엇을 보는가. 그리고 프로모션 고치기."""
import json
import pathlib
import tempfile
from datetime import date

import pytest

from app import db, masters
from app.engine import org_award as oa
from app.engine import promotion as pm
from app.web import auth

YM = "202610"


@pytest.fixture()
def env():
    con = db.init_db(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    masters.seed(con)
    auth.seed_accounts(con, "pw-12345678")
    hqs = [r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE level='hq' ORDER BY org_id")]
    branches = lambda hq: [r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE parent_id=? ORDER BY org_id", (hq,))]
    members = lambda org: [r[0] for r in con.execute(
        "SELECT h.agent_id FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id WHERE h.org_id=? AND h.effective_to IS NULL AND a.job_type='fc' ORDER BY h.agent_id", (org,))]
    b1, b2 = branches(hqs[0])[:2]
    other = branches(hqs[1])[0]
    n = [0]

    def add(agent, day, premium, status="inforce", sector="life", cat="health", count=1, withdrawn_on=None):
        p = con.execute("SELECT p.product_id, p.insurer_id FROM product p JOIN insurer i ON i.insurer_id=p.insurer_id WHERE i.sector=? AND p.product_category=? LIMIT 1",
                        (sector, cat)).fetchone()
        n[0] += 1
        con.execute("""INSERT INTO contract(insurer_id, policy_no, product_id, agent_id, application_no, application_date, contract_date, premium_monthly,
                                            converted_premium, policy_count, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (p["insurer_id"], f"T{n[0]:05d}", p["product_id"], agent, f"A{n[0]:05d}", day, day, premium, premium, count, status, db.now()))
        if status == "withdrawn":
            con.execute("UPDATE contract SET withdrawal_date=? WHERE policy_no=?", (withdrawn_on or day, f"T{n[0]:05d}"))
        con.commit()
    ctx = lambda agent_id=None: auth.context(con, con.execute(
        "SELECT user_id FROM user_account WHERE " + ("login_id='admin'" if agent_id is None else "agent_id=?"), () if agent_id is None else (agent_id,)).fetchone()[0])
    head = lambda org: con.execute("SELECT agent_id FROM org_manager WHERE org_id=? AND effective_to IS NULL", (org,)).fetchone()[0]
    yield {"con": con, "hq": hqs[0], "hq2": hqs[1], "b1": b1, "b2": b2, "other": other, "m1": members(b1), "m2": members(b2), "mo": members(other),
           "add": add, "ctx": ctx, "head": head}
    con.close()


def test_standing_ranks_ties_together_and_stays_inside_the_org(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    a3, out = env["m2"][0], env["mo"][0]
    add(a1, "2026-10-03", 300_000)
    add(a2, "2026-10-05", 200_000)
    add(a2, "2026-10-20", 100_000)
    add(a3, "2026-10-07", 100_000)
    add(a3, "2026-10-08", 900_000, status="withdrawn")     # 같은 달에 성립하고 철회하면 0 이다
    add(a3, "2026-09-30", 900_000)                          # 지난달
    add(out, "2026-10-07", 999_000)                         # 다른 본부
    did = oa.save(con, by="t", org_id=env["hq"], name="인정업적 부문", metric="converted_premium", top_n=2, effective_from=YM,
                  rewards=[(1, "50만 원"), (2, "30만 원")])
    rows = oa.standing(con, oa.load(con, did), YM)
    assert [(r["agent_id"], r["rank"], r["value"]) for r in rows] == [(a1, 1, 300_000), (a2, 1, 300_000), (a3, 3, 100_000)]
    assert [r["winner"] for r in rows] == [True, True, False] and rows[0]["reward"] == rows[1]["reward"] == "50만 원" and rows[2]["reward"] is None
    branch = oa.save(con, by="t", org_id=env["b2"], name="지점 부문", metric="converted_premium", top_n=1, effective_from=YM)
    assert [r["agent_id"] for r in oa.standing(con, oa.load(con, branch), YM)] == [a3]


def test_standing_matches_the_performance_figures(env):
    """부문 순위의 값은 업적 집계의 값과 같아야 한다. 성립한 달에 더하고 철회한 달에 뺀다."""
    from app.engine import performance
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    add(a1, "2026-10-03", 300_000)
    add(a1, "2026-09-20", 120_000, status="withdrawn", withdrawn_on="2026-10-02")   # 지난달 계약을 이번 달에 철회
    add(a2, "2026-10-05", 200_000)
    add(a2, "2026-10-06", 50_000, status="withdrawn", withdrawn_on="2026-11-02")    # 이번 달에 성립, 다음 달에 철회
    did = oa.save(con, by="t", org_id=env["hq"], name="인정업적 부문", metric="converted_premium", top_n=3, effective_from=YM)
    rows = {r["agent_id"]: r["value"] for r in oa.standing(con, oa.load(con, did), YM)}
    perf = performance.compute_range(con, YM, YM)
    assert rows == {a1: 180_000, a2: 250_000} == {a: perf[a]["converted_premium"] for a in (a1, a2)}
    cnt = oa.save(con, by="t", org_id=env["hq"], name="건수 부문", metric="policy_count", top_n=3, effective_from=YM)
    assert {r["agent_id"]: r["value"] for r in oa.standing(con, oa.load(con, cnt), YM)} == {a2: perf[a2]["net_count"]}   # a1 은 1건에서 1건을 빼 0 이다


def test_nonlife_count_division_and_minimum(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    add(a1, "2026-10-03", 10_000, sector="nonlife")
    add(a1, "2026-10-04", 10_000, sector="nonlife", count=0.5)
    add(a1, "2026-10-05", 500_000)                          # 생명보험은 세지 않는다
    add(a2, "2026-10-06", 10_000, sector="nonlife")
    did = oa.save(con, by="t", org_id=env["hq"], name="손해보험 건수 부문", metric="policy_count", sector="nonlife", top_n=3, effective_from=YM, min_value=1.5)
    rows = oa.standing(con, oa.load(con, did), YM)
    assert [(r["agent_id"], r["value"]) for r in rows] == [(a1, 1.5)]      # 1건인 사람은 최소 기준에 못 미친다


def test_confirm_keeps_a_snapshot(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    add(a1, "2026-10-03", 300_000)
    add(a2, "2026-10-05", 200_000)
    did = oa.save(con, by="t", org_id=env["hq"], name="부문", metric="converted_premium", top_n=1, effective_from=YM, rewards=[(1, "상품권")])
    assert oa.confirm(con, did, YM, "admin") == 1
    add(a2, "2026-10-09", 500_000)                          # 확정 뒤에 자료가 바뀌어도 확정은 그대로다
    assert [(r["agent_id"], r["reward"]) for r in oa.confirmed(con, did, YM)] == [(a1, "상품권")]
    assert oa.standing(con, oa.load(con, did), YM)[0]["agent_id"] == a2
    oa.confirm(con, did, YM, "admin")                       # 다시 확정하면 바뀐다
    assert [r["agent_id"] for r in oa.confirmed(con, did, YM)] == [a2]


def test_who_sees_and_who_manages(env):
    con = env["con"]
    mk = lambda org, name: oa.save(con, by="t", org_id=org, name=name, metric="converted_premium", top_n=3, effective_from=YM)
    d_hq, d_b1, d_b2, d_other = mk(env["hq"], "본부"), mk(env["b1"], "지점1"), mk(env["b2"], "지점2"), mk(env["hq2"], "다른 본부")
    old = oa.save(con, by="t", org_id=env["hq"], name="끝난 부문", metric="converted_premium", top_n=3, effective_from="202601", effective_to="202609")
    ids = lambda ctx, **kw: {d["division_id"] for d in oa.visible(con, ctx, YM, **kw)}
    agent, branch_head, hq_head, admin = env["ctx"](env["m1"][0]), env["ctx"](env["head"](env["b1"])), env["ctx"](env["head"](env["hq"])), env["ctx"]()
    assert ids(agent) == {d_hq, d_b1} == ids(branch_head)           # 위 조직이 연 부문과 자기 조직의 부문
    assert ids(hq_head) == {d_hq, d_b1, d_b2}
    assert ids(admin) == {d_hq, d_b1, d_b2, d_other} and old not in ids(admin)
    oa.set_status(con, d_b1, "inactive", "t")
    assert d_b1 not in ids(agent) and d_b1 in ids(admin, include_inactive=True)
    assert not oa.can_manage(agent, env["b1"])
    assert oa.can_manage(branch_head, env["b1"]) and not oa.can_manage(branch_head, env["hq"]) and not oa.can_manage(branch_head, env["b2"])
    assert oa.can_manage(hq_head, env["b2"]) and not oa.can_manage(hq_head, env["hq2"]) and oa.can_manage(admin, env["hq2"])


def test_agents_see_top_ten_and_themselves(env):
    con, add = env["con"], env["add"]
    people = (env["m1"] + env["m2"])[:12]
    assert len(people) == 12
    for i, a in enumerate(people):
        add(a, "2026-10-03", 1_000_000 - i * 10_000)
    did = oa.save(con, by="t", org_id=env["hq"], name="부문", metric="converted_premium", top_n=3, effective_from=YM)
    rows = oa.standing(con, oa.load(con, did), YM)
    last = env["ctx"](people[-1])
    assert [r["rank"] for r in oa.view_rows(rows, last)] == list(range(1, 11)) + [12]
    assert len(oa.view_rows(rows, env["ctx"]())) == 12


def test_save_validates_and_records_changes(env):
    con = env["con"]
    base = dict(by="t", org_id=env["hq"], name="부문", metric="converted_premium", top_n=3, effective_from=YM)
    for bad in (dict(name=" "), dict(metric="x"), dict(top_n=0), dict(effective_from="2026-10"), dict(org_id=99999), dict(rewards=[(4, "상")]), dict(sector="etc")):
        with pytest.raises(ValueError):
            oa.save(con, **{**base, **bad})
    did = oa.save(con, **base, rewards=[(1, "30만 원")])
    oa.save(con, **{**base, "top_n": 5, "name": "새 이름"}, rewards=[(1, "50만 원")], division_id=did)
    d = oa.load(con, did)
    assert d["name"] == "새 이름" and d["top_n"] == 5 and d["rewards"] == {1: "50만 원"}
    log = con.execute("SELECT before_json, after_json FROM audit_log WHERE action='org_award_update' ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(log["before_json"])["top_n"] == 3 and json.loads(log["after_json"])["rewards"] == {"1": "50만 원"}


def test_promotion_update_changes_result_and_keeps_history(env):
    con, add = env["con"], env["add"]
    a1 = env["m1"][0]
    add(a1, "2026-10-03", 80_000)
    w = [("1주차", "2026-10-01", "2026-10-09")]
    pid = pm.create(con, period_ym=YM, name="주차", kind="weekly", windows=w, tiers=[(100_000, "fixed", 50_000, None)], by="t", publish=True)
    assert pm.progress(con, pm._load(con, pid), [a1], date(2026, 10, 9))[a1]["tier"] is None
    res = pm.update(con, pid, by="admin", name="주차 수정", windows=w, tiers=[(50_000, "fixed", 20_000, None), (100_000, "fixed", 50_000, None)])
    assert res["changed"] == ["name", "tiers"]
    p = pm._load(con, pid)
    assert p["status"] == "published" and p["name"] == "주차 수정" and pm.progress(con, p, [a1], date(2026, 10, 9))[a1]["amount"] == 20_000
    log = con.execute("SELECT actor, target, before_json FROM audit_log WHERE action='promotion_update' ORDER BY id DESC LIMIT 1").fetchone()
    assert log["actor"] == "admin" and "tiers" in log["target"] and json.loads(log["before_json"])["name"] == "주차"
    with pytest.raises(ValueError):
        pm.update(con, pid, by="admin", name="주차", windows=w, tiers=[])
    assert len(pm._load(con, pid)["tiers"]) == 2            # 잘못된 입력은 아무것도 바꾸지 않는다
    rate = pm.create(con, period_ym=YM, name="시책", kind="rate", windows=w, rates=[(None, "initial_premium", 2.0, "next_month")], by="t")
    pm.update(con, rate, by="admin", name="시책", kind="weekly", windows=w, tiers=[(1, "fixed", 1, None)], categories=["health"])
    r = pm._load(con, rate)
    assert r["kind"] == "rate" and len(r["rates"]) == 1 and not r["tiers"] and r["category_list"] == ["health"]   # 정률 시책은 유형과 지급률을 지킨다
