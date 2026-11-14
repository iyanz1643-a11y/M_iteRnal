"""실제 양식의 가짜 계약현황으로 계산 엔진을 본다.

실제 계약은 상품 마스터와 잇지 않고 계약에 구분(product_category)과 업권(sector)을 직접 싣는다.
엔진은 계약의 값을 먼저 읽고 없으면 마스터를 읽는다(app/engine/classify.py). 조직은 여섯 단계다.

열린 돈 기준은 여기서 단정하지 않는다.
- 일시납 보험료를 그대로 넣을지 환산할지, 철회 계약의 인정보험료가 원본에 음수로 오는지는 정해지지 않았다.
  시험은 엔진이 원장(contract)의 값을 고치지 않고 그대로 더하는지만 본다. 원장과 파일의 대조는 적재기 시험이 본다.
- 공동모집 건수와 1차년에서 4차년 성적도 같다. 건수는 원장의 policy_count 를 그대로 더하는지만 본다.
사람 이름과 금액은 출력하지 않는다. 비교만 한다.
"""
import pathlib
import tempfile
from collections import defaultdict

import openpyxl
import pytest

from app import db, masters
from app.engine import certification, classify, incentive, orgs, performance, ranking
from app.engine import org_award as oa
from app.engine import promotion as pm
from app.engine.performance import ACTIVE_STATUSES
from app.loader import eternal_contract as ec
from app.loader import eternal_employee as ee
from app.loader import fake_eternal as fe
from app.loader import mapping
from app.org import sync

YM, NEXT = "202602", "202603"
ELIGIBLE = ("whole_life", "health")          # 시상 대상 기본 구분 (masters.CATEGORIES 의 incentive_eligible=1)


@pytest.fixture()
def env():
    tmp = pathlib.Path(tempfile.mkdtemp())
    con = db.init_db(tmp / "t.db")
    for i, (code, name, elig) in enumerate(masters.CATEGORIES):
        con.execute("INSERT INTO product_category(code, name, incentive_eligible, sort_order) VALUES(?,?,?,?)", (code, name, elig, i))
    for k, (name, sector) in enumerate(fe.MASTER_INSURERS.items()):
        con.execute("INSERT INTO insurer(code, name, sector) VALUES(?,?,?)", (f"T{k:02d}", name, sector))
    con.commit()
    roster = fe.fake_roster()
    emp_path, _ = fe.make_employee_file(tmp / "emp.xlsx", roster)
    sync.sync_staff(con, ee.parse_employee_file(emp_path)["rows"], YM)       # 조직 여섯 단계, 설계사, 소속 이력
    path, rows = fe.make_contract_file(tmp / "contract.xlsx", period_ym=YM, roster=roster)
    loaded = ec.load_contract(con, path, YM)
    assert loaded["block"] == 0
    sync.sync_contract_orgs(con)
    performance.run(con, YM)
    ranking.run(con, YM)
    yield {"con": con, "tmp": tmp, "roster": roster, "path": path, "rows": rows, "loaded": loaded}
    con.close()


# ------------------------------------------------------------------ 도우미
def _raw(path):
    """적재기와 상관없이 원본 엑셀을 직접 읽는다."""
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    head = [c.value for c in ws[1]]
    return [dict(zip(head, r)) for r in ws.iter_rows(min_row=2, values_only=True) if any(v not in (None, "") for v in r)]


def _num(v):
    return int(str(v).replace(",", "")) if v not in (None, "") else 0


def _agent_ids(con):
    return {r["internal_code"]: r["agent_id"] for r in con.execute("SELECT agent_id, internal_code FROM agent")}


