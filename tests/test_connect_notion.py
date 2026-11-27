"""노션 읽기 전용 연결 시험. D-13, D-115.

바깥은 가짜 전송 함수로 바꿔 끼운다. 실제 네트워크와 실제 노션을 부르지 않는다. 아래 autouse 장치가 urlopen 과 소켓을 막는다.
시험 자료의 이름과 금액은 모두 가짜 글자다. 키 자리에는 구별되는 가짜 값을 넣고 어디에도 안 남는지 본다.
"""
import json
import pathlib
import re
import sqlite3
import urllib.error

import pytest

from app import db
from app.connect import notion, status_view
from app.connect.base import FAILED, NOT_CONFIGURED, OK
from app.connect.state import StateStore
from app.ops import jobs

TOKEN = "secret_FAKE_notion_token_Zq81xk"
PAGE_A = "11111111-1111-4111-8111-111111111111"
PAGE_B = "22222222-2222-4222-8222-222222222222"
TOG = "33333333-3333-4333-8333-333333333333"
DS = "4215b77e-5d18-8186-934f-000b5cabc040"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    for k in (notion.ENV_TOKEN, notion.ENV_VERSION, notion.ENV_DB):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CONNECT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CONNECT_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("CONNECT_DATA_DIR", str(tmp_path / "data"))

    def _no_network(*a, **k):
        raise AssertionError("시험이 실제 네트워크를 부르려 했습니다")
    monkeypatch.setattr(notion.urllib.request, "urlopen", _no_network)


# ------------------------------------------------------------------ 가짜 노션
def rt(text):
    return [{"type": "text", "plain_text": text}]


def page(pid=PAGE_A, title="가상보험 11월 시책", edited="2026-11-20T01:00:00.000Z", **over):
    props = {
        "제목": {"type": "title", "title": rt(title)},
        "보험사": {"type": "select", "select": {"name": "가상화재"}},
        "구분": {"type": "select", "select": {"name": "시책"}},
        "연월": {"type": "select", "select": {"name": "2026-11"}},
        "시책기간": {"type": "date", "date": {"start": "2026-11-01", "end": "2026-11-30"}},
        "자료유형": {"type": "select", "select": {"name": "월간시상안(PDF)"}},
        "주차": {"type": "select", "select": {"name": "1주차"}},
        "대상상품": {"type": "multi_select", "multi_select": [{"name": "상품가"}, {"name": "상품나"}]},
        "지급·환수조건": {"type": "rich_text", "rich_text": rt("13회차 유지 조건 가상")},
        "핵심시책": {"type": "rich_text", "rich_text": rt("가상 시책 두 줄")},
        "원본링크": {"type": "url", "url": "https://example.test/plan.pdf"},
        "원문이식": {"type": "checkbox", "checkbox": True},
        "처리일": {"type": "date", "date": {"start": "2026-11-21"}},
        "담당": {"type": "people", "people": [{"object": "user", "name": "가상인물"}]},
    }
    props.update(over)
    return {"object": "page", "id": pid, "last_edited_time": edited, "properties": props}


def blk(bid, typ, text="", has_children=False, **extra):
    return {"object": "block", "id": bid, "type": typ, "has_children": has_children, typ: {"rich_text": rt(text), **extra}}


BODY = {
    PAGE_A: [
        {"results": [blk("aaaaaaaa-0000-4000-8000-000000000001", "heading_2", "핵심 시책 요약"),
                     blk("aaaaaaaa-0000-4000-8000-000000000002", "bulleted_list_item", "가상 시책 첫째")], "has_more": True, "next_cursor": "c1"},
        {"results": [blk("aaaaaaaa-0000-4000-8000-000000000003", "bulleted_list_item", "가상 시책 둘째"),
                     blk(TOG, "toggle", "원문 글자", has_children=True)], "has_more": False, "next_cursor": None},
    ],
    TOG: [{"results": [blk("bbbbbbbb-0000-4000-8000-000000000001", "paragraph", "원문 첫 줄 가상"),
                       blk("bbbbbbbb-0000-4000-8000-000000000002", "paragraph", "")], "has_more": False}],
    PAGE_B: [{"results": [blk("cccccccc-0000-4000-8000-000000000001", "paragraph", "둘째 쪽 본문 가상")], "has_more": False}],
}


