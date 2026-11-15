"""S3 앱 소개와 초대 랜딩. docs/10 6.1.

- /welcome 은 로그인 없이 열리고 DB 를 열지 않는다. 사람, 조직, 금액을 내지 않는다.
- 초대 토큰은 해시로만 저장하고, 한 번 쓰면 무효이며, 7일이 지나면 쓸 수 없다.
- 초대 링크는 메인 관리자만 계정 화면에서 만들고, 링크는 그 응답에 한 번만 보인다.
- /invite 의 POST 는 CSRF 토큰이 있어야 하고, 비밀번호 규칙을 지켜야 한다.
"""
import hashlib
import os
import pathlib
import re
import tempfile
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db, masters
from app.web import auth
from app.web import invite as iv

PW = "test-pass-1234"
NEW_PW = "Fresh2026pass"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
LINK = re.compile(r'<textarea readonly aria-label="초대 링크"[^>]*>([^<]+)</textarea>')


class Client(TestClient):
    """폼을 보낼 때 세션의 CSRF 토큰을 함께 보낸다. csrf=False 면 보내지 않는다."""
    def post(self, url, data=None, csrf=True, **kw):
        if csrf and data is not None and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


@pytest.fixture(scope="module")
def env():
    path = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    con = db.init_db(path)
    masters.seed(con)
    auth.seed_accounts(con, PW)
    os.environ["APP_DB"] = str(path)
    from app.web.main import app
    yield {"con": con, "app": app}
    con.close()


def login(env, login_id):
    c = Client(env["app"], follow_redirects=False)
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, r.text
    return c


def roles(con):
    manager = con.execute("""SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                             JOIN org_unit o ON o.org_id=m.org_id WHERE o.level='branch' LIMIT 1""").fetchone()[0]
    agents = [r[0] for r in con.execute("""SELECT internal_code FROM agent WHERE status='active'
                                           AND agent_id NOT IN (SELECT agent_id FROM org_manager) ORDER BY internal_code""")]
    return manager, agents


_used = set()


def fresh_target(con) -> tuple:
    """초기 비밀번호 상태로 돌린 설계사 계정 하나. 시험마다 다른 사람을 쓴다. (user_id, login_id)"""
    _, agents = roles(con)
    code = next(c for c in agents[3:] if c not in _used)
    _used.add(code)
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id=?", (code,)).fetchone()[0]
    auth.reset_password(con, uid)
    con.commit()
    return uid, code


def make_link(env, admin, uid) -> str:
    r = admin.post("/org/accounts", data={"action": "invite", "user_id": str(uid)})
    assert r.status_code == 200, r.text
    m = LINK.search(r.text)
    assert m, "초대 링크가 응답에 없다"
    return m.group(1)


def token_of(url: str) -> str:
    return url.split("?t=", 1)[1]


def invitee(env, url):
    """링크를 연 사람. 토큰은 세션으로 옮기고 토큰 없는 /invite 로 넘긴다."""
    c = Client(env["app"], follow_redirects=False)
    r = c.get(url.replace("http://testserver", ""))
    assert r.status_code == 303 and r.headers["location"] == "/invite"
    return c


def can_login(env, login_id, pw):
    c = Client(env["app"], follow_redirects=False)
    return c.post("/login", data={"login_id": login_id, "password": pw}).status_code == 303


# ---------------------------------------------------------------- 랜딩이 자료를 내지 않음
def test_welcome_is_public_and_never_opens_the_db(env):
    from app.web.deps import get_con

    def boom():
        raise AssertionError("앱 소개 화면이 DB 를 열었다")
        yield
    env["app"].dependency_overrides[get_con] = boom
    try:
        r = TestClient(env["app"], follow_redirects=False).get("/welcome")
    finally:
        env["app"].dependency_overrides.pop(get_con, None)
    assert r.status_code == 200
    for want in ("GA 업무 앱", "홈 화면에 추가", "아이폰", "안드로이드", "초대 링크", 'href="/login"'):
        assert want in r.text, want
    assert r.headers["cache-control"] == "no-store"


