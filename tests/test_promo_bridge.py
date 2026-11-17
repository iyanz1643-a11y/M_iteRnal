"""월간 브릿지 시상: 달을 넘는 두 창, 창별 단계 기준, 낮은 쪽 단계(all), 정액과 비율 보상, 문턱 창, 진행 표시 값, 다음 달 복사.

세 프로모션(연속가동, 주력상품 연속가동 연장, 브릿지 연속)은 보험사 주차 포스터의 꼴만 따른 시험용이고 샘플 보험사로 만든다.
포스터에서 읽힌 숫자만 쓴다. 창 날짜, 단계 기준 10만에서 50만, '최대 300%' 다. 단계별 현금 금액은 그림 글자가 깨져 읽히지 않아
금액을 비워 둔다. 엔진은 그 금액을 '원문 확인' 으로 내야 한다. 운영 DB 에는 넣지 않는다.
D-93(문턱 창), D-94(비율 보상의 바탕), D-95(복사에서 옮기지 않는 값)는 제안대로 둔 가정이다. 실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import json
import pathlib
import tempfile
from datetime import date

import pytest

from app import db, masters
from app.engine import promotion as pm
from app.web import auth

YM = "202610"
W1_OPEN = date(2026, 10, 25)            # 창 1 진행 중, 창 2 시작 전
W2_OPEN = date(2026, 11, 10)            # 창 1 끝, 창 2 진행 중
AFTER = date(2026, 11, 20)              # 두 창 모두 끝
STEPS = [100_000, 200_000, 300_000, 500_000]                    # 연속가동 포스터의 단계 기준 (10만, 20만, 30만, 50만 꼴)
EXT_STEPS = [100_000, 200_000, 300_000, 400_000, 500_000]       # 연장 포스터 창 2의 단계 기준
MAIN_PRODUCTS = ["온동보장", "새로고침", "내돈내삼", "마이헬스파트너", "천만안심보장어카운트", "마이핏", "요양건강보험", "마이슈퍼스타",
                 "마이스타0515", "실속라이프"]
SAM_WINDOWS = [("10월 19일에서 31일", "2026-10-19", "2026-10-31"), ("11월 1일에서 15일", "2026-11-01", "2026-11-15")]
EXT_WINDOWS = [("10월 26일에서 31일", "2026-10-26", "2026-10-31"), ("11월 1일에서 15일", "2026-11-01", "2026-11-15")]


@pytest.fixture()
def env():
    con = db.init_db(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    masters.seed(con)
    auth.seed_accounts(con, "pw-12345678")
    fcs = [r[0] for r in con.execute(
        """SELECT h.agent_id FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id
           WHERE h.effective_to IS NULL AND a.job_type='fc' AND a.status='active' ORDER BY h.agent_id LIMIT 8""")]
    ins = {r["name"]: r["insurer_id"] for r in con.execute("SELECT insurer_id, name FROM insurer")}
    n = [0]

    def add(agent, day, premium, *, insurer="미래화재", raw="시험 건강보험", status="inforce"):
        n[0] += 1
        con.execute(
            """INSERT INTO contract(insurer_id, policy_no, product_id, product_code_raw, agent_id, application_no, application_date, contract_date,
                                    premium, premium_monthly, converted_premium, policy_count, status, payment_mode, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ins[insurer], f"B{n[0]:05d}", None, raw, agent, f"BA{n[0]:05d}", day, day, premium, premium, premium, 1, status, "monthly", db.now()))
        con.commit()

    yield {"con": con, "fcs": fcs, "add": add, "ins": ins}
    con.close()


def _unread(steps):
    """단계별 현금 정액. 금액이 원문 그림에만 있어 비워 둔다."""
    return [(m, "fixed", None, None) for m in steps]


def _sam(env, tiers=None, **kw):
    """연속가동 시상 꼴. 창 1과 창 2에 같은 꼴의 단계 기준, 단계별 현금 정액, 13회차 유지."""
    rows = tiers or _unread(STEPS)
    return pm.create(env["con"], period_ym=YM, name="시험 연속가동", kind="bridge", windows=SAM_WINDOWS, insurer_name="미래화재",
                     insurer_id=env["ins"]["미래화재"], date_basis="contract_date", tiers=rows,
                     window_tiers={1: _unread(STEPS), 2: _unread(STEPS)}, inst13_required=True, by="t", publish=True, **kw)