class Fake:
    """노션을 흉내 내는 전송 함수. 호출을 모두 적어 둔다. script 로 상태 번호나 예외를 정해 둘 수 있다."""

    def __init__(self, pages=None, bodies=None, script=None):
        self.pages = [page()] if pages is None else pages
        self.bodies = {k: list(v) for k, v in (BODY if bodies is None else bodies).items()}
        self.calls = []
        self.script = script or (lambda call: None)   # call -> None | (status, body) | Exception 을 던지게 하려면 예외를 돌려준다

    def __call__(self, method, url, headers, body, timeout):
        call = {"method": method, "url": url, "headers": dict(headers), "body": json.loads(body) if body else None, "timeout": timeout}
        self.calls.append(call)
        hook = self.script(call)
        if isinstance(hook, BaseException):
            raise hook
        if hook is not None:
            return notion.Reply(hook[0], {}, json.dumps(hook[1]).encode())
        path = url[len(notion.API_BASE):].split("?")[0]
        if method == "POST" and path.endswith("/query"):
            return self._ok({"object": "list", "results": self.pages, "has_more": False, "next_cursor": None})
        m = re.match(r"^/pages/([0-9a-f-]+)$", path)
        if m:
            return self._ok(next(x for x in self.pages if x["id"] == m.group(1)))
        m = re.match(r"^/blocks/([0-9a-f-]+)/children$", path)
        if m:
            seq = self.bodies.get(m.group(1), [{"results": [], "has_more": False}])
            cursor = re.search(r"start_cursor=([^&]+)", url)
            return self._ok(seq[1 if cursor and len(seq) > 1 else 0])
        raise AssertionError(f"모르는 요청 {method} {path}")

    @staticmethod
    def _ok(obj):
        return notion.Reply(200, {}, json.dumps(obj, ensure_ascii=False).encode())


def client(fake, **kw):
    kw.setdefault("min_interval", 0)
    kw.setdefault("backoff", 0)
    kw.setdefault("sleep", lambda s: None)
    return notion.NotionClient(kw.pop("token", TOKEN), transport=fake, **kw)


@pytest.fixture()
def con(tmp_path):
    c = db.init_db(tmp_path / "t.db")
    yield c
    c.close()


def run(con, tmp_path, fake=None, **kw):
    cl = kw.pop("client", None) or client(fake or Fake())
    return notion.import_plan_pages(con, cl, data_dir=tmp_path / "data", state=StateStore(con=con), **kw)


def rows(con):
    return [dict(r) for r in con.execute("SELECT * FROM notion_plan_page ORDER BY page_id")]


def raw_files(tmp_path):
    root = tmp_path / "data" / "raw" / "notion_plan"
    return sorted(p for p in root.rglob("*.json") if not p.name.endswith(".meta.json")) if root.exists() else []


# ------------------------------------------------------------------ 속성과 본문 읽기
def test_properties_are_cleaned_into_a_dict():
    c = notion.clean_page(page(), [])
    assert c["page_id"] == PAGE_A and c["title"] == "가상보험 11월 시책"
    assert (c["insurer"], c["kind"], c["doc_type"], c["week"]) == ("가상화재", "시책", "월간시상안(PDF)", "1주차")
    assert c["ym"] == "202611" and c["period_text"] == "2026-11-01 ~ 2026-11-30"
    assert c["products"] == "상품가, 상품나" and c["payback_text"] == "13회차 유지 조건 가상" and c["core_text"] == "가상 시책 두 줄"
    assert c["source_link"] == "https://example.test/plan.pdf" and c["transcribed"] is True and c["processed_on"] == "2026-11-21"
    assert c["last_edited"] == "2026-11-20T01:00:00.000Z" and re.fullmatch(r"[0-9a-f]{64}", c["content_hash"])
    assert set(notion.CLEAN_FIELDS) <= set(c)
    assert "가상인물" not in json.dumps(c, ensure_ascii=False), "사람 속성은 읽지 않는다"


def test_property_variants_and_missing_columns():
    p = page(**{"연월": {"type": "rich_text", "rich_text": rt("2026년 12월")}, "원문이식": {"type": "select", "select": {"name": "미완료"}},
                "원본링크": {"type": "url", "url": "javascript:alert(1)"}})
    del p["properties"]["주차"]
    p["properties"]["지급 환수 조건"] = p["properties"].pop("지급·환수조건")      # 띄어쓰기와 가운뎃점 차이는 같은 열이다
    c = notion.clean_page(p, [])
    assert c["ym"] == "202612" and c["transcribed"] is False and c["source_link"] == "" and c["payback_text"].startswith("13회차")
    assert notion.missing_columns(p) == ["주차"]
    no_ym = page(**{"연월": {"type": "select", "select": None}})
    assert notion.clean_page(no_ym, [])["ym"] == "202611", "연월이 비면 시책기간 글에서 읽는다"


