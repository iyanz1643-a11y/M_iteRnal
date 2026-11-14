"""실제 계약현황(56열)과 사원현황 양식의 적재기 시험. 가짜 샘플 파일로 돈다."""
import pathlib
import tempfile

import openpyxl
import pytest
import yaml

from app import db, masters
from app.loader import eternal_contract as ec
from app.loader import eternal_employee as ee
from app.loader import fake_eternal as fe
from app.loader import mapping, org_path
from app.loader.excel_loader import DuplicateFile
from app.loader.sheet import HeaderMismatch

YM = "202602"


@pytest.fixture()
def env():
    tmp = pathlib.Path(tempfile.mkdtemp())
    con = db.init_db(tmp / "t.db")
    for i, (code, name, elig) in enumerate(masters.CATEGORIES):
        con.execute("INSERT INTO product_category(code, name, incentive_eligible, sort_order) VALUES(?,?,?,?)", (code, name, elig, i))
    for k, (name, sector) in enumerate(fe.MASTER_INSURERS.items()):
        con.execute("INSERT INTO insurer(code, name, sector) VALUES(?,?,?)", (f"T{k:02d}", name, sector))
    roster = fe.fake_roster()
    emp_path, _ = fe.make_employee_file(tmp / "emp.xlsx", roster)
    for r in ee.parse_employee_file(emp_path)["rows"]:   # 설계사 마스터는 사원현황 파서 결과로 채운다
        con.execute("INSERT INTO agent(internal_code, name) VALUES(?,?)", (r["employee_no"], r["name"]))
    con.commit()
    path, rows = fe.make_contract_file(tmp / "contract.xlsx", period_ym=YM, roster=roster)
    yield {"con": con, "tmp": tmp, "roster": roster, "path": path, "rows": rows, "emp_path": emp_path}
    con.close()


def _source_totals(path):
    """적재기와 상관없이 원본 엑셀을 직접 읽어 건수와 합계를 센다."""
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    head = [c.value for c in ws[1]]
    rows = [dict(zip(head, r)) for r in ws.iter_rows(min_row=2, values_only=True) if any(v not in (None, "") for v in r)]
    num = lambda v: int(str(v).replace(",", ""))
    return {"n": len(rows), "recognized": sum(num(r["인정보험료"]) for r in rows), "first": sum(num(r["초회보험료"]) for r in rows),
            "perf_y1": sum(num(r["1차년성적"]) for r in rows),
            "by_status": {s: sum(1 for r in rows if r["계약상태"] == s) for s in {r["계약상태"] for r in rows}},
            "by_hq": {h: sum(num(r["인정보험료"]) for r in rows if r["본부"] == h) for h in {r["본부"] for r in rows}}}


def _codes(con, batch_id):
    return {r["check_code"] for r in con.execute("SELECT check_code FROM quality_issue WHERE batch_id=?", (batch_id,))}


# ------------------------------------------------------------------ 매핑
def test_mapping_headers_are_the_real_56():
    m = mapping.load("eternal", "contract", YM)
    assert len(m["headers"]) == 56
    assert m["headers"] == fe.CONTRACT_HEADERS
    assert set(m["columns"]) <= set(m["headers"])


