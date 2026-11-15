"""데이터 묶음의 계약 엑셀, 시상안, 품질검증, 적재 이력과 조직·설정의 마스터. 기존 운영 도구에서 옮긴 화면이다.

메인 관리자만 열고, 미리보기는 DB 에 쓰지 않고, 같은 파일을 막고, 재적재는 확인 단계를 거치고, 민감 칸은 DB 에 남지 않는지 본다.
가짜 실제 양식(app/loader/fake_eternal.py)과 샘플 양식만 쓴다. 실제 자료는 열지 않는다.
"""
import os
import pathlib
import re
import tempfile

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import db, masters, samples
from app.loader import fake_eternal as fe
from app.ops import jobs
from app.web import auth
from app.web.routes import data as data_routes

PW = "test-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
PATHS = ("/data/contracts", "/data/plan", "/data/quality", "/data/batches", "/org/masters")
SENSITIVE_HEAD = "계약자주민번호"
SENSITIVE_VALUE = "민감값-지워야-함"


class Client(TestClient):
    def post(self, url, data=None, **kw):
        if data is not None and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


@pytest.fixture(scope="module")
def env():
    tmp = pathlib.Path(tempfile.mkdtemp())
    path = tmp / "t.db"
    con = db.init_db(path)
    masters.seed(con)
    auth.seed_accounts(con, PW)
    keep = {k: os.environ.get(k) for k in ("APP_DB", "APP_UPLOAD_DIR", "CONNECT_DATA_DIR")}
    os.environ.update({"APP_DB": str(path), "APP_UPLOAD_DIR": str(tmp / "up"), "CONNECT_DATA_DIR": str(tmp / "data")})
    from app.web.main import app
    yield {"con": con, "app": app, "tmp": tmp}
    con.close()
    for k, v in keep.items():
        if v is None:
            os.environ.pop(k, None)
        elif k != "APP_DB":
            os.environ[k] = v


def login(env, login_id="admin"):
    c = Client(env["app"], follow_redirects=False)
    assert c.post("/login", data={"login_id": login_id, "password": PW}).status_code == 303
    return c


def others(env):
    con = env["con"]
    manager = con.execute("SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id LIMIT 1").fetchone()[0]
    agent = con.execute("SELECT internal_code FROM agent WHERE agent_id NOT IN (SELECT agent_id FROM org_manager) LIMIT 1").fetchone()[0]
    return manager, agent


def counts(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("import_batch", "contract", "raw_row", "quality_issue", "insurer", "incentive_plan_doc", "incentive_plan_line", "job")}


def real_file(path, n=40, ym="202602", seed=7):
    """가짜 실제 양식에 민감 열 하나를 덧붙인다. 적재기는 이 열을 읽지 않고 버려야 한다."""
    rows = fe.fake_contract_rows(n, ym, seed)
    for r in rows:
        r[SENSITIVE_HEAD] = SENSITIVE_VALUE
    fe.write_xlsx(path, fe.CONTRACT_HEADERS + [SENSITIVE_HEAD], rows, sheet="계약현황")
    return path, rows


def sample_file(con, path, ym="202611"):
    """샘플 양식(본부 계약현황). 마스터에 없는 사번 줄을 하나 넣는다. 그 줄의 이름은 화면에 보이면 안 된다."""
    ag = con.execute("SELECT internal_code, name FROM agent LIMIT 1").fetchone()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "계약현황"
    ws["A1"] = "샘플 계약 현황"
    ws.append([])
    ws.append(samples.ORG_HEADERS)
    lines = [(ag["internal_code"], ag["name"], "APPX0001", 100000, 80000), ("Z9999", "숨길이름", "APPX0002", 50000, 40000)]
    for i, (code, name, app_no, prem, rec) in enumerate(lines, start=1):
        ws.append([i, "본부", "지점", code, name, "샘플생명", "샘플생명 건강보험", "건강", app_no, "", f"{ym[:4]}-{ym[4:]}-03", f"{ym[:4]}-{ym[4:]}-05",
                   "성립", f"{ym[:4]}-{ym[4:]}-05", "월납", 20, prem, rec, 1, "계*자", ""])
    ws.append(["", "", "", "합계", "", "", "", "", "", "", "", "", "", "", "", "", 150000, 120000, 2, "", ""])
    wb.save(path)
    return path


def upload(c, url, path, **data):
    return c.post(url, data=data, files={"file": (pathlib.Path(path).name, pathlib.Path(path).read_bytes(), "application/octet-stream")})


