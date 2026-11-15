"""S3 운영 기반 (파트 K, platform). 세션 버전, 횟수 제한, 상태 점검, 관측, 작업과 워커, 저장소, 백업과 되살리기, SQL 호환 층."""
import json
import logging
import os
import pathlib
import re
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app import db, masters
from app.ops import backup, health, jobs, observe, ratelimit, schema, sessions, sqlcompat
from app.ops.storage import LocalStorage, StorageError, get_storage
from app.web import auth

PW = "platform-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')


class Client(TestClient):
    """폼을 보낼 때 화면의 숨은 CSRF 토큰을 받아 함께 보낸다. with_csrf=False 면 토큰 없이 보낸다."""
    def post(self, url, data=None, with_csrf=True, **kw):
        if with_csrf and data is not None and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    for k in ratelimit.ENV.values():
        monkeypatch.delenv(k, raising=False)
    ratelimit.LIMITER.reset()
    yield
    ratelimit.LIMITER.reset()


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    path = tmp_path_factory.mktemp("platform") / "t.db"
    con = db.init_db(path)
    masters.seed(con)
    auth.seed_accounts(con, PW)
    before = os.environ.get("APP_DB")
    os.environ["APP_DB"] = str(path)
    from app.web.main import app
    yield {"con": con, "app": app, "path": path}
    con.close()
    if before is None:
        os.environ.pop("APP_DB", None)
    else:
        os.environ["APP_DB"] = before


@pytest.fixture(autouse=True)
def _db_env(request):
    """web 을 쓰는 시험은 그 DB 를 APP_DB 로 둔다. 다른 모듈의 시험이 APP_DB 를 바꿔 두었을 수 있다."""
    if "web" in request.fixturenames:
        os.environ["APP_DB"] = str(request.getfixturevalue("web")["path"])
    yield


def login(web, login_id="admin", password=PW, client=("testclient", 50000)):
    c = Client(web["app"], follow_redirects=False, client=client)
    r = c.post("/login", data={"login_id": login_id, "password": password})
    assert r.status_code == 303, r.text
    return c


def agent_login(web):
    return web["con"].execute("SELECT internal_code FROM agent WHERE agent_id NOT IN (SELECT agent_id FROM org_manager) AND status='active' LIMIT 1").fetchone()[0]


def uid(web, login_id):
    return web["con"].execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0]


def copy_of(c):
    """브라우저 밖으로 빼 둔 쿠키. 같은 서명 쿠키를 다른 클라이언트에 넣는다."""
    other = Client(c.app, follow_redirects=False)
    other.cookies.set("session", c.cookies.get("session"))
    return other


# ---------------------------------------------------------------- 세션 버전
def test_logout_ends_a_copied_cookie(web):
    c = login(web)
    stolen = copy_of(c)
    assert stolen.get("/").status_code == 200                       # 나가기 전에는 복사한 쿠키도 된다
    assert c.get("/logout").status_code == 303
    r = stolen.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert login(web).get("/").status_code == 200                   # 다시 들어오면 새 쿠키로 된다


def test_logout_with_an_old_cookie_does_not_bump_again(web):
    c = login(web, agent_login(web))
    stolen = copy_of(c)
    c.get("/logout")
    v = sessions.version(web["con"], uid(web, agent_login(web)))
    stolen.get("/logout")                                           # 끝난 쿠키로 나가도 버전은 그대로다
    assert sessions.version(web["con"], uid(web, agent_login(web))) == v


def test_password_change_keeps_this_device_and_ends_others(web):
    con = web["con"]
    who = agent_login(web)
    here, there = login(web, who), login(web, who)
    stolen = copy_of(here)
    r = here.post("/me", data={"action": "password", "old": PW, "new": "changed-pass-5678", "new2": "changed-pass-5678"})
    assert r.status_code == 200 and "비밀번호를 바꿨습니다" in r.text
    assert here.get("/").status_code == 200
    assert there.get("/").status_code == 303 and stolen.get("/").status_code == 303
    con.execute("UPDATE user_account SET password_hash=? WHERE login_id=?", (auth.hash_password(PW), who))
    con.commit()


def test_suspend_and_reset_end_open_sessions(web):
    con = web["con"]
    who = agent_login(web)
    target = uid(web, who)
    admin = login(web)
    victim = login(web, who)
    assert admin.post("/org/accounts", data={"action": "suspend", "user_id": str(target)}).status_code == 303
    assert admin.post("/org/accounts", data={"action": "activate", "user_id": str(target)}).status_code == 303
    assert victim.get("/").status_code == 303                        # 다시 켜도 중지 전 쿠키는 끝났다
    victim = login(web, who)
    assert admin.post("/org/accounts", data={"action": "reset", "user_id": str(target)}).status_code == 200
    assert victim.get("/").status_code == 303
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0 WHERE user_id=?", (auth.hash_password(PW), target))
    con.commit()


def test_cookie_without_version_goes_to_login(web):
    """예전 쿠키처럼 sv 가 없거나 꼴이 다르면 맞지 않는다."""
    class Req:
        def __init__(self, s):
            self.session = s
    me = uid(web, "admin")
    v = sessions.version(web["con"], me)
    assert sessions.matches(Req({"uid": me, sessions.KEY: v}), web["con"], me)
    for s in ({"uid": me}, {"uid": me, sessions.KEY: str(v)}, {"uid": me, sessions.KEY: True}, {"uid": me, sessions.KEY: v + 1}):
        assert not sessions.matches(Req(s), web["con"], me), s
    assert not sessions.matches(Req({"uid": 999999, sessions.KEY: 0}), web["con"], 999999)


