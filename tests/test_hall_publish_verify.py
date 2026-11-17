"""검증: 명예의전당 공개 스위치 /perf/hall POST 를 공격하듯 보고, 참고용 안내 문구를 세 역할의 메뉴 화면 전체에서 본다.

만든 쪽의 시험(tests/test_hall_publish_and_ref_note.py)과 따로 쓴다. 겹치지 않는 경우를 본다.

- 남의 공개를 바꿀 길: 폼과 주소의 agent_id, 남이 가진 item_key, 관리자가 팀원 항목, 다른 세션의 CSRF 토큰,
  GET 과 다른 메서드, 로그인 전, 비밀번호를 바꿔야 하는 계정, 퇴사자, 확정 전 항목, 같은 요청 두 번, 변경 기록.
- 안내 문구: 세 역할에게 보이는 메뉴 화면 전부에서 맨 아래 footer 가 한 번, 같은 글자로, main 바로 뒤에 있다.
  문구의 다른 꼴이 없다. main 밖에는 footer 말고 새 글이 없다(D-17 의 main 본문 검사가 뜻을 잃지 않는다).

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. 실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import os
import pathlib
import re
import shutil
import tempfile
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db
from app.connect import card_badges as cb
from app.connect import card_site
from app.web import auth, menu

PW = "hall-verify-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
REF = "이 자료는 참고용이며 정확한 계산은 수수료 담당 부서 또는 유관부서에 문의해 주세요."
FOOT = f'<footer class="ref-foot" data-note="ref-foot">{REF}</footer>'
TEAM_KEY, GONE_KEY = "cert:MDRT:2018", "cert:MDRT:2017"       # 샘플에 없는 연도. 팀원 한 사람과 퇴사 시험용 한 사람에게만 둔다


class Client(TestClient):
    def __init__(self, app, db_path):
        super().__init__(app, follow_redirects=False)
        self.db_path = str(db_path)

    def request(self, *a, **kw):
        os.environ["APP_DB"] = self.db_path
        return super().request(*a, **kw)

    def token(self):
        return TOKEN.search(self.get("/login").text).group(1)

    def post(self, url, data=None, csrf=True, **kw):
        data = dict(data or {})
        if csrf:
            data.setdefault("csrf_token", self.token())
        return super().post(url, data=data, **kw)


def _program(con, code):
    return con.execute("SELECT program_id FROM certification_program WHERE code=?", (code,)).fetchone()[0]


@pytest.fixture(scope="module")
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="hallverify-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR", "CARD_SITE_WRITE", "ANTHROPIC_API_KEY",
            "APP_CONSULT_OPEN", card_site.ENV_URL, card_site.ENV_KEY, card_site.ENV_WRITE, menu.MODULES_ENV)
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    for k in keys[5:]:
        os.environ.pop(k, None)
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    users = {r[0]: r[1] for r in con.execute("""SELECT u.agent_id, u.login_id FROM user_account u JOIN agent a ON a.agent_id=u.agent_id
                                                 WHERE u.status='active' AND a.status='active' ORDER BY u.agent_id""")}
    uid = {r[0]: r[1] for r in con.execute("SELECT login_id, user_id FROM user_account")}
    # 범위 안에 계정 있는 팀원이 둘 이상인 관리자와, 그 팀원 둘
    manager = team = spare = None
    for a in users:
        if a not in heads:
            continue
        ctx = auth.context(con, uid[users[a]])
        if ctx["role"] != "manager":
            continue
        members = [m for m in sorted(ctx["agent_ids"]) if m in users and m not in heads]
        if len(members) >= 2:
            manager, team, spare = a, members[0], members[1]
            break
    assert manager is not None
    # 확정 전 배지가 있는 계정. 같은 열쇠가 확정으로는 없는 것
    everyone = list(users)
    badges = cb.judge_badges(con, everyone)
    valid = {(b.agent_id, b.key) for b in badges if b.eligible}
    expected = next(((b.agent_id, b.key) for b in badges if b.status == cb.EXPECTED and (b.agent_id, b.key) not in valid
                     and b.agent_id not in heads), None)
    assert expected is not None, "샘플에 확정 전 배지가 없다"
    for year, who in ((2018, team), (2017, spare)):
        assert not con.execute("SELECT 1 FROM agent_certification_award WHERE year=?", (year,)).fetchone()
        con.execute("INSERT INTO agent_certification_award(agent_id, program_id, year, awarded_at) VALUES(?,?,?,?)",
                    (who, _program(con, "MDRT"), year, f"{year}-12-01"))
    con.commit()
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "con": con, "app": app, "manager": manager, "team": team, "spare": spare,
           "expected": expected, "uid": uid,
           "login": {"manager": users[manager], "team": users[team], "spare": users[spare], "expected": users[expected[0]], "main": "admin"}}
    con.close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _modules_env(monkeypatch):
    monkeypatch.delenv(menu.MODULES_ENV, raising=False)
    menu._off_from.cache_clear()
    yield
    menu._off_from.cache_clear()


def login(env, who):
    c = Client(env["app"], env["db"])
    assert c.post("/login", data={"login_id": env["login"][who], "password": PW}).status_code == 303, who
    return c


def _pub(con, agent_id) -> dict:
    cb.ensure_publish(con)
    return {r[0]: r[1] for r in con.execute("SELECT item_key, published FROM card_publish WHERE agent_id=?", (agent_id,))}


def _all_pub(con) -> list:
    cb.ensure_publish(con)
    return [tuple(r) for r in con.execute("SELECT agent_id, item_key, published, changed_by, changed_at FROM card_publish ORDER BY 1, 2")]


def _audit_n(con) -> int:
    return con.execute("SELECT COUNT(*) FROM audit_log WHERE action=?", (cb.AUDIT_PUBLISH,)).fetchone()[0]


def _reset(con, *agent_ids):
    cb.ensure_publish(con)
    for a in agent_ids:
        con.execute("DELETE FROM card_publish WHERE agent_id=?", (a,))
    con.commit()


def _form(key, published="1", **kw):
    return {"action": "publish", "item_key": key, "published": published, **kw}


# ---------------------------------------------------------------- 1. 관리자가 팀원의 항목을 바꾸려 한다
def test_manager_cannot_publish_or_unpublish_team_member_item(env):
    con, team = env["con"], env["team"]
    _reset(con, team, env["manager"])
    member = login(env, "team")
    assert member.post("/perf/hall", data=_form(TEAM_KEY)).status_code == 303
    assert _pub(con, team) == {TEAM_KEY: 1}
    before, n = _all_pub(con), _audit_n(con)
    mgr = login(env, "manager")
    assert team in auth.context(con, env["uid"][env["login"]["manager"]])["agent_ids"]      # 정말 범위 안 팀원이다
    tries = [
        ("/perf/hall", _form(TEAM_KEY, "0", agent_id=str(team))),
        (f"/perf/hall?agent_id={team}", _form(TEAM_KEY, "0")),
        (f"/perf/hall?agent={team}&agent_id={team}", _form(TEAM_KEY, "0", agent=str(team), login_id=env["login"]["team"])),
        ("/perf/hall", _form(TEAM_KEY, "1", agent_id=str(team))),
        ("/perf/hall", _form(f"{TEAM_KEY}", "0", by=env["login"]["team"])),
    ]
    for url, data in tries:
        r = mgr.post(url, data=data)
        assert r.status_code == 303 and r.headers["location"] == "/perf/hall#mine", url
    assert _all_pub(con) == before                                  # 팀원 것도 관리자 것도 그대로다
    assert _audit_n(con) == n
    # 명함 화면의 같은 토글로도 같다
    mgr.post("/sales/card", data=_form(TEAM_KEY, "0", agent_id=str(team)))
    assert _all_pub(con) == before
    # 화면에도 팀원의 스위치가 없다
    html = mgr.get("/perf/hall").text
    assert f'data-mine-key="{TEAM_KEY}"' not in html and f'value="{TEAM_KEY}"' not in html
    _reset(con, team)


# ---------------------------------------------------------------- 2. 다른 세션의 CSRF 토큰, GET, 다른 메서드, 로그인 전
def test_csrf_from_other_session_is_refused(env):
    con, team = env["con"], env["team"]
    _reset(con, team)
    victim = login(env, "team")
    attacker = login(env, "manager")
    stolen = attacker.token()
    assert stolen != victim.token()
    r = victim.post("/perf/hall", data=_form(TEAM_KEY, csrf_token=stolen), csrf=False)
    assert r.status_code == 403
    r = victim.post("/perf/hall", data=_form(TEAM_KEY), csrf=False, headers={"x-csrf-token": stolen})
    assert r.status_code == 403
    assert _pub(con, team) == {}


def test_get_and_other_methods_do_not_change(env):
    con, team = env["con"], env["team"]
    _reset(con, team)
    n = _audit_n(con)
    c = login(env, "team")
    tok = c.token()
    q = f"/perf/hall?action=publish&item_key={TEAM_KEY}&published=1&csrf_token={tok}"
    r = c.get(q)
    assert r.status_code == 200
    assert "공개로 바꿨습니다." not in r.text
    for m in ("PUT", "PATCH", "DELETE"):
        r = c.request(m, "/perf/hall", data=_form(TEAM_KEY, csrf_token=tok))
        assert r.status_code in (403, 404, 405), (m, r.status_code)
    assert _pub(con, team) == {} and _audit_n(con) == n


def test_logged_out_and_password_change_pending_cannot_change(env):
    con, team = env["con"], env["team"]
    _reset(con, team)
    anon = Client(env["app"], env["db"])
    r = anon.post("/perf/hall", data=_form(TEAM_KEY))
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert _pub(con, team) == {}
    c = login(env, "team")
    con.execute("UPDATE user_account SET must_change_password=1 WHERE login_id=?", (env["login"]["team"],))
    con.commit()
    try:
        r = c.post("/perf/hall", data=_form(TEAM_KEY))
        assert r.status_code == 303 and r.headers["location"] != "/perf/hall#mine"
        assert _pub(con, team) == {}
    finally:
        con.execute("UPDATE user_account SET must_change_password=0 WHERE login_id=?", (env["login"]["team"],))
        con.commit()


# ---------------------------------------------------------------- 3. 확정 전 항목
def test_expected_item_cannot_be_published(env):
    con = env["con"]
    agent_id, key = env["expected"]
    _reset(con, agent_id)
    n = _audit_n(con)
    c = login(env, "expected")
    html = c.get("/perf/hall").text
    row = html[html.index('id="mine"'):html.index("</main>")]
    i = row.find(f'data-mine-key="{key}"')
    assert i >= 0, "확정 전 항목이 내 수상과 자격에 보이지 않는다"
    j = row.find('<div class="req"', i + 1)
    row = row[i:j if j > 0 else len(row)]
    assert "확정 전이라 아직 공개할 수 없습니다" in row and "<form" not in row
    r = c.post("/perf/hall", data=_form(key))
    assert r.status_code == 303
    assert "자격이 확인된 항목만 공개할 수 있습니다." in c.get("/perf/hall").text
    assert _pub(con, agent_id) == {} and _audit_n(con) == n


# ---------------------------------------------------------------- 4. 퇴사자
@pytest.mark.parametrize("how", ["termination_date", "status"])
def test_departed_agent_cannot_publish(env, how):
    con, spare = env["con"], env["spare"]
    _reset(con, spare)
    c = login(env, "spare")
    assert c.post("/perf/hall", data=_form(GONE_KEY)).status_code == 303
    assert _pub(con, spare) == {GONE_KEY: 1}
    old = con.execute("SELECT status, termination_date FROM agent WHERE agent_id=?", (spare,)).fetchone()
    if how == "termination_date":
        con.execute("UPDATE agent SET termination_date=? WHERE agent_id=?", ((date.today() - timedelta(days=1)).isoformat(), spare))
    else:
        con.execute("UPDATE agent SET status='terminated' WHERE agent_id=?", (spare,))
    con.commit()
    try:
        # 남에게는 이미 공개였던 항목도 보이지 않는다
        assert spare not in cb.published_keys(con, [spare])
        c.post("/perf/hall", data=_form(GONE_KEY, "0"))                 # 내리기는 막지 않는다
        assert _pub(con, spare) == {GONE_KEY: 0}
        n = _audit_n(con)
        r = c.post("/perf/hall", data=_form(GONE_KEY))
        if r.status_code == 303:                                           # 계정이 살아 있으면 화면에 이유가 나온다
            assert "공개로 바꿨습니다." not in c.get("/perf/hall").text
        assert _pub(con, spare) == {GONE_KEY: 0} and _audit_n(con) == n
        html = c.get("/perf/hall").text
        assert f'name="item_key" value="{GONE_KEY}"' not in html           # 스위치가 그려지지 않는다
    finally:
        con.execute("UPDATE agent SET status=?, termination_date=? WHERE agent_id=?", (old[0], old[1], spare))
        con.commit()
        _reset(con, spare)


# ---------------------------------------------------------------- 5. 같은 요청 두 번과 변경 기록
def test_double_submit_logs_once_and_audit_has_before_after(env):
    import json
    con, team = env["con"], env["team"]
    _reset(con, team)
    c = login(env, "team")
    n = _audit_n(con)
    tok = c.token()
    for _ in range(2):
        assert c.post("/perf/hall", data=_form(TEAM_KEY, csrf_token=tok), csrf=False).status_code == 303
    assert _pub(con, team) == {TEAM_KEY: 1}
    assert _audit_n(con) == n + 1
    assert "이미 공개로 두셨습니다." in c.get("/perf/hall").text
    for _ in range(2):
        c.post("/perf/hall", data=_form(TEAM_KEY, "0", csrf_token=tok), csrf=False)
    assert _audit_n(con) == n + 2
    rows = con.execute("SELECT actor, target, before_json, after_json FROM audit_log WHERE action=? ORDER BY id DESC LIMIT 2",
                       (cb.AUDIT_PUBLISH,)).fetchall()
    off, on = rows
    assert on[0] == off[0] == env["login"]["team"] and on[1] == off[1] == f"agent:{team}"
    assert json.loads(on[2]) == {"item_key": TEAM_KEY, "published": False} and json.loads(on[3]) == {"item_key": TEAM_KEY, "published": True}
    assert json.loads(off[2]) == {"item_key": TEAM_KEY, "published": True} and json.loads(off[3]) == {"item_key": TEAM_KEY, "published": False}
    r = con.execute("SELECT changed_by FROM card_publish WHERE agent_id=? AND item_key=?", (team, TEAM_KEY)).fetchone()
    assert r[0] == env["login"]["team"]
    _reset(con, team)


# ---------------------------------------------------------------- 6. 메인 관리자는 설계사 번호가 이어져 있어도 403
def test_main_admin_403_even_with_linked_agent(env):
    """본인 배지가 있는 설계사 계정을 잠시 메인 관리자로 올린다. 메인 관리자는 내 명함 화면이 없어 여기서도 바꾸지 않는다."""
    con, team = env["con"], env["team"]
    _reset(con, team)
    con.execute("UPDATE user_account SET is_main_admin=1 WHERE login_id=?", (env["login"]["team"],))
    con.commit()
    try:
        c = login(env, "team")
        assert c.post("/perf/hall", data=_form(TEAM_KEY)).status_code == 403
        assert 'id="mine"' not in c.get("/perf/hall").text
        assert _pub(con, team) == {}
    finally:
        con.execute("UPDATE user_account SET is_main_admin=0 WHERE login_id=?", (env["login"]["team"],))
        con.commit()


# ---------------------------------------------------------------- 7. 안내 문구: 세 역할의 메뉴 화면 전부
def _menu_paths(env, who):
    ctx = auth.context(env["con"], env["uid"][env["login"][who]])
    return ctx["role"], [it["path"] for g in menu.for_ctx(ctx) for it in g["items"]]


@pytest.mark.parametrize("who", ["main", "manager", "team"])
def test_ref_footer_on_every_menu_screen(env, who):
    role, paths = _menu_paths(env, who)
    assert len(paths) >= 10, (who, len(paths))
    c = login(env, who)
    bad, seen = [], 0
    for p in paths:
        r = c.get(p)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            bad.append((p, r.status_code))
            continue
        seen += 1
        html = r.text
        if html.count('data-note="ref-foot"') != 1 or html.count(FOOT) != 1:
            bad.append((p, "footer"))
            continue
        tail = html.split("</main>", 1)[1]
        if not tail.lstrip().startswith(FOOT):
            bad.append((p, "footer 가 main 바로 뒤가 아님"))
        # 다른 꼴의 문구가 없다. 참고용이라는 말이 나오는 곳은 모두 정확히 같은 글이거나 다른 뜻의 문구다
        if html.count("참고용이며") != html.count(REF):
            bad.append((p, "문구가 다른 꼴"))
        # 숫자 가까이의 안내도 같은 글이다
        for m in re.finditer(r'<p class="cap ref-note" data-note="ref"[^>]*>(.*?)</p>', html):
            if m.group(1) != REF:
                bad.append((p, "숫자 가까이의 문구가 다름"))
        # main 밖에서 footer 뒤에는 아래 탭과 더보기와 스크립트만 있다. 숫자 토큰이 새로 생기지 않는다
        after = tail.lstrip()[len(FOOT):]
        if re.search(r"\d{1,3}(,\d{3})+\s*원|\d+\s*원<", after):
            bad.append((p, "main 밖에 금액 꼴"))
    assert seen >= 10
    assert not bad, (role, bad)


def test_d17_activity_page_outside_main_is_only_the_note(env):
    """footer 를 main 밖에 둔 까닭이 된 D-17 시험(tests/test_sales.py)은 main 안만 본다. 관리자의 활동 깔때기 화면에서
    main 밖에 수수료라는 말이 나오는 곳이 footer 의 안내 한 곳뿐이고, 그 안내에는 숫자가 없음을 함께 본다."""
    c = login(env, "manager")
    r = c.get("/team/activity")
    assert r.status_code == 200
    html = r.text
    head, rest = html.split('<main class="wrap">', 1)
    body, tail = rest.split("</main>", 1)
    assert "수수료" not in body and "원</" not in body
    assert tail.count(FOOT) == 1
    # footer 를 뺀 main 밖의 수수료라는 말은 메뉴의 이름뿐이다
    rest = tail.replace(FOOT, "")
    names = {it["name"] for g in menu.load()["groups"] for it in g["items"]} | {g["name"] for g in menu.load()["groups"]}
    for name in names:
        rest = rest.replace(f">{name}<", "><")
    assert "수수료" not in re.sub(r"<[^>]+>", "", rest), "main 밖에 메뉴 이름이 아닌 수수료라는 말"
    assert not re.search(r"\d", REF)
