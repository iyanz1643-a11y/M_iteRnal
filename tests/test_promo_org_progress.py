"""프로모션 조직별 진척과 조건 확장: 조직 합계, 범위 밖 제외, 가중 인정 한도, 조건 묶음의 모두와 또는, 상품 이름 거르기,
건당 한도, 청약 기준, 딸린 시책과 조직 달성 인원 문턱, 달성 인원 순위, 13회차 유지율 표시, 퇴사자(D-42).

세 프로모션(흥국화재 BOOM-UP, DB생명 여행시상과 이터널 Convention, 삼성화재 드림+)은 샘플 보험사로 만든 시험용이다.
금액은 공문과 포스터에서 읽힌 숫자를 시험에만 쓴다. 운영 DB 에는 넣지 않는다.
D-84 에서 D-92 는 제안대로 둔 가정이다. 실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import pathlib
import tempfile
from datetime import date

import pytest

from app import db, masters
from app.engine import orgs
from app.engine import promotion as pm
from app.web import auth

YM = "202610"
Q4 = [("10월", "2026-10-01", "2026-10-31"), ("11월", "2026-11-01", "2026-11-30"), ("12월", "2026-12-01", "2026-12-31")]
AFTER = date(2027, 1, 5)
MID = date(2026, 11, 17)


@pytest.fixture()
def env():
    con = db.init_db(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    masters.seed(con)
    auth.seed_accounts(con, "pw-12345678")
    root = con.execute("SELECT org_id, path_key FROM org_unit WHERE parent_id IS NULL").fetchone()
    hqs = [dict(r) for r in con.execute("SELECT org_id, path_key FROM org_unit WHERE parent_id=? ORDER BY org_id", (root["org_id"],))]
    fcs = lambda org: [r[0] for r in con.execute(
        """SELECT h.agent_id FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id
           WHERE h.org_id=? AND h.effective_to IS NULL AND a.job_type='fc' AND a.status='active' ORDER BY h.agent_id""", (org,))]
    branches = {h["org_id"]: [r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE parent_id=? AND level='branch' ORDER BY org_id",
                                                         (h["org_id"],))] for h in hqs}
    ins = {r["name"]: r["insurer_id"] for r in con.execute("SELECT insurer_id, name FROM insurer")}
    prod = {r["product_name"]: r["product_id"] for r in con.execute("SELECT product_id, product_name FROM product")}
    n = [0]

    def add(agent, day, premium, *, insurer="샘플생명", product="샘플생명 건강보험", raw=None, status="inforce", app_day=None, self_family=None, paid=None):
        n[0] += 1
        pid = prod.get(product) if product else None
        cid = con.execute(
            """INSERT INTO contract(insurer_id, policy_no, product_id, product_code_raw, agent_id, application_no, application_date, contract_date,
                                    premium, premium_monthly, converted_premium, policy_count, status, payment_mode, last_paid_ym, self_family, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ins[insurer], f"T{n[0]:05d}", pid, raw, agent, f"A{n[0]:05d}", app_day or day, day if status != "applied" else None,
             premium, premium, premium, 1, status, "monthly", paid, self_family, db.now())).lastrowid
        con.commit()
        return cid

    def everyone():
        return [r[0] for r in con.execute("SELECT agent_id FROM agent")]

    yield {"con": con, "root": dict(root), "hqs": hqs, "branches": branches, "fcs": fcs, "add": add, "all": everyone}
    con.close()


def _tiers(*rows):
    return [(m, "travel", None, label) for m, label in rows]