def test_welcome_shows_no_people_orgs_or_money(env):
    con = env["con"]
    html = TestClient(env["app"]).get("/welcome").text
    names = [r[0] for r in con.execute("SELECT name FROM agent")] + [r[0] for r in con.execute("SELECT display_name FROM user_account")]
    codes = [r[0] for r in con.execute("SELECT internal_code FROM agent")]
    orgs = [r[0] for r in con.execute("SELECT name FROM org_unit WHERE level<>'ga'")]
    leaked = [x for x in names + codes + orgs if x and x in html and x not in auth.ROLE_KO.values()]   # 샘플 admin 의 이름은 역할 이름이다
    assert not leaked, leaked
    assert not re.search(r"\d[\d,]*\s*(원|만원|건|명)", html), "앱 소개에 숫자 자료가 있다"
    links = set(re.findall(r'href="(/[^"#]*)', html))
    assert links <= {"/login", "/static/manifest.webmanifest", "/static/icons/icon-192.png",
                     "/static/icons/apple-touch-icon.png", "/static/app.css"}, links
    assert "<form" not in html


def test_welcome_does_not_need_login_but_menu_screens_still_do(env):
    c = TestClient(env["app"], follow_redirects=False)
    assert c.get("/welcome").status_code == 200
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/org/accounts").headers["location"] == "/login"
    assert "/welcome" in c.get("/login").text


def test_invite_without_token_reveals_nothing(env):
    c = TestClient(env["app"], follow_redirects=False)
    r = c.get("/invite")
    assert r.status_code == 410 and "링크를 쓸 수 없습니다" in r.text and "<form" not in r.text


# ---------------------------------------------------------------- 메인 관리자만 만든다
def test_only_main_admin_makes_invite_links(env):
    con = env["con"]
    uid, _ = fresh_target(con)
    manager, agents = roles(con)
    before = con.execute("SELECT COUNT(*) FROM user_invite").fetchone()[0]
    for who in (manager, agents[0]):
        c = login(env, who)
        assert c.post("/org/accounts", data={"action": "invite", "user_id": str(uid)}).status_code == 403, who
    assert con.execute("SELECT COUNT(*) FROM user_invite").fetchone()[0] == before
    assert TestClient(env["app"], follow_redirects=False).post("/org/accounts", data={"action": "invite", "user_id": str(uid)}).status_code in (303, 403)
    assert con.execute("SELECT COUNT(*) FROM user_invite").fetchone()[0] == before


def test_invite_button_on_accounts_screen_and_link_shown_once(env):
    con = env["con"]
    uid, code = fresh_target(con)
    admin = login(env, "admin")
    page = admin.get("/org/accounts").text
    assert re.search(rf'name="user_id" value="{uid}">.*?value="invite"', page, re.S), "초기 비밀번호 계정에 초대 링크 단추가 없다"
    url = make_link(env, admin, uid)
    assert url.startswith("http://testserver/invite?t=")
    again = admin.get("/org/accounts")
    assert token_of(url) not in again.text and "초대 링크 대기" in again.text
    assert again.headers["cache-control"] == "no-store"
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='account_invite' AND target=?", (code,)).fetchone()[0] == 1


def test_main_admin_cannot_invite_unsuitable_accounts(env):
    con = env["con"]
    admin = login(env, "admin")
    main_uid = con.execute("SELECT user_id FROM user_account WHERE login_id='admin'").fetchone()[0]
    r = admin.post("/org/accounts", data={"action": "invite", "user_id": str(main_uid)})
    assert r.status_code == 400 and not LINK.search(r.text)
    _, agents = roles(con)
    settled = con.execute("SELECT user_id FROM user_account WHERE login_id=?", (agents[1],)).fetchone()[0]   # 이미 비밀번호를 정한 계정
    r = admin.post("/org/accounts", data={"action": "invite", "user_id": str(settled)})
    assert r.status_code == 400 and "재설정" in r.text and not LINK.search(r.text)
    assert admin.post("/org/accounts", data={"action": "invite"}).status_code == 400


# ---------------------------------------------------------------- 해시 저장
def test_token_is_stored_only_as_hash(env):
    con = env["con"]
    uid, _ = fresh_target(con)
    url = make_link(env, login(env, "admin"), uid)
    token = token_of(url)
    assert len(token) >= 40
    row = dict(con.execute("SELECT * FROM user_invite WHERE user_id=? ORDER BY invite_id DESC LIMIT 1", (uid,)).fetchone())
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert all(token not in str(v) for v in row.values())
    for table in ("audit_log", "user_account"):
        for r in con.execute(f"SELECT * FROM {table}"):
            assert all(token not in str(v) for v in tuple(r)), table


