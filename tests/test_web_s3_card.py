"""S3 2차 화면 시험: 명함 연동 3차 화면, 비밀번호 규칙 통일, 조직별 설정 값의 화면 연결.

- 내 명함의 배지는 본인 것만 보인다. 상태 넷(유효, 만료 임박, 만료, 예정)과 재발급 안내, 예정은 올릴 수 없다는 안내
- 배지 만료 예정 목록과 해촉자 명함 내리기 후보는 메인 관리자만 본다
- 내리기는 메인 관리자만, 확인 단계와 CSRF 를 거친다. 쓰기가 꺼져 있으면 보내지 않고 안내한다. 결과 여덟 가지마다 존댓말이 있다
- 내 정보의 비밀번호 바꾸기는 초대 링크와 같은 password_error 를 쓴다
- 조직별 설정 값이 화면에 반영된다. 조직마다 다른 값이면 사람마다 소속 조직의 값이 보인다

명함 사이트는 httpx.MockTransport 로 흉내 낸다. 실제 네트워크로 나가는 호출은 막는다.
"""
import json
import os
import re
import secrets
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, masters
from app.connect import card_badges as cb
from app.connect import card_decide, card_remove, card_site
from app.connect.base import FAILED, NOT_CONFIGURED, OK, Outcome
from app.connect.card_link import CardLinkTable
from app.connect.state import StateStore
from app.engine import promotion as pm
from app.org import card_grant
from app.org import settings as st
from app.web import auth, invite
from app.web.routes import card as card_route
from app.web.routes import notify

PW = "card3-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
BASE = "https://card-site.test"
PATH = "/sales/card-approve"
TODAY = date(2031, 12, 10)                       # 배지 판정의 오늘. MDRT 2030 은 2031-12-31 까지라 만료 임박이다
GONE_SLUG = "card-gone"
PHONE = "-".join(("010", "1" * 4, "2" * 4))       # 컨텍스트 점검이 원문을 찾지 않도록 조각을 이어 만든다
REASON_OK = "위촉이 끝나 내립니다"


class FakeSite:
    """명함 사이트. 관리자 목록, 공개 상태, 내리기를 받는다. 받은 요청을 모두 남긴다."""

    def __init__(self, key):
        self.key = key
        self.pending = {"card-in": "2026-11-10T01:00:00Z"}
        self.state = {GONE_SLUG: "approved"}       # pending 이 아닌 주소의 상태
        self.requests = []
        self.bodies = []

    def removes(self):
        return [r for r in self.requests if r.method == "POST" and r.url.path == "/api/admin/remove"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/api/admin/") and request.headers.get("x-admin-key") != self.key:
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method == "GET" and path == "/api/admin/list":
            return httpx.Response(200, json={"pending": [{"slug": s, "name": "가짜", "phone": "", "at": at, "status": "pending", "files": ["index.html"],
                                                          "replaces": None, "conflict": False} for s, at in self.pending.items()],
                                             "decided": [], "live": []})
        if request.method == "GET" and path == "/api/status":
            slug = request.url.params.get("slug")
            if slug in self.pending:
                return httpx.Response(200, json={"slug": slug, "live": False, "status": "pending", "at": self.pending[slug]})
            s = self.state.get(slug, "none")
            return httpx.Response(200, json={"slug": slug, "live": s == "approved", "status": s, "reason": "", "decidedAt": None})
        if request.method == "POST" and path == "/api/admin/remove":
            body = json.loads(request.content or b"{}")
            self.bodies.append(body)
            self.pending.pop(body.get("slug"), None)
            self.state[body.get("slug")] = "removed"
            return httpx.Response(200, json={"ok": True})
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
    for k in (card_site.ENV_URL, card_site.ENV_KEY, card_site.ENV_WRITE):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CONNECT_STATE_DIR", str(tmp_path / "state"))

    def _no_network(self, request):
        raise AssertionError("시험이 실제 네트워크를 부르려 했습니다")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _no_network)
    card_route.clear_cache()
    notify.clear_cache()
    yield
    card_route.clear_cache()
    notify.clear_cache()
    card_decide._INFLIGHT.clear()
    card_remove._INFLIGHT.clear()


