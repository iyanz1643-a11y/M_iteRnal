"""S2 2차 화면: 프로모션 비교, 지난 프로모션, 알림함, 일정.

- 세 역할이 네 화면에 들어간다. 로그인 전에는 로그인 화면으로 간다
- 비교의 원안 칸은 메인 관리자에게만 보인다 (D-21). 설계사에게는 내부 참고용 안내가 붙는다 (D-19)
- 지난 프로모션에서 범위 밖 사람이 보이지 않고, 관리자에게는 팀원의 시상 금액이 없다 (D-17)
- 알림은 원본 화면과 같은 권한을 따른다. 명함 승인 대기는 card_approver 인 사람만, 권한 요청은 메인 관리자만 본다
- 읽음 처리와 종 아이콘의 숫자
"""
import os
import pathlib
import re
import tempfile
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db, masters
from app.engine import education as edu
from app.engine import promotion as pm
from app.org import card_grant
from app.web import auth

PW = "test-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
BADGE = re.compile(r'data-unread="(\d+)"')
PAGES = ("/promo/compare", "/promo/archive", "/notifications", "/calendar")
TODAY = date.today()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()
PAST_REWARD = 123_457            # 끝난 프로모션의 구간 보상. 관리자 화면에 이 숫자가 나오면 안 된다
NEAR_REWARD = 654_321            # 진행 중 구간 시상의 보상. 구간 근접 알림에 나오면 안 된다


class Client(TestClient):
    def post(self, url, data=None, **kw):
        if data is not None and "csrf_token" not in data:
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
    from app.web.routes import notify

    # 지점장 한 명과, 그 범위 안 설계사 한 명, 범위 밖 설계사 한 명
    manager = con.execute("""SELECT a.internal_code, u.user_id FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id
                             JOIN user_account u ON u.agent_id=a.agent_id WHERE o.level='branch' ORDER BY m.agent_id LIMIT 1""").fetchone()
    mctx = auth.context(con, manager["user_id"])
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    active = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' ORDER BY agent_id")]
    inside = next(a for a in active if a in mctx["agent_ids"] and a not in heads)
    outside = next(a for a in active if a not in mctx["agent_ids"] and a not in heads)
    con.execute("UPDATE agent SET name='범위안설계사' WHERE agent_id=?", (inside,))
    con.execute("UPDATE agent SET name='범위밖설계사' WHERE agent_id=?", (outside,))
    con.commit()
    code = lambda a: con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (a,)).fetchone()[0]

    prod = con.execute("""SELECT p.product_id, p.insurer_id FROM product p JOIN insurer i ON i.insurer_id=p.insurer_id
                          WHERE i.sector='life' AND p.product_category='health' ORDER BY p.product_id LIMIT 1""").fetchone()
    n = [0]

    def add(agent, day, premium):
        n[0] += 1
        con.execute("""INSERT INTO contract(insurer_id, policy_no, product_id, agent_id, application_no, application_date, contract_date, premium_monthly,
                                            converted_premium, policy_count, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,1,'inforce',?)""",
                    (prod["insurer_id"], f"W{n[0]:05d}", prod["product_id"], agent, f"WA{n[0]:05d}", day, day, premium, premium, db.now()))
        con.commit()

    ym_now = auth.current_ym(con)
    # 끝난 프로모션: 두 사람 다 달성
    old_from = TODAY - timedelta(days=40)
    past_id = pm.create(con, period_ym=old_from.strftime("%Y%m"), name="지난 주차 시험", kind="weekly", windows=[("1주차", D(-40), D(-30))],
                        tiers=[(50_000, "fixed", PAST_REWARD, None)], by="t", publish=True)
    add(inside, D(-35), 60_000)
    add(outside, D(-35), 70_000)
    # 진행 중: 정률 시책 하나, 구간 시상 하나(범위 안 설계사가 다음 구간 가까이), 여행 시상 하나(견줄 수 없음)
    rate_id = pm.create(con, period_ym=ym_now, name="비교 정률 시험", kind="rate", windows=[("", D(-5), D(2))],
                        rates=[(None, "initial_premium", 0.5, "next_month")], by="t", publish=True, insurer_name="시험생명")
    near_id = pm.create(con, period_ym=ym_now, name="근접 구간 시험", kind="weekly", windows=[("이번 주", D(-5), D(10))],
                        tiers=[(100_000, "fixed", NEAR_REWARD, None)], by="t", publish=True)
    add(inside, D(-2), 80_000)
    pm.create(con, period_ym=ym_now, name="여행 비교 시험", kind="travel", metric="converted_premium", windows=[("", D(-5), D(20))],
              tiers=[(1_000_000, "travel", None, "해외 연수")], by="t", publish=True)
    # 교육: 사흘 뒤가 기한인 필수 과정
    course_id = edu.save_course(con, by="t", code="T-DUE", name="기한시험 과정", kind="compliance", required=True, target_role="all",
                                deadline_rule="fixed", deadline_date=D(3))
    edu.save_session(con, by="t", course_id=course_id, date_from=D(1), online=True)
    yield {"con": con, "app": app, "notify": notify, "manager": manager["internal_code"], "inside": inside, "outside": outside,
           "inside_code": code(inside), "outside_code": code(outside), "past_id": past_id, "rate_id": rate_id, "near_id": near_id,
           "course_id": course_id}
    con.close()