def test_body_blocks_are_read_with_pages_and_children():
    fake = Fake()
    tree, cut = client(fake).read_body(PAGE_A)
    assert cut is False
    text = notion.body_text_of(tree)
    assert text.splitlines() == ["## 핵심 시책 요약", "- 가상 시책 첫째", "- 가상 시책 둘째", "원문 글자", "  원문 첫 줄 가상"]
    urls = [c["url"] for c in fake.calls]
    assert any("start_cursor=c1" in u for u in urls), "다음 쪽을 이어 읽는다"
    assert any(TOG in u for u in urls), "접힌 블록 안을 읽는다"
    assert all(c["method"] == "GET" for c in fake.calls)


def test_body_reading_has_limits():
    many = {PAGE_A: [{"results": [blk(f"dddddddd-0000-4000-8000-{i:012d}", "paragraph", f"줄 {i}") for i in range(30)], "has_more": False}]}
    tree, cut = client(Fake(bodies=many)).read_body(PAGE_A, max_blocks=10)
    assert len(tree) == 10 and cut is True
    deep = {PAGE_A: [{"results": [blk(TOG, "toggle", "x", has_children=True)], "has_more": False}],
            TOG: [{"results": [blk("eeeeeeee-0000-4000-8000-000000000001", "paragraph", "y")], "has_more": False}]}
    tree, cut = client(Fake(bodies=deep)).read_body(PAGE_A, max_depth=1)
    assert cut is True and "children" not in tree[0]


def test_query_uses_data_source_endpoint_by_version():
    fake = Fake()
    pages, more = client(fake).query_pages("collection://" + DS)
    assert [p["id"] for p in pages] == [PAGE_A] and more is False
    call = fake.calls[0]
    assert call["method"] == "POST" and call["url"] == f"{notion.API_BASE}/data_sources/{DS}/query"
    assert call["headers"]["Notion-Version"] == notion.DEFAULT_API_VERSION
    old = Fake()
    client(old, version="2022-06-28").query_pages(DS)
    assert old.calls[0]["url"] == f"{notion.API_BASE}/databases/{DS}/query" and old.calls[0]["headers"]["Notion-Version"] == "2022-06-28"
    with pytest.raises(notion.NotionError):
        client(Fake()).query_pages("not-an-id")


def test_api_version_and_database_come_from_environment(monkeypatch):
    monkeypatch.setenv(notion.ENV_VERSION, "2026-01-15")
    monkeypatch.setenv(notion.ENV_DB, "collection://" + DS.replace("4215", "4216"))
    fake = Fake()
    client(fake, version=None).query_pages()
    assert fake.calls[0]["headers"]["Notion-Version"] == "2026-01-15" and "/data_sources/4216b77e" in fake.calls[0]["url"]
    fake = Fake()
    monkeypatch.delenv(notion.ENV_DB)
    client(fake, version=None).query_pages()
    assert f"/data_sources/{DS}/" in fake.calls[0]["url"], "환경 변수가 없으면 코드의 기본 ID"


def test_query_pagination_and_page_cap():
    class Paged(Fake):
        def __call__(self, method, url, headers, body, timeout):
            if method == "POST":
                self.calls.append({"method": method, "url": url, "headers": headers, "body": json.loads(body), "timeout": timeout})
                n = len(self.calls)
                return self._ok({"results": [page(f"{n:08d}-0000-4000-8000-000000000000")] * 2, "has_more": n < 5, "next_cursor": f"k{n}"})
            return super().__call__(method, url, headers, body, timeout)
    fake = Paged()
    pages, more = client(fake).query_pages(max_pages=5)
    assert len(pages) == 5 and more is True
    assert fake.calls[1]["body"]["start_cursor"] == "k1" and fake.calls[0]["body"]["sorts"][0]["direction"] == "descending"


# ------------------------------------------------------------------ 넣기와 같은 해시 건너뛰기
def test_import_inserts_pages_and_keeps_raw_json(con, tmp_path):
    out = run(con, tmp_path, Fake(pages=[page(), page(PAGE_B, "가상보험 둘째")]))
    assert out.status == OK and out.value["new"] == 2 and out.value["failed"] == 0 and out.value["missing_columns"] == []
    got = {r["page_id"]: r for r in rows(con)}
    a = got[PAGE_A]
    assert a["title"] == "가상보험 11월 시책" and a["ym"] == "202611" and a["transcribed"] == 1 and a["verify_status"] == "unverified"
    assert a["body_text"].startswith("## 핵심 시책 요약") and "원문 첫 줄 가상" in a["body_text"]
    assert a["fetched_at"] and a["first_seen_at"] and a["gone_at"] is None
    files = raw_files(tmp_path)
    assert len(files) == 2 and all(re.match(r"^[0-9a-f]{16}_notion_[0-9a-f]{16}\.json$", f.name) for f in files)
    saved = json.loads(next(f for f in files if a["content_hash"][:16] in f.name).read_text(encoding="utf-8"))
    assert saved["page"]["id"] == PAGE_A and saved["blocks"], "원본 JSON 은 속성과 본문 트리를 그대로 담는다"
    assert a["raw_path"].startswith("raw/notion_plan/202611/")
    st = StateStore(con=con).get(notion.CONN_NAME)
    assert st["last_status"] == OK and st["last_success_at"] and st["detail"]["new"] == 2