def test_session_version_column_is_added_to_an_old_db(tmp_path):
    con = sqlite3.connect(str(tmp_path / "old.db"))
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE user_account (user_id INTEGER PRIMARY KEY, login_id TEXT, status TEXT)")
    con.execute("INSERT INTO user_account VALUES (1, 'x', 'active')")
    con.commit()
    assert sessions.version(con, 1) == 0
    assert sessions.bump(con, 1) == 1
    assert schema.schema_version(con) == schema.SCHEMA_VERSION
    assert {"job", "schema_migration"} <= {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ---------------------------------------------------------------- 횟수 제한
def test_login_failures_are_limited_by_ip(web, monkeypatch):
    monkeypatch.setenv("APP_RATE_LOGIN_IP", "3/900")
    c = Client(web["app"], follow_redirects=False, client=("10.0.0.1", 1))
    for i in range(3):
        assert c.post("/login", data={"login_id": f"nobody{i}", "password": "x"}).status_code == 401
    r = c.post("/login", data={"login_id": "admin", "password": PW})     # 맞는 비밀번호도 막는다
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0 and "잠시 막았습니다" in r.text
    other = Client(web["app"], follow_redirects=False, client=("10.0.0.2", 1))
    assert other.post("/login", data={"login_id": "admin", "password": PW}).status_code == 303


def test_login_failures_are_limited_by_account_across_ips(web, monkeypatch):
    monkeypatch.setenv("APP_RATE_LOGIN_ID", "2/900")
    for i in range(2):
        c = Client(web["app"], follow_redirects=False, client=(f"10.1.0.{i}", 1))
        assert c.post("/login", data={"login_id": "ghost-account", "password": "x"}).status_code == 401
    c = Client(web["app"], follow_redirects=False, client=("10.1.0.9", 1))
    assert c.post("/login", data={"login_id": " Ghost-Account ", "password": "x"}).status_code == 429


def test_successful_logins_are_not_counted(web, monkeypatch):
    monkeypatch.setenv("APP_RATE_LOGIN_IP", "2/900")
    for _ in range(5):
        login(web, client=("10.2.0.1", 1))


def test_memory_store_window_slides():
    lim = ratelimit.Limiter(ratelimit.MemoryStore())
    os.environ["APP_RATE_LOGIN_ID"] = "2/60"
    try:
        lim.hit("login_id", "a", now=0)
        lim.hit("login_id", "a", now=10)
        assert lim.blocked("login_id", "a", now=20) == 41
        assert lim.blocked("login_id", "a", now=61) == 0               # 첫 실패가 창 밖으로 나갔다
        assert lim.blocked("login_id", "b", now=20) == 0
        os.environ["APP_RATE_LOGIN_ID"] = "0/0"                          # 끄기
        assert lim.blocked("login_id", "a", now=20) == 0
    finally:
        os.environ.pop("APP_RATE_LOGIN_ID", None)


def test_memory_store_drops_old_keys_when_full():
    s = ratelimit.MemoryStore(max_keys=3)
    for k in "abcd":
        s.hit(k, 60, now=1)
    assert s.count("a", 60, now=2) == 0 and s.count("d", 60, now=2) == 1


def test_landing_rule_limits_public_pages(web, monkeypatch):
    monkeypatch.setenv("APP_RATE_LANDING_IP", "2/60")
    c = Client(web["app"], follow_redirects=False, client=("10.3.0.1", 1))
    codes = [c.get("/invite").status_code for _ in range(3)]
    assert codes[:2] != [429, 429] and codes[2] == 429
    # 공개 화면을 새로 만들 때 다는 의존성
    app = FastAPI()

    @app.get("/landing", dependencies=[Depends(ratelimit.landing)])
    def landing():
        return {"ok": True}
    t = TestClient(app, client=("10.3.0.2", 1))
    assert [t.get("/landing").status_code for _ in range(3)] == [200, 200, 429]


# ---------------------------------------------------------------- 상태 점검
def test_healthz_reports_db_and_schema_only(web):
    c = TestClient(web["app"], follow_redirects=False)
    r = c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "db": "ok", "schema_version": schema.SCHEMA_VERSION, "schema_expected": schema.SCHEMA_VERSION}
    assert "set-cookie" not in r.headers and r.headers["cache-control"] == "no-store"
    text = r.text
    names = [x[0] for x in web["con"].execute("SELECT name FROM agent LIMIT 20")]
    assert not any(n in text for n in names) and str(web["path"]) not in text and "admin" not in text


def test_healthz_does_not_create_a_missing_db(tmp_path):
    p = tmp_path / "none.db"
    out = health.check(p)
    assert out["status"] == "down" and out["db"] == "missing" and not p.exists()


def test_healthz_says_degraded_without_schema_version(tmp_path):
    p = tmp_path / "bare.db"
    sqlite3.connect(str(p)).execute("CREATE TABLE x (a)").connection.commit()
    assert health.check(p) == {"status": "degraded", "db": "ok", "schema_version": None, "schema_expected": schema.SCHEMA_VERSION}


def test_healthz_is_open_in_both_lists():
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_context", db.ROOT / "scripts" / "check_context.py")
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    from tests.test_web_access import OPEN_ROUTES
    assert "/healthz" in cc.NON_MENU_ROUTES and "/healthz" in OPEN_ROUTES


# ---------------------------------------------------------------- 관측
def _observed_app():
    app = FastAPI()
    app.add_middleware(observe.ObserveMiddleware)
    app.add_middleware(SessionMiddleware, secret_key="t")

    @app.get("/boom/{item_id}")
    def boom(item_id: int):
        raise ValueError("가상인물 가상번호 1,234,000원")

    @app.get("/set")
    def set_uid(request: __import__("fastapi").Request):
        request.session["uid"] = 42
        return {}

    @app.get("/ok")
    def ok():
        return {}
    return app


def test_error_log_has_request_id_path_and_user_only(caplog):
    c = TestClient(_observed_app(), raise_server_exceptions=False)
    c.get("/set")
    with caplog.at_level(logging.INFO, logger="app.ops"):
        r = c.get("/boom/7?name=가상인물", headers={"X-Request-ID": "abc12345-req"})
    assert r.status_code == 500
    recs = [json.loads(x.getMessage()) for x in caplog.records if x.name == "app.ops"]
    err = [x for x in recs if x["event"] == "error"][0]
    assert err["rid"] == "abc12345-req" and err["path"] == "/boom/{item_id}" and err["uid"] == 42 and err["error"] == "ValueError"
    assert err["where"] == []                                       # 앱 밖(시험 파일)의 줄은 넣지 않는다
    text = " ".join(x.getMessage() for x in caplog.records)
    assert "가상인물" not in text and "가상번호" not in text and "1,234,000" not in text and "name=" not in text


