"""연결 어댑터 시험. 임시 폴더와 임시 DB 만 쓰고, 바깥 서비스는 httpx.MockTransport 로 바꿔 끼운다.
실제 네트워크로 나가는 호출은 아래 autouse 장치가 막는다."""
import dataclasses
import json
import time

import httpx
import pytest

from app import db
from app.connect import __main__ as cli
from app.connect import base, card_site, retry
from app.connect.base import DUPLICATE, FAILED, LOADED, NO_HANDLER, NOT_CONFIGURED, OK, SKIPPED, RawFile
from app.connect.handlers import Registry, default_registry
from app.connect.pipeline import Pipeline
from app.connect.sources import FolderWatchSource, GoogleDriveSource, UploadSource, infer_kind, infer_ym
from app.connect.state import StateStore
from app.loader import fake_eternal as fe
from app.loader.incentive_plan import COLUMNS as PLAN_COLUMNS

YM = "202610"
BASE = "https://card.test"
KEY = "test-key"
FAKE_NAMES = ("가상인물", "시험 사람")       # 명함 신청자 이름 자리. 결과에 남으면 안 된다
FAKE_PHONE = "x-test-phone"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """바깥 환경과 실제 네트워크를 막는다."""
    for k in (card_site.ENV_URL, card_site.ENV_KEY, card_site.ENV_WRITE, "GDRIVE_SERVICE_ACCOUNT_FILE", "GDRIVE_FOLDER_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CONNECT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CONNECT_STATE_DB", str(tmp_path / "state.db"))   # 연결 상태가 data/demo.db 로 가지 않게 한다
    monkeypatch.setenv("CONNECT_DATA_DIR", str(tmp_path / "data"))

    def _no_network(self, request):
        raise AssertionError("시험이 실제 네트워크를 부르려 했습니다")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _no_network)


@pytest.fixture()
def env(tmp_path):
    con = db.init_db(tmp_path / "t.db")
    state = StateStore(tmp_path / "state")
    pipe = Pipeline(con, registry=default_registry(), data_dir=tmp_path / "data", state=state)
    path, _ = fe.make_contract_file(tmp_path / "in" / "계약현황.xlsx", n=15, period_ym=YM)
    yield {"con": con, "tmp": tmp_path, "state": state, "pipe": pipe, "contract": path}
    con.close()


def _no_sleep(_):
    return None


def _dump(path):
    """DB 의 모든 줄을 글로. 남으면 안 되는 값을 찾을 때 쓴다."""
    import sqlite3
    c = sqlite3.connect(str(path))
    try:
        return list(c.iterdump())
    finally:
        c.close()


# ------------------------------------------------------------------ 같은 파일과 원본 보관
def test_same_contract_file_twice_is_blocked(env):
    pipe, con = env["pipe"], env["con"]
    first = pipe.run(UploadSource.from_path(env["contract"], kind="contract", period_ym=YM))
    second = pipe.run(UploadSource.from_path(env["contract"], kind="contract", period_ym=YM))
    assert [r.status for r in first.results] == [LOADED]
    assert [r.status for r in second.results] == [DUPLICATE]
    assert con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 1
    row = con.execute("SELECT received_via, file_path, file_hash, status FROM import_batch").fetchone()
    assert row["received_via"] == "upload"
    assert row["file_path"] == first.results[0].raw_path and row["file_path"].startswith("raw/contract/202610/")
    assert row["file_hash"] == first.results[0].file_hash
    # 결과에는 금액 합계가 없다
    assert not any("sum" in k for k in first.results[0].detail)


def test_same_file_from_another_route_is_also_blocked(env, tmp_path):
    pipe = env["pipe"]
    assert pipe.run(UploadSource.from_path(env["contract"], kind="contract", period_ym=YM)).results[0].status == LOADED
    folder = tmp_path / "sync" / "contract"
    folder.mkdir(parents=True)
    (folder / "다시받은_202610.xlsx").write_bytes(env["contract"].read_bytes())
    run = pipe.run(FolderWatchSource(tmp_path / "sync", state=env["state"], min_age=0))
    assert [r.status for r in run.results] == [DUPLICATE]


def test_raw_is_kept_with_hash(env):
    res = env["pipe"].ingest(RawFile.from_path(env["contract"], kind="contract", period_ym=YM))
    stored = env["tmp"] / "data" / res.raw_path
    assert stored.exists() and stored.read_bytes() == env["contract"].read_bytes()
    assert stored.parent == env["tmp"] / "data" / "raw" / "contract" / YM
    assert stored.name.startswith(res.file_hash[:16] + "_")
    meta = json.loads(stored.with_name(stored.name + ".meta.json").read_text(encoding="utf-8"))
    assert meta["sha256"] == res.file_hash and meta["via"] == "upload" and meta["size"] == stored.stat().st_size
    # 같은 바이트는 한 번만 보관한다
    env["pipe"].ingest(RawFile.from_path(env["contract"], kind="contract", period_ym=YM))
    assert len([p for p in stored.parent.iterdir() if not p.name.endswith(".meta.json")]) == 1


def test_loader_duplicate_exception_becomes_duplicate(env):
    class Dup(Exception):
        pass

    def load(con, path, raw, rel):
        raise Dup("이미 있음")
    reg = Registry()
    reg.register("contract", load, already_loaded=None, duplicate_errors=(Dup,))
    pipe = Pipeline(env["con"], registry=reg, data_dir=env["tmp"] / "data", state=env["state"])
    assert pipe.ingest(RawFile(name="a.xlsx", data=b"abc", via="upload", kind="contract", period_ym=YM)).status == DUPLICATE


def test_plan_file_loads_once(env, tmp_path):
    path = tmp_path / "in" / "시상안_202610.xlsx"
    row = dict(zip(PLAN_COLUMNS, ["가상생명", "FC", "시책", "2026.10.01~2026.10.31", "월초 10만 이상", "월초 100%"]))
    fe.write_xlsx(path, PLAN_COLUMNS, [row], sheet="1.생보사")
    pipe = env["pipe"]
    first = pipe.ingest(RawFile.from_path(path, kind="plan", period_ym=YM))
    second = pipe.ingest(RawFile.from_path(path, kind="plan", period_ym=YM))
    assert first.status == LOADED and first.detail["lines"] == 1
    assert second.status == DUPLICATE
    assert env["con"].execute("SELECT COUNT(*) FROM incentive_plan_doc").fetchone()[0] == 1


# ------------------------------------------------------------------ 처리기 없음과 실패
def test_employee_without_handler_returns_no_handler_and_keeps_raw(env, tmp_path):
    path, _ = fe.make_employee_file(tmp_path / "in" / "사원현황.xlsx")
    res = env["pipe"].ingest(RawFile.from_path(path, kind="employee", period_ym=YM))
    assert res.status == NO_HANDLER and "처리기 없음" in res.reason
    assert (tmp_path / "data" / res.raw_path).exists()


def test_employee_handler_slot_is_used_when_registered(env, tmp_path):
    seen = []
    reg = default_registry()
    reg.register("employee", lambda con, path, raw, rel: seen.append(rel) or {"rows": 3}, needs_period=False)
    pipe = Pipeline(env["con"], registry=reg, data_dir=tmp_path / "data", state=env["state"])
    res = pipe.ingest(RawFile(name="사원현황.xlsx", data=b"x", via="upload", kind="employee"))
    assert res.status == LOADED and res.detail == {"rows": 3} and seen == [res.raw_path]