# ------------------------------------------------------------------ 시험용 세 프로모션
def _heungkuk(con, **kw):
    """흥국화재 BOOM-UP 꼴. 청약 기준, 조건 1 월별 5/10/15만, 조건 2 합산 40만, 둘 다(both). 13회차 유지율 95% 표시."""
    return pm.create(con, period_ym=YM, name="시험 BOOM-UP", kind="travel", windows=Q4, combine_rule="both", insurer_name="한빛손해",
                     insurer_id=con.execute("SELECT insurer_id FROM insurer WHERE name='한빛손해'").fetchone()[0],
                     categories=["health"], date_basis="application_date", tiers=_tiers((400_000, "여행")),
                     window_tiers={1: _tiers((50_000, "여행")), 2: _tiers((100_000, "여행")), 3: _tiers((150_000, "여행"))},
                     rules={"count_basis": "application", "retention_min": 95, "retention_ref": "2027-12-31"}, by="t", publish=True, **kw)


def _db_life(con, **kw):
    """DB생명 여행시상 꼴. 조건 1 월별 10/30/40만, 조건 2 합산 100만, 하나만(either). 1, 2주차 2배는 조건 1에만, 원보험료 10만까지."""
    rules = {"boost": {"factor": 2, "month_days": [1, 14], "cap": 100_000, "applies_to": "each"}, "exclude_self": True}
    rules.update(kw.pop("rules", {}))
    return pm.create(con, period_ym=YM, name="시험 여행시상", kind="travel", windows=Q4, combine_rule="either", insurer_name="샘플생명",
                     insurer_id=con.execute("SELECT insurer_id FROM insurer WHERE name='샘플생명'").fetchone()[0], categories=["health"],
                     date_basis="contract_date", tiers=_tiers((1_000_000, "싱가포르")),
                     window_tiers={1: _tiers((100_000, "싱가포르")), 2: _tiers((300_000, "싱가포르")), 3: _tiers((400_000, "싱가포르"))},
                     rules=rules, by="t", publish=True, **kw)


DREAM = {1: [100_000, 200_000, 300_000, 500_000, 800_000], 2: [200_000, 300_000, 500_000, 1_000_000, 1_500_000],
         3: [200_000, 300_000, 500_000, 1_000_000, 1_500_000]}
DREAM_WINDOWS = [("10월 19일에서 31일", "2026-10-19", "2026-10-31"), ("11월", "2026-11-01", "2026-11-30"), ("12월", "2026-12-01", "2026-12-31")]
EXCLUDE = ["단체", "실손", "펫", "캣", "재물", "The 라이프업"]


def _samsung(con):
    """삼성화재 드림+ 꼴. 창별 구간표 5단계, 낮은 쪽 구간. 보상은 선택형이라 단계만 낸다. 제외 상품과 해지(품보) 제외, 13회차 유지."""
    wt = {s: [(m, "goods", None, f"{i}단계 선택형") for i, m in enumerate(ms, start=1)] for s, ms in DREAM.items()}
    return pm.create(con, period_ym=YM, name="시험 드림+", kind="linked", windows=DREAM_WINDOWS, combine_rule="all", window_tiers=wt,
                     insurer_name="미래화재", insurer_id=con.execute("SELECT insurer_id FROM insurer WHERE name='미래화재'").fetchone()[0],
                     date_basis="contract_date", inst13_required=True,
                     rules={"product_exclude": EXCLUDE, "drop_statuses": ["cancelled"]}, by="t", publish=True)


