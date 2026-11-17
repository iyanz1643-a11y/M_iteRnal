"""메뉴 개편(프로모션 중심)과 모듈 켜고 끄기의 검증. verifier 가 만든 쪽과 따로 쓴 시험이다.

- 메뉴: 항목 69개의 id, 경로, 역할, requires 가 개편 전과 같다. 개편 전 값은 menu.yaml 을 읽지 않고 여기에 적었다.
- 모듈: 모듈 판정은 항목 단위이고 경로 접두어가 아니다. 알 수 없는 키는 무시한다. 꺼진 모듈은 쓰기 요청도 404 이고 아무것도 쓰지 않는다.
- 권한: 모듈 하나를 꺼도 켜진 화면의 응답 코드는 그대로다. 범위 안 설계사 목록과 화면에 보이는 사람이 늘지 않는다.
- 링크: 모두 끄면 남은 화면 어디에도 꺼진 화면으로 가는 링크가 없다.

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. 실패 메시지에는 사람 이름과 금액을 넣지 않는다. 건수와 주소만 넣는다.
"""
import os
import pathlib
import re
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db
from app.sales import core
from app.web import auth, menu

PW = "verify-modules-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
ADDONS = ("perf", "team", "edu", "sales", "settle", "ai")

# 개편 전(HEAD f258028) 메뉴의 항목. id: (경로, 역할별 범위, requires). 개편은 묶음 소속과 module 만 바꾼다
FROZEN = {
    "home": ("/", {"main": "all", "manager": "org", "agent": "self"}, None),
    "my-summary": ("/my/summary", {"manager": "self", "agent": "self"}, None),
    "my-awards": ("/my/awards", {"manager": "self", "agent": "self"}, None),
    "my-persistency": ("/my/persistency", {"manager": "self", "agent": "self"}, None),
    "my-education": ("/my/education", {"manager": "self", "agent": "self"}, None),
    "my-career": ("/my/career", {"manager": "self", "agent": "self"}, None),
    "my-cert": ("/my/cert", {"manager": "self", "agent": "self"}, None),
    "promo-list": ("/promo/list", {"main": "all", "manager": "org", "agent": "self"}, None),
    "promo-progress": ("/promo/progress", {"main": "all", "manager": "org", "agent": "self"}, None),
    "promo-org-awards": ("/promo/org-awards", {"main": "all", "manager": "org", "agent": "self"}, None),
    "promo-compare": ("/promo/compare", {"main": "all", "manager": "org", "agent": "self"}, None),
    "promo-archive": ("/promo/archive", {"main": "all", "manager": "org", "agent": "self"}, None),
    "promo-publish": ("/promo/publish", {"main": "all"}, None),
    "promo-tiers": ("/promo/tiers", {"main": "all"}, None),
    "perf-period": ("/perf/period", {"main": "all", "manager": "org", "agent": "self"}, None),
    "perf-category": ("/perf/category", {"main": "all", "manager": "org", "agent": "self"}, None),
    "perf-contracts": ("/perf/contracts", {"main": "all", "manager": "org", "agent": "self"}, None),
    "perf-persistency": ("/perf/persistency", {"main": "all", "manager": "org"}, None),
    "perf-ranking": ("/perf/ranking", {"main": "all", "manager": "org", "agent": "self"}, None),
    "perf-hall": ("/perf/hall", {"main": "all", "manager": "org", "agent": "self"}, None),
    "perf-org-compare": ("/perf/org-compare", {"main": "all", "manager": "org"}, None),
    "sales-leads": ("/sales/leads", {"manager": "self", "agent": "self"}, None),
    "sales-assign": ("/sales/assign", {"main": "all", "manager": "org"}, None),
    "sales-customers": ("/sales/customers", {"manager": "self", "agent": "self"}, None),
    "sales-guide": ("/sales/guide", {"main": "all", "manager": "org", "agent": "self"}, None),
    "sales-report": ("/sales/report", {"manager": "self", "agent": "self"}, None),
    "sales-cardnews": ("/sales/cardnews", {"main": "all"}, None),
    "sales-card": ("/sales/card", {"manager": "self", "agent": "self"}, None),
    "sales-card-approve": ("/sales/card-approve", {"main": "all", "manager": "org"}, "card_approver"),
    "team-members": ("/team/members", {"main": "all", "manager": "org"}, None),
    "team-education": ("/team/education", {"main": "all", "manager": "org"}, None),
    "team-onboarding": ("/team/onboarding", {"main": "all", "manager": "org"}, None),
    "team-coaching": ("/team/coaching", {"manager": "org"}, None),
    "team-activity": ("/team/activity", {"main": "all", "manager": "org"}, None),
    "edu-courses": ("/edu/courses", {"main": "all", "manager": "org", "agent": "self"}, None),
    "edu-sessions": ("/edu/sessions", {"main": "all", "manager": "org", "agent": "self"}, None),
    "edu-completions": ("/edu/completions", {"main": "all", "manager": "org"}, None),
    "program-career": ("/program/career", {"main": "all", "manager": "org"}, None),
    "program-cert": ("/program/cert", {"main": "all", "manager": "org"}, None),
    "program-executive": ("/program/executive", {"main": "all", "manager": "org"}, None),
    "program-excellence": ("/program/excellence", {"main": "all", "manager": "org"}, None),
    "data-contracts": ("/data/contracts", {"main": "all"}, None),
    "data-staff": ("/data/staff", {"main": "all"}, None),
    "data-person-perf": ("/data/person-perf", {"main": "all"}, None),
    "data-plan": ("/data/plan", {"main": "all"}, None),
    "data-plan-qa": ("/data/plan-qa", {"main": "all"}, None),
    "data-courses": ("/data/courses", {"main": "all"}, None),
    "data-statement": ("/data/statement", {"main": "all"}, None),
    "data-quality": ("/data/quality", {"main": "all"}, None),
    "data-batches": ("/data/batches", {"main": "all"}, None),
    "data-jobs": ("/data/jobs", {"main": "all"}, None),
    "settle-commission": ("/settle/commission", {"main": "all", "manager": "self", "agent": "self"}, None),
    "settle-clawback": ("/settle/clawback", {"main": "all", "manager": "self", "agent": "self"}, None),
    "settle-recon": ("/settle/recon", {"main": "all"}, None),
    "settle-adjust": ("/settle/adjust", {"main": "all"}, None),
    "settle-close": ("/settle/close", {"main": "all"}, None),
    "org-tree": ("/org/tree", {"main": "all", "manager": "org"}, None),
    "org-heads": ("/org/heads", {"main": "all"}, None),
    "org-accounts": ("/org/accounts", {"main": "all"}, None),
    "org-settings": ("/org/settings", {"main": "all", "manager": "org"}, None),
    "org-masters": ("/org/masters", {"main": "all"}, None),
    "org-audit": ("/org/audit", {"main": "all"}, None),
    "org-ai": ("/org/ai", {"main": "all"}, None),
    "review-requests": ("/review/requests", {"main": "all"}, None),
    "review-results": ("/review/results", {"main": "all"}, None),
    "notifications": ("/notifications", {"main": "all", "manager": "org", "agent": "self"}, None),
    "calendar": ("/calendar", {"main": "all", "manager": "org", "agent": "self"}, None),
    "board": ("/board", {"main": "all", "manager": "org", "agent": "self"}, None),
    "me": ("/me", {"main": "all", "manager": "self", "agent": "self"}, None),
}
ITEM_KEYS = {"id", "name", "path", "roles", "phase", "status", "desc", "requires", "module", "group"}


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리킨다. 폼에는 세션의 CSRF 토큰을 붙인다."""

    def __init__(self, app, db_path, **kw):
        super().__init__(app, follow_redirects=False, **kw)
        self.db_path = str(db_path)

    def request(self, *a, **kw):
        os.environ["APP_DB"] = self.db_path
        return super().request(*a, **kw)

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data.setdefault("csrf_token", TOKEN.search(self.get("/login").text).group(1))
        return super().post(url, data=data, **kw)


@pytest.fixture(scope="module")
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="modverify-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR", "CARD_SITE_WRITE",
            "ANTHROPIC_API_KEY", menu.MODULES_ENV)
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    for k in ("CARD_SITE_WRITE", "ANTHROPIC_API_KEY", menu.MODULES_ENV):
        os.environ.pop(k, None)
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    ym = auth.current_ym(con)
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    users = {r[0]: r[1] for r in con.execute("""SELECT u.agent_id, u.login_id FROM user_account u JOIN agent a ON a.agent_id=u.agent_id
                                                 WHERE u.status='active' AND a.status='active'""")}
    mgr = None
    for r in con.execute("""SELECT a.agent_id, a.internal_code code, m.org_id FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                            JOIN org_unit o ON o.org_id=m.org_id WHERE o.level='branch' AND m.effective_to IS NULL ORDER BY o.org_id"""):
        uid = con.execute("SELECT user_id FROM user_account WHERE agent_id=?", (r["agent_id"],)).fetchone()[0]
        mctx = auth.context(con, uid)
        direct = [a for a in sorted(mctx["agent_ids"] - {r["agent_id"]}) if a not in heads and a in users
                  and r["agent_id"] in core.direct_heads(con, a, ym)]
        if direct:
            mgr = {**dict(r), "uid": uid, "direct": direct}
            break
    assert mgr, "직속 팀원이 있는 지점장이 있어야 한다"
    who = {"main": "admin", "manager": mgr["code"], "agent": users[mgr["direct"][0]]}
    uids = {role: con.execute("SELECT user_id FROM user_account WHERE login_id=?", (lid,)).fetchone()[0] for role, lid in who.items()}
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "con": con, "app": app, "who": who, "uids": uids}
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


def login(env, role):
    c = Client(env["app"], env["db"])
    assert c.post("/login", data={"login_id": env["who"][role], "password": PW}).status_code == 303, role
    return c


def items():
    return [it for g in menu.load()["groups"] for it in g["items"]]


def main_body(html):
    m = re.search(r"<main[^>]*>(.*?)</main>", html, re.S)
    return m.group(1) if m else html


def statuses(c, paths):
    return {p: c.get(p).status_code for p in paths}


# ---------------------------------------------------------------- 1. 항목은 그대로, 바뀐 것은 묶음과 module 뿐
def test_items_keep_id_path_roles_requires():
    now = {it["id"]: (it["path"], it["roles"], it.get("requires")) for it in items()}
    assert set(now) == set(FROZEN), sorted(set(now) ^ set(FROZEN))
    changed = sorted(i for i in FROZEN if now[i] != FROZEN[i])
    assert not changed, changed
    assert len(items()) == 69


def test_items_carry_no_new_keys_but_module():
    raw = menu.yaml.safe_load(menu.MENU_FILE.read_text(encoding="utf-8"))
    extra = sorted({k for g in raw["groups"] for it in g["items"] for k in it} - ITEM_KEYS)
    assert not extra, extra


# ---------------------------------------------------------------- 2. 알 수 없는 키, 모양이 틀린 값
@pytest.mark.parametrize("raw", ["nope", "PERF", "Perf", "perf-x", " , ,", ",,,", "core", "core, CORE", "모듈"])
def test_unknown_or_malformed_keys_turn_nothing_off(env, monkeypatch, raw):
    monkeypatch.setenv(menu.MODULES_ENV, raw)
    assert menu.modules_off() == frozenset(), raw
    assert all(menu.item_on(it) for it in items()), raw
    c = login(env, "agent")
    assert statuses(c, ["/perf/period", "/my/summary", "/sales/leads", "/edu/courses", "/settle/commission"]) == \
        {p: 200 for p in ["/perf/period", "/my/summary", "/sales/leads", "/edu/courses", "/settle/commission"]}, raw


def test_spaces_around_known_keys_are_trimmed(monkeypatch):
    monkeypatch.setenv(menu.MODULES_ENV, "  perf ,\tai\t,, nope ")
    assert menu.modules_off() == {"perf", "ai"}


# ---------------------------------------------------------------- 3. 모듈은 항목 단위다. 경로 접두어로 정하지 않는다
@pytest.mark.parametrize("off,role,want", [
    ("perf", "agent", {"/my/awards": 200, "/perf/hall": 200, "/my/summary": 404, "/my/persistency": 404, "/perf/period": 404,
                       "/my/education": 200}),
    ("perf", "main", {"/data/contracts": 200, "/data/plan": 200, "/data/person-perf": 404, "/perf/hall": 200}),
    ("edu", "agent", {"/my/awards": 200, "/my/education": 404, "/my/career": 404, "/my/summary": 200, "/edu/courses": 404}),
    ("edu", "main", {"/data/courses": 404, "/data/staff": 200, "/program/career": 404}),
    ("ai", "main", {"/data/plan": 200, "/data/plan-qa": 404, "/org/ai": 404, "/org/tree": 200, "/review/results": 404}),
    ("settle", "main", {"/data/statement": 404, "/data/quality": 200, "/settle/close": 404}),
    ("sales", "manager", {"/sales/leads": 404, "/sales/card-approve": 404, "/promo/list": 200}),
])
def test_module_follows_the_item_not_the_path_prefix(env, monkeypatch, off, role, want):
    monkeypatch.setenv(menu.MODULES_ENV, off)
    assert statuses(login(env, role), list(want)) == want, (off, role)


@pytest.mark.parametrize("path", ["/perf/period/", "/PERF/period", "/perf/period/../hall", "/perf/hall/", "/my/awards/x", "/promo/list/"])
def test_off_modules_do_not_change_paths_outside_the_menu(env, monkeypatch, path):
    """메뉴에 없는 주소의 응답은 모듈을 꺼도 같다. 꺼진 화면 주소를 비틀어 켜진 화면처럼 열 수 없다."""
    c = login(env, "agent")
    before = c.get(path).status_code
    monkeypatch.setenv(menu.MODULES_ENV, "perf")
    after = c.get(path).status_code
    assert after == before, (path, before, after)


# ---------------------------------------------------------------- 4. 쓰기 요청
def _counts(con):
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {n: con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


@pytest.mark.parametrize("role,path", [("agent", "/sales/leads"), ("agent", "/sales/customers"), ("manager", "/team/coaching"),
                                       ("main", "/org/ai"), ("main", "/data/plan-qa"), ("main", "/settle/close"),
                                       ("main", "/data/person-perf"), ("main", "/program/career"), ("main", "/review/requests")])
def test_post_to_an_off_module_is_404_and_writes_nothing(env, monkeypatch, role, path):
    c = login(env, role)
    monkeypatch.setenv(menu.MODULES_ENV, ",".join(ADDONS))
    con = db.connect(env["db"])
    try:
        before = _counts(con)
        r = c.post(path, data={"action": "save", "note": "x", "q": "x"})
        assert r.status_code == 404, (role, path, r.status_code)
        after = _counts(con)
    finally:
        con.close()
    assert {k: (before[k], after[k]) for k in before if before[k] != after[k]} == {}, (role, path)


# ---------------------------------------------------------------- 5. 권한 코드는 모듈 하나를 꺼도 켜진 화면에서 그대로다
@pytest.fixture(scope="module")
def baseline(env):
    out = {}
    for role in ("main", "manager", "agent"):
        c = login(env, role)
        out[role] = {it["path"]: c.get(it["path"]).status_code for it in items()}
    return out


@pytest.mark.parametrize("off", ADDONS)
def test_turning_one_module_off_keeps_every_other_status(env, baseline, monkeypatch, off):
    monkeypatch.setenv(menu.MODULES_ENV, off)
    for role in ("main", "manager", "agent"):
        c = login(env, role)
        diff = {}
        for it in items():
            want = 404 if it["module"] == off else baseline[role][it["path"]]
            got = c.get(it["path"]).status_code
            if got != want:
                diff[it["path"]] = (baseline[role][it["path"]], got)
        assert not diff, (off, role, diff)
        assert {p for p, s in baseline[role].items() if s == 200} >= \
            {it["path"] for it in items() if menu.visible(it, auth.context(env["con"], env["uids"][role]))}, (off, role)


# ---------------------------------------------------------------- 6. 범위는 넓어지지 않는다
CORE_PAGES = ["/", "/promo/list", "/promo/progress", "/promo/org-awards", "/my/awards", "/perf/hall", "/promo/compare",
              "/promo/archive", "/notifications", "/calendar", "/board", "/me", "/org/tree", "/org/settings"]


@pytest.mark.parametrize("role", ["manager", "agent", "main"])
def test_scope_and_shown_people_do_not_grow_when_modules_are_off(env, monkeypatch, role):
    con = env["con"]
    names = {r[0] for r in con.execute("SELECT DISTINCT name FROM agent WHERE length(name) >= 2")}
    ctx_on = auth.context(con, env["uids"][role])
    c = login(env, role)
    shown_on = {}
    for p in CORE_PAGES:
        r = c.get(p)
        if r.status_code == 200:
            body = main_body(r.text)
            shown_on[p] = {n for n in names if n in body}
    assert sum(len(v) for v in shown_on.values()) > 1, role         # 비교할 사람이 화면에 있어야 시험이 뜻이 있다
    monkeypatch.setenv(menu.MODULES_ENV, ",".join(ADDONS))
    ctx_off = auth.context(con, env["uids"][role])
    assert ctx_off.get("agent_ids") == ctx_on.get("agent_ids"), role
    for p, before in shown_on.items():
        r = c.get(p)
        assert r.status_code == 200, (role, p, r.status_code)
        extra = {n for n in names if n in main_body(r.text)} - before
        assert not extra, (role, p, len(extra))              # 이름은 적지 않고 건수만 남긴다


# ---------------------------------------------------------------- 7. 꺼진 모듈의 화면으로 가는 링크가 남은 화면 어디에도 없다
# 검증 때 찾은 문제의 재현. 켜진 화면의 본문에 꺼진 화면 링크가 남아 누르면 404 다. 고치면 이 표에서 지운다.
# (꺼진 모듈, 역할): 남는 링크
DEAD_LINKS = {
    (("perf",), "main"): "/team/members 의 인별실적 링크 /data/person-perf",
    (("perf",), "agent"): "/my/career, /my/cert 의 실적 요약 링크 /my/summary",
    (("team",), "main"): "/edu/completions 의 교육 진척도 링크 /team/education",
    (("team",), "manager"): "/edu/completions 의 교육 진척도 링크 /team/education",
    (("sales",), "manager"): "/perf/hall 의 내 명함 링크 /sales/card. 공개 여부를 바꿀 곳도 없어진다",
    (("sales",), "agent"): "/perf/hall 의 내 명함 링크 /sales/card. 공개 여부를 바꿀 곳도 없어진다",
    (ADDONS, "manager"): "/perf/hall 의 내 명함 링크 /sales/card",
    (ADDONS, "agent"): "/perf/hall 의 내 명함 링크 /sales/card",
}
LINK_CASES = [pytest.param(o, r, id=f"{'+'.join(o)}-{r}",
                           marks=[pytest.mark.xfail(strict=True, reason=DEAD_LINKS[(o, r)])] if (o, r) in DEAD_LINKS else [])
              for o in [(m,) for m in ADDONS] + [ADDONS] for r in ("main", "manager", "agent")]


@pytest.mark.parametrize("off_set,role", LINK_CASES)
def test_open_pages_do_not_link_to_off_screens(env, monkeypatch, off_set, role):
    monkeypatch.setenv(menu.MODULES_ENV, ",".join(off_set))
    off = {it["path"] for it in items() if it["module"] in off_set}
    c = login(env, role)
    bad = {}
    for it in items():
        if it["module"] in off_set:
            continue
        r = c.get(it["path"])
        if r.status_code != 200:
            continue
        hrefs = set(re.findall(r'(?:href|action|formaction)="([^"?#]+)', r.text))
        if hrefs & off:
            bad[it["path"]] = sorted(hrefs & off)
    assert not bad, (off_set, role, bad)