def test_handler_error_becomes_failed_and_rolls_back(env):
    con = env["con"]

    def load(con, path, raw, rel):
        con.execute("INSERT INTO insurer(code, name, sector) VALUES('ZZ', '되돌릴 보험사', 'life')")
        raise RuntimeError("적재 중 오류")
    reg = Registry()
    reg.register("contract", load)
    pipe = Pipeline(con, registry=reg, data_dir=env["tmp"] / "data", state=env["state"])
    res = pipe.ingest(RawFile(name="a.xlsx", data=b"abc", via="upload", kind="contract", period_ym=YM))
    assert res.status == FAILED and "적재 중 오류" in res.reason
    assert con.execute("SELECT COUNT(*) FROM insurer WHERE code='ZZ'").fetchone()[0] == 0


def test_wrong_header_file_fails_without_batch(env, tmp_path):
    path = tmp_path / "in" / "엉뚱한.xlsx"
    fe.write_xlsx(path, ["가", "나"], [{"가": 1, "나": 2}])
    res = env["pipe"].ingest(RawFile.from_path(path, kind="contract", period_ym=YM))
    assert res.status == FAILED and "머리글" in res.reason
    assert env["con"].execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 0


@pytest.mark.parametrize("kind, ym, status", [(None, YM, SKIPPED), ("unknown", YM, FAILED), ("contract", "2026-10", FAILED), ("contract", None, FAILED)])
def test_bad_kind_or_period_is_reported(env, kind, ym, status):
    res = env["pipe"].ingest(RawFile(name="a.xlsx", data=b"abc", via="upload", kind=kind, period_ym=ym))
    assert res.status == status and res.reason


# ------------------------------------------------------------------ 시간 제한과 재시도
def test_retry_until_success():
    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("끊김")
        return "ok"
    out = retry.call_with_retry(flaky, attempts=3, backoff=1.0, sleep=waits.append)
    assert out.status == OK and out.value == "ok" and out.attempts == 3 and waits == [1.0, 2.0]


def test_non_retryable_error_stops_at_once():
    calls = []

    def bad():
        calls.append(1)
        raise ValueError("잘못된 값")
    out = retry.call_with_retry(bad, attempts=5, sleep=_no_sleep)
    assert out.status == FAILED and out.attempts == 1 and len(calls) == 1 and "ValueError" in out.reason


def test_timeout_is_enforced_and_retried():
    started = time.monotonic()
    out = retry.call_with_retry(lambda: time.sleep(0.5), attempts=2, timeout=0.05, sleep=_no_sleep)
    assert out.status == FAILED and out.attempts == 2 and "CallTimeout" in out.reason
    assert time.monotonic() - started < 0.5


def test_not_configured_is_not_retried():
    calls = []

    def nc():
        calls.append(1)
        raise base.NotConfigured("설정 안 됨")
    out = retry.call_with_retry(nc, attempts=3, sleep=_no_sleep)
    assert out.status == NOT_CONFIGURED and len(calls) == 1


def test_source_failure_does_not_raise_and_is_recorded(env):
    class Broken:
        name = "broken"

        def fetch(self):
            raise ConnectionError("연결이 끊겼습니다")
    run = env["pipe"].run(Broken(), attempts=2, sleep=_no_sleep)
    assert run.status == FAILED and run.attempts == 2 and not run.ok
    st = env["state"].get("broken")
    assert st["last_status"] == FAILED and "연결이 끊겼습니다" in st["last_failure_reason"]
    assert st["consecutive_failures"] == 1 and st["last_success_at"] is None

    class Fine:
        name = "broken"

        def fetch(self):
            return []
    assert env["pipe"].run(Fine()).status == OK
    st = env["state"].get("broken")
    assert st["last_success_at"] and st["consecutive_failures"] == 0 and "연결이 끊겼습니다" in st["last_failure_reason"]


def test_slow_source_times_out(env):
    class Slow:
        name = "slow"

        def fetch(self):
            time.sleep(0.5)
            return []
    run = env["pipe"].run(Slow(), attempts=1, timeout=0.05)
    assert run.status == FAILED and "CallTimeout" in run.reason


def test_gdrive_without_key_is_not_configured(env):
    run = env["pipe"].run(GoogleDriveSource())
    assert run.status == NOT_CONFIGURED and "설정 안 됨" in run.reason and run.attempts == 1
    assert env["state"].get("gdrive")["last_status"] == NOT_CONFIGURED


# ------------------------------------------------------------------ 폴더 감시
def test_folder_watch_takes_only_new_files(env, tmp_path):
    root = tmp_path / "sync"
    (root / "contract").mkdir(parents=True)
    (root / "contract" / f"계약현황_{YM}.xlsx").write_bytes(env["contract"].read_bytes())
    (root / "contract" / "~$잠금.xlsx").write_bytes(b"lock")
    (root / "contract" / "받는중.xlsx.crdownload").write_bytes(b"part")
    (root / "메모.txt").write_bytes(b"memo")
    src = FolderWatchSource(root, state=env["state"], min_age=0)
    run = env["pipe"].run(src)
    by_name = {r.file_name: r for r in run.results}
    assert by_name[f"계약현황_{YM}.xlsx"].status == LOADED
    assert by_name[f"계약현황_{YM}.xlsx"].period_ym == YM
    assert by_name["메모.txt"].status == SKIPPED          # 종류를 알 수 없다
    assert len(run.results) == 2
    assert env["con"].execute("SELECT received_via FROM import_batch").fetchone()[0] == "folder"
    # 두 번째에는 끝난 파일을 다시 읽지 않는다. 종류를 모르는 파일만 다시 본다
    again = env["pipe"].run(FolderWatchSource(root, state=env["state"], min_age=0))
    assert [r.file_name for r in again.results] == ["메모.txt"]


def test_folder_watch_waits_for_settle_and_limits_size(env, tmp_path):
    root = tmp_path / "sync"
    root.mkdir()
    (root / f"계약_{YM}.xlsx").write_bytes(b"x" * 10)
    young = FolderWatchSource(root, state=env["state"], min_age=60)
    assert env["pipe"].run(young).results == []
    small = FolderWatchSource(root, state=env["state"], min_age=0, max_bytes=5)
    res = env["pipe"].run(small).results
    assert [r.status for r in res] == [SKIPPED] and "MB" in res[0].reason


def test_missing_watch_folder_fails_softly(env, tmp_path):
    run = env["pipe"].run(FolderWatchSource(tmp_path / "없음", state=env["state"]), attempts=2, sleep=_no_sleep)
    assert run.status == FAILED and run.attempts == 2


@pytest.mark.parametrize("text, ym", [("계약현황_202610.xlsx", "202610"), ("2026-09 시상안.xlsx", "202609"), ("2026년 3월.xlsx", "202603"),
                                      ("20261031_계약.xlsx", "202610"), ("파일.xlsx", None)])
def test_infer_ym(text, ym):
    assert infer_ym(text) == ym