# ------------------------------------------------------------------ 1. 조직 합계와 사람별 합
def test_org_rows_add_up_to_person_values(env):
    con, add = env["con"], env["add"]
    hq1, hq2 = env["hqs"]
    b1 = env["branches"][hq1["org_id"]][0]
    b3 = env["branches"][hq2["org_id"]][0]
    m1, m3 = env["fcs"](b1), env["fcs"](b3)
    for i, a in enumerate(m1[:4]):
        for mon in ("10", "11", "12"):
            add(a, f"2026-{mon}-20", 120_000 + i * 150_000)
    add(m3[0], "2026-10-20", 500_000)
    add(m3[0], "2026-11-20", 500_000)
    pid = _db_life(con)
    p = pm._load(con, pid)
    scope = env["all"]()
    res = pm.org_progress(con, p, env["root"]["path_key"], scope, AFTER, near_pct=30)
    per = pm.progress(con, p, scope, AFTER)
    rows = {r["path_key"]: r for r in res["rows"]}
    assert {hq1["path_key"], hq2["path_key"]} <= set(rows)
    for r in res["rows"]:
        assert r["value"] == pytest.approx(sum(per[a]["value"] for a in r["agent_ids"] + r["left_ids"]))
        assert r["n_achieved"] == sum(1 for a in r["agent_ids"] if pm.is_achieved(p, per[a]))
        assert r["n_target"] == r["n_active"] + r["n_inactive"]
        # 같은 사람의 같은 숫자: 한 사람만 넣은 progress 와 같다
        for a in r["agent_ids"][:3]:
            assert pm.progress(con, p, [a], AFTER)[a]["value"] == per[a]["value"]
    assert res["total"]["value"] == pytest.approx(sum(r["value"] for r in res["rows"]))
    assert res["total"]["n_target"] == sum(r["n_target"] for r in res["rows"])
    # 바로 아래 조직만 줄이 된다. 손자 조직은 위 조직 줄에 합쳐진다
    assert all(r["path_key"].count(">") == 0 for r in res["rows"])
    assert res["windows"][0]["label"] == "10월" and res["total"]["windows"] is not None
    assert res["amounts"]["in_kind"] and res["amounts"]["total"] is None
    # 본부 아래로 내려가면 지점 줄이다
    down = pm.org_progress(con, p, hq1["path_key"], scope, AFTER, near_pct=30)
    assert sum(r["value"] for r in down["rows"]) == pytest.approx(rows[hq1["path_key"]]["value"])


def test_org_rank_by_achievers_and_direct_row_has_no_rank(env):
    con, add = env["con"], env["add"]
    hq1, hq2 = env["hqs"]
    b1 = env["branches"][hq1["org_id"]][0]
    b3 = env["branches"][hq2["org_id"]][0]
    for a in env["fcs"](b1)[:3]:
        for mon in ("10", "11", "12"):
            add(a, f"2026-{mon}-20", 450_000)
    a3 = env["fcs"](b3)[0]
    for mon in ("10", "11", "12"):
        add(a3, f"2026-{mon}-20", 450_000)
    p = pm._load(con, _db_life(con))
    res = pm.org_progress(con, p, env["root"]["path_key"], env["all"](), AFTER, near_pct=30)
    rows = {r["path_key"]: r for r in res["rows"]}
    assert (rows[hq1["path_key"]]["n_achieved"], rows[hq1["path_key"]]["rank"]) == (3, 1)
    assert (rows[hq2["path_key"]]["n_achieved"], rows[hq2["path_key"]]["rank"]) == (1, 2)
    assert all(r["rank"] is None for r in res["rows"] if r["direct"])


# ------------------------------------------------------------------ 2. 범위 밖 제외
def test_people_outside_scope_are_never_counted(env):
    con, add = env["con"], env["add"]
    hq1, hq2 = env["hqs"]
    b1 = env["branches"][hq1["org_id"]][0]
    b3 = env["branches"][hq2["org_id"]][0]
    inside, outside = env["fcs"](b1)[0], env["fcs"](b3)[0]
    for a in (inside, outside):
        for mon in ("10", "11", "12"):
            add(a, f"2026-{mon}-20", 450_000)
    p = pm._load(con, _db_life(con))
    scope = orgs.members(con, b1, "202611", fc_only=False, active_only=False)
    res = pm.org_progress(con, p, env["root"]["path_key"], scope, AFTER, near_pct=30)
    assert [r["path_key"] for r in res["rows"]] == [hq1["path_key"]]           # 범위 안 사람이 없는 본부는 줄도 없다
    assert res["total"]["n_achieved"] == 1 and outside not in res["rows"][0]["agent_ids"]
    full = pm.org_progress(con, p, env["root"]["path_key"], scope, AFTER, near_pct=30, keep_empty=True)
    empty = next(r for r in full["rows"] if r["path_key"] == hq2["path_key"])
    assert empty["n_target"] == 0 and empty["value"] == 0 and empty["achieve_rate"] is None
    # 범위가 비면 아무도 세지 않는다
    assert pm.org_progress(con, p, env["root"]["path_key"], [], AFTER, near_pct=30)["total"]["n_target"] == 0
    with pytest.raises(ValueError):
        pm.org_progress(con, p, "없는 조직", scope, AFTER)