# ---------------------------------------------------------------- 권한과 CSRF
def test_only_main_admin(env):
    admin = login(env)
    for p in PATHS:
        assert admin.get(p).status_code == 200, p
    for who in others(env):
        c = login(env, who)
        for p in PATHS:
            assert c.get(p).status_code == 403, (who, p)
            assert c.post(p, data={"action": "preview"}).status_code == 403, (who, p)
    html = admin.get("/").text
    assert "계약 엑셀" in html


def test_post_without_csrf_is_refused(env):
    con = env["con"]
    admin = login(env)
    before = counts(con)
    name = con.execute("SELECT name FROM insurer WHERE code='SAM'").fetchone()[0]
    for p, data in (("/data/contracts", {"action": "apply"}), ("/data/plan", {"action": "apply"}), ("/data/quality", {"action": "resolve", "ids": "1"}),
                    ("/data/batches", {"action": "reload", "batch_id": "1", "confirm": "yes"}),
                    ("/org/masters", {"action": "insurer_save", "id": "1", "name": "바꿈", "sector": "life", "active": "1"})):
        assert TestClient.post(admin, p, data=data).status_code == 403, p
        assert TestClient.post(admin, p, data={**data, "csrf_token": "wrong"}).status_code == 403, p
    assert counts(con) == before
    assert con.execute("SELECT name FROM insurer WHERE code='SAM'").fetchone()[0] == name


# ---------------------------------------------------------------- 계약 엑셀
def test_contract_preview_writes_nothing_then_apply(env):
    con, tmp = env["con"], env["tmp"]
    path, rows = real_file(tmp / "contract_a.xlsx")
    admin = login(env)
    before = counts(con)
    r = upload(admin, "/data/contracts", path, action="preview", ym="202602", fmt="auto")
    assert r.status_code == 200, r.text
    assert "미리보기" in r.text and "합계 대조" in r.text and "이대로 적재하기" in r.text and "실제 양식" in r.text
    assert counts(con) == before                                                    # 미리보기는 원래 DB 에 쓰지 않는다
    names = {x["계약자"] for x in rows} | {x["피보험자"] for x in rows} | {x["모집사원명"] for x in rows}
    assert not any(n in r.text for n in names)                                      # 이름은 보이지 않는다
    assert SENSITIVE_VALUE not in r.text
    assert len(list((tmp / "up").iterdir())) == 1

    r = admin.post("/data/contracts", data={"action": "apply"})
    assert r.status_code == 200 and "배치" in r.text and "적재해" in r.text
    after = counts(con)
    assert after["import_batch"] == before["import_batch"] + 1 and after["contract"] > before["contract"]
    assert list((tmp / "up").iterdir()) == []                                       # 올린 임시 파일은 지운다
    b = con.execute("SELECT * FROM import_batch ORDER BY batch_id DESC LIMIT 1").fetchone()
    assert b["mapping_version"].startswith("eternal_contract@") and b["received_via"] == "upload" and b["file_path"].startswith("raw/contract/202602/")
    assert (tmp / "data" / b["file_path"]).is_file()                                # 원본은 원본 구역에 남는다
    kinds = {r[0] for r in con.execute("SELECT kind FROM job WHERE status='queued'")}
    assert {"perf_run", "rank_run"} <= kinds                                        # 계산은 작업으로 넣는다
    assert con.execute("SELECT 1 FROM audit_log WHERE action='contract_upload' AND target LIKE ?", (f"batch:{b['batch_id']}%",)).fetchone()


def test_sensitive_column_not_in_db(env):
    con = env["con"]
    b = con.execute("SELECT batch_id FROM import_batch WHERE mapping_version LIKE 'eternal_contract@%' ORDER BY batch_id LIMIT 1").fetchone()[0]
    raws = [r[0] for r in con.execute("SELECT raw_json FROM raw_row WHERE batch_id=?", (b,))]
    assert raws and not any(SENSITIVE_HEAD in x or SENSITIVE_VALUE in x for x in raws)
    cols = [r[1] for r in con.execute("PRAGMA table_info(contract)")]
    assert not any("주민" in c or "계좌" in c or "소득" in c for c in cols)
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        text_cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})") if (r[2] or "").upper() in ("TEXT", "")]
        for c in text_cols:
            assert not con.execute(f'SELECT 1 FROM "{t}" WHERE "{c}" LIKE ? LIMIT 1', (f"%{SENSITIVE_VALUE}%",)).fetchone(), (t, c)
    assert con.execute("SELECT 1 FROM quality_issue WHERE batch_id=? AND check_code='dropped_column'", (b,)).fetchone()


