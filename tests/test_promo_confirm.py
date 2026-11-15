"""S3 프로모션 확정 기능: 여행 확정과 명단 범위(D-20), 13회차 확정, 창별 구간표, 확정 무르기(D-50), 기존 프로모션 그대로.

D-20 과 D-50 은 열린 결정이라 제안대로 둔 가정이다. 13회차 판정 규칙과 창별 구간표의 보상 규칙도 가정이다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import json
import pathlib
import tempfile
from datetime import date

import pytest

from app import db, masters
from app.engine import persist
from app.engine import promotion as pm
from app.web import auth

YM = "202610"
REF = date(2026, 11, 5)
OCT = [("10월", "2026-10-01", "2026-10-31")]


@pytest.fixture()
def env():
    con = db.init_db(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    masters.seed(con)
    auth.seed_accounts(con, "pw-12345678")
    hq = con.execute("SELECT org_id FROM org_unit WHERE level='hq' ORDER BY org_id").fetchone()[0]
    b1, b2 = [r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE parent_id=? AND level='branch' ORDER BY org_id", (hq,))][:2]
    members = lambda org: [r[0] for r in con.execute(
        """SELECT h.agent_id FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id
           WHERE h.org_id=? AND h.effective_to IS NULL AND a.job_type='fc' AND a.status='active' ORDER BY h.agent_id""", (org,))]
    head = lambda org: con.execute("SELECT agent_id FROM org_manager WHERE org_id=? AND effective_to IS NULL", (org,)).fetchone()[0]
    n = [0]

    def add(agent, day, premium, status="inforce", mode="monthly", paid=None):
        p = con.execute("""SELECT p.product_id, p.insurer_id FROM product p JOIN insurer i ON i.insurer_id=p.insurer_id
                           WHERE i.sector='life' AND p.product_category='health' ORDER BY p.product_id LIMIT 1""").fetchone()
        n[0] += 1
        cid = con.execute("""INSERT INTO contract(insurer_id, policy_no, product_id, agent_id, application_no, application_date, contract_date, premium_monthly,
                                                  converted_premium, policy_count, status, payment_mode, last_paid_ym, created_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (p["insurer_id"], f"T{n[0]:05d}", p["product_id"], agent, f"A{n[0]:05d}", day, day, premium, premium, 1, status, mode, paid,
                           db.now())).lastrowid
        con.commit()
        return cid

    def ctx(agent_id=None):
        uid = con.execute("SELECT user_id FROM user_account WHERE " + ("login_id='admin'" if agent_id is None else "agent_id=?"),
                          () if agent_id is None else (agent_id,)).fetchone()[0]
        return auth.context(con, uid)
    yield {"con": con, "hq": hq, "b1": b1, "b2": b2, "m1": members(b1), "m2": members(b2), "head": head, "add": add, "ctx": ctx,
           "all": [r[0] for r in con.execute("SELECT agent_id FROM agent")]}
    con.close()


TRAVEL = [(300_000, "goods", None, "건강검진권"), (600_000, "travel", None, "오사카 3박 4일")]


def _travel(con, **kw):
    return pm.create(con, period_ym=YM, name="여행", kind="travel", windows=OCT, tiers=TRAVEL, by="t", publish=True, **kw)