def test_retired_agent_counts_in_total_but_not_in_targets(env):
    con, add = env["con"], env["add"]
    b1 = env["branches"][env["hqs"][0]["org_id"]][0]
    a, b = env["fcs"](b1)[:2]
    for x in (a, b):
        for mon in ("10", "11", "12"):
            add(x, f"2026-{mon}-20", 450_000)
    con.execute("UPDATE agent SET status='terminated', termination_date='2026-12-20' WHERE agent_id=?", (b,))
    con.commit()
    p = pm._load(con, _db_life(con))
    res = pm.org_progress(con, p, env["hqs"][0]["path_key"], env["all"](), AFTER, near_pct=30)
    row = next(r for r in res["rows"] if b in r["left_ids"])
    assert a in row["agent_ids"] and b not in row["agent_ids"]
    assert row["n_left"] == 1 and row["value_left"] == pytest.approx(450_000 * 3)
    assert row["n_achieved"] == 1                                               # 퇴사자는 달성 대상에서 빠진다 (D-42)


def test_near_and_inactive_counts(env):
    con, add = env["con"], env["add"]
    b1 = env["branches"][env["hqs"][0]["org_id"]][0]
    near_one, far_one, idle = env["fcs"](b1)[:3]
    add(near_one, "2026-10-20", 80_000)                  # 10월 창 문턱 10만의 30% 안
    add(far_one, "2026-10-21", 10_000)
    p = pm._load(con, _db_life(con))
    res = pm.org_progress(con, p, env["hqs"][0]["path_key"], [near_one, far_one, idle], date(2026, 10, 25), near_pct=30)
    row = res["rows"][0]
    assert (row["n_target"], row["n_active"], row["n_inactive"], row["n_near"]) == (3, 2, 1, 1)
    assert res["near_pcts"] == [30]


# ------------------------------------------------------------------ 3. 가중 인정 한도 (D-84)
def test_boost_doubles_first_two_weeks_up_to_the_cap(env):
    con, add = env["con"], env["add"]
    a, b = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[:2]
    add(a, "2026-11-03", 50_000)                         # 5만이면 10만으로 인정한다(공문 예시)
    add(a, "2026-11-05", 150_000)                        # 10만까지만 2배, 넘는 5만은 1배
    add(a, "2026-11-20", 70_000)                         # 3주차는 그대로
    add(b, "2026-11-02", 60_000)
    add(b, "2026-11-09", 80_000)
    p = pm._load(con, _db_life(con))
    r = pm.progress(con, p, [a], MID)[a]
    assert r["windows"][1]["value"] == pytest.approx(100_000 + 250_000 + 70_000)
    assert r["windows"][1]["plain"] == pytest.approx(270_000)
    assert r["value"] == pytest.approx(270_000)          # 2배는 조건 1에만. 합산(조건 2)은 원래 값
    # 한도의 범위를 '그 기간 합계' 로 고르면 사람마다 1, 2주차 합계 10만까지만 2배다
    p2 = pm._load(con, _db_life(con, rules={"boost": {"factor": 2, "month_days": [1, 14], "cap": 100_000, "cap_scope": "range",
                                                      "applies_to": "each"}}))
    rb = pm.progress(con, p2, [b], MID)[b]
    assert rb["windows"][1]["value"] == pytest.approx(140_000 + 100_000)
    # 모든 조건에 쓰면 합계도 늘어난다
    p3 = pm._load(con, _db_life(con, rules={"boost": {"factor": 2, "month_days": [1, 14], "cap": 100_000}}))
    assert pm.progress(con, p3, [a], MID)[a]["value"] == pytest.approx(420_000)
    with pytest.raises(ValueError):
        pm.create(con, period_ym=YM, name="x", kind="monthly", windows=[Q4[0]], tiers=[(1, "fixed", 1, None)], date_basis="contract_date",
                  rules={"boost": {"factor": 2, "month_days": [1, 14], "applies_to": "each"}}, by="t")   # 창 하나에는 조건 1이 없다