def test_infer_kind():
    assert infer_kind("contract/a.xlsx") == "contract"
    assert infer_kind("월별/시상안_10월.xlsx") == "plan"
    assert infer_kind("사원현황.xlsx") == "employee"
    assert infer_kind("기타.xlsx") is None


# ------------------------------------------------------------------ 명령줄
def test_cli_ingest_and_status(env, tmp_path, capsys):
    env["con"].close()
    dbp = str(tmp_path / "t.db")
    assert cli.main(["ingest", str(env["contract"]), "--kind", "contract", "--ym", YM, "--db", dbp]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {LOADED: 1}
    assert cli.main(["ingest", str(env["contract"]), "--kind", "contract", "--ym", YM, "--db", dbp]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {DUPLICATE: 1}
    assert (tmp_path / "data" / "raw" / "contract" / YM).is_dir()
    assert cli.main(["status", "--db", dbp]) == 0
    assert json.loads(capsys.readouterr().out)["upload"]["last_success_at"]
    assert cli.main(["status", "--db", str(tmp_path / "없는.db")]) == 0      # DB 가 없으면 빈 목록. 파일을 만들지 않는다
    assert json.loads(capsys.readouterr().out) == {} and not (tmp_path / "없는.db").exists()
    assert cli.main(["gdrive"]) == 2
    env["con"] = db.connect(tmp_path / "t.db")


# ------------------------------------------------------------------ 명함 사이트
def _admin_payload():
    """명함 사이트 관리자 목록 모양의 가짜 자료. 이름과 연락처 자리에 뻔한 가짜 값을 둔다."""
    return {
        "pending": [{"slug": "card-a", "name": FAKE_NAMES[0], "phone": FAKE_PHONE, "title": "팀장", "at": "2026-11-10T01:00:00Z",
                     "status": "pending", "reason": "", "files": ["index.html", "og.jpg"], "bytes": 10,
                     "replaces": {"name": FAKE_NAMES[0], "at": "2026-10-01T00:00:00Z"}, "conflict": False}],
        "decided": [{"slug": "card-b", "name": FAKE_NAMES[1], "phone": FAKE_PHONE, "title": "", "at": "2026-11-01T00:00:00Z",
                     "status": "rejected", "reason": f"{FAKE_NAMES[1]} 사진 확인", "decidedAt": "2026-11-02T00:00:00Z"}],
        "live": [{"slug": "card-a", "name": FAKE_NAMES[0], "title": "팀장", "phone4": "5791", "at": "2026-10-01T00:00:00Z",
                  "files": ["index.html"]}],
    }


class FakeSite:
    """명함 사이트를 흉내 낸다. 받은 요청을 모두 남긴다."""

    def __init__(self, admin=None, status=None, fail=None):
        self.requests = []
        self.admin = admin or _admin_payload()
        self.status_map = status or {"card-a": {"slug": "card-a", "live": True, "liveAt": "2026-10-01T00:00:00Z", "status": "pending",
                                                "reason": "", "at": "2026-11-10T01:00:00Z", "decidedAt": None}}
        self.fail = list(fail or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            f = self.fail.pop(0)
            if isinstance(f, Exception):
                raise f
            return httpx.Response(f, json={"error": "x"})
        if request.url.path == "/api/status":
            slug = request.url.params.get("slug")
            return httpx.Response(200, json=self.status_map.get(slug, {"slug": slug, "live": False, "liveAt": None, "status": "none",
                                                                        "reason": "", "at": None, "decidedAt": None}))
        if request.url.path == "/api/admin/list":
            if request.headers.get("x-admin-key") != KEY:
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json=self.admin)
        return httpx.Response(404, json={"error": "not_found"})


def _client(site, tmp_path, key=KEY, **kw):
    return card_site.CardSiteClient(BASE, key, transport=httpx.MockTransport(site), state=StateStore(tmp_path / "state"),
                                    sleep=_no_sleep, **kw)


def test_card_status_public(tmp_path):
    site = FakeSite()
    c = _client(site, tmp_path, key="")
    out = c.status("card-a")
    assert out.status == OK
    assert out.value.live and out.value.status == "pending" and out.value.url == f"{BASE}/card-a/"
    assert "x-admin-key" not in site.requests[0].headers        # 공개 호출에는 키를 붙이지 않는다
    none = c.status("card-zz").value
    assert none.status == "none" and not none.live and none.url is None


def test_card_status_rejects_bad_slug_without_calling(tmp_path):
    site = FakeSite()
    assert _client(site, tmp_path).status("Bad Slug!").status == FAILED
    assert site.requests == []


def test_card_admin_without_key_is_blocked(tmp_path):
    site = FakeSite()
    c = _client(site, tmp_path, key="")
    for out in (c.admin_list(), c.suggest_links([(1, FAKE_NAMES[0])])):
        assert out.status == NOT_CONFIGURED and "CARD_ADMIN_KEY" in out.reason
    assert site.requests == []
    assert StateStore(tmp_path / "state").get(card_site.CONN_ADMIN)["last_status"] == NOT_CONFIGURED


def test_card_client_reads_env(monkeypatch, tmp_path):
    c = card_site.CardSiteClient(state=StateStore(tmp_path / "state"))
    assert not c.configured and c.status("card-a").status == NOT_CONFIGURED
    monkeypatch.setenv(card_site.ENV_URL, BASE)
    monkeypatch.setenv(card_site.ENV_KEY, KEY)
    c = card_site.CardSiteClient(state=StateStore(tmp_path / "state"))
    assert c.admin_configured and KEY not in repr(c)


def test_card_admin_list_drops_names_and_phones(tmp_path):
    site = FakeSite()
    out = _client(site, tmp_path).admin_list()
    assert out.status == OK
    assert site.requests[0].headers["x-admin-key"] == KEY
    entries = {e.slug: e for e in out.value}
    assert set(entries) == {"card-a", "card-b"}
    a, b = entries["card-a"], entries["card-b"]
    assert a.live and a.request_status == "pending" and a.live_at == "2026-10-01T00:00:00Z" and a.replaces_live
    assert not b.live and b.request_status == "rejected" and b.decided_at == "2026-11-02T00:00:00Z"
    assert [e.slug for e in card_site.pending_entries(out.value)] == ["card-a"]
    fields = {f.name for f in dataclasses.fields(card_site.CardEntry)}
    assert not fields & {"name", "phone", "phone4", "title", "reason"}
    dumped = json.dumps([dataclasses.asdict(e) for e in out.value], ensure_ascii=False) + repr(out)
    dumped += "\n".join(_dump(tmp_path / "state.db"))
    for secret in (*FAKE_NAMES, FAKE_PHONE, "5791", KEY):
        assert secret not in dumped


def test_card_suggest_links_returns_ids_only(tmp_path):
    site = FakeSite()
    agents = [(11, FAKE_NAMES[0]), (12, "다른사람"), (13, FAKE_NAMES[1].replace(" ", "")), (14, FAKE_NAMES[1])]
    out = _client(site, tmp_path).suggest_links(agents)
    assert out.status == OK
    got = {s.slug: s for s in out.value}
    assert got["card-a"].agent_ids == (11,) and got["card-a"].live
    assert got["card-b"].agent_ids == (13, 14) and got["card-b"].request_status == "rejected"
    dumped = json.dumps([dataclasses.asdict(s) for s in out.value], ensure_ascii=False) + repr(out)
    for secret in (*FAKE_NAMES, FAKE_PHONE):
        assert secret not in dumped


def test_card_client_sends_only_get(tmp_path):
    site = FakeSite()
    c = _client(site, tmp_path)
    c.status("card-a")
    c.admin_list()
    c.suggest_links([(1, FAKE_NAMES[0])])
    assert site.requests and {r.method for r in site.requests} == {"GET"}
    # D-44 로 승인과 반려만 생겼다. 내리기와 수료 코드 발급은 없다
    assert not any(hasattr(c, m) for m in ("remove", "remove_live", "issue_codes", "codes"))
    assert not c.write_enabled                                                     # 쓰기는 기본으로 꺼져 있다


def test_card_wrong_key_is_not_retried(tmp_path):
    site = FakeSite()
    out = _client(site, tmp_path, key="wrong-key").admin_list()
    assert out.status == FAILED and out.attempts == 1 and "401" in out.reason
    assert len(site.requests) == 1


def test_card_server_error_is_retried(tmp_path):
    site = FakeSite(fail=[503, 502])
    out = _client(site, tmp_path, attempts=3).status("card-a")
    assert out.status == OK and out.attempts == 3 and len(site.requests) == 3


def test_card_timeout_is_retried_then_fails_softly(tmp_path):
    site = FakeSite(fail=[httpx.ReadTimeout("느림"), httpx.ConnectTimeout("느림"), httpx.ReadTimeout("느림")])
    out = _client(site, tmp_path, attempts=3).admin_list()
    assert out.status == FAILED and out.attempts == 3 and "Timeout" in out.reason
    st = StateStore(tmp_path / "state").get(card_site.CONN_ADMIN)
    assert st["last_status"] == FAILED and st["consecutive_failures"] == 1


def test_card_client_has_http_timeout(tmp_path):
    seen = {}

    def handler(request):
        seen.update(request.extensions.get("timeout") or {})
        return httpx.Response(200, json={"slug": "card-a", "live": False, "status": "none"})
    c = card_site.CardSiteClient(BASE, "", timeout=3.0, transport=httpx.MockTransport(handler), state=StateStore(tmp_path / "state"))
    assert c.status("card-a").status == OK
    assert seen and all(v == 3.0 for v in seen.values())


def test_my_card_uses_link_store(tmp_path):
    c = _client(FakeSite(), tmp_path)
    links = card_site.MemoryLinkStore()
    assert card_site.my_card(c, links, 7).value is None
    links.link(7, "card-a", by="main-admin")
    assert card_site.my_card(c, links, 7).value.slug == "card-a"
    with pytest.raises(ValueError):
        links.link(8, "card-a", by="main-admin")


# ------------------------------------------------------------------ 연결 상태 표
def test_state_is_kept_in_db(env, tmp_path):
    con = env["con"]
    run = env["pipe"].run(UploadSource.from_path(env["contract"], kind="contract", period_ym=YM))
    assert run.ok
    row = con.execute("SELECT * FROM connection_state WHERE name='upload'").fetchone()
    assert row["last_status"] == OK and row["last_success_at"] and json.loads(row["detail_json"]) == {LOADED: 1}
    env["state"].record_failure("gdrive", "시간 초과")
    # 다른 연결, 다른 객체로 열어도 같은 값이 보인다
    again = StateStore(db_path=tmp_path / "t.db")
    assert again.get("upload")["last_success_at"] == row["last_success_at"]
    assert again.get("gdrive")["last_failure_reason"] == "시간 초과" and again.get("gdrive")["consecutive_failures"] == 1
    assert not (tmp_path / "state" / "connections.json").exists()         # 파일에는 더 쓰지 않는다
    assert not (tmp_path / "state.db").exists()                          # Pipeline 의 DB 에 붙었다


def test_state_watch_index_is_in_db(env, tmp_path):
    root = tmp_path / "sync" / "contract"
    root.mkdir(parents=True)
    (root / f"계약현황_{YM}.xlsx").write_bytes(env["contract"].read_bytes())
    src = FolderWatchSource(tmp_path / "sync", min_age=0)                 # 상태 기록을 주지 않아도 흐름의 DB 를 쓴다
    assert [r.status for r in env["pipe"].run(src).results] == [LOADED]
    rows = env["con"].execute("SELECT rel_path, status, file_hash FROM connection_watch").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(f"contract/계약현황_{YM}.xlsx", LOADED)] and len(rows[0][2]) == 64
    assert env["pipe"].run(FolderWatchSource(tmp_path / "sync", min_age=0)).results == []


def test_state_write_does_not_commit_outer_transaction(tmp_path):
    path = tmp_path / "o.db"
    db.init_db(path).close()
    con = db.connect(path)
    con.execute("INSERT INTO insurer(code, name, sector) VALUES('ZZ', '되돌릴 보험사', 'life')")
    StateStore(con=con).record_success("upload")
    con.rollback()
    assert con.execute("SELECT COUNT(*) FROM insurer WHERE code='ZZ'").fetchone()[0] == 0
    StateStore(con=con).record_success("upload")                          # 거래 밖에서는 바로 확정한다
    other = db.connect(path)
    assert other.execute("SELECT last_status FROM connection_state WHERE name='upload'").fetchone()[0] == OK
    other.close()
    con.close()


def test_state_creates_tables_in_old_db(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    sqlite3.connect(str(path)).close()                                    # 연결 표가 없는 DB
    st = StateStore(db_path=path)
    st.record("card_site.status", NOT_CONFIGURED, reason="설정 안 됨: 주소 없음")
    got = st.get("card_site.status")
    assert got["last_status"] == NOT_CONFIGURED and got["consecutive_failures"] == 0 and got["last_failure_at"] is None
    assert not StateStore(db_path=tmp_path / "없음.db", create=False).create
    with pytest.raises(FileNotFoundError):
        StateStore(db_path=tmp_path / "없음.db", create=False).all()


def _legacy_json(tmp_path):
    p = tmp_path / "state" / "connections.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"connections": {
        "upload": {"name": "upload", "last_status": "ok", "last_attempt_at": "2026-11-13 10:00:00", "last_success_at": "2026-11-13 10:00:00",
                   "last_failure_at": None, "last_failure_reason": "", "consecutive_failures": 0, "detail": {"loaded": 2}},
        "gdrive": {"name": "gdrive", "last_status": "failed", "last_attempt_at": "2026-11-13 11:00:00", "last_success_at": None,
                   "last_failure_at": "2026-11-13 11:00:00", "last_failure_reason": "시간 초과", "consecutive_failures": 3}},
        "watch": {"folder:/x": {"contract/a_202610.xlsx": {"size": 10, "mtime": 1.5, "hash": "h" * 64, "status": "loaded", "at": "2026-11-13 10:00:00"}}}}
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def test_state_migrates_from_json_once_and_keeps_file(tmp_path):
    legacy = _legacy_json(tmp_path)
    before = legacy.read_bytes()
    path = tmp_path / "m.db"
    db.init_db(path).close()
    st = StateStore(tmp_path / "state", db_path=path)
    got = st.all()                                                         # 처음 읽을 때 옮긴다
    assert set(got) == {"upload", "gdrive"}
    assert got["upload"]["detail"] == {"loaded": 2} and got["upload"]["migrated_from"] == "connections.json"
    assert got["gdrive"]["consecutive_failures"] == 3 and got["gdrive"]["last_failure_reason"] == "시간 초과"
    assert st.watch_index("folder:/x")["contract/a_202610.xlsx"] == {
        "size": 10, "mtime": 1.5, "hash": "h" * 64, "status": "loaded", "at": "2026-11-13 10:00:00"}
    # 표의 값이 새로워진 뒤 다시 옮겨도 덮어쓰지 않고 두 번 넣지 않는다
    st.record_success("gdrive")
    assert StateStore(tmp_path / "state", db_path=path).migrate_json() == {"connections": 0, "watch": 0, "file": "connections.json"}
    con = db.connect(path)
    assert con.execute("SELECT COUNT(*) FROM connection_state").fetchone()[0] == 2
    assert con.execute("SELECT consecutive_failures FROM connection_state WHERE name='gdrive'").fetchone()[0] == 0
    con.close()
    assert legacy.read_bytes() == before                                    # 원본 파일은 그대로 있다


