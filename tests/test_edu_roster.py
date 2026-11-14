"""수료자 명부 적재기 시험. 임시 DB 와 가짜 명부만 쓴다. 실제 명부 양식은 아직 없다(D-09).

적재 건수, 겹친 건 세기, 다시 올리기, 민감 칸이 DB 에 없음, 모르는 과정과 사번, 머리글 검사,
연결 흐름을 거친 적재와 같은 파일 막기를 본다.
"""
import json
import sqlite3

import pytest

from app import db, masters
from app.connect.base import DUPLICATE, FAILED, LOADED, RawFile
from app.connect.handlers import DEFAULT_REGISTRY
from app.connect.pipeline import Pipeline
from app.connect.sources import FolderWatchSource, UploadSource, infer_kind
from app.connect.state import StateStore
from app.engine import education as edu
from app.loader import edu_roster as er
from app.loader import fake_edu_roster as fr
from app.loader.excel_loader import DuplicateFile
from app.loader.sheet import HeaderMismatch

CP = {"code": "CP", "name": "준법 과정"}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CONNECT_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("CONNECT_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture()
def env(tmp_path):
    con = db.init_db(tmp_path / "t.db")
    masters.seed(con)
    cid = edu.save_course(con, by="t", code="CP", name="준법 과정", kind="compliance", required=True)
    yearly = edu.save_course(con, by="t", code="YR", name="매년 과정", kind="refresher", required=True,
                             deadline_rule="yearly", deadline_md="12-31")
    for d in ("2026-09-01", "2026-10-01"):
        edu.save_session(con, by="t", course_id=cid, date_from=d, online=True, status="held")
    people = fr.agents_from_db(con)
    sessions = fr.sessions_from_db(con, "CP")
    make = lambda name, **kw: fr.make_roster_file(tmp_path / "in" / name, people, kw.pop("course", CP),
                                                  kw.pop("sessions", sessions), **kw)
    yield {"con": con, "tmp": tmp_path, "cid": cid, "yearly": yearly, "people": people, "make": make, "db_path": tmp_path / "t.db"}
    con.close()


def _live(con, batch_id=None):
    sql = "SELECT COUNT(*) FROM edu_completion WHERE voided_at IS NULL"
    return con.execute(sql + (" AND batch_id=?" if batch_id else ""), (batch_id,) if batch_id else ()).fetchone()[0]


def _dump(path) -> str:
    c = sqlite3.connect(str(path))
    try:
        return "\n".join(c.iterdump())
    finally:
        c.close()


# ------------------------------------------------------------------ 적재 건수
def test_clean_roster_loads_one_completion_per_row(env):
    con, people = env["con"], env["people"]
    path, rows = env["make"]("명부.xlsx")
    pv = er.preview(con, path)
    assert pv["ok"] and pv["file_rows"] == len(people) and pv["new"] == len(people) and pv["block"] == 0
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 0         # 미리보기는 쓰지 않는다

    out = er.load_roster(con, path, by="담당")
    assert (out["file_rows"], out["loaded"], out["duplicates"], out["block"]) == (len(people), len(people), 0, 0)
    assert out["loaded"] == pv["new"] and out["courses"] == {"CP": len(people)}
    b = con.execute("SELECT * FROM import_batch WHERE batch_id=?", (out["batch_id"],)).fetchone()
    assert (b["data_type"], b["status"], b["row_count"]) == ("edu_roster", "active", len(people))
    assert b["mapping_version"] == "edu_roster@v1"
    got = con.execute("SELECT agent_id, session_id, completed_on, source, row_no, recorded_by FROM edu_completion ORDER BY row_no").fetchall()
    assert [r["row_no"] for r in got] == list(range(2, len(people) + 2))
    assert {r["source"] for r in got} == {"roster"} and {r["recorded_by"] for r in got} == {"담당"}
    assert [r["agent_id"] for r in got] == [p["agent_id"] for p in people]
    # 차수가 있으면 차수에 잇고, 수료일은 명부의 값이다
    s1 = con.execute("SELECT session_id, date_to FROM edu_session WHERE course_id=? AND session_no=1", (env["cid"],)).fetchone()
    assert (got[0]["session_id"], got[0]["completed_on"]) == (s1["session_id"], s1["date_to"])
    # 귀속월을 주지 않으면 수료일이 가장 많은 달이다
    assert out["period_ym"] in ("202609", "202610") and b["period_ym"] == out["period_ym"]


def test_csv_with_other_header_names_and_cp949(env):
    con, people = env["con"], env["people"]
    path, _ = env["make"]("명부.csv", alt_headers=True, encoding="cp949")
    out = er.load_roster(con, path, "202610")
    assert out["loaded"] == len(people) and out["block"] == 0 and out["period_ym"] == "202610"


def test_session_or_education_day_gives_completion_date(env):
    con = env["con"]
    path, _ = env["make"]("명부.xlsx", n=4, drop=("수료일",))          # 수료일 칸이 없으면 차수의 종료일
    out = er.load_roster(con, path)
    assert out["loaded"] == 4
    days = {r[0] for r in con.execute("SELECT completed_on FROM edu_completion")}
    assert days <= {"2026-09-01", "2026-10-01"}


# ------------------------------------------------------------------ 겹친 건
def test_duplicates_in_file_and_against_existing_are_counted(env):
    con, people = env["con"], env["people"]
    path, _ = env["make"]("명부.xlsx", dirty=True)
    out = er.load_roster(con, path)
    assert out["dup_in_file"] == 1 and out["dup_existing"] == 0
    first = out["loaded"]
    assert first == len(people) - 5 - 1                     # 막힌 5줄과 파일 안에서 겹친 1줄
    # 처음 8명을 다시 담은 다른 파일. 앞 파일에서 앞 6명은 막히거나 다른 사람으로 바뀌어 빠졌고, 7, 8번째만 들어가 있다
    path2, _ = env["make"]("명부2.xlsx", n=8, seed=99)
    pv = er.preview(con, path2)
    out2 = er.load_roster(con, path2)
    assert (pv["new"], pv["dup_existing"]) == (out2["loaded"], out2["dup_existing"]) == (6, 2)
    assert _live(con) == first + out2["loaded"]
    codes = {r["check_code"] for r in er.batch_issues(con, out2["batch_id"])}
    assert "duplicate_existing" in codes


def test_yearly_course_counts_each_year_once(env):
    con, people = env["con"], env["people"]
    yr = {"code": "YR", "name": "매년 과정"}
    rows = fr.fake_roster_rows(people[:2], yr, [])
    rows += [dict(rows[0], **{"수료일": "2025.11.02"}), dict(rows[0], **{"수료일": "2026.12.01"})]
    path = fr.write_csv(env["tmp"] / "in" / "yr.csv", fr.HEADERS, rows)
    out = er.load_roster(con, path)
    # 첫 사람: 2026 한 번, 2025 한 번, 2026 다시(겹침). 둘째 사람: 2026 한 번
    assert (out["loaded"], out["dup_in_file"]) == (3, 1)


# ------------------------------------------------------------------ 다시 올리기
def test_reupload_with_replaces_voids_previous_batch(env):
    con, people = env["con"], env["people"]
    bad, _ = env["make"]("명부_틀림.xlsx", dirty=True)
    a = er.load_roster(con, bad)
    fixed, _ = env["make"]("명부_고침.xlsx")
    # 바꿀 배치를 주지 않으면 앞 배치와 모두 겹친다
    pv = er.preview(con, fixed)
    assert pv["dup_existing"] == a["loaded"] and pv["new"] == len(people) - a["loaded"]
    pv = er.preview(con, fixed, replaces=a["batch_id"])
    assert pv["dup_existing"] == 0 and pv["new"] == len(people)

    b = er.load_roster(con, fixed, replaces=a["batch_id"], by="담당")
    assert b["replaced_batch"] == a["batch_id"] and b["voided"] == a["loaded"]
    assert b["loaded"] == len(people) and b["duplicates"] == 0
    assert _live(con, a["batch_id"]) == 0 and _live(con) == len(people)
    old = con.execute("SELECT status FROM import_batch WHERE batch_id=?", (a["batch_id"],)).fetchone()
    assert old["status"] == "superseded"
    # 지우지 않는다. 앞 배치의 줄은 뺀 표시와 함께 남는다
    kept = con.execute("SELECT COUNT(*), MIN(voided_by), MIN(void_reason) FROM edu_completion WHERE batch_id=?", (a["batch_id"],)).fetchone()
    assert kept[0] == a["loaded"] and kept[1] == "담당" and str(b["batch_id"]) in kept[2]
    assert con.execute("SELECT 1 FROM audit_log WHERE action='edu_batch_void' AND target=?", (str(a["batch_id"]),)).fetchone()
    # 이미 바꾼 배치는 다시 바꿀 수 없다
    again, _ = env["make"]("명부_또.xlsx", seed=3)
    with pytest.raises(ValueError):
        er.load_roster(con, again, replaces=a["batch_id"])
    listed = {r["batch_id"]: r for r in er.roster_batches(con)}
    assert listed[a["batch_id"]]["live"] == 0 and listed[b["batch_id"]]["live"] == len(people)


def test_reupload_without_loadable_rows_keeps_previous_batch(env):
    con = env["con"]
    a = er.load_roster(con, env["make"]("명부.xlsx")[0])
    rows = fr.fake_roster_rows(env["people"][:3], {"code": "NO-SUCH", "name": "없는 과정"}, [])
    path = fr.write_csv(env["tmp"] / "in" / "전부틀림.csv", fr.HEADERS, rows)
    assert not er.preview(con, path, replaces=a["batch_id"])["ok"]
    with pytest.raises(ValueError):
        er.load_roster(con, path, replaces=a["batch_id"])
    assert _live(con, a["batch_id"]) == a["loaded"]
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 1


def test_same_file_twice_is_blocked(env):
    con = env["con"]
    path, _ = env["make"]("명부.xlsx")
    er.load_roster(con, path)
    assert er.already_loaded(con, er.file_hash(path))
    assert er.preview(con, path)["already_loaded"]
    with pytest.raises(DuplicateFile):
        er.load_roster(con, path)
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 1


# ------------------------------------------------------------------ 민감 칸
def test_sensitive_columns_and_names_never_reach_db(env):
    con, people = env["con"], env["people"]
    path, rows = env["make"]("명부.xlsx", dirty=True)
    pv = er.preview(con, path)
    out = er.load_roster(con, path)
    assert set(out["dropped_columns"]) == set(fr.SENSITIVE)
    con.commit()
    dump = _dump(env["db_path"])
    for bad in (fr.FAKE_PHONE_PREFIX, fr.FAKE_RRN_PREFIX, "x-fake-birth", fr.MISMATCH_NAME, fr.UNKNOWN_NAME):
        assert bad not in dump
    # 원본 행을 남기지 않는다. 이름이 들어 있기 때문이다
    assert con.execute("SELECT COUNT(*) FROM raw_row WHERE batch_id=?", (out["batch_id"],)).fetchone()[0] == 0
    # 품질 문제, 미리보기, 적재 결과에 사람 이름이 없다
    text = json.dumps([pv, out, er.batch_issues(con, out["batch_id"]), er.roster_batches(con)], ensure_ascii=False)
    for name in {r["성명"] for r in rows} | {p["name"] for p in people}:
        assert name not in text


# ------------------------------------------------------------------ 모르는 과정과 사번
def test_unknown_agent_course_bad_date_and_name_mismatch_are_blocked(env):
    con = env["con"]
    path, _ = env["make"]("명부.xlsx", dirty=True)
    out = er.load_roster(con, path)
    blocks = {(i["row_no"], i["check_code"]) for i in er.batch_issues(con, out["batch_id"]) if i["severity"] == "block"}
    row = lambda k: fr.DIRTY[k] + 1
    assert blocks == {(row("unknown_agent"), "unmapped_agent"), (row("blank_agent"), "blank_agent"),
                      (row("unknown_course"), "unknown_course"), (row("bad_date"), "bad_date"),
                      (row("name_mismatch"), "name_mismatch")}
    loaded_rows = {r[0] for r in con.execute("SELECT row_no FROM edu_completion WHERE batch_id=?", (out["batch_id"],))}
    assert not loaded_rows & {r for r, _ in blocks}
    assert out["batch_status"] == "active"


def test_course_name_only_and_alias(env, monkeypatch):
    con = env["con"]
    rows = fr.fake_roster_rows(env["people"][:3], {"code": "", "name": "준법교육"}, [])
    path = fr.write_csv(env["tmp"] / "in" / "이름만.csv", [h for h in fr.HEADERS if h != "과정코드"], rows)
    pv = er.preview(con, path)
    assert pv["loadable"] == 0 and pv["by_code"].get("unknown_course") == 3
    real = er.load_mapping

    def with_alias(ym=None):
        m = real(ym)
        return {**m, "course_alias": {"준법교육": "CP"}}
    monkeypatch.setattr(er, "load_mapping", with_alias)
    assert er.load_roster(con, path)["loaded"] == 3


def test_name_mismatch_can_be_warning_and_masked_names_match(env, monkeypatch):
    assert er.same_name("홍*동", "홍길동") and er.same_name("홍 길동", "홍길동")
    assert not er.same_name("홍길순", "홍길동") and not er.same_name("홍*", "홍길동")
    real = er.load_mapping
    monkeypatch.setattr(er, "load_mapping", lambda ym=None: {**real(ym), "name_mismatch": "warn"})
    path, _ = env["make"]("명부.xlsx", n=7, dirty=True)
    out = er.load_roster(env["con"], path)
    codes = {(i["check_code"], i["severity"]) for i in er.batch_issues(env["con"], out["batch_id"])}
    assert ("name_mismatch", "warn") in codes


def test_all_rows_blocked_marks_batch_failed(env):
    con = env["con"]
    rows = fr.fake_roster_rows(env["people"][:2], {"code": "NO-SUCH", "name": "없는 과정"}, [])
    out = er.load_roster(con, fr.write_csv(env["tmp"] / "in" / "x.csv", fr.HEADERS, rows))
    assert out["batch_status"] == "failed" and out["loaded"] == 0 and out["block"] == 2


# ------------------------------------------------------------------ 머리글
def test_missing_or_ambiguous_headers_block_before_writing(env):
    con = env["con"]
    path, _ = env["make"]("사번없음.xlsx", drop=("사번",))
    with pytest.raises(HeaderMismatch) as e:
        er.load_roster(con, path)
    assert "사번" in str(e.value)
    assert not er.preview(con, path)["ok"]
    rows = fr.fake_roster_rows(env["people"][:2], CP, [])
    for r in rows:
        r["사원번호"] = r["사번"]
    two = fr.write_csv(env["tmp"] / "in" / "두칸.csv", fr.HEADERS + ["사원번호"], rows)
    with pytest.raises(HeaderMismatch) as e:
        er.load_roster(con, two)
    assert "둘 이상" in str(e.value)
    nodate, _ = env["make"]("날짜없음.xlsx", drop=("차수", "교육일", "수료일"))
    with pytest.raises(HeaderMismatch):
        er.load_roster(con, nodate)
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 0


# ------------------------------------------------------------------ 연결 흐름
def _pipe(env):
    return Pipeline(env["con"], registry=DEFAULT_REGISTRY, data_dir=env["tmp"] / "data", state=StateStore(env["tmp"] / "state"))


def test_pipeline_loads_once_and_blocks_same_file(env):
    con, people = env["con"], env["people"]
    assert DEFAULT_REGISTRY.get("edu_roster") is not None
    path, _ = env["make"]("수료자명부.xlsx", dirty=True)
    pipe = _pipe(env)
    first = pipe.run(UploadSource.from_path(path, kind="edu_roster", period_ym="202610"))
    second = pipe.run(UploadSource.from_path(path, kind="edu_roster", period_ym="202610"))
    assert [r.status for r in first.results] == [LOADED]
    assert [r.status for r in second.results] == [DUPLICATE]
    res = first.results[0]
    assert res.detail["loaded"] == len(people) - 6 and res.detail["dup_in_file"] == 1 and res.detail["block"] == 5
    b = con.execute("SELECT * FROM import_batch").fetchall()
    assert len(b) == 1 and b[0]["data_type"] == "edu_roster" and b[0]["received_via"] == "upload"
    assert b[0]["file_path"] == res.raw_path and res.raw_path.startswith("raw/edu_roster/202610/")
    assert (env["tmp"] / "data" / res.raw_path).exists()
    for p in people:
        assert p["name"] not in json.dumps(res.to_dict(), ensure_ascii=False)


def test_pipeline_without_period_folder_inference_and_replaces(env):
    assert infer_kind("수료자명부_202610.xlsx") == "edu_roster"
    folder = env["tmp"] / "sync" / "edu_roster"
    path, _ = env["make"]("명부.xlsx", dirty=True)
    folder.mkdir(parents=True)
    (folder / "명부.xlsx").write_bytes(path.read_bytes())
    pipe = _pipe(env)
    run = pipe.run(FolderWatchSource(env["tmp"] / "sync", state=StateStore(env["tmp"] / "state"), min_age=0))
    assert [r.status for r in run.results] == [LOADED]
    first = run.results[0].detail
    assert first["period_ym"] in ("202609", "202610")
    fixed, _ = env["make"]("명부_고침.xlsx")
    res = pipe.ingest(RawFile.from_path(fixed, kind="edu_roster", replaces=first["batch_id"]))
    assert res.status == LOADED and res.detail["voided"] == first["loaded"] and res.detail["loaded"] == len(env["people"])


def test_pipeline_header_mismatch_fails_without_batch(env):
    path, _ = env["make"]("사번없음.xlsx", drop=("사번",))
    res = _pipe(env).ingest(RawFile.from_path(path, kind="edu_roster", period_ym="202610"))
    assert res.status == FAILED and "사번" in res.reason
    assert env["con"].execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 0
