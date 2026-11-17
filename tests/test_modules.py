"""모듈 켜고 끄기. 기본 상품은 프로모션 달성관리(core)이고 나머지는 추가기능 모듈이다.

config/menu.yaml 의 modules 와 환경 변수 APP_MODULES_OFF 로 끈다. 꺼진 모듈의 항목은 메뉴, 사이드바, 더보기, 탭에서 빠지고
guard 가 404 로 막는다. core 는 끌 수 없다. 기존 권한 검사는 그대로다.
"""
import logging
import os
import pathlib
import re
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db, masters
from app.web import auth, menu

PW = "test-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
ALL = {"core", "perf", "team", "edu", "sales", "settle", "ai"}


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


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(menu.MODULES_ENV, raising=False)
    menu._off_from.cache_clear()
    yield
    menu._off_from.cache_clear()


def logins(env):
    con = env["con"]
    manager = con.execute("""SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id
                             WHERE o.level='branch' AND m.effective_to IS NULL LIMIT 1""").fetchone()[0]
    agent = con.execute("SELECT internal_code FROM agent WHERE status='active' AND agent_id NOT IN (SELECT agent_id FROM org_manager) LIMIT 1").fetchone()[0]
    return {"main": "admin", "manager": manager, "agent": agent}


def login(env, login_id):
    c = TestClient(env["app"], follow_redirects=False)
    tok = TOKEN.search(c.get("/login").text).group(1)
    assert c.post("/login", data={"login_id": login_id, "password": PW, "csrf_token": tok}).status_code == 303
    return c


def items():
    return [it for g in menu.load()["groups"] for it in g["items"]]


def opens(it, role):
    """권한만으로 정해지는 응답. 시험의 관리자에게는 명함 승인 권한을 켜지 않았다(test_web_access 와 같다)."""
    return 200 if role in it["roles"] and (role == "main" or not it.get("requires")) else 403


def chrome_links(html):
    """화면 틀의 링크: 사이드바, 아래 탭, 더보기, 묶음 안 알약 줄."""
    side = re.search(r'<nav class="nav-groups"[^>]*>(.*?)</nav>', html, re.S).group(1)
    tabs = re.search(r'<nav class="tabbar"[^>]*>(.*?)</nav>', html, re.S).group(1)
    sheet = re.search(r'<div class="sheet" id="sheet"[^>]*>(.*)</div>\s*<script>', html, re.S).group(1)
    sub = re.search(r'<nav class="subnav"[^>]*>(.*?)</nav>', html, re.S)
    out = {}
    for name, part in (("side", side), ("tabs", tabs), ("sheet", sheet), ("sub", sub.group(1) if sub else "")):
        out[name] = set(re.findall(r'href="([^"?#]+)', part))
    return out


# ---------------------------------------------------------------- 파일의 모듈 배정
def test_module_assignment_follows_the_plan():
    cfg = menu.load()
    assert set(cfg["modules"]) == ALL
    assert all(m["enabled"] is True and m["name"] for m in cfg["modules"].values())
    assert cfg["modules"]["core"]["name"] == "프로모션 달성관리"
    by = {it["id"]: it["module"] for it in items()}
    groups = {g["id"]: g["module"] for g in cfg["groups"]}
    assert groups == {"home": "core", "promo": "core", "my": "edu", "perf": "perf", "team": "team", "sales": "sales", "edu": "edu",
                      "program": "edu", "data": "core", "settle": "settle", "org": "core", "review": "ai", "common": "core"}
    assert {i for i, m in by.items() if m == "perf" and i.split("-")[0] != "perf"} == {"my-summary", "my-persistency", "data-person-perf"}
    assert by["data-plan-qa"] == by["org-ai"] == "ai" and by["data-courses"] == "edu" and by["data-statement"] == "settle"
    assert {by[i] for i in ("my-education", "my-career", "my-cert")} == {"edu"}
    promo = next(g for g in cfg["groups"] if g["id"] == "promo")
    assert all(it["module"] == "core" for it in promo["items"] if it["id"] != "data-plan-qa")
    # 항목에 따로 적는 것은 묶음과 다를 때뿐이다
    raw = menu.yaml.safe_load(menu.MENU_FILE.read_text(encoding="utf-8"))
    assert all(it.get("module") in (None,) or it["module"] != g["module"] for g in raw["groups"] for it in g["items"])


# ---------------------------------------------------------------- 기본은 모두 켜짐
def test_default_is_all_on(env):
    assert menu.modules_off() == frozenset()
    assert all(menu.item_on(it) for it in items())
    for role, login_id in logins(env).items():
        uid = env["con"].execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0]
        ctx = auth.context(env["con"], uid)
        shown = {it["id"] for g in menu.for_ctx(ctx) for it in g["items"]}
        assert shown == {it["id"] for it in items() if menu.allowed(it, ctx)}, role
        assert len(menu.tabs_for(ctx)) == 4, role


# ---------------------------------------------------------------- perf, sales 를 끄면
@pytest.mark.parametrize("role", ["agent", "manager", "main"])
def test_off_modules_leave_menus_and_return_404(env, role, monkeypatch):
    monkeypatch.setenv(menu.MODULES_ENV, "perf, sales")
    assert menu.modules_off() == {"perf", "sales"}
    off = {it["path"] for it in items() if it["module"] in ("perf", "sales")}
    assert {"/perf/period", "/my/summary", "/my/persistency", "/data/person-perf", "/sales/leads", "/sales/card-approve"} <= off
    c = login(env, logins(env)[role])
    for it in items():
        r = c.get(it["path"])
        want = 404 if it["path"] in off else opens(it, role)       # 꺼진 모듈은 역할과 상관없이 404, 나머지는 권한 검사 그대로
        assert r.status_code == want, (role, it["path"], r.status_code)
    for path in ("/", "/promo/list", "/my/education", "/promo/progress"):
        if c.get(path).status_code != 200:
            continue
        html = c.get(path).text
        for name, links in chrome_links(html).items():
            assert not links & off, (role, path, name, links & off)
    home = c.get("/").text
    assert not set(re.findall(r'href="([^"?#]+)', home)) & off, role            # 홈의 카드와 링크도 꺼진 화면을 가리키지 않는다
    assert "/sales/card" not in home and "/sales/card-approve" not in home


