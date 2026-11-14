"""프로모션 달성 계산: 구간, 인정 한도, 연계 무효, 정률 예상액, 게시 범위."""
import pathlib
import tempfile
from datetime import date

import pytest

from app import db, masters, samples
from app.engine import promotion as pm
from app.loader import incentive_plan

YM = "202610"


@pytest.fixture()
def env():
    tmp = pathlib.Path(tempfile.mkdtemp())
    con = db.init_db(tmp / "t.db")
    masters.seed(con)
    agents = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE job_type='fc' ORDER BY agent_id LIMIT 2")]
    prod = lambda sector, cat: con.execute(
        "SELECT p.product_id, p.insurer_id FROM product p JOIN insurer i ON i.insurer_id=p.insurer_id WHERE i.sector=? AND p.product_category=? ORDER BY p.product_id LIMIT 1",
        (sector, cat)).fetchone()
    n = [0]

    def add(agent, day, premium, status="inforce", sector="life", cat="health", count=1):
        p = prod(sector, cat)
        n[0] += 1
        con.execute("""INSERT INTO contract(insurer_id, policy_no, product_id, agent_id, application_no, application_date, contract_date, premium_monthly,
                                            converted_premium, policy_count, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (p["insurer_id"], f"T{n[0]:05d}", p["product_id"], agent, f"A{n[0]:05d}", day, None if status in ("applied", "declined") else day,
                     premium, premium, count, status, db.now()))
        con.commit()
        return p["insurer_id"]
    yield {"con": con, "a1": agents[0], "a2": agents[1], "add": add, "tmp": tmp}
    con.close()


TIERS = [(50_000, "fixed", 30_000, None), (100_000, "fixed", 70_000, None), (200_000, "fixed", 150_000, None)]


def test_weekly_counts_only_confirmed_contracts_inside_the_window(env):
    con, a1, add = env["con"], env["a1"], env["add"]
    add(a1, "2026-10-02", 60_000)
    add(a1, "2026-10-09", 50_000)                       # 마지막 날은 포함
    add(a1, "2026-10-10", 100_000)                      # 창 밖
    add(a1, "2026-10-05", 200_000, status="withdrawn")  # 철회는 세지 않는다
    add(a1, "2026-10-05", 200_000, status="applied")    # 청약도 세지 않는다
    pid = pm.create(con, period_ym=YM, name="1주차", kind="weekly", windows=[("1주차", "2026-10-01", "2026-10-09")], tiers=TIERS, by="t", publish=True)
    r = pm.progress(con, pm._load(con, pid), [a1, env["a2"]], date(2026, 10, 5))
    assert r[a1]["value"] == 110_000 and r[a1]["tier"]["tier_no"] == 2 and r[a1]["amount"] == 70_000
    assert r[a1]["next"]["gap"] == 90_000 and r[a1]["status"] == "in_progress"
    assert r[env["a2"]]["value"] == 0 and r[env["a2"]]["tier"] is None and r[env["a2"]]["next"]["gap"] == 50_000
    ended = pm.progress(con, pm._load(con, pid), [a1, env["a2"]], date(2026, 10, 20))
    assert ended[a1]["status"] == "achieved" and ended[env["a2"]]["status"] == "missed"


def test_per_contract_cap(env):
    con, a1, add = env["con"], env["a1"], env["add"]
    add(a1, "2026-10-02", 90_000)
    add(a1, "2026-10-03", 30_000)
    pid = pm.create(con, period_ym=YM, name="한도", kind="weekly", windows=[("1주차", "2026-10-01", "2026-10-09")], tiers=TIERS, per_contract_cap=50_000, by="t")
    r = pm.progress(con, pm._load(con, pid), [a1], date(2026, 10, 9))[a1]
    assert r["value"] == 80_000 and r["tier"]["tier_no"] == 1          # 90,000 은 50,000 까지만 인정


def test_linked_takes_the_lower_tier_and_breaks_when_a_window_is_missed(env):
    con, a1, a2, add = env["con"], env["a1"], env["a2"], env["add"]
    add(a1, "2026-09-25", 60_000)
    add(a1, "2026-10-03", 120_000)
    add(a2, "2026-10-03", 300_000)                      # 앞 창을 놓쳤다
    pid = pm.create(con, period_ym=YM, name="연계", kind="linked", tiers=TIERS, by="t",
                    windows=[("9월 4주", "2026-09-20", "2026-09-30"), ("10월 1주", "2026-10-01", "2026-10-09")])
    p = pm._load(con, pid)
    assert p["combine_rule"] == "all"
    r = pm.progress(con, p, [a1, a2], date(2026, 10, 5))
    assert [w["tier"]["tier_no"] for w in r[a1]["windows"]] == [1, 2] and r[a1]["tier"]["tier_no"] == 1 and not r[a1]["broken"]
    assert r[a2]["broken"] and r[a2]["status"] == "missed" and r[a2]["tier"] is None and r[a2]["next"] is None
    early = pm.progress(con, p, [a2], date(2026, 9, 25))[a2]       # 앞 창이 아직 열려 있으면 무효가 아니다
    assert not early["broken"] and early["status"] == "in_progress"


def test_linked_cannot_rise_above_a_closed_window(env):
    con, a1, a2, add = env["con"], env["a1"], env["a2"], env["add"]
    add(a1, "2026-09-25", 60_000)                       # 앞 창은 1구간으로 끝났다
    add(a1, "2026-10-03", 500_000)                      # 뒤 창을 아무리 채워도 1구간이다
    add(a2, "2026-09-25", 250_000)                      # 앞 창 3구간
    add(a2, "2026-10-03", 120_000)                      # 뒤 창 2구간, 더 채우면 3구간까지 간다
    pid = pm.create(con, period_ym=YM, name="연계", kind="linked", tiers=TIERS, by="t",
                    windows=[("9월 4주", "2026-09-20", "2026-09-30"), ("10월 1주", "2026-10-01", "2026-10-09")])
    r = pm.progress(con, pm._load(con, pid), [a1, a2], date(2026, 10, 5))
    assert r[a1]["tier"]["tier_no"] == 1 and r[a1]["next"] is None and r[a1]["capped"] and r[a1]["amount"] == 30_000
    assert r[a2]["tier"]["tier_no"] == 2 and r[a2]["next"]["gap"] == 80_000 and not r[a2]["capped"]


def test_sum_rule_and_travel_reward_has_no_amount(env):
    con, a1, add = env["con"], env["a1"], env["add"]
    add(a1, "2026-10-03", 200_000)
    add(a1, "2026-11-20", 150_000)
    pid = pm.create(con, period_ym=YM, name="여행", kind="travel", by="t", tiers=[(300_000, "travel", None, "오사카 3박 4일")],
                    windows=[("10월", "2026-10-01", "2026-10-31"), ("11월", "2026-11-01", "2026-11-30")])
    p = pm._load(con, pid)
    assert p["combine_rule"] == "sum"
    r = pm.progress(con, p, [a1], date(2026, 11, 30))[a1]
    assert r["value"] == 350_000 and r["reward"] == "오사카 3박 4일" and r["amount"] is None and r["status"] == "achieved"
    assert pm.headline(p)["big"] == "오사카 3박 4일"


def test_rate_promotion_filters_by_category_sector_and_insurer(env):
    con, a1, add = env["con"], env["a1"], env["add"]
    life = add(a1, "2026-10-03", 100_000, cat="health")
    add(a1, "2026-10-04", 50_000, cat="whole_life")
    add(a1, "2026-10-05", 70_000, sector="nonlife", cat="health")
    w = [("시책 기간", "2026-10-01", "2026-10-31")]
    both = [(None, "initial_premium", 0.5, "next_month"), (None, "initial_premium", 1.5, "inst13")]
    pid = pm.create(con, period_ym=YM, name="건강", kind="rate", windows=w, rates=both, sector="life", categories=["health"], by="t")
    r = pm.progress(con, pm._load(con, pid), [a1], date(2026, 10, 31))[a1]
    assert r["value"] == 100_000 and r["amount"] == 200_000 and [x["amount"] for x in r["parts"]] == [50_000, 150_000]
    pid2 = pm.create(con, period_ym=YM, name="전체", kind="rate", windows=w, rates=both[:1], insurer_id=life, by="t")
    assert pm.progress(con, pm._load(con, pid2), [a1], date(2026, 10, 31))[a1]["value"] == 150_000
    opts = [("1", "initial_premium", 2.5, "year1"), ("2", "initial_premium", 1.5, "year1")]
    pid3 = pm.create(con, period_ym=YM, name="조건별", kind="rate", windows=w, rates=opts, by="t")
    p3 = pm._load(con, pid3)
    assert pm.progress(con, p3, [a1], date(2026, 10, 31))[a1]["amount"] is None and pm.headline(p3)["big"] is None   # 숫자 하나로 줄이지 않는다


def test_count_metric(env):
    con, a1, add = env["con"], env["a1"], env["add"]
    add(a1, "2026-10-03", 10_000, sector="nonlife", cat="health")
    add(a1, "2026-10-04", 10_000, sector="nonlife", cat="health", count=0.5)
    pid = pm.create(con, period_ym=YM, name="손보 가동", kind="activity", windows=[("10월", "2026-10-01", "2026-10-31")], metric="policy_count",
                    sector="nonlife", tiers=[(1, "fixed", 50_000, None), (3, "fixed", 150_000, None)], by="t")
    r = pm.progress(con, pm._load(con, pid), [a1], date(2026, 10, 10))[a1]
    assert r["value"] == 1.5 and r["tier"]["tier_no"] == 1 and r["next"]["gap"] == 1.5


def test_listing_shows_agents_only_published_fc_promotions(env):
    con = env["con"]
    w = [("10월", "2026-10-01", "2026-10-31")]
    draft = pm.create(con, period_ym=YM, name="초안", kind="weekly", windows=w, tiers=TIERS, by="t")
    live = pm.create(con, period_ym=YM, name="게시", kind="weekly", windows=w, tiers=TIERS, by="t", publish=True)
    hq = pm.create(con, period_ym=YM, name="총괄 몫", kind="rate", windows=w, rates=[(None, "initial_premium", 0.5, "inst13")], recipient_level="hq", by="t", publish=True)
    other = pm.create(con, period_ym="202608", name="8월", kind="weekly", windows=[("8월", "2026-08-01", "2026-08-09")], tiers=TIERS, by="t", publish=True)
    ids = lambda role, **kw: {p["promo_id"] for p in pm.listing(con, YM, role, **kw)}
    assert ids("agent") == {live} == ids("manager")
    assert ids("main") == {live, hq} and ids("main", include_unpublished=True) == {draft, live, hq}
    assert other not in ids("main", include_unpublished=True)
    pm.set_status(con, live, "withdrawn", "t")
    assert ids("agent") == set()
    with pytest.raises(ValueError):
        pm.delete_draft(con, hq)                       # 게시 중인 것은 지울 수 없다
    pm.delete_draft(con, draft)
    assert draft not in ids("main", include_unpublished=True)


def test_publish_from_plan_line(env):
    con = env["con"]
    res = incentive_plan.load(con, samples.write_incentive_plan(env["tmp"]), YM)
    line = lambda where: con.execute(f"SELECT line_id FROM incentive_plan_line WHERE plan_doc_id=? AND recipient_level='fc' AND {where} ORDER BY line_id LIMIT 1",
                                     (res["plan_doc_id"],)).fetchone()[0]
    auto = line("parse_status='auto' AND line_kind='rate_promo'")
    pid = pm.publish_from_line(con, auto, "t", categories=["health"])
    p = pm._load(con, pid)
    assert p["status"] == "published" and p["kind"] == "rate" and p["rates"] and p["category_list"] == ["health"] and p["source_line_id"] == auto
    assert con.execute("SELECT review_status FROM incentive_plan_line WHERE line_id=?", (auto,)).fetchone()[0] == "approved"
    with pytest.raises(ValueError):
        pm.publish_from_line(con, auto, "t")           # 같은 줄을 두 번 게시하지 않는다
    with pytest.raises(ValueError):
        pm.publish_from_line(con, line("parse_status='manual'"), "t")


def test_create_rejects_bad_input(env):
    con = env["con"]
    w = [("10월", "2026-10-01", "2026-10-31")]
    for kw in (dict(kind="weekly", windows=w, tiers=[]), dict(kind="weekly", windows=[], tiers=TIERS),
               dict(kind="weekly", windows=[("x", "2026-10-09", "2026-10-01")], tiers=TIERS),
               dict(kind="nope", windows=w, tiers=TIERS), dict(kind="weekly", windows=w, tiers=[(1, "cash", 1, None)]),
               dict(kind="weekly", windows=w, tiers=[(1, "fixed", 1, None), (1, "fixed", 2, None)])):
        with pytest.raises(ValueError):
            pm.create(con, period_ym=YM, name="x", by="t", **kw)


def test_ref_date():
    assert pm.ref_date("202610") == date(2026, 10, 31)
    assert pm.ref_date(date.today().strftime("%Y%m")) == date.today()