# ---------------------------------------------------------------- 한 번 쓰기와 만료
def test_invite_sets_first_password_once(env):
    con = env["con"]
    uid, code = fresh_target(con)
    url = make_link(env, login(env, "admin"), uid)
    c = invitee(env, url)
    form = c.get("/invite")
    assert form.status_code == 200 and code in form.text and 'name="password"' in form.text
    assert form.headers["cache-control"] == "no-store" and form.headers["referrer-policy"] == "no-referrer"
    assert token_of(url) not in form.text
    done = c.post("/invite", data={"password": NEW_PW, "password2": NEW_PW})
    assert done.status_code == 200 and "다 됐습니다" in done.text
    u = con.execute("SELECT must_change_password FROM user_account WHERE user_id=?", (uid,)).fetchone()
    assert u[0] == 0
    assert can_login(env, code, NEW_PW)
    assert con.execute("SELECT used_at FROM user_invite WHERE token_hash=?", (iv.digest(token_of(url)),)).fetchone()[0]
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='account_invite_used' AND target=?", (code,)).fetchone()[0] == 1
    # 같은 링크를 다시 연다
    again = invitee(env, url)
    assert again.get("/invite").status_code == 410
    assert again.post("/invite", data={"password": "Other2026pass", "password2": "Other2026pass"}).status_code == 410
    assert c.post("/invite", data={"password": "Other2026pass", "password2": "Other2026pass"}).status_code == 410
    assert can_login(env, code, NEW_PW) and not can_login(env, code, "Other2026pass")