def test_request_id_header_and_unsafe_id_replaced():
    c = TestClient(_observed_app())
    r = c.get("/ok", headers={"X-Request-ID": "bad id<script>"})
    rid = r.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{16}", rid)
    assert c.get("/ok", headers={"X-Request-ID": "good-id-1234"}).headers["x-request-id"] == "good-id-1234"


def test_slow_requests_are_logged(caplog, monkeypatch):
    monkeypatch.setenv("APP_SLOW_MS", "0")
    c = TestClient(_observed_app())
    with caplog.at_level(logging.INFO, logger="app.ops"):
        c.get("/ok?q=가상인물")
    slow = [json.loads(x.getMessage()) for x in caplog.records if x.name == "app.ops"]
    slow = [x for x in slow if x["event"] == "slow_request"]
    assert slow and slow[0]["path"] == "/ok" and slow[0]["status"] == 200 and "ms" in slow[0] and slow[0]["uid"] is None


def test_app_500_page_shows_request_id_but_not_the_error(web, caplog):
    from app.web.main import app
    from app.web.routes import jobs as job_routes

    def broken(*a, **k):
        raise RuntimeError("가상인물 비밀")
    c = login(web)
    real = job_routes.jobs.recent
    job_routes.jobs.recent = broken
    try:
        with caplog.at_level(logging.INFO, logger="app.ops"):
            r = TestClient(app, raise_server_exceptions=False, cookies=c.cookies).get("/data/jobs")
    finally:
        job_routes.jobs.recent = real
    rid = r.headers.get("x-request-id")
    assert r.status_code == 500 and f"요청 번호 {rid}" in r.text and "가상인물" not in r.text
    err = [json.loads(x.getMessage()) for x in caplog.records if x.name == "app.ops" and '"error"' in x.getMessage()][0]
    assert err["rid"] == rid and err["path"] == "/data/jobs" and err["uid"] == uid(web, "admin")
    assert any(w.startswith("app/web/routes/jobs.py:") for w in err["where"])
    assert "가상인물" not in caplog.text