def test_same_content_hash_is_not_processed_again(con, tmp_path):
    run(con, tmp_path, Fake())
    first = rows(con)[0]
    files = raw_files(tmp_path)
    # 편집 시각만 바뀐 같은 내용
    out = run(con, tmp_path, Fake(pages=[page(edited="2026-11-25T09:00:00.000Z")]))
    assert out.value["unchanged"] == 1 and out.value["new"] == 0 and out.value["changed"] == 0
    again = rows(con)[0]
    assert again["content_hash"] == first["content_hash"] and again["changed_at"] == first["changed_at"]
    assert again["last_edited"] == "2026-11-25T09:00:00.000Z"
    assert raw_files(tmp_path) == files, "같은 해시는 원본을 다시 쓰지 않는다"
    # 내용이 바뀌면 다시 처리한다
    out = run(con, tmp_path, Fake(pages=[page(**{"핵심시책": {"type": "rich_text", "rich_text": rt("바뀐 시책 가상")}})]))
    assert out.value["changed"] == 1 and len(rows(con)) == 1 and len(raw_files(tmp_path)) == 2
    assert rows(con)[0]["core_text"] == "바뀐 시책 가상" and rows(con)[0]["first_seen_at"] == first["first_seen_at"]


def test_content_hash_ignores_edit_time_but_sees_body_and_columns():
    base = notion.clean_page(page(), [])
    assert notion.clean_page(page(edited="2030-01-01T00:00:00.000Z"), [])["content_hash"] == base["content_hash"]
    assert notion.clean_page(page(), [blk("a" * 8 + "-0000-4000-8000-000000000001", "paragraph", "본문")])["content_hash"] != base["content_hash"]
    assert notion.clean_page(page(pid=PAGE_B), [])["content_hash"] != base["content_hash"]


def test_vanished_page_is_marked_not_deleted(con, tmp_path):
    run(con, tmp_path, Fake(pages=[page(), page(PAGE_B, "둘째")]))
    out = run(con, tmp_path, Fake(pages=[page()]))
    assert out.value["gone"] == 1 and len(rows(con)) == 2
    gone = {r["page_id"]: r["gone_at"] for r in rows(con)}
    assert gone[PAGE_A] is None and gone[PAGE_B]
    run(con, tmp_path, Fake(pages=[page(), page(PAGE_B, "둘째")]))
    assert all(r["gone_at"] is None for r in rows(con)), "돌아오면 표시를 지운다"


def test_trashed_pages_are_skipped(con, tmp_path):
    p = page(PAGE_B, "휴지통")
    p["in_trash"] = True
    out = run(con, tmp_path, Fake(pages=[page(), p]))
    assert out.value["new"] == 1 and out.value["trashed"] == 1 and [r["page_id"] for r in rows(con)] == [PAGE_A]


def test_one_bad_page_does_not_stop_the_others(con, tmp_path):
    def script(call):
        if PAGE_A in call["url"] and "/blocks/" in call["url"]:
            return (500, {"code": "internal_server_error"})
    out = run(con, tmp_path, Fake(pages=[page(), page(PAGE_B, "둘째")], script=script))
    assert out.status == FAILED and out.value["failed"] == 1 and out.value["new"] == 1
    assert [r["page_id"] for r in rows(con)] == [PAGE_B] and "1쪽" in out.reason
    st = StateStore(con=con).get(notion.CONN_NAME)
    assert st["last_status"] == FAILED and st["last_failure_reason"] and st["consecutive_failures"] == 1


def test_time_budget_stops_with_partial_result(con, tmp_path):
    ticks = iter(range(0, 1000, 100))
    out = run(con, tmp_path, Fake(pages=[page(), page(PAGE_B, "둘째")]), budget_seconds=150, clock=lambda: next(ticks))
    assert out.status == FAILED and "시간 예산" in out.reason and out.value["new"] == 1 and len(rows(con)) == 1
    assert out.value["gone"] == 0, "다 읽지 못했으면 사라진 쪽을 표시하지 않는다"


