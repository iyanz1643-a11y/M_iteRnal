"""S2 계산: 기간별 실적, 구분별 집계, 업권과 조직 범위 랭킹, 조직 비교, 프로모션 비교, 지난 프로모션.

같은 사람의 같은 숫자는 화면이 달라도 같아야 한다. 업적 집계 규칙(성립한 달에 더하고 철회한 달에 뺀다)과
D-42(그 달 중간 퇴사자는 합계에 넣고 랭킹과 프로모션 달성 대상에서 뺀다)를 본다.
"""
import pathlib
import tempfile
from datetime import date

import pytest

from app import db, masters
from app.engine import orgs, performance, ranking
from app.engine import promotion as pm
from app.engine import results as rs
from app.web import auth

YM = "202610"


@pytest.fixture()
def env():
    con = db.init_db(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    masters.seed(con)
    auth.seed_accounts(con, "pw-12345678")
    hqs = [r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE level='hq' ORDER BY org_id")]
    branches = lambda hq: [r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE parent_id=? AND level='branch' ORDER BY org_id", (hq,))]
    members = lambda org: [r[0] for r in con.execute(
        """SELECT h.agent_id FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id
           WHERE h.org_id=? AND h.effective_to IS NULL AND a.job_type='fc' ORDER BY h.agent_id""", (org,))]
    b1, b2 = branches(hqs[0])[:2]
    other = branches(hqs[1])[0]
    n = [0]

    def add(agent, day, premium, status="inforce", sector="life", cat="health", count=1, withdrawn_on=None):
        p = con.execute("""SELECT p.product_id, p.insurer_id FROM product p JOIN insurer i ON i.insurer_id=p.insurer_id
                           WHERE i.sector=? AND p.product_category=? ORDER BY p.product_id LIMIT 1""", (sector, cat)).fetchone()
        n[0] += 1
        made = None if status in ("applied", "declined") else day
        con.execute("""INSERT INTO contract(insurer_id, policy_no, product_id, agent_id, application_no, application_date, contract_date, premium_monthly,
                                            converted_premium, policy_count, status, withdrawal_date, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (p["insurer_id"], f"T{n[0]:05d}", p["product_id"], agent, f"A{n[0]:05d}", day, made, premium, premium, count, status,
                     (withdrawn_on or day) if status == "withdrawn" else None, db.now()))
        con.commit()

    def ctx(agent_id=None):
        uid = con.execute("SELECT user_id FROM user_account WHERE " + ("login_id='admin'" if agent_id is None else "agent_id=?"),
                          () if agent_id is None else (agent_id,)).fetchone()[0]
        return auth.context(con, uid)
    all_ids = [r[0] for r in con.execute("SELECT agent_id FROM agent")]
    yield {"con": con, "hq": hqs[0], "hq2": hqs[1], "b1": b1, "b2": b2, "other": other, "m1": members(b1), "m2": members(b2),
           "mo": members(other), "add": add, "ctx": ctx, "all": all_ids, "ga": con.execute("SELECT org_id FROM org_unit WHERE level='ga'").fetchone()[0]}
    con.close()


def _leave(con, agent_id, day="2026-10-15"):
    """그 달 중간 퇴사. 사원현황 동기화가 하는 것처럼 상태를 바꾸고 소속 이력을 그 달에서 닫는다."""
    con.execute("UPDATE agent SET status='terminated', termination_date=? WHERE agent_id=?", (day, agent_id))
    con.execute("UPDATE agent_org_history SET effective_to=? WHERE agent_id=? AND effective_to IS NULL", (day[:4] + day[5:7], agent_id))
    con.commit()


PICK = ("applied_count", "applied_premium", "new_count", "withdrawn_count", "net_count", "monthly_premium", "converted_premium")


def _pick(d):
    return {k: d[k] for k in PICK}


# ------------------------------------------------------------------ 기간별 실적
def test_period_total_equals_the_sum_of_months(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    add(a1, "2026-10-03", 300_000)
    add(a1, "2026-10-20", 120_000, status="withdrawn", withdrawn_on="2026-11-05")    # 10월 성립, 11월 철회
    add(a1, "2026-11-11", 80_000, sector="nonlife", cat="property")
    add(a2, "2026-12-01", 50_000, status="applied")                                 # 청약은 예정 업적
    add(a2, "2026-12-02", 200_000, count=0.5)
    add(a2, "2026-09-30", 999_000)                                                   # 3분기
    ids = env["all"]
    months = [rs.period_summary(con, ids, "month", ym) for ym in ("202610", "202611", "202612")]
    q4 = rs.period_summary(con, ids, "quarter", "202612")
    assert q4["period"]["period_key"] == "2026Q4" and q4["period"]["start_ym"] == "202610" and not q4["period"]["partial"]
    summed = {k: sum(m["total"][k] for m in months) for k in PICK}
    assert _pick(q4["total"]) == summed
    assert [_pick(m) for m in q4["months"]] == [_pick(m["total"]) for m in months]           # 기간 안의 달마다 나눈 것도 같다
    assert q4["total"]["converted_premium"] == 300_000 + 120_000 - 120_000 + 80_000 + 200_000
    # 예정과 확정을 나눈다
    assert q4["total"]["planned"]["applied_count"] == 1 and q4["total"]["planned"]["applied_premium"] == 50_000
    assert q4["total"]["confirmed"]["net_count"] == 2 + 1 - 1 + 0.5 and "applied_count" not in q4["total"]["confirmed"]
    # 사람별 줄도 달의 합이고, 저장하는 업적 run 의 분기 줄과 같다
    row = {r["agent_id"]: r for r in q4["rows"]}
    for a in (a1, a2):
        assert _pick(row[a]) == {k: sum(next(r for r in m["rows"] if r["agent_id"] == a)[k] for m in months) for k in PICK}
    assert rs.freshness(con, "202612")["run_at"] is None
    performance.run(con, "202612")
    fresh = rs.freshness(con, "202612")
    assert fresh["run_at"] and not fresh["stale"]
    stored = {r["agent_id"]: dict(r) for r in con.execute(
        """SELECT s.* FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym='202612'
           WHERE s.period_type='quarter'""")}
    for a in (a1, a2):
        assert {k: stored[a][k] for k in ("applied_count", "new_count", "withdrawn_count", "net_count", "monthly_premium", "converted_premium")} == \
            {k: row[a][k] for k in ("applied_count", "new_count", "withdrawn_count", "net_count", "monthly_premium", "converted_premium")}
    # 반기와 연도 같은 규칙이다
    h2 = rs.period_summary(con, ids, "half", "202612")
    y = rs.period_summary(con, ids, "year", "202612")
    q3 = rs.period_summary(con, ids, "quarter", "202609")
    assert _pick(h2["total"]) == {k: q3["total"][k] + q4["total"][k] for k in PICK}
    assert y["total"]["converted_premium"] >= h2["total"]["converted_premium"] and len(y["months"]) == 12
    # 분기 중간이면 그 달까지의 누적이라고 적는다
    part = rs.period_summary(con, ids, "quarter", "202611")
    assert part["period"]["partial"] and part["period"]["label"] == "2026년 4분기 11월까지" and len(part["months"]) == 2


def test_breakdown_by_category_and_sector_adds_up(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    add(a1, "2026-10-03", 300_000, cat="health")
    add(a1, "2026-10-04", 100_000, sector="nonlife", cat="auto")
    add(a2, "2026-10-05", 70_000, cat="whole_life", status="withdrawn", withdrawn_on="2026-10-25")
    add(a2, "2026-10-06", 40_000, sector="nonlife", cat="property", status="applied")
    ids = env["m1"]
    total = rs.period_summary(con, ids, "month", YM)["total"]
    cat = rs.breakdown(con, ids, "month", YM, by="category")
    sec = rs.breakdown(con, ids, "month", YM, by="sector")
    assert _pick(cat["total"]) == _pick(sec["total"]) == _pick(total)
    cells = {r["key"]: r for r in cat["rows"]}
    assert cells["health"]["converted_premium"] == 300_000 and cells["auto"]["net_count"] == 1
    assert cells["whole_life"]["new_count"] == 1 and cells["whole_life"]["withdrawn_count"] == 1 and cells["whole_life"]["converted_premium"] == 0
    assert cells["property"]["planned"]["applied_count"] == 1 and cells["property"]["confirmed"]["new_count"] == 0
    assert {r["key"] for r in sec["rows"]} == {"life", "nonlife"}
    life = rs.period_summary(con, ids, "month", YM, sector="life")["total"]
    nonlife = rs.period_summary(con, ids, "month", YM, sector="nonlife")["total"]
    assert {k: life[k] + nonlife[k] for k in PICK} == _pick(total)
    # 범위 밖 사람의 계약은 섞이지 않는다
    add(env["mo"][0], "2026-10-07", 555_000)
    assert _pick(rs.period_summary(con, ids, "month", YM)["total"]) == _pick(total)


# ------------------------------------------------------------------ 랭킹
def test_ranking_sector_filter(env):
    con, add = env["con"], env["add"]
    a1, a2, a3 = env["m1"][:3]
    add(a1, "2026-10-03", 100_000, sector="nonlife", cat="auto")
    add(a1, "2026-10-04", 50_000, sector="nonlife", cat="property", count=1)
    add(a2, "2026-10-05", 400_000, sector="life", cat="health")
    add(a3, "2026-10-06", 120_000, sector="nonlife", cat="auto")
    add(a3, "2026-10-07", 120_000, sector="life", cat="health")
    pop = rs.rank_population(con, env["ga"], YM)
    top = lambda **kw: [(r["agent_id"], r["rank"], r["value"]) for r in rs.ranking(con, YM, agent_ids=pop, include_zero=False, **kw)]
    assert top(sector="nonlife") == [(a1, 1, 150_000), (a3, 2, 120_000)]
    assert top(sector="life") == [(a2, 1, 400_000), (a3, 2, 120_000)]
    assert top() == [(a2, 1, 400_000), (a3, 2, 240_000), (a1, 3, 150_000)]
    assert top(sector="nonlife", metric="net_count") == [(a1, 1, 2), (a3, 2, 1)]
    # 전체, 월 순위는 랭킹 run 의 순위와 같다
    ranking.run(con, YM)
    snap = {r["agent_id"]: (r["rank_no"], r["value"]) for r in con.execute(
        """SELECT s.agent_id, s.rank_no, s.value FROM ranking_snapshot s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done'
           WHERE s.period_type='month' AND s.period_key=? AND s.metric='recognized_premium' AND s.scope='all'""", (YM,))}
    for agent_id, rank, value in top():
        assert snap[agent_id] == (rank, value)


def test_org_scope_ranking_does_not_mix_outside(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    inside2, out = env["m2"][0], env["mo"][0]
    add(a1, "2026-10-03", 100_000)
    add(a2, "2026-10-04", 200_000)
    add(inside2, "2026-10-05", 300_000)
    add(out, "2026-10-06", 900_000)                               # 다른 본부
    others = [a for a in rs.rank_population(con, env["ga"], YM) if a not in (a1, a2, inside2, out)][:10]
    for i, a in enumerate(others):                                 # a1 이 상위 10명 밖으로 밀리게 한다
        add(a, "2026-10-10", 110_000 + i * 1_000)
    main = env["ctx"]()
    b1 = rs.ranking_for(con, main, env["b1"], YM)
    assert {r["agent_id"] for r in b1["rows"]} == set(orgs.members(con, env["b1"], YM))
    assert out not in {r["agent_id"] for r in b1["rows"]} and inside2 not in {r["agent_id"] for r in b1["rows"]}
    assert [r["agent_id"] for r in b1["rows"] if r["agent_id"] in (a1, a2)] == [a2, a1]
    hq = rs.ranking_for(con, main, env["hq"], YM)
    assert [r["agent_id"] for r in hq["rows"] if r["agent_id"] in (a1, a2, inside2)] == [inside2, a2, a1] and out not in {r["agent_id"] for r in hq["rows"]}
    ga = rs.ranking_for(con, main, env["ga"], YM)
    assert ga["rows"][0]["agent_id"] == out and ga["total"] == len(rs.rank_population(con, env["ga"], YM))
    # 설계사는 자기 조직과 그 위만 고르고, 상위 10명과 본인만 본다
    me = env["ctx"](a1)
    allowed = {o["org_id"] for o in rs.ranking_orgs(con, me)}
    assert {env["ga"], env["hq"], env["b1"]} <= allowed and env["hq2"] not in allowed and env["other"] not in allowed
    with pytest.raises(PermissionError):
        rs.ranking_for(con, me, env["hq2"], YM)
    seen = rs.ranking_for(con, me, env["ga"], YM)
    assert seen["me"]["agent_id"] == a1 and seen["me"]["rank"] > 10
    assert all(r["rank"] <= 10 or r["agent_id"] == a1 for r in seen["rows"]) and len(seen["rows"]) == 11 < seen["total"]
    assert len(rs.ranking_for(con, main, env["ga"], YM)["rows"]) == seen["total"]          # 메인 관리자는 전부 본다


def test_leaver_is_out_of_ranking_but_in_totals(env):
    con, add = env["con"], env["add"]
    a1, leaver = env["m1"][:2]
    add(a1, "2026-10-03", 100_000)
    add(leaver, "2026-10-04", 500_000)                             # 퇴사 전에 성립
    _leave(con, leaver)
    ids = set(orgs.members(con, env["b1"], YM, fc_only=False, active_only=False))
    assert leaver in ids
    s = rs.period_summary(con, ids, "month", YM)
    row = next(r for r in s["rows"] if r["agent_id"] == leaver)
    assert row["left"] and row["converted_premium"] == 500_000 and s["left"] == 1
    assert s["total"]["converted_premium"] == 600_000
    assert leaver not in {r["agent_id"] for r in rs.ranking(con, YM, agent_ids=ids)}
    assert leaver not in {r["agent_id"] for r in rs.ranking_for(con, env["ctx"](), env["b1"], YM)["rows"]}
    # 조직 비교에는 든다. 업적 run 의 조직 집계와 같은 값이다
    performance.run(con, YM)
    oc = {r["org_id"]: r for r in rs.org_compare(con, YM, org_ids=env["ctx"]()["org_ids"], level="branch")["rows"]}
    assert oc[env["b1"]]["left"] == 1 and oc[env["b1"]]["converted_premium"] == 600_000
    for level in ("ga", "hq", "branch", "center", "team"):
        for r in rs.org_compare(con, YM, org_ids=env["ctx"]()["org_ids"], level=level)["rows"]:
            roll = performance.org_rollup(con, r["org_id"], YM)
            assert (r["converted_premium"], r["net_count"]) == (roll["converted_premium"], roll["net_count"]), (level, r["name"])
    # 다음 달에는 소속 이력이 없어 합계에도 들지 않는다
    assert leaver not in {r["agent_id"] for r in rs.period_summary(con, ids, "month", "202611")["rows"]}


def test_org_compare_stays_in_scope_and_counts_activity(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    add(a1, "2026-10-03", 100_000)
    add(a2, "2026-10-04", 50_000, status="withdrawn", withdrawn_on="2026-10-10")    # 순건수 0 이라 가동이 아니다
    add(env["m2"][0], "2026-10-05", 70_000)
    head = con.execute("SELECT agent_id FROM org_manager WHERE org_id=? AND effective_to IS NULL", (env["hq"],)).fetchone()[0]
    scope = env["ctx"](head)["org_ids"]
    res = rs.org_compare(con, YM, org_ids=scope, level="branch")
    assert {r["org_id"] for r in res["rows"]} == set(orgs.descendants(con, env["hq"])) & {r["org_id"] for r in res["rows"]}
    assert env["other"] not in {r["org_id"] for r in res["rows"]}
    b1 = next(r for r in res["rows"] if r["org_id"] == env["b1"])
    assert b1["active"] == 1 and b1["active_rate"] == pytest.approx(1 / b1["headcount"]) and b1["path"]["hq"]
    assert res["rows"][0]["org_id"] == env["b1"] and res["rows"][0]["rank"] == 1
    assert rs.org_compare(con, YM, org_ids=scope, level="branch", parent_id=env["hq2"])["rows"] == []
    assert rs.org_compare(con, YM, org_ids=set(), level="branch")["rows"] == []


# ------------------------------------------------------------------ 프로모션 비교
def test_compare_puts_promotions_on_the_same_basis(env):
    con = env["con"]
    week = [("1주차", "2026-10-01", "2026-10-31")]
    pm.create(con, period_ym=YM, name="정률", kind="rate", windows=week, rates=[(None, "initial_premium", 0.5, "next_month"), (None, "initial_premium", 0.3, "inst13")],
              by="t", publish=True)
    pm.create(con, period_ym=YM, name="조건별", kind="rate", windows=week, rates=[("1", "initial_premium", 1.0, "next_month"), ("2", "initial_premium", 2.0, "next_month")],
              by="t", publish=True)
    pm.create(con, period_ym=YM, name="구간", kind="weekly", windows=week, per_contract_cap=200_000, pay_timing="inst13", by="t", publish=True,
              tiers=[(50_000, "fixed", 30_000, None), (100_000, "fixed", 70_000, None), (200_000, "fixed", 150_000, None)])
    pm.create(con, period_ym=YM, name="비율 구간", kind="monthly", windows=week, by="t", publish=True, tiers=[(100_000, "rate", 0.9, None)])
    pm.create(con, period_ym=YM, name="가동", kind="activity", windows=week, metric="policy_count", by="t", publish=True, tiers=[(3, "fixed", 100_000, None)])
    pm.create(con, period_ym=YM, name="여행", kind="travel", windows=week, metric="converted_premium", by="t", publish=True,
              tiers=[(600_000, "travel", None, "오사카")])
    pm.create(con, period_ym=YM, name="지사 몫", kind="rate", recipient_level="branch", windows=week, rates=[(None, "initial_premium", 2.0, "next_month")],
              by="t", publish=True)
    pm.create(con, period_ym=YM, name="초안", kind="rate", windows=week, rates=[(None, "initial_premium", 3.0, "next_month")], by="t")
    pm.create(con, period_ym=YM, name="끝남", kind="rate", windows=[("앞", "2026-10-01", "2026-10-05")], rates=[(None, "initial_premium", 4.0, "next_month")],
              by="t", publish=True)
    res = pm.compare(con, YM, ref=date(2026, 10, 20))
    rows = {r["name"]: r for r in res["rows"]}
    assert set(rows) == {"정률", "조건별", "구간", "비율 구간", "가동", "여행"}       # 설계사 몫의 게시 중, 끝나지 않은 것만
    assert rows["정률"]["per_unit"] == pytest.approx(80_000) and [p["per_unit"] for p in rows["정률"]["plan"]["distributed"]["parts"]] == [50_000, 30_000]
    assert rows["조건별"]["per_unit"] is None and "원문 확인" in rows["조건별"]["note"]
    t = rows["구간"]["plan"]["distributed"]["tiers"]
    assert [x["per_unit"] for x in t] == pytest.approx([60_000, 70_000, 75_000])
    assert rows["구간"]["per_unit"] == pytest.approx(75_000) and rows["구간"]["per_unit_entry"] == pytest.approx(60_000)
    assert "건당 200,000원까지 인정" in rows["구간"]["conditions"] and "13회차 유지 시 지급" in rows["구간"]["conditions"]
    assert rows["비율 구간"]["per_unit"] == pytest.approx(90_000)
    assert rows["가동"]["per_unit"] is None and not rows["가동"]["comparable"] and rows["여행"]["per_unit"] is None
    # 큰 순, 견줄 수 없는 줄은 뒤
    assert [r["name"] for r in res["rows"]][:3] == ["비율 구간", "정률", "구간"]
    assert [r["per_unit"] for r in res["rows"]][:3] == pytest.approx([90_000, 80_000, 75_000])
    assert all(r["per_unit"] is None for r in res["rows"][3:])
    # 실적 기준이 다른 줄은 다른 묶음이다
    assert {g["metric"]: len(g["rows"]) for g in res["groups"]} == {"premium_monthly": 4, "converted_premium": 1, "policy_count": 1}
    # D-21: 지금 금액은 배포안으로 보고 원안은 비운다. 모두 예상이다
    assert all(r["plan"]["original"] is None and r["plan_assumed"] and r["estimate"] for r in res["rows"])
    # 수령 계층은 부르는 쪽이 고른다. 함수는 역할을 받지 않는다
    assert [r["name"] for r in pm.compare(con, YM, ref=date(2026, 10, 20), recipient_level="branch")["rows"]] == ["지사 몫"]
    assert "끝남" in {r["name"] for r in pm.compare(con, YM, ref=date(2026, 10, 20), include_ended=True)["rows"]}


# ------------------------------------------------------------------ 지난 프로모션
def test_past_results_match_progress(env):
    con, add = env["con"], env["add"]
    a1, a2, leaver = env["m1"][:3]
    add(a1, "2026-10-02", 60_000)
    add(a1, "2026-10-09", 50_000)
    add(a2, "2026-10-03", 30_000)
    add(leaver, "2026-10-04", 300_000)
    tiers = [(50_000, "fixed", 30_000, None), (100_000, "fixed", 70_000, None), (200_000, "fixed", 150_000, None)]
    done = pm.create(con, period_ym=YM, name="1주차", kind="weekly", windows=[("1주차", "2026-10-01", "2026-10-09")], tiers=tiers, by="t", publish=True)
    rate = pm.create(con, period_ym=YM, name="정률", kind="rate", windows=[("기간", "2026-10-01", "2026-10-09")],
                     rates=[(None, "initial_premium", 0.5, "next_month")], by="t", publish=True)
    pm.create(con, period_ym=YM, name="진행 중", kind="weekly", windows=[("4주차", "2026-10-19", "2026-10-31")], tiers=tiers, by="t", publish=True)
    _leave(con, leaver, "2026-10-20")
    ref = date(2026, 10, 25)
    ids = env["m1"]
    past = pm.past(con, ids, ym=YM, ref=ref)
    assert [x["promo"]["promo_id"] for x in past] == [rate, done] or [x["promo"]["promo_id"] for x in past] == [done, rate]
    assert "진행 중" not in {x["promo"]["name"] for x in past}
    for x in past:
        p = pm._load(con, x["promo"]["promo_id"])
        live = pm.progress(con, p, [a for a in ids if a != leaver], ref)
        got = {r["agent_id"]: r for r in x["rows"]}
        assert leaver not in got and set(got) == set(live)                     # 퇴사자는 달성 대상에서 뺀다 (D-42)
        for a, r in live.items():
            assert (got[a]["value"], got[a]["amount"], got[a]["reward"]) == (r["value"], r["amount"], r["reward"])
            assert got[a]["tier_no"] == (r["tier"]["tier_no"] if r["tier"] else None)
            assert got[a]["settlement"] == "estimate" and got[a]["settlement_ko"] == "마감 전 예상"
        assert x["settlement"] == "estimate"
    wk = next(x for x in past if x["promo"]["promo_id"] == done)
    mine = {r["agent_id"]: r for r in wk["rows"]}
    assert mine[a1]["outcome"] == "achieved" and mine[a1]["tier_no"] == 2 and mine[a1]["amount"] == 70_000
    assert mine[a2]["outcome"] == "missed" and wk["summary"]["achieved"] == 1 and wk["summary"]["amount"] == 70_000
    rt = next(x for x in past if x["promo"]["promo_id"] == rate)
    assert {r["agent_id"]: r["amount"] for r in rt["rows"] if r["outcome"] == "achieved"} == {a1: 55_000, a2: 15_000}
    with pytest.raises(ValueError):
        pm.final_results(con, pm._load(con, done), ids, date(2026, 10, 9))     # 마지막 날에는 아직 끝나지 않았다
    assert {x["promo"]["promo_id"] for x in pm.past(con, ids, ref=ref)} == {done, rate}