def _program(con, code):
    return con.execute("SELECT program_id FROM certification_program WHERE code=?", (code,)).fetchone()[0]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """지점장 한 명, 그 범위 안 설계사(inside), 범위 밖 설계사(outside), 해촉할 설계사(gone). 명함 주소 셋을 잇는다."""
    path = tmp_path / "card3.db"
    con = db.init_db(path)
    masters.seed(con)
    auth.seed_accounts(con, PW)
    uid = lambda login: con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login,)).fetchone()[0]
    manager = con.execute("""SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id
                             WHERE o.level='branch' ORDER BY a.internal_code LIMIT 1""").fetchone()[0]
    mctx = auth.context(con, uid(manager))
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    active = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' ORDER BY agent_id")]
    inside = next(a for a in active if a in mctx["agent_ids"] and a not in heads)
    outside = next(a for a in active if a not in mctx["agent_ids"] and a not in heads)
    gone = next(a for a in active if a not in (inside, outside) and a not in heads)
    names = {inside: "배지안설계사", outside: "범위밖설계사", gone: "해촉예정설계사"}
    for a, n in names.items():
        con.execute("UPDATE agent SET name=? WHERE agent_id=?", (n, a))
    links = CardLinkTable(con)
    links.link(inside, "card-in", by="admin")
    links.link(outside, "card-out", by="admin")
    links.link(gone, GONE_SLUG, by="admin")
    # 배지: MDRT 2030(만료 임박), MDRT 2031(유효), 우수인증 2029(만료), 영업우수 2031 계산 달성(예정)
    for code, year in (("MDRT", 2030), ("MDRT", 2031), ("EXCELLENT_FC_LIFE", 2029)):
        con.execute("INSERT INTO agent_certification_award(agent_id, program_id, year, awarded_at) VALUES(?,?,?,?)",
                    (inside, _program(con, code), year, f"{year}-12-01"))
    ym = auth.current_ym(con)                                          # 자료 기준 달을 바꾸지 않도록 같은 달의 마지막 run 으로 둔다
    run = con.execute("INSERT INTO calc_run(run_type, period_ym, started_at, status) VALUES('certification',?,?, 'done')", (ym, db.now())).lastrowid
    con.execute("""INSERT INTO agent_certification_status(run_id, agent_id, program_id, year, as_of_ym, metric_json, progress_rate, achieved)
                   VALUES(?,?,?,?,?,?,?,1)""", (run, inside, _program(con, "SALES_EXCELLENT"), 2031, ym, "{}", 1))
    con.execute("UPDATE agent SET status='terminated', termination_date='2026-11-01' WHERE agent_id=?", (gone,))
    con.commit()
    card_grant.grant(con, [manager], by="admin")
    code = lambda a: con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (a,)).fetchone()[0]
    key = secrets.token_hex(8)
    state = {"site": FakeSite(key), "write": True, "key": key}

    def fake_client(c, *, quick=False):
        return card_site.CardSiteClient(BASE, state["key"], transport=httpx.MockTransport(state["site"]), state=StateStore(con=c),
                                        attempts=1, write=state["write"], sleep=lambda s: None)
    monkeypatch.setattr(card_route, "card_client", fake_client)
    monkeypatch.setattr(card_route, "_today", lambda: TODAY)
    from app.web.main import app
    yield {"con": con, "path": path, "app": app, "state": state, "manager": manager, "mctx": mctx, "inside": inside, "outside": outside,
           "gone": gone, "names": names, "code_in": code(inside), "code_out": code(outside), "uid": uid}
    con.close()


def login(env, login_id, pw=PW):
    c = Client(env["app"], env["path"])
    r = c.post("/login", data={"login_id": login_id, "password": pw})
    assert r.status_code == 303, r.status_code
    return c


def _audit(con, action):
    return [dict(r) for r in con.execute("SELECT actor, target, after_json FROM audit_log WHERE action=? ORDER BY id", (action,))]


