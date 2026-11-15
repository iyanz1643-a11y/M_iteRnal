"""S2 계산: 기간별 실적, 구분별 집계, 업권과 조직 범위 랭킹, 조직 비교, 프로모션 비교, 지난 프로모션.
뒤에 덧붙인 것: 프로모션 실적 기준 설명(D-08), 지급 시점 표기 없음, 원안 칸(D-21), 확정 결과, 유지율, 유지 위험, 13회차 도래.

같은 사람의 같은 숫자는 화면이 달라도 같아야 한다. 업적 집계 규칙(성립한 달에 더하고 철회한 달에 뺀다)과
D-42(그 달 중간 퇴사자는 합계에 넣고 랭킹과 프로모션 달성 대상에서 뺀다)를 본다.
"""
import pathlib
import tempfile
from datetime import date

import pytest

from app import db, masters
from app.engine import orgs, performance, persist, ranking
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


# ------------------------------------------------------------------ 프로모션 실적 기준 (D-08 가정)
def _set(con, policy_no, **cols):
    con.execute(f"UPDATE contract SET {', '.join(f'{k}=?' for k in cols)} WHERE policy_no=?", (*cols.values(), policy_no))
    con.commit()


def test_basis_default_is_receipt_date_and_falls_back_to_contract_date(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    add(a1, "2026-09-28", 100_000)          # T00001 계약은 9월, 영수는 10월
    add(a1, "2026-10-05", 40_000)           # T00002 영수일자 없음. 계약일자로 센다
    add(a2, "2026-10-29", 70_000)           # T00003 계약은 10월, 영수는 11월
    add(a2, "2026-10-10", 50_000, status="withdrawn", withdrawn_on="2026-10-20")   # 철회는 세지 않는다
    _set(con, "T00001", receipt_date="2026-10-02")
    _set(con, "T00003", receipt_date="2026-11-02")
    win = [("10월", "2026-10-01", "2026-10-31")]
    tiers = [(50_000, "fixed", 10_000, None)]
    by_receipt = pm._load(con, pm.create(con, period_ym=YM, name="영수", kind="monthly", windows=win, tiers=tiers, by="t", publish=True))
    by_contract = pm._load(con, pm.create(con, period_ym=YM, name="계약", kind="monthly", windows=win, tiers=tiers, by="t", publish=True,
                                          date_basis="contract_date"))
    assert by_receipt["date_basis"] == pm.DEFAULT_DATE_BASIS == "receipt_date"
    ref = date(2026, 11, 10)
    r = pm.progress(con, by_receipt, [a1, a2], ref)
    c = pm.progress(con, by_contract, [a1, a2], ref)
    assert (r[a1]["value"], r[a2]["value"]) == (140_000, 0)
    assert (c[a1]["value"], c[a2]["value"]) == (40_000, 70_000)
    # basis 설명 칸. 화면이 그대로 보여 준다
    note = r[a1]["basis"]
    assert note == pm.basis_note(by_receipt) and r[a1]["date_basis"] == "receipt_date"
    assert "영수일자" in note and "계약일자" in note and "철회" in note and "업적 집계와 다를 수 있습니다" in note
    assert "영수일자" not in c[a1]["basis"] and "계약일자" in c[a1]["basis"]
    assert pm.compare_row(by_receipt)["basis"] == note
    assert "실적 보험료, 기준일 영수일자" in pm.conditions(by_receipt)


def test_receipt_basis_without_receipt_dates_gives_the_same_numbers(env):
    """샘플처럼 영수일자가 모두 비면 영수일자 기준과 계약일자 기준의 결과가 같다. 계산 규칙은 바꾸지 않았다."""
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    add(a1, "2026-10-03", 60_000)
    add(a2, "2026-10-12", 80_000, status="withdrawn", withdrawn_on="2026-11-02")
    add(a2, "2026-10-14", 30_000, status="applied")
    win = [("10월", "2026-10-01", "2026-10-31")]
    tiers = [(50_000, "fixed", 10_000, None)]
    ids = [a1, a2]
    ref = date(2026, 11, 10)
    got = [pm.progress(con, pm._load(con, pm.create(con, period_ym=YM, name=b, kind="monthly", windows=win, tiers=tiers, by="t", date_basis=b)),
                       ids, ref) for b in ("receipt_date", "contract_date")]
    strip = lambda res: {a: (x["value"], x["amount"], x["status"]) for a, x in res.items()}
    assert strip(got[0]) == strip(got[1]) == {a1: (60_000, 10_000, "achieved"), a2: (0, None, "missed")}


# ------------------------------------------------------------------ 지급 시점 표기 없음
def test_unspecified_pay_timing_reads_as_no_label(env):
    con = env["con"]
    week = [("1주차", "2026-10-01", "2026-10-31")]
    pid = pm.create(con, period_ym=YM, name="표기 없음", kind="rate", windows=week, pay_timing="unspecified",
                    rates=[(None, "initial_premium", 0.5, "unspecified")], by="t", publish=True)
    p = pm._load(con, pid)
    assert pm.TIMING_KO["unspecified"] == "표기 없음"
    cond = pm.conditions(p)
    assert "지급 시점 표기 없음" in cond and not any("unspecified" in x for x in cond)
    assert p["timing_ko"] is None                                 # 카드의 '… 지급' 꼬리를 붙이지 않는다
    row = pm.compare_row(p)
    assert row["plan"]["distributed"]["parts"][0]["timing_ko"] == "지급 시점 표기 없음"
    env["add"](env["m1"][0], "2026-10-03", 100_000)
    part = pm.progress(con, p, [env["m1"][0]], date(2026, 10, 20))[env["m1"][0]]["parts"][0]
    assert part["timing"] == "지급 시점 표기 없음"
    assert pm.timing_text("next_month") == "익월 지급" and pm.timing_text(None) == "지급 시점 표기 없음"


# ------------------------------------------------------------------ 원안 칸 (D-21 가정)
def test_original_plan_columns_feed_compare_and_survive_update(env):
    con = env["con"]
    week = [("1주차", "2026-10-01", "2026-10-31")]
    tiers = [(50_000, "fixed", 30_000, None), (100_000, "fixed", 70_000, None)]
    wk = pm.create(con, period_ym=YM, name="구간", kind="weekly", windows=week, tiers=tiers, by="t", publish=True)
    rt = pm.create(con, period_ym=YM, name="정률", kind="rate", windows=week, rates=[(None, "initial_premium", 0.5, "next_month"),
                                                                                    (None, "initial_premium", 0.3, "inst13")], by="t", publish=True)
    # 원안 칸이 없으면 지금처럼 None 이다
    assert pm.compare_row(pm._load(con, wk))["plan"]["original"] is None
    with pytest.raises(PermissionError):
        pm.set_original(con, wk, by="M0001", tiers={1: 40_000})                # 관리자는 넣지 못한다
    with pytest.raises(PermissionError):
        pm.set_original(con, wk, by={"role": "manager", "login_id": "admin"}, tiers={1: 40_000})
    pm.set_original(con, wk, by="admin", tiers={1: 40_000})                    # 2구간은 비워 둔다
    pm.set_original(con, rt, by="admin", rates={1: 0.6, 2: 0.4})
    row = pm.compare_row(pm._load(con, wk))
    assert row["has_original"] and row["per_unit"] == pytest.approx(70_000)   # 견주는 값은 배포안 그대로
    orig = row["plan"]["original"]
    assert [t["per_unit"] for t in orig["tiers"]] == [pytest.approx(80_000), None]
    assert orig["tiers"][1]["missing"] and orig["tiers"][1]["reward"] == "원문 확인"
    assert row["plan"]["distributed"]["tiers"][1]["reward"] == "70,000원"
    r = pm.compare_row(pm._load(con, rt))
    assert r["per_unit"] == pytest.approx(80_000) and r["plan"]["original"]["per_unit"] == pytest.approx(100_000)
    assert any(a["action"] == "promotion_original" for a in con.execute("SELECT action FROM audit_log"))
    # 고치기: 문턱이 같은 구간은 원안이 남고 문턱이 바뀐 구간은 원문 확인이 된다
    pm.update(con, wk, by="admin", name="구간", windows=week, tiers=[(50_000, "fixed", 35_000, None), (120_000, "fixed", 80_000, None)])
    t = pm._load(con, wk)["tiers"]
    assert [x["original_value"] for x in t] == [40_000, None]
    # compare() 도 원안을 읽는다. 원안을 보일지는 화면이 정한다
    rows = {x["name"]: x for x in pm.compare(con, YM, ref=date(2026, 10, 20))["rows"]}
    assert rows["정률"]["plan"]["original"]["per_unit"] == pytest.approx(100_000)
    pm.ensure(con)                                                            # 이미 있는 칸과 표는 건너뛴다
    pm.ensure(con)


# ------------------------------------------------------------------ 확정 결과
def test_confirm_results_freezes_values_and_only_main_admin_confirms(env):
    con, add = env["con"], env["add"]
    a1, a2, leaver = env["m1"][:3]
    add(a1, "2026-10-02", 60_000)
    add(a1, "2026-10-09", 50_000)
    add(a2, "2026-10-03", 30_000)
    add(leaver, "2026-10-04", 300_000)
    tiers = [(50_000, "fixed", 30_000, None), (100_000, "fixed", 70_000, None)]
    pid = pm.create(con, period_ym=YM, name="1주차", kind="weekly", windows=[("1주차", "2026-10-01", "2026-10-09")], tiers=tiers, by="t", publish=True)
    pm.set_original(con, pid, by="admin", tiers={2: 90_000})
    _leave(con, leaver, "2026-10-20")
    ref = date(2026, 10, 25)
    ids = env["m1"]
    p = pm._load(con, pid)
    before = pm.final_results(con, p, ids, ref)
    assert before["settlement"] == "estimate" and before["confirmed_by"] is None and "업적 집계와 다를 수 있습니다" in before["basis"]
    mine = {r["agent_id"]: r for r in before["rows"]}
    assert mine[a1]["amount"] == 70_000 and mine[a1]["original_amount"] == 90_000 and before["summary"]["original_amount"] == 90_000
    with pytest.raises(PermissionError):
        pm.confirm_results(con, pid, "M0001", ref)
    with pytest.raises(ValueError):
        pm.confirm_results(con, pid, "admin", date(2026, 10, 9))               # 아직 끝나지 않았다
    done = pm.confirm_results(con, pid, "admin", ref)
    assert done["settlement"] == "confirmed" and done["settlement_ko"] == "확정" and done["confirmed_by"] == "admin" and done["confirmed_at"]
    head = pm.confirmation(con, pid)
    assert head["n_rows"] == con.execute("SELECT COUNT(*) FROM agent WHERE status='active'").fetchone()[0]
    # 확정 뒤에 계약이 더 들어와도 확정한 값을 낸다
    add(a2, "2026-10-05", 80_000)
    live = pm.progress(con, p, [a2], ref)[a2]
    after = pm.final_results(con, p, ids, ref)
    got = {r["agent_id"]: r for r in after["rows"]}
    assert live["value"] == 110_000 and got[a2]["value"] == 30_000 and got[a2]["outcome"] == "missed"
    assert (got[a1]["value"], got[a1]["tier_no"], got[a1]["amount"], got[a1]["original_amount"]) == (110_000, 2, 70_000, 90_000)
    assert leaver not in got                                                   # 확정할 때 퇴사자는 대상이 아니었다 (D-42)
    assert all(r["settlement"] == "confirmed" and r["r"] is None for r in after["rows"])
    assert after["summary"]["achieved"] == 1 and after["summary"]["amount"] == 70_000
    assert pm.past(con, ids, ym=YM, ref=ref)[0]["settlement"] == "confirmed"
    with pytest.raises(ValueError):
        pm.confirm_results(con, pid, "admin", ref)                             # 두 번 확정하지 않는다
    assert any(a["action"] == "promotion_confirm" for a in con.execute("SELECT action FROM audit_log"))


# ------------------------------------------------------------------ 유지율
def test_persistency_rates_add_up_by_insurer_and_sector(env):
    con = env["con"]
    a1, a2, a3 = env["m1"][:3]
    rows = [(a1, 1, "202609", "p13", 8, 10), (a1, 3, "202609", "p13", 3, 5), (a2, 2, "202609", "p13", 9, 10),
            (a1, 1, "202608", "p13", 1, 10), (a1, 1, "202609", "p25", 4, 5)]
    con.executemany("INSERT INTO persistency(agent_id, insurer_id, period_ym, kind, numerator, denominator, rate) VALUES(?,?,?,?,?,?,0)", rows)
    con.commit()
    res = persist.rates(con, [a1, a2, a3], YM)
    p13, p25 = res["kinds"]
    assert (p13["kind"], p13["period_ym"], p13["kept"], p13["total"], p13["rate"]) == ("p13", "202609", 20, 25, 0.8)
    assert p13["rate"] == persist.rate(con, [a1, a2, a3], "p13", YM)[0]       # 기존 규칙과 같은 값
    assert {s["sector"]: (s["kept"], s["total"]) for s in p13["by_sector"]} == {"life": (17, 20), "nonlife": (3, 5)}
    assert sum(i["total"] for i in p13["by_insurer"]) == 25
    assert (p25["kept"], p25["total"], p25["rate"]) == (4, 5, 0.8)
    people = {x["agent_id"]: x for x in persist.by_agent(con, [a1, a2, a3], YM)}
    assert people[a1]["p13"]["rate"] == round(11 / 15, 4) and people[a3]["p13"]["rate"] is None
    # 자료 없는 범위와 빈 범위는 None 이지 0 이 아니다
    assert persist.rates(con, [a3], YM)["kinds"][0]["rate"] is None
    assert persist.rates(con, [], YM)["kinds"][0]["rate"] is None
    assert persist.rates(con, [a1], "202608")["kinds"][0]["period_ym"] == "202608"


def test_risk_counts_unpaid_and_lapse_risk_without_names(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    out = env["mo"][0]
    add(a1, "2026-03-05", 50_000)            # T00001 9월까지 냄: 정상
    add(a1, "2026-03-06", 50_000)            # T00002 8월까지: 9월 미납 1회
    add(a1, "2026-03-07", 50_000)            # T00003 6월까지: 실효 위험
    add(a2, "2026-03-08", 50_000)            # T00004 마지막 납입 달을 모른다
    add(a2, "2026-03-09", 50_000)            # T00005 연납은 보지 않는다
    add(a2, "2026-03-10", 50_000, status="lapsed")    # T00006 9월 실효
    add(out, "2026-03-11", 50_000)           # T00007 범위 밖
    con.execute("UPDATE contract SET policyholder_name='홍길동'")
    _set(con, "T00001", last_paid_ym="202609")
    _set(con, "T00002", last_paid_ym="202608")
    _set(con, "T00003", last_paid_ym="202606")
    _set(con, "T00005", last_paid_ym="202603", payment_mode="annual")
    _set(con, "T00006", status_date="2026-09-15")
    _set(con, "T00007", last_paid_ym="202601")
    con.execute("UPDATE contract SET payment_mode='monthly' WHERE payment_mode IS NULL")
    con.commit()
    res = persist.risk(con, [a1, a2], YM)
    assert res["counts"] == {"unpaid": 1, "lapse_risk": 1, "lapsed": 1, "no_data": 1}
    items = {x["policy_tail"]: x for x in res["items"]}
    assert set(items) == {"0002", "0003", "0006"}
    assert (items["0002"]["level"], items["0002"]["missed"]) == ("unpaid", 1)
    assert (items["0003"]["level"], items["0003"]["level_ko"], items["0003"]["missed"]) == ("lapse_risk", "실효 위험", 3)
    assert res["items"][0]["level"] == "lapse_risk"
    for x in res["items"]:                                                    # D-05: 이름과 증권번호 전체를 돌려주지 않는다
        assert "홍길동" not in repr(x) and not any(k in x for k in ("policyholder_name", "insured_name", "policy_no"))
    # 납입 회차 표로 마지막 납입 달을 메운다
    con.execute("INSERT INTO premium_payment(contract_id, period_ym, installment_no, amount) SELECT contract_id, '202607', 5, 1 FROM contract WHERE policy_no='T00004'")
    con.commit()
    again = persist.risk(con, [a1, a2], YM)["counts"]
    assert again["no_data"] == 0 and again["lapse_risk"] == 2


# ------------------------------------------------------------------ 13회차 도래
def test_inst13_due_dates_and_counts(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    add(a1, "2025-11-03", 10_000)            # T00001 도래 2026-11-03
    add(a1, "2025-11-03", 10_000)            # T00002 같은 날
    add(a2, "2025-11-30", 10_000)            # T00003 도래 2026-11-30, 실효 위험
    add(a2, "2025-12-31", 10_000)            # T00004 도래 2026-12-31
    add(a2, "2025-11-10", 10_000, status="withdrawn")   # 철회는 세지 않는다
    add(a2, "2025-11-11", 10_000)            # T00006 일시납은 뺀다
    add(a2, "2025-10-31", 10_000)            # T00007 범위 앞
    add(env["mo"][0], "2025-11-05", 10_000)  # 범위 밖 사람
    _set(con, "T00006", payment_mode="single")
    _set(con, "T00003", last_paid_ym="202607", payment_mode="monthly")
    assert persist.due13("2024-02-29") == date(2025, 2, 28)
    res = persist.inst13_due(con, [a1, a2], "2026-11-01", "2026-12-31", ym=YM)
    assert res["total"] == 4 and res["at_risk"] == 1
    assert res["days"] == [{"date": "2026-11-03", "count": 2, "at_risk": 0}, {"date": "2026-11-30", "count": 1, "at_risk": 1},
                           {"date": "2026-12-31", "count": 1, "at_risk": 0}]
    assert res["months"] == [{"ym": "202611", "count": 3, "at_risk": 1}, {"ym": "202612", "count": 1, "at_risk": 0}]
    assert all(set(d) == {"date", "count", "at_risk"} for d in res["days"])   # 날짜와 건수만
    assert persist.inst13_due(con, [a1], "2026-11-01", "2026-11-30")["total"] == 2
    view = persist.overview(con, [a1, a2], YM)                               # 기준일 10월 31일 다음 날부터 석 달
    assert view["due"]["date_from"] == "2026-11-01" and view["due"]["date_to"] == "2027-01-31" and view["due"]["total"] == 4
    assert view["risk"]["counts"]["lapse_risk"] == 1 and [k["kind"] for k in view["rates"]["kinds"]] == ["p13", "p25"]