def _ext(env):
    """주력상품 연속가동 연장 꼴. 창 1은 10만 문턱 하나, 창 2는 10만에서 50만 5단계. 대상은 주력상품 이름 목록."""
    return pm.create(env["con"], period_ym=YM, name="시험 주력상품 연장", kind="bridge", windows=EXT_WINDOWS, insurer_name="미래화재",
                     insurer_id=env["ins"]["미래화재"], date_basis="contract_date",
                     window_tiers={1: _unread([100_000]), 2: _unread(EXT_STEPS)},
                     rules={"product_include": MAIN_PRODUCTS}, by="t", publish=True)


# ------------------------------------------------------------------ 1. 상태 판정
def test_bridge_states_across_month_boundary(env):
    con, add = env["con"], env["add"]
    miss1, ongoing, both, w1_only = env["fcs"][:4]
    add(miss1, "2026-11-03", 300_000)                               # 창 1을 놓침
    add(ongoing, "2026-10-20", 300_000)                             # 창 1 3단계
    add(ongoing, "2026-11-04", 120_000)                             # 창 2 1단계
    add(both, "2026-10-28", 250_000)                                # 창 1 2단계
    add(both, "2026-11-05", 550_000)                                # 창 2 4단계
    add(w1_only, "2026-10-20", 120_000)
    p = pm._load(con, _sam(env))
    assert p["kind"] == "bridge" and p["kind_ko"] == "브릿지 시상" and p["combine_rule"] == "all" and pm.has_inst13(p)
    ids = [miss1, ongoing, both, w1_only]

    # 창 2가 시작 전 (창 1 진행 중)
    r = pm.progress(con, p, [w1_only], W1_OPEN)[w1_only]
    b = r["bridge"]
    assert [w["state"] for w in r["windows"]] == ["active", "upcoming"]
    assert r["status"] == "in_progress" and r["tier"] is None and not r["broken"]
    assert b["stage"] == "open" and b["state"] == "open" and b["open_seq"] == 1 and b["target_seq"] == 1
    assert b["secured_tier_no"] is None and b["target_tier_no"] == 1
    assert b["next"]["tier_no"] == 2 and b["next"]["gap"] == pytest.approx(80_000) and not b["next"]["gate"]
    assert b["estimate"] is True and b["windows"][1]["state"] == "upcoming"

    mid = pm.progress(con, p, ids, W2_OPEN)
    # 창 1을 놓치면 무효
    assert mid[miss1]["status"] == "missed" and mid[miss1]["broken"] and mid[miss1]["next"] is None
    assert mid[miss1]["bridge"]["state"] == "void" and mid[miss1]["bridge"]["next"] is None and mid[miss1]["bridge"]["secured_tier_no"] == 0
    assert mid[w1_only]["bridge"]["state"] == "open" and mid[w1_only]["bridge"]["secured_tier_no"] == 1
    # 창 1 달성 뒤 창 2 진행 중
    g = mid[ongoing]
    assert g["status"] == "in_progress" and g["tier"]["tier_no"] == 1
    assert g["bridge"]["open_seq"] == 2 and g["bridge"]["open_label"] == "11월 1일에서 15일" and g["bridge"]["days_left"] == 5
    assert g["bridge"]["secured_tier_no"] == 3 and g["bridge"]["target_tier_no"] == 1
    assert g["bridge"]["next"]["tier_no"] == 2 and g["bridge"]["next"]["gap"] == pytest.approx(80_000)
    assert g["next"]["gap"] == pytest.approx(80_000)                # 기존 키와 같은 값
    # 창 2가 앞 창 단계에 닿으면 더 오르지 않는다
    h = mid[both]
    assert h["tier"]["tier_no"] == 2 and h["capped"] and h["bridge"]["capped"] and h["bridge"]["next"] is None

    end = pm.progress(con, p, ids, AFTER)
    # 두 창 모두 달성. 낮은 쪽 단계
    assert end[both]["status"] == "achieved" and end[both]["tier"]["tier_no"] == 2
    assert end[both]["bridge"]["state"] == "achieved" and end[both]["bridge"]["stage"] == "after" and end[both]["bridge"]["secured_tier_no"] == 2
    assert end[ongoing]["status"] == "achieved" and end[ongoing]["tier"]["tier_no"] == 1
    assert end[w1_only]["bridge"]["state"] == "void" and end[miss1]["bridge"]["state"] == "void"
    # 금액이 읽히지 않은 단계는 금액을 지어내지 않는다
    assert end[both]["amount"] is None and end[both]["reward"] == pm.MISSING_ORIGINAL and end[both]["bridge"]["secured_amount"] is None