# ---------------------------------------------------------------- 작업과 워커
@pytest.fixture()
def jdb(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONNECT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CONNECT_STATE_DB", str(tmp_path / "state.db"))
    for k in ("CONNECT_WATCH_DIR", "GDRIVE_SERVICE_ACCOUNT_FILE", "GDRIVE_FOLDER_ID"):
        monkeypatch.delenv(k, raising=False)
    con = db.init_db(tmp_path / "j.db")
    masters.seed(con)
    yield con
    con.close()


def test_enqueue_dedupes_and_checks_params(jdb):
    a = jobs.enqueue(jdb, "perf_run", {"ym": "202610"})
    assert jobs.enqueue(jdb, "perf_run", {"ym": "202610"}) == a
    assert jobs.enqueue(jdb, "perf_run", {"ym": "202609"}) != a
    for kind, params in (("perf_run", {"ym": "2026-10"}), ("nope", {}), ("ingest", {"area": "raw", "key": "../x", "kind": "contract"}),
                         ("ingest", {"area": "board", "key": "x", "kind": "contract"})):
        with pytest.raises(jobs.JobError):
            jobs.enqueue(jdb, kind, params)


def test_worker_runs_perf_and_rank_runs(jdb):
    jobs.enqueue(jdb, "perf_run", {"ym": "202610"})
    jobs.enqueue(jdb, "rank_run", {"ym": "202610"})
    done = jobs.drain(jdb, "w1")
    assert [j["status"] for j in done] == ["done", "done"]
    assert done[0]["result"]["run_id"] and jdb.execute("SELECT run_by FROM calc_run WHERE run_id=?", (done[0]["result"]["run_id"],)).fetchone()[0] == "worker"
    assert jobs.run_one(jdb, "w1") is None
    assert jobs.enqueue(jdb, "perf_run", {"ym": "202610"}) not in [j["job_id"] for j in done]   # 끝난 뒤에는 다시 넣을 수 있다


def test_failed_job_is_retried_with_backoff_then_fails(jdb, monkeypatch):
    calls = []

    def bad(con, p, job):
        calls.append(job["attempts"])
        raise KeyError("가상인물")
    monkeypatch.setitem(jobs.HANDLERS, "perf_run", bad)
    t0 = datetime(2026, 11, 15, 9, 0, 0)
    jid = jobs.enqueue(jdb, "perf_run", {"ym": "202610"}, max_attempts=2, now=t0)
    j = jobs.run_one(jdb, "w", now=t0)
    assert j["status"] == "queued" and j["error"] == "KeyError" and j["run_after"] == "2026-11-15 09:00:30"
    assert jobs.run_one(jdb, "w", now=t0 + timedelta(seconds=10)) is None        # 미룬 시각 전에는 꺼내지 않는다
    j = jobs.run_one(jdb, "w", now=t0 + timedelta(seconds=31))
    assert j["job_id"] == jid and j["status"] == "failed" and calls == [1, 2]
    assert "가상인물" not in json.dumps(dict(jdb.execute("SELECT * FROM job WHERE job_id=?", (jid,)).fetchone()), ensure_ascii=False)


def test_stale_running_job_goes_back_to_queue(jdb):
    t0 = datetime(2026, 11, 15, 9, 0, 0)
    jid = jobs.enqueue(jdb, "notify_sweep", now=t0)
    assert jobs.claim(jdb, "dead-worker", now=t0)["job_id"] == jid
    assert jobs.claim(jdb, "w2", now=t0 + timedelta(minutes=5)) is None          # 아직 잡혀 있다
    j = jobs.claim(jdb, "w2", now=t0 + timedelta(seconds=jobs.LEASE_SECONDS + 1))
    assert j["job_id"] == jid and j["worker"] == "w2" and j["attempts"] == 2


def test_refresh_months_include_later_saved_runs(jdb):
    from app.engine import performance
    for ym in ("202608", "202609", "202610"):
        performance.run(jdb, ym)
    assert jobs.refresh_months(jdb, "202609") == ["202609", "202610"]
    assert jobs.refresh_months(jdb, "202611") == ["202611"]
    assert jobs.refresh_months(jdb, None) == ["202610"]


def test_contract_ingest_queues_perf_and_rank_refresh(jdb, tmp_path):
    from app.connect.base import KINDS
    from app.loader import fake_eternal as fe
    assert "contract" in KINDS
    src, _ = fe.make_contract_file(tmp_path / "in" / "계약현황.xlsx", n=12, period_ym="202610")
    st = get_storage()
    key, _ = st.put_named("raw", "contract/202610", "contract.xlsx", pathlib.Path(src).read_bytes())
    jid = jobs.enqueue(jdb, "ingest", {"area": "raw", "key": key, "kind": "contract", "ym": "202610"}, by="admin")
    first = jobs.run_one(jdb, "w")
    assert first["job_id"] == jid and first["status"] == "done", first
    assert first["result"]["counts"].get("loaded") == 1 and len(first["result"]["follow"]) == 2
    follow = jdb.execute("SELECT kind, params, parent_id, status FROM job WHERE parent_id=? ORDER BY job_id", (jid,)).fetchall()
    assert [(r["kind"], json.loads(r["params"])["ym"], r["status"]) for r in follow] == [("perf_run", "202610", "queued"), ("rank_run", "202610", "queued")]
    assert [j["status"] for j in jobs.drain(jdb, "w")] == ["done", "done"]
    # 저장된 업적 run 이 계약에서 새로 센 값과 같다. 홈과 실적 화면이 어긋나지 않는다
    from app.engine import performance
    fresh = performance.compute_range(jdb, "202610", "202610")
    saved = {r["agent_id"]: r["net_count"] for r in jdb.execute(
        """SELECT s.agent_id, s.net_count FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.run_type='performance'
           WHERE r.period_ym='202610' AND s.period_type='month'""")}
    for aid, a in fresh.items():
        assert saved.get(aid, 0) == a.get("net_count", a["new_count"] - a["withdrawn_count"])
    text = json.dumps([dict(r) for r in jdb.execute("SELECT params, result FROM job")], ensure_ascii=False)
    assert "계약현황.xlsx" not in text                                             # 결과에 파일 이름을 넣지 않는다


def test_connect_jobs_without_settings_end_as_not_configured(jdb):
    jobs.enqueue(jdb, "connect_watch")
    jobs.enqueue(jdb, "gdrive")
    out = jobs.drain(jdb, "w")
    assert [j["status"] for j in out] == ["done", "done"]
    assert out[0]["result"] == {"status": "not_configured"} and out[1]["result"]["status"] == "not_configured"


def test_schedule_adds_daily_sweep_once(jdb):
    now = datetime(2026, 11, 15, 3, 0)
    assert len(jobs.schedule_due(jdb, now)) == 1
    assert jobs.schedule_due(jdb, now + timedelta(hours=5)) == []
    assert len(jobs.schedule_due(jdb, now + timedelta(days=1))) == 1


def test_worker_command_line(jdb, tmp_path, capsys):
    from app import worker
    path = str(tmp_path / "j.db")
    assert worker.main(["--db", path, "enqueue", "perf_run", "--ym", "202610"]) == 0
    assert worker.main(["enqueue", "perf_run", "--ym", "bad", "--db", path]) == 1
    assert worker.main(["--db", path, "--once"]) == 0
    capsys.readouterr()
    assert worker.main(["status", "--db", path]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"].get("done", 0) >= 2 and not out["counts"].get("queued")


def test_jobs_screen_main_only_and_needs_csrf(web):
    admin = login(web)
    assert admin.get("/data/jobs").status_code == 200
    assert admin.post("/data/jobs", data={"action": "refresh", "ym": "202610"}, with_csrf=False).status_code == 403
    r = admin.post("/data/jobs", data={"action": "refresh", "ym": "202610"})
    assert r.status_code == 303 and r.headers["location"].startswith("/data/jobs?added=")
    assert admin.post("/data/jobs", data={"action": "perf_run", "ym": "20261"}).status_code == 400
    assert admin.post("/data/jobs", data={"action": "drop_tables"}).status_code == 400
    kinds = {r[0] for r in web["con"].execute("SELECT kind FROM job WHERE status='queued' AND requested_by='admin'")}
    assert {"perf_run", "rank_run"} <= kinds
    agent = login(web, agent_login(web))
    assert agent.get("/data/jobs").status_code == 403
    assert agent.post("/data/jobs", data={"action": "notify_sweep"}).status_code == 403


# ---------------------------------------------------------------- 저장소
def test_local_storage_interface(tmp_path):
    st = LocalStorage({"raw": tmp_path / "raw", "board": tmp_path / "board", "uploads": tmp_path / "up"})
    d = st.put_hashed("board", b"hello")
    assert d == st.put_hashed("board", b"hello") and (tmp_path / "board" / d).read_bytes() == b"hello"
    key, new = st.put_named("raw", "contract/202610", "a.xlsx", b"abc")
    assert new and re.fullmatch(r"contract/202610/[0-9a-f]{16}_a\.xlsx", key)
    assert st.put_named("raw", "contract/202610", "other-name.xlsx", b"abc") == (key, False)      # 같은 내용은 한 번만
    assert st.get("raw", key) == b"abc" and st.exists("raw", key) and st.local_path("raw", key).is_file()
    assert st.keys("raw", "contract") == [key]
    for bad in ("../x", "/etc/passwd", "a/../../x", "", "C:/x"):
        with pytest.raises(StorageError):
            st.get("raw", bad)
    with pytest.raises(StorageError):
        st.delete("raw", key)
    with pytest.raises(StorageError):
        st.delete("board", d)
    with pytest.raises(StorageError):
        st.put_at("raw", "x", b"1")
    k = st.put_at("uploads", "staff_tok.xlsx", b"1")
    assert st.delete("uploads", k) and not st.exists("uploads", k)
    with pytest.raises(StorageError):
        st.root("nowhere")


def test_storage_keeps_old_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_BOARD_DIR", str(tmp_path / "board"))
    monkeypatch.setenv("APP_UPLOAD_DIR", str(tmp_path / "up"))
    st = get_storage()
    assert st.root("raw") == tmp_path / "data" / "raw" and st.root("board") == tmp_path / "board" and st.root("uploads") == tmp_path / "up"
    from app.connect.base import RawFile
    from app.connect.raw_store import store_raw
    from app.web import board
    from app.web.routes import staff
    raw = RawFile(name="계약 현황.xlsx", data=b"xyz", via="upload", kind="contract", period_ym="202610")
    path, rel = store_raw(raw)
    assert rel == f"raw/contract/202610/{raw.sha256[:16]}_계약_현황.xlsx" and path == tmp_path / "data" / rel
    meta = json.loads(path.with_name(path.name + ".meta.json").read_text(encoding="utf-8"))
    assert meta["sha256"] == raw.sha256 and meta["size"] == 3
    assert store_raw(raw) == (path, rel)
    digest = board.save_blob(b"attach")
    assert board.blob_path(digest) == tmp_path / "board" / digest and board.blob_path("0" * 64) is None
    assert staff.upload_dir() == tmp_path / "up"


def test_object_storage_is_not_chosen_by_itself(monkeypatch):
    monkeypatch.setenv("APP_STORAGE", "s3")
    with pytest.raises(StorageError):
        get_storage()


# ---------------------------------------------------------------- 백업과 되살리기
KEY_ENV = "BACKUP_KEY"


@pytest.fixture()
def bdb(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    p = tmp_path / "live.db"
    con = db.init_db(p)
    masters.seed(con)
    auth.seed_accounts(con, PW)
    jobs.enqueue(con, "notify_sweep")
    con.close()
    return p


def test_plain_backup_and_restore_check(bdb, tmp_path):
    m = backup.backup(bdb, tmp_path / "bk", encrypt=False)
    f = tmp_path / "bk" / m["file"]
    assert not m["encrypted"] and m["schema_version"] == schema.SCHEMA_VERSION and m["tables"]["agent"] > 0
    out = backup.verify(f)
    assert out["ok"] and out["rows"] == sum(m["tables"].values())
    side = (tmp_path / "bk" / (m["file"] + ".json")).read_text(encoding="utf-8")
    names = [r[0] for r in sqlite3.connect(str(bdb)).execute("SELECT name FROM agent")]
    assert not any(n in side for n in names)


def test_encrypted_backup_needs_the_key(bdb, tmp_path, monkeypatch):
    key = "test-only-backup-key-0001"
    monkeypatch.setenv(KEY_ENV, key)
    m = backup.backup(bdb, tmp_path / "bk")                    # 키가 있으면 기본으로 암호화한다
    f = tmp_path / "bk" / m["file"]
    assert m["encrypted"] and f.name.endswith(".db.enc")
    blob = f.read_bytes()
    assert blob.startswith(backup.MAGIC) and b"SQLite format 3" not in blob and key.encode() not in blob
    assert key not in (tmp_path / "bk" / (m["file"] + ".json")).read_text(encoding="utf-8")
    assert backup.verify(f)["ok"]
    monkeypatch.setenv(KEY_ENV, "another-wrong-key-0002")
    with pytest.raises(backup.BackupError):
        backup.verify(f)
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(backup.BackupError):
        backup.verify(f)
    monkeypatch.setenv(KEY_ENV, "short")
    with pytest.raises(backup.BackupError):
        backup.backup(bdb, tmp_path / "bk2")


def test_tampered_or_truncated_backup_is_refused(bdb, tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "test-only-backup-key-0001")
    m = backup.backup(bdb, tmp_path / "bk")
    f = tmp_path / "bk" / m["file"]
    good = f.read_bytes()
    bad = bytearray(good)
    bad[len(good) // 2] ^= 0x01
    f.write_bytes(bytes(bad))
    with pytest.raises(backup.BackupError):
        backup.verify(f)
    f.write_bytes(good[:-100])
    with pytest.raises(backup.BackupError):
        backup.verify(f)
    # 큰 DB 를 흉내 내 조각 여러 개를 끝에서 잘라도 막는다
    monkeypatch.setattr(backup, "CHUNK", 4096)
    m2 = backup.backup(bdb, tmp_path / "bk3")
    f2 = tmp_path / "bk3" / m2["file"]
    import struct
    data = f2.read_bytes()
    head = len(backup.MAGIC) + 24
    n = struct.unpack(">I", data[head:head + 4])[0]
    f2.write_bytes(data[:head + 4 + n])                         # 첫 조각만 남긴다
    with pytest.raises(backup.BackupError):
        backup.verify(f2)


def test_restore_keeps_the_old_file(bdb, tmp_path):
    m = backup.backup(bdb, tmp_path / "bk", encrypt=False)
    f = tmp_path / "bk" / m["file"]
    target = tmp_path / "restored.db"
    target.write_bytes(b"old")
    with pytest.raises(backup.BackupError):
        backup.restore(f, target)
    out = backup.restore(f, target, force=True)
    assert (tmp_path / out["moved"]).read_bytes() == b"old"
    con = sqlite3.connect(str(target))
    assert con.execute("SELECT COUNT(*) FROM user_account").fetchone()[0] == m["tables"]["user_account"]
    assert con.execute("SELECT kind FROM job").fetchone()[0] == "notify_sweep"


def test_backup_is_consistent_while_the_db_is_open(bdb, tmp_path):
    con = sqlite3.connect(str(bdb))
    con.execute("INSERT INTO audit_log(at, actor, action) VALUES('2026-11-15 00:00:00', 'x', 'y')")      # 커밋 전 쓰기는 백업에 들어가지 않는다
    m = backup.backup(bdb, tmp_path / "bk", encrypt=False)
    con.rollback()
    con.close()
    assert backup.verify(tmp_path / "bk" / m["file"])["ok"]


def test_backup_scripts(bdb, tmp_path, monkeypatch, capsys):
    import importlib.util

    def load(name):
        spec = importlib.util.spec_from_file_location(name, db.ROOT / "scripts" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv(KEY_ENV, "test-only-backup-key-0001")
    assert load("backup_db").main(["--db", str(bdb)]) == 0
    assert load("restore_check").main([]) == 0
    out = capsys.readouterr().out
    assert "되살리기 시험 통과" in out and "test-only-backup-key" not in out
    assert load("restore_check").main([str(tmp_path / "bk" / "nothing.db")]) == 1
    assert load("backup_db").main(["--db", str(tmp_path / "none.db")]) == 1


def test_backup_prune_only_when_asked(bdb, tmp_path):
    for i in range(3):
        backup.backup(bdb, tmp_path / "bk", encrypt=False, now=datetime(2026, 11, 15, 3, 0, i))
    (tmp_path / "bk" / "keep-me.txt").write_text("x")
    assert len(list((tmp_path / "bk").glob("app-*.db"))) == 3
    m = backup.backup(bdb, tmp_path / "bk", encrypt=False, now=datetime(2026, 11, 16, 3, 0, 0), keep=2)
    assert m["removed"] == ["app-20261115-030000.db", "app-20261115-030001.db"]
    assert sorted(p.name for p in (tmp_path / "bk").iterdir()) == ["app-20261115-030002.db", "app-20261115-030002.db.json", "app-20261116-030000.db",
                                                                    "app-20261116-030000.db.json", "keep-me.txt"]


# ---------------------------------------------------------------- SQL 호환 층
def test_translate_for_postgres():
    t = lambda s: sqlcompat.translate(s, sqlcompat.POSTGRES)
    assert sqlcompat.translate("SELECT ?", sqlcompat.SQLITE) == "SELECT ?"
    assert t("SELECT a FROM t WHERE b=? AND c LIKE '%?%'") == "SELECT a FROM t WHERE b=%s AND c LIKE '%%?%%'"
    assert t("INSERT OR IGNORE INTO r(a) VALUES(?)") == "INSERT INTO r(a) VALUES(%s) ON CONFLICT DO NOTHING"
    assert t("SELECT GROUP_CONCAT(c.rate, '|') x FROM c") == "SELECT string_agg(CAST(c.rate AS TEXT), '|') x FROM c"
    assert t("SELECT GROUP_CONCAT(name) FROM c") == "SELECT string_agg(CAST(name AS TEXT), ',') FROM c"
    assert t("WHERE insurer_id IS ? AND x IS NOT ? AND y IS NULL") == "WHERE insurer_id IS NOT DISTINCT FROM %s AND x IS DISTINCT FROM %s AND y IS NULL"
    assert t("SELECT 'it''s ?' , \"a?b\" FROM t WHERE x=?") == "SELECT 'it''s ?' , \"a?b\" FROM t WHERE x=%s"
    with pytest.raises(sqlcompat.NotPortable):
        t("INSERT OR REPLACE INTO r VALUES(?)")


def test_pg_schema_turns_every_integer_key_into_identity():
    out = sqlcompat.pg_schema()
    assert not re.search(r"\bINTEGER\s+PRIMARY\s+KEY\b(?! )", out.replace("INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", ""))
    assert out.count("GENERATED BY DEFAULT AS IDENTITY") == len(re.findall(r"\bINTEGER\s+PRIMARY\s+KEY\b", db.SCHEMA.read_text(encoding="utf-8")))


def test_row_reads_like_sqlite_row():
    r = sqlcompat.Row(["a", "b"], (1, "x"))
    assert r[0] == 1 and r["b"] == "x" and dict(r) == {"a": 1, "b": "x"} and r.keys() == ["a", "b"] and tuple(r) == (1, "x")


def test_scan_lists_sqlite_only_syntax():
    found = {name: n for name, n, _, _ in sqlcompat.scan()}
    assert found["INTEGER PRIMARY KEY 번호 칸"] > 50 and found["lastrowid"] > 0 and "INSERT OR REPLACE" in found


# ---------------------------------------------------------------- 초기 비밀번호
def test_temp_password_always_meets_the_password_rule():
    from app.web.invite import password_error
    for login_id in ("", "23", "ab", "A12345"):
        for _ in range(1500):
            pw = auth.temp_password(login_id)
            assert password_error(pw, pw, login_id) is None, "초기 비밀번호가 비밀번호 규칙에 맞지 않는다"
            assert len(pw) == auth.TEMP_PW_LEN and set(pw) <= set(auth.TEMP_PW_CHARS)


def test_issued_and_reset_passwords_meet_the_rule(jdb):
    from app.web.invite import password_error
    auth.seed_accounts(jdb, PW)
    u = jdb.execute("SELECT user_id, login_id FROM user_account WHERE agent_id IS NOT NULL LIMIT 1").fetchone()
    for _ in range(200):
        pw = auth.reset_password(jdb, u["user_id"])
        assert password_error(pw, pw, u["login_id"]) is None
    jdb.execute("DELETE FROM user_account WHERE user_id=?", (u["user_id"],))
    aid = jdb.execute("SELECT agent_id FROM agent WHERE status='active' AND agent_id NOT IN (SELECT agent_id FROM user_account WHERE agent_id IS NOT NULL) LIMIT 1").fetchone()
    login_id, pw = auth.issue_account(jdb, aid[0])
    assert password_error(pw, pw, login_id) is None
    jdb.rollback()


# ---------------------------------------------------------------- 정산서와 유지율 작업
@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """run_demo 샘플을 한 번 만든다. 시험마다 sdb 가 복사본을 쓴다."""
    import run_demo
    tmp = tmp_path_factory.mktemp("platform_settle")
    keys = ("CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "CONNECT_STATE_DB", "APP_UPLOAD_DIR")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"),
                      CONNECT_STATE_DB=str(tmp / "state.db"), APP_UPLOAD_DIR=str(tmp / "up"))
    try:
        run_demo.main(tmp / "demo.db", tmp / "raw")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    con = db.connect(tmp / "demo.db")
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    con.close()
    return {"tmp": tmp, "db": tmp / "demo.db", "raw": tmp / "raw"}


@pytest.fixture()
def sdb(demo, tmp_path, monkeypatch):
    import shutil
    path = tmp_path / "s.db"
    shutil.copy(demo["db"], path)
    monkeypatch.setenv("APP_DB", str(path))
    monkeypatch.setenv("CONNECT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONNECT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CONNECT_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("APP_UPLOAD_DIR", str(tmp_path / "up"))
    con = db.connect(path)
    cur = auth.current_ym(con)
    ym = [m for (m,) in con.execute("SELECT DISTINCT period_ym FROM calc_run WHERE run_type='commission' AND status='done' ORDER BY 1 DESC") if m != cur][0]
    yield {"con": con, "path": path, "ym": ym, "tmp": tmp_path, "raw": demo["raw"]}
    con.close()


def _statement_files(s):
    from app import samples
    files, _ = samples.write_statement_files(s["con"], s["ym"], s["tmp"] / "stmt_raw")
    return files


def _put(name, data):
    get_storage().put_at("uploads", f"settle_test/{name}", data)
    return f"settle_test/{name}"


def test_ingest_checks_statement_params(sdb):
    con = sdb["con"]
    key = _put("a.xlsx", b"x")
    for params in ({"area": "uploads", "key": key, "kind": "statement", "ym": sdb["ym"]},                    # 보험사 없음
                   {"area": "uploads", "key": key, "kind": "statement", "insurer": "SAM"},                    # 귀속월 없음
                   {"area": "uploads", "key": key, "kind": "persistency", "ym": "2026"},
                   {"area": "uploads", "key": key, "kind": "statement", "ym": sdb["ym"], "insurer": "../x"},
                   {"area": "uploads", "key": "settle_test/none.xlsx", "kind": "persistency", "ym": sdb["ym"]}):
        with pytest.raises(jobs.JobError):
            jobs.enqueue(con, "ingest", params)
    with pytest.raises(jobs.JobError):
        jobs.enqueue(con, "recon_run", {"ym": "20261"})
    jid = jobs.enqueue(con, "ingest", {"area": "uploads", "key": key, "kind": "persistency", "ym": sdb["ym"], "insurer": "SAM"})
    assert "insurer" not in jobs.get(con, jid)["params"]                   # 유지율에는 보험사를 두지 않는다
    assert jobs.KIND_KO["recon_run"] == "대사 다시 돌리기"


def test_statement_ingest_job_loads_and_queues_recon(sdb):
    con, ym = sdb["con"], sdb["ym"]
    f = _statement_files(sdb)[0]
    lines = lambda: con.execute("SELECT COUNT(*) FROM commission_statement_line WHERE period_ym=?", (ym,)).fetchone()[0]   # noqa: E731
    l0 = lines()
    key = _put("statement.xlsx", f["path"].read_bytes())
    jid = jobs.enqueue(con, "ingest", {"area": "uploads", "key": key, "kind": "statement", "ym": ym, "insurer": f["icode"], "by": "admin"}, by="admin")
    j = jobs.run_one(con, "w")
    assert j["job_id"] == jid and j["status"] == "done", j
    r = j["result"]
    assert r["status"] == "active" and r["rows"] > 0 and len(r["follow"]) == 1 and lines() > l0
    follow = con.execute("SELECT kind, params, parent_id, status, requested_by FROM job WHERE job_id=?", (r["follow"][0],)).fetchone()
    assert (follow["kind"], json.loads(follow["params"]), follow["parent_id"], follow["status"], follow["requested_by"]) == ("recon_run", {"ym": ym}, jid, "queued", "admin")
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='statement_load' AND actor='admin'").fetchone()[0] == 1
    rec = jobs.run_one(con, "w")
    assert rec["kind"] == "recon_run" and rec["status"] == "done" and rec["result"]["status"] == "done", rec
    run = con.execute("SELECT run_by, status FROM calc_run WHERE run_id=?", (rec["result"]["run_id"],)).fetchone()
    assert (run["run_by"], run["status"]) == ("admin", "done")
    assert con.execute("SELECT COUNT(*) FROM reconciliation_diff WHERE run_id=?", (rec["result"]["run_id"],)).fetchone()[0] == rec["result"]["diffs"]
    # 결과와 작업 표에는 금액도 파일 이름도 없다
    text = json.dumps([dict(x) for x in con.execute("SELECT result, error FROM job")], ensure_ascii=False)
    assert "statement.xlsx" not in text and "amount" not in text
    # 같은 파일을 다시 넣으면 다시 해도 같으므로 실패 없이 duplicate 로 끝난다
    jobs.enqueue(con, "ingest", {"area": "uploads", "key": key, "kind": "statement", "ym": ym, "insurer": f["icode"]})
    again = jobs.run_one(con, "w")
    assert again["status"] == "done" and again["result"] == {"status": "duplicate"} and jobs.run_one(con, "w") is None


def test_closed_month_is_neither_loaded_nor_reconciled(sdb):
    con, ym = sdb["con"], sdb["ym"]
    f = _statement_files(sdb)[1]
    key = _put("statement2.xlsx", f["path"].read_bytes())
    jobs.enqueue(con, "ingest", {"area": "uploads", "key": key, "kind": "statement", "ym": ym, "insurer": f["icode"]})
    con.execute("INSERT OR REPLACE INTO close_period(period_ym, status, closed_at, closed_by) VALUES(?,?,?,?)", (ym, "closed", db.now(), "admin"))
    con.commit()
    b0 = con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0]
    j = jobs.run_one(con, "w")                                              # 넣은 뒤에 마감했어도 처리할 때 다시 본다
    assert j["status"] == "done" and j["result"] == {"status": "closed"}
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == b0
    jobs.enqueue(con, "recon_run", {"ym": ym})
    assert jobs.run_one(con, "w")["result"] == {"status": "closed"}
    assert jobs.run_one(con, "w") is None                                   # 뒤따르는 대사가 없다
    # 정산서가 없는 달은 대사를 돌리지 않는다
    jobs.enqueue(con, "recon_run", {"ym": "202001"})
    assert jobs.run_one(con, "w")["result"] == {"status": "no_statement"}


def test_persistency_ingest_job_has_no_recon(sdb):
    import openpyxl
    con, ym = sdb["con"], sdb["ym"]
    src = sdb["raw"] / "internal" / ym / f"{ym}_유지율현황.xlsx"
    wb = openpyxl.load_workbook(src)
    wb.active["A1"] = f"{wb.active['A1'].value} 다시 받음"                    # 같은 내용의 다른 파일
    out = sdb["tmp"] / "p.xlsx"
    wb.save(out)
    key = _put("persistency.xlsx", out.read_bytes())
    b0 = con.execute("SELECT COUNT(*) FROM import_batch WHERE data_type='persistency'").fetchone()[0]
    jobs.enqueue(con, "ingest", {"area": "uploads", "key": key, "kind": "persistency", "ym": ym})
    j = jobs.run_one(con, "w")
    assert j["status"] == "done" and j["result"]["status"] == "active" and j["result"]["rows"] > 0 and j["result"]["follow"] == []
    assert con.execute("SELECT COUNT(*) FROM import_batch WHERE data_type='persistency'").fetchone()[0] == b0 + 1
    assert jobs.run_one(con, "w") is None


def test_worker_command_line_takes_statement_insurer(sdb, capsys):
    from app import worker
    f = _statement_files(sdb)[0]
    key = _put("cli.xlsx", f["path"].read_bytes())
    base = ["--db", str(sdb["path"]), "enqueue", "ingest", "--area", "uploads", "--key", key, "--kind", "statement", "--ym", sdb["ym"]]
    assert worker.main(base) == 1                                          # 보험사 코드가 없다
    assert worker.main([*base, "--insurer", f["icode"]]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["params"]["insurer"] == f["icode"] and out["status"] == "queued"


def _settle_client(sdb):
    from app.web.main import app
    c = Client(app, follow_redirects=False)
    assert c.post("/login", data={"login_id": "admin", "password": PW}).status_code == 303
    return c


def test_statement_screen_sends_big_files_to_jobs(sdb, monkeypatch):
    from app.web.routes import settle
    con, ym = sdb["con"], sdb["ym"]
    f = _statement_files(sdb)[0]
    data = f["path"].read_bytes()
    c = _settle_client(sdb)
    form = {"action": "preview", "kind": "statement", "insurer": f["icode"], "ym": ym}
    up = lambda: {"file": ("statement.xlsx", data, "application/octet-stream")}   # noqa: E731
    b0 = con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0]
    monkeypatch.setattr(settle, "MAX_BYTES", len(data) - 1)
    r = c.post("/data/statement", data=form, files=up())
    assert r.status_code == 400 and "너무 큽니다" in r.text
    monkeypatch.setattr(settle, "MAX_BYTES", len(data) + 10)
    monkeypatch.setattr(settle, "INLINE_BYTES", len(data) - 1)
    assert c.post("/data/statement", data=form, files=up(), with_csrf=False).status_code == 403
    r = c.post("/data/statement", data=form, files=up())
    assert r.status_code == 303 and r.headers["location"] == "/data/jobs?added=1"
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == b0           # 요청 안에서는 넣지 않았다
    job = jobs.recent(con, 1)[0]
    assert (job["kind"], job["status"], job["requested_by"]) == ("ingest", "queued", "admin")
    assert {k: job["params"][k] for k in ("area", "kind", "ym", "insurer")} == {"area": "uploads", "kind": "statement", "ym": ym, "insurer": f["icode"]}
    page = c.get("/data/jobs")
    assert page.status_code == 200 and "큰 적재" in page.text
    done = jobs.drain(con, "w")
    assert [j["kind"] for j in done] == ["ingest", "recon_run"] and all(j["status"] == "done" for j in done)
    assert done[0]["result"]["status"] == "active" and done[1]["result"]["status"] == "done"
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == b0 + 1
    # 관리자와 설계사는 정산서 올리기를 쓰지 못한다
    agent = con.execute("SELECT internal_code FROM agent WHERE agent_id NOT IN (SELECT agent_id FROM org_manager) AND status='active' LIMIT 1").fetchone()[0]
    a = Client(c.app, follow_redirects=False)
    assert a.post("/login", data={"login_id": agent, "password": PW}).status_code == 303
    assert a.post("/data/statement", data=form, files=up()).status_code == 403


def test_small_statement_loads_in_request_and_queues_recon(sdb):
    con, ym = sdb["con"], sdb["ym"]
    f = _statement_files(sdb)[0]
    c = _settle_client(sdb)
    r = c.post("/data/statement", data={"action": "preview", "kind": "statement", "insurer": f["icode"], "ym": ym},
               files={"file": ("statement.xlsx", f["path"].read_bytes(), "application/octet-stream")})
    assert r.status_code == 200 and "이대로 넣기" in r.text
    assert not con.execute("SELECT 1 FROM job WHERE kind='recon_run'").fetchone()
    assert c.post("/data/statement", data={"action": "load"}).status_code == 200
    rows = con.execute("SELECT params, status FROM job WHERE kind='recon_run'").fetchall()
    assert [(json.loads(x["params"]), x["status"]) for x in rows] == [({"ym": ym}, "queued")]