# ------------------------------------------------------------------ 여행 확정과 명단 범위
def test_travel_confirm_keeps_outcome_and_rank(env):
    con, add = env["con"], env["add"]
    a1, a2, a3, a4 = env["m1"][:4]
    other = env["m2"][0]
    add(a1, "2026-10-02", 700_000)
    add(a2, "2026-10-03", 700_000)                      # a1 과 같은 값이라 같은 순위
    add(a3, "2026-10-04", 350_000)
    add(a4, "2026-10-05", 100_000)                      # 구간에 닿지 못했다
    add(other, "2026-10-06", 1_000_000)
    pid = _travel(con)
    p = pm._load(con, pid)
    assert pm.in_kind(p) and not p["inst13"]
    est = {r["agent_id"]: r for r in pm.final_results(con, p, env["m1"], REF)["rows"]}
    # 순위는 재직자 전체에서 매긴다. 범위가 좁아도 같은 사람은 같은 순위다
    assert (est[a1]["rank"], est[a2]["rank"], est[a3]["rank"], est[a4]["rank"]) == (2, 2, 4, 5)
    only = pm.final_results(con, p, [a3], REF)["rows"]
    assert only[0]["rank"] == 4
    done = pm.confirm_results(con, pid, "admin", REF)
    got = {r["agent_id"]: r for r in done["rows"]}
    assert got[other]["rank"] == 1 and got[a1]["outcome"] == "achieved" and got[a4]["outcome"] == "missed"
    assert got[a1]["reward"] == "오사카 3박 4일" and got[a3]["reward"] == "건강검진권" and got[a1]["amount"] is None
    stored = {r["agent_id"]: r for r in con.execute("SELECT agent_id, rank_no, outcome, tier_no FROM promotion_result WHERE promo_id=?", (pid,))}
    assert (stored[a1]["rank_no"], stored[a2]["rank_no"], stored[a3]["rank_no"]) == (2, 2, 4)
    assert stored[a4]["outcome"] == "missed" and stored[a4]["tier_no"] is None
    zero = next(a for a in env["all"] if a not in (a1, a2, a3, a4, other) and a in stored)
    assert stored[zero]["rank_no"] is None                                     # 실적이 없으면 순위가 없다
    n_snap = con.execute("SELECT COUNT(*) FROM promotion_result_contract WHERE promo_id=?", (pid,)).fetchone()[0]
    assert n_snap == 5                                                         # 확정 때 센 계약을 남긴다
    add(a4, "2026-10-20", 900_000)                                             # 확정 뒤에 들어온 계약은 확정 명단을 바꾸지 않는다
    after = {r["agent_id"]: r for r in pm.final_results(con, p, env["m1"], REF)["rows"]}
    assert after[a4]["outcome"] == "missed" and after[a1]["rank"] == 2


def test_travel_roster_follows_d20_scope(env):
    con, add, ctx = env["con"], env["add"], env["ctx"]
    a1, a2, a3 = env["m1"][:3]
    other = env["m2"][0]
    for a, v in ((a1, 700_000), (a2, 350_000), (other, 800_000)):
        add(a, "2026-10-02", v)
    pid = _travel(con)
    p = pm._load(con, pid)
    main = pm.travel_roster(con, p, ctx(), env["all"], REF)
    assert {r["agent_id"] for r in main["rows"]} == {a1, a2, other} and main["phase"] == "ended" and main["settlement"] == "estimate"
    assert [r["rank"] for r in main["rows"]] == [1, 2, 3] and "amount" not in main["rows"][0]
    # 직속 관리자: 자기 지점의 설계사만 본다
    boss = ctx(env["head"](env["b1"]))
    assert {r["agent_id"] for r in pm.travel_roster(con, p, boss, boss["agent_ids"], REF)["rows"]} == {a1, a2}
    # 위 조직의 관리자: 범위 안이어도 직속이 아니면 보지 못한다
    top = ctx(env["head"](env["hq"]))
    assert a1 in top["agent_ids"] and pm.travel_roster(con, p, top, top["agent_ids"], REF)["rows"] == []
    # 설계사: 본인만
    assert [r["agent_id"] for r in pm.travel_roster(con, p, ctx(a1), ctx(a1)["agent_ids"], REF)["rows"]] == [a1]
    assert pm.travel_roster(con, p, ctx(a3), env["all"], REF)["rows"] == []      # 범위를 넓게 넘겨도 본인만이고 달성하지 않았다
    # 범위 밖은 메인 관리자 명단에서도 빠진다. 명단은 넘긴 목록 안에서만 만든다
    assert {r["agent_id"] for r in pm.travel_roster(con, p, ctx(), env["m1"], REF)["rows"]} == {a1, a2}
    # 진행 중이면 지금까지의 예상이다
    live = pm.travel_roster(con, p, ctx(), env["all"], date(2026, 10, 15))
    assert live["phase"] == "active" and live["settlement"] == "estimate" and len(live["rows"]) == 3
    # 확정하면 확정 명단이다
    pm.confirm_results(con, pid, "admin", REF)
    sure = pm.travel_roster(con, p, boss, boss["agent_ids"], REF)
    assert sure["settlement"] == "confirmed" and {r["agent_id"] for r in sure["rows"]} == {a1, a2}
    rate = pm.create(con, period_ym=YM, name="정률", kind="rate", windows=OCT, rates=[(None, "initial_premium", 0.5, "next_month")], by="t", publish=True)
    with pytest.raises(ValueError):
        pm.travel_roster(con, pm._load(con, rate), ctx(), env["all"], REF)