def test_same_file_is_blocked(env):
    con, tmp = env["con"], env["tmp"]
    admin = login(env)
    before = counts(con)
    r = upload(admin, "/data/contracts", tmp / "contract_a.xlsx", action="preview", ym="202602")
    assert r.status_code == 409 and "이미 적재한 파일" in r.text
    assert counts(con) == before and list((tmp / "up").iterdir()) == []
    assert admin.post("/data/contracts", data={"action": "apply"}).status_code == 400          # 미리보기한 파일이 없다


def test_reload_needs_confirm_and_keeps_old_batch(env):
    con = env["con"]
    admin = login(env)
    old = con.execute("SELECT * FROM import_batch WHERE data_type='org_contract' ORDER BY batch_id DESC LIMIT 1").fetchone()
    n_contract = con.execute("SELECT COUNT(*) FROM contract").fetchone()[0]
    page = admin.get(f"/data/batches?id={old['batch_id']}&confirm=reload").text
    assert "위 내용을 확인했습니다" in page
    before = counts(con)
    r = admin.post("/data/batches", data={"action": "reload", "batch_id": old["batch_id"]})
    assert r.status_code == 400 and "확인하는 칸" in r.text
    assert counts(con) == before
    assert con.execute("SELECT status FROM import_batch WHERE batch_id=?", (old["batch_id"],)).fetchone()[0] == old["status"]

    r = admin.post("/data/batches", data={"action": "reload", "batch_id": old["batch_id"], "confirm": "yes"})
    assert r.status_code == 200 and "다시 적재해" in r.text
    o = con.execute("SELECT * FROM import_batch WHERE batch_id=?", (old["batch_id"],)).fetchone()
    new = con.execute("SELECT * FROM import_batch ORDER BY batch_id DESC LIMIT 1").fetchone()
    assert o["status"] == "superseded" and o["file_hash"].startswith(old["file_hash"]) and new["file_hash"] == old["file_hash"]
    assert new["batch_id"] != old["batch_id"] and new["version"] == old["version"] + 1
    assert con.execute("SELECT COUNT(*) FROM raw_row WHERE batch_id=?", (old["batch_id"],)).fetchone()[0] > 0     # 원본 행은 지우지 않는다
    assert con.execute("SELECT COUNT(*) FROM contract").fetchone()[0] == n_contract
    assert (env["tmp"] / "data" / old["file_path"]).is_file()
    assert con.execute("SELECT 1 FROM audit_log WHERE action='contract_reload'").fetchone()
    # 대체된 배치는 다시 적재하지 않는다
    r = admin.post("/data/batches", data={"action": "reload", "batch_id": old["batch_id"], "confirm": "yes"})
    assert r.status_code == 400 and "대체된" in r.text


def test_big_file_goes_to_worker(env, monkeypatch):
    con, tmp = env["con"], env["tmp"]
    monkeypatch.setattr(data_routes, "BIG_ROWS", 10)
    path, _ = real_file(tmp / "contract_big.xlsx", n=30, ym="202603", seed=21)
    admin = login(env)
    r = upload(admin, "/data/contracts", path, action="preview", ym="202603")
    assert r.status_code == 200 and "작업으로 넣기" in r.text
    n_batch = counts(con)["import_batch"]
    r = admin.post("/data/contracts", data={"action": "apply"})
    assert r.status_code == 200 and "작업" in r.text and "/data/jobs" in r.text
    assert counts(con)["import_batch"] == n_batch                                     # 화면은 적재하지 않는다
    job = con.execute("SELECT * FROM job WHERE kind='ingest' ORDER BY job_id DESC LIMIT 1").fetchone()
    assert job and job["status"] == "queued" and "202603" in job["params"]
    jobs.drain(con)                                                                    # 워커가 하는 일. 앞서 넣은 업적 run 도 함께 처리한다
    done = jobs.get(con, job["job_id"])
    assert done["status"] == "done" and done["result"]["counts"] == {"loaded": 1}, done
    assert counts(con)["import_batch"] == n_batch + 1


def test_reload_blocked_when_later_batch_exists(env):
    con = env["con"]
    admin = login(env)
    first = con.execute("SELECT batch_id FROM import_batch WHERE data_type='org_contract' AND status!='superseded' ORDER BY batch_id LIMIT 1").fetchone()[0]
    r = admin.post("/data/batches", data={"action": "reload", "batch_id": first, "confirm": "yes"})
    assert r.status_code == 400 and "그 뒤에 들어온" in r.text


