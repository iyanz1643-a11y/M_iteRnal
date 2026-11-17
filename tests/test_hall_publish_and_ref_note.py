"""명예의전당의 내 수상과 자격 공개 스위치와, 참고용 안내 문구의 시험.

- 공개 스위치: /perf/hall POST. guard 와 CSRF 를 거치고 본인 항목만 바꾼다. 남의 항목, 없는 항목은 거절한다.
  내 명함(/sales/card)의 토글과 같은 card_badges.set_publish 를 불러 결과가 같다. 영업 모듈을 꺼도 동작한다. D-77, D-80
- 참고용 안내: 로그인한 화면 맨 아래에 한 번(base.html), 금액이나 예상액, 달성 여부가 나오는 화면에는 숫자 가까이 한 번 더.
  로그인 전 화면(로그인, 초대, 상담 신청, 소개)에는 없다.

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. 실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import os
import pathlib
import re
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db
from app.connect import card_badges as cb
from app.connect import card_site
from app.web import auth, menu

PW = "hall-publish-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
REF = "이 자료는 참고용이며 정확한 계산은 수수료 담당 부서 또는 유관부서에 문의해 주세요."
MDRT_A, MDRT_B = "cert:MDRT:2019", "cert:MDRT:2020"       # 샘플에 없는 연도라 본인에게만 있다


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리킨다. csrf=False 가 아니면 폼에 세션의 CSRF 토큰을 붙인다."""

    def __init__(self, app, db_path):
        super().__init__(app, follow_redirects=False)
        self.db_path = str(db_path)

    def request(self, *a, **kw):
        os.environ["APP_DB"] = self.db_path
        return super().request(*a, **kw)

    def post(self, url, data=None, csrf=True, **kw):
        data = dict(data or {})
        if csrf:
            data.setdefault("csrf_token", TOKEN.search(self.get("/login").text).group(1))
        return super().post(url, data=data, **kw)


def _program(con, code):
    return con.execute("SELECT program_id FROM certification_program WHERE code=?", (code,)).fetchone()[0]


@pytest.fixture(scope="module")
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="hallpub-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR", "CARD_SITE_WRITE", "ANTHROPIC_API_KEY",
            "APP_CONSULT_OPEN", card_site.ENV_URL, card_site.ENV_KEY, card_site.ENV_WRITE, menu.MODULES_ENV)
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    for k in keys[5:]:
        os.environ.pop(k, None)                       # 명함 사이트 키가 없어 내 명함 화면은 사이트를 부르지 않는다
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    users = {r[0]: r[1] for r in con.execute("""SELECT u.agent_id, u.login_id FROM user_account u JOIN agent a ON a.agent_id=u.agent_id
                                                 WHERE u.status='active' AND a.status='active' ORDER BY u.agent_id""")}
    plain = [a for a in users if a not in heads]
    me, other = plain[0], plain[1]
    manager = next(a for a in users if a in heads)
    assert not con.execute("SELECT 1 FROM agent_certification_award WHERE year IN (2019, 2020)").fetchone()
    for year in (2019, 2020):                          # 본인에게만 유효한 배지 둘을 둔다. 판정은 날짜를 쓰지 않는다
        con.execute("INSERT INTO agent_certification_award(agent_id, program_id, year, awarded_at) VALUES(?,?,?,?)",
                    (me, _program(con, "MDRT"), year, f"{year}-12-01"))
    con.commit()
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "con": con, "app": app, "me": me, "other": other, "manager": manager,
           "login": {"me": users[me], "other": users[other], "manager": users[manager], "main": "admin"}}
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


def _reset(con, agent_id):
    cb.ensure_publish(con)
    con.execute("DELETE FROM card_publish WHERE agent_id=?", (agent_id,))
    con.commit()


def _audit(con):
    return [dict(r) for r in con.execute("SELECT actor, target, before_json, after_json FROM audit_log WHERE action=? ORDER BY id",
                                         (cb.AUDIT_PUBLISH,))]


def _mine_row(html, key) -> str:
    """내 수상과 자격 구획에서 그 항목의 줄. 없으면 빈 글이다."""
    if 'id="mine"' not in html:
        return ""
    sec = html[html.index('id="mine"'):]
    i = sec.find(f'data-mine-key="{key}"')
    if i < 0:
        return ""
    j = sec.find('<div class="req"', i + 1)
    return sec[i:j if j > 0 else len(sec)]


def _card_row(html, label) -> str:
    sec = html[html.index('id="badges"'):]
    i = sec.index(f'<span class="strong">{label}</span>')
    j = sec.find('<div class="req"', i)
    return sec[i:j if j > 0 else len(sec)]


FORM = {"action": "publish", "item_key": MDRT_A, "published": "1"}