def test_boost_survives_confirm_and_inst13_rejudge(env):
    con, add = env["con"], env["add"]
    a = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[0]
    add(a, "2026-10-02", 60_000, paid="202711")           # 12만으로 인정 → 10월 10만 달성
    add(a, "2026-11-20", 300_000, paid="202711")
    add(a, "2026-12-20", 400_000, paid="202712")
    pid = _db_life(con, pay_timing="inst13")
    p = pm._load(con, pid)
    est = pm.final_results(con, p, [a], AFTER)["rows"][0]
    assert est["outcome"] == "achieved"
    pm.confirm_results(con, pid, "admin", AFTER)
    stored = con.execute("SELECT value, value_each FROM promotion_result_contract WHERE agent_id=? AND window_seq=1", (a,)).fetchone()
    assert (stored["value"], stored["value_each"]) == (60_000, 120_000)
    got = pm.inst13_results(con, p, [a], date(2028, 1, 10))["rows"][0]
    assert got["decision"] == "pay" and got["outcome_kept"] == "achieved"


# ------------------------------------------------------------------ 4. 조건 묶음: 모두(both)와 또는(either)
def test_either_needs_one_condition(env):
    con, add = env["con"], env["add"]
    each_only, sum_only, neither = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[:3]
    for mon, v in (("10", 100_000), ("11", 300_000), ("12", 400_000)):
        add(each_only, f"2026-{mon}-20", v)              # 합 80만이라 조건 2는 못 채움
    add(sum_only, "2026-10-20", 1_100_000)               # 11월과 12월이 비어 조건 1은 깨짐
    add(neither, "2026-10-20", 100_000)
    p = pm._load(con, _db_life(con))
    res = pm.progress(con, p, [each_only, sum_only, neither], AFTER)
    assert res[each_only]["status"] == "achieved" and res[each_only]["paths"]["sum"]["tier_no"] is None
    assert res[sum_only]["status"] == "achieved" and res[sum_only]["paths"]["each"]["broken"]
    assert res[neither]["status"] == "missed"
    # 진행 중에는 남은 비율이 작은 조건이 다음 구간이 된다. 깨진 조건 1이 있어도 조건 2로 갈 수 있다
    mid = pm.progress(con, p, [sum_only], MID)[sum_only]
    assert mid["status"] == "achieved" and not mid["broken"]
    nb = pm.progress(con, p, [neither], MID)[neither]
    assert not nb["broken"] and nb["next"]["path"] == "sum"


