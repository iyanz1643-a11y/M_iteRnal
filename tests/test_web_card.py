"""명함 승인 화면 /sales/card-approve 의 시험. D-44.

명함 사이트는 httpx.MockTransport 로 흉내 낸다. 실제 네트워크로 나가는 호출은 아래 장치가 막는다.
관리자 키는 실행할 때 무작위로 만든다. 가짜 이름과 연락처는 화면에 나오면 안 되는 값이다.
"""
import itertools
import json
import os
import re
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, masters
from app.connect import card_decide, card_site
from app.connect.card_link import CardLinkTable
from app.connect.state import StateStore
from app.org import card_grant
from app.web import auth, menu
from app.web.routes import card as card_route

PW = "card-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
BASE = "https://card-site.test"
PATH = "/sales/card-approve"
FAKE_NAME = "가짜신청자"                                    # 명함 사이트 쪽 신청자 이름. 화면에 나오면 안 된다
FAKE_MOBILE = "-".join(("010", "0" * 4, "0" * 4))          # 컨텍스트 점검이 원문을 찾지 않도록 조각을 이어 만든다
FAKE_TAIL = "4321"
_AT = itertools.count(10)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32               # 신청본 그림. 머리만 JPEG 이면 된다
# 신청본 index.html. 다듬기에서 빠져야 하는 것을 일부러 넣었다
CARD_HTML = ("<!doctype html><html><head><script>alert(1)</script><link rel=stylesheet href='https://evil.test/x.css'>"
             "<meta http-equiv='refresh' content='0;url=https://evil.test'></head>"
             "<body onload='steal()'><h1>미리보기 명함 본문</h1><img src='card.jpg' onerror='steal()'>"
             "<img src='https://evil.test/track.png'><a href='https://evil.test/'>바로가기</a>"
             "<iframe src='https://evil.test/'></iframe><form action='https://evil.test/'><input name=q></form></body></html>")