def test_bridge_shows_in_both_months_and_before_start(env):
    con, add = env["con"], env["add"]
    a = env["fcs"][0]
    pid = _sam(env)
    assert [p["promo_id"] for p in pm.bridges(con, "202610", "fc")] == [pid]
    assert [p["promo_id"] for p in pm.bridges(con, "202611", "fc")] == [pid]
    assert pm.bridges(con, "202612", "fc") == []
    p = pm._load(con, pid)
    b = pm.progress(con, p, [a], date(2026, 10, 10))[a]["bridge"]
    assert b["stage"] == "before" and b["state"] == "before" and b["target_seq"] == 1 and b["open_seq"] is None
    assert b["next"]["gap"] == pytest.approx(100_000)
    add(a, "2026-10-31", 100_000)                                   # 창 1 마지막 날
    last = pm.progress(con, p, [a], pm.ref_date("202610"))[a]["bridge"]
    assert last["open_seq"] == 1 and last["windows"][1]["state"] == "upcoming"


def test_bridge_waiting_between_windows(env):
    con, add = env["con"], env["add"]
    a = env["fcs"][0]
    gap = [("10월 26일에서 29일", "2026-10-26", "2026-10-29"), ("11월 1일에서 15일", "2026-11-01", "2026-11-15")]
    pid = pm.create(con, period_ym=YM, name="시험 창 사이", kind="bridge", windows=gap, window_tiers={1: _unread(STEPS), 2: _unread(STEPS)},
                    date_basis="contract_date", by="t", publish=True)
    add(a, "2026-10-27", 200_000, insurer="한빛손해")
    r = pm.progress(con, pm._load(con, pid), [a], date(2026, 10, 31))[a]
    b = r["bridge"]
    assert r["status"] == "in_progress" and not r["broken"]
    assert b["stage"] == "between" and b["state"] == "waiting" and b["open_seq"] is None and b["target_seq"] == 2
    assert b["secured_tier_no"] == 2 and b["next"]["tier_no"] == 1 and b["next"]["gap"] == pytest.approx(100_000)


# ------------------------------------------------------------------ 2. 보상: 정액과 비율
def test_rate_reward_uses_total_of_both_windows(env):
    """비율 보상은 모든 창의 실적 합계에 곱한다 (D-94 가정). '최대 300%' 만 읽혀 맨 위 단계에만 둔다."""
    con, add = env["con"], env["add"]
    top, low = env["fcs"][:2]
    add(top, "2026-10-21", 500_000)
    add(top, "2026-11-02", 600_000)
    add(low, "2026-10-21", 200_000)
    add(low, "2026-11-02", 600_000)
    tiers = _unread(STEPS[:3]) + [(STEPS[3], "rate", 3.0, "최대 300%")]
    p = pm._load(con, _sam(env, tiers=tiers))
    res = pm.progress(con, p, [top, low], AFTER)
    assert res[top]["tier"]["tier_no"] == 4 and res[top]["reward"] == "300%"
    assert res[top]["amount"] == pytest.approx(3.0 * 1_100_000)
    assert res[low]["tier"]["tier_no"] == 2 and res[low]["amount"] is None and res[low]["reward"] == pm.MISSING_ORIGINAL
    assert res[top]["bridge"]["secured_reward"] == "300%" and res[top]["bridge"]["secured_amount"] is None


def test_withdrawn_and_application_do_not_count(env):
    """프로모션은 보험사 기준으로 지금 상태를 센다. 철회와 청약은 세지 않는다 (D-08)."""
    con, add = env["con"], env["add"]
    a = env["fcs"][0]
    add(a, "2026-10-20", 300_000)
    add(a, "2026-11-03", 300_000, status="withdrawn")
    add(a, "2026-11-04", 300_000, status="applied")
    r = pm.progress(con, pm._load(con, _sam(env)), [a], AFTER)[a]
    assert r["windows"][1]["value"] == 0 and r["bridge"]["state"] == "void"