# ---------------------------------------------------------------- 내 명함의 배지
def test_my_card_badges_only_own_with_four_states_and_notice(env):
    html = login(env, env["code_in"]).get("/sales/card").text
    sec = html[html.index('id="badges"'):]
    states = dict(re.findall(r'<span class="strong">([^<]+)</span> <span class="chip [a-z]*">([^<]+)</span>', sec))
    assert states["MDRT 2030"] == "만료 임박"
    assert states["MDRT 2031"] == "유효"
    assert states["우수인증설계사 2029"] == "만료"
    assert states["2031 영업우수설계사"] == "예정"
    assert card_route.EXPECTED_NOTE in sec and "명함에 올릴 수 없습니다" in sec
    assert "MDRT 2030 배지의 유효기간이 2031-12-31 에 끝납니다" in sec and "연도를 2031 로 바꿔" in sec    # reissue_notice
    assert "우수인증설계사 2029 배지의 유효기간이 2030-12-31 에 끝났습니다" in sec
    assert "자격·수상에서 MDRT 를 고르고 연도에 2031 를 넣습니다" in sec
    assert "2031년 12월 10일 기준" in sec
    # 남의 배지는 보이지 않는다. 관리자도 본인 것만이다
    for login_id in (env["code_out"], env["manager"]):
        other = login(env, login_id).get("/sales/card").text
        assert "MDRT 2030" not in other and "영업우수설계사" not in other, login_id
    # 메인 관리자는 내 명함 화면이 없다
    assert login(env, "admin").get("/sales/card").status_code == 403


def test_my_badges_function_takes_one_person():
    """my_badges 는 넘긴 한 사람의 것만 묶는다. 설계사 번호가 없으면 None 이다."""
    assert card_route.my_badges(None, None) is None


# ---------------------------------------------------------------- 메인 관리자 전용 묶음
def test_expiring_and_removal_candidates_only_for_main(env):
    admin = login(env, "admin").get(PATH).text
    tools = admin[admin.index('id="main-tools"'):]
    expiring = tools[:tools.index('id="removal"')]
    removal = tools[tools.index('id="removal"'):]
    assert env["names"][env["inside"]] in expiring and "MDRT 2030" in expiring and "2031년 12월 31일까지 · 21일 남음" in expiring
    assert "MDRT 2031" not in expiring                                       # 유효한 배지는 만료 예정이 아니다
    assert env["names"][env["gone"]] in removal and GONE_SLUG in removal and "해촉" in removal
    assert env["names"][env["inside"]] not in removal
    # 관리자는 같은 화면을 열어도 묶음이 없고, 확인 단계도 열리지 않는다
    mgr = login(env, env["manager"])
    for url in (PATH, f"{PATH}?do=remove&slug={GONE_SLUG}", f"{PATH}?refresh=1"):
        html = mgr.get(url).text
        assert 'id="main-tools"' not in html and "내리기 후보" not in html and 'id="rm-confirm"' not in html, url
        assert env["names"][env["gone"]] not in html
    # 설계사는 화면 자체가 막힌다
    assert login(env, env["code_in"]).get(PATH).status_code == 403


def test_refresh_reads_status_by_get_only(env):
    site = env["state"]["site"]
    html = login(env, "admin").get(f"{PATH}?refresh=1").text
    assert "명함 사이트에서 다시 읽었습니다" in html
    assert any(r.method == "GET" and r.url.path == "/api/status" and r.url.params.get("slug") == GONE_SLUG for r in site.requests)
    assert not [r for r in site.requests if r.method == "POST"]