# ------------------------------------------------------------------ 키 없음과 실패
def test_no_token_is_not_configured_and_makes_no_call(con, tmp_path):
    fake = Fake()
    out = run(con, tmp_path, client=notion.NotionClient("", transport=fake))
    assert out.status == NOT_CONFIGURED and "키 없음" in out.reason and fake.calls == []
    st = StateStore(con=con).get(notion.CONN_NAME)
    assert st["last_status"] == NOT_CONFIGURED and "키 없음" in st["last_failure_reason"] and st["last_success_at"] is None
    assert not notion.NotionClient("").configured and notion.NotionClient(TOKEN).configured
    assert con.execute("SELECT COUNT(*) FROM notion_plan_page").fetchone()[0] == 0


def test_status_view_shows_no_key_and_last_times(con, tmp_path):
    env = {}
    r = [x for x in status_view.rows(con, env) if x["name"] == "notion"][0]
    assert r["status"] == "no_key" and r["status_ko"] == "키 없음" and "NOTION_TOKEN" in r["reason"]
    run(con, tmp_path, Fake())
    r = [x for x in status_view.rows(con, {notion.ENV_TOKEN: "x"}) if x["name"] == "notion"][0]
    assert r["status_ko"] == "성공" and r["last_success_at"] and r["summary"].startswith("읽음 1")
    run(con, tmp_path, Fake(script=lambda c: (401, {"code": "unauthorized"})))
    r = [x for x in status_view.rows(con, {notion.ENV_TOKEN: "x"}) if x["name"] == "notion"][0]
    assert r["status_ko"] == "실패" and "401" in r["reason"] and r["last_success_at"] and r["last_failure_at"]
    assert [x["name"] for x in status_view.rows(con, {})][0] == "notion"


def test_401_is_not_retried(con, tmp_path):
    fake = Fake(script=lambda c: (401, {"object": "error", "code": "unauthorized", "message": "API token is invalid. 가상 본문 글"}))
    out = run(con, tmp_path, fake)
    assert out.status == FAILED and "401" in out.reason and len(fake.calls) == 1
    assert "가상 본문" not in out.reason, "응답 본문의 글은 사유에 넣지 않는다"
    assert StateStore(con=con).get(notion.CONN_NAME)["last_status"] == FAILED


def test_403_and_404_are_not_retried(con, tmp_path):
    for status, word in ((403, "권한"), (404, "찾지 못함")):
        fake = Fake(script=lambda c, s=status: (s, {"code": "x"}))
        out = run(con, tmp_path, fake)
        assert out.status == FAILED and word in out.reason and len(fake.calls) == 1


def test_429_is_retried_then_succeeds(con, tmp_path):
    n = {"i": 0}
    sleeps = []

    def script(call):
        if call["method"] == "POST" and n["i"] < 2:
            n["i"] += 1
            return (429, {"code": "rate_limited"})
    fake = Fake(script=script)
    out = run(con, tmp_path, client=client(fake, sleep=sleeps.append, backoff=1.0))
    assert out.status == OK and out.value["new"] == 1
    assert [c["method"] for c in fake.calls[:3]] == ["POST", "POST", "POST"] and sleeps == [1.0, 2.0]


def test_429_forever_fails_after_attempts(con, tmp_path):
    fake = Fake(script=lambda c: (429, {"code": "rate_limited"}))
    out = run(con, tmp_path, client=client(fake, attempts=3))
    assert out.status == FAILED and "429" in out.reason and len(fake.calls) == 3
    assert StateStore(con=con).get(notion.CONN_NAME)["consecutive_failures"] == 1


def test_5xx_is_retried(con, tmp_path):
    fake = Fake(script=lambda c: (503, {}))
    out = run(con, tmp_path, client=client(fake, attempts=2))
    assert out.status == FAILED and "503" in out.reason and len(fake.calls) == 2


def test_timeout_is_retried_then_fails(con, tmp_path):
    fake = Fake(script=lambda c: TimeoutError("노션 응답 시간이 넘었습니다"))
    out = run(con, tmp_path, client=client(fake, attempts=3))
    assert out.status == FAILED and "시간" in out.reason and len(fake.calls) == 3
    assert all(c["timeout"] == 15.0 for c in fake.calls), "소켓 시간 제한을 걸어 보낸다"
    assert con.execute("SELECT COUNT(*) FROM notion_plan_page").fetchone()[0] == 0


def test_connection_error_does_not_leak_out(con, tmp_path):
    out = run(con, tmp_path, client=client(Fake(script=lambda c: ConnectionError("끊김")), attempts=2))
    assert out.status == FAILED and "끊김" in out.reason