def client_for(env, login_id):
    env["notify"].clear_cache()
    c = Client(env["app"], follow_redirects=False)
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, r.text
    return c


def roles(env):
    return {"main": "admin", "manager": env["manager"], "agent": env["inside_code"]}


def unread_on(html):
    m = BADGE.search(html)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------- 접근
def test_three_roles_open_all_four_screens(env):
    for role, login in roles(env).items():
        c = client_for(env, login)
        for p in PAGES:
            r = c.get(p)
            assert r.status_code == 200, (role, p, r.status_code)
    anon = Client(env["app"], follow_redirects=False)
    for p in PAGES:
        assert anon.get(p).headers["location"] == "/login"


def test_new_screens_are_built_in_menu():
    from app.web import menu
    for p in PAGES:
        assert menu.find(p)["status"] == "built", p


# ---------------------------------------------------------------- 비교
def test_original_plan_only_for_main(env):
    html = {role: client_for(env, login).get("/promo/compare").text for role, login in roles(env).items()}
    assert 'data-plan="original"' in html["main"] and "원문 확인" in html["main"]
    for role in ("manager", "agent"):
        assert 'data-plan="original"' not in html[role], role
        assert "원안과 배포안을 나누는 칸이 없어" not in html[role]
    for role, h in html.items():
        assert "내부 참고용" in h and "예상 50,000원" in h, role                # 정률 50% × 10만 원
        assert "비교 정률 시험" in h and "여행이나 물품 보상이라 금액으로 견주지 않습니다" in h, role


def test_compare_row_strips_original_for_non_main(env, monkeypatch):
    """화면 문구가 바뀌어도 원안 값 자체가 템플릿에 넘어가지 않는지 본다."""
    from app.web.routes import promo
    seen, real = {}, promo.page

    def spy(request, ctx, name, **kw):
        seen[ctx["role"]] = kw
        return real(request, ctx, name, **kw)
    monkeypatch.setattr(promo, "page", spy)
    for login in roles(env).values():
        client_for(env, login).get("/promo/compare")
    for role in ("manager", "agent"):
        rows = [r for g in seen[role]["cmp_groups"] for r in g["rows"]]
        assert rows and all(set(r["plan"]) == {"distributed"} for r in rows), role
    rows = [r for g in seen["main"]["cmp_groups"] for r in g["rows"]]
    assert rows and all("original" in r["plan"] for r in rows)


# ---------------------------------------------------------------- 지난 프로모션
def test_archive_scope_and_amounts(env):
    amount = f"{PAST_REWARD:,}"
    agent = client_for(env, env["inside_code"]).get("/promo/archive").text
    assert "지난 주차 시험" in agent and "마감 전 예상" in agent and amount in agent
    assert "범위밖설계사" not in agent and env["outside_code"] not in agent

    mgr = client_for(env, env["manager"]).get("/promo/archive").text
    assert "범위안설계사" in mgr and "달성" in mgr and "마감 전 예상" in mgr
    assert "범위밖설계사" not in mgr and env["outside_code"] not in mgr
    assert amount not in mgr and "예상 시상" not in mgr                      # D-17: 팀원의 시상 금액이 없다

    main = client_for(env, "admin").get("/promo/archive").text
    assert "범위안설계사" in main and "범위밖설계사" in main and amount in main


def test_manager_own_view_has_only_own_row(env):
    c = client_for(env, env["manager"])
    me = c.get("/promo/archive?view=me").text
    assert "범위안설계사" not in me and "범위밖설계사" not in me


# ---------------------------------------------------------------- 알림
def notes(env, login):
    from app.web.routes import notify
    con = env["con"]
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login,)).fetchone()[0]
    return notify.collect(con, auth.context(con, uid))


def test_notification_kinds_follow_source_screens(env):
    agent = {n["kind"] for n in notes(env, env["inside_code"])}
    assert {"promo_new", "promo_due", "tier_near", "edu_due"} <= agent
    assert not agent & {"card_pending", "card_grant"}
    mgr = notes(env, env["manager"])
    assert any(n["kind"] == "tier_near" and "팀원 1명" in n["title"] for n in mgr)
    assert any(n["kind"] == "edu_due" and "팀원" in n["title"] for n in mgr)
    assert not any(n["kind"] == "card_grant" for n in mgr)
    main = {n["kind"] for n in notes(env, "admin")}
    assert "card_grant" in main and "tier_near" not in main and "promo_new" not in main