# ---------------------------------------------------------------- 1. 내 수상과 자격 구획은 본인 것만
def test_mine_section_shows_only_own_items_with_switches(env):
    html = login(env, "me").get("/perf/hall").text
    assert 'id="mine"' in html and "내 수상과 자격" in html
    for key in (MDRT_A, MDRT_B):
        row = _mine_row(html, key)
        assert row and 'action="/perf/hall"' in row and 'name="csrf_token"' in row, key
        assert 'name="agent_id"' not in row, key                                # 설계사 번호는 폼으로 받지 않는다
    for who in ("other", "manager"):
        other = login(env, who).get("/perf/hall").text
        assert not _mine_row(other, MDRT_A) and not _mine_row(other, MDRT_B), who
    main = login(env, "main").get("/perf/hall").text
    assert 'id="mine"' not in main                                             # 메인 관리자는 내 명함 화면이 없어 여기서도 없다


# ---------------------------------------------------------------- 2. POST: 본인 것만, CSRF, 거절
def test_hall_post_changes_only_own_item_and_needs_csrf(env):
    con, me_id = env["con"], env["me"]
    _reset(con, me_id)
    me = login(env, "me")
    # CSRF 토큰이 없으면 403 이고 아무것도 바뀌지 않는다
    assert me.post("/perf/hall", data=FORM, csrf=False).status_code == 403
    assert me.post("/perf/hall", data={**FORM, "csrf_token": "x" * 43}, csrf=False).status_code == 403
    assert _pub(con, me_id) == {}
    # 본인이 공개로 바꾼다. 다른 항목은 그대로 비공개다
    r = me.post("/perf/hall", data=FORM)
    assert r.status_code == 303 and r.headers["location"] == "/perf/hall#mine"
    assert _pub(con, me_id) == {MDRT_A: 1}
    html = me.get("/perf/hall").text
    assert "공개로 바꿨습니다." in html
    assert 'aria-pressed="true">공개</button>' in _mine_row(html, MDRT_A)
    assert 'aria-pressed="true">비공개</button>' in _mine_row(html, MDRT_B)
    log = _audit(con)
    assert log[-1]["actor"] == env["login"]["me"] and log[-1]["target"] == f"agent:{me_id}"
    # 없는 항목과 자격이 없는 항목은 거절한다
    n = len(_audit(con))
    for key in ("cert:MDRT:1999", "award:NOPE:2025", "x" * 200):
        assert me.post("/perf/hall", data={**FORM, "item_key": key}).status_code == 303, key
        assert key not in _pub(con, me_id), key
    assert "자격이 확인된 항목만 공개할 수 있습니다." in me.get("/perf/hall").text
    assert len(_audit(con)) == n
    # 잘못된 요청은 400 이다
    for bad in ({**FORM, "published": "yes"}, {**FORM, "action": "remove"}, {**FORM, "item_key": ""}, {**FORM, "item_key": "x" * 201}):
        assert me.post("/perf/hall", data=bad).status_code == 400, bad["action"]
    # 남의 항목은 바꾸지 못한다. 설계사 번호를 폼에 실어도 세션의 본인 번호로만 간다
    for who in ("other", "manager"):
        c = login(env, who)
        r = c.post("/perf/hall", data={**FORM, "published": "0", "agent_id": str(me_id)})
        assert r.status_code == 303, who
        assert "이미 비공개입니다." in c.get("/perf/hall").text, who            # 본인 몫에는 그 항목이 없어 바뀐 것이 없다
        assert _pub(con, me_id) == {MDRT_A: 1}, who
        c.post("/perf/hall", data={**FORM, "published": "1", "agent_id": str(me_id)})
        assert MDRT_A not in _pub(con, env[who]), who
    # 메인 관리자는 403 이고, 엔진도 남의 것은 거절한다
    assert login(env, "main").post("/perf/hall", data={**FORM, "published": "0"}).status_code == 403
    assert cb.set_publish(con, me_id, MDRT_A, False, "admin").status == cb.DENIED
    assert cb.set_publish(con, me_id, MDRT_A, False, env["login"]["manager"]).status == cb.DENIED
    assert _pub(con, me_id) == {MDRT_A: 1}
    # 본인이 다시 비공개로 돌린다
    me.post("/perf/hall", data={**FORM, "published": "0"})
    assert _pub(con, me_id) == {MDRT_A: 0}