def test_state_migrate_cli_and_broken_json(tmp_path, capsys):
    legacy = _legacy_json(tmp_path)
    path = tmp_path / "c.db"
    db.init_db(path).close()
    assert cli.main(["migrate-state", "--db", str(path), "--json", str(legacy)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["connections"] in (0, 2) and set(StateStore(db_path=path).all()) == {"upload", "gdrive"}
    broken = tmp_path / "broken" / "connections.json"
    broken.parent.mkdir()
    broken.write_text("{깨짐", encoding="utf-8")
    assert cli.main(["migrate-state", "--db", str(path), "--json", str(broken)]) == 1
    assert "error" in json.loads(capsys.readouterr().out) and broken.read_text(encoding="utf-8") == "{깨짐"
    assert StateStore(broken.parent, db_path=path).get("upload")                # 깨진 파일이 있어도 상태는 읽힌다


def test_schema_has_connect_tables(tmp_path):
    import sqlite3

    from app.connect import tables
    assert all("IF NOT EXISTS" in s for s in tables.statements())
    full = db.init_db(tmp_path / "s.db")
    bare = sqlite3.connect(":memory:")
    tables.ensure(bare)                                                     # 오래된 DB 에 덧붙이는 길도 같은 표를 만든다
    for t in tables.TABLES:
        cols = [tuple(r)[1:] for r in full.execute(f"PRAGMA table_info({t})")]
        assert cols and cols == [tuple(r)[1:] for r in bare.execute(f"PRAGMA table_info({t})")]
    full.close()
    bare.close()


# ------------------------------------------------------------------ 명함 연결 표
ADMIN, NOT_ADMIN = "admin", "fc-login"


@pytest.fixture()
def links_env(tmp_path):
    from app.connect.card_link import CardLinkTable
    con = db.init_db(tmp_path / "l.db")
    ids = []
    for i, name in enumerate((FAKE_NAMES[0], FAKE_NAMES[1], FAKE_NAMES[1], "다른사람")):
        ids.append(con.execute("INSERT INTO agent(internal_code, name) VALUES(?,?)", (f"T{i:03d}", name)).lastrowid)
    for login, main in ((ADMIN, 1), (NOT_ADMIN, 0)):
        con.execute("INSERT INTO user_account(login_id, password_hash, display_name, is_main_admin, created_at) VALUES(?,?,?,?,?)",
                    (login, "x", "시험 계정", main, db.now()))
    con.commit()
    site = FakeSite()
    yield {"con": con, "ids": ids, "links": CardLinkTable(con), "site": site, "client": _client(site, tmp_path), "path": tmp_path / "l.db"}
    con.close()


def _agents(env):
    return [(r[0], r[1]) for r in env["con"].execute("SELECT agent_id, name FROM agent")]


def test_card_link_suggestion_is_not_stored_until_confirmed(links_env):
    from app.connect.card_link import LinkError
    env = links_env
    con, links, a = env["con"], env["links"], env["ids"]
    sugg = env["client"].suggest_links(_agents(env)).value
    assert {s.slug: s.agent_ids for s in sugg} == {"card-a": (a[0],), "card-b": (a[1], a[2])}
    assert con.execute("SELECT COUNT(*) FROM card_link").fetchone()[0] == 0          # 제안만으로는 표에 없다
    opened = links.open_suggestions(sugg)
    assert [s.slug for s in opened] == ["card-a", "card-b"]
    b = next(s for s in opened if s.slug == "card-b")
    with pytest.raises(LinkError):
        links.confirm_suggestion(b, a[3], by=ADMIN)                                  # 후보가 아닌 설계사
    with pytest.raises(LinkError):
        links.confirm_suggestion(b, a[1], by=NOT_ADMIN)                              # 메인 관리자가 아니다
    assert con.execute("SELECT COUNT(*) FROM card_link").fetchone()[0] == 0
    row = links.confirm_suggestion(b, a[2], by=ADMIN)
    assert row["slug"] == "card-b" and row["agent_id"] == a[2] and row["linked_via"] == "suggest"
    assert row["linked_by"] == ADMIN and row["linked_at"]
    # 확정한 주소와 설계사는 다음 제안에서 빠진다
    again = links.open_suggestions(env["client"].suggest_links(_agents(env)).value)
    assert [(s.slug, s.agent_ids) for s in again] == [("card-a", (a[0],))]
    audit = con.execute("SELECT actor, action, target FROM audit_log WHERE action LIKE 'card_%'").fetchall()
    assert [tuple(r) for r in audit] == [(ADMIN, "card_link", f"agent:{a[2]}")]
    assert {r.method for r in env["site"].requests} == {"GET"}                       # 명함 사이트에는 읽기만 보냈다


def test_card_link_one_slug_one_agent(links_env):
    import sqlite3

    from app.connect.card_link import LinkError
    env = links_env
    con, links, a = env["con"], env["links"], env["ids"]
    links.link(a[0], "Card-A ", by=ADMIN)
    assert links.slug_for(a[0]) == "card-a" and links.agent_for("card-a") == a[0]
    assert links.link(a[0], "card-a", by=ADMIN)["id"] == links.get(a[0])["id"]     # 같은 짝은 그대로
    with pytest.raises(LinkError):
        links.link(a[1], "card-a", by=ADMIN)
    with pytest.raises(sqlite3.IntegrityError):                                       # 표의 색인도 막는다
        con.execute("INSERT INTO card_link(agent_id, slug, linked_by, linked_at) VALUES(?,?,?,?)", (a[1], "card-a", ADMIN, db.now()))
    con.rollback()
    for bad in ("Bad Slug!", "a", ""):
        with pytest.raises(LinkError):
            links.link(a[1], bad, by=ADMIN)
    with pytest.raises(LinkError):
        links.link(99999, "card-z", by=ADMIN)
    # 다른 주소로 바꾸면 이전 줄은 지우지 않고 닫는다. 닫힌 주소는 다른 설계사에게 이을 수 있다
    links.link(a[0], "card-c", by=ADMIN)
    links.link(a[1], "card-a", by=ADMIN)
    hist = links.history(a[0])
    assert [(h["slug"], bool(h["unlinked_at"])) for h in hist] == [("card-a", True), ("card-c", False)]
    assert hist[0]["unlink_reason"] == "다른 주소로 바꿈"
    with pytest.raises(LinkError):
        links.unlink(a[0], by=NOT_ADMIN)
    links.unlink(a[0], by=ADMIN, reason="해촉")
    assert links.slug_for(a[0]) is None and len(links.history(a[0])) == 2
    assert [r["agent_id"] for r in links.links()] == [a[1]] and links.links(agent_ids=[a[0]]) == []


def test_card_link_records_last_checked_status(links_env):
    env = links_env
    links, a, client = env["links"], env["ids"], env["client"]
    links.link(a[0], "card-a", by=ADMIN)
    links.link(a[1], "card-b", by=ADMIN)
    out = card_site.my_card(client, links, a[0])
    assert out.value.slug == "card-a"
    row = links.get(a[0])
    assert row["last_status"] == "pending" and row["last_live"] is True and row["last_live_at"] == "2026-10-01T00:00:00Z" and row["last_checked_at"]
    assert links.record_entries(client.admin_list().value) == 2
    assert links.get(a[1])["last_status"] == "rejected" and links.get(a[1])["last_live"] is False
    assert links.refresh(client) == {"checked": 2, "failed": 0}
    assert links.get(a[1])["last_status"] == "none"                                # 가짜 사이트의 공개 상태 응답
    assert {r.method for r in env["site"].requests} == {"GET"}


def test_card_link_keeps_no_name_or_phone(links_env):
    env = links_env
    con, links, a, client = env["con"], env["links"], env["ids"], env["client"]
    cols = {r[1] for r in con.execute("PRAGMA table_info(card_link)")}
    assert not cols & {"name", "phone", "phone4", "title", "contact", "reason"}
    for s in links.open_suggestions(client.suggest_links(_agents(env)).value):
        links.confirm_suggestion(s, s.agent_ids[0], by=ADMIN)
    links.record_entries(client.admin_list().value)
    links.refresh(client)
    card_site.my_card(client, links, a[0])
    assert con.execute("SELECT COUNT(*) FROM card_link").fetchone()[0] == 2
    con.commit()
    dump = _dump(env["path"])
    # 연락처와 끝 네 자리, 관리자 키는 DB 어디에도 없다
    for secret in (FAKE_PHONE, "5791", KEY):
        assert not any(secret in line for line in dump)
    # 이름은 사원 표에만 있고 연결 표와 변경 기록에는 없다
    mine = [line for line in dump if any(t in line for t in ('"card_link"', '"audit_log"', '"connection_state"', '"connection_watch"'))]
    assert mine and not any(n in line for line in mine for n in FAKE_NAMES)
    out = json.dumps([links.get(x) for x in a], ensure_ascii=False)
    assert not any(n in out for n in (*FAKE_NAMES, FAKE_PHONE))


# ------------------------------------------------------------------ 명함 승인과 반려 (D-44)
MAIN, MGR, DEP, FC, OUT_MGR = "admin", "mgr-login", "dep-login", "fc-only", "gone-mgr"
# 개인정보 꼴의 가짜 값. 컨텍스트 점검이 원문을 찾지 않도록 실행할 때 조각을 이어 만든다
FAKE_RRN = "-".join(("000101", "3" + "0" * 6))
FAKE_MOBILE = "-".join(("010", "0" * 4, "0" * 4))


class WriteSite(FakeSite):
    """승인과 반려를 받는 가짜 명함 사이트. 사이트처럼 대기 중이 아니면 404 no_pending 이다."""

    def __init__(self, pending=("card-a", "card-c", "card-x"), post_fail=None, stale=False, **kw):
        super().__init__(**kw)
        self.pending = {s: f"2026-11-1{i}T01:00:00Z" for i, s in enumerate(pending)}
        self.decided = {}
        self.post_fail = list(post_fail or [])
        self.stale = stale                          # 참이면 공개 상태가 늦게 바뀌는 사이트를 흉내 낸다
        self.bodies = []

    def __call__(self, request):
        path = request.url.path
        if request.method == "POST" and path.startswith("/api/admin/"):
            self.requests.append(request)
            if request.headers.get("x-admin-key") != KEY:
                return httpx.Response(401, json={"error": "unauthorized"})
            body = json.loads(request.content or b"{}")
            self.bodies.append(body)
            op, slug = path.rsplit("/", 1)[1], body.get("slug")
            if self.post_fail:
                f = self.post_fail.pop(0)
                if isinstance(f, tuple):                  # (예외, 서버가 처리했는가)
                    exc, applied = f
                    if applied:
                        self._apply(op, slug)
                    raise exc
                return httpx.Response(f, json={"error": "server_error"})
            if slug not in self.pending:
                return httpx.Response(404, json={"error": "no_pending", "message": "대기 중인 요청이 없습니다."})
            at = self._apply(op, slug)
            return httpx.Response(200, json={"ok": True, "slug": slug, "at": at} if op == "approve" else {"ok": True})
        if request.method == "GET" and path == "/api/status":
            self.requests.append(request)
            slug = request.url.params.get("slug")
            if slug in self.pending or (self.stale and slug in self.decided):
                at = self.pending.get(slug) or self.decided[slug][1]
                return httpx.Response(200, json={"slug": slug, "live": False, "liveAt": None, "status": "pending", "reason": "",
                                                 "at": at, "decidedAt": None})
            if slug in self.decided:
                st, at = self.decided[slug]
                return httpx.Response(200, json={"slug": slug, "live": st == "approved", "liveAt": "2026-11-14T00:00:00Z" if st == "approved" else None,
                                                 "status": st, "reason": "", "at": at, "decidedAt": "2026-11-14T00:00:00Z"})
            return httpx.Response(200, json={"slug": slug, "live": False, "liveAt": None, "status": "none", "reason": "", "at": None, "decidedAt": None})
        if request.method == "GET" and path == "/api/admin/list":
            self.requests.append(request)
            if request.headers.get("x-admin-key") != KEY:
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={
                "pending": [{"slug": s, "name": FAKE_NAMES[0], "phone": FAKE_MOBILE, "title": "", "at": at, "status": "pending",
                             "reason": "", "files": ["index.html"]} for s, at in self.pending.items()],
                "decided": [], "live": [{"slug": "card-a", "name": FAKE_NAMES[0], "phone4": "5791", "at": "2026-10-01T00:00:00Z"}]})
        return super().__call__(request)

    def _apply(self, op, slug):
        at = self.pending.pop(slug, None)
        if at:
            self.decided[slug] = ("approved" if op == "approve" else "rejected", at)
        return "2026-11-14T00:00:00Z"

    def posts(self):
        return [r for r in self.requests if r.method == "POST"]


@pytest.fixture()
def decide_env(tmp_path):
    """조직 둘. 관리자는 조직 1의 팀장이고, 대리는 조직 1의 대리다. 설계사 a1 은 범위 안, a2 는 범위 밖이다."""
    from app.connect.card_link import CardLinkTable
    con = db.init_db(tmp_path / "d.db")
    o1 = con.execute("INSERT INTO org_unit(name, level) VALUES('시험 팀 1', 'team')").lastrowid
    o2 = con.execute("INSERT INTO org_unit(name, level) VALUES('시험 팀 2', 'team')").lastrowid
    ids = [con.execute("INSERT INTO agent(internal_code, name) VALUES(?,?)", (f"D{i:03d}", f"{FAKE_NAMES[0]}{i}")).lastrowid for i in range(6)]
    a_mgr, a1, a2, a_dep, a_fc, a_gone = ids
    con.execute("INSERT INTO org_manager(org_id, agent_id, role, effective_from) VALUES(?,?,?,?)", (o1, a_mgr, "team_head", "202401"))
    con.execute("INSERT INTO org_manager(org_id, agent_id, role, effective_from, effective_to) VALUES(?,?,?,?,?)",
                (o2, a_gone, "team_head", "202401", "202402"))                     # 지난 조직장
    con.execute("INSERT INTO org_deputy(org_id, agent_id, date_from, date_to, created_by, created_at) VALUES(?,?,?,?,?,?)",
                (o1, a_dep, "2000-01-01", "2999-12-31", MAIN, db.now()))
    for login, agent_id, main in ((MAIN, None, 1), (MGR, a_mgr, 0), (DEP, a_dep, 0), (FC, a_fc, 0), (OUT_MGR, a_gone, 0)):
        con.execute("INSERT INTO user_account(login_id, password_hash, agent_id, display_name, is_main_admin, created_at) VALUES(?,?,?,?,?,?)",
                    (login, "x", agent_id, "시험 계정", main, db.now()))
    con.commit()
    links = CardLinkTable(con)
    links.link(a1, "card-a", by=MAIN)
    links.link(a2, "card-c", by=MAIN)                                               # card-x 는 이어지지 않았다
    site = WriteSite()
    client = _client(site, tmp_path, write=True)
    scope = [a_mgr, a1, a_dep]                                                       # guard 가 관리자에게 주는 목록
    yield {"con": con, "links": links, "site": site, "client": client, "a1": a1, "a2": a2, "scope": scope,
           "path": tmp_path / "d.db", "tmp": tmp_path}
    con.close()


def _decide(env, slug, action, actor, *, is_main=False, reason="", client=None):
    from app.connect.card_decide import decide_card
    return decide_card(env["con"], client or env["client"], slug=slug, action=action, actor=actor,
                       scope_agent_ids=[] if is_main else env["scope"], is_main=is_main, reason=reason, link_table=env["links"])


def _card_audit(con):
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in
            con.execute("SELECT actor, action, target, after_json FROM audit_log WHERE action IN ('card_approve','card_reject') ORDER BY id")]


