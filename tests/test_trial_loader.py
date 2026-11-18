"""대조용 미리보기 DB 적재: 인별업적순위와 보험사별 두 양식의 읽기, 조직 칸의 계층 이동, 민감 칸 버리기, 머리글 막기,
본문이 같은 파일 건너뛰기, scripts/build_trial_db.py 의 조직, 사람, 계정, 조직장, 프로모션, 월 실적, 월 합계 계약.
아래쪽은 계약현황 58열 양식(v2)과 --contracts: 매핑 자동 고르기, 새 두 열, 센터와 팀 이동, 철회와 반송, 같은 파일 막기, 이름 비우기.

파일은 모두 이 시험이 만드는 작은 가짜 파일이다. 이름, 사번, 금액은 임의 값이다. 실제 파일은 쓰지 않는다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import importlib.util
import json
import sqlite3
from datetime import date

import openpyxl
import pytest
import yaml

from app import db, masters
from app.engine import promotion as pm
from app.loader import eternal_contract as ec
from app.loader import mapping
from app.loader import trial_perf as tp
from app.loader.excel_loader import DuplicateFile
from app.loader.sheet import HeaderMismatch

PW = "Trial2026pw"
ORG = ("가상회사", "가상직할", "가상총괄")
# (사번, 이름, 센터 칸, 팀 칸, 누적 건수, 월납, 인정, 원수사성적)
PEOPLE = [
    ("Z0001", "시험가", "가상1본부", "가상1지점", 2, 300_000, 280_000, 3_600_000),
    ("Z0002", "시험나", "가상1본부", "가상1지점", 1, 120_000, 100_000, 1_440_000),
    ("Z0003", "시험다", "가상1본부", "", 0, 0, 0, 0),             # 팀 칸이 빈 사람: 본부에 바로 붙는다
    ("Z0004", "시험중복", "가상2본부", "가상2지점", 3, 450_000, 400_000, 5_400_000),
    ("Z0005", "시험중복", "가상2본부", "가상2지점", 0, 0, 0, 0),
    ("Z0006", "시험총괄", "", "", 0, 0, 0, 0),                     # 센터 칸이 빈 사람: 총괄본부에 바로 붙는다
]
# 시트 key: [(보험사, {사번: (건수, 월납)})]
INS = {
    "보험사 인별_손보": [("삼성화재", {"Z0001": (1, 200_000), "Z0004": (2, 300_000)}), ("흥국화재", {"Z0002": (1, 120_000)}),
                     ("가상손보", {"Z0004": (1, 150_000)})],
    "보험사 인별_생보": [("DB생명", {"Z0001": (1, 100_000)}), ("(구)신한생명", {})],
    "보험사 인별_자동차": [("삼성화재", {"Z0002": (1, 50_000)})],
    "보험사 인별_일반": [("삼성화재", {})],
}


def _rank(path, people=PEOPLE, *, rename=None, sensitive=True, avg_factor=1, creator=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "인별업적순위"
    person = ["순위", "사원번호", "사원명", "본사", "본부", "지점", "센터", "팀"] + (["주민번호", "계좌번호"] if sensitive else [])
    h1 = person + ["누적", None, None, None, "평균", None, None, None]
    h2 = [None] * len(person) + ["건수", "월납보험료", "인정보험료", "원수사성적"] * 2
    if rename:
        h1 = [rename.get(h, h) for h in h1]
        h2 = [rename.get(h, h) for h in h2]
    ws.append(h1)
    ws.append(h2)
    for i, (code, name, center, team, n, mp, cp, ip) in enumerate(people, start=1):
        extra = ["000000-0000000", "000-00-000000"] if sensitive else []
        ws.append([i, code, name, *ORG, center, team] + extra + [n, mp, cp, ip, n * avg_factor, mp, cp, ip])
    if creator:
        wb.properties.creator = creator
    wb.save(path)
    return path


def _insurer(path, *, creator=None, drop_sheet=None):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sname, groups in INS.items():
        if sname == drop_sheet:
            continue
        ws = wb.create_sheet(sname)
        h1 = ["NO", "본사", "본부", "지점", "센터", "팀", "사원번호", "사원명", "총합계", None]
        h2 = [None] * 8 + ["건수", "보험료(월납)"]
        for name, _ in groups:
            h1 += [name, None]
            h2 += ["건수", "보험료(월납)"]
        ws.append(h1)
        ws.append(h2)
        tot = [0] * (2 + 2 * len(groups))
        for i, (code, name, center, team, *_rest) in enumerate(PEOPLE, start=1):
            vals = [groups[k][1].get(code, (0, 0)) for k in range(len(groups))]
            line = [sum(v[0] for v in vals), sum(v[1] for v in vals)] + [x for v in vals for x in v]
            tot = [a + b for a, b in zip(tot, line)]
            ws.append([i, *ORG, center, team, code, name] + line)
        ws.append([None] * 8 + tot)                              # 합계 줄: 사원번호와 사원명이 빈 줄
    if creator:
        wb.properties.creator = creator
    wb.save(path)
    return path


def _script():
    spec = importlib.util.spec_from_file_location("build_trial_db", db.ROOT / "scripts" / "build_trial_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def src(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    _rank(d / "20990101_인별업적순위 (1).xlsx")
    _insurer(d / "20990101_보험사별 (1).xlsx", creator="첫째")
    _insurer(d / "20990101_보험사별 (2).xlsx", creator="둘째")      # 저장 정보만 다르고 본문이 같다
    (d / "20990101_개인계약현황.xlsx").write_bytes(b"")            # 양식이 아닌 파일은 읽지 않는다
    heads = tmp_path / "heads.yaml"
    heads.write_text(yaml.safe_dump({"parent": "가상총괄", "parent_head_name": "시험총괄", "parent_head_title": "이사",
                                     "bonbu": [{"org": "가상1본부", "head_name": "시험가"}, {"org": "가상2본부", "head_name": "시험중복"},
                                               {"org": "가상3본부", "head_name": "없는이름"}]}, allow_unicode=True), encoding="utf-8")
    return {"dir": d, "heads": heads, "tmp": tmp_path}


# ------------------------------------------------------------------ 읽기
def test_rank_reads_shifted_levels_and_drops_sensitive(tmp_path):
    r = tp.read_rank(_rank(tmp_path / "r.xlsx"))
    assert not r["blocked"] and len(r["people"]) == len(PEOPLE)
    p = r["people"]["Z0001"]
    # 파일 칸 지점은 총괄본부, 센터는 예하 본부, 팀은 지점이다
    assert (p["org"]["org_general"], p["org"]["org_hq"], p["org"]["org_branch"]) == ("가상총괄", "가상1본부", "가상1지점")
    assert (p["org"]["org_company"], p["org"]["org_division"]) == ORG[:2]
    assert p["values"]["total_count"] == 2 and p["values"]["total_monthly_premium_amt"] == 300_000
    assert r["people"]["Z0003"]["org"]["org_branch"] is None
    assert {"주민번호", "계좌번호"} <= set(r["dropped"])
    for q in r["people"].values():
        assert "사원명" not in q["raw"] and "주민번호" not in q["raw"] and "계좌번호" not in q["raw"]
        assert all("000000-0000000" != v for v in q["raw"].values())


def test_insurer_reads_four_sheets_and_summary_row(tmp_path):
    r = tp.read_insurer(_insurer(tmp_path / "i.xlsx"))
    assert len(r["people"]) == len(PEOPLE)                     # 합계 줄은 사람이 아니다
    assert all(v is True for v in r["summary_match"].values()) and len(r["summary_match"]) == 4
    p = r["people"]["Z0004"]
    assert set(p["sheets"]) == {"nonlife", "life", "auto", "general"}
    assert p["sheets"]["nonlife"]["total"] == (3, 450_000)
    assert p["sheets"]["nonlife"]["insurers"]["삼성화재"] == (2, 300_000)
    assert p["org"]["org_hq"] == "가상2본부"
    assert any("가상손보" in w for w in r["warnings"])           # 매핑에 없던 보험사는 읽고 경고한다


def test_changed_header_is_blocked_before_loading(tmp_path):
    with pytest.raises(HeaderMismatch):
        tp.read_rank(_rank(tmp_path / "a.xlsx", rename={"월납보험료": "월보험료"}))
    with pytest.raises(HeaderMismatch):
        tp.read_rank(_rank(tmp_path / "b.xlsx", rename={"센터": "센타"}))
    with pytest.raises(HeaderMismatch):
        tp.read_insurer(_insurer(tmp_path / "c.xlsx", drop_sheet="보험사 인별_일반"))


def test_multi_month_rank_file_is_blocked(tmp_path):
    r = tp.read_rank(_rank(tmp_path / "m.xlsx", avg_factor=3))
    assert r["blocked"] and any(c == "multi_month" for _, c, _m, _r in r["issues"])


def test_same_content_is_ignored(tmp_path):
    a = tp.read_insurer(_insurer(tmp_path / "보험사별 (5).xlsx", creator="가"))
    b = tp.read_insurer(_insurer(tmp_path / "보험사별 (6).xlsx", creator="나"))
    assert (tmp_path / "보험사별 (5).xlsx").read_bytes() != (tmp_path / "보험사별 (6).xlsx").read_bytes()
    assert a["content_hash"] == b["content_hash"]
    keep, dropped = tp.dedupe([b, a])
    assert [k["file"] for k in keep] == ["보험사별 (5).xlsx"] and dropped == [("보험사별 (6).xlsx", "보험사별 (5).xlsx")]
    con = db.init_db(tmp_path / "t.db")
    tp.load_month(con, "202610", ins=a, agents={})
    with pytest.raises(DuplicateFile):
        tp.load_month(con, "202611", ins=b, agents={})
    assert con.execute("SELECT COUNT(*) FROM agent_period_perf").fetchone()[0] == len(PEOPLE)
    con.close()


# ------------------------------------------------------------------ 스크립트
def test_build_without_months_makes_orgs_people_accounts_and_promotions(src):
    mod = _script()
    out, months = src["tmp"] / "trial.db", src["tmp"] / "months.yaml"
    rep = mod.build(out, src["dir"], months, src["heads"], PW)
    # 월 설정이 없으면 빈 틀을 만들고 실적은 넣지 않는다
    tpl = yaml.safe_load(months.read_text(encoding="utf-8"))
    assert set(tpl) == {"20990101_인별업적순위 (1).xlsx", "20990101_보험사별 (1).xlsx", "20990101_보험사별 (2).xlsx"}
    assert all(v is None for v in tpl.values())
    assert rep["duplicates"] == [("20990101_보험사별 (2).xlsx", "20990101_보험사별 (1).xlsx")]
    con = db.connect(out)
    assert con.execute("PRAGMA user_version").fetchone()[0] == mod.TRIAL_MARK
    # 조직: 회사 > 직할본부 > 총괄본부 > 예하 본부 > 지점. 경로 규칙은 app/org/sync.py 다
    root = con.execute("SELECT name, level, path_key FROM org_unit WHERE parent_id IS NULL").fetchall()
    assert [tuple(r) for r in root] == [("가상회사", "ga", "ga")]
    keys = {r[0] for r in con.execute("SELECT path_key FROM org_unit")}
    assert "hq:가상직할 > branch:가상총괄 > center:가상1본부 > team:가상1지점" in keys
    assert dict(con.execute("SELECT level, COUNT(*) FROM org_unit GROUP BY level").fetchall()) == {"ga": 1, "hq": 1, "branch": 1, "center": 2, "team": 2}
    # 사람과 소속. 팀 칸이 빈 사람은 본부에, 센터 칸이 빈 사람은 총괄본부에 붙는다
    assert con.execute("SELECT COUNT(*) FROM agent").fetchone()[0] == len(PEOPLE)
    where = dict(con.execute("""SELECT a.internal_code, o.path_key FROM agent a JOIN agent_org_history h ON h.agent_id=a.agent_id
                                JOIN org_unit o ON o.org_id=h.org_id""").fetchall())
    assert where["Z0003"].endswith("center:가상1본부") and where["Z0006"].endswith("branch:가상총괄")
    # 계정: admin 과 사번이 아닌 T 번호
    logins = [r[0] for r in con.execute("SELECT login_id FROM user_account ORDER BY login_id")]
    assert logins == ["T0001", "T0002", "T0003", "T0004", "T0005", "T0006", "admin"]
    assert not any(code in logins for code, *_ in PEOPLE)
    # 조직장: 총괄 1, 본부 1 지정. 이름이 둘인 사람과 없는 이름은 지정하지 않는다
    assert rep["heads"] == {"wanted": 4, "assigned": 2, "no_match": 1, "many_match": 1, "no_org": 0, "outside": 0}
    roles = sorted(r[0] for r in con.execute("SELECT role FROM org_manager"))
    assert roles == ["branch_head", "center_head"]
    # 프로모션 넷, 실적과 계약은 없다
    assert con.execute("SELECT COUNT(*) FROM promotion WHERE status='published'").fetchone()[0] == 4
    assert con.execute("SELECT COUNT(*) FROM agent_period_perf").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM contract").fetchone()[0] == 0
    con.close()
    # 같은 경로에 다시 만들 수 있다. 이 스크립트가 만든 DB 가 아니면 지우지 않는다
    mod.build(out, src["dir"], months, src["heads"], PW)
    other = src["tmp"] / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x(a)").connection.commit()
    with pytest.raises(SystemExit):
        mod.build(other, src["dir"], months, src["heads"], PW)
    with pytest.raises(SystemExit):
        mod.build(src["tmp"] / "x.db", src["dir"], months, src["heads"], None)


def test_build_with_months_loads_perf_and_month_sum_contracts(src):
    mod = _script()
    out, months = src["tmp"] / "trial.db", src["tmp"] / "months.yaml"
    months.write_text(yaml.safe_dump({"20990101_인별업적순위 (1).xlsx": "202610", "20990101_보험사별 (2).xlsx": 202610}, allow_unicode=True),
                      encoding="utf-8")
    rep = mod.build(out, src["dir"], months, src["heads"], PW, month_sum=True)
    line = rep["months"]["202610"]
    assert line["sums_match"] and line["people"] == len(PEOPLE) and line["from_rank"] == len(PEOPLE)
    con = db.connect(out)
    rows = {r["employee_no"]: dict(r) for r in con.execute("SELECT * FROM v_agent_period_perf WHERE start_ym='202610'")}
    assert len(rows) == len(PEOPLE)
    z1 = rows["Z0001"]
    assert (z1["total_count"], z1["org_general"], z1["org_hq"], z1["org_branch"]) == (2, "가상총괄", "가상1본부", "가상1지점")
    assert rows["Z0002"]["auto_count"] == 1 and rows["Z0002"]["perf_tier"] == "some" and rows["Z0003"]["perf_tier"] == "none"
    names = {r[0] for r in con.execute("SELECT insurer_name FROM agent_period_perf_insurer")}
    assert {"삼성화재", "삼성화재 자동차", "흥국화재", "DB생명", "가상손보"} <= names
    # 원본 행과 실적 표에 이름과 민감 값이 없다
    for (raw,) in con.execute("SELECT raw_json FROM raw_row"):
        d = json.loads(raw)
        assert "사원명" not in d and "주민번호" not in d and not any(n in raw for _, n, *_r in PEOPLE)
    assert con.execute("SELECT COUNT(*) FROM quality_issue WHERE check_code='dropped_column'").fetchone()[0] >= 2
    # 월 합계 계약: 표시가 붙고, 마스터에 없는 보험사와 자동차, 일반 시트는 넣지 않는다
    cs = [dict(r) for r in con.execute("SELECT * FROM contract")]
    assert line["month_sum"]["contracts"] == len(cs) == 4 and line["month_sum"]["skipped_insurer"] == 1
    assert {c["product_category"] for c in cs} == {"health"}
    assert all(c["product_code_raw"] == tp.MONTH_SUM_LABEL and c["policy_no"].startswith("TRIALSUM-") and c["contract_date"] == "2026-10-31" for c in cs)
    # 엔진이 그 계약을 센다: 삼성화재 드림+ 첫 창에 10월 합계가 들어간다
    pid = con.execute("SELECT promo_id FROM promotion WHERE name LIKE '삼성화재 드림+%'").fetchone()[0]
    p = pm._load(con, pid)
    aid = con.execute("SELECT agent_id FROM agent WHERE internal_code='Z0001'").fetchone()[0]
    res = pm.progress(con, p, [aid], date(2027, 1, 5))[aid]
    assert res["value"] == pytest.approx(200_000)
    assert "추정" in p["caution"]
    con.close()


# ------------------------------------------------------------------ 계약현황 58열 (v2) 와 --contracts
# (사번, 설계사 이름, 센터 칸, 팀 칸, 계약일자, 보험사, 증권번호, 초회보험료, 인정보험료, 계약상태, 계약자)
CROWS = [
    ("Z0001", "시험가", "가상1본부", "가상1지점", "2026-10-05", "흥국화재", "P001", 60_000, 60_000, "정상", "가계약자1"),
    ("Z0001", "시험가", "가상1본부", "가상1지점", "2026-11-03", "흥국화재", "P002", 100_000, 100_000, "정상", "가계약자2"),
    ("Z0001", "시험가", "가상1본부", "가상1지점", "2026-10-07", "삼성화재", "P003", 90_000, 90_000, "정상", "가계약자3"),
    ("Z0002", "시험나", "가상1본부", "가상1지점", "2026-10-10", "흥국화재", "P004", 70_000, -70_000, "철회", "가계약자4"),   # 철회는 음수로 온다
    ("Z0002", "시험나", "가상1본부", "가상1지점", "2026-10-12", "흥국화재", "P005", 30_000, 30_000, "반송", "가계약자5"),
    ("Z0003", "시험다", "가상2본부", "가상2지점", "2026-10-15", "흥국화재", "P006", 80_000, 80_000, "정상", "가계약자6"),      # 실적 파일과 센터와 팀이 다르다
    ("Z0003", "시험다", "가상2본부", "가상2지점", "2026-11-10", "흥국화재", "P007", 20_000, 20_000, "정상", "가계약자7"),
    ("Z0007", "시험새", "가상3본부", "", "2026-10-20", "흥국화재", "P008", 50_000, 50_000, "정상", "가계약자8"),             # 파일에만 있는 사번, 팀 칸이 빈다
]
V2_NEW = ("이관일자", "고객구분")


def _contract_headers(version=2):
    m = mapping.load("eternal", "contract_v2" if version == 2 else "contract", "202611")
    return list(m["headers"])


def _contracts(path, rows=CROWS, *, version=2, rename=None, swap=False, org=ORG):
    """가짜 계약현황. 머리글은 매핑 파일에서 가져오고 값은 모두 임의다."""
    heads = _contract_headers(version)
    if swap:                                           # 열 순서만 바꾼다
        i, j = heads.index("계약일자"), heads.index("보험사")
        heads[i], heads[j] = heads[j], heads[i]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "계약현황"
    ws.append([(rename or {}).get(h, h) for h in heads])
    for i, (code, name, center, team, cdate, ins, pno, first, rec, status, holder) in enumerate(rows, start=1):
        v = {"No.": i, "본사": org[0], "본부": org[1], "지점": org[2], "센터": center, "팀": team, "모집사원번호": code, "모집사원명": name,
             "계약일자": cdate, "보험사": ins, "증권번호": pno, "계약자": holder, "피보험자": holder + "피", "상품명": "가상상품", "초회보험료": first,
             "인정보험료": rec, "계약상태": status, "계약종류": "손보", "상품종류": "손보장기", "납입주기(방법)": "월납", "수금사원명": "가수금",
             "최초등록자명": "가등록", "이관일자": "2026-11-01", "고객구분": "개인", "영수일자": "2026-12-31"}
        ws.append([v.get(h) for h in heads])
    wb.save(path)
    return path


def _mem(tmp_path, name="c.db"):
    con = db.init_db(tmp_path / name)
    con.executemany("INSERT INTO product_category(code, name, incentive_eligible, sort_order) VALUES(?,?,?,?)",
                    [(c, n, e, i) for i, (c, n, e) in enumerate(masters.CATEGORIES)])
    masters.seed_real_insurers(con)
    for code, name, *_ in PEOPLE:
        con.execute("INSERT INTO agent(internal_code, name, status, job_type) VALUES(?,?,'active','fc')", (code, name))
    con.execute("INSERT INTO agent(internal_code, name, status, job_type) VALUES('Z0007','시험새','active','fc')")
    return con


def test_v2_header_is_picked_by_headers_and_two_new_columns_are_not_loaded(tmp_path):
    assert len(_contract_headers(2)) == 58 and len(_contract_headers(1)) == 56
    assert [h for h in _contract_headers(2) if h not in _contract_headers(1)] == list(V2_NEW)
    p2, p1 = _contracts(tmp_path / "v2.xlsx"), _contracts(tmp_path / "v1.xlsx", version=1)
    assert ec.preview(p2, "202611")["mapping_version"] == "eternal_contract_v2@v1" and ec.preview(p1, "202611")["mapping_version"] == "eternal_contract@v1"
    assert ec.preview(p2, "202611")["ok"] and ec.preview(p1, "202611")["ok"]
    con = _mem(tmp_path)
    s = ec.load_contract(con, p2, "202611", redact=("계약자", "피보험자"))
    assert s["loaded"] == len(CROWS) and s["skipped"] == 0 and s["block"] == 0
    assert con.execute("SELECT mapping_version FROM import_batch").fetchone()[0] == "eternal_contract_v2@v1"
    # 새 두 열은 계약 원장에 칸이 없고 원본 행에만 남는다
    cols = {r[1] for r in con.execute("PRAGMA table_info(contract)")}
    assert not ({"transfer_date", "customer_kind", "이관일자", "고객구분"} & cols)
    raw = json.loads(con.execute("SELECT raw_json FROM raw_row ORDER BY row_no LIMIT 1").fetchone()[0])
    assert raw["이관일자"] == "2026-11-01" and raw["고객구분"] == "개인"
    # v1 양식은 v1 매핑으로 읽고 청약일을 만들지 않는다. v2 는 계약일자를 청약일로 쓴다(가정)
    con2 = _mem(tmp_path, "c1.db")
    ec.load_contract(con2, p1, "202611")
    assert con2.execute("SELECT mapping_version FROM import_batch").fetchone()[0] == "eternal_contract@v1"
    assert con2.execute("SELECT COUNT(*) FROM contract WHERE application_date IS NOT NULL").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM contract WHERE application_date=contract_date").fetchone()[0] == len(CROWS)
    # 영수일자를 청약일로 쓰지 않는다. 영수일자는 따로 남는다
    assert con.execute("SELECT COUNT(*) FROM contract WHERE application_date=receipt_date").fetchone()[0] == 0
    con.close(), con2.close()


def test_withdrawn_and_declined_are_kept_and_names_are_blank(tmp_path):
    con = _mem(tmp_path)
    ec.load_contract(con, _contracts(tmp_path / "v2.xlsx"), "202611", redact=("계약자", "피보험자", "수금사원명", "최초등록자명"))
    assert dict(con.execute("SELECT status, COUNT(*) FROM contract GROUP BY status").fetchall()) == {"inforce": 6, "withdrawn": 1, "declined": 1}
    w = con.execute("SELECT converted_premium, withdrawal_date FROM contract WHERE policy_no='P004'").fetchone()
    assert w[0] == 70_000 and w[1] == "2026-10-10"                       # 철회의 음수는 절댓값으로 (D-31)
    hist = [r[0] for r in con.execute("SELECT h.status FROM contract_status_history h JOIN contract c USING(contract_id) WHERE c.policy_no='P004' ORDER BY h.id")]
    assert hist == ["inforce", "withdrawn"]
    assert con.execute("SELECT COUNT(*) FROM contract WHERE policyholder_name IS NOT NULL OR insured_name IS NOT NULL").fetchone()[0] == 0
    for (raw,) in con.execute("SELECT raw_json FROM raw_row"):
        d = json.loads(raw)
        assert d["계약자"] is None and d["피보험자"] is None and d["수금사원명"] is None and d["최초등록자명"] is None
        assert "가계약자" not in raw
    con.close()


def test_same_contract_file_is_blocked_second_time_and_changed_header_writes_nothing(tmp_path):
    con = _mem(tmp_path)
    p = _contracts(tmp_path / "v2.xlsx")
    ec.load_contract(con, p, "202611")
    with pytest.raises(DuplicateFile):
        ec.load_contract(con, p, "202611")
    assert con.execute("SELECT COUNT(*) FROM contract").fetchone()[0] == len(CROWS)
    n_batch = con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0]
    for bad in (_contracts(tmp_path / "a.xlsx", rename={"인정보험료": "인정보험"}), _contracts(tmp_path / "b.xlsx", swap=True)):
        with pytest.raises(HeaderMismatch) as e:
            ec.load_contract(con, bad, "202611")
        assert "인정보험" in " ".join(e.value.reasons) or "순서" in " ".join(e.value.reasons)
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == n_batch
    con.close()


def test_read_people_uses_latest_contract_org(tmp_path):
    rows = [("Z0001", "시험가", "가상1본부", "가상1지점", "2026-10-05", "흥국화재", "Q1", 1, 1, "정상", "가"),
            ("Z0001", "시험가", "가상2본부", "", "2026-11-05", "흥국화재", "Q2", 1, 1, "정상", "가")]
    cp = ec.read_people(_contracts(tmp_path / "p.xlsx", rows), "202611")
    assert cp["Z0001"]["contracts"] == 2 and cp["Z0001"]["paths"] == 2
    assert cp["Z0001"]["org"]["org_center"] == "가상2본부" and cp["Z0001"]["org"]["org_team"] is None


def test_build_with_contracts_uses_real_contract_rows_and_not_month_sums(src):
    mod = _script()
    out, months = src["tmp"] / "trial2.db", src["tmp"] / "months.yaml"
    cfile = _contracts(src["tmp"] / "20990102_계약현황.xlsx")
    # 월 설정이 있어도 이 모드에서는 쓰지 않는다. --month-sum-contracts 도 무시한다
    months.write_text(yaml.safe_dump({"20990101_인별업적순위 (1).xlsx": "202610"}, allow_unicode=True), encoding="utf-8")
    rep = mod.build(out, src["dir"], months, src["heads"], PW, month_sum=True, contracts=cfile)
    assert any("--month-sum-contracts" in n for n in rep["notes"])
    assert rep["contract_people"] == {"both": 3, "only_contract": 1, "org_moved": 1, "only_rank": 3}
    cl = rep["contract_load"]
    assert cl["mapping_version"] == "eternal_contract_v2@v1" and cl["loaded"] == len(CROWS) and cl["sums_ok"] and cl["blank_names"] == 0
    assert cl["by_status"] == {"declined": 1, "inforce": 6, "withdrawn": 1} and sorted(cl["perf_runs"]) == ["202610", "202611"]
    con = db.connect(out)
    # 실적 합계 파일의 월 가정과 월 합계 대표 계약은 없다
    assert con.execute("SELECT COUNT(*) FROM agent_period_perf").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM contract WHERE product_code_raw LIKE '%월 합계%' OR policy_no LIKE 'TRIALSUM-%'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM agent_performance_summary").fetchone()[0] > 0
    # 사람: 같은 사번은 같은 사람이고 파일에만 있는 사번은 새로 만든다. 센터와 팀이 바뀐 사람은 계약현황을 따른다. 팀 칸이 빈 사람은 본부에 붙는다
    assert con.execute("SELECT COUNT(*) FROM agent").fetchone()[0] == len(PEOPLE) + 1
    where = dict(con.execute("""SELECT a.internal_code, o.path_key FROM agent a JOIN agent_org_history h ON h.agent_id=a.agent_id
                                JOIN org_unit o ON o.org_id=h.org_id""").fetchall())
    assert where["Z0003"].endswith("center:가상2본부 > team:가상2지점") and where["Z0007"].endswith("center:가상3본부")
    assert where["Z0001"].endswith("center:가상1본부 > team:가상1지점") and where["Z0006"].endswith("branch:가상총괄")
    # 계정: T 번호는 사번 순. 새 사번은 맨 뒤 번호
    logins = [r[0] for r in con.execute("SELECT login_id FROM user_account ORDER BY login_id")]
    assert logins == [f"T{i:04d}" for i in range(1, 8)] + ["admin"]
    # 프로모션: BOOM-UP 만 게시. 나머지는 초안
    st = {r["name"]: r["status"] for r in con.execute("SELECT name, status FROM promotion")}
    assert [n for n, v in st.items() if v == "published"] == ["흥국화재 BOOM-UP (대조용)"] and len(st) == 4
    pid = con.execute("SELECT promo_id FROM promotion WHERE status='published'").fetchone()[0]
    p = pm._load(con, pid)
    assert (p["metric"], p["date_basis"], p["insurer_name"], p["rules"]["count_basis"], p["category_list"]) == \
        ("converted_premium", "application_date", "흥국화재", "application", [])
    ids = {c: a for c, a in con.execute("SELECT internal_code, agent_id FROM agent")}
    res = pm.progress(con, p, list(ids.values()), date(2026, 11, 18))
    w = lambda c: [x["value"] for x in res[ids[c]]["windows"]]          # noqa: E731
    assert w("Z0001")[:2] == [60_000, 100_000]                          # 삼성화재 계약은 세지 않는다
    assert w("Z0002")[:2] == [0, 0]                                     # 철회와 반송은 뺀다
    assert w("Z0003")[:2] == [80_000, 20_000] and w("Z0007")[0] == 50_000
    assert not any(pm.is_achieved(p, r) for r in res.values())          # 12월 창이 아직 없어 최종 달성은 없다
    # 조직별 진척이 사람별 값과 같다
    top = con.execute("SELECT path_key FROM org_unit WHERE level='branch'").fetchone()[0]
    org = pm.org_progress(con, p, top, list(ids.values()), date(2026, 11, 18), ym="202611")
    assert org["total"]["phases"][0]["n_met"] == 3 and org["total"]["phases"][1]["n_met"] == 1
    con.close()
    # 같은 계약현황을 같은 DB 에 다시 적재하면 막는다
    con = db.connect(out)
    with pytest.raises(DuplicateFile):
        ec.load_contract(con, cfile, "202611")
    con.close()


def test_build_with_changed_contract_header_stops_before_writing(src):
    mod = _script()
    bad = _contracts(src["tmp"] / "20990102_계약현황.xlsx", rename={"인정보험료": "인정보험"})
    out = src["tmp"] / "trial2.db"
    with pytest.raises(SystemExit):
        mod.build(out, src["dir"], src["tmp"] / "m.yaml", src["heads"], PW, contracts=bad)
    assert not out.exists()