# ------------------------------------------------------------------ 건수와 합계
def test_counts_and_sums_match_source(env):
    con, path = env["con"], env["path"]
    src = _source_totals(path)
    s = ec.load_contract(con, path, YM)
    assert s["block"] == 0
    assert s["file_rows"] == src["n"] == s["loaded"] == s["inserted"]
    assert s["file_recognized_sum"] == src["recognized"] == s["loaded_recognized_sum"]
    n, rp, fp, y1 = con.execute("SELECT COUNT(*), SUM(converted_premium), SUM(premium), SUM(perf_y1) FROM contract").fetchone()
    assert (n, rp, fp, y1) == (src["n"], src["recognized"], src["first"], src["perf_y1"])
    assert con.execute("SELECT COUNT(*) FROM raw_row WHERE batch_id=?", (s["batch_id"],)).fetchone()[0] == src["n"]
    by_hq = {r[0]: r[1] for r in con.execute("SELECT org_hq, SUM(converted_premium) FROM contract GROUP BY org_hq")}
    assert by_hq == src["by_hq"]
    st = {r[0]: r[1] for r in con.execute("SELECT status_raw, COUNT(*) FROM contract GROUP BY status_raw")}
    assert st == src["by_status"]
    status = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM contract GROUP BY status")}
    assert status["withdrawn"] == src["by_status"]["철회"] and status["lapsed"] == src["by_status"]["실효"]
    assert status["cancelled"] == src["by_status"]["해지"] and status["declined"] == src["by_status"]["반송"]
    b = con.execute("SELECT status, row_count, data_type FROM import_batch WHERE batch_id=?", (s["batch_id"],)).fetchone()
    assert tuple(b) == ("active", src["n"], "org_contract")


def test_values_kept_as_given(env):
    """인정보험료는 다시 계산하지 않고, 날짜와 Y/N 과 년월은 표준 꼴로 바뀐다."""
    con, rows = env["con"], env["rows"]
    ec.load_contract(con, env["path"], YM)
    r0 = rows[12]  # 일시납 행
    c = con.execute("SELECT * FROM contract WHERE policy_no=?", (r0["증권번호"],)).fetchone()
    assert c["converted_premium"] == int(r0["인정보험료"].replace(",", ""))
    assert c["payment_mode"] == "single" and c["sector"] in ("life", "nonlife")
    assert c["contract_date"] == f"{r0['계약일자'][:4]}-{r0['계약일자'][4:6]}-{r0['계약일자'][6:]}"
    assert c["perf_ym"] == YM and c["last_paid_ym"] == YM
    assert c["self_family"] in (0, 1) and c["confirm_fcpa"] in (0, 1)
    assert c["policyholder_name"] == r0["계약자"] and c["insured_name"] == r0["피보험자"]   # D-05 제안대로 저장
    assert c["collector_code"] == r0["수금사원번호"] and c["collector_agent_id"] is not None
    assert c["perf_y2"] == 0 and c["perf_y1"] == int(r0["1차년성적"].replace(",", ""))


# ------------------------------------------------------------------ 머리글 검사
def _rewrite(env, name, headers, rows):
    return fe.write_xlsx(env["tmp"] / name, headers, rows)


@pytest.mark.parametrize("change, expect", [
    ("rename", "없는 열"),
    ("drop", "없는 열"),
    ("add", "매핑에 없는 열"),
    ("swap", "열 순서가 바뀜"),
])
def test_changed_header_is_blocked_before_load(env, change, expect):
    con = env["con"]
    h, rows = list(fe.CONTRACT_HEADERS), [dict(r) for r in env["rows"]]
    if change == "rename":
        h[h.index("인정보험료")] = "인정 보험료"
        for r in rows:
            r["인정 보험료"] = r.pop("인정보험료")
    elif change == "drop":
        h.remove("최종상태변경일")
    elif change == "add":
        h.append("추가항목")
    elif change == "swap":
        i, j = h.index("계약자"), h.index("피보험자")
        h[i], h[j] = h[j], h[i]
    path = _rewrite(env, f"{change}.xlsx", h, rows)
    assert ec.preview(path, YM)["ok"] is False
    with pytest.raises(HeaderMismatch) as e:
        ec.load_contract(con, path, YM)
    assert any(expect in r for r in e.value.reasons)
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM contract").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM raw_row").fetchone()[0] == 0