# ------------------------------------------------------------------ 3. 문턱 창과 상품 이름 (D-93)
def test_gate_window_and_main_products(env):
    con, add = env["con"], env["add"]
    ok, wrong, mid = env["fcs"][:3]
    add(ok, "2026-10-27", 150_000, raw="무배당 내돈내삼 보험")
    add(ok, "2026-11-03", 420_000, raw="마이 핏 건강")                  # 공백은 무시한다
    add(wrong, "2026-10-27", 150_000, raw="시험 운전자보험")              # 주력상품이 아니다
    add(wrong, "2026-11-03", 500_000, raw="실속라이프")
    add(mid, "2026-10-30", 100_000, raw="새로고침")
    add(mid, "2026-11-04", 250_000, raw="요양건강보험")
    p = pm._load(con, _ext(env))
    assert pm.is_gate(p, p["windows"][0]) and not pm.is_gate(p, p["windows"][1])
    assert [t["min_value"] for t in p["tiers"]] == EXT_STEPS          # 공통 구간표가 없으면 마지막 창의 표에서 보상을 읽는다

    early = pm.progress(con, p, [wrong], date(2026, 10, 27))[wrong]          # 창 1에 주력상품 실적이 없다
    assert early["next"]["gate"] and early["next"]["reward"] == pm.GATE_TEXT
    assert early["bridge"]["next"]["gate"] and early["bridge"]["next"]["gap"] == pytest.approx(100_000)

    now = pm.progress(con, p, [mid], W2_OPEN)[mid]
    assert now["bridge"]["secured_tier_no"] == 5 and now["bridge"]["windows"][0]["gate"]
    assert now["tier"]["tier_no"] == 2 and now["bridge"]["next"]["tier_no"] == 3 and now["bridge"]["next"]["gap"] == pytest.approx(50_000)
    assert not now["bridge"]["capped"]

    end = pm.progress(con, p, [ok, wrong], AFTER)
    assert end[ok]["status"] == "achieved" and end[ok]["tier"]["tier_no"] == 4
    assert end[wrong]["status"] == "missed" and end[wrong]["bridge"]["state"] == "void"


def test_gate_is_bridge_only_and_validation(env):
    con = env["con"]
    with pytest.raises(ValueError):                                 # 연계 시상은 예전대로 구간 수가 같아야 한다
        pm.create(con, period_ym=YM, name="시험", kind="linked", windows=EXT_WINDOWS, window_tiers={1: _unread([100_000]), 2: _unread(EXT_STEPS)}, by="t")
    with pytest.raises(ValueError):                                 # 창 하나
        pm.create(con, period_ym=YM, name="시험", kind="bridge", windows=EXT_WINDOWS[:1], tiers=_unread(STEPS), by="t")
    with pytest.raises(ValueError):                                 # 겹치는 창
        pm.create(con, period_ym=YM, name="시험", kind="bridge", windows=[SAM_WINDOWS[0], ("겹침", "2026-10-31", "2026-11-15")], tiers=_unread(STEPS), by="t")
    for rule in ("sum", "either"):
        with pytest.raises(ValueError):
            pm.create(con, period_ym=YM, name="시험", kind="bridge", windows=SAM_WINDOWS, combine_rule=rule, tiers=_unread(STEPS),
                      window_tiers={1: _unread(STEPS), 2: _unread(STEPS)}, by="t")
    pid = pm.create(con, period_ym=YM, name="시험 공통표", kind="bridge", windows=SAM_WINDOWS, tiers=_unread(STEPS), by="t")
    assert pm._load(con, pid)["combine_rule"] == "all"


def test_bridge_survives_confirm(env):
    con, add = env["con"], env["add"]
    a = env["fcs"][0]
    add(a, "2026-10-20", 300_000)
    add(a, "2026-11-04", 200_000)
    pid = _sam(env)
    p = pm._load(con, pid)
    est = pm.final_results(con, p, [a], AFTER)["rows"][0]
    pm.confirm_results(con, pid, "admin", AFTER)
    fin = pm.final_results(con, pm._load(con, pid), [a], AFTER)["rows"][0]
    assert est["tier_no"] == fin["tier_no"] == 2 and fin["outcome"] == "achieved"