def test_sample_format_needs_hq_and_hides_names(env):
    con, tmp = env["con"], env["tmp"]
    path = sample_file(con, tmp / "sample.xlsx")
    admin = login(env)
    r = upload(admin, "/data/contracts", path, action="preview", ym="202611", fmt="auto")
    assert r.status_code == 400 and "업로드 본부" in r.text
    hq = con.execute("SELECT org_id FROM org_unit WHERE level='hq' LIMIT 1").fetchone()[0]
    before = counts(con)
    r = upload(admin, "/data/contracts", path, action="preview", ym="202611", fmt="auto", hq=str(hq))
    assert r.status_code == 200 and "샘플 양식" in r.text and "마스터에 없는 사번" in r.text
    assert "숨길이름" not in r.text and data_routes.HIDDEN_TEXT in r.text
    assert counts(con) == before
    r = admin.post("/data/contracts", data={"action": "apply"})
    assert r.status_code == 200
    b = con.execute("SELECT * FROM import_batch ORDER BY batch_id DESC LIMIT 1").fetchone()
    assert b["mapping_version"].startswith("internal_org_contract@") and b["upload_org_id"] == hq
    q = admin.get(f"/data/quality?batch={b['batch_id']}&state=all").text
    assert "숨길이름" not in q and "마스터에 없는 사번" in q


def test_wrong_file_is_refused(env):
    tmp = env["tmp"]
    bad = tmp / "bad.xlsx"
    bad.write_bytes(b"not an excel file")
    r = upload(login(env), "/data/contracts", bad, action="preview", ym="202602")
    assert r.status_code == 400 and "양식" in r.text
    assert list((tmp / "up").iterdir()) == []


# ---------------------------------------------------------------- 품질검증
def test_quality_resolve_and_reopen_are_logged(env):
    con = env["con"]
    admin = login(env)
    ids = [r[0] for r in con.execute("SELECT id FROM quality_issue WHERE resolved=0 LIMIT 2")]
    assert ids
    r = admin.post("/data/quality", data={"action": "resolve", "ids": [str(i) for i in ids]})
    assert r.status_code == 200 and "2건을 처리함" in r.text
    assert all(con.execute("SELECT resolved FROM quality_issue WHERE id=?", (i,)).fetchone()[0] == 1 for i in ids)
    assert con.execute("SELECT target FROM audit_log WHERE action='quality_resolve' ORDER BY id DESC").fetchone()[0].startswith("n=2")
    r = admin.post("/data/quality", data={"action": "reopen", "ids": [str(ids[0])]})
    assert r.status_code == 200 and con.execute("SELECT resolved FROM quality_issue WHERE id=?", (ids[0],)).fetchone()[0] == 0
    assert "처리 기록" in admin.get("/data/quality").text
    assert admin.post("/data/quality", data={"action": "resolve"}).status_code == 400


# ---------------------------------------------------------------- 시상안
def test_plan_preview_apply_and_approve(env):
    con, tmp = env["con"], env["tmp"]
    path = samples.write_incentive_plan(tmp / "plan")
    admin = login(env)
    before = counts(con)
    r = upload(admin, "/data/plan", path, action="preview", ym="202610")
    assert r.status_code == 200 and "이대로 원장에 쌓기" in r.text and "사람 확인 필요" in r.text
    assert counts(con) == before
    r = admin.post("/data/plan", data={"action": "apply"})
    assert r.status_code == 200 and "원장" in r.text
    doc = con.execute("SELECT * FROM incentive_plan_doc ORDER BY plan_doc_id DESC LIMIT 1").fetchone()
    assert doc and counts(con)["incentive_plan_line"] == doc["row_count"] > 0
    page = admin.get(f"/data/plan?doc={doc['plan_doc_id']}").text
    assert "원문 확인" in page                                                       # 금액이 원문에만 있는 줄
    lines = [r[0] for r in con.execute("SELECT line_id FROM incentive_plan_line WHERE plan_doc_id=? ORDER BY line_id LIMIT 3", (doc["plan_doc_id"],))]
    r = admin.post("/data/plan", data={"action": "approve", "doc": doc["plan_doc_id"], "ids": [str(i) for i in lines[:2]] + ["999999"]})
    assert r.status_code == 200 and "2줄을 승인" in r.text
    st = {i: con.execute("SELECT review_status, reviewed_by FROM incentive_plan_line WHERE line_id=?", (i,)).fetchone() for i in lines}
    assert st[lines[0]][0] == st[lines[1]][0] == "approved" and st[lines[0]][1] == "admin" and st[lines[2]][0] == "draft"
    r = admin.post("/data/plan", data={"action": "reject", "doc": doc["plan_doc_id"], "ids": [str(lines[2])]})
    assert con.execute("SELECT review_status FROM incentive_plan_line WHERE line_id=?", (lines[2],)).fetchone()[0] == "rejected"
    r = upload(admin, "/data/plan", path, action="preview", ym="202610")
    assert r.status_code == 409 and "이미 적재한 시상안" in r.text