# ------------------------------------------------------------------ 13회차 판정
def test_judge13_kept_lapsed_pending():
    on = date(2026, 10, 20)
    kept = persist.judge13("2025-10-02", "monthly", "inforce", "202610", on)
    lapsed = persist.judge13("2025-10-02", "monthly", "lapsed", "202606", on)
    not_due = persist.judge13("2025-11-02", "monthly", "inforce", "202610", on)
    unpaid = persist.judge13("2025-10-02", "monthly", "inforce", "202609", on)
    assert (kept["judgement"], kept["reason"], kept["due_date"]) == ("kept", "paid", "2026-10-02")
    assert (lapsed["judgement"], lapsed["reason"]) == ("lapsed", "lapsed")
    assert (not_due["judgement"], not_due["reason"], not_due["judgement_ko"]) == ("pending", "not_due", "판정 전")
    assert (unpaid["judgement"], unpaid["reason"]) == ("pending", "unpaid")
    # 13회차를 낸 뒤 실효된 계약은 유지다
    assert persist.judge13("2025-10-02", "monthly", "lapsed", "202611", on)["judgement"] == "kept"
    # 일시납은 도래일이 지나고 실효가 아니면 유지다
    assert persist.judge13("2025-10-02", "single", "inforce", None, on)["reason"] == "single"
    assert persist.judge13(None, "monthly", "inforce", None, on)["reason"] == "no_date"