def test_both_needs_every_condition_and_application_basis(env):
    con, add = env["con"], env["add"]
    ok, each_only, sum_only = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[:3]
    kw = {"insurer": "한빛손해", "product": "한빛손해 건강종합보험"}
    for mon, v in (("10", 100_000), ("11", 150_000), ("12", 200_000)):
        add(ok, f"2026-{mon}-20", v, status="applied", **kw)             # 청약만 된 계약도 센다
    for mon, v in (("10", 50_000), ("11", 100_000), ("12", 150_000)):
        add(each_only, f"2026-{mon}-20", v, **kw)                        # 합 30만이라 조건 2 미달
    add(sum_only, "2026-10-20", 500_000, **kw)
    add(sum_only, "2026-11-20", 500_000, status="withdrawn", **kw)       # 철회는 세지 않는다
    add(sum_only, "2026-12-20", 500_000, status="declined", **kw)        # 반송도 세지 않는다 (D-85)
    pid = _heungkuk(con)
    p = pm._load(con, pid)
    res = pm.progress(con, p, [ok, each_only, sum_only], AFTER)
    assert res[ok]["status"] == "achieved" and res[ok]["value"] == pytest.approx(450_000)
    assert res[each_only]["status"] == "missed" and res[each_only]["paths"]["each"]["tier_no"] == 1
    assert res[sum_only]["status"] == "missed" and res[sum_only]["value"] == pytest.approx(500_000)
    # 조건 1이 깨지면 both 는 진행 중에도 무효다. 11월 창이 0으로 끝난 12월 5일에 본다
    mid = pm.progress(con, p, [sum_only], MID)[sum_only]
    assert not mid["broken"] and mid["status"] == "in_progress"
    dec = pm.progress(con, p, [sum_only], date(2026, 12, 5))[sum_only]
    assert dec["broken"] and dec["status"] == "missed" and dec["next"] is None
    # 청약 기준은 기준일도 청약일자여야 한다
    with pytest.raises(ValueError):
        pm.create(con, period_ym=YM, name="x", kind="travel", windows=Q4, combine_rule="both", tiers=_tiers((1, "x")),
                  window_tiers={1: _tiers((1, "x")), 2: _tiers((1, "x")), 3: _tiers((1, "x"))}, date_basis="contract_date",
                  rules={"count_basis": "application"}, by="t")
    # 묶음 결합은 창별 구간표와 합산 구간표가 모두 있어야 한다
    with pytest.raises(ValueError):
        pm.create(con, period_ym=YM, name="x", kind="travel", windows=Q4, combine_rule="either", tiers=_tiers((1, "x")), by="t")
    assert "청약 기준" in " ".join(pm.conditions(p)) and "청약" in pm.basis_note(p)


# ------------------------------------------------------------------ 5. 상품 이름 거르기 (D-86)
def test_product_name_include_exclude_and_unnamed(env):
    con, add = env["con"], env["add"]
    a = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[0]
    kw = {"insurer": "미래화재"}
    add(a, "2026-10-20", 100_000, product="미래화재 건강종합보험", **kw)
    add(a, "2026-10-21", 200_000, product="미래화재 재물보험", **kw)               # 마스터 이름에 '재물'
    add(a, "2026-10-22", 300_000, product=None, raw="무배당 펫 사랑보험", **kw)     # 원장 상품명 원문에 '펫'
    add(a, "2026-10-23", 400_000, product=None, raw="the 라이프업 플러스", **kw)    # 대소문자와 공백은 무시
    add(a, "2026-10-24", 500_000, product=None, raw=None, **kw)                     # 이름이 없다
    pid = pm.create(con, period_ym=YM, name="x", kind="monthly", windows=[Q4[0]], tiers=[(1, "fixed", 1, None)], date_basis="contract_date",
                    insurer_id=con.execute("SELECT insurer_id FROM insurer WHERE name='미래화재'").fetchone()[0],
                    rules={"product_exclude": EXCLUDE}, by="t")
    r = pm.progress(con, pm._load(con, pid), [a], AFTER)[a]
    assert r["value"] == pytest.approx(100_000) and r["unnamed"] == 1
    inc = pm.create(con, period_ym=YM, name="y", kind="monthly", windows=[Q4[0]], tiers=[(1, "fixed", 1, None)], date_basis="contract_date",
                    rules={"product_include": "건강종합\n펫"}, by="t")
    r2 = pm.progress(con, pm._load(con, inc), [a], AFTER)[a]
    assert r2["value"] == pytest.approx(400_000) and r2["unnamed"] == 1
    org = pm.org_progress(con, pm._load(con, pid), env["hqs"][0]["path_key"], [a], AFTER, near_pct=30)
    assert org["total"]["unnamed"] == 1
    # 이름 조건이 없으면 이름 없는 계약도 예전처럼 센다
    plain = pm.create(con, period_ym=YM, name="z", kind="monthly", windows=[Q4[0]], tiers=[(1, "fixed", 1, None)], date_basis="contract_date",
                      by="t")
    assert pm.progress(con, pm._load(con, plain), [a], AFTER)[a]["unnamed"] == 0