def test_card_decide_manager_approves_in_scope(decide_env):
    from app.connect.card_decide import DECIDE_KO
    env = decide_env
    out = _decide(env, "card-a", "approve", MGR)
    assert out.status == OK and out.value.agent_id == env["a1"] and out.value.decided_at == "2026-11-14T00:00:00Z"
    assert out.value.to_dict()["result_ko"] == DECIDE_KO[OK]
    posts = env["site"].posts()
    assert [(r.url.path, json.loads(r.content)) for r in posts] == [("/api/admin/approve", {"slug": "card-a"})]
    assert posts[0].headers["x-admin-key"] == KEY
    row = env["links"].get(env["a1"])
    assert row["last_status"] == "approved" and row["last_live"] is True and row["last_checked_at"]
    assert _card_audit(env["con"]) == [(MGR, "card_approve", f"agent:{env['a1']}",
                                        {"slug": "card-a", "result": OK, "requested_at": "2026-11-10T01:00:00Z", "scope": "manager"})]
    assert StateStore(env["tmp"] / "state").get(card_site.CONN_WRITE)["last_status"] == OK


def test_card_decide_deputy_rejects_in_scope_with_trimmed_reason(decide_env):
    env = decide_env
    long_reason = "  사진이 흐립니다 " + "가" * 300 + "  "
    out = _decide(env, "card-a", "reject", DEP, reason=long_reason)
    assert out.status == OK and out.value.reason_len == 200
    body = env["site"].bodies[-1]
    assert body["slug"] == "card-a" and len(body["reason"]) == 200 and body["reason"].startswith("사진이 흐립니다")
    assert env["links"].get(env["a1"])["last_status"] == "rejected"
    (actor, action, target, after), = _card_audit(env["con"])
    assert (actor, action, target, after["result"], after["reason_len"]) == (DEP, "card_reject", f"agent:{env['a1']}", OK, 200)