def test_inst13_confirm_pays_claws_back_and_waits(env):
    con, add = env["con"], env["add"]
    a1, a2, a3, a4 = env["m1"][:4]
    week = [("1주차", "2025-10-01", "2025-10-09")]
    c1 = add(a1, "2025-10-02", 60_000, paid="202610")                          # 유지
    c2 = add(a1, "2025-10-05", 50_000, status="lapsed", paid="202605")         # 실효
    c3 = add(a2, "2025-10-03", 120_000)                                        # 납입 회차 표로 유지 확인
    for i, ym in enumerate(["202510", "202511", "202512"] + [f"2026{m:02d}" for m in range(1, 11)], start=1):
        con.execute("INSERT INTO premium_payment(contract_id, period_ym, installment_no, amount) VALUES(?,?,?,?)", (c3, ym, i, 120_000))
    con.commit()
    c4 = add(a3, "2025-10-08", 100_000, paid="202609")                         # 판정 기준일에 아직 도래 전
    add(a4, "2025-10-01", 60_000, paid="202608")                               # 도래했지만 납입 확인 전
    tiers = [(50_000, "fixed", 30_000, None), (100_000, "fixed", 70_000, None)]
    pid = pm.create(con, period_ym="202510", name="1주차", kind="weekly", windows=week, tiers=tiers, pay_timing="inst13", by="t", publish=True)
    p = pm._load(con, pid)
    assert p["inst13"]
    on = date(2026, 10, 6)
    assert pm.inst13_results(con, p, env["all"], on)["available"] is False    # 결과 확정 전에는 판정하지 않는다
    with pytest.raises(ValueError):
        pm.confirm_inst13(con, pid, "admin", on)
    pm.confirm_results(con, pid, "admin", date(2025, 10, 20))
    res = pm.inst13_results(con, p, env["m1"], on)
    assert res["available"] and res["contracts_from"] == "confirm"
    got = {r["agent_id"]: r for r in res["rows"]}
    assert set(got) == {a1, a2, a3, a4}                                         # 결과 확정에서 달성한 사람만
    r1 = got[a1]
    assert (r1["decision"], r1["tier_no"], r1["tier_no_kept"], r1["amount_kept"], r1["clawback_amount"]) == ("clawback", 2, 1, 30_000, 40_000)
    assert {c["contract_id"]: c["judgement"] for c in r1["contracts"]} == {c1: "kept", c2: "lapsed"} and r1["n_lapsed"] == 1
    assert all(c["policy_tail"] and len(c["policy_tail"]) == 4 for c in r1["contracts"])
    assert got[a2]["decision"] == "pay" and got[a2]["amount_kept"] == got[a2]["amount"] and got[a2]["clawback_amount"] is None
    assert got[a3]["decision"] == "pending" and got[a3]["contracts"][0]["reason"] == "not_due"
    assert got[a4]["decision"] == "pending" and got[a4]["contracts"][0]["reason"] == "unpaid"
    assert res["summary"]["pay"] == 1 and res["summary"]["clawback"] == 1 and res["summary"]["pending"] == 2
    with pytest.raises(PermissionError):
        pm.confirm_inst13(con, pid, "M0001", on)
    first = pm.confirm_inst13(con, pid, "admin", on)
    assert first["n_new"] == 2
    assert con.execute("SELECT COUNT(*) FROM promotion_inst13 WHERE promo_id=?", (pid,)).fetchone()[0] == 2
    # 확정한 판정은 원장이 바뀌어도 그대로다
    con.execute("UPDATE contract SET status='lapsed' WHERE contract_id=?", (c3,))
    con.execute("DELETE FROM premium_payment WHERE contract_id=?", (c3,))
    con.execute("UPDATE contract SET last_paid_ym='202610' WHERE contract_id=?", (c4,))
    con.commit()
    later = date(2026, 10, 20)
    again = {r["agent_id"]: r for r in pm.inst13_results(con, p, env["m1"], later)["rows"]}
    assert again[a2]["decision"] == "pay" and again[a2]["settlement"] == "confirmed" and again[a2]["judged_by"] == "admin"
    assert again[a1]["settlement"] == "confirmed" and again[a1]["clawback_amount"] == 40_000
    assert again[a3]["decision"] == "pay" and again[a3]["settlement"] == "estimate"   # 도래해 납입이 확인되었다
    second = pm.confirm_inst13(con, pid, "admin", later)
    assert second["n_new"] == 1 and second["summary"]["pending"] == 1                  # a4 는 아직 판정 전
    assert pm.confirm_inst13(con, pid, "admin", later)["n_new"] == 0                  # 같은 사람을 두 번 남기지 않는다
    assert any(a["action"] == "promotion_inst13" for a in con.execute("SELECT action FROM audit_log"))
    # 13회차 조건이 없는 프로모션은 판정하지 않는다
    plain = pm.create(con, period_ym="202510", name="익월", kind="weekly", windows=week, tiers=tiers, pay_timing="next_month", by="t", publish=True)
    with pytest.raises(ValueError):
        pm.inst13_results(con, pm._load(con, plain), env["m1"], on)


def test_inst13_on_travel_drops_the_trip(env):
    con, add = env["con"], env["add"]
    a1 = env["m1"][0]
    add(a1, "2025-10-02", 400_000, paid="202610")
    add(a1, "2025-10-03", 300_000, status="lapsed", paid="202601")
    pid = pm.create(con, period_ym="202510", name="여행", kind="travel", windows=[("10월", "2025-10-01", "2025-10-31")], tiers=TRAVEL,
                    inst13_required=True, by="t", publish=True)
    p = pm._load(con, pid)
    assert p["inst13"] and p["inst13_required"] == 1
    pm.confirm_results(con, pid, "admin", date(2025, 11, 5))
    r = pm.inst13_results(con, p, [a1], date(2026, 10, 20))["rows"][0]
    assert (r["decision"], r["tier_no"], r["tier_no_kept"], r["reward_kept"]) == ("clawback", 2, 1, "건강검진권")
    assert r["amount"] is None and r["clawback_amount"] is None                 # 여행은 금액이 없다