def test_middle_dot_variant_is_same_header(env):
    h = [x.replace("·", "ㆍ") for x in fe.CONTRACT_HEADERS]
    rows = [{k.replace("·", "ㆍ"): v for k, v in r.items()} for r in env["rows"]]
    path = _rewrite(env, "dot.xlsx", h, rows)
    s = ec.load_contract(env["con"], path, YM)
    assert s["block"] == 0 and s["loaded"] == len(rows)
    assert env["con"].execute("SELECT COUNT(*) FROM contract WHERE self_family IS NOT NULL").fetchone()[0] == len(rows)


# ------------------------------------------------------------------ 중복 파일
def test_same_file_twice_is_blocked(env):
    con = env["con"]
    ec.load_contract(con, env["path"], YM)
    with pytest.raises(DuplicateFile):
        ec.load_contract(con, env["path"], YM)
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM contract").fetchone()[0] == len(env["rows"])


# ------------------------------------------------------------------ 상태 변화 이력
def test_status_change_builds_history(env):
    con, rows = env["con"], [dict(r) for r in env["rows"]]
    ec.load_contract(con, env["path"], YM)
    a, b, c = rows[0], rows[1], rows[2]
    assert a["계약상태"] == b["계약상태"] == c["계약상태"] == "정상"
    a.update({"계약상태": "실효", "최종상태변경일": "20260320"})
    b.update({"계약상태": "철회", "최종상태변경일": "20260305"})
    c.update({"납입회차": "2", "최종납입년월": "202603"})   # 상태는 그대로
    s2 = ec.load_contract(con, fe.write_xlsx(env["tmp"] / "c2.xlsx", fe.CONTRACT_HEADERS, rows), "202603")
    assert s2["block"] == 0 and s2["inserted"] == 0
    assert s2["updated"] == 2 and s2["refreshed"] == len(rows) - 2
    hist = lambda pol: [(h["status"], h["status_date"]) for h in con.execute(
        """SELECT h.status, h.status_date FROM contract_status_history h JOIN contract k ON k.contract_id=h.contract_id
           WHERE k.policy_no=? ORDER BY h.id""", (pol,))]
    first = lambda r: f"{r['계약일자'][:4]}-{r['계약일자'][4:6]}-{r['계약일자'][6:]}"
    assert hist(a["증권번호"]) == [("inforce", first(a)), ("lapsed", "2026-03-20")]
    assert hist(b["증권번호"]) == [("inforce", first(b)), ("withdrawn", "2026-03-05")]
    assert hist(c["증권번호"]) == [("inforce", first(c))]
    kb = con.execute("SELECT status, withdrawal_date, status_raw FROM contract WHERE policy_no=?", (b["증권번호"],)).fetchone()
    assert tuple(kb) == ("withdrawn", "2026-03-05", "철회")
    kc = con.execute("SELECT paid_installments, last_paid_ym FROM contract WHERE policy_no=?", (c["증권번호"],)).fetchone()
    assert tuple(kc) == (2, "202603")
    assert con.execute("SELECT COUNT(*) FROM contract").fetchone()[0] == len(rows)

    # 이전 날짜의 파일이 늦게 오면 상태를 되돌리지 않는다
    a.update({"계약상태": "정상", "최종상태변경일": "20260210"})
    s3 = ec.load_contract(con, fe.write_xlsx(env["tmp"] / "c3.xlsx", fe.CONTRACT_HEADERS, rows), "202603")
    assert s3["stale"] == 1 and "stale_row" in _codes(con, s3["batch_id"])
    assert con.execute("SELECT status FROM contract WHERE policy_no=?", (a["증권번호"],)).fetchone()[0] == "lapsed"
    assert len(hist(a["증권번호"])) == 2


def test_unknown_status_blocks_only_that_row(env):
    con, rows = env["con"], [dict(r) for r in env["rows"]]
    rows[3]["계약상태"] = "보류"
    s = ec.load_contract(con, fe.write_xlsx(env["tmp"] / "st.xlsx", fe.CONTRACT_HEADERS, rows), YM)
    assert s["skipped"] == 1 and s["loaded"] == len(rows) - 1 and s["file_rows"] == len(rows)
    assert con.execute("SELECT severity FROM quality_issue WHERE check_code='bad_row'").fetchone()[0] == "block"
    assert con.execute("SELECT status FROM import_batch").fetchone()[0] == "active"