@pytest.mark.parametrize("slug, actor, why", [
    ("card-c", MGR, "범위 밖"),                     # 이어진 설계사가 범위 밖
    ("card-x", MGR, "메인 관리자만"),               # 이어지지 않은 주소
    ("card-x", DEP, "메인 관리자만"),
    ("card-a", FC, "조직장이나 대리가 아니라"),       # 조직장도 대리도 아닌 계정
    ("card-a", OUT_MGR, "조직장이나 대리가 아니라"),  # 지난 조직장
    ("card-a", "no-such-login", "활성 계정이 아니라"),
])
def test_card_decide_denied_without_sending(decide_env, slug, actor, why):
    env = decide_env
    for action in ("approve", "reject"):
        out = _decide(env, slug, action, actor, reason="사진 확인")
        assert out.status == "denied" and why in out.reason
    assert env["site"].requests == []                                                # 사이트에는 아무것도 보내지 않았다
    audit = _card_audit(env["con"])
    assert len(audit) == 2 and all(a[3]["result"] == "denied" for a in audit)


def test_card_decide_suspended_account_and_fake_main_are_denied(decide_env):
    env = decide_env
    env["con"].execute("UPDATE user_account SET status='suspended' WHERE login_id=?", (MGR,))
    env["con"].commit()
    assert _decide(env, "card-a", "approve", MGR).status == "denied"
    assert _decide(env, "card-x", "approve", DEP, is_main=True).status == "denied"     # 화면이 메인이라 해도 DB 가 아니면 막는다
    assert env["site"].requests == []