def test_notifications_carry_no_reward_amount(env):
    for login in roles(env).values():
        html = client_for(env, login).get("/notifications").text
        assert f"{NEAR_REWARD:,}" not in html and f"{PAST_REWARD:,}" not in html
    html = client_for(env, env["inside_code"]).get("/notifications").text
    assert "1구간까지 20,000원 남았습니다" in html                             # 본인에게는 남은 실적만
    html = client_for(env, env["manager"]).get("/notifications").text
    assert "범위밖설계사" not in html


def test_card_pending_only_for_card_approver(env, monkeypatch):
    from app.web.routes import card
    calls = []

    def fake(con, ctx):
        calls.append(ctx["login_id"])
        return {"n": 3, "at_label": "시험"} if ctx["role"] in ("main", "manager") and ctx.get("card_approver") else None
    monkeypatch.setattr(card, "pending_count", fake)
    kinds = lambda login: {n["kind"] for n in notes(env, login)}
    assert "card_pending" not in kinds(env["manager"]) and env["manager"] not in calls   # 권한이 없으면 부르지도 않는다
    assert "card_pending" not in kinds(env["inside_code"])
    assert "card_pending" in kinds("admin")
    card_grant.grant(env["con"], [env["manager"]], by="admin")
    try:
        assert "card_pending" in kinds(env["manager"])
        html = client_for(env, env["manager"]).get("/notifications").text
        assert "명함 승인 대기 3건" in html and 'href="/sales/card-approve"' in html
    finally:
        card_grant.revoke(env["con"], [env["manager"]], by="admin")
    assert "card_pending" not in kinds(env["manager"])


def test_grant_request_link_only_where_allowed(env):
    main = [n for n in notes(env, "admin") if n["kind"] == "card_grant"]
    assert main and all(n["link"] == "/org/heads#card-requests" for n in main)
    for n in notes(env, env["inside_code"]):
        assert n["link"] is None or not n["link"].startswith(("/org/", "/data/", "/sales/card-approve")), n


def test_mark_read_and_bell_count(env):
    c = client_for(env, env["inside_code"])
    page = c.get("/notifications").text
    keys = re.findall(r'name="key" value="([^"]+)"', page)
    total = len(keys)
    assert total >= 4 and unread_on(page) == total
    assert unread_on(c.get("/promo/list").text) == total                      # 다른 화면의 종 아이콘도 같은 수

    r = c.post("/notifications", data={"action": "read", "key": keys[0]})
    assert r.status_code == 303
    page = c.get("/notifications").text
    assert unread_on(page) == total - 1 and f"안 읽은 알림 {total - 1}건" in page and "1건을 읽음으로 표시했습니다" in page
    assert unread_on(c.get("/").text) == total - 1

    # 남의 알림이나 지어낸 열쇠는 표에 넣지 않는다
    c.post("/notifications", data={"action": "read", "key": "card_grant:999999"})
    con = env["con"]
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id=?", (env["inside_code"],)).fetchone()[0]
    assert not con.execute("SELECT 1 FROM notification_read WHERE user_id=? AND note_key='card_grant:999999'", (uid,)).fetchone()

    c.post("/notifications", data={"action": "read_all"})
    page = c.get("/notifications").text
    assert unread_on(page) == 0 and "data-unread" not in page and "안 읽은 알림 0건" in page
    # 다른 사람의 읽음은 따로다
    other = client_for(env, env["manager"]).get("/notifications").text
    assert unread_on(other) > 0


def test_read_requires_csrf(env):
    c = client_for(env, env["inside_code"])
    r = TestClient.post(c, "/notifications", data={"action": "read_all"})
    assert r.status_code == 403


def test_ensure_creates_table_on_old_db(tmp_path):
    from app.web.routes import notify
    con = db.connect(tmp_path / "old.db")
    con.execute("CREATE TABLE user_account(user_id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO user_account(user_id) VALUES(1)")
    assert notify.read_keys(con, 1) == set()
    notify.mark_read(con, 1, ["promo_new:1"])
    assert notify.read_keys(con, 1) == {"promo_new:1"}
    con.close()


# ---------------------------------------------------------------- 일정
def test_calendar_shows_promotions_sessions_and_deadlines(env):
    agent = client_for(env, env["inside_code"]).get(f"/calendar?m={D(3)[:7]}").text
    assert "기한시험 과정 기한" in agent and "필수 과정" in agent
    month = client_for(env, env["inside_code"]).get(f"/calendar?m={D(2)[:7]}&view=month").text
    assert 'class="cal"' in month and "cal-month force" in month
    lst = client_for(env, env["inside_code"]).get(f"/calendar?m={D(2)[:7]}&view=list").text
    assert 'class="cal"' not in lst and "비교 정률 시험" in lst
    mgr = client_for(env, env["manager"]).get(f"/calendar?m={D(3)[:7]}").text
    assert "미수료" in mgr and "범위밖설계사" not in mgr and "범위안설계사" not in mgr
    bad = client_for(env, "admin").get("/calendar?m=2026-13")
    assert bad.status_code == 200 and f"{TODAY.year}년 {TODAY.month}월" in bad.text