# ------------------------------------------------------------------ 버릴 칸
def test_sensitive_columns_are_not_stored(env):
    con, rows = env["con"], [dict(r) for r in env["rows"]]
    secret = {"주민번호": "SECRETRRN", "계좌번호": "SECRETACCT", "소득": "SECRETINCOME", "이행보증금액": "SECRETBOND"}
    for i, r in enumerate(rows):
        for k, v in secret.items():
            r[k] = f"{v}{i}"
    path = fe.write_xlsx(env["tmp"] / "sens.xlsx", fe.CONTRACT_HEADERS + list(secret), rows)
    s = ec.load_contract(con, path, YM)
    assert s["block"] == 0 and s["loaded"] == len(rows)
    assert sorted(s["dropped_columns"]) == sorted(secret)
    assert "dropped_column" in _codes(con, s["batch_id"])
    dump = "\n".join(con.iterdump())
    for v in secret.values():
        assert v not in dump
    raw = con.execute("SELECT raw_json FROM raw_row LIMIT 1").fetchone()[0]
    assert not any(k in raw for k in secret)


def test_employee_parser_drops_sensitive_and_returns_only_allowed(env):
    out = ee.parse_employee_file(env["emp_path"])
    src = fe.fake_employee_rows(env["roster"])
    assert len(out["rows"]) == len(env["roster"]) and out["sheet"] == "조회"
    assert sorted(out["dropped_columns"]) == sorted(fe.SENSITIVE_EMPLOYEE)
    allowed = {"employee_no", "name", "org", "org_path", "position", "hire_date", "termination_date", "life", "nonlife", "_row"}
    assert all(set(r) == allowed for r in out["rows"])
    text = repr(out["rows"]) + repr(out["warnings"])
    distinct = ("주민번호", "보험업계고유번호", "계좌번호", "소득(전년)", "이행보증보험금액")   # 다른 칸과 겹칠 수 없는 꼴의 값
    for r in src:
        for k in distinct:
            assert str(r[k]) not in text, k
    assert not any(k in text for k in fe.SENSITIVE_EMPLOYEE)
    p0, a0 = out["rows"][0], env["roster"][0]
    assert p0["employee_no"] == a0["code"] and p0["org"]["본부"] == a0["path"][1]
    left = out["rows"][3]
    assert left["termination_date"] == "2026-01-31" and left["life"]["deregistered"] == "2026-01-31"
    assert out["rows"][1]["life"]["status"] == "등록" and out["rows"][0]["nonlife"]["status"] == "미등록"


def test_employee_parser_blocks_missing_required(env):
    h = [x for x in fe.EMPLOYEE_HEADERS if x != "사원번호"]
    path = fe.write_xlsx(env["tmp"] / "emp_bad.xlsx", h, fe.fake_employee_rows(env["roster"]))
    with pytest.raises(HeaderMismatch):
        ee.parse_employee_file(path)


# ------------------------------------------------------------------ 분류표
def test_category_map_is_draft_with_low_confidence_lines():
    text = ec.CATEGORY_MAP.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# 초안. 대표님 확인 전"
    doc = yaml.safe_load(text)
    assert doc["status"] == "draft"
    types = [l["product_type"] for l in doc["map"]]
    assert sorted(types) == sorted(fe.LIFE_TYPES + fe.NONLIFE_TYPES) and len(types) == 19
    low = {l["product_type"] for l in doc["map"] if l.get("confidence") == "low"}
    assert low == {"생보정기", "경영인정기", "손보운전자", "생보단체"}


