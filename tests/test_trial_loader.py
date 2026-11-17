"""대조용 미리보기 DB 적재: 인별업적순위와 보험사별 두 양식의 읽기, 조직 칸의 계층 이동, 민감 칸 버리기, 머리글 막기,
본문이 같은 파일 건너뛰기, scripts/build_trial_db.py 의 조직, 사람, 계정, 조직장, 프로모션, 월 실적, 월 합계 계약.

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

from app import db
from app.engine import promotion as pm
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