def test_exclude_self_contracts(env):
    con, add = env["con"], env["add"]
    a = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[0]
    add(a, "2026-10-20", 100_000)
    add(a, "2026-10-21", 900_000, self_family=1)
    p = pm._load(con, _db_life(con))
    assert pm.progress(con, p, [a], AFTER)[a]["value"] == pytest.approx(100_000)


# ------------------------------------------------------------------ 6. 건당 한도
def test_per_contract_cap_applies_before_boost(env):
    con, add = env["con"], env["add"]
    a = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[0]
    add(a, "2026-10-20", 500_000)
    add(a, "2026-10-03", 200_000)
    p = pm._load(con, _db_life(con, per_contract_cap=300_000, rules={"boost": {"factor": 2, "month_days": [1, 14], "cap": 100_000}}))
    r = pm.progress(con, p, [a], AFTER)[a]
    assert r["windows"][0]["plain"] == pytest.approx(300_000 + 300_000)      # 50만은 30만까지, 20만은 10만만 2배
    rows = pm._contract_rows(con, p, [a])
    assert sum(x["value"] for x in rows) == pytest.approx(r["value"])


# ------------------------------------------------------------------ 7. 딸린 시책과 조직 달성 인원 문턱 (D-88, D-89)
def _levels(con, ids, code):
    con.execute("INSERT OR IGNORE INTO career_level(code, name, rank_order) VALUES(?,?,?)", (code, code, 9 if code == "BM" else 8))
    lid = con.execute("SELECT level_id FROM career_level WHERE code=?", (code,)).fetchone()[0]
    for a in ids:
        con.execute("UPDATE agent_career_history SET effective_to='202609' WHERE agent_id=? AND effective_to IS NULL", (a,))
        con.execute("INSERT INTO agent_career_history(agent_id, level_id, effective_from) VALUES(?,?,?)", (a, lid, "202610"))
    con.commit()


def test_convention_gate_and_target_levels(env):
    con, add = env["con"], env["add"]
    hq1 = env["hqs"][0]
    people = [a for b in env["branches"][hq1["org_id"]] for a in env["fcs"](b)][:4]
    for a in people[:3]:
        for mon in ("10", "11", "12"):
            add(a, f"2026-{mon}-20", 450_000)
    _levels(con, people[:2], "BM")
    _levels(con, people[2:3], "CA")
    parent = _db_life(con)
    conv = pm.create(con, period_ym=YM, name="시험 Convention", kind="travel", windows=Q4, tiers=_tiers((1, "추가 시책 원문 확인")),
                     combine_rule="sum", date_basis="contract_date",
                     rules={"parent_promo_id": parent, "target_levels": ["bm", "CA"], "gate_min_achievers": 4}, by="t", publish=True)
    p = pm._load(con, conv)
    res = pm.progress(con, p, people, AFTER)
    gate = res[people[0]]["gate"]
    assert (gate["min"], gate["n"], gate["open"], gate["left"]) == (4, 3, False, 1)
    assert res[people[0]]["status"] == "missed" and res[people[0]]["tier"] is None
    assert res[people[3]]["status"] == "not_target" and not res[people[3]]["target"]
    org = pm.org_progress(con, p, env["root"]["path_key"], env["all"](), AFTER, near_pct=30)
    assert org["gate"]["n"] == 3 and org["total"]["n_target"] == 3            # 대상 직급만 대상이다
    # 문턱을 3명으로 낮추면 열린다
    pm.update(con, conv, by="t", name="시험 Convention", kind="travel", windows=Q4, tiers=_tiers((1, "추가 시책 원문 확인")),
              combine_rule="sum", date_basis="contract_date", rules={"gate_min_achievers": 3})
    p = pm._load(con, conv)
    assert p["rules"]["parent_promo_id"] == parent and p["rules"]["target_levels"] == ["BM", "CA"]
    res = pm.progress(con, p, people, AFTER)
    assert res[people[0]]["gate"]["open"] and res[people[0]]["status"] == "achieved"
    assert res[people[2]]["status"] == "achieved"
    fin = pm.final_results(con, p, people, AFTER)
    assert {r["agent_id"] for r in fin["rows"]} == set(people[:3])            # 대상 직급이 아닌 사람은 줄이 없다
    # 문턱은 범위와 상관없이 조직 전체에서 센다
    assert pm.progress(con, p, [people[0]], AFTER)[people[0]]["gate"]["n"] == 3
    with pytest.raises(ValueError):
        pm.create(con, period_ym=YM, name="x", kind="travel", windows=Q4, tiers=_tiers((1, "x")), combine_rule="sum",
                  rules={"parent_promo_id": conv}, by="t")
    with pytest.raises(ValueError):
        pm.create(con, period_ym=YM, name="x", kind="travel", windows=Q4, tiers=_tiers((1, "x")), combine_rule="sum",
                  rules={"gate_min_achievers": 3}, by="t")