def test_category_map_is_applied(env):
    con = env["con"]
    s = ec.load_contract(con, env["path"], YM)
    cmap = ec.load_category_map()
    for pt, cat in con.execute("SELECT product_type_raw, product_category FROM contract"):
        if pt == fe.UNKNOWN_PRODUCT_TYPE:
            assert cat is None
        else:
            assert cat == cmap[pt]["category"]
    assert con.execute("SELECT DISTINCT product_category FROM contract WHERE product_type_raw='종신'").fetchone()[0] == "whole_life"
    assert con.execute("SELECT DISTINCT product_category FROM contract WHERE product_type_raw='손보운전자'").fetchone()[0] == "general"
    msg = con.execute("SELECT message FROM quality_issue WHERE batch_id=? AND check_code='unknown_category'", (s["batch_id"],)).fetchone()[0]
    assert fe.UNKNOWN_PRODUCT_TYPE in msg


# ------------------------------------------------------------------ 마스터에 없는 값
def test_unknown_insurer_and_agent_warn_but_load(env):
    con = env["con"]
    s = ec.load_contract(con, env["path"], YM)
    codes = _codes(con, s["batch_id"])
    assert {"unmapped_insurer", "unmapped_agent"} <= codes
    ins = con.execute("SELECT origin, sector FROM insurer WHERE name=?", (fe.UNKNOWN_INSURER,)).fetchone()
    assert tuple(ins) == ("loader", "nonlife")
    assert con.execute("SELECT COUNT(*) FROM contract c JOIN insurer i ON i.insurer_id=c.insurer_id WHERE i.name=?",
                       (fe.UNKNOWN_INSURER,)).fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM contract WHERE agent_id IS NULL").fetchone()[0] == 1
    assert con.execute("SELECT severity FROM quality_issue WHERE check_code='unmapped_agent'").fetchone()[0] == "warn"


def test_messages_have_no_person_names(env):
    con, rows = env["con"], env["rows"]
    ec.load_contract(con, env["path"], YM)
    names = {r[k] for r in rows for k in ("계약자", "피보험자", "모집사원명", "수금사원명")}
    for (msg,) in con.execute("SELECT message FROM quality_issue"):
        assert not any(n in msg for n in names)


# ------------------------------------------------------------------ 조직 경로
def test_org_path_kept_and_identified_by_path(env):
    con = env["con"]
    ec.load_contract(con, env["path"], YM)
    total = con.execute("SELECT COUNT(*) FROM contract").fetchone()[0]
    no_center = con.execute("SELECT COUNT(*) FROM contract WHERE org_center IS NULL").fetchone()[0]
    with_team = con.execute("SELECT COUNT(*) FROM contract WHERE org_team IS NOT NULL").fetchone()[0]
    assert 0 < no_center < total and 0 < with_team < total
    assert con.execute("SELECT COUNT(*) FROM contract WHERE org_team IS NOT NULL AND org_center IS NULL").fetchone()[0] == 0
    shared = org_path.shared_names(con)
    assert shared["센터"][fe.SHARED_CENTER] == 2 and shared["팀"][fe.SHARED_TEAM] == 2
    nodes = org_path.org_nodes(con)
    centers = [n for n in nodes if n["level"] == "org_center" and n["name"] == fe.SHARED_CENTER]
    assert len(centers) == 2 and centers[0]["path"] != centers[1]["path"]
    assert sum(n["contracts"] for n in nodes if n["level"] == "org_head_office") == total
    cid = con.execute("SELECT contract_id FROM contract WHERE org_team IS NOT NULL LIMIT 1").fetchone()[0]
    p = org_path.contract_org_path(con, cid)
    assert len(p) == 5 and all(p)
    assert org_path.path_text(p) == con.execute("SELECT org_path FROM contract WHERE contract_id=?", (cid,)).fetchone()[0]