# ------------------------------------------------------------------ 창별 구간표
REWARD = [(50_000, "fixed", 100_000, None), (150_000, "fixed", 300_000, None)]
LINKED = [("1주", "2026-10-01", "2026-10-09"), ("2주", "2026-10-10", "2026-10-18")]
PER_WINDOW = {1: [(30_000, "fixed", 0, None), (100_000, "fixed", 0, None)], 2: [(60_000, "fixed", 0, None), (200_000, "fixed", 0, None)]}


def test_window_tiers_decide_each_window_and_pay_from_common(env):
    con, add = env["con"], env["add"]
    a1, a2, a3 = env["m1"][:3]
    for a, v1, v2 in ((a1, 100_000, 60_000), (a2, 100_000, 200_000), (a3, 40_000, 70_000)):
        add(a, "2026-10-03", v1)
        add(a, "2026-10-12", v2)
    pid = pm.create(con, period_ym=YM, name="연계", kind="linked", windows=LINKED, tiers=REWARD, window_tiers=PER_WINDOW, by="t", publish=True)
    p = pm._load(con, pid)
    assert sorted(p["window_tiers"]) == [1, 2] and [t["min_value"] for t in p["tiers"]] == [50_000, 150_000]
    assert [t["min_value"] for t in pm.tiers_for(p, p["windows"][1])] == [60_000, 200_000]
    r = pm.progress(con, p, [a1, a2, a3], date(2026, 10, 25))
    assert (r[a1]["tier"]["tier_no"], r[a1]["amount"]) == (1, 100_000)        # 1주는 2구간, 2주는 1구간이라 낮은 쪽
    assert (r[a2]["tier"]["tier_no"], r[a2]["amount"]) == (2, 300_000)
    assert (r[a3]["tier"]["tier_no"], r[a3]["status"]) == (1, "achieved")     # 공통 표라면 1주 미달이지만 1주의 표로는 1구간이다
    assert [w["tier"]["tier_no"] for w in r[a3]["windows"]] == [1, 1]
    mid = pm.progress(con, p, [a1], date(2026, 10, 12))[a1]                    # 2주 진행 중: 2주의 표로 다음 구간까지 남은 값
    assert mid["next"]["gap"] == 140_000 and mid["next"]["reward"] == "300,000원" and mid["pct"] == pytest.approx(30)
    # 공통 구간표가 없으면 보상은 마지막 창의 표에서 읽는다
    own = {1: [(30_000, "fixed", 10_000, None), (100_000, "fixed", 20_000, None)], 2: [(60_000, "fixed", 50_000, None), (200_000, "fixed", 90_000, None)]}
    q = pm.create(con, period_ym=YM, name="연계2", kind="linked", windows=LINKED, window_tiers=own, by="t", publish=True)
    pq = pm._load(con, q)
    assert [t["reward_value"] for t in pq["tiers"]] == [50_000, 90_000]
    assert pm.progress(con, pq, [a2], date(2026, 10, 25))[a2]["amount"] == 90_000
    pm.set_original(con, q, by="admin", tiers={2: 120_000})                    # 원안은 보상을 읽는 표에 들어간다
    assert pm.original_amount(pq := pm._load(con, q), pm.progress(con, pq, [a2], date(2026, 10, 25))[a2]) == 120_000
    # 확정은 그때의 창별 구간표를 규칙으로 남긴다
    pm.confirm_results(con, pid, "admin", date(2026, 10, 25))
    rule = json.loads(pm.confirmation(con, pid)["rule_json"])
    assert sorted(rule["window_tiers"]) == ["1", "2"] and len(rule["tiers"]) == 2