def test_bad_json_reply_is_a_failure_not_an_exception(con, tmp_path):
    class Junk(Fake):
        def __call__(self, *a, **k):
            return notion.Reply(200, {}, b"<html>not json</html>")
    out = run(con, tmp_path, Junk())
    assert out.status == FAILED and "응답" in out.reason


def test_failure_does_not_touch_other_work(con, tmp_path):
    """노션이 실패해도 같은 DB 의 다른 일은 그대로 된다."""
    run(con, tmp_path, Fake(script=lambda c: (500, {})))
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES('x','t','ok','t')")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='ok'").fetchone()[0] == 1


# ------------------------------------------------------------------ 키가 새지 않는다
def _everything(con, tmp_path, capsys, caplog):
    dump = "\n".join(con.iterdump())
    state_db = tmp_path / "state.db"
    if state_db.exists():
        s = sqlite3.connect(str(state_db))
        dump += "\n".join(s.iterdump())
        s.close()
    files = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in (tmp_path / "data").rglob("*") if p.is_file())
    cap = capsys.readouterr()
    return dump + files + cap.out + cap.err + caplog.text


def test_token_never_appears_in_output_errors_state_or_files(con, tmp_path, capsys, caplog):
    cases = [
        Fake(), Fake(script=lambda c: (401, {"message": TOKEN})), Fake(script=lambda c: (429, {})),
        Fake(script=lambda c: TimeoutError(f"가상 {TOKEN}")),
        Fake(script=lambda c: ValueError(f"Invalid header value b'Bearer {TOKEN}'")),    # http.client 가 이런 글을 낸다
        Fake(script=lambda c: ConnectionError(TOKEN)),
    ]
    reasons = []
    for f in cases:
        cl = client(f, attempts=2)
        assert TOKEN not in repr(cl) and TOKEN not in str(cl) and TOKEN not in f"{cl!r}"
        out = run(con, tmp_path, client=cl)
        reasons.append(out.reason)
        print(out, out.value)
    assert not any(TOKEN in r for r in reasons) and any("***" in r for r in reasons)
    sent = [c for f in cases for c in f.calls]
    assert sent and all(c["headers"]["Authorization"] == f"Bearer {TOKEN}" for c in sent), "키는 머리글로만 나간다"
    assert not any(TOKEN in c["url"] or TOKEN in json.dumps(c["body"]) for c in sent)
    assert TOKEN not in _everything(con, tmp_path, capsys, caplog)


def test_bad_token_shape_is_refused_without_echo(con, tmp_path):
    bad = "bad token\nwith newline " + TOKEN
    fake = Fake()
    out = run(con, tmp_path, client=notion.NotionClient(bad, transport=fake, min_interval=0, sleep=lambda s: None))
    assert out.status == FAILED and "꼴" in out.reason and TOKEN not in out.reason and fake.calls == []


def test_token_comes_only_from_environment(monkeypatch):
    monkeypatch.setenv(notion.ENV_TOKEN, TOKEN)
    cl = notion.NotionClient(transport=Fake())
    assert cl.configured and TOKEN not in repr(cl)
    src = pathlib.Path(notion.__file__).read_text(encoding="utf-8")
    assert "secret_" not in src and "ntn_" not in src


# ------------------------------------------------------------------ 쓰기 호출이 없다
WRITE_WORDS = ("create", "update", "delete", "append", "patch", "put", "post", "write", "archive", "trash", "restore", "move", "upload", "send")


def test_client_has_no_write_methods():
    names = [n for n in dir(notion.NotionClient) if not n.startswith("__")]
    assert not [n for n in names if any(w in n.lower() for w in WRITE_WORDS)], names
    assert {n for n in names if not n.startswith("_")} >= {"query_pages", "get_page", "block_children", "read_body"}
    module_funcs = [n for n in dir(notion) if callable(getattr(notion, n)) and not n.startswith("_")]
    assert not [n for n in module_funcs if any(w in n.lower() for w in ("create", "delete", "append", "patch", "archive", "trash"))], module_funcs


def test_only_read_requests_are_ever_sent(con, tmp_path):
    fake = Fake(pages=[page(), page(PAGE_B, "둘째")])
    run(con, tmp_path, fake)
    client(fake).get_page(PAGE_A)
    sent = {(c["method"], re.sub(_ID_RE, "<id>", c["url"][len(notion.API_BASE):].split("?")[0])) for c in fake.calls}
    assert sent <= {("POST", "/data_sources/<id>/query"), ("GET", "/blocks/<id>/children"), ("GET", "/pages/<id>")}, sent
    src = pathlib.Path(notion.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\"(PATCH|PUT|DELETE)\"", src), "쓰는 방법은 코드에 없다"