def test_card_decide_main_admin_handles_any_slug(decide_env):
    env = decide_env
    assert _decide(env, "card-x", "approve", MAIN, is_main=True).status == OK          # 이어지지 않은 주소
    assert _decide(env, "card-c", "reject", MAIN, is_main=True, reason="다시 올려 주세요").status == OK   # 누구의 범위든
    assert [r.url.path for r in env["site"].posts()] == ["/api/admin/approve", "/api/admin/reject"]
    audit = _card_audit(env["con"])
    assert [(a[2], a[3]["scope"]) for a in audit] == [("agent:none", "main"), (f"agent:{env['a2']}", "main")]


def test_card_decide_write_switch_off_sends_nothing(decide_env, monkeypatch):
    env = decide_env
    off = _client(env["site"], env["tmp"])                                             # 환경 변수가 없으면 꺼짐
    assert not off.write_enabled
    monkeypatch.setenv(card_site.ENV_WRITE, "true")                                    # 1 이 아니면 꺼짐
    assert not _client(env["site"], env["tmp"]).write_enabled
    out = _decide(env, "card-a", "approve", MGR, client=off)
    assert out.status == card_site.WRITE_OFF and "쓰기가 꺼져 있습니다" in out.reason
    assert off.approve("card-a").status == card_site.WRITE_OFF and off.reject("card-a", "x").status == card_site.WRITE_OFF
    assert env["site"].requests == []
    st = StateStore(env["tmp"] / "state").get(card_site.CONN_WRITE)
    assert st["last_status"] == NOT_CONFIGURED and card_site.ENV_WRITE in st["last_failure_reason"]
    monkeypatch.setenv(card_site.ENV_WRITE, "1")
    assert _client(env["site"], env["tmp"]).write_enabled
    assert _card_audit(env["con"])[0][3]["result"] == card_site.WRITE_OFF