def test_window_tiers_validation_and_edit(env):
    con = env["con"]
    with pytest.raises(ValueError):                                            # 합산에는 창별 구간표를 두지 않는다
        pm.create(con, period_ym=YM, name="x", kind="monthly", windows=LINKED, combine_rule="sum", tiers=REWARD, window_tiers=PER_WINDOW, by="t")
    with pytest.raises(ValueError):                                            # 구간 수가 달라서는 안 된다
        pm.create(con, period_ym=YM, name="x", kind="linked", windows=LINKED, tiers=REWARD[:1], window_tiers=PER_WINDOW, by="t")
    with pytest.raises(ValueError):                                            # 공통 표가 없으면 모든 창에 표가 있어야 한다
        pm.create(con, period_ym=YM, name="x", kind="linked", windows=LINKED, window_tiers={1: PER_WINDOW[1]}, by="t")
    with pytest.raises(ValueError):
        pm.create(con, period_ym=YM, name="x", kind="linked", windows=LINKED, tiers=REWARD, window_tiers={3: PER_WINDOW[1]}, by="t")
    # 창이 하나면 창별 구간표가 곧 공통 구간표다
    one = pm._load(con, pm.create(con, period_ym=YM, name="주", kind="weekly", windows=LINKED[:1], window_tiers={1: REWARD}, by="t"))
    assert one["window_tiers"] == {} and [t["min_value"] for t in one["tiers"]] == [50_000, 150_000]
    # 고치기: window_tiers 를 넘기지 않으면 그대로 두고, {} 를 넘기면 지운다
    pid = pm.create(con, period_ym=YM, name="연계", kind="linked", windows=LINKED, tiers=REWARD, window_tiers=PER_WINDOW, by="t", publish=True)
    out = pm.update(con, pid, by="admin", name="연계 고침", kind="linked", windows=LINKED, tiers=REWARD)
    assert out["changed"] == ["name"] and sorted(pm._load(con, pid)["window_tiers"]) == [1, 2]
    out = pm.update(con, pid, by="admin", name="연계 고침", kind="linked", windows=LINKED, tiers=REWARD, window_tiers={})
    assert out["changed"] == ["window_tiers"] and pm._load(con, pid)["window_tiers"] == {}


# ------------------------------------------------------------------ 확정 무르기
def test_revoke_keeps_the_record_and_allows_reconfirm(env):
    con, add = env["con"], env["add"]
    a1, a2 = env["m1"][:2]
    add(a1, "2026-10-02", 60_000)
    tiers = [(50_000, "fixed", 30_000, None), (100_000, "fixed", 70_000, None)]
    pid = pm.create(con, period_ym=YM, name="1주차", kind="weekly", windows=[("1주차", "2026-10-01", "2026-10-09")], tiers=tiers, pay_timing="inst13",
                    by="t", publish=True)
    p = pm._load(con, pid)
    ref = date(2026, 10, 20)
    with pytest.raises(ValueError):
        pm.revoke_confirmation(con, pid, "admin", "확정을 잘못했습니다")         # 무를 확정이 없다
    pm.confirm_results(con, pid, "admin", ref)
    first = pm.confirmation(con, pid)["confirm_id"]
    pm.confirm_inst13(con, pid, "admin", ref)
    add(a1, "2026-10-05", 50_000)                                             # 확정 뒤에 늦게 들어온 계약
    assert {r["agent_id"]: r for r in pm.final_results(con, p, [a1], ref)["rows"]}[a1]["value"] == 60_000
    with pytest.raises(PermissionError):
        pm.revoke_confirmation(con, pid, "M0001", "늦게 들어온 계약 반영")
    with pytest.raises(PermissionError):
        pm.revoke_confirmation(con, pid, {"role": "manager", "login_id": "admin"}, "늦게 들어온 계약 반영")
    with pytest.raises(ValueError):
        pm.revoke_confirmation(con, pid, "admin", "  짧음  ")                 # 사유는 5자 이상
    gone = pm.revoke_confirmation(con, pid, "admin", "늦게 들어온 계약 반영")
    assert gone["confirm_id"] == first and gone["revoked_by"] == "admin" and gone["revoked_at"] and gone["reason"] == "늦게 들어온 계약 반영"
    # 무른 기록은 지우지 않는다
    row = con.execute("SELECT voided_at, voided_by, void_reason FROM promotion_confirm WHERE confirm_id=?", (first,)).fetchone()
    assert row["voided_at"] and row["voided_by"] == "admin" and row["void_reason"] == "늦게 들어온 계약 반영"
    assert con.execute("SELECT COUNT(*) FROM promotion_result WHERE confirm_id=?", (first,)).fetchone()[0] > 0
    hist = pm.confirm_history(con, pid)
    assert len(hist) == 1 and not hist[0]["live"] and hist[0]["revoked_by"] == "admin" and hist[0]["reason"] == "늦게 들어온 계약 반영"
    # 무른 뒤에는 다시 계산한 값을 보인다
    now = pm.final_results(con, p, [a1, a2], ref)
    assert now["settlement"] == "estimate" and now["revoked"]["confirm_id"] == first and "무른 뒤" in now["estimate_note"]
    assert {r["agent_id"]: r for r in now["rows"]}[a1]["value"] == 110_000
    assert pm.inst13_results(con, p, [a1], ref)["available"] is False         # 무른 확정에 달린 13회차 판정도 보이지 않는다
    # 다시 확정한다
    again = pm.confirm_results(con, pid, "admin", ref)
    assert again["settlement"] == "confirmed" and again["revoked"] is None
    assert {r["agent_id"]: r for r in again["rows"]}[a1]["value"] == 110_000
    hist = pm.confirm_history(con, pid)
    assert [h["live"] for h in hist] == [True, False] and hist[0]["confirm_id"] != first
    audit = [r for r in con.execute("SELECT action, target, after_json FROM audit_log WHERE action='promotion_revoke'")]
    assert len(audit) == 1 and "늦게" not in (audit[0]["after_json"] or "")  # 변경 기록에는 사유의 길이만 남긴다