# ---------------------------------------------------------------- 내리기
def test_remove_needs_main_confirm_and_csrf_then_sends_once(env):
    site, con = env["state"]["site"], env["con"]
    form = {"action": "remove", "slug": GONE_SLUG, "reason": REASON_OK, "confirm": "1"}
    # 관리자는 403 이고 아무것도 보내지 않는다
    r = login(env, env["manager"]).post(PATH, data=form)
    assert r.status_code == 403
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied' AND target=?", (f"{PATH} remove",)).fetchone()[0] == 1
    admin = login(env, "admin")
    # CSRF 토큰이 없으면 403
    assert admin.post(PATH, data=form, csrf=False).status_code == 403
    # 확인 칸이 없으면 확인 단계로 돌린다
    r = admin.post(PATH, data={**form, "confirm": ""})
    assert r.status_code == 303 and r.headers["location"] == f"{PATH}?do=remove&slug={GONE_SLUG}#rm-confirm"
    assert not site.removes()
    # 확인 단계: 경고와 사유 안내
    html = admin.get(f"{PATH}?do=remove&slug={GONE_SLUG}").text
    box = html[html.index('id="rm-confirm"'):]
    box = box[:box.index("</section>")]
    assert "사유는 명함 사이트에서 누구나 볼 수 있습니다. 비워 두면 사이트 기본 문구가 들어갑니다" in box
    assert "되돌릴 수 없습니다" in box and 'name="confirm" value="1"' in box and env["names"][env["gone"]] in box
    # 사유에 전화번호 꼴이 있으면 보내지 않고 확인 단계를 다시 연다
    r = admin.post(PATH, data={**form, "reason": f"연락처 {PHONE}"})
    assert r.status_code == 400 and 'id="rm-confirm"' in r.text and "보내지 않았습니다" in r.text and PHONE not in r.text
    # 사유에 설계사 이름이 있어도 보내지 않는다
    r = admin.post(PATH, data={**form, "reason": f"{env['names'][env['gone']]} 해촉"})
    assert r.status_code == 400 and not site.removes()
    # 확인하고 보내면 한 번 보내고 결과를 존댓말로 보여 준다
    r = admin.post(PATH, data=form)
    assert r.status_code == 303 and r.headers["location"] == f"{PATH}#main-tools"
    assert len(site.removes()) == 1 and site.bodies[-1] == {"slug": GONE_SLUG, "reason": REASON_OK}
    after = admin.get(PATH).text
    assert card_route.REMOVE_LINE[OK] in after
    removal = after[after.index('id="removal"'):]
    assert GONE_SLUG not in removal.split("내리기는 메인 관리자만")[0]      # 후보에서 빠진다
    # 변경 기록에는 사유 원문이 없고 길이만 있다
    rows = _audit(con, "card_remove")
    assert rows and all(REASON_OK not in (x["after_json"] or "") and PHONE not in (x["after_json"] or "") for x in rows)
    assert json.loads(rows[-1]["after_json"])["reason_len"] == len(REASON_OK)
    assert rows[-1]["target"] == f"agent:{env['gone']}"
    # 변경 기록 화면의 이름표
    audit = admin.get("/org/audit").text
    assert "해촉자 명함 내리기" in audit


def test_remove_with_write_off_shows_notice_and_sends_nothing(env):
    env["state"]["write"] = False
    admin = login(env, "admin")
    html = admin.get(PATH).text
    removal = html[html.index('id="removal"'):]
    assert "쓰기가 아직 꺼져 있습니다" in removal
    r = admin.post(PATH, data={"action": "remove", "slug": GONE_SLUG, "reason": "", "confirm": "1"})
    assert r.status_code == 303
    assert card_route.REMOVE_LINE["write_off"] in admin.get(PATH).text
    assert not env["state"]["site"].removes()
    assert json.loads(_audit(env["con"], "card_remove")[-1]["after_json"])["result"] == "write_off"


def test_remove_not_candidate_has_no_confirm(env):
    html = login(env, "admin").get(f"{PATH}?do=remove&slug=card-in").text
    assert 'id="rm-confirm"' not in html and "내리기 후보에 없는 명함입니다" in html


def test_eight_remove_results_have_polite_lines():
    eight = (OK, card_decide.DENIED, card_decide.IN_FLIGHT, card_remove.NOT_LIVE, card_site.UNKNOWN, card_site.WRITE_OFF, NOT_CONFIGURED, FAILED)
    assert set(card_remove.REMOVE_KO) == set(eight) == set(card_route.REMOVE_LINE)
    kinds = set()
    for status in eight:
        f = card_route._remove_flash(Outcome(status, reason="엔진 설명"))
        assert f["text"] == card_route.REMOVE_LINE[status]
        assert re.search(r"(습니다|주세요)\.", f["text"]), status
        assert ("엔진 설명" == f["detail"]) == (status in (card_decide.DENIED, FAILED, card_site.UNKNOWN))
        kinds.add(f["kind"])
    assert kinds == {"ok", "warn", "bad"}