def test_plan_lines_flag_original_when_no_rate():
    line = {"recipient_level": "fc", "parse_note": "시상내용 칸이 비어 있음. 금액은 원본의 표·그림 확인 필요", "reward_raw": ""}
    assert data_routes.needs_original(line, [])
    assert not data_routes.needs_original({"recipient_level": "fc", "parse_note": None, "reward_raw": "월초 300%"}, ["월초 × 300%"])
    assert not data_routes.needs_original({"recipient_level": "note", "parse_note": None, "reward_raw": ""}, [])


# ---------------------------------------------------------------- 마스터
def test_masters_edit_with_history(env):
    con = env["con"]
    admin = login(env)
    for tab in ("insurer", "product", "category", "map", "history"):
        assert admin.get(f"/org/masters?tab={tab}").status_code == 200, tab
    assert "D-04" in admin.get("/org/masters?tab=map").text
    r = admin.post("/org/masters", data={"action": "insurer_save", "code": "tst1", "name": "시험보험", "sector": "nonlife", "active": "1"})
    assert r.status_code == 200 and "만들었습니다" in r.text
    iid = con.execute("SELECT insurer_id FROM insurer WHERE code='TST1'").fetchone()[0]
    assert admin.post("/org/masters", data={"action": "insurer_save", "code": "TST1", "name": "또", "sector": "life"}).status_code == 400
    r = admin.post("/org/masters", data={"action": "insurer_save", "id": iid, "name": "시험보험 고침", "sector": "nonlife", "active": "1"})
    assert r.status_code == 200
    log = con.execute("SELECT before_json, after_json FROM audit_log WHERE action='master_update' AND target='insurer:TST1'").fetchone()
    assert "시험보험" in log[0] and "시험보험 고침" in log[1]

    p = con.execute("SELECT * FROM product LIMIT 1").fetchone()
    form = {"action": "product_save", "id": p["product_id"], "insurer_id": p["insurer_id"], "product_code": p["product_code"],
            "product_name": p["product_name"], "product_category": p["product_category"], "conversion_rate": "0.55",
            "effective_from": p["effective_from"], "effective_to": p["effective_to"] or ""}
    r = admin.post("/org/masters", data=form)
    assert r.status_code == 400 and "계산 기준" in r.text                              # 인정률은 확인 칸이 있어야 바꾼다
    assert float(con.execute("SELECT conversion_rate FROM product WHERE product_id=?", (p["product_id"],)).fetchone()[0]) == float(p["conversion_rate"])
    r = admin.post("/org/masters", data={**form, "ack": "1"})
    assert r.status_code == 200 and float(con.execute("SELECT conversion_rate FROM product WHERE product_id=?", (p["product_id"],)).fetchone()[0]) == 0.55
    assert admin.post("/org/masters", data={**form, "ack": "1", "conversion_rate": "abc"}).status_code == 400

    r = admin.post("/org/masters", data={"action": "category_save", "id": "savings", "sort_order": "2", "incentive_eligible": "1"})
    assert r.status_code == 400
    assert con.execute("SELECT incentive_eligible FROM product_category WHERE code='savings'").fetchone()[0] == 0
    hist = admin.get("/org/masters?tab=history").text
    assert "insurer:TST1" in hist and "인정률" in hist
    assert "마스터 고침" in admin.get("/org/audit").text


def test_loader_insurer_can_be_confirmed(env):
    con = env["con"]
    row = con.execute("SELECT insurer_id, code FROM insurer WHERE origin='loader' LIMIT 1").fetchone()
    assert row, "가짜 실제 양식의 모르는 보험사가 임시로 등록돼 있어야 한다"
    admin = login(env)
    assert "임시 등록" in admin.get("/org/masters").text
    r = admin.post("/org/masters", data={"action": "insurer_confirm", "id": row["insurer_id"]})
    assert r.status_code == 200 and "확정" in r.text
    assert con.execute("SELECT origin FROM insurer WHERE insurer_id=?", (row["insurer_id"],)).fetchone()[0] == "master"