class FakeSite:
    """명함 사이트. 관리자 목록, 공개 상태, 승인과 반려를 받는다. 받은 요청을 모두 남긴다."""

    def __init__(self, key, pending=("card-in", "card-out", "card-free"), live=("card-in",)):
        n = next(_AT)
        self.key = key
        self.pending = {s: f"2026-11-{n:02d}T0{i}:00:00Z" for i, s in enumerate(pending)}
        self.live = set(live)
        self.decided = {}
        self.requests = []
        self.bodies = []
        self.no_files = set()

    def posts(self):
        return [r for r in self.requests if r.method == "POST"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/api/admin/") and request.headers.get("x-admin-key") != self.key:
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method == "GET" and path == "/api/admin/list":
            return httpx.Response(200, json={
                "pending": [{"slug": s, "name": FAKE_NAME, "phone": FAKE_MOBILE, "title": "", "at": at, "status": "pending", "reason": "",
                             "files": ["index.html"], "replaces": {"name": FAKE_NAME} if s in self.live else None, "conflict": s == "card-out"}
                            for s, at in self.pending.items()],
                "decided": [], "live": [{"slug": s, "name": FAKE_NAME, "phone4": FAKE_TAIL, "at": "2026-10-01T00:00:00Z"} for s in self.live]})
        if request.method == "GET" and path == "/api/status":
            slug = request.url.params.get("slug")
            if slug in self.pending:
                return httpx.Response(200, json={"slug": slug, "live": slug in self.live, "liveAt": None, "status": "pending", "reason": "",
                                                 "at": self.pending[slug], "decidedAt": None})
            st = self.decided.get(slug, "none")
            return httpx.Response(200, json={"slug": slug, "live": st == "approved", "status": st, "reason": "", "at": None, "decidedAt": None})
        if request.method == "GET" and path == "/api/admin/file":
            slug, fpath = request.url.params.get("slug"), request.url.params.get("path")
            if slug not in self.pending or slug in self.no_files:
                return httpx.Response(404, json={"error": "not_found"})
            if fpath == "index.html":
                return httpx.Response(200, content=CARD_HTML.encode(), headers={"content-type": "text/html; charset=utf-8"})
            if fpath == "card.jpg":
                return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
            return httpx.Response(404, json={"error": "not_found"})
        if request.method == "POST" and path in ("/api/admin/approve", "/api/admin/reject"):
            body = json.loads(request.content or b"{}")
            self.bodies.append(body)
            slug = body.get("slug")
            if slug not in self.pending:
                return httpx.Response(404, json={"error": "no_pending"})
            self.pending.pop(slug)
            op = path.rsplit("/", 1)[1]
            self.decided[slug] = "approved" if op == "approve" else "rejected"
            return httpx.Response(200, json={"ok": True, "slug": slug, "at": "2026-11-14T00:00:00Z"} if op == "approve" else {"ok": True})
        return httpx.Response(404, json={"error": "not_found"})


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리키고, 폼에는 화면의 CSRF 토큰을 넣는다. csrf=False 면 넣지 않는다."""

    def __init__(self, app, db_path):
        super().__init__(app, follow_redirects=False)
        self.db_path = str(db_path)

    def request(self, *a, **kw):
        os.environ["APP_DB"] = self.db_path
        return super().request(*a, **kw)

    def post(self, url, data=None, csrf=True, **kw):
        if csrf and data is not None and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """바깥 환경과 실제 네트워크를 막는다."""
    for k in (card_site.ENV_URL, card_site.ENV_KEY, card_site.ENV_WRITE):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CONNECT_STATE_DIR", str(tmp_path / "state"))

    def _no_network(self, request):
        raise AssertionError("시험이 실제 네트워크를 부르려 했습니다")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _no_network)
    card_route.clear_cache()
    yield
    card_route.clear_cache()
    card_decide._INFLIGHT.clear()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """샘플 조직. 지점장 한 명과 그 범위 안 설계사, 범위 밖 설계사를 고르고 명함 주소 둘을 잇는다. card-free 는 잇지 않는다."""
    path = tmp_path / "card.db"
    con = db.init_db(path)
    masters.seed(con)
    auth.seed_accounts(con, PW)
    uid = lambda login: con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login,)).fetchone()[0]
    manager = con.execute("""SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id
                             WHERE o.level='branch' ORDER BY a.internal_code LIMIT 1""").fetchone()[0]
    mctx = auth.context(con, uid(manager))
    assert mctx["role"] == "manager"
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    active = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' ORDER BY agent_id")]
    inside = next(a for a in active if a in mctx["agent_ids"] and a not in heads)
    outside = next(a for a in active if a not in mctx["agent_ids"])
    free = next(a for a in active if a not in (inside, outside) and a not in heads)
    links = CardLinkTable(con)
    links.link(inside, "card-in", by="admin")
    links.link(outside, "card-out", by="admin")
    con.commit()
    card_grant.grant(con, [manager], by="admin")                       # 메인 관리자가 이 지점장에게 명함 승인 권한을 켜 둔다
    code = lambda a: con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (a,)).fetchone()[0]
    key = secrets.token_hex(8)
    state = {"site": FakeSite(key), "write": True, "key": key}

    def fake_client(c, *, quick=False):
        return card_site.CardSiteClient(BASE, state["key"], transport=httpx.MockTransport(state["site"]), state=StateStore(con=c),
                                        attempts=1, write=state["write"], sleep=lambda s: None)
    monkeypatch.setattr(card_route, "card_client", fake_client)
    from app.web.main import app
    yield {"con": con, "path": path, "app": app, "state": state, "manager": manager, "manager_agent": mctx["agent_id"],
           "inside": inside, "outside": outside, "free": free,
           "code_in": code(inside), "code_out": code(outside), "code_free": code(free),
           "agent": con.execute("SELECT internal_code FROM agent WHERE agent_id NOT IN (SELECT agent_id FROM org_manager) AND status='active' "
                                "ORDER BY agent_id DESC LIMIT 1").fetchone()[0]}
    con.close()


def login(env, login_id):
    c = Client(env["app"], env["path"])
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, r.status_code
    return c


def _section(html, title):
    """제목 뒤부터 다음 구획 제목 앞까지."""
    start = html.index(f'<h2 class="sec">{title}')
    nxt = html.find('<h2 class="sec">', start + 1)
    return html[start: nxt if nxt != -1 else len(html)]


def _card_audit(con):
    return [(r[0], r[1], r[2]) for r in con.execute("SELECT actor, action, after_json FROM audit_log WHERE action IN ('card_approve','card_reject','card_link') ORDER BY id")]


# ---------------------------------------------------------------- 메인 관리자
def test_main_sees_linked_and_unlinked_then_links_and_approves(env):
    site, con = env["state"]["site"], env["con"]
    c = login(env, "admin")
    html = c.get(PATH).text
    assert "명함 사이트 기준" in html and "승인 대기" in html
    linked = _section(html, "설계사와 이어진 요청")
    assert env["code_in"] in linked and env["code_out"] in linked and "card-free" not in linked
    unlinked = _section(html, "이어지지 않은 요청")
    assert "card-free" in unlinked and 'name="action" value="link"' in unlinked
    assert "공개 중인 명함을 바꿈" in html and "공개본과 신청 정보가 다름" in html
    assert f"{BASE}/card-in/" in html                                                   # 공개 중인 명함은 링크로 본다
    # 잇기
    r = c.post(PATH, data={"action": "link", "slug": "card-free", "code": env["code_free"]})
    assert r.status_code == 303
    assert CardLinkTable(con).agent_for("card-free") == env["free"]
    html = c.get(PATH).text
    assert "이었습니다" in html and env["code_free"] in _section(html, "설계사와 이어진 요청")
    assert "card-free" not in _section(html, "이어지지 않은 요청")
    # 승인은 확인 단계를 거친다. 확인 없이 보내면 확인 화면으로 돌린다
    r = c.post(PATH, data={"action": "approve", "slug": "card-free"})
    assert r.status_code == 303 and r.headers["location"] == f"{PATH}?do=approve&slug=card-free" and not site.posts()
    ask = c.get(r.headers["location"]).text
    assert "누구에게나 공개됩니다" in ask and 'name="confirm" value="1"' in ask
    r = c.post(PATH, data={"action": "approve", "slug": "card-free", "confirm": "1"})
    assert r.status_code == 303
    assert [(p.url.path, json.loads(p.content)["slug"]) for p in site.posts()] == [("/api/admin/approve", "card-free")]
    html = c.get(PATH).text
    assert "승인했습니다" in html and "card-free" not in html
    # 같은 요청을 다시 보내도 사이트에 두 번 가지 않는다
    c.post(PATH, data={"action": "approve", "slug": "card-free", "confirm": "1"})
    assert len(site.posts()) == 1
    assert [a for _, a, _ in _card_audit(con)][-3:] == ["card_link", "card_approve", "card_approve"]


def test_main_can_decide_unlinked_request_directly(env):
    site = env["state"]["site"]
    c = login(env, "admin")
    assert c.post(PATH, data={"action": "reject", "slug": "card-free", "reason": "사진을 다시 올려 주세요"}).status_code == 303
    assert [p.url.path for p in site.posts()] == ["/api/admin/reject"]
    assert "반려했습니다" in c.get(PATH).text


# ---------------------------------------------------------------- 관리자
def test_manager_sees_only_scope_and_out_of_scope_post_is_denied(env):
    site, con = env["state"]["site"], env["con"]
    c = login(env, env["manager"])
    html = c.get(PATH).text
    assert env["code_in"] in html and "범위 안 요청 1건" in html
    assert env["code_out"] not in html and "card-out" not in html and "card-free" not in html
    assert "이어지지 않은 요청" not in html and 'value="link"' not in html
    # 범위 밖과 이어지지 않은 주소는 거부되고 아무것도 보내지 않는다
    for slug in ("card-out", "card-free"):
        r = c.post(PATH, data={"action": "approve", "slug": slug, "confirm": "1"})
        assert r.status_code == 403, slug
        r = c.post(PATH, data={"action": "reject", "slug": slug, "reason": "x"})
        assert r.status_code == 403, slug
    assert not site.posts()
    assert c.get(f"{PATH}?do=approve&slug=card-out").text.count("누구에게나 공개됩니다") == 0     # 범위 밖은 확인 화면도 열지 않는다
    # 잇기는 메인 관리자만
    assert c.post(PATH, data={"action": "link", "slug": "card-free", "code": env["code_free"]}).status_code == 403
    assert CardLinkTable(con).agent_for("card-free") is None
    # 범위 안은 반려한다. 사유는 사이트로만 가고 앱에는 남지 않는다
    reason = "사진이 흐립니다 다시 올려 주세요"
    ask = c.get(f"{PATH}?do=reject&slug=card-in").text
    assert "전화번호나 주민번호" in ask and "앱에 저장하지 않고 명함 사이트로만" in ask and 'maxlength="200"' in ask
    assert c.post(PATH, data={"action": "reject", "slug": "card-in", "reason": reason}).status_code == 303
    assert site.bodies[-1] == {"slug": "card-in", "reason": reason}
    assert reason not in json.dumps(_card_audit(con), ensure_ascii=False)
    assert reason not in c.get(PATH).text
    denied = [json.loads(j)["result"] for _, a, j in _card_audit(con) if a in ("card_approve", "card_reject")]
    assert denied.count("denied") == 4 and denied[-1] == "ok"


def test_reject_reason_is_checked_before_sending(env):
    site = env["state"]["site"]
    c = login(env, env["manager"])
    r = c.post(PATH, data={"action": "reject", "slug": "card-in", "reason": f"연락 주세요 {FAKE_MOBILE}"})
    assert r.status_code == 403 and "전화번호나 주민번호" in r.text and FAKE_MOBILE not in r.text
    r = c.post(PATH, data={"action": "reject", "slug": "card-in", "reason": "가" * 201})
    assert r.status_code == 400 and "200자" in r.text
    assert not site.posts()


# ---------------------------------------------------------------- 막는 것
def test_agent_gets_403(env):
    c = login(env, env["agent"])
    assert c.get(PATH).status_code == 403
    assert c.post(PATH, data={"action": "approve", "slug": "card-in", "confirm": "1"}).status_code == 403
    assert "명함 승인" not in c.get("/").text
    assert not env["state"]["site"].requests


def test_post_without_csrf_is_403(env):
    site = env["state"]["site"]
    for who in ("admin", env["manager"]):
        c = login(env, who)
        for data in ({"action": "approve", "slug": "card-in", "confirm": "1"}, {"action": "reject", "slug": "card-in", "reason": "x"},
                     {"action": "link", "slug": "card-free", "code": env["code_free"]}):
            assert c.post(PATH, data=data, csrf=False).status_code == 403
            assert c.post(PATH, data={**data, "csrf_token": "wrong"}).status_code == 403
    assert not site.posts() and CardLinkTable(env["con"]).agent_for("card-free") is None


def test_write_off_shows_notice_and_sends_nothing(env):
    env["state"]["write"] = False
    site = env["state"]["site"]
    c = login(env, "admin")
    html = c.get(PATH).text
    assert "쓰기가 아직 꺼져 있습니다" in html and env["code_in"] in html
    assert "쓰기가 꺼져 있어 눌러도" in c.get(f"{PATH}?do=approve&slug=card-in").text
    assert c.post(PATH, data={"action": "approve", "slug": "card-in", "confirm": "1"}).status_code == 303
    assert c.post(PATH, data={"action": "reject", "slug": "card-out", "reason": "x"}).status_code == 303
    assert "쓰기가 아직 꺼져 있어 보내지 않았습니다" in c.get(PATH).text
    assert not site.posts()
    assert {p.url.path for p in site.requests} == {"/api/admin/list"}


def test_without_key_shows_notice_and_calls_nothing(env):
    env["state"]["key"] = ""
    site = env["state"]["site"]
    for who in ("admin", env["manager"]):
        c = login(env, who)
        r = c.get(PATH)
        assert r.status_code == 200 and "연동 키가 아직 없어" in r.text and "명함 사이트 기준" not in r.text
        assert "명함 승인 대기" not in c.get("/").text
        c.post(PATH, data={"action": "approve", "slug": "card-in", "confirm": "1"})
        assert "연동 키가 아직 없어 보내지 않았습니다" in c.get(PATH).text
    assert not site.requests


def test_html_has_no_card_site_contact(env):
    pages = []
    for who in ("admin", env["manager"]):
        c = login(env, who)
        pages += [c.get(PATH).text, c.get(f"{PATH}?do=approve&slug=card-in").text, c.get(f"{PATH}?do=reject&slug=card-in").text, c.get("/").text]
    pages.append(login(env, "admin").get(f"{PATH}?do=approve&slug=card-free").text)
    assert all("명함" in p for p in pages)
    for p in pages:
        assert FAKE_MOBILE not in p and FAKE_NAME not in p and FAKE_TAIL not in p and env["state"]["key"] not in p


# ---------------------------------------------------------------- 홈 알림
def test_home_shows_pending_count_from_short_cached_call(env):
    site = env["state"]["site"]
    admin = login(env, "admin")
    html = admin.get("/").text
    assert "명함 승인 대기 3건" in html and 'href="/sales/card-approve"' in html
    lists = lambda: sum(1 for r in site.requests if r.url.path == "/api/admin/list")
    n = lists()
    assert n == 1
    m = login(env, env["manager"])
    assert "명함 승인 대기 1건" in m.get("/").text
    admin.get("/")
    assert lists() == n                                                                  # 담아 둔 목록을 쓰고 다시 부르지 않는다
    admin.get(PATH)                                                                     # 승인 화면은 언제나 새로 읽는다
    assert lists() == n + 1


def test_home_hides_notice_when_site_fails(env, monkeypatch):
    def broken(request):
        raise httpx.ConnectError("down")
    env["state"]["site"] = broken
    for who in ("admin", env["manager"]):
        r = login(env, who).get("/")
        assert r.status_code == 200 and "명함 승인 대기" not in r.text
    r = login(env, "admin").get(PATH)
    assert r.status_code == 200 and "목록을 읽지 못했습니다" in r.text


# ---------------------------------------------------------------- 명함 승인 권한. 2026-11-14 대표님 결정
HEADS = "/org/heads"


def _granted(env, login_id):
    return card_grant.is_granted(env["con"], login_id)


def _other_heads(env, n):
    """권한을 켤 수 있는 다른 관리자 계정 n 개. 이 시험의 지점장은 뺀다."""
    out = [c["login_id"] for c in card_grant.candidates(env["con"]) if c["login_id"] != env["manager"]]
    assert len(out) >= n
    return out[:n]


def test_manager_without_grant_has_no_menu_and_gets_403(env):
    card_grant.revoke(env["con"], [env["manager"]], by="admin")
    site = env["state"]["site"]
    c = login(env, env["manager"])
    home = c.get("/").text
    assert 'href="/sales/card-approve"' not in home and "명함 승인 대기" not in home
    assert "명함 승인 권한을 요청할 수 있습니다" in home and 'href="/me#card-request"' in home     # 권한도 요청도 없으면 요청 안내 한 줄만
    assert c.get("/sales/leads").status_code == 200 and "명함 승인" not in c.get("/sales/leads").text   # 같은 묶음의 다른 화면에도 없다
    for url in (PATH, f"{PATH}?preview=card-in", f"{PATH}?do=approve&slug=card-in"):
        assert c.get(url).status_code == 403, url
    assert c.post(PATH, data={"action": "approve", "slug": "card-in", "confirm": "1"}).status_code == 403
    assert not site.requests
    denied = env["con"].execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied' AND actor=? AND target=?",
                                (env["manager"], PATH)).fetchone()[0]
    assert denied >= 1
    assert menu.find(PATH)["requires"] == "card_approver"


def test_main_grants_and_revokes_many_on_heads_with_confirm(env):
    con = env["con"]
    card_grant.revoke(con, [env["manager"]], by="admin")
    others = _other_heads(env, 1)
    pick = [env["manager"], *others]
    admin = login(env, "admin")
    html = admin.get(HEADS).text
    assert "명함 승인 권한" in html and all(f'name="login_id" value="{x}"' in html for x in pick)
    # CSRF 없이는 막힌다
    for action in ("card_grant", "card_revoke"):
        assert admin.post(HEADS, data={"action": action, "login_id": pick, "confirm": "1"}, csrf=False).status_code == 403
        assert admin.post(HEADS, data={"action": action, "login_id": pick, "confirm": "1", "csrf_token": "wrong"}).status_code == 403
    assert not any(_granted(env, x) for x in pick)
    # 확인 단계: 고른 사람 수와 이름을 보여 주고 아직 켜지 않는다
    r = admin.post(HEADS, data={"action": "card_grant", "login_id": pick})
    assert r.status_code == 200 and f"{len(pick)}명의 명함 승인 권한을 켤까요" in r.text
    names = {x: con.execute("SELECT display_name FROM user_account WHERE login_id=?", (x,)).fetchone()[0] for x in pick}
    confirm = r.text[r.text.index('id="grant-confirm"'):]
    confirm = confirm[:confirm.index("</section>")]
    assert all(n in confirm for n in names.values()) and 'name="confirm" value="1"' in confirm
    assert not any(_granted(env, x) for x in pick)
    # 보낸다
    r = admin.post(HEADS, data={"action": "card_grant", "login_id": pick, "confirm": "1"})
    assert r.status_code == 303 and r.headers["location"].startswith(HEADS)
    assert all(_granted(env, x) for x in pick)
    assert f"{len(pick)}명의 명함 승인 권한을 켰습니다" in admin.get(HEADS).text
    m = login(env, env["manager"])
    assert 'href="/sales/card-approve"' in m.get("/").text and m.get(PATH).status_code == 200
    # 끈다. 확인 단계를 거친다
    r = admin.post(HEADS, data={"action": "card_revoke", "login_id": pick})
    assert r.status_code == 200 and f"{len(pick)}명의 명함 승인 권한을 끌까요" in r.text and all(_granted(env, x) for x in pick)
    assert admin.post(HEADS, data={"action": "card_revoke", "login_id": pick, "confirm": "1"}).status_code == 303
    assert not any(_granted(env, x) for x in pick)
    assert "명함 승인 권한을 껐습니다" in admin.get(HEADS).text
    assert m.get(PATH).status_code == 403 and 'href="/sales/card-approve"' not in m.get("/").text
    acts = [r[0] for r in con.execute("SELECT action FROM audit_log WHERE action IN ('card_grant','card_revoke') ORDER BY id")]
    assert acts.count("card_revoke") >= len(pick) and acts.count("card_grant") >= len(pick)
    # 고르지 않으면 존댓말 한 줄
    r = admin.post(HEADS, data={"action": "card_grant"})
    assert r.status_code == 400 and "한 명 이상 골라 주세요" in r.text


def test_grant_error_is_one_polite_line(env):
    admin = login(env, "admin")
    r = admin.post(HEADS, data={"action": "card_grant", "login_id": ["admin"], "confirm": "1"})
    assert r.status_code == 400 and "목록에 없는 계정" in r.text


def test_granted_but_no_longer_head_is_turned_off_and_suspended_shows_no_effect(env):
    """권한 요청 표가 pending_requests 로 먼저 맞추므로, 조직장에서 빠진 사람의 줄은 화면을 열 때 꺼진다.
    계정이 중지된 사람의 줄은 남고 효력 없음으로 보인다."""
    con = env["con"]
    admin = login(env, "admin")                     # 로그인 때 expire_stale 이 빠진 사람의 줄을 끄므로, 빠지는 일은 로그인 뒤에 일어나게 한다
    other = _other_heads(env, 1)[0]
    card_grant.grant(con, [other], by="admin")
    mid = con.execute("SELECT id FROM org_manager WHERE agent_id=?", (env["manager_agent"],)).fetchone()[0]
    con.execute("UPDATE org_manager SET effective_to='200001' WHERE id=?", (mid,))
    con.execute("UPDATE user_account SET status='suspended' WHERE login_id=?", (other,))
    con.commit()
    html = admin.get(HEADS).text
    sec = html[html.index('id="card-grant"'):]
    assert f'value="{env["manager"]}"' not in sec and not _granted(env, env["manager"])
    assert con.execute("SELECT revoked_by FROM card_approver WHERE login_id=? ORDER BY id DESC LIMIT 1", (env["manager"],)).fetchone()[0] == "system:left"
    assert "효력 없음" in sec and "중지된 계정" in sec and f'value="{other}"' in sec


def test_manager_cannot_post_grant_on_heads(env):
    other = _other_heads(env, 1)
    m = login(env, env["manager"])
    for action in ("card_grant", "card_revoke"):
        assert m.post(HEADS, data={"action": action, "login_id": other}).status_code == 403
        assert m.post(HEADS, data={"action": action, "login_id": other, "confirm": "1"}).status_code == 403
    assert not _granted(env, other[0]) and _granted(env, env["manager"])
    assert login(env, env["agent"]).post(HEADS, data={"action": "card_grant", "login_id": other, "confirm": "1"}).status_code == 403


# ---------------------------------------------------------------- 신청본 미리보기
SRCDOC = re.compile(r'<iframe ([^>]*)></iframe>')


def _frame(html):
    m = SRCDOC.search(html)
    assert m, "iframe 이 없습니다"
    attrs = dict(re.findall(r'([a-z-]+)="([^"]*)"', m.group(1)))
    import html as h
    return attrs, h.unescape(attrs.get("srcdoc", ""))


def test_preview_is_sandboxed_without_script(env):
    con = env["con"]
    for who in (env["manager"], "admin"):
        c = login(env, who)
        assert 'href="?preview=card-in"' in c.get(PATH).text                                 # 목록에 신청본 보기가 있다
        r = c.get(f"{PATH}?preview=card-in")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        attrs, doc = _frame(r.text)
        assert 'sandbox=""' in r.text and attrs["sandbox"] == "" and attrs["referrerpolicy"] == "no-referrer"
        assert "미리보기 명함 본문" in doc and "data:image/jpeg;base64," in doc
        low = doc.lower()
        for bad in ("<script", "onload", "onerror", "<iframe", "<form", "evil.test/x.css", "refresh", 'href="https://evil'):
            assert bad not in low, bad
        assert "https://evil.test/track.png" not in doc and "content-security-policy" in low     # 다듬기가 넣은 CSP 만 있다
        assert "?do=approve&amp;slug=card-in" in r.text and "?do=reject&amp;slug=card-in" in r.text
        assert env["code_in"] in r.text
    assert [json.loads(j)["slug"] for (j,) in con.execute("SELECT after_json FROM audit_log WHERE action='card_preview'")] == ["card-in", "card-in"]


def test_preview_out_of_scope_is_denied(env):
    site = env["state"]["site"]
    c = login(env, env["manager"])
    for slug in ("card-out", "card-free"):
        r = c.get(f"{PATH}?preview={slug}")
        assert r.status_code == 403 and "<iframe" not in r.text and "보여 드릴 수 없습니다" in r.text, slug
        assert env["code_out"] not in r.text
    assert not [q for q in site.requests if q.url.path == "/api/admin/file"]
    assert login(env, env["agent"]).get(f"{PATH}?preview=card-in").status_code == 403
    assert c.get(f"{PATH}?preview=Bad_Slug!").status_code == 400


def test_preview_failure_states_have_polite_notice(env):
    site = env["state"]["site"]
    c = login(env, "admin")
    r = c.get(f"{PATH}?preview=card-gone")                                              # 대기 중이 아니다
    assert r.status_code == 200 and "승인 대기 중인 신청이 아니라" in r.text and "<iframe" not in r.text
    site.no_files.add("card-out")
    r = c.get(f"{PATH}?preview=card-out")
    assert r.status_code == 200 and "신청본 파일이 없습니다" in r.text and "<iframe" not in r.text
    env["state"]["key"] = ""
    r = c.get(f"{PATH}?preview=card-in")
    assert r.status_code == 200 and "연동 키가 아직 없어" in r.text and r.headers["cache-control"] == "no-store"


def test_own_request_has_no_decision_buttons(env):
    con, site = env["con"], env["state"]["site"]
    CardLinkTable(con).link(env["manager_agent"], "card-self", by="admin")
    con.commit()
    site.pending["card-self"] = "2026-11-14T01:00:00Z"
    c = login(env, env["manager"])
    html = c.get(PATH).text
    assert "slug=card-self" not in html                                                # 본인 요청은 목록에 없거나 단추가 없다
    ask = c.get(f"{PATH}?do=approve&slug=card-self").text
    assert "누구에게나 공개됩니다" not in ask
    assert c.post(PATH, data={"action": "approve", "slug": "card-self", "confirm": "1"}).status_code == 403
    assert "명함 승인 대기 1건" in c.get("/").text                                       # 홈 건수에도 본인 요청은 없다
    assert not site.posts()


# ---------------------------------------------------------------- 명함 승인 권한 요청. 2026-11-14 대표님 추가 결정
def _pending(env):
    return card_grant.pending_requests(env["con"])


def _req_status(env, rid):
    return env["con"].execute("SELECT status FROM card_approver_request WHERE id=?", (rid,)).fetchone()[0]


def _requests_of(env, login_id):
    return env["con"].execute("SELECT id, status, reason FROM card_approver_request WHERE login_id=? ORDER BY id", (login_id,)).fetchall()


def _me_card(html):
    """내 정보의 명함 승인 권한 칸. 비밀번호 바꾸기 앞까지."""
    start = html.index('id="card-request"')
    return html[start: html.index('<h2 class="sec">비밀번호', start)]


def _confirm_box(html, anchor='id="req-confirm"'):
    box = html[html.index(anchor):]
    return box[:box.index("</section>")]


def test_main_approves_and_rejects_many_requests_with_confirm(env):
    con = env["con"]
    admin = login(env, "admin")                                       # 로그인 때 조직장과 대리의 요청이 저절로 생긴다
    reqs = _pending(env)
    assert len(reqs) >= 4 and all(r["reason"] == "new_head" for r in reqs)
    html = admin.get(HEADS).text
    sec = html[html.index('id="card-grant"'):]
    assert f"권한 요청 {len(reqs)}건" in sec and "요청 없이 직접 켜기와 끄기" in sec and "새로 맡음" in sec
    assert sec.index('id="card-requests"') < sec.index("요청 없이 직접 켜기와 끄기")      # 요청 표가 맨 위에 있다
    assert all(f'name="req_id" value="{r["id"]}"' in sec for r in reqs)
    assert "메인 관리자가 승인해야 켜집니다" in sec and "고른 관리자만" not in sec
    a, b, c, d = reqs[:4]
    # CSRF 없이는 막힌다
    for action in ("req_approve", "req_reject"):
        assert admin.post(HEADS, data={"action": action, "req_id": [a["id"]], "confirm": "1"}, csrf=False).status_code == 403
        assert admin.post(HEADS, data={"action": action, "req_id": [a["id"]], "confirm": "1", "csrf_token": "wrong"}).status_code == 403
    assert _req_status(env, a["id"]) == "pending"
    # 확인 단계: 고른 사람을 다시 보여 주고 아직 처리하지 않는다
    r = admin.post(HEADS, data={"action": "req_approve", "req_id": [a["id"], b["id"]]})
    assert r.status_code == 200 and "2명의 권한 요청을 승인할까요" in r.text
    box = _confirm_box(r.text)
    assert a["name"] in box and b["name"] in box and c["name"] not in box and 'name="confirm" value="1"' in box
    assert _req_status(env, a["id"]) == _req_status(env, b["id"]) == "pending"
    # 승인
    r = admin.post(HEADS, data={"action": "req_approve", "req_id": [a["id"], b["id"]], "confirm": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/org/heads#card-requests"
    assert card_grant.is_granted(con, a["login_id"]) and card_grant.is_granted(con, b["login_id"])
    html = admin.get(HEADS).text
    assert "요청 2건을 승인했습니다" in html and f"권한 요청 {len(reqs) - 2}건" in html
    assert f'name="req_id" value="{a["id"]}"' not in html
    # 반려도 확인 단계를 거친다
    r = admin.post(HEADS, data={"action": "req_reject", "req_id": [c["id"]]})
    assert r.status_code == 200 and "1명의 권한 요청을 반려할까요" in r.text and c["name"] in _confirm_box(r.text)
    assert admin.post(HEADS, data={"action": "req_reject", "req_id": [c["id"]], "confirm": "1"}).status_code == 303
    assert _req_status(env, c["id"]) == "rejected" and not card_grant.is_granted(con, c["login_id"])
    assert "요청 1건을 반려했습니다" in admin.get(HEADS).text
    # 이미 처리된 요청이 섞이면 건너뛰고 이름과 까닭을 존댓말로 알린다
    assert admin.post(HEADS, data={"action": "req_approve", "req_id": [a["id"], d["id"]], "confirm": "1"}).status_code == 303
    html = admin.get(HEADS).text
    assert "요청 1건을 승인했습니다" in html and "1건은 건너뛰었습니다" in html and f"{a['name']} {a['code']}: 이미 승인 처리된 요청입니다" in html
    # 확인 단계에서 이미 처리된 요청을 고르면 다시 고르게 한다
    r = admin.post(HEADS, data={"action": "req_reject", "req_id": [a["id"]]})
    assert r.status_code == 400 and "이미 처리되었거나 목록에 없는 요청" in r.text
    r = admin.post(HEADS, data={"action": "req_approve"})
    assert r.status_code == 400 and "한 건 이상 골라 주세요" in r.text
    acts = [x[0] for x in con.execute("SELECT action FROM audit_log WHERE action IN ('card_grant','card_request_reject') ORDER BY id")]
    assert acts.count("card_request_reject") == 1 and acts.count("card_grant") >= 4
    # 직접 켜기 표의 대기 표시는 남은 요청만큼이다
    assert html.count("요청 대기</span>") == 2 * len(_pending(env))            # 휴대폰용과 PC용 칸에 한 번씩


def test_manager_and_agent_cannot_post_request_decisions(env):
    login(env, "admin")
    rid = _pending(env)[0]["id"]
    for who in (env["manager"], env["agent"]):
        c = login(env, who)
        for action in ("req_approve", "req_reject"):
            assert c.post(HEADS, data={"action": action, "req_id": [rid]}).status_code == 403
            assert c.post(HEADS, data={"action": action, "req_id": [rid], "confirm": "1"}).status_code == 403
    assert _req_status(env, rid) == "pending"


def test_manager_requests_on_me_once_then_rejected_and_asks_again(env):
    con, mgr = env["con"], env["manager"]
    card_grant.revoke(con, [mgr], by="admin")
    before = len(_requests_of(env, mgr))
    other = _other_heads(env, 1)[0]
    m = login(env, mgr)
    html = m.get("/me").text
    sec = _me_card(html)
    assert "없음" in sec and "권한 요청하기" in sec and 'name="action" value="card_request"' in sec
    # CSRF 없이는 막힌다
    assert m.post("/me", data={"action": "card_request"}, csrf=False).status_code == 403
    assert m.post("/me", data={"action": "card_request", "csrf_token": "wrong"}).status_code == 403
    assert len(_requests_of(env, mgr)) == before
    # 두 번 눌러도 하나만 생긴다. 폼에 다른 계정을 넣어도 본인 것만 요청한다
    other_before = _requests_of(env, other)
    r1 = m.post("/me", data={"action": "card_request", "login_id": other})
    assert r1.status_code == 303 and r1.headers["location"] == "/me#card-request"
    assert "명함 승인 권한을 요청했습니다" in m.get("/me").text
    r2 = m.post("/me", data={"action": "card_request"})
    assert r2.status_code == 303 and "이미 요청이 처리 중입니다" in m.get("/me").text
    mine = [x for x in _requests_of(env, mgr) if x["status"] == "pending"]
    assert len(mine) == 1 and mine[0]["reason"] == "self"
    assert _requests_of(env, other) == other_before
    sec = _me_card(m.get("/me").text)
    assert "요청 처리 중" in sec and "본인이 요청했습니다" in sec and 'value="card_request"' not in sec
    assert "명함 승인 권한을 요청할 수 있습니다" not in m.get("/").text                    # 대기 중이면 홈 안내는 없다
    # 메인 관리자 화면에는 본인 요청으로 보인다
    admin = login(env, "admin")
    row = next(r for r in _pending(env) if r["login_id"] == mgr)
    assert row["reason"] == "self" and "본인 요청" in admin.get(HEADS).text
    # 반려되면 반려됨이 보이고 다시 요청할 수 있다
    assert admin.post(HEADS, data={"action": "req_reject", "req_id": [row["id"]], "confirm": "1"}).status_code == 303
    sec = _me_card(m.get("/me").text)
    assert "반려됨" in sec and "권한 다시 요청하기" in sec
    assert m.post("/me", data={"action": "card_request"}).status_code == 303
    assert [x["status"] for x in _requests_of(env, mgr)][-2:] == ["rejected", "pending"]
    # 비밀번호 바꾸기는 그대로 돈다
    assert "서로 다릅니다" in m.post("/me", data={"action": "password", "old": PW, "new": "new-pass-5678", "new2": "x"}).text


def test_agent_has_no_card_grant_on_me_and_post_is_denied(env):
    for who in (env["agent"], "admin"):
        html = login(env, who).get("/me").text
        assert "명함 승인 권한" not in html and "card_request" not in html
    c = login(env, env["agent"])
    assert c.post("/me", data={"action": "card_request"}).status_code == 403
    assert not _requests_of(env, env["agent"])
    assert login(env, "admin").post("/me", data={"action": "card_request"}).status_code == 403
    assert not _requests_of(env, "admin")


def test_home_shows_request_count_for_main(env):
    admin = login(env, "admin")
    n = len(_pending(env))
    html = admin.get("/").text
    assert f"명함 승인 권한 요청 {n}건" in html and 'href="/org/heads#card-requests"' in html
    rid = _pending(env)[0]["id"]
    admin.post(HEADS, data={"action": "req_approve", "req_id": [rid], "confirm": "1"})
    assert f"명함 승인 권한 요청 {n - 1}건" in admin.get("/").text
    m = login(env, env["manager"])
    home = m.get("/").text
    assert "명함 승인 권한 요청" not in home and "명함 승인 권한을 요청할 수 있습니다" not in home     # 권한이 있는 관리자에게는 없다
    ids = [r["id"] for r in _pending(env)]
    admin.post(HEADS, data={"action": "req_reject", "req_id": ids, "confirm": "1"})
    assert "명함 승인 권한 요청" not in admin.get("/").text


def test_approved_manager_sees_card_menu(env):
    con, mgr = env["con"], env["manager"]
    card_grant.revoke(con, [mgr], by="admin")
    m = login(env, mgr)
    assert 'href="/sales/card-approve"' not in m.get("/").text and m.get(PATH).status_code == 403
    assert m.post("/me", data={"action": "card_request"}).status_code == 303
    admin = login(env, "admin")
    rid = next(r["id"] for r in _pending(env) if r["login_id"] == mgr)
    assert admin.post(HEADS, data={"action": "req_approve", "req_id": [rid], "confirm": "1"}).status_code == 303
    assert 'href="/sales/card-approve"' in m.get("/").text and m.get(PATH).status_code == 200
    assert "지금 권한이 있음" in _me_card(m.get("/me").text)