# ------------------------------------------------------------------ 본부 업로드의 소속 검사
def _org_contract_xlsx(path, rows):
    """본부 계약현황 샘플 양식(config/mapping/internal_org_contract.yaml). 3행이 머리글이다."""
    from app.samples import ORG_HEADERS

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "계약현황"
    ws["A1"] = "시험 계약 현황"
    ws.append([])
    ws.append(ORG_HEADERS)
    for r in rows:
        ws.append([r.get(h, "") for h in ORG_HEADERS])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def test_center_and_team_members_pass_hq_upload_check(tmp_path):
    """본부 아래 지점, 센터, 팀 어디에 붙은 설계사든 본부 업로드에서 소속 경고가 없다. 다른 본부 사람만 경고한다."""
    from datetime import date

    from app.loader import excel_loader
    from app.org import sync

    con = db.init_db(tmp_path / "t.db")
    for i, (code, name, elig) in enumerate(masters.CATEGORIES):
        con.execute("INSERT INTO product_category(code, name, incentive_eligible, sort_order) VALUES(?,?,?,?)", (code, name, elig, i))
    ins = con.execute("INSERT INTO insurer(code, name, sector) VALUES('SAM', '샘플생명', 'life')").lastrowid
    con.execute("INSERT INTO product(insurer_id, product_code, product_name, product_category, conversion_rate, effective_from) "
                "VALUES(?, 'P1', '시험상품', 'health', 1.0, '202601')", (ins,))
    con.commit()
    roster = fe.fake_roster()
    emp_path, _ = fe.make_employee_file(tmp_path / "emp.xlsx", roster)
    staff = ee.parse_employee_file(emp_path)["rows"]
    sync.sync_staff(con, staff, YM)
    active = {r["employee_no"] for r in staff if not r["termination_date"]}

    hq_name = roster[0]["path"][1]
    hq_id = con.execute("SELECT org_id FROM org_unit WHERE path_key=?", (f"hq:{hq_name}",)).fetchone()[0]
    inside = [a for a in roster if a["path"][1] == hq_name and a["code"] in active]
    outside = next(a for a in roster if a["path"][1] != hq_name and a["code"] in active)
    levels_inside = {con.execute("""SELECT o.level FROM agent_org_history h JOIN agent g ON g.agent_id=h.agent_id
                                    JOIN org_unit o ON o.org_id=h.org_id WHERE g.internal_code=? AND h.effective_to IS NULL""",
                                 (a["code"],)).fetchone()[0] for a in inside}
    assert {"branch", "center", "team"} <= levels_inside       # 한 단계 아래만 보던 검사가 놓치던 사람이 들어 있다

    d = date(2026, 2, 10)
    rows = [{"본부": hq_name, "사번": a["code"], "원수사": "샘플생명", "상품명": "시험상품", "구분": "건강",
             "청약번호": f"APP{i:05d}", "청약일": d, "계약일": d, "상태": "성립", "상태변경일": d, "납입주기": "월납",
             "납입기간": 20, "보험료": 100000, "인정보험료": 100000, "건수": 1}
            for i, a in enumerate(inside + [outside])]
    path = _org_contract_xlsx(tmp_path / "org_contract.xlsx", rows)
    s = excel_loader.load_org_contract(con, path, YM, hq_id)
    assert s["block"] == 0 and s["inserted"] == len(rows) and s["skipped"] == 0
    bad = [r[0] for r in con.execute("SELECT row_no FROM quality_issue WHERE batch_id=? AND check_code='org_mismatch'", (s["batch_id"],))]
    assert bad == [3 + len(rows)]                              # 맨 끝 행, 다른 본부 사람 하나만
    assert s["warn"] == 1
    con.close()


# ------------------------------------------------------------------ 사원현황 연결 처리기
@pytest.fixture()
def connect_env(tmp_path, monkeypatch):
    from app.connect.pipeline import Pipeline
    from app.connect.state import StateStore

    monkeypatch.setenv("CONNECT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CONNECT_DATA_DIR", str(tmp_path / "data"))
    con = db.init_db(tmp_path / "t.db")
    pipe = Pipeline(con, data_dir=tmp_path / "data", state=StateStore(tmp_path / "state"))   # 기본 등록부를 쓴다
    roster = fe.fake_roster()
    emp_path, emp_rows = fe.make_employee_file(tmp_path / "in" / "사원현황.xlsx", roster)
    yield {"con": con, "pipe": pipe, "tmp": tmp_path, "emp": emp_path, "emp_rows": emp_rows, "roster": roster}
    con.close()