def test_invite_expires_after_seven_days(env):
    con = env["con"]
    uid, code = fresh_target(con)
    assert iv.TTL_DAYS == 7
    t0 = datetime.now() - timedelta(days=7, minutes=1)
    old, expires = iv.create(con, uid, "admin", now=t0)
    assert expires == (t0 + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    assert iv.lookup(con, old) is None
    assert iv.lookup(con, old, now=t0 + timedelta(days=6, hours=23)) is not None
    c = Client(env["app"], follow_redirects=False)
    c.get(f"/invite?t={old}")
    assert c.get("/invite").status_code == 410
    assert c.post("/invite", data={"password": NEW_PW, "password2": NEW_PW}).status_code == 410
    assert not can_login(env, code, NEW_PW)


def test_new_link_revokes_the_old_one(env):
    con = env["con"]
    uid, code = fresh_target(con)
    admin = login(env, "admin")
    first, second = make_link(env, admin, uid), make_link(env, admin, uid)
    assert first != second
    assert invitee(env, first).get("/invite").status_code == 410
    assert invitee(env, second).get("/invite").status_code == 200


def test_link_dies_if_user_already_changed_password(env):
    """초대를 기다리는 사이에 초기 비밀번호로 들어와 바꿨다면 링크로 다시 덮어쓰지 못한다."""
    con = env["con"]
    uid, code = fresh_target(con)
    url = make_link(env, login(env, "admin"), uid)
    con.execute("UPDATE user_account SET must_change_password=0 WHERE user_id=?", (uid,))
    con.commit()
    assert invitee(env, url).get("/invite").status_code == 410


def test_suspended_account_link_is_refused(env):
    con = env["con"]
    uid, code = fresh_target(con)
    url = make_link(env, login(env, "admin"), uid)
    con.execute("UPDATE user_account SET status='suspended' WHERE user_id=?", (uid,))
    con.commit()
    try:
        assert invitee(env, url).get("/invite").status_code == 410
    finally:
        con.execute("UPDATE user_account SET status='active' WHERE user_id=?", (uid,))
        con.commit()


def test_every_refusal_reads_the_same(env):
    """맞지 않는 토큰, 지난 토큰, 쓴 토큰이 같은 말로 거절된다. 어느 쪽인지 알려 주지 않는다."""
    con = env["con"]
    uid, _ = fresh_target(con)
    expired, _ = iv.create(con, uid, "admin", now=datetime.now() - timedelta(days=8))
    uid2, _ = fresh_target(con)
    used, _ = iv.create(con, uid2, "admin")
    iv.redeem(con, used, NEW_PW, NEW_PW)
    bodies = set()
    for tok in ("x" * 43, expired, used, "../../etc", ""):
        c = Client(env["app"], follow_redirects=False)
        if tok:
            c.get(f"/invite?t={tok}")
        r = c.get("/invite")
        assert r.status_code == 410
        bodies.add(re.sub(r'value="[^"]+"', "", r.text))
    assert len(bodies) == 1


# ---------------------------------------------------------------- CSRF
def test_invite_post_needs_csrf(env):
    con = env["con"]
    uid, code = fresh_target(con)
    url = make_link(env, login(env, "admin"), uid)
    c = invitee(env, url)
    assert c.get("/invite").status_code == 200
    r = c.post("/invite", data={"password": NEW_PW, "password2": NEW_PW}, csrf=False)
    assert r.status_code == 403
    r = c.post("/invite", data={"password": NEW_PW, "password2": NEW_PW, "csrf_token": "wrong"})
    assert r.status_code == 403
    assert not can_login(env, code, NEW_PW)
    assert c.get("/invite").status_code == 200, "CSRF 로 막힌 요청은 링크를 쓰지 않는다"


def test_admin_invite_post_needs_csrf(env):
    con = env["con"]
    uid, _ = fresh_target(con)
    before = con.execute("SELECT COUNT(*) FROM user_invite").fetchone()[0]
    r = login(env, "admin").post("/org/accounts", data={"action": "invite", "user_id": str(uid)}, csrf=False)
    assert r.status_code == 403
    assert con.execute("SELECT COUNT(*) FROM user_invite").fetchone()[0] == before


# ---------------------------------------------------------------- 비밀번호 규칙
@pytest.mark.parametrize("pw, pw2, ok", [
    ("Fresh2026pass", "Fresh2026pass", True),
    ("abc12345", "abc12345", True),
    ("", "", False),
    ("Fresh2026pass", "Fresh2026pasS", False),          # 두 칸이 다름
    ("Ab1", "Ab1", False),                              # 짧음
    ("a1" * 33, "a1" * 33, False),                      # 64자 넘음
    ("onlyletters", "onlyletters", False),              # 숫자 없음
    ("1234567890", "1234567890", False),                # 영문 없음
    (" Fresh2026 ", " Fresh2026 ", False),              # 앞뒤 빈칸
    ("a1a1a1a1a1", "a1a1a1a1a1", False),                # 같은 글자 되풀이
    ("xM0001yz9", "xM0001yz9", False),                  # 사번이 들어감
])
def test_password_rules(pw, pw2, ok):
    assert (iv.password_error(pw, pw2, "M0001") is None) == ok


def test_weak_password_keeps_the_link(env):
    con = env["con"]
    uid, code = fresh_target(con)
    url = make_link(env, login(env, "admin"), uid)
    c = invitee(env, url)
    r = c.post("/invite", data={"password": "short1", "password2": "short1"})
    assert r.status_code == 400 and "8자 이상" in r.text and 'name="password"' in r.text
    r = c.post("/invite", data={"password": f"{code}abc9", "password2": f"{code}abc9"})
    assert r.status_code == 400 and "사번" in r.text
    assert c.get("/invite").status_code == 200
    assert c.post("/invite", data={"password": NEW_PW, "password2": NEW_PW}).status_code == 200
    assert can_login(env, code, NEW_PW)


# ---------------------------------------------------------------- 주소와 자리
def test_invite_routes_keep_the_rate_limit_slot(env):
    """platform 의 요청 횟수 제한이 들어올 자리. 초대 주소 GET 과 POST 모두에 달려 있다."""
    from app.web.routes.landing import invite_throttle
    from tests.test_web_access import api_routes
    found = {}
    for r in api_routes(env["app"].routes):
        if r.path == "/invite":
            found.update({m: any(d.call is invite_throttle for d in r.dependant.dependencies) for m in r.methods})
    assert found == {"GET": True, "POST": True}


def test_landing_routes_are_in_both_open_lists():
    import importlib.util

    from tests.test_web_access import OPEN_ROUTES
    spec = importlib.util.spec_from_file_location("check_context", db.ROOT / "scripts" / "check_context.py")
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    assert {"/welcome", "/invite"} <= OPEN_ROUTES and {"/welcome", "/invite"} <= cc.NON_MENU_ROUTES


def test_old_db_without_invite_table_gets_it(env):
    path = pathlib.Path(tempfile.mkdtemp()) / "old.db"
    con = db.init_db(path)
    con.execute("DROP TABLE user_invite")
    con.commit()
    assert iv.pending(con) == {}
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name='user_invite'").fetchone()
    con.close()