def test_home_hides_todo_cards_of_off_modules(env, monkeypatch):
    """홈의 할 일 카드 가운데 명함 승인 권한 요청은 영업과 모바일 명함 모듈이다. 끄면 카드를 만들지 않는다."""
    c = login(env, "admin")
    assert "명함 승인 권한 요청" in c.get("/").text                                  # 샘플 계정에는 대기 중인 요청이 있다
    monkeypatch.setenv(menu.MODULES_ENV, "sales")
    assert "명함 승인 권한 요청" not in c.get("/").text


def test_off_modules_keep_the_my_group_and_tabs(env, monkeypatch):
    """내 진척 묶음은 실적 화면 둘이 빠져도 교육 화면이 남는다. 탭은 모두 core 라 그대로다."""
    monkeypatch.setenv(menu.MODULES_ENV, "perf,sales")
    c = login(env, logins(env)["agent"])
    html = c.get("/").text
    links = chrome_links(html)
    assert "/my/education" in links["sheet"] and "/my/summary" not in links["sheet"]
    assert re.findall(r'href="([^"]+)"', re.search(r'<nav class="tabbar"[^>]*>(.*?)</nav>', html, re.S).group(1)) == \
        ["/", "/promo/list", "/promo/progress", "/notifications"]
    assert 'id="more-sales"' not in html and 'id="more-perf"' not in html


def test_tab_of_an_off_module_is_skipped(env, monkeypatch):
    """탭 항목의 모듈이 꺼져 있으면 그 칸을 건너뛴다."""
    cfg = menu.load()
    saved = cfg["mobile_tabs"]["agent"]
    try:
        cfg["mobile_tabs"]["agent"] = [{"id": "home"}, {"id": "my-summary", "name": "실적"}, {"id": "promo-list", "name": "프로모션"}]
        monkeypatch.setenv(menu.MODULES_ENV, "perf")
        html = login(env, logins(env)["agent"]).get("/").text
        tabs = re.search(r'<nav class="tabbar"[^>]*>(.*?)</nav>', html, re.S).group(1)
        assert re.findall(r'href="([^"]+)"', tabs) == ["/", "/promo/list"]
    finally:
        cfg["mobile_tabs"]["agent"] = saved


def test_permission_checks_are_unchanged_when_modules_are_off(env, monkeypatch):
    monkeypatch.setenv(menu.MODULES_ENV, "perf,sales")
    who = logins(env)
    agent = login(env, who["agent"])
    before = env["con"].execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied'").fetchone()[0]
    assert agent.get("/data/contracts").status_code == 403                        # 켜진 모듈의 남의 화면은 그대로 403 이고 기록된다
    assert agent.get("/promo/publish").status_code == 403
    after = env["con"].execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied'").fetchone()[0]
    assert after == before + 2
    assert agent.get("/perf/period").status_code == 404                           # 꺼진 모듈은 없는 화면이다
    assert agent.get("/team/members").status_code == 403                          # team 은 켜져 있고 설계사에게 닫혀 있다
    assert login(env, who["manager"]).get("/team/members").status_code == 200


# ---------------------------------------------------------------- core 는 끌 수 없다
def test_core_cannot_be_turned_off(env, monkeypatch, caplog):
    monkeypatch.setenv(menu.MODULES_ENV, "core,ai,nope")
    with caplog.at_level(logging.WARNING, logger=menu.__name__):
        assert menu.modules_off() == {"ai"}
    assert any("core" in r.getMessage() for r in caplog.records)
    assert any("nope" in r.getMessage() for r in caplog.records)
    c = login(env, "admin")
    for path in ("/", "/promo/list", "/promo/progress", "/data/plan", "/promo/archive", "/org/tree", "/notifications", "/me"):
        assert c.get(path).status_code == 200, path
    assert c.get("/data/plan-qa").status_code == 404 and c.get("/org/ai").status_code == 404 and c.get("/review/requests").status_code == 404


def test_enabled_false_in_file_turns_off_but_not_core(monkeypatch):
    mods = menu.load()["modules"]
    saved = {k: dict(v) for k, v in mods.items()}
    try:
        mods["settle"]["enabled"] = False
        mods["core"]["enabled"] = False
        menu._off_from.cache_clear()
        assert menu.modules_off() == {"settle"}
        assert not menu.item_on(menu.find("/settle/close")) and not menu.item_on(menu.find("/data/statement"))
        assert menu.item_on(menu.find("/promo/list"))
    finally:
        for k, v in saved.items():
            mods[k].clear()
            mods[k].update(v)
        menu._off_from.cache_clear()


def test_only_core_left(env, monkeypatch):
    """추가기능을 모두 끄면 기본 상품만 남는다. 세 역할 모두 탭 네 칸이 그대로이고 화면 틀에 core 밖의 주소가 없다."""
    monkeypatch.setenv(menu.MODULES_ENV, "perf,team,edu,sales,settle,ai")
    core = {it["path"] for it in items() if it["module"] == "core"}
    for role, login_id in logins(env).items():
        html = login(env, login_id).get("/").text
        links = chrome_links(html)
        assert (links["side"] | links["sheet"] | links["tabs"]) <= core | {"/logout"}, role
        assert len(links["tabs"]) == 4, role