_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.mark.parametrize("method,path", [
    ("PATCH", f"/pages/{PAGE_A}"), ("DELETE", f"/blocks/{PAGE_A}"), ("PUT", f"/pages/{PAGE_A}"),
    ("POST", "/pages"), ("POST", f"/blocks/{PAGE_A}/children"), ("PATCH", f"/blocks/{PAGE_A}/children"),
    ("POST", f"/pages/{PAGE_A}"), ("GET", "/users"), ("GET", "/search"), ("POST", "/search"),
    ("POST", f"/data_sources/{DS}"), ("GET", "/pages/../users"), ("GET", f"/pages/{PAGE_A}/properties/x"),
])
def test_guard_blocks_anything_outside_the_read_list(method, path):
    fake = Fake()
    cl = client(fake)
    with pytest.raises(notion.NotionError):
        cl._request(method, path, body={"x": 1} if method != "GET" else None)
    assert fake.calls == []


def test_default_transport_refuses_other_hosts():
    with pytest.raises(notion.NotionReadOnlyError):
        notion.urllib_transport("GET", "https://evil.test/v1/pages/x", {}, None, 1)


# ------------------------------------------------------------------ urllib 전송 (urlopen 을 가짜로)
def _fake_urlopen(monkeypatch, behavior):
    seen = {}

    class Resp:
        status = 200
        headers = {"X": "y"}

        def __init__(self, body):
            self._b = body

        def read(self, n=-1):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(), headers=dict(req.header_items()), data=req.data, timeout=timeout)
        r = behavior(req)
        if isinstance(r, BaseException):
            raise r
        return Resp(r)
    monkeypatch.setattr(notion.urllib.request, "urlopen", fake)
    return seen


def test_urllib_transport_maps_status_and_errors(monkeypatch):
    seen = _fake_urlopen(monkeypatch, lambda req: b'{"ok": true}')
    r = notion.urllib_transport("POST", f"{notion.API_BASE}/data_sources/{DS}/query", {"Authorization": "Bearer x"}, b"{}", 7)
    assert r.status == 200 and json.loads(r.body) == {"ok": True} and seen["timeout"] == 7 and seen["method"] == "POST"

    def http_401(req):
        return urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {"Retry-After": "2"}, __import__("io").BytesIO(b'{"code":"unauthorized"}'))
    _fake_urlopen(monkeypatch, http_401)
    r = notion.urllib_transport("GET", f"{notion.API_BASE}/pages/{PAGE_A}", {}, None, 7)
    assert r.status == 401 and json.loads(r.body)["code"] == "unauthorized"

    _fake_urlopen(monkeypatch, lambda req: urllib.error.URLError(TimeoutError("timed out")))
    with pytest.raises(TimeoutError):
        notion.urllib_transport("GET", f"{notion.API_BASE}/pages/{PAGE_A}", {}, None, 7)
    _fake_urlopen(monkeypatch, lambda req: TimeoutError("read timed out"))
    with pytest.raises(TimeoutError):
        notion.urllib_transport("GET", f"{notion.API_BASE}/pages/{PAGE_A}", {}, None, 7)
    _fake_urlopen(monkeypatch, lambda req: urllib.error.URLError(OSError(f"dns {TOKEN}")))
    with pytest.raises(ConnectionError) as ei:
        notion.urllib_transport("GET", f"{notion.API_BASE}/pages/{PAGE_A}", {}, None, 7)
    assert TOKEN not in str(ei.value)


def test_default_transport_end_to_end_with_faked_urlopen(con, tmp_path, monkeypatch):
    def behavior(req):
        path = req.full_url[len(notion.API_BASE):].split("?")[0]
        if path.endswith("/query"):
            return json.dumps({"results": [page()], "has_more": False}).encode()
        return json.dumps(BODY[PAGE_A][1] if "start_cursor" in req.full_url else {"results": [], "has_more": False}).encode()
    seen = _fake_urlopen(monkeypatch, behavior)
    cl = notion.NotionClient(TOKEN, min_interval=0, sleep=lambda s: None)
    out = notion.import_plan_pages(con, cl, data_dir=tmp_path / "data", state=StateStore(con=con))
    assert out.status == OK and seen["headers"]["Authorization"] == f"Bearer {TOKEN}" and seen["headers"]["Notion-version"] == notion.DEFAULT_API_VERSION