# ---------------------------------------------------------------- 3. 내 명함 토글과 결과가 같다
def test_hall_switch_matches_my_card_toggle(env):
    con, me_id = env["con"], env["me"]
    me = login(env, "me")
    outcomes = {}
    for path in ("/perf/hall", "/sales/card"):
        _reset(con, me_id)
        n = len(_audit(con))
        flashes = []
        for published in ("1", "1", "0"):                     # 바꿈, 그대로, 되돌림
            r = me.post(path, data={**FORM, "published": published})
            assert r.status_code == 303, path
            page = me.get(path).text
            flashes.append(next(t for t in ("공개로 바꿨습니다.", "이미 공개로 두셨습니다.", "비공개로 바꿨습니다.") if t in page))
        bad = me.post(path, data={**FORM, "item_key": "cert:MDRT:1999"})
        flashes.append("자격이 확인된 항목만 공개할 수 있습니다." in me.get(path).text and bad.status_code)
        log = [(x["actor"], x["target"], x["before_json"], x["after_json"]) for x in _audit(con)[n:]]
        outcomes[path] = (flashes, log, _pub(con, me_id))
    assert outcomes["/perf/hall"] == outcomes["/sales/card"]
    # 한쪽에서 바꾼 것이 다른 쪽에 그대로 보인다
    _reset(con, me_id)
    me.post("/perf/hall", data=FORM)
    assert 'aria-pressed="true">공개</button>' in _card_row(me.get("/sales/card").text, "MDRT 2019")
    me.post("/sales/card", data={**FORM, "item_key": MDRT_B})
    assert 'aria-pressed="true">공개</button>' in _mine_row(me.get("/perf/hall").text, MDRT_B)
    _reset(con, me_id)


# ---------------------------------------------------------------- 4. 영업 모듈을 꺼도 동작한다
@pytest.mark.parametrize("off", ["sales", "perf,team,edu,sales,settle,ai"])
def test_hall_switch_works_with_sales_module_off(env, monkeypatch, off):
    con, me_id = env["con"], env["me"]
    _reset(con, me_id)
    monkeypatch.setenv(menu.MODULES_ENV, off)
    me = login(env, "me")
    assert me.get("/sales/card").status_code == 404
    html = me.get("/perf/hall").text
    assert "/sales/card" not in html                                           # 꺼진 화면으로 가는 링크가 없다
    assert _mine_row(html, MDRT_A) and 'href="#mine"' in html
    assert me.post("/perf/hall", data=FORM).status_code == 303
    assert _pub(con, me_id) == {MDRT_A: 1}
    assert 'aria-pressed="true">공개</button>' in _mine_row(me.get("/perf/hall").text, MDRT_A)
    assert me.post("/perf/hall", data={**FORM, "published": "0"}).status_code == 303
    assert _pub(con, me_id) == {MDRT_A: 0}
    assert me.post("/perf/hall", data=FORM, csrf=False).status_code == 403
    assert me.post("/sales/card", data=FORM).status_code == 404
    assert _pub(con, me_id) == {MDRT_A: 0}


def test_hall_keeps_my_card_link_when_sales_is_on(env):
    html = login(env, "me").get("/perf/hall").text
    assert 'href="/sales/card#badges"' in html


# ---------------------------------------------------------------- 5. 참고용 안내 문구
NUMBER_PAGES = {
    "agent": ["/", "/promo/progress", "/my/awards", "/promo/archive", "/promo/compare", "/perf/hall", "/settle/commission",
              "/settle/clawback", "/my/persistency"],
    "manager": ["/", "/promo/progress", "/promo/progress?view=me", "/my/awards", "/perf/hall", "/settle/commission"],
    "main": ["/", "/promo/progress", "/promo/archive", "/promo/compare", "/perf/hall", "/settle/commission", "/settle/clawback"],
}
PLAIN_PAGES = ["/notifications", "/calendar", "/board", "/me", "/promo/list"]
WHO = {"agent": "me", "manager": "manager", "main": "main"}


def _foot_last(html):
    """안내가 본문(main) 바로 다음의 footer 에 한 번 있다. 아래 탭 막대 몫의 여백은 이 footer 가 맡는다."""
    return re.search(r'</main>\s*<footer class="ref-foot" data-note="ref-foot">' + re.escape(REF) + "</footer>", html) is not None


@pytest.mark.parametrize("role", ["agent", "manager", "main"])
def test_ref_note_on_logged_in_pages(env, role):
    c = login(env, WHO[role])
    for p in NUMBER_PAGES[role]:
        r = c.get(p)
        assert r.status_code == 200, (role, p, r.status_code)
        assert r.text.count('data-note="ref-foot"') == 1, (role, p)
        assert _foot_last(r.text), (role, p)
        assert r.text.count(REF) == 2 and 'data-note="ref"' in r.text, (role, p, r.text.count(REF))   # 맨 아래 한 번과 숫자 가까이 한 번
    for p in PLAIN_PAGES:
        r = c.get(p)
        assert r.status_code == 200 and r.text.count(REF) == 1 and _foot_last(r.text), (role, p)


@pytest.mark.parametrize("path", ["/login", "/welcome", "/invite", "/invite?t=nope", "/consult"])
def test_ref_note_not_on_pages_before_login(env, monkeypatch, path):
    monkeypatch.setenv("APP_CONSULT_OPEN", "1")
    c = Client(env["app"], env["db"])
    r = c.get(path)
    assert r.status_code in (200, 303, 410), (path, r.status_code)
    assert REF not in r.text and "참고용이며" not in r.text, path


def test_ref_note_text_lives_in_one_place():
    root = db.ROOT / "app" / "web" / "templates"
    hits = [f.name for f in root.glob("*.html") if REF in f.read_text(encoding="utf-8")]
    assert hits == ["_ui.html"], hits