# ------------------------------------------------------------------ 8. 13회차 유지율 문턱 (표시만, D-90)
def test_retention_threshold_is_shown_per_person(env):
    con, add = env["con"], env["add"]
    a, b = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[:2]
    kw = {"insurer": "한빛손해", "product": "한빛손해 건강종합보험"}
    for i in range(20):
        add(a, f"2026-10-{i + 1:02d}", 10_000, paid="202711", **kw)
    add(b, "2026-10-05", 10_000, paid="202711", **kw)
    add(b, "2026-10-06", 10_000, status="lapsed", paid="202612", **kw)
    c = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[2]
    add(c, "2026-10-07", 10_000, **kw)                   # 납입 기록이 없고 도래 전이다
    p = pm._load(con, _heungkuk(con))
    got = pm.retention(con, p, [a, b], date(2027, 12, 31))
    assert got["threshold"] == pytest.approx(0.95) and got["final"]
    assert got["people"][a]["rate"] == 1 and got["people"][a]["meets"]
    assert got["people"][b]["rate"] == pytest.approx(0.5) and got["people"][b]["meets"] is False
    early = pm.retention(con, p, [c], date(2027, 1, 10))
    assert early["people"][c]["pending"] == 1 and early["people"][c]["meets"] is None and not early["final"]
    with pytest.raises(ValueError):
        pm.retention(con, pm._load(con, _db_life(con)), [a], AFTER)


# ------------------------------------------------------------------ 9. 삼성화재 드림+: 일반 연계 시상으로 처리
def test_samsung_dream_lowest_stage_with_exclusions(env):
    con, add = env["con"], env["add"]
    a, b = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[:2]
    kw = {"insurer": "미래화재", "product": "미래화재 건강종합보험"}
    add(a, "2026-10-18", 900_000, **kw)                  # 창 앞이라 세지 않는다
    add(a, "2026-10-20", 300_000, **kw)                  # 10월 3단계
    add(a, "2026-11-10", 1_000_000, **kw)                # 11월 4단계
    add(a, "2026-12-10", 300_000, **kw)                  # 12월 2단계
    add(a, "2026-12-11", 900_000, status="cancelled", **kw)            # 품보(해지)는 세지 않는다 (D-92)
    add(a, "2026-12-12", 900_000, product="미래화재 재물보험", insurer="미래화재")  # 제외 상품
    add(b, "2026-10-20", 50_000, **kw)                   # 10월 창 미달
    p = pm._load(con, _samsung(con))
    res = pm.progress(con, p, [a, b], AFTER)
    assert [w["tier"]["tier_no"] for w in res[a]["windows"]] == [3, 4, 2]
    assert res[a]["tier"]["tier_no"] == 2 and res[a]["status"] == "achieved" and res[a]["amount"] is None
    assert res[a]["reward"] == "2단계 선택형"
    assert res[b]["status"] == "missed"
    assert p["inst13"] and pm.compare_row(p)["conditions"]