def _active_ids(con):
    return {r["agent_id"] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' AND job_type='fc'")}


def _expected_by_agent(con, ym, where="1=1", params=()):
    """업적 집계 규칙을 원장에서 직접 손으로 센다: 성립한 달에 더하고 철회한 달에 뺀다."""
    out = defaultdict(lambda: {"rp": 0, "cnt": 0})
    month = f"{ym[:4]}-{ym[4:]}-"
    q = f"""SELECT c.agent_id, c.status, c.contract_date, c.withdrawal_date, c.converted_premium rp, c.policy_count cnt
            FROM contract c {classify.JOINS} WHERE c.agent_id IS NOT NULL AND {where}"""
    for r in con.execute(q, params):
        if r["status"] in ACTIVE_STATUSES and (r["contract_date"] or "").startswith(month):
            out[r["agent_id"]]["rp"] += r["rp"] or 0
            out[r["agent_id"]]["cnt"] += r["cnt"] or 0
        if r["status"] == "withdrawn" and (r["withdrawal_date"] or "").startswith(month):
            out[r["agent_id"]]["rp"] -= r["rp"] or 0
            out[r["agent_id"]]["cnt"] -= r["cnt"] or 0
    return out


def _perf(con, ym):
    """그 달 업적 run 의 월 집계 {agent_id: 행}."""
    run = db.latest_run(con, "performance", ym)
    return {r["agent_id"]: dict(r) for r in con.execute(
        "SELECT * FROM agent_performance_summary WHERE run_id=? AND period_type='month' AND period_key=?", (run, ym))}


def _ranking(con, ym, metric="recognized_premium", scope="all"):
    run = db.latest_run(con, "ranking", ym)
    return {r["agent_id"]: r["value"] for r in con.execute(
        "SELECT agent_id, value FROM ranking_snapshot WHERE run_id=? AND period_type='month' AND period_key=? AND metric=? AND scope=?",
        (run, ym, metric, scope))}


def _division(con, **kw):
    kw.setdefault("top_n", 50)
    kw.setdefault("metric", "converted_premium")
    return oa.load(con, oa.save(con, by="t", org_id=sync.root_id(con), name=kw.pop("name", "시험 부문"), effective_from=YM, **kw))


# ------------------------------------------------------------------ 1. 구분: 종신·건강이 시상 대상으로 잡힌다
def test_real_contracts_are_classified_from_the_contract_itself(env):
    con, rows = env["con"], env["rows"]
    cmap = ec.load_category_map()
    # 실제 계약은 상품 마스터와 잇지 않는다. 그래도 구분은 계약에서 읽힌다
    assert con.execute("SELECT COUNT(*) FROM contract WHERE product_id IS NOT NULL").fetchone()[0] == 0
    codes = _agent_ids(con)
    mapped = [r for r in rows if r["모집사원번호"] in codes]               # 뷰는 설계사가 있는 계약만 보인다
    want_eligible = sum(1 for r in mapped if cmap.get(r["상품종류"], {}).get("category") in ELIGIBLE)
    want_unclassified = sum(1 for r in mapped if r["상품종류"] not in cmap)
    got = {(r["구분"], r["시상대상구분"]): r["n"] for r in con.execute("SELECT 구분, 시상대상구분, COUNT(*) n FROM v_contract_extract GROUP BY 1, 2")}
    assert sum(n for (_, e), n in got.items() if e == 1) == want_eligible > 0
    assert got.get(("미분류", 0), 0) == want_unclassified == 1           # 분류표에 없는 상품종류 하나만 미분류다
    assert got[("종신", 1)] == sum(1 for r in mapped if cmap.get(r["상품종류"], {}).get("category") == "whole_life") > 0
    # 업권도 계약에서 읽는다
    assert {r[0] for r in con.execute("SELECT DISTINCT 업권 FROM v_contract_extract")} == {"life", "nonlife"}


def test_incentive_rule_on_whole_life_and_health_picks_real_contracts(env):
    con = env["con"]
    rid = con.execute("""INSERT INTO incentive_rule(name, status, period_type, metric, effective_from, effective_to)
                         VALUES('종신·건강 시험', 'active', 'month', 'converted_premium', ?, ?)""", (YM, YM)).lastrowid
    for c in ELIGIBLE:
        con.execute("INSERT INTO incentive_rule_category(rule_id, product_category) VALUES(?,?)", (rid, c))
    rule = con.execute("SELECT * FROM incentive_rule WHERE rule_id=?", (rid,)).fetchone()
    got = {r["contract_id"] for r in incentive._metric_contracts(con, rule, YM, YM)}
    want = {r[0] for r in con.execute(f"""SELECT contract_id FROM contract WHERE product_category IN {ELIGIBLE}
                                          AND status IN {incentive.ACTIVE} AND contract_date BETWEEN '2026-02-01' AND '2026-02-28'""")}
    assert got == want and len(want) > 0
    # 구분 하나만 고르는 규칙(scope=product_category)도 계약의 구분을 읽는다
    con.execute("UPDATE incentive_rule SET scope='product_category', scope_value='whole_life' WHERE rule_id=?", (rid,))
    rule = con.execute("SELECT * FROM incentive_rule WHERE rule_id=?", (rid,)).fetchone()
    only = {r["contract_id"] for r in incentive._metric_contracts(con, rule, YM, YM)}
    assert only and only <= want and all(classify.of_contract(con, c)[0] == "whole_life" for c in only)


def test_promotion_and_ranking_eligible_scope_read_contract_category(env):
    con = env["con"]
    pid = pm.create(con, period_ym=YM, name="종신·건강 시험", kind="monthly", windows=[("2월", "2026-02-01", "2026-02-28")],
                    tiers=[(1, "fixed", 1000, None)], by="t", metric="converted_premium", categories=list(ELIGIBLE))
    p = pm._load(con, pid)
    ids = sorted(_active_ids(con))
    vals = pm._values(con, p, ids)
    # 프로모션은 철회를 세지 않는다(promotion.COUNTED). 그 규칙대로 원장에서 직접 센다
    want = defaultdict(float)
    for r in con.execute(f"""SELECT agent_id, converted_premium v FROM contract WHERE product_category IN {ELIGIBLE}
                             AND status IN {pm.COUNTED} AND contract_date BETWEEN '2026-02-01' AND '2026-02-28'"""):
        want[r["agent_id"]] += r["v"]
    assert {a: v[0] for a, v in vals.items() if v[0]} == {a: v for a, v in want.items() if a in vals and v}
    assert sum(v[0] for v in vals.values()) > 0
    # 랭킹의 시상 대상 범위도 같은 구분을 본다
    elig = _expected_by_agent(con, YM, f"{classify.CATEGORY} IN {ELIGIBLE}")
    rk = _ranking(con, YM, scope="eligible")
    assert rk and all(rk[a] == elig[a]["rp"] for a in rk)


# ------------------------------------------------------------------ 2. 업적 집계의 합이 원본과 맞는다
def test_performance_total_reconciles_with_loaded_source(env):
    con, path, loaded = env["con"], env["path"], env["loaded"]
    raw = _raw(path)
    status_map = mapping.load("eternal", "contract", YM)["status_map"]
    file_sum = sum(_num(r["인정보험료"]) for r in raw)
    ledger_sum = con.execute("SELECT SUM(converted_premium) FROM contract").fetchone()[0]
    assert file_sum == loaded["file_recognized_sum"] == ledger_sum              # 적재한 원본 = 원장

    perf = _perf(con, YM)
    perf_rp = sum(r["converted_premium"] for r in perf.values())
    perf_cnt = sum(r["net_count"] for r in perf.values())
    none = performance.unassigned(con, YM, YM)                                   # 모집사원번호가 마스터에 없는 계약
    assert none["new_count"] >= 1
    # 원본을 상태별로 나누어 맞춘다. 반송은 업적이 아니고, 철회는 같은 달에 성립하고 철회해 0 이다
    by = defaultdict(int)
    for r in raw:
        by[status_map[r["계약상태"]]] += _num(r["인정보험료"])
    withdrawn_same_month = [r for r in raw if status_map[r["계약상태"]] == "withdrawn" and str(r["최종상태변경일"])[:6] == YM]
    assert withdrawn_same_month and len(withdrawn_same_month) == sum(1 for r in raw if status_map[r["계약상태"]] == "withdrawn")
    assert perf_rp + none["converted_premium"] == file_sum - by["declined"] - by["withdrawn"]
    # 건수도 원장의 policy_count 를 그대로 더한다 (공동모집 0.5건은 아직 정하지 않았다)
    cnt_ledger = con.execute(f"SELECT SUM(policy_count) FROM contract WHERE status IN {ACTIVE_STATUSES} AND status<>'withdrawn'").fetchone()[0]
    assert perf_cnt + none["net_count"] == cnt_ledger
    # 설계사 한 명씩도 손으로 센 값과 같다
    hand = _expected_by_agent(con, YM)
    assert {a: r["converted_premium"] for a, r in perf.items() if r["converted_premium"]} == {a: v["rp"] for a, v in hand.items() if v["rp"]}


# ------------------------------------------------------------------ 3. 손해보험 건수 부문은 계약의 업권으로 거른다
def test_nonlife_count_division_filters_by_contract_sector(env):
    con = env["con"]
    # 원수사 마스터가 업권을 모두 생보로 잘못 알고 있어도 계약의 업권을 따른다
    con.execute("UPDATE insurer SET sector='life'")
    con.commit()
    d = _division(con, name="손해보험 건수 부문", metric="policy_count", sector="nonlife")
    got = {r["agent_id"]: r["value"] for r in oa.standing(con, d, YM)}
    want = _expected_by_agent(con, YM, "c.sector='nonlife'")
    active = _active_ids(con)
    assert got == {a: v["cnt"] for a, v in want.items() if a in active and v["cnt"] > 0}
    total = _expected_by_agent(con, YM)
    assert any(got.get(a, 0) < total[a]["cnt"] for a in total if a in active)       # 생보 건이 실제로 빠진다
    # 2월 원본의 철회는 같은 달에 성립하고 철회해 업권별 건수가 음수가 되지 않는다. 생보와 손보를 더하면 전체다
    life = {r["agent_id"]: r["value"] for r in oa.standing(con, _division(con, name="생보 건수", metric="policy_count", sector="life"), YM)}
    for a in active:
        assert got.get(a, 0) + life.get(a, 0) == total[a]["cnt"]


# ------------------------------------------------------------------ 4. 센터와 팀의 설계사가 본부 집계에 들어간다
def test_center_and_team_agents_roll_up_to_hq_and_branch(env):
    con, roster = env["con"], env["roster"]
    codes = _agent_ids(con)
    active = _active_ids(con)
    hqs = {r["name"]: r["org_id"] for r in con.execute("SELECT org_id, name FROM org_unit WHERE level='hq'")}
    deep = 0
    for name, hq in hqs.items():
        want = {codes[a["code"]] for a in roster if a["path"][1] == name and codes[a["code"]] in active}
        assert set(orgs.members(con, hq, YM)) == want == set(certification.org_members(con, hq, YM))
        deep += sum(1 for a in roster if a["path"][1] == name and a["path"][4])
        # 본부 집계는 원본 파일의 본부 칸으로 센 값과 같다. 퇴사자와 마스터에 없는 사번은 소속이 없어 빠진다
        hand = _expected_by_agent(con, YM, "c.org_hq=?", (name,))
        expect = sum(v["rp"] for a, v in hand.items() if a in active)
        got = performance.org_rollup(con, hq, YM)
        view = con.execute("""SELECT converted_premium, agent_count FROM v_org_performance_rollup
                              WHERE as_of_ym=? AND period_type='month' AND period_key=? AND org_id=?""", (YM, YM, hq)).fetchone()
        assert got["converted_premium"] == expect == view["converted_premium"] and got["agents"] == view["agent_count"] == len(want)
        # 바로 아래 지점만 더한 값보다 크거나 같고, 아래 조직 전부의 잎을 더한 값과 같다
        leaf = con.execute(f"""SELECT COALESCE(SUM(converted_premium),0) FROM v_org_performance
                               WHERE period_type='month' AND period_key=? AND org_id IN ({','.join('?' * len(orgs.descendants(con, hq)))})""",
                           [YM, *orgs.descendants(con, hq)]).fetchone()[0]
        assert leaf == expect
    assert deep > 0
    # 팀에 속한 사람이 실제로 있고, 그 사람의 실적이 본부와 지점 집계에 들어간다
    perf = _perf(con, YM)
    team_agent = next(codes[a["code"]] for a in roster if a["path"][4] and codes[a["code"]] in active
                      and perf[codes[a["code"]]]["converted_premium"] > 0)
    org = performance.org_at(con, team_agent, YM)
    assert con.execute("SELECT level FROM org_unit WHERE org_id=?", (org,)).fetchone()[0] == "team"
    up = orgs.ancestors(con, org)
    levels = {con.execute("SELECT level FROM org_unit WHERE org_id=?", (o,)).fetchone()[0]: o for o in up}
    assert set(levels) == {"ga", "hq", "branch", "center", "team"}
    for lv in ("hq", "branch", "center"):
        assert team_agent in orgs.members(con, levels[lv], YM)
    # 조직 부문 시상을 지점에 열어도 팀의 설계사가 순위에 든다
    d = oa.load(con, oa.save(con, by="t", org_id=levels["branch"], name="지점 부문", metric="converted_premium", top_n=50, effective_from=YM))
    assert {r["agent_id"]: r["value"] for r in oa.standing(con, d, YM)}[team_agent] == perf[team_agent]["converted_premium"]


# ------------------------------------------------------------------ 5. 같은 사람의 같은 달 숫자는 어디서나 같다
def _same_everywhere(con, ym):
    perf = _perf(con, ym)
    rk_rp, rk_cnt = _ranking(con, ym), _ranking(con, ym, "net_count")
    award = {r["agent_id"]: r["value"] for r in oa.standing(con, _division(con, name=f"인정업적 {ym}"), ym)}
    award_cnt = {r["agent_id"]: r["value"] for r in oa.standing(con, _division(con, name=f"건수 {ym}", metric="policy_count"), ym)}
    active = _active_ids(con)
    assert set(rk_rp) == active
    for a in active:
        p = perf[a]
        assert rk_rp[a] == p["converted_premium"] and rk_cnt[a] == p["net_count"]
        if p["converted_premium"] > 0:
            assert award[a] == p["converted_premium"]
        else:
            assert a not in award                                        # 조직 부문 순위는 0 이하를 싣지 않는다
        if p["net_count"] > 0:
            assert award_cnt[a] == p["net_count"]
    # 시상 대상 구분만 보는 순위도 랭킹과 같다
    elig = {r["agent_id"]: r["value"] for r in oa.standing(con, _division(con, name=f"종신·건강 {ym}", categories=list(ELIGIBLE)), ym)}
    rk_e = _ranking(con, ym, scope="eligible")
    assert all(rk_e[a] == v for a, v in elig.items())
    return perf, award, elig


def test_same_person_same_month_same_number(env):
    perf, award, elig = _same_everywhere(env["con"], YM)
    assert award and elig                                                  # 비교할 사람이 실제로 있다


def test_withdrawal_next_month_is_subtracted_in_that_month_everywhere(env):
    """2월에 성립한 계약이 3월에 철회되면 2월에는 더하고 3월에는 뺀다. 세 엔진이 같은 값을 낸다."""
    con, rows, tmp = env["con"], [dict(r) for r in env["rows"]], env["tmp"]
    codes = _agent_ids(con)
    active = _active_ids(con)
    pick = next(r for r in rows if r["계약상태"] == "정상" and r["모집사원번호"] in codes and codes[r["모집사원번호"]] in active
                and _num(r["인정보험료"]) > 0)
    before = _perf(con, YM)
    pick["계약상태"], pick["최종상태변경일"] = "철회", "20260310"
    path = fe.write_xlsx(tmp / "contract_03.xlsx", fe.CONTRACT_HEADERS, rows, sheet="계약현황")
    assert ec.load_contract(con, path, NEXT)["block"] == 0
    for ym in (YM, NEXT):
        performance.run(con, ym)
        ranking.run(con, ym)
    aid, rp = codes[pick["모집사원번호"]], _num(pick["인정보험료"])
    feb, mar = _same_everywhere(con, YM)[0], _same_everywhere(con, NEXT)[0]
    keep = ("converted_premium", "new_count", "withdrawn_count", "net_count")
    assert {a: tuple(r[k] for k in keep) for a, r in feb.items()} == {a: tuple(r[k] for k in keep) for a, r in before.items()}   # 2월은 그대로
    assert mar[aid]["converted_premium"] == -rp and mar[aid]["withdrawn_count"] == 1 and mar[aid]["net_count"] == -1
    assert _ranking(con, NEXT)[aid] == -rp
    # 같은 사람의 분기 누적은 두 달을 더한 값이다
    q1 = {r["agent_id"]: r["converted_premium"] for r in con.execute(
        "SELECT agent_id, converted_premium FROM agent_performance_summary WHERE run_id=? AND period_type='quarter'", (db.latest_run(con, "performance", NEXT),))}
    assert q1[aid] == feb[aid]["converted_premium"] + mar[aid]["converted_premium"]
