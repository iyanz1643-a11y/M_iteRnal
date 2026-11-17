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

    def add(agent, day, premium, *, insurer="샘플생명", product="샘플생명 건강보험", raw=None, status="inforce", app_day=None, self_family=None, paid=None, converted=None):
        n[0] += 1
        pid = prod.get(product) if product else None
        cid = con.execute(
            """INSERT INTO contract(insurer_id, policy_no, product_id, product_code_raw, agent_id, application_no, application_date, contract_date,
                                    premium, premium_monthly, converted_premium, policy_count, status, payment_mode, last_paid_ym, self_family, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ins[insurer], f"T{n[0]:05d}", pid, raw, agent, f"A{n[0]:05d}", app_day or day, day if status != "applied" else None,
             premium, premium, premium if converted is None else converted, 1, status, "monthly", paid, self_family, db.now())).lastrowid
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


# ------------------------------------------------------------------ 10. 창별 단계(phases)와 연속, 합산 인원
OCT_END, DEC_MID, DEC_END = date(2026, 10, 31), date(2026, 12, 15), date(2026, 12, 31)


def _five(env):
    """서울본부 두 사람과 부산본부 세 사람. 범위를 이 다섯으로 좁혀 인원을 고정한다."""
    hq1, hq2 = env["hqs"]
    return env["fcs"](env["branches"][hq1["org_id"]][0])[:2] + env["fcs"](env["branches"][hq2["org_id"]][0])[:3]


def _value_check(res, per, boosted):
    """조직 줄의 value_sum, plain_sum, sum_value 와 추가안의 value_sum 이 사람별 progress() 값의 합과 같고 합계 줄이 줄의 합인지 본다.
    per 는 같은 범위와 기준일의 progress() 다. boosted 는 창을 읽는 프로모션(딸린 시책이면 부모)에 가중 인정이 있는가다."""
    rows, t = res["rows"], res["total"]
    for r in rows:
        ids = r["agent_ids"]
        for i, ph in enumerate(r["phases"]):
            if ph["state"] == "future":
                assert ph["value_sum"] is None and ph["plain_sum"] is None
                continue
            assert ph["value_sum"] == pytest.approx(sum(per[a]["windows"][i]["value"] for a in ids))
            if boosted:
                assert ph["plain_sum"] == pytest.approx(sum(per[a]["windows"][i]["plain"] for a in ids))
            else:
                assert ph["plain_sum"] is None
        if r["sum_target"] is None:
            assert r["sum_value"] == r["value"]
        else:
            assert r["sum_value"] == pytest.approx(sum(per[a]["value"] for a in ids)) == pytest.approx(r["value"] - r["value_left"])
        for k, c in enumerate(r["alts"]):
            elig = [per[a]["paths"]["alts"][k] for a in ids if per[a]["paths"]["alts"][k]["eligible"]]
            for j, ph in enumerate(c["phases"]):
                assert ph["value_sum"] == (None if ph["state"] == "future" else pytest.approx(sum(m["windows"][j]["value"] for m in elig)))
    assert t["sum_value"] == pytest.approx(sum(r["sum_value"] for r in rows))
    for i, ph in enumerate(t["phases"]):
        for key in ("value_sum", "plain_sum"):
            vals = [r["phases"][i][key] for r in rows]
            assert ph[key] == (None if ph["state"] == "future" or (key == "plain_sum" and not boosted) else pytest.approx(sum(vals)))
    for k, c in enumerate(t["alts"]):
        for j, ph in enumerate(c["phases"]):
            assert ph["value_sum"] == (None if ph["state"] == "future" else pytest.approx(sum(r["alts"][k]["phases"][j]["value_sum"] for r in rows)))


def _phase_check(con, p, scope, ref, root):
    """조직 줄의 phases, n_chain, n_sum 이 사람별 progress() 를 센 것과 같고 합계가 줄의 합인지 본다. 합계 줄을 돌려준다."""
    res = pm.org_progress(con, p, root, scope, ref, near_pct=30)
    per = pm.progress(con, p, scope, ref)
    _value_check(res, per, bool(p["rules"]["boost"]))
    states = [pm.PHASE_STATE[pm.window_state(w, ref)["state"]] for w in p["windows"]]
    for r in res["rows"]:
        ids = r["agent_ids"]
        assert [ph["state"] for ph in r["phases"]] == states
        for i, ph in enumerate(r["phases"]):
            assert (ph["seq"], ph["label"], ph["date_from"], ph["date_to"]) == tuple(p["windows"][i][k] for k in ("seq", "label", "date_from", "date_to"))
            assert ph["target"] == p["window_tiers"][ph["seq"]][0]["min_value"]
            if ph["state"] == "future":
                assert ph["n_met"] is None and ph["n_chain"] is None and r["windows"][i]["n_achieved"] == 0
                continue
            assert ph["n_met"] == sum(1 for a in ids if per[a]["windows"][i]["tier"]) == r["windows"][i]["n_achieved"]
            assert ph["n_chain"] == sum(1 for a in ids if all(per[a]["windows"][j]["tier"] for j in range(i + 1)))
        assert r["n_chain"] == sum(1 for a in ids if not per[a]["paths"]["each"]["broken"])
        assert r["n_sum"] == sum(1 for a in ids if per[a]["paths"]["sum"]["tier_no"])
        assert r["n_achieved"] == sum(1 for a in ids if pm.is_achieved(p, per[a]))
        assert r["sum_target"] == p["tiers"][0]["min_value"]
        if r["alt"]:
            mine = [per[a]["paths"]["alt"] for a in ids]
            assert r["alt"]["n_eligible"] == sum(1 for m in mine if m["eligible"])
            assert r["alt"]["n_achieved"] == sum(1 for m in mine if m["achieved"])
            for j, ph in enumerate(r["alt"]["phases"]):
                assert ph["n_met"] == (None if ph["state"] == "future" else sum(1 for m in mine if m["eligible"] and m["windows"][j]["met"]))
    t = res["total"]
    if t["alt"]:
        assert t["alt"]["n_eligible"] == sum(r["alt"]["n_eligible"] for r in res["rows"])
        assert t["alt"]["n_achieved"] == sum(r["alt"]["n_achieved"] for r in res["rows"])
    for i, ph in enumerate(t["phases"]):
        for k in ("n_met", "n_chain"):
            assert ph[k] == (None if ph["state"] == "future" else sum(r["phases"][i][k] for r in res["rows"]))
    for k in ("n_chain", "n_sum", "n_achieved"):
        assert t[k] == sum(r[k] for r in res["rows"])
    assert "agent_ids" not in t and t["sum_target"] == p["tiers"][0]["min_value"]
    return t


def _shape(t):
    return [(ph["state"], ph["n_met"], ph["n_chain"]) for ph in t["phases"]]


def test_phases_both_heungkuk_by_ref_date(env):
    """흥국 BOOM-UP 꼴(both). 계약을 달마다 넣어 가며 10월 31일, 11월 17일, 12월 31일에 본다."""
    con, add = env["con"], env["add"]
    a1, a2, a3, a4, _idle = scope = _five(env)
    root = env["root"]["path_key"]
    kw = {"insurer": "한빛손해", "product": "한빛손해 건강종합보험"}
    p = pm._load(con, _heungkuk(con))
    add(a1, "2026-10-10", 100_000, status="applied", **kw)
    add(a2, "2026-10-12", 50_000, **kw)
    add(a3, "2026-10-14", 40_000, **kw)                  # 10월 5만에 못 미친다
    add(a4, "2026-10-16", 60_000, **kw)
    t = _phase_check(con, p, scope, OCT_END, root)        # 10월 마지막 날은 10월 창이 진행 중이다
    assert _shape(t) == [("open", 3, 3), ("future", None, None), ("future", None, None)]
    assert (t["n_target"], t["n_chain"], t["n_sum"], t["sum_target"], t["n_achieved"]) == (5, 5, 0, 400_000, 0)
    assert [ph["target"] for ph in t["phases"]] == [50_000, 100_000, 150_000]
    assert [w["n_achieved"] for w in t["windows"]] == [3, 0, 0]

    add(a1, "2026-11-10", 150_000, **kw)
    add(a2, "2026-11-12", 100_000, **kw)
    add(a3, "2026-11-05", 400_000, **kw)                 # 합산 44만. 10월을 놓쳐 연속은 깨졌다
    add(a4, "2026-11-15", 80_000, **kw)                  # 11월 10만에 아직 못 미친다
    t = _phase_check(con, p, scope, MID, root)
    assert _shape(t) == [("done", 3, 3), ("open", 3, 2), ("future", None, None)]
    assert (t["n_chain"], t["n_sum"], t["n_achieved"]) == (3, 1, 0)      # 진행 중인 11월을 못 채운 사람도 연속은 살아 있다

    add(a1, "2026-12-10", 200_000, **kw)
    add(a2, "2026-12-11", 150_000, **kw)
    t = _phase_check(con, p, scope, DEC_MID, root)
    assert _shape(t) == [("done", 3, 3), ("done", 3, 2), ("open", 2, 2)]
    # 지금 동작: both 는 마지막 창의 기준과 합산을 채우면 기간이 끝나기 전에도 달성으로 센다. 그 전에는 세지 않는다
    assert (t["n_chain"], t["n_sum"], t["n_achieved"]) == (2, 2, 1)

    add(a4, "2026-12-20", 300_000, **kw)                 # 합산 44만. 11월을 놓쳐 연속은 깨졌다
    t = _phase_check(con, p, scope, DEC_END, root)
    assert _shape(t) == [("done", 3, 3), ("done", 3, 2), ("open", 3, 2)]
    assert (t["n_chain"], t["n_sum"], t["n_achieved"]) == (2, 3, 1)
    t = _phase_check(con, p, scope, AFTER, root)
    assert _shape(t) == [("done", 3, 3), ("done", 3, 2), ("done", 3, 2)]
    assert (t["n_chain"], t["n_sum"], t["n_achieved"]) == (2, 3, 1)


BOOST_V2 = {"factor": 2, "ranges": [["2026-10-01", "2026-10-14"], ["2026-12-01", "2026-12-14"]], "cap": 100_000, "applies_to": "each"}
ALT_V2 = [{"key": "oct_missed", "label": "추가안", "windows": {2: 250_000, 3: 450_000}, "requires_missed": [1]}]


def _db_life_v2(con, alt=ALT_V2, boost=BOOST_V2, **kw):
    """DB생명 여행시상 변경판(지금 적용). 조건 1-1 은 10월 달성자의 10/20/40만 연속, 조건 1-2 는 10월 미달성자의 11월 25만과 12월 45만 연속(추가안),
    조건 2 는 합산 100만이다. 2배 인정은 10월과 12월의 1, 2주차만이고 11월은 없다. 연속 조건에만 쓴다. _db_life 는 1차 원안이다."""
    return pm.create(con, period_ym=YM, name="시험 여행시상 변경판", kind="travel", windows=Q4, combine_rule="either", insurer_name="샘플생명",
                     insurer_id=con.execute("SELECT insurer_id FROM insurer WHERE name='샘플생명'").fetchone()[0], categories=["health"],
                     date_basis="contract_date", tiers=_tiers((1_000_000, "싱가포르")),
                     window_tiers={1: _tiers((100_000, "싱가포르")), 2: _tiers((200_000, "싱가포르")), 3: _tiers((400_000, "싱가포르"))},
                     rules={"boost": boost, "exclude_self": True, "alt_chains": alt}, by="t", publish=True, **kw)


def _alt_shape(t):
    return (t["alt"]["n_eligible"], [(ph["state"], ph["n_met"]) for ph in t["alt"]["phases"]], t["alt"]["n_achieved"])


def test_phases_either_db_life_with_boost_by_ref_date(env):
    """DB생명 변경판(either, 2배 인정은 조건 1에만, 11월 제외). n_met 은 원안 연속의 기준과 인정값으로, n_sum 은 원래 값으로 센다."""
    con, add = env["con"], env["add"]
    b1, b2, b3, b4, _idle = scope = _five(env)
    root = env["root"]["path_key"]
    p = pm._load(con, _db_life_v2(con))
    add(b1, "2026-10-03", 50_000)                        # 1주차 5만은 10만으로 인정해 10월 10만을 채운다
    add(b2, "2026-10-20", 1_100_000)                     # 합산 100만을 10월에 채운다
    add(b3, "2026-10-20", 90_000)                        # 3주차라 그대로 9만. 10월을 놓친다
    add(b4, "2026-10-10", 60_000)                        # 12만으로 인정
    t = _phase_check(con, p, scope, OCT_END, root)
    assert _shape(t) == [("open", 3, 3), ("future", None, None), ("future", None, None)]
    # 지금 동작: either 는 합산을 채우면 기간 중에도 바로 달성이다
    assert (t["n_chain"], t["n_sum"], t["sum_target"], t["n_achieved"]) == (5, 1, 1_000_000, 1)
    assert [ph["target"] for ph in t["phases"]] == [100_000, 200_000, 400_000]
    assert _alt_shape(t) == (0, [("future", None), ("future", None)], 0)      # 10월이 끝나야 추가안 대상이 정해진다

    add(b1, "2026-11-03", 50_000)                        # 11월은 2배가 없다
    add(b1, "2026-11-15", 150_000)                       # 5만 + 15만으로 11월 20만을 채운다
    add(b3, "2026-11-10", 250_000)                       # 10월 미달성자의 추가안 11월 25만
    add(b4, "2026-11-16", 190_000)                       # 11월 20만에 못 미친다
    t = _phase_check(con, p, scope, MID, root)
    assert _shape(t) == [("done", 3, 3), ("open", 2, 1), ("future", None, None)]      # 11월 20만 진행 중, 12월은 시작 전
    assert (t["n_chain"], t["n_sum"], t["n_achieved"]) == (3, 1, 1)
    assert _alt_shape(t) == (2, [("open", 1), ("future", None)], 0)                   # 추가안 11월 25만 진행 중
    assert [ph["min_value"] for ph in t["alt"]["phases"]] == [250_000, 450_000]

    add(b1, "2026-12-10", 400_000)                       # 합산은 원래 값 65만이라 못 채우고 연속으로 달성한다
    add(b3, "2026-12-05", 400_000)                       # 12월 1주차라 10만까지 2배. 인정값 50만으로 추가안 45만을 채운다
    t = _phase_check(con, p, scope, DEC_END, root)
    assert _shape(t) == [("done", 3, 3), ("done", 2, 1), ("open", 2, 1)]
    assert (t["n_chain"], t["n_sum"], t["n_achieved"]) == (1, 1, 3)
    assert _alt_shape(t) == (2, [("done", 1), ("open", 1)], 1)
    rows = {r["path_key"]: r for r in pm.org_progress(con, p, root, scope, DEC_END, near_pct=30)["rows"]}
    hq1, hq2 = (rows[h["path_key"]] for h in env["hqs"])
    assert (hq1["n_chain"], hq1["n_sum"], hq1["n_achieved"], hq1["alt"]["n_eligible"], hq1["alt"]["n_achieved"]) == (1, 1, 2, 0, 0)
    assert (hq2["n_chain"], hq2["n_sum"], hq2["n_achieved"], hq2["alt"]["n_eligible"], hq2["alt"]["n_achieved"]) == (0, 0, 1, 2, 1)
    # 퇴사자는 실적 합계에만 남고 단계와 조건 인원에서는 빠진다 (D-42)
    con.execute("UPDATE agent SET status='terminated', termination_date='2026-12-20' WHERE agent_id=?", (b1,))
    con.commit()
    t = _phase_check(con, p, scope, DEC_END, root)
    assert _shape(t) == [("done", 2, 2), ("done", 1, 0), ("open", 1, 0)] and (t["n_chain"], t["n_sum"], t["n_achieved"]) == (0, 1, 2)


def test_windows_not_started_report_no_achievers(env):
    """기준일 뒤 날짜의 계약이 원장에 있어도 시작 전 창은 달성 인원을 내지 않는다. 예전에는 뒤 창의 계약으로 숫자가 나왔다."""
    con, add = env["con"], env["add"]
    a1, a2, *_rest = scope = _five(env)
    root = env["root"]["path_key"]
    kw = {"insurer": "한빛손해", "product": "한빛손해 건강종합보험"}
    for a in (a1, a2):
        for mon, v in (("10", 100_000), ("11", 150_000), ("12", 200_000)):
            add(a, f"2026-{mon}-10", v, **kw)
    p = pm._load(con, _heungkuk(con))
    t = pm.org_progress(con, p, root, scope, OCT_END, near_pct=30)["total"]
    assert [w["n_achieved"] for w in t["windows"]] == [2, 0, 0]
    assert _shape(t) == [("open", 2, 2), ("future", None, None), ("future", None, None)]
    t = pm.org_progress(con, p, root, scope, AFTER, near_pct=30)["total"]
    assert [w["n_achieved"] for w in t["windows"]] == [2, 2, 2] and _shape(t) == [("done", 2, 2)] * 3
    # 범위가 비어도 창과 상태는 나온다
    t = pm.org_progress(con, p, root, [], MID, near_pct=30)["total"]
    assert _shape(t) == [("done", 0, 0), ("open", 0, 0), ("future", None, None)] and (t["n_chain"], t["n_sum"], t["sum_target"]) == (0, 0, 400_000)


def test_phases_for_other_combine_rules(env):
    """창마다 달성하는 결합이 아니면 창의 기준이 없어 n_met, n_chain 이 None 이다. 합산 기준이 없으면 n_sum 도 None 이다."""
    con, add = env["con"], env["add"]
    a1, a2, *_rest = scope = _five(env)
    root = env["root"]["path_key"]
    for mon in ("10", "11", "12"):
        add(a1, f"2026-{mon}-20", 450_000)
    add(a2, "2026-10-20", 100_000)
    single = pm.create(con, period_ym=YM, name="z", kind="monthly", windows=[Q4[0]], tiers=[(200_000, "fixed", 1, None)],
                       date_basis="contract_date", by="t")
    t = pm.org_progress(con, pm._load(con, single), root, scope, AFTER, near_pct=30)["total"]
    assert [(ph["state"], ph["target"], ph["n_met"], ph["n_chain"]) for ph in t["phases"]] == [("done", None, None, None)]
    assert (t["n_chain"], t["n_sum"], t["sum_target"], t["n_achieved"]) == (None, None, None, 1)
    # 합산(sum): 창의 기준은 없고 합산 인원만 있다. 최종 달성과 같다
    summed = pm.create(con, period_ym=YM, name="s", kind="travel", windows=Q4, tiers=_tiers((1_000_000, "x")), combine_rule="sum",
                       date_basis="contract_date", by="t")
    t = pm.org_progress(con, pm._load(con, summed), root, scope, MID, near_pct=30)["total"]
    assert [(ph["state"], ph["target"], ph["n_met"]) for ph in t["phases"]] == [("done", None, None), ("open", None, None), ("future", None, None)]
    assert (t["n_chain"], t["n_sum"], t["sum_target"], t["n_achieved"]) == (None, 1, 1_000_000, 1)
    # 창마다 달성(all): 합산 기준이 없다
    kw = {"insurer": "미래화재", "product": "미래화재 건강종합보험"}
    add(a1, "2026-10-20", 300_000, **kw)
    add(a1, "2026-11-10", 150_000, **kw)                 # 11월 1단계 20만에 못 미친다
    t = pm.org_progress(con, pm._load(con, _samsung(con)), root, scope, date(2026, 12, 5), near_pct=30)["total"]
    assert [(ph["state"], ph["target"], ph["n_met"], ph["n_chain"]) for ph in t["phases"]] == [
        ("done", 100_000, 1, 1), ("done", 200_000, 0, 0), ("open", 200_000, 0, 0)]
    assert (t["n_chain"], t["n_sum"], t["sum_target"]) == (0, None, None)
    # 딸린 시책은 부모의 창과 기준으로 센다
    parent = _db_life(con)
    conv = pm.create(con, period_ym=YM, name="시험 Convention", kind="travel", windows=Q4, tiers=_tiers((1, "추가 시책 원문 확인")),
                     combine_rule="sum", date_basis="contract_date", rules={"parent_promo_id": parent}, by="t", publish=True)
    got = pm.org_progress(con, pm._load(con, conv), root, scope, AFTER, near_pct=30)["total"]
    want = pm.org_progress(con, pm._load(con, parent), root, scope, AFTER, near_pct=30)["total"]
    assert got["phases"] == want["phases"] and got["sum_target"] == 1_000_000
    assert (got["n_chain"], got["n_sum"]) == (want["n_chain"], want["n_sum"]) == (1, 1)


# ------------------------------------------------------------------ 11. 가중 인정: 변경판은 11월에 2배가 없다
def test_boost_changed_version_skips_november(env):
    """변경판: 10월 1주차 5만 가동은 10만으로 인정해 10월 기준 10만 달성이다. 11월은 2배 없이 20만을 채워야 한다. 12월 1, 2주차는 다시 2배다.
    1차 원안 포스터의 '11월 1주차 5만이면 10만 인정' 은 변경판에서 맞지 않는다. 1, 2주차를 1일에서 14일로 보는 것은 가정이다."""
    con, add = env["con"], env["add"]
    a, b, c, d = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[:4]
    p = pm._load(con, _db_life_v2(con))
    assert [p["window_tiers"][s][0]["min_value"] for s in (1, 2, 3)] == [100_000, 200_000, 400_000]
    add(a, "2026-10-05", 50_000)
    oct_w = pm.progress(con, p, [a], OCT_END)[a]["windows"][0]
    assert oct_w["value"] == pytest.approx(100_000) and oct_w["plain"] == pytest.approx(50_000) and oct_w["tier"]
    add(a, "2026-11-03", 50_000)                         # 11월 1주차도 그대로 5만이다
    add(a, "2026-11-10", 100_000)
    nov = pm.progress(con, p, [a], MID)[a]["windows"][1]
    assert nov["value"] == pytest.approx(150_000) == nov["plain"] and nov["tier"] is None
    add(a, "2026-11-17", 50_000)                         # 2배 없이 20만을 채운다
    r = pm.progress(con, p, [a], MID)[a]
    assert r["windows"][1]["value"] == pytest.approx(200_000) and r["windows"][1]["tier"] and not r["paths"]["each"]["broken"]
    add(a, "2026-12-03", 50_000)                         # 12월 1주차는 10만으로 인정
    add(a, "2026-12-20", 300_000)
    r = pm.progress(con, p, [a], DEC_END)[a]
    assert r["windows"][2]["value"] == pytest.approx(400_000) and r["windows"][2]["plain"] == pytest.approx(350_000)
    assert r["status"] == "achieved" and r["paths"]["each"]["tier_no"] == 1
    assert r["value"] == pytest.approx(600_000) and r["paths"]["sum"]["tier_no"] is None      # 합산은 원래 값 5만 + 20만 + 35만
    # 가중 구간 밖의 5만은 그대로 5만, 1주차 4만은 8만이라 10월 10만에 못 미친다. 15만은 10만까지만 2배라 25만이다
    add(b, "2026-10-15", 50_000)
    add(c, "2026-10-05", 40_000)
    add(d, "2026-10-02", 150_000)
    res = pm.progress(con, p, [b, c, d], OCT_END)
    assert res[b]["windows"][0]["value"] == pytest.approx(50_000) and res[b]["windows"][0]["tier"] is None
    assert res[c]["windows"][0]["value"] == pytest.approx(80_000) and res[c]["windows"][0]["tier"] is None
    assert res[d]["windows"][0]["value"] == pytest.approx(250_000)
    org = pm.org_progress(con, p, env["hqs"][0]["path_key"], [a, b, c, d], MID, near_pct=30)["total"]
    assert _shape(org) == [("done", 2, 2), ("open", 1, 1), ("future", None, None)]


# ------------------------------------------------------------------ 12. 추가안 (alt_chains, D-96)
def test_alt_chain_in_november(env):
    """기준일 11월 17일. 10월 미달성자는 추가안 11월 25만이 진행 중이고, 10월 달성자는 추가안 대상이 아니다."""
    con, add = env["con"], env["add"]
    x, y, z, w = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[:4]
    p = pm._load(con, _db_life_v2(con))
    assert p["rules"]["alt_chains"] == [{"key": "oct_missed", "label": "추가안", "windows": {2: 250_000, 3: 450_000}, "requires_missed": [1],
                                         "boost": True, "tier_no": 1}]
    add(x, "2026-10-20", 90_000)                         # 10월 미달성
    add(y, "2026-10-20", 90_000)
    add(z, "2026-10-20", 100_000)                        # 10월 달성
    early = pm.progress(con, p, [x, z], date(2026, 10, 25))
    assert (early[x]["paths"]["alt"]["eligible"], early[x]["paths"]["alt"]["pending"]) == (False, True)     # 10월이 끝나야 정해진다
    assert (early[z]["paths"]["alt"]["eligible"], early[z]["paths"]["alt"]["pending"]) == (False, False)
    assert early[x]["next"]["path"] == "each"
    add(x, "2026-11-10", 250_000)                        # 추가안 11월 25만을 채웠다
    add(y, "2026-11-10", 100_000)                        # 못 채웠다
    add(z, "2026-11-10", 250_000)
    res = pm.progress(con, p, [x, y, z, w], MID)
    ax, ay, az, aw = (res[a]["paths"]["alt"] for a in (x, y, z, w))
    assert (ax["key"], ax["label"], ax["eligible"], ax["broken"], ax["achieved"]) == ("oct_missed", "추가안", True, False, False)
    assert [(v["seq"], v["min_value"], v["value"], v["met"], v["state"]) for v in ax["windows"]] == [
        (2, 250_000, 250_000, True, "active"), (3, 450_000, 0, False, "upcoming")]
    assert res[x]["status"] == "in_progress" and res[x]["tier"] is None and res[x]["paths"]["each"]["broken"]
    assert res[x]["next"]["path"] == "sum"               # 11월 추가안을 이미 채워 지금 남은 길은 합산이다. 12월은 시작 전이다
    assert ay["eligible"] and not ay["windows"][0]["met"] and not ay["broken"]
    assert res[y]["next"] == {"tier_no": 1, "min_value": 250_000, "gap": 150_000, "reward": "싱가포르", "path": "alt", "key": "oct_missed", "window_seq": 2}
    assert pm.near(res[y], 0.6) and not pm.near(res[y], 0.3)
    assert not az["eligible"] and not az["pending"] and not az["achieved"]      # 10월 달성자는 원안 연속 길로만 본다
    assert res[z]["windows"][1]["tier"] and not res[z]["paths"]["each"]["broken"]
    assert aw["eligible"] and res[w]["next"]["path"] == "alt" and res[w]["value"] == 0
    assert res[x]["paths"]["alts"] == [ax]
    # 11월이 끝나면 추가안 11월을 못 채운 사람은 추가안도 깨진다. either 라 합산은 남는다
    dec = pm.progress(con, p, [y], date(2026, 12, 5))[y]
    assert dec["paths"]["alt"]["broken"] and dec["next"]["path"] == "sum" and dec["status"] == "in_progress"
    org = pm.org_progress(con, p, env["hqs"][0]["path_key"], [x, y, z, w], MID, near_pct=30)["total"]
    assert _alt_shape(org) == (3, [("open", 1), ("future", None)], 0)
    assert _shape(org) == [("done", 1, 1), ("open", 2, 1), ("future", None, None)]      # 원안 11월 20만은 10월과 상관없이 센다
    assert any("추가안: 10월 미달성자만, 11월 250,000원, 12월 450,000원 연속 달성" in c for c in pm.conditions(p))


def test_alt_chain_three_ways_to_achieve_and_confirm(env):
    """12월 말에 원안 연속, 합산, 추가안 각각으로 달성한다. 확정한 값은 확정 전 예상과 같고 13회차 다시 계산에서도 그대로다."""
    con, add = env["con"], env["add"]
    e, s, t, u, n = scope = _five(env)
    for mon, v in (("10", 100_000), ("11", 200_000), ("12", 400_000)):
        add(e, f"2026-{mon}-20", v, paid="202712")       # 조건 1-1 원안 연속. 합 70만
    add(s, "2026-10-20", 1_100_000, paid="202712")       # 조건 2 합산
    add(t, "2026-11-20", 250_000, paid="202712")         # 조건 1-2 추가안. 합 70만
    add(t, "2026-12-20", 450_000, paid="202712")
    add(u, "2026-11-20", 250_000, paid="202712")
    add(u, "2026-12-05", 350_000, paid="202712")         # 12월 1주차라 인정값 45만. 가중 인정을 추가안에 쓸 때만 달성이다 (D-96)
    add(n, "2026-11-20", 240_000, paid="202712")         # 추가안 11월 25만에 못 미친다
    add(n, "2026-12-20", 450_000, paid="202712")
    pid = _db_life_v2(con, pay_timing="inst13")
    p = pm._load(con, pid)
    res = pm.progress(con, p, scope, DEC_END)
    assert [res[a]["status"] for a in scope] == ["achieved", "achieved", "achieved", "achieved", "in_progress"]
    assert [bool(res[a]["paths"]["each"]["tier_no"]) for a in scope] == [True, False, False, False, False]
    assert [bool(res[a]["paths"]["sum"]["tier_no"]) for a in scope] == [False, True, False, False, False]
    assert [res[a]["paths"]["alt"]["achieved"] for a in scope] == [False, False, True, True, False]
    assert [res[a]["paths"]["alt"]["eligible"] for a in scope] == [False, False, True, True, True]
    assert res[t]["tier"]["tier_no"] == 1 and res[t]["reward"] == "싱가포르" and res[t]["next"] is None and res[t]["value"] == pytest.approx(700_000)
    assert res[u]["paths"]["alt"]["windows"][1]["value"] == pytest.approx(450_000) and res[u]["value"] == pytest.approx(600_000)
    assert res[n]["paths"]["alt"]["broken"] and pm.progress(con, p, [n], AFTER)[n]["status"] == "missed"
    # 가중 인정을 추가안에서 끄면 원래 값 35만이라 12월 45만에 못 미친다
    off = pm._load(con, _db_life_v2(con, alt=[{**ALT_V2[0], "boost": False}]))
    ru = pm.progress(con, off, [t, u], AFTER)
    assert ru[u]["status"] == "missed" and ru[u]["paths"]["alt"]["windows"][1]["value"] == pytest.approx(350_000) and ru[t]["status"] == "achieved"
    root = env["root"]["path_key"]
    tot = _phase_check(con, p, scope, AFTER, root)
    assert (tot["n_achieved"], tot["n_sum"], tot["n_chain"]) == (4, 1, 1) and _alt_shape(tot) == (3, [("done", 2), ("done", 3)], 2)   # 창마다 그 창만 본다
    # 확정 전 예상과 확정한 값이 같다
    key = lambda rows: {x["agent_id"]: (x["value"], x["tier_no"], x["reward"], x["outcome"]) for x in rows if x["agent_id"] in scope}
    est = key(pm.final_results(con, p, scope, AFTER)["rows"])
    assert {a: v[3] for a, v in est.items()} == {e: "achieved", s: "achieved", t: "achieved", u: "achieved", n: "missed"}
    done = pm.confirm_results(con, pid, "admin", AFTER)
    assert done["settlement"] == "confirmed" and key(done["rows"]) == est == key(pm.final_results(con, p, scope, AFTER)["rows"])
    # 확정 때의 규칙과 계약으로 다시 계산해도 추가안 달성이 그대로다. 그 뒤에 규칙을 고쳐도 확정 때의 추가안으로 본다
    pm.update(con, pid, by="t", name="시험 여행시상 변경판", kind="travel", windows=Q4, combine_rule="either", categories=["health"],
              insurer_id=p["insurer_id"], insurer_name="샘플생명", date_basis="contract_date", pay_timing="inst13",
              tiers=_tiers((1_000_000, "싱가포르")), rules={"alt_chains": []})
    assert pm._load(con, pid)["rules"]["alt_chains"] == []
    got = {x["agent_id"]: x for x in pm.inst13_results(con, pm._load(con, pid), scope, date(2028, 1, 10))["rows"]}
    assert set(got) == {e, s, t, u}
    assert all(x["decision"] == "pay" and x["outcome_kept"] == "achieved" and x["tier_no_kept"] == 1 for x in got.values())


def test_alt_chain_validation(env):
    con = env["con"]
    life = con.execute("SELECT insurer_id FROM insurer WHERE name='샘플생명'").fetchone()[0]
    wt = {1: _tiers((100_000, "x")), 2: _tiers((200_000, "x")), 3: _tiers((400_000, "x"))}
    base = dict(period_ym=YM, name="x", kind="travel", windows=Q4, insurer_id=life, date_basis="contract_date", by="t")

    def make(combine="either", alt=ALT_V2, boost=None, **rules):
        return pm.create(con, combine_rule=combine, tiers=_tiers((1_000_000, "x")) if combine != "all" else (), window_tiers=wt,
                         rules={"alt_chains": alt, "boost": boost, **rules}, **base)

    for bad in ("both", "all"):                          # both 와는 함께 쓰지 않는다
        with pytest.raises(ValueError):
            make(combine=bad)
    with pytest.raises(ValueError):
        pm.create(con, combine_rule="sum", tiers=_tiers((1_000_000, "x")), rules={"alt_chains": ALT_V2}, **base)
    for bad in ({"windows": {4: 1}}, {"windows": {}}, {"windows": {2: 0}}, {"requires_missed": [0]}, {"tier_no": 2}, {"floors": {2: 1}}):
        with pytest.raises(ValueError):
            make(alt=[{**ALT_V2[0], **bad}])
    with pytest.raises(ValueError):                      # 열쇠가 겹친다
        make(alt=[ALT_V2[0], ALT_V2[0]])
    with pytest.raises(ValueError):                      # 모든 조건에 쓰는 가중 인정은 추가안에서만 끌 수 없다
        make(alt=[{**ALT_V2[0], "boost": False}], boost={"factor": 2, "month_days": [1, 14]})
    parent = make()
    with pytest.raises(ValueError):                      # 딸린 시책에는 두지 않는다
        make(parent_promo_id=parent)
    # 열쇠와 이름이 비면 기본값이다. 추가안이 없는 프로모션은 paths.alt 가 None 이고 조직 줄의 alt 도 None 이다
    p = pm._load(con, make(alt=[{"windows": {"2": "250000"}, "requires_missed": ["1"]}]))
    assert p["rules"]["alt_chains"] == [{"key": "alt1", "label": "추가안", "windows": {2: 250_000}, "requires_missed": [1], "boost": True, "tier_no": 1}]
    a = env["fcs"](env["branches"][env["hqs"][0]["org_id"]][0])[0]
    plain = pm._load(con, _db_life(con))
    r = pm.progress(con, plain, [a], MID)[a]
    assert r["paths"]["alt"] is None and r["paths"]["alts"] == []
    tot = pm.org_progress(con, plain, env["root"]["path_key"], [a], MID, near_pct=30)["total"]
    assert tot["alt"] is None and tot["alts"] == []
    with pytest.raises(ValueError):                      # 결합을 바꾸려면 추가안을 먼저 비운다
        pm.update(con, parent, by="t", name="x", kind="travel", windows=Q4, combine_rule="both", insurer_id=life, date_basis="contract_date",
                  tiers=_tiers((1_000_000, "x")))


# ------------------------------------------------------------------ 13. 조직 줄의 실적 합계 (value_sum, plain_sum, sum_value)
def _sums(t):
    return [(ph["state"], ph["value_sum"], ph["plain_sum"]) for ph in t["phases"]]


def test_value_sums_follow_the_promotion_metric_converted_premium(env):
    """지표가 인정보험료인 흥국 BOOM-UP 꼴. 창별 합계와 합산 값이 사람별 progress() 의 합과 같고, 보험료 지표의 같은 프로모션과는 다르다."""
    con, add = env["con"], env["add"]
    a1, a2, a3, a4, _idle = scope = _five(env)
    root = env["root"]["path_key"]
    kw = {"insurer": "한빛손해", "product": "한빛손해 건강종합보험"}
    add(a1, "2026-10-10", 100_000, converted=60_000, **kw)
    add(a2, "2026-10-12", 50_000, converted=30_000, **kw)       # 보험료로는 10월 5만을 채우고 인정보험료로는 못 채운다
    add(a3, "2026-10-14", 40_000, converted=80_000, **kw)       # 거꾸로다
    add(a4, "2026-10-16", 60_000, converted=70_000, **kw)       # 퇴사자
    add(a1, "2026-11-10", 100_000, converted=130_000, **kw)
    add(a3, "2026-11-05", 200_000, converted=250_000, **kw)
    add(a1, "2026-12-10", 200_000, converted=210_000, **kw)     # 11월 17일에 보면 기준일 뒤 날짜의 계약이다
    con.execute("UPDATE agent SET status='terminated', termination_date='2026-11-10' WHERE agent_id=?", (a4,))
    con.commit()
    conv = pm._load(con, _heungkuk(con, metric="converted_premium"))
    prem = pm._load(con, _heungkuk(con))
    assert (conv["metric"], conv["metric_ko"], prem["metric"]) == ("converted_premium", "인정보험료", "premium_monthly")

    t = _phase_check(con, conv, scope, MID, root)
    assert _sums(t) == [("done", 170_000, None), ("open", 380_000, None), ("future", None, None)]
    assert _shape(t)[0] == ("done", 2, 2)                       # a1, a3. 인정보험료로 센다
    # 합산 값은 대상자만 더한다. 퇴사자 몫은 value 에만 있다. 기준일 뒤 날짜의 계약은 지금 동작대로 합산에 들어 있다
    assert (t["sum_value"], t["value"], t["value_left"], t["n_left"]) == (760_000, 830_000, 70_000, 1)
    assert t["sum_value"] - sum(ph["value_sum"] for ph in t["phases"] if ph["value_sum"] is not None) == 210_000
    res = pm.org_progress(con, conv, root, scope, MID, near_pct=30)
    assert res["metric"] == "converted_premium" and res["unit"] == "원"
    rows = {r["path_key"]: r for r in res["rows"]}
    hq1, hq2 = (rows[h["path_key"]] for h in env["hqs"])
    assert ([ph["value_sum"] for ph in hq1["phases"]], hq1["sum_value"], hq1["value"]) == ([90_000, 130_000, None], 430_000, 430_000)
    assert ([ph["value_sum"] for ph in hq2["phases"]], hq2["sum_value"], hq2["value"]) == ([80_000, 250_000, None], 330_000, 400_000)
    # 사람별 progress() 와 같은 숫자다. 한 사람만 물어도 같다
    per = pm.progress(con, conv, scope, MID)
    assert [per[a]["windows"][0]["value"] for a in (a1, a2, a3)] == [60_000, 30_000, 80_000]
    assert pm.progress(con, conv, [a3], MID)[a3]["value"] == per[a3]["value"] == 330_000
    # 기간이 끝나면 창별 합계의 합이 합산 값이다
    t = _phase_check(con, conv, scope, AFTER, root)
    assert _sums(t) == [("done", 170_000, None), ("done", 380_000, None), ("done", 210_000, None)]
    assert t["sum_value"] == sum(ph["value_sum"] for ph in t["phases"]) == 760_000

    # 같은 계약을 보험료 지표로 세면 다른 숫자다
    t = _phase_check(con, prem, scope, MID, root)
    assert _sums(t) == [("done", 190_000, None), ("open", 300_000, None), ("future", None, None)]
    assert _shape(t)[0] == ("done", 2, 2) and (t["sum_value"], t["value"]) == (690_000, 750_000)


def test_value_sums_with_boost_and_alt_chain(env):
    """DB생명 변경판. value_sum 은 조건 1의 인정값, plain_sum 은 합산용 원래 값, 추가안의 value_sum 은 추가안 대상자만 더한다."""
    con, add = env["con"], env["add"]
    b1, _b2, b3, b4, _idle = scope = _five(env)
    root = env["root"]["path_key"]
    p = pm._load(con, _db_life_v2(con))
    add(b1, "2026-10-03", 50_000)                        # 10만으로 인정
    add(b3, "2026-10-20", 90_000)                        # 3주차라 그대로. 10월을 놓친다
    add(b4, "2026-10-10", 60_000)                        # 12만으로 인정
    t = _phase_check(con, p, scope, OCT_END, root)
    assert _sums(t) == [("open", 310_000, 200_000), ("future", None, None), ("future", None, None)]
    assert t["sum_value"] == t["value"] == 200_000
    assert [ph["value_sum"] for ph in t["alt"]["phases"]] == [None, None]

    add(b1, "2026-11-03", 50_000)                        # 11월은 2배가 없다
    add(b3, "2026-11-10", 250_000)                       # 추가안 대상자의 11월
    t = _phase_check(con, p, scope, MID, root)
    assert _sums(t) == [("done", 310_000, 200_000), ("open", 300_000, 300_000), ("future", None, None)]
    assert t["sum_value"] == 500_000
    # 추가안 대상은 10월을 놓친 세 사람이고 11월 실적은 그 가운데 한 사람에게만 있다. 10월을 채운 b1 의 5만은 넣지 않는다
    assert (t["alt"]["n_eligible"], [ph["value_sum"] for ph in t["alt"]["phases"]]) == (3, [250_000, None])

    add(b3, "2026-12-05", 400_000)                       # 12월 1주차. 10만까지 2배라 인정값 50만
    t = _phase_check(con, p, scope, DEC_END, root)
    assert _sums(t) == [("done", 310_000, 200_000), ("done", 300_000, 300_000), ("open", 500_000, 400_000)]
    assert t["sum_value"] == 900_000 == sum(ph["plain_sum"] for ph in t["phases"])
    assert [ph["value_sum"] for ph in t["alt"]["phases"]] == [250_000, 500_000]
    # 추가안을 합산용 값으로 재게 하면(boost 거짓) 추가안 합계도 원래 값이다
    flat = pm._load(con, _db_life_v2(con, alt=[{**ALT_V2[0], "boost": False}]))
    t = _phase_check(con, flat, scope, DEC_END, root)
    assert [ph["value_sum"] for ph in t["alt"]["phases"]] == [250_000, 400_000]
    # 가중 인정을 모든 조건에 쓰면 합산에도 배수가 들어간다. plain_sum 은 value_sum 과 같고 sum_value 도 늘어난다
    every = pm._load(con, _db_life_v2(con, alt=[], boost={**BOOST_V2, "applies_to": "all"}))
    t = _phase_check(con, every, scope, DEC_END, root)
    assert _sums(t) == [("done", 310_000, 310_000), ("done", 300_000, 300_000), ("open", 500_000, 500_000)]
    assert t["sum_value"] == 1_110_000 and t["alt"] is None


def test_value_sums_for_other_combine_rules_rate_and_attached(env):
    """합산 조건이 없는 결합과 정률은 sum_value 가 value 와 같다(퇴사자 몫 포함). 창별 합계는 대상자만 더한다. 딸린 시책은 부모의 값이다."""
    con, add = env["con"], env["add"]
    a1, a2, *_rest = scope = _five(env)
    root = env["root"]["path_key"]
    for mon in ("10", "11", "12"):
        add(a1, f"2026-{mon}-20", 450_000)
    add(a2, "2026-10-20", 100_000)
    con.execute("UPDATE agent SET status='terminated', termination_date='2026-11-10' WHERE agent_id=?", (a2,))
    con.commit()

    def check(pid, ref, boosted=False):
        p = pm._load(con, pid)
        res = pm.org_progress(con, p, root, scope, ref, near_pct=30)
        _value_check(res, pm.progress(con, p, scope, ref), boosted)
        return res["total"]

    single = pm.create(con, period_ym=YM, name="z", kind="monthly", windows=[Q4[0]], tiers=[(200_000, "fixed", 1, None)],
                       date_basis="contract_date", by="t")
    t = check(single, AFTER)
    assert _sums(t) == [("done", 450_000, None)]
    assert (t["sum_target"], t["sum_value"], t["value"], t["value_left"]) == (None, 550_000, 550_000, 100_000)
    rate = pm.create(con, period_ym=YM, name="r", kind="rate", windows=Q4[:2], rates=[(None, "initial_premium", 0.5, "next_month")],
                     date_basis="contract_date", by="t")
    t = check(rate, MID)
    assert _sums(t) == [("done", 450_000, None), ("open", 450_000, None)] and (t["sum_value"], t["value"]) == (1_000_000, 1_000_000)
    summed = pm.create(con, period_ym=YM, name="s", kind="travel", windows=Q4, tiers=_tiers((1_000_000, "x")), combine_rule="sum",
                       date_basis="contract_date", by="t")
    t = check(summed, MID)
    assert _sums(t) == [("done", 450_000, None), ("open", 450_000, None), ("future", None, None)]
    assert (t["sum_target"], t["sum_value"], t["value"], t["value_left"]) == (1_000_000, 1_350_000, 1_450_000, 100_000)
    # 범위가 비어도 끝난 창과 진행 중 창은 0 이고 시작 전 창은 None 이다
    t = pm.org_progress(con, pm._load(con, summed), root, [], MID, near_pct=30)["total"]
    assert _sums(t) == [("done", 0, None), ("open", 0, None), ("future", None, None)] and t["sum_value"] == 0
    # 딸린 시책은 부모의 창과 값으로 낸다
    parent = _db_life(con)
    conv = pm.create(con, period_ym=YM, name="시험 Convention", kind="travel", windows=Q4, tiers=_tiers((1, "추가 시책 원문 확인")),
                     combine_rule="sum", date_basis="contract_date", rules={"parent_promo_id": parent}, by="t", publish=True)
    got, want = check(conv, AFTER, boosted=True), check(parent, AFTER, boosted=True)
    assert got["phases"] == want["phases"] and got["sum_value"] == want["sum_value"] == 1_350_000


def test_metric_can_be_changed_after_publish(env):
    """게시한 프로모션의 지표를 pm.update 로 인정보험료로 바꿀 수 있다. 변경 기록에 남고 그 뒤의 진척은 새 지표로 센다."""
    con, add = env["con"], env["add"]
    a1, a2, *_rest = scope = _five(env)
    root = env["root"]["path_key"]
    kw = {"insurer": "한빛손해", "product": "한빛손해 건강종합보험"}
    add(a1, "2026-10-10", 100_000, converted=40_000, **kw)
    add(a2, "2026-10-12", 30_000, converted=90_000, **kw)
    pid = _heungkuk(con)
    before = pm._load(con, pid)
    assert before["status"] == "published" and before["metric"] == "premium_monthly"
    t = pm.org_progress(con, before, root, scope, OCT_END, near_pct=30)["total"]
    assert (t["phases"][0]["value_sum"], t["phases"][0]["n_met"]) == (130_000, 1)
    # update 는 준 값으로 모두 바꾼다. 지표만 바꾸려면 나머지는 지금 값을 그대로 넘긴다
    out = pm.update(con, pid, by="t", name=before["name"], kind=before["kind"], windows=[(w["label"], w["date_from"], w["date_to"]) for w in before["windows"]],
                    tiers=[(x["min_value"], x["reward_kind"], x["reward_value"], x["reward_label"]) for x in before["tiers"]],
                    insurer_id=before["insurer_id"], insurer_name=before["insurer_name"], sector=before["sector"], categories=before["categories"],
                    target_note=before["target_note"], date_basis=before["date_basis"], per_contract_cap=before["per_contract_cap"],
                    combine_rule=before["combine_rule"], pay_timing=before["pay_timing"], caution=before["caution"], metric="converted_premium")
    assert out["changed"] == ["metric"]
    after = pm._load(con, pid)
    assert (after["status"], after["metric"], after["metric_ko"], after["unit"]) == ("published", "converted_premium", "인정보험료", "원")
    assert after["window_tiers"].keys() == before["window_tiers"].keys() and after["rules"] == before["rules"]
    log = con.execute("SELECT target, before_json, after_json FROM audit_log WHERE action='promotion_update' ORDER BY id DESC LIMIT 1").fetchone()
    assert log["target"] == f"{pid}:metric" and "premium_monthly" in log["before_json"] and "converted_premium" in log["after_json"]
    t = _phase_check(con, after, scope, OCT_END, root)
    assert (t["phases"][0]["value_sum"], t["phases"][0]["n_met"], t["sum_value"]) == (130_000, 1, 130_000)
    per = pm.progress(con, after, scope, OCT_END)
    assert (per[a1]["windows"][0]["value"], per[a2]["windows"][0]["value"]) == (40_000, 90_000)      # 달성한 사람이 a1 에서 a2 로 바뀐다
    assert not per[a1]["windows"][0]["tier"] and per[a2]["windows"][0]["tier"]


# ------------------------------------------------------------------ 14. 확정한 프로모션의 조직별 진척, 센 계약 목록, 미달성 인원
ROW_KEYS = ("n_target", "n_active", "n_achieved", "n_missed", "n_lost", "n_inactive", "value", "sum_value", "n_chain", "n_sum", "phases", "alt", "alts",
            "windows", "agent_ids")
CONTRACT_KEYS = {"contract_id", "window_seq", "window_label", "date", "contract_date", "insurer", "product", "premium", "converted", "count",
                 "value", "value_each", "boosted", "status", "after_ref"}


def _three_ways(env, **kw):
    """DB생명 변경판을 12월 말까지 채운다. 원안 연속(e), 합산(s), 추가안(t, u)으로 달성하고 n 은 놓친다. 프로모션 번호와 다섯 사람을 돌려준다."""
    add = env["add"]
    e, s, t, u, n = scope = _five(env)
    for mon, v in (("10", 100_000), ("11", 200_000), ("12", 400_000)):
        add(e, f"2026-{mon}-20", v)
    add(s, "2026-10-20", 1_100_000)
    add(t, "2026-11-20", 250_000)
    add(t, "2026-12-20", 450_000)
    add(u, "2026-11-20", 250_000)
    add(u, "2026-12-05", 350_000)                        # 12월 1주차. 인정값 45만
    add(n, "2026-11-20", 240_000)
    add(n, "2026-12-20", 450_000)
    return _db_life_v2(env["con"], **kw), scope


def _fin(con, p, scope, ref=AFTER):
    return {x["agent_id"]: x for x in pm.final_results(con, p, scope, ref)["rows"]}


def test_confirmed_org_progress_keeps_confirmed_values_and_revoke_recalculates(env):
    """확정한 프로모션의 조직별 진척은 확정 때 남긴 값으로 센다. 확정 뒤에 계약을 철회하고, 새 계약을 넣고, 사람이 퇴사하고, 조건을 고쳐도
    숫자가 그대로이고 사람별 확정 값(final_results)과 같다. 무르면 다시 계산한 값으로 돌아간다."""
    con, add = env["con"], env["add"]
    root = env["root"]["path_key"]
    pid, scope = _three_ways(env)
    e, s, t, u, n = scope
    p = pm._load(con, pid)
    before = pm.org_progress(con, p, root, scope, AFTER, near_pct=30)
    assert before["confirmed"] is False and before["confirm"] is None and before["detail"] is None
    assert before["estimate_note"] == pm.ESTIMATE_NOTE and pm.confirmed_progress(con, p, scope) is None
    est = _fin(con, p, scope)
    pm.confirm_results(con, pid, "admin", AFTER)
    got = pm.org_progress(con, p, root, scope, MID, near_pct=30)        # 기준일은 쓰지 않는다. 창은 모두 끝난 것으로 본다
    assert got["confirmed"] is True and got["detail"] == "full" and got["confirm"]["ref_date"] == AFTER.isoformat()
    assert got["estimate_note"] == got["amounts"]["note"] == pm.CONFIRMED_NOTE and got["note"] == pm.ORG_NOTE_CONFIRMED
    # 바뀐 것이 없으면 확정 전 예상과 같은 숫자다
    assert len(got["rows"]) == len(before["rows"]) == 2
    for a, b in zip(got["rows"] + [got["total"]], before["rows"] + [before["total"]]):
        assert {k: a.get(k) for k in ROW_KEYS} == {k: b.get(k) for k in ROW_KEYS}
        assert (a["n_open"], a["n_near"], a["n_left"], a["value_left"], a["unnamed"]) == (0, 0, 0, 0, None)
    assert [ph["state"] for ph in got["total"]["phases"]] == ["done", "done", "done"]
    assert (got["total"]["n_achieved"], got["total"]["n_missed"], got["total"]["n_sum"], got["total"]["n_chain"]) == (4, 1, 1, 1)
    assert _alt_shape(got["total"]) == (3, [("done", 2), ("done", 3)], 2)

    # 확정 뒤에 원장과 조건이 바뀐다
    con.execute("UPDATE contract SET status='withdrawn' WHERE agent_id=?", (e,))           # 달성자의 계약이 모두 철회됐다
    add(n, "2026-12-21", 2_000_000)                                                        # 놓친 사람에게 늦게 들어온 계약
    con.execute("UPDATE agent SET status='terminated', termination_date='2027-01-06' WHERE agent_id=?", (s,))
    con.commit()
    pm.update(con, pid, by="t", name="시험 여행시상 변경판", kind="travel", windows=Q4, combine_rule="either", categories=["health"],
              insurer_id=p["insurer_id"], insurer_name="샘플생명", date_basis="contract_date", tiers=_tiers((1_000_000, "싱가포르")),
              rules={"alt_chains": []})
    p = pm._load(con, pid)
    live = pm.progress(con, p, scope, AFTER)
    assert live[e]["value"] == 0 and live[n]["status"] == "achieved"                       # 다시 세면 다른 숫자다
    again = pm.org_progress(con, p, root, scope, AFTER, near_pct=30)
    assert again["rows"] == got["rows"] and again["total"] == got["total"] and again["windows"] == got["windows"]
    # 같은 사람의 같은 숫자: 사람별 확정 값과 같다
    fin = _fin(con, p, scope)
    key = lambda x: (x["value"], x["tier_no"], x["reward"], x["amount"], x["outcome"])
    assert set(fin) == set(scope) and {a: key(x) for a, x in fin.items()} == {a: key(x) for a, x in est.items()}
    per = pm.confirmed_progress(con, p, scope)
    assert set(per) == set(scope)
    for a, x in fin.items():
        r = per[a]
        assert (r["value"], (r["tier"] or {}).get("tier_no"), r["reward"], r["amount"], r["outcome"]) == key(x)
        assert r["confirmed"] and r["next"] is None and r["status"] == ("achieved" if x["outcome"] == "achieved" else "missed")
        assert sum(w["plain"] for w in r["window_values"]) == pytest.approx(x["value"])
        assert [w["value"] for w in r["windows"]] == [w["value"] for w in r["window_values"]]
    assert per[u]["paths"]["alt"]["achieved"] and per[e]["paths"]["each"]["tier_no"] == 1  # 확정 때의 규칙과 계약으로 본 길
    for r in again["rows"]:
        ids = r["agent_ids"]
        assert r["value"] == pytest.approx(sum(fin[a]["value"] for a in ids)) == pytest.approx(r["sum_value"])
        assert r["n_achieved"] == sum(1 for a in ids if fin[a]["tier_no"]) == sum(1 for a in ids if fin[a]["outcome"] == "achieved")
        assert r["n_missed"] == r["n_lost"] == r["n_target"] - r["n_achieved"] and r["left_ids"] == []
        for i, ph in enumerate(r["phases"]):
            assert ph["value_sum"] == pytest.approx(sum(per[a]["window_values"][i]["value"] for a in ids))
            assert ph["plain_sum"] == pytest.approx(sum(per[a]["window_values"][i]["plain"] for a in ids))
    assert s in [a for r in again["rows"] for a in r["agent_ids"]]                         # 확정 뒤에 퇴사한 사람은 확정 대상 그대로다
    # 범위 밖 사람은 확정 값에서도 세지 않는다
    part = pm.org_progress(con, p, root, [e, n], AFTER, near_pct=30)
    assert part["total"]["n_target"] == 2 and part["total"]["n_achieved"] == 1 and set(pm.confirmed_progress(con, p, [e, n])) == {e, n}

    # 무르면 다시 계산한 값이다
    pm.revoke_confirmation(con, pid, "admin", "늦게 들어온 계약 반영")
    back = pm.org_progress(con, p, root, scope, AFTER, near_pct=30)
    assert back["confirmed"] is False and back["detail"] is None and back["estimate_note"] == pm.ESTIMATE_NOTE
    assert pm.confirmed_progress(con, p, scope) is None
    live = pm.progress(con, p, scope, AFTER)
    for r in back["rows"]:
        assert r["value"] == pytest.approx(sum(live[a]["value"] for a in r["agent_ids"] + r["left_ids"]))
        assert r["n_achieved"] == sum(1 for a in r["agent_ids"] if pm.is_achieved(p, live[a]))
    t_back = back["total"]
    assert (t_back["n_target"], t_back["n_left"], t_back["value_left"], t_back["alt"]) == (4, 1, 1_100_000, None)      # 퇴사자와 고친 조건이 반영된다
    assert t_back["n_achieved"] == 1 and e not in [a for r in back["rows"] for a in r["agent_ids"] if pm.is_achieved(p, live[a])]
    assert t_back["value"] == pytest.approx(got["total"]["value"] - 700_000 + 2_000_000)


def test_counted_contracts_add_up_to_person_values_live_and_confirmed(env):
    """센 계약 목록의 합이 사람별 값과 같다. 확정 전에는 progress(), 확정 뒤에는 확정 값이다. 고객 이름과 증권번호는 싣지 않는다."""
    con, add = env["con"], env["add"]
    pid, scope = _three_ways(env)
    e, _s, _t, u, n = scope
    add(e, "2026-11-25", 30_000, status="withdrawn")                   # 세지 않는 계약
    add(e, "2026-11-26", 40_000, self_family=1)                        # 본인 계약 제외
    add(e, "2026-12-28", 10_000)                                       # 12월 15일에 보면 기준일 뒤 날짜의 계약이다
    p = pm._load(con, pid)
    for ref in (DEC_MID, AFTER):
        per = pm.progress(con, p, scope, ref)
        for a in scope:
            cc = pm.counted_contracts(con, p, a, ref)
            assert (cc["confirmed"], cc["source"], cc["ref"], cc["metric"], cc["unnamed"]) == (False, "ledger", ref.isoformat(), "premium_monthly", 0)
            assert cc["value"] == pytest.approx(per[a]["value"]) == pytest.approx(sum(x["value"] for x in cc["rows"]))
            assert cc["n"] == len(cc["rows"]) and all(set(x) == CONTRACT_KEYS for x in cc["rows"])
            for w in per[a]["windows"]:
                mine = [x for x in cc["rows"] if x["window_seq"] == w["seq"]]
                assert sum(x["value_each"] for x in mine) == pytest.approx(w["value"]) and sum(x["value"] for x in mine) == pytest.approx(w["plain"])
                assert all(x["window_label"] == w["label"] and w["date_from"] <= x["date"] <= w["date_to"] for x in mine)
    rows = pm.counted_contracts(con, p, e, DEC_MID)["rows"]
    assert [(x["date"], x["value"], x["status"], x["after_ref"]) for x in rows] == [
        ("2026-10-20", 100_000, "inforce", False), ("2026-11-20", 200_000, "inforce", False), ("2026-12-20", 400_000, "inforce", True),
        ("2026-12-28", 10_000, "inforce", True)]
    assert rows[0]["insurer"] == "샘플생명" and rows[0]["product"] == "샘플생명 건강보험" and rows[0]["premium"] == rows[0]["converted"] == 100_000
    boosted = pm.counted_contracts(con, p, u, AFTER)["rows"]
    assert [(x["date"], x["value"], x["value_each"], x["boosted"]) for x in boosted] == [
        ("2026-11-20", 250_000, 250_000, False), ("2026-12-05", 350_000, 450_000, True)]

    est = _fin(con, p, scope)
    pm.confirm_results(con, pid, "admin", AFTER)
    con.execute("UPDATE contract SET status='withdrawn' WHERE agent_id=? AND contract_date='2026-10-20'", (e,))
    add(n, "2026-12-21", 2_000_000)
    con.commit()
    fin = _fin(con, p, scope)
    for a in scope:
        cc = pm.counted_contracts(con, p, a, date(2027, 3, 1))
        assert (cc["confirmed"], cc["source"], cc["ref"], cc["unnamed"]) == (True, "confirm", AFTER.isoformat(), None)
        assert cc["value"] == pytest.approx(fin[a]["value"]) == pytest.approx(est[a]["value"])
        assert all(set(x) == CONTRACT_KEYS and not x["after_ref"] for x in cc["rows"])
    kept = pm.counted_contracts(con, p, e)["rows"]
    assert [(x["date"], x["value"], x["status"]) for x in kept][0] == ("2026-10-20", 100_000, "withdrawn")     # 확정 값은 그대로, 상태는 지금 것
    assert 2_000_000 not in [x["value"] for x in pm.counted_contracts(con, p, n)["rows"]]
    assert [(x["value"], x["value_each"], x["boosted"]) for x in pm.counted_contracts(con, p, u)["rows"]] == [(250_000, 250_000, False), (350_000, 450_000, True)]
    # 무르면 다시 원장에서 센다
    pm.revoke_confirmation(con, pid, "admin", "늦게 들어온 계약 반영")
    live = pm.progress(con, p, scope, AFTER)
    for a in scope:
        cc = pm.counted_contracts(con, p, a, AFTER)
        assert cc["source"] == "ledger" and cc["value"] == pytest.approx(live[a]["value"])
    assert "2026-10-20" not in [x["date"] for x in pm.counted_contracts(con, p, e, AFTER)["rows"]]
    assert 2_000_000 in [x["value"] for x in pm.counted_contracts(con, p, n, AFTER)["rows"]]


def test_n_missed_splits_into_lost_and_open(env):
    """미달성 인원은 대상에서 최종 달성을 뺀 수다. 그 가운데 놓친 것이 정해진 사람(n_lost)과 진행 중인 사람(n_open)을 가른다.
    화면의 명단 열쇠 missed(달성하지 않은 사람 모두)와 최종 상태 miss, open 과 같은 조건이다."""
    con, add = env["con"], env["add"]
    a1, a2, a3, a4, idle = scope = _five(env)
    root = env["root"]["path_key"]
    kw = {"insurer": "한빛손해", "product": "한빛손해 건강종합보험"}
    p = pm._load(con, _heungkuk(con))
    for a, v in ((a1, 100_000), (a2, 50_000), (a3, 40_000)):           # a3 은 10월 5만을 놓친다. a4 와 idle 은 실적이 없다
        add(a, "2026-10-10", v, **kw)

    def check(ref):
        res = pm.org_progress(con, p, root, scope, ref, near_pct=30)
        per = pm.progress(con, p, scope, ref)
        for r in res["rows"]:
            ids = r["agent_ids"]
            assert r["n_missed"] == r["n_target"] - r["n_achieved"] == sum(1 for a in ids if not pm.is_achieved(p, per[a])) == r["n_lost"] + r["n_open"]
            assert r["n_lost"] == sum(1 for a in ids if not pm.is_achieved(p, per[a]) and per[a]["status"] == "missed")
        t = res["total"]
        for k in ("n_missed", "n_lost", "n_open"):
            assert t[k] == sum(r[k] for r in res["rows"])
        return t["n_target"], t["n_achieved"], t["n_missed"], t["n_lost"], t["n_open"]

    assert check(date(2026, 10, 20)) == (5, 0, 5, 0, 5)               # 10월이 끝나기 전에는 모두 진행 중이다
    for a, v in ((a1, 150_000), (a2, 100_000), (a3, 400_000)):
        add(a, "2026-11-10", v, **kw)
    assert check(MID) == (5, 0, 5, 3, 2)                               # 10월을 놓친 세 사람은 무효다. 진행 중인 두 사람도 미달성에 든다
    add(a1, "2026-12-10", 200_000, **kw)
    assert check(DEC_MID) == (5, 1, 4, 3, 1)                           # a1 달성. a2 는 12월이 남아 진행 중이다
    assert check(AFTER) == (5, 1, 4, 4, 0)                             # 기간이 끝나면 진행 중이 없다
    assert pm.org_progress(con, p, root, [], MID, near_pct=30)["total"]["n_missed"] == 0


def test_confirmed_org_progress_for_attached_and_old_confirmations(env):
    """확정 값으로 낼 수 없는 칸은 None 이다. 딸린 시책은 부모의 확정 때 규칙이 없어 창별 인원을 내지 않고 실적 합계만 낸다.
    규칙과 센 계약을 남기지 않은 예전 확정은 사람별 값과 달성 인원만 낸다."""
    con = env["con"]
    root = env["root"]["path_key"]
    parent, scope = _three_ways(env, alt=[])
    e, s, _t, _u, _n = scope
    conv = pm.create(con, period_ym=YM, name="시험 Convention", kind="travel", windows=Q4, tiers=_tiers((1, "추가 시책 원문 확인")),
                     combine_rule="sum", date_basis="contract_date", rules={"parent_promo_id": parent}, by="t", publish=True)
    c = pm._load(con, conv)
    before = pm.org_progress(con, c, root, scope, AFTER, near_pct=30)["total"]
    assert before["n_achieved"] == 2
    pm.confirm_results(con, conv, "admin", AFTER)
    con.execute("UPDATE contract SET status='withdrawn' WHERE agent_id=?", (e,))
    con.commit()
    res = pm.org_progress(con, c, root, scope, AFTER, near_pct=30)
    t = res["total"]
    assert res["confirmed"] and res["detail"] == "values"
    assert (t["n_target"], t["n_achieved"], t["n_missed"], t["value"], t["sum_value"]) == tuple(before[k] for k in ("n_target", "n_achieved", "n_missed", "value", "sum_value"))
    assert [(ph["state"], ph["target"], ph["n_met"], ph["n_chain"]) for ph in t["phases"]] == [("done", m, None, None) for m in (100_000, 200_000, 400_000)]
    assert [(ph["value_sum"], ph["plain_sum"]) for ph in t["phases"]] == [(ph["value_sum"], ph["plain_sum"]) for ph in before["phases"]]
    assert (t["n_chain"], t["n_sum"], t["windows"], t["unnamed"], t["sum_target"]) == (None, None, None, None, before["sum_target"])
    fin = _fin(con, c, scope)
    assert t["n_achieved"] == sum(1 for x in fin.values() if x["tier_no"]) and t["value"] == pytest.approx(sum(x["value"] for x in fin.values()))
    cc = pm.counted_contracts(con, c, e)                               # 딸린 시책은 부모의 계약이다
    assert (cc["confirmed"], cc["source"]) == (True, "confirm") and cc["value"] == pytest.approx(fin[e]["value"]) == 700_000
    assert {x["status"] for x in cc["rows"]} == {"withdrawn"}

    # 예전 확정: 규칙과 센 계약이 없다. 시험 DB 에서만 지워서 흉내 낸다
    pp = pm._load(con, parent)
    pm.confirm_results(con, parent, "admin", AFTER)
    head = pm.confirmation(con, parent)
    full = pm.org_progress(con, pp, root, scope, AFTER, near_pct=30)
    assert full["detail"] == "full" and full["total"]["n_achieved"] == 1 and full["total"]["phases"][0]["n_met"] == 1    # 확정 때는 e 의 계약이 이미 철회였다
    con.execute("UPDATE promotion_confirm SET rule_json=NULL WHERE confirm_id=?", (head["confirm_id"],))
    con.execute("DELETE FROM promotion_result_contract WHERE confirm_id=?", (head["confirm_id"],))
    con.commit()
    old = pm.org_progress(con, pp, root, scope, AFTER, near_pct=30)
    t = old["total"]
    assert old["confirmed"] and old["detail"] == "none"
    assert (t["n_target"], t["n_achieved"], t["n_missed"], t["value"]) == tuple(full["total"][k] for k in ("n_target", "n_achieved", "n_missed", "value"))
    assert [(ph["n_met"], ph["n_chain"], ph["value_sum"], ph["plain_sum"]) for ph in t["phases"]] == [(None, None, None, None)] * 3
    assert (t["n_chain"], t["n_sum"], t["windows"]) == (None, None, None) and t["sum_value"] == t["value"]
    per = pm.confirmed_progress(con, pp, scope)
    assert per[s]["window_values"] is None and per[s]["windows"] == [] and per[s]["detail"] is None and per[s]["tier"]["tier_no"] == 1
    gone = pm.counted_contracts(con, pp, s)
    assert (gone["confirmed"], gone["source"], gone["rows"], gone["value"]) == (True, None, [], 0)