# ---------------------------------------------------------------- 비밀번호 규칙
def test_me_password_uses_invite_rule(env):
    con = env["con"]
    me = login(env, env["code_in"])
    cases = {
        "abcdefgh": "영문과 숫자를 함께 넣어 주세요",
        "abc123": f"{invite.PW_MIN}자 이상",
        f"key-{env['code_in'].lower()}-9": "사번이 들어간",
        "a1a1a1a1": "같은 글자를 되풀이한",
        " pass-1234 ": "앞뒤에 빈칸",
    }
    for pw, msg in cases.items():
        assert invite.password_error(pw, pw, env["code_in"]) is not None
        html = me.post("/me", data={"action": "password", "old": PW, "new": pw, "new2": pw}).text
        assert msg in html, pw
    assert "지금 것과 달라야" in me.post("/me", data={"action": "password", "old": PW, "new": PW, "new2": PW}).text
    assert "서로 다릅니다" in me.post("/me", data={"action": "password", "old": PW, "new": "fresh-pass-77", "new2": "fresh-pass-78"}).text
    assert "영문과 숫자를 함께" in me.get("/me").text                                   # 규칙을 화면에 적는다
    assert "바꿨습니다" in me.post("/me", data={"action": "password", "old": PW, "new": "fresh-pass-77", "new2": "fresh-pass-77"}).text
    # 계정 화면의 재설정은 초기 비밀번호를 주고 내 정보에서 같은 규칙으로 바꾸게 한다
    uid = env["uid"](env["code_out"])
    temp = auth.reset_password(con, uid)
    con.commit()
    c = Client(env["app"], env["path"])
    r = c.post("/login", data={"login_id": env["code_out"], "password": temp})
    assert r.status_code == 303 and r.headers["location"] == "/me"
    assert "영문과 숫자를 함께" in c.post("/me", data={"action": "password", "old": temp, "new": "abcdefghij", "new2": "abcdefghij"}).text
    assert "바꿨습니다" in c.post("/me", data={"action": "password", "old": temp, "new": "fresh-pass-99", "new2": "fresh-pass-99"}).text


# ---------------------------------------------------------------- 조직별 설정 값
def _set(env, org_id, key, value):
    con = env["con"]
    admin = auth.context(con, env["uid"]("admin"))
    st.set_value(con, org_id, key, value, st.first_open_ym(con), admin)


def _ctx(env, login_id):
    return auth.context(env["con"], env["uid"](login_id))


def _orgs(env):
    """inside 와 outside 의 소속 조직. 서로 다른 조직이어야 한다."""
    a, b = st.person_org(env["con"], _ctx(env, env["code_in"])), st.person_org(env["con"], _ctx(env, env["code_out"]))
    assert a != b
    return a, b


def test_home_cards_follow_each_persons_org(env):
    org_in, _ = _orgs(env)
    _set(env, org_in, "home.cards", ["goal", "team_top"])                     # 프로모션과 내 순위를 끈다
    mine = login(env, env["code_in"]).get("/").text
    other = login(env, env["code_out"]).get("/").text
    assert 'data-home="my_rank"' not in mine and 'data-home="promos"' not in mine
    assert 'data-home="my_rank"' in other and 'data-home="promos"' in other
    assert "이번 달 인정보험료" in mine                                       # 주인공 숫자는 끌 수 없다