# ------------------------------------------------------------------ 4. 다음 달 복사
def test_copy_bridge_to_next_month(env):
    con = env["con"]
    src = _ext(env)
    p = pm._load(con, src)
    con.execute("UPDATE promotion_tier SET original_value=? WHERE promo_id=? AND tier_no=1 AND window_seq=2", (1, src))
    con.execute("UPDATE promotion SET retention_ref='2027-11-30', retention_min=0.95 WHERE promo_id=?", (src,))
    con.commit()
    out = pm.copy_bridge(con, src, period_ym="202611", windows=[("2026-11-24", "2026-11-30"), ("2026-12-01", "2026-12-15")], by="admin")
    new = pm._load(con, out["promo_id"])
    assert out["copied_from"] == src and new["copied_from"] == src and new["status"] == "draft" and new["period_ym"] == "202611"
    assert [(w["label"], w["date_from"], w["date_to"]) for w in new["windows"]] == [
        ("11월 24일에서 30일", "2026-11-24", "2026-11-30"), ("12월 1일에서 15일", "2026-12-01", "2026-12-15")]
    shape = lambda q: {s: [(t["min_value"], t["reward_kind"], t["reward_value"], t["reward_label"]) for t in rows]
                       for s, rows in q["window_tiers"].items()}
    assert shape(new) == shape(p) and new["kind"] == "bridge" and new["combine_rule"] == p["combine_rule"]
    assert new["rules"]["product_include"] == MAIN_PRODUCTS and new["rules"]["retention_min"] == pytest.approx(0.95)
    assert new["rules"]["retention_ref"] is None and set(out["cleared"]) == {"retention_ref", "original_value"}
    assert all(t["original_value"] is None for rows in new["window_tiers"].values() for t in rows)
    assert pm._load(con, src)["status"] == "published"                # 원본은 그대로
    log = con.execute("SELECT * FROM audit_log WHERE action='promotion_copy'").fetchone()
    assert log["target"] == f"{new['promo_id']}:{src}" and json.loads(log["before_json"])["promo_id"] == src
    assert json.loads(log["after_json"])["status"] == "draft"
    # 게시 전에는 설계사에게 보이지 않고 메인 관리자 목록에만 있다
    assert out["promo_id"] not in [x["promo_id"] for x in pm.bridges(con, "202612", "fc")]
    assert out["promo_id"] in [x["promo_id"] for x in pm.bridges(con, "202612", "main", include_unpublished=True)]
    with pytest.raises(ValueError):                                 # 같은 달로 두 번
        pm.copy_bridge(con, src, period_ym="202611", windows=[("2026-11-24", "2026-11-30"), ("2026-12-01", "2026-12-15")], by="admin")
    pm.set_status(con, out["promo_id"], "published", "admin")
    assert pm._load(con, out["promo_id"])["status"] == "published"


def test_copy_bridge_checks(env):
    con = env["con"]
    src = _sam(env)
    with pytest.raises(ValueError):                                 # 창 수가 다르다
        pm.copy_bridge(con, src, period_ym="202611", windows=[("2026-12-01", "2026-12-15")], by="admin")
    with pytest.raises(ValueError):                                 # 귀속 월 꼴
        pm.copy_bridge(con, src, period_ym="2026-11", windows=[("2026-11-24", "2026-11-30"), ("2026-12-01", "2026-12-15")], by="admin")
    with pytest.raises(ValueError):                                 # 엔진은 날짜 순서를 고쳐 주지 않는다
        pm.copy_bridge(con, src, period_ym="202611", windows=[("2026-12-01", "2026-12-15"), ("2026-11-24", "2026-11-30")], by="admin")
    weekly = pm.create(con, period_ym=YM, name="시험 주차", kind="weekly", windows=SAM_WINDOWS[:1], tiers=_unread(STEPS), by="t")
    with pytest.raises(ValueError):                                 # 브릿지만 복사한다
        pm.copy_bridge(con, weekly, period_ym="202611", windows=[("2026-11-24", "2026-11-30")], by="admin")
    out = pm.copy_bridge(con, src, period_ym="202611", windows=[("창 1", "2026-11-24", "2026-11-30"), ("창 2", "2026-12-01", "2026-12-15")],
                         by="admin", name="시험 11~12월 연속가동")
    new = pm._load(con, out["promo_id"])
    assert new["name"] == "시험 11~12월 연속가동" and [w["label"] for w in new["windows"]] == ["창 1", "창 2"]
    assert new["inst13_required"] and out["cleared"] == []
    assert [t["min_value"] for t in new["tiers"]] == STEPS and all(t["reward_value"] is None for t in new["tiers"])
    pm.ensure(con)                                                   # 이미 있는 칸은 건너뛴다
    pm.ensure(con)