def test_card_decide_no_pending(decide_env):
    env = decide_env
    env["site"].pending.pop("card-a")                                                 # 사이트에서 이미 처리됨
    out = _decide(env, "card-a", "approve", MGR)
    assert out.status == card_site.NO_PENDING and env["site"].posts() == []           # 미리 읽은 상태로 보내지 않는다
    # 상태 읽기와 보내기 사이에 처리되어 사이트가 404 no_pending 을 돌려주는 경우
    raw = _client(env["site"], env["tmp"], write=True).approve("card-a")
    assert raw.status == card_site.NO_PENDING and len(env["site"].posts()) == 1
    assert StateStore(env["tmp"] / "state").get(card_site.CONN_WRITE)["last_status"] == OK   # 연결은 정상이다


def test_card_decide_never_sends_twice(decide_env):
    env = decide_env
    site = WriteSite(stale=True)                      # 처리한 뒤에도 공개 상태가 한동안 대기로 보이는 사이트
    env["site"] = site
    client = _client(site, env["tmp"], write=True)
    assert _decide(env, "card-a", "approve", MGR, client=client).status == OK
    again = _decide(env, "card-a", "approve", MGR, client=client)                     # 두 번 누름
    other = _decide(env, "card-a", "reject", DEP, client=client, reason="x")          # 다른 관리자가 같은 신청을 반려
    assert again.status == "already_done" and other.status == "already_done"
    assert len(site.posts()) == 1


def test_card_write_is_not_retried(decide_env):
    env = decide_env
    # 5xx: 다시 보내지 않고 상태를 한 번 읽는다. 아직 대기면 실패로 돌려준다
    site = WriteSite(post_fail=[503, 503, 503])
    out = _client(site, env["tmp"], write=True, attempts=3).approve("card-a")
    assert out.status == FAILED and "아직 승인 대기" in out.reason and len(site.posts()) == 1
    # 응답을 못 받았지만 서버는 처리한 경우: 상태로 반영을 확인한다
    site = WriteSite(post_fail=[(httpx.ReadTimeout("느림"), True)])
    out = _client(site, env["tmp"], write=True).reject("card-a", "x")
    assert out.status == OK and out.value.confirmed_by_status and len(site.posts()) == 1
    # 연결 자체가 안 된 경우: 서버에 닿지 않았으니 처리되지 않았고 다시 보내지 않는다
    site = WriteSite(post_fail=[(httpx.ConnectError("끊김"), False)])
    out = _client(site, env["tmp"], write=True).approve("card-a")
    assert out.status == FAILED and "닿지 않아" in out.reason and len(site.posts()) == 1
    assert StateStore(env["tmp"] / "state").get(card_site.CONN_WRITE)["last_status"] == FAILED
    # 키가 틀리면 한 번만
    site = WriteSite()
    out = _client(site, env["tmp"], key="wrong-key", write=True).approve("card-a")
    assert out.status == FAILED and "401" in out.reason and len(site.posts()) == 1


def test_card_decide_in_flight_blocks_second_call(decide_env):
    from app.connect import card_decide
    env = decide_env
    with card_decide._LOCK:
        card_decide._INFLIGHT.add("card-a")
    try:
        out = _decide(env, "card-a", "approve", MGR)
    finally:
        card_decide._INFLIGHT.discard("card-a")
    assert out.status == "in_flight" and env["site"].posts() == []


@pytest.mark.parametrize("bad", [FAKE_MOBILE, "연락 " + FAKE_MOBILE.replace("-", "") + " 주세요", "+82 " + FAKE_MOBILE[1:],
                                 "02-000-0000", FAKE_RRN, FAKE_RRN.replace("-", "")])
def test_card_reject_reason_with_pii_is_refused(decide_env, bad):
    env = decide_env
    out = _decide(env, "card-a", "reject", MGR, reason=f"사유 {bad}")
    assert out.status == "denied" and "전화번호나 주민번호" in out.reason
    assert env["site"].requests == []
    dumped = json.dumps(_card_audit(env["con"]), ensure_ascii=False) + repr(out)
    assert bad not in dumped


def test_card_reject_reason_keeps_ordinary_numbers():
    from app.connect.card_decide import clean_reason
    for ok_text in ("2026-11-14 사진 다시", "3번째 사진", "제목 30자 초과"):
        assert clean_reason(ok_text) == (ok_text, None)


def test_card_decide_audit_has_no_name_phone_or_reason(decide_env):
    env = decide_env
    secret_reason = "사진 속 배경이 어둡습니다 확인부탁"
    _decide(env, "card-a", "reject", MGR, reason=secret_reason)
    _decide(env, "card-c", "approve", MGR)
    _decide(env, "card-x", "approve", MAIN, is_main=True)
    _decide(env, "card-a", "reject", MGR, reason=f"{FAKE_NAMES[1]} {FAKE_MOBILE}")
    env["con"].commit()
    lines = [line for line in _dump(env["path"]) if '"audit_log"' in line or '"card_link"' in line or '"connection_state"' in line]
    lines += [line for line in _dump(env["tmp"] / "state.db") if '"connection_state"' in line]
    assert any("card_reject" in line for line in lines)
    for secret in (secret_reason, *FAKE_NAMES, FAKE_MOBILE, KEY):
        assert not any(secret in line for line in lines)


def test_pending_for_scope_filters_and_hides_phone(decide_env):
    from app.connect.card_decide import PendingCard, pending_for_scope
    env = decide_env
    mgr = pending_for_scope(env["client"], env["links"], env["scope"], is_main=False)
    assert mgr.status == OK
    assert [(p.slug, p.agent_id) for p in mgr.value["linked"]] == [("card-a", env["a1"])] and mgr.value["unlinked"] == []
    assert mgr.value["linked"][0].live and mgr.value["linked"][0].live_url == f"{BASE}/card-a/"
    main = pending_for_scope(env["client"], env["links"], [], is_main=True)
    assert sorted(p.slug for p in main.value["linked"]) == ["card-a", "card-c"]
    assert [(p.slug, p.agent_id) for p in main.value["unlinked"]] == [("card-x", None)]
    fields = {f.name for f in dataclasses.fields(PendingCard)}
    assert not fields & {"name", "phone", "phone4", "title", "reason"}
    dumped = json.dumps({k: [p.to_dict() for p in v] for out in (mgr, main) for k, v in out.value.items()}, ensure_ascii=False)
    for secret in (FAKE_MOBILE, "5791", *FAKE_NAMES):
        assert secret not in dumped
    assert {r.method for r in env["site"].requests} == {"GET"}
    # 키가 없으면 빈 목록과 설정 안 됨
    none = pending_for_scope(_client(env["site"], env["tmp"], key=""), env["links"], env["scope"], is_main=False)
    assert none.status == NOT_CONFIGURED and none.value == {"linked": [], "unlinked": []}