def test_due_days_and_near_ratio_follow_each_persons_org(env):
    con = env["con"]
    org_in, org_out = _orgs(env)
    _set(env, org_in, "notify.promo_due_days", 7)
    _set(env, org_in, "promo.near_pct", 50)
    ym = auth.current_ym(con)
    ref = pm.ref_date(ym)
    day = lambda n: (ref + timedelta(days=n)).isoformat()
    pid = pm.create(con, period_ym=ym, name="설정 근접 시험", kind="weekly", windows=[("이번 주", day(-3), day(5))],
                    tiers=[(100_000, "fixed", 1, None)], by="t", publish=True)
    prod = con.execute("SELECT product_id, insurer_id FROM product ORDER BY product_id LIMIT 1").fetchone()
    for n, a in enumerate((env["inside"], env["outside"])):                   # 둘 다 다음 구간까지 40% 남는다
        con.execute("""INSERT INTO contract(insurer_id, policy_no, product_id, agent_id, application_no, application_date, contract_date, premium_monthly,
                                            converted_premium, policy_count, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,1,'inforce',?)""",
                    (prod["insurer_id"], f"S3C{n}", prod["product_id"], a, f"S3A{n}", day(-1), day(-1), 60_000, 60_000, db.now()))
    con.commit()
    notes = {}
    for who in ("code_in", "code_out"):
        notify.clear_cache()
        notes[who] = notify.collect(con, _ctx(env, env[who]))
    due = lambda ns: [n for n in ns if n["kind"] == "promo_due" and n["key"].startswith(f"promo_due:{pid}:")]
    near = lambda ns: [n for n in ns if n["kind"] == "tier_near" and n["key"].startswith(f"tier_near:{pid}:")]
    assert due(notes["code_in"]) and "마감 5일 전" in due(notes["code_in"])[0]["title"]      # 7일 조직
    assert not due(notes["code_out"])                                                         # 기본 3일 조직
    assert near(notes["code_in"]) and not near(notes["code_out"])                             # 50% 조직만 근접
    assert "7일 전부터" in login(env, env["code_in"]).get("/notifications").text
    assert "3일 전부터" in login(env, env["code_out"]).get("/notifications").text
    # 달성 현황: 사람마다 소속 조직의 비율로 센다
    html = login(env, "admin").get(f"/promo/progress?p={pid}").text
    card = html[html.index("구간 근접"):][:400]
    assert re.search(r'<div class="mid">1<span class="unit">명', card) and "소속 조직마다 30~50%" in card


def test_award_top_n_default_follows_org(env):
    mgr_org = st.person_org(env["con"], env["mctx"])
    _set(env, mgr_org, "award.default_top_n", 7)
    top_n = lambda html: re.search(r'name="top_n"[^>]*value="(\d+)"', html).group(1)
    assert top_n(login(env, env["manager"]).get("/promo/org-awards?new=1").text) == "7"
    assert top_n(login(env, "admin").get("/promo/org-awards?new=1").text) == str(st.ITEMS["award.default_top_n"]["default"])


def test_badge_constants_match_engine():
    """화면의 상태 이름은 엔진의 네 상태를 모두 덮는다."""
    assert set(card_route.BADGE_KO) == {cb.VALID, cb.EXPIRING, cb.EXPIRED, cb.EXPECTED}


def test_education_soon_days_from_engine_follow_each_persons_org(env):
    """교육 기한 임박 날짜 수는 엔진이 사람마다 낸 soon_days 를 쓴다. 14일 조직의 설계사에게만 열흘 뒤 기한이 임박이다."""
    from app.engine import education as edu
    con = env["con"]
    org_in, _ = _orgs(env)
    _set(env, org_in, "notify.edu_due_days", 14)
    edu.save_course(con, by="t", code="S3-SOON", name="설정 임박 시험 과정", kind="compliance", required=True, target_role="all",
                    deadline_rule="fixed", deadline_date=(date.today() + timedelta(days=10)).isoformat())
    mine = login(env, env["code_in"])
    other = login(env, env["code_out"])
    assert "14일 안" in mine.get("/my/education").text
    assert "7일 안" in other.get("/my/education").text
    assert "14일 안에 기한이 오는 필수 과정" in mine.get("/").text
    assert "교육 기한 임박" not in other.get("/").text
    team = login(env, env["manager"]).get("/team/education").text
    assert re.search(r"(\d+일|소속 조직마다 \d+~\d+일) 안</div>", team)