# ------------------------------------------------------------------ 기존 프로모션 그대로
def test_existing_promotions_behave_as_before(env):
    con, add = env["con"], env["add"]
    a1 = env["m1"][0]
    add(a1, "2026-10-03", 100_000)
    add(a1, "2026-10-12", 60_000)
    pid = pm.create(con, period_ym=YM, name="연계", kind="linked", windows=LINKED, tiers=REWARD, by="t", publish=True)
    p = pm._load(con, pid)
    assert p["window_tiers"] == {} and not p["inst13"] and pm.tiers_for(p, p["windows"][0]) is p["tiers"]
    assert all(t["window_seq"] is None for t in p["tiers"])
    r = pm.progress(con, p, [a1], date(2026, 10, 25))[a1]
    assert (r["tier"]["tier_no"], r["amount"], r["status"]) == (1, 100_000, "achieved")
    out = pm.update(con, pid, by="admin", name="연계", kind="linked", windows=LINKED, tiers=REWARD)
    assert out["changed"] == []
    assert pm.headline(p)["big"] == "300,000원" and pm.compare_row(p)["per_unit"] == pytest.approx(200_000)


def test_old_db_without_the_new_block_still_works(tmp_path):
    """[promotion_confirm] 묶음 앞까지만 있는 DB 에서도 등록, 조회, 확정이 되고, 확정할 때 ensure 가 칸과 표를 붙인다."""
    text = db.SCHEMA.read_text(encoding="utf-8")
    con = db.connect(tmp_path / "old.db")
    con.executescript(text[:text.index(pm.CONFIRM_BEGIN)])
    masters.seed(con)
    auth.seed_accounts(con, "pw-12345678")
    assert "window_seq" not in {r[1] for r in con.execute("PRAGMA table_info(promotion_tier)")}
    pid = pm.create(con, period_ym=YM, name="주", kind="weekly", windows=LINKED[:1], tiers=REWARD, by="t", publish=True)
    p = pm._load(con, pid)
    assert p["window_tiers"] == {} and pm.final_results(con, p, [1], REF)["settlement"] == "estimate"
    pm.confirm_results(con, pid, "admin", REF)
    assert "window_seq" in {r[1] for r in con.execute("PRAGMA table_info(promotion_tier)")}
    assert pm.confirmation(con, pid)["rule_json"]
    pm.ensure(con)
    con.close()
