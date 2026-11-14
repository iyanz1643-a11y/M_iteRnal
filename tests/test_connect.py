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
    for k in (card_site.ENV_URL, card_site.ENV_KEY, "GDRIVE_SERVICE_ACCOUNT_FILE", "GDRIVE_FOLDER_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CONNECT_STATE_DIR", str(tmp_path / "state"))
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
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["upload"]["last_success_at"]
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
    dumped += (tmp_path / "state" / "connections.json").read_text(encoding="utf-8")
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
    assert not any(hasattr(c, m) for m in ("approve", "reject", "remove", "issue_codes"))


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