# ------------------------------------------------------------------ 작업 워커
def test_worker_job_is_not_configured_without_key(con):
    assert "notion_import" in jobs.HANDLERS and jobs.KIND_KO["notion_import"] == "노션 가져오기"
    jid = jobs.enqueue(con, "notion_import", {}, by="t")
    assert jobs.enqueue(con, "notion_import", {}, by="t") == jid, "같은 작업은 대기 중이면 새로 넣지 않는다"
    other = jobs.enqueue(con, "notify_sweep", {}, by="t")
    done = {j["job_id"]: j for j in jobs.drain(con)}
    assert done[jid]["status"] == jobs.DONE and done[jid]["result"]["status"] == NOT_CONFIGURED
    assert done[other]["status"] == jobs.DONE, "다른 작업은 그대로 돈다"
    assert StateStore(con=con).get(notion.CONN_NAME)["last_status"] == NOT_CONFIGURED


def test_worker_job_imports_with_a_fake_transport(con, monkeypatch):
    monkeypatch.setenv(notion.ENV_TOKEN, TOKEN)
    fake = Fake(pages=[page(), page(PAGE_B, "둘째")])
    monkeypatch.setattr(notion, "urllib_transport", fake)
    monkeypatch.setattr(notion.time, "sleep", lambda s: None)
    jid = jobs.enqueue(con, "notion_import", {}, by="t")
    j = {x["job_id"]: x for x in jobs.drain(con)}[jid]
    assert j["status"] == jobs.DONE and j["result"] == {"status": OK, "counts": {"seen": 2, "new": 2, "changed": 0, "unchanged": 0, "failed": 0, "gone": 0, "trashed": 0, "truncated_pages": 0}}
    jid2 = jobs.enqueue(con, "notion_import", {}, by="t")
    j2 = {x["job_id"]: x for x in jobs.drain(con)}[jid2]
    assert j2["result"]["counts"]["unchanged"] == 2 and j2["result"]["counts"]["new"] == 0
    assert TOKEN not in json.dumps(j) and TOKEN not in json.dumps(j2, default=str)
    assert len(rows(con)) == 2


def test_worker_job_survives_a_failing_notion(con, monkeypatch):
    monkeypatch.setenv(notion.ENV_TOKEN, TOKEN)
    monkeypatch.setattr(notion, "urllib_transport", Fake(script=lambda c: (401, {})))
    monkeypatch.setattr(notion.time, "sleep", lambda s: None)
    jid = jobs.enqueue(con, "notion_import", {}, by="t")
    j = {x["job_id"]: x for x in jobs.drain(con)}[jid]
    assert j["status"] == jobs.DONE and j["result"]["status"] == FAILED and j["error"] is None


# ------------------------------------------------------------------ 표와 화면
def test_table_is_created_on_an_old_db_without_it(tmp_path):
    path = tmp_path / "old.db"
    con = db.init_db(path)
    con.execute("DROP TABLE notion_plan_page")
    con.commit()
    out = notion.import_plan_pages(con, client(Fake()), data_dir=tmp_path / "data", state=StateStore(con=con))
    assert out.status == OK and con.execute("SELECT COUNT(*) FROM notion_plan_page").fetchone()[0] == 1
    cols = {r[1] for r in con.execute("PRAGMA table_info(notion_plan_page)")}
    assert set(notion.CLEAN_FIELDS) - {"products"} <= cols | {"transcribed"} and {"fetched_at", "verify_status", "content_hash", "gone_at"} <= cols
    con.close()


def test_jobs_screen_shows_connection_state_and_the_notion_button(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import masters
    from app.web import auth
    path = tmp_path / "web.db"
    c0 = db.init_db(path)
    masters.seed(c0)
    auth.seed_accounts(c0, "notion-pass-1234")
    monkeypatch.setenv("APP_DB", str(path))
    from app.ops import ratelimit
    ratelimit.LIMITER.reset()
    from app.web.main import app
    web = TestClient(app, follow_redirects=False)
    tok = re.compile(r'name="csrf_token" value="([^"]+)"')
    assert web.post("/login", data={"login_id": "admin", "password": "notion-pass-1234", "csrf_token": tok.search(web.get("/login").text).group(1)}).status_code == 303
    html = web.get("/data/jobs").text
    assert "연결 상태" in html and "노션 시책·시상 정리본" in html and "키 없음" in html and "노션 가져오기" in html
    csrf = tok.search(html).group(1)
    r = web.post("/data/jobs", data={"action": "notion_import", "csrf_token": csrf})
    assert r.status_code == 303 and c0.execute("SELECT COUNT(*) FROM job WHERE kind='notion_import' AND status='queued'").fetchone()[0] == 1
    jobs.drain(c0)
    html = web.get("/data/jobs").text
    assert "노션 가져오기" in html and "키 없음" in html and TOKEN not in html
    assert web.post("/data/jobs", data={"action": "notion_write", "csrf_token": csrf}).status_code == 400
    c0.close()
    ratelimit.LIMITER.reset()