def test_employee_file_goes_through_connect_pipeline_once(connect_env):
    import json

    from app.connect import DEFAULT_REGISTRY
    from app.connect.base import DUPLICATE, LOADED
    from app.connect.sources import UploadSource
    from app.loader import employee_handler

    env = connect_env
    con, pipe = env["con"], env["pipe"]
    assert DEFAULT_REGISTRY.get("employee").load is employee_handler._load

    first = pipe.run(UploadSource.from_path(env["emp"], kind="employee", period_ym=YM))
    r = first.results[0]
    assert first.ok and r.status == LOADED, r.reason
    st = r.detail["stats"]
    assert r.detail["file_rows"] == st["rows"] == len(env["roster"]) == st["new_agents"]
    assert r.detail["block"] == 0
    assert con.execute("SELECT COUNT(*) FROM agent").fetchone()[0] == len(env["roster"])
    by_level = {x[0]: x[1] for x in con.execute("SELECT level, COUNT(*) FROM org_unit GROUP BY level")}
    assert by_level["ga"] == 1 and by_level["hq"] == 4 and by_level["branch"] == 8 and by_level["center"] >= 9 and by_level["team"] >= 8
    for a in env["roster"]:                                    # 사람은 경로의 맨 아래 조직에 붙는다
        if a["path"][4]:
            lv = con.execute("""SELECT o.level FROM agent_org_history h JOIN agent g ON g.agent_id=h.agent_id
                                JOIN org_unit o ON o.org_id=h.org_id WHERE g.internal_code=? ORDER BY h.id DESC""", (a["code"],)).fetchone()[0]
            assert lv == "team"
    b = con.execute("SELECT data_type, period_ym, status, row_count, received_via, file_path, file_hash FROM import_batch").fetchall()
    assert len(b) == 1
    assert tuple(b[0])[:5] == ("employee", YM, "active", len(env["roster"]), "upload")
    assert b[0]["file_path"] == r.raw_path and b[0]["file_hash"] == r.file_hash
    assert con.execute("SELECT COUNT(*) FROM raw_row").fetchone()[0] == 0     # 민감 열이 많은 원본 행은 남기지 않는다
    # 결과에는 이름이 없다
    names = {x["사원명"] for x in env["emp_rows"]}
    text = json.dumps(r.to_dict(), ensure_ascii=False, default=str)
    assert "changes" not in r.detail and not any(n in text for n in names)

    tables = ("org_unit", "agent", "agent_org_history", "org_manager", "import_batch")
    before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    second = pipe.run(UploadSource.from_path(env["emp"], kind="employee", period_ym=YM))
    assert [x.status for x in second.results] == [DUPLICATE]
    assert employee_handler.already_loaded(con, r.file_hash)
    assert {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables} == before


def test_employee_file_with_wrong_header_fails_without_batch(connect_env):
    from app.connect.base import FAILED
    from app.connect.sources import UploadSource

    env = connect_env
    h = [x for x in fe.EMPLOYEE_HEADERS if x != "사원번호"]
    path = fe.write_xlsx(env["tmp"] / "in" / "사원현황_틀림.xlsx", h, fe.fake_employee_rows(env["roster"]))
    res = env["pipe"].run(UploadSource.from_path(path, kind="employee", period_ym=YM)).results[0]
    assert res.status == FAILED and "머리글" in res.reason
    assert env["con"].execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 0
    assert env["con"].execute("SELECT COUNT(*) FROM agent").fetchone()[0] == 0
