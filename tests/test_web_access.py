"""역할별 메뉴와 조직 범위가 서버에서 강제되는지 확인한다."""
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


class Client(TestClient):
    """폼을 보낼 때 화면의 숨은 CSRF 토큰을 받아 함께 보낸다."""
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
    yield {"con": con, "app": app}
    con.close()


def client_for(env, login_id):
    c = Client(env["app"], follow_redirects=False)
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, r.text
    return c


def ids(env):
    con = env["con"]
    manager = con.execute("SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id WHERE o.level='branch' LIMIT 1").fetchone()[0]
    agent = con.execute("SELECT internal_code FROM agent WHERE agent_id NOT IN (SELECT agent_id FROM org_manager) LIMIT 1").fetchone()[0]
    return manager, agent


def test_menu_file_is_consistent():
    cfg = menu.load()
    items = [it for g in cfg["groups"] for it in g["items"]]
    assert len({it["id"] for it in items}) == len(items) == len({it["path"] for it in items})
    assert all(g in [x["id"] for x in cfg["groups"]] for g in cfg["mobile_tabs"])
    # 데이터, 심사함은 메인 관리자만
    for it in items:
        if it["group"] in ("data", "review"):
            assert set(it["roles"]) == {"main"}, it["id"]


def test_login_required_and_wrong_password(env):
    c = Client(env["app"], follow_redirects=False)
    assert c.get("/").status_code == 303 and c.get("/").headers["location"] == "/login"
    assert c.post("/login", data={"login_id": "admin", "password": "nope"}).status_code == 401
    assert c.post("/login", data={"login_id": "nobody", "password": PW}).status_code == 401


def test_each_role_sees_only_its_menus(env):
    manager, agent = ids(env)
    for login_id, role in (("admin", "main"), (manager, "manager"), (agent, "agent")):
        c = client_for(env, login_id)
        for g in menu.load()["groups"]:
            for it in g["items"]:
                r = c.get(it["path"])
                want = 200 if role in it["roles"] else 403
                assert r.status_code == want, (login_id, role, it["path"], r.status_code)
        html = c.get("/").text
        assert ("계약 엑셀" in html) == (role == "main")
        assert ("팀원 현황" in html) == (role in ("main", "manager"))
    assert client_for(env, "admin").get("/no/such/page").status_code == 404


def test_denied_access_is_logged(env):
    _, agent = ids(env)
    client_for(env, agent).get("/data/contracts")
    row = env["con"].execute("SELECT actor, target FROM audit_log WHERE action='access_denied' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["actor"] == agent and row["target"] == "/data/contracts"


def test_manager_scope_is_own_org_and_below(env):
    con = env["con"]
    manager, agent = ids(env)
    uid = lambda login: con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login,)).fetchone()[0]
    m = auth.context(con, uid(manager))
    assert m["role"] == "manager" and m["org_ids"]
    inside = {r[0] for r in con.execute(f"SELECT agent_id FROM agent_org_history WHERE effective_to IS NULL AND org_id IN ({','.join('?' * len(m['org_ids']))})", tuple(m["org_ids"]))}
    outside = {r[0] for r in con.execute("SELECT agent_id FROM agent")} - inside - {m["agent_id"]}
    assert inside <= m["agent_ids"] and outside and not (outside & m["agent_ids"])
    # 팀원 현황 화면에도 범위 밖 사람이 나오지 않는다
    html = client_for(env, manager).get("/team/members").text
    codes = lambda s: {r[0] for r in con.execute(f"SELECT internal_code FROM agent WHERE agent_id IN ({','.join('?' * len(s))})", tuple(s))}
    assert all(code in html for code in codes(inside)) and not any(f">{code}<" in html for code in codes(outside))
    a = auth.context(con, uid(agent))
    assert a["role"] == "agent" and a["agent_ids"] == {a["agent_id"]} and not a["org_ids"]
    # 본부장은 아래 지점까지 본다
    hq = con.execute("SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id WHERE o.level='hq' LIMIT 1").fetchone()[0]
    h = auth.context(con, uid(hq))
    assert len(h["org_ids"]) > 1 and len(h["agent_ids"]) > len(m["agent_ids"])


def test_lockout_and_suspend(env):
    con = env["con"]
    _, agent = ids(env)
    c = Client(env["app"], follow_redirects=False)
    for _ in range(auth.MAX_FAIL):
        assert c.post("/login", data={"login_id": agent, "password": "wrong"}).status_code == 401
    r = c.post("/login", data={"login_id": agent, "password": PW})
    assert r.status_code == 401 and "잠겼습니다" in r.text
    admin = client_for(env, "admin")
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id=?", (agent,)).fetchone()[0]
    assert admin.post("/org/accounts", data={"user_id": uid, "action": "unlock"}).status_code == 303
    session = client_for(env, agent)                       # 풀린 뒤에는 들어간다
    assert admin.post("/org/accounts", data={"user_id": uid, "action": "suspend"}).status_code == 303
    assert session.get("/").status_code == 303            # 중지되면 열려 있던 세션도 끊긴다
    assert c.post("/login", data={"login_id": agent, "password": PW}).status_code == 401
    assert admin.post("/org/accounts", data={"user_id": uid, "action": "activate"}).status_code == 303
    # 관리자와 설계사는 계정 처리를 못 한다
    assert client_for(env, agent).post("/org/accounts", data={"user_id": uid, "action": "unlock"}).status_code == 403


def test_password_change(env):
    _, agent = ids(env)
    c = client_for(env, agent)
    assert "서로 다릅니다" in c.post("/me", data={"old": PW, "new": "new-pass-5678", "new2": "x"}).text
    assert "바꿨습니다" in c.post("/me", data={"old": PW, "new": "new-pass-5678", "new2": "new-pass-5678"}).text
    c2 = Client(env["app"], follow_redirects=False)
    assert c2.post("/login", data={"login_id": agent, "password": PW}).status_code == 401
    assert c2.post("/login", data={"login_id": agent, "password": "new-pass-5678"}).status_code == 303


def test_org_award_actions_are_limited_by_role(env):
    """부문 화면은 모두에게 열려 있지만 만들기, 닫기, 확정은 권한이 있는 사람만 한다."""
    from app.engine import org_award as oa
    con = env["con"]
    manager, _ = ids(env)
    # 앞의 시험이 첫 설계사의 비밀번호를 바꿔 두었으므로 다른 설계사를 쓴다
    agent = con.execute("SELECT internal_code FROM agent WHERE agent_id NOT IN (SELECT agent_id FROM org_manager) ORDER BY agent_id LIMIT 1 OFFSET 1").fetchone()[0]
    branch = con.execute("SELECT m.org_id, o.parent_id FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id WHERE a.internal_code=?", (manager,)).fetchone()
    form = lambda org, **kw: {"action": "save", "org_id": org, "name": "시험 부문", "metric": "converted_premium", "top_n": 3, "effective_from": "202601", **kw}
    count = lambda: con.execute("SELECT COUNT(*) FROM org_award_division").fetchone()[0]
    assert client_for(env, agent).post("/promo/org-awards", data=form(branch["org_id"])).status_code == 403 and count() == 0
    m = client_for(env, manager)
    assert m.post("/promo/org-awards", data=form(branch["parent_id"])).status_code == 403 and count() == 0        # 위 조직의 부문은 만들 수 없다
    assert m.post("/promo/org-awards", data=form(branch["org_id"], reward_1="포상")).status_code == 303 and count() == 1
    did = con.execute("SELECT division_id FROM org_award_division").fetchone()[0]
    assert m.post("/promo/org-awards", data={"action": "confirm", "division_id": did}).status_code == 403             # 확정은 메인 관리자만
    assert m.get(f"/promo/org-awards?edit={did}").status_code == 200
    hq_div = oa.save(con, by="t", org_id=branch["parent_id"], name="본부 부문", metric="converted_premium", top_n=3, effective_from="202601")
    assert m.get(f"/promo/org-awards?edit={hq_div}").status_code == 403
    assert m.post("/promo/org-awards", data={"action": "inactive", "division_id": hq_div}).status_code == 403
    admin = client_for(env, "admin")
    assert admin.post("/promo/org-awards", data={"action": "confirm", "division_id": did}).status_code == 303
    assert admin.post("/promo/org-awards", data=form(branch["org_id"], top_n=0)).status_code == 303 and count() == 2  # 잘못된 값은 저장하지 않는다
    assert client_for(env, agent).get("/promo/org-awards").status_code == 200


def test_promotion_edit_screen(env):
    from app.engine import promotion as pm
    con = env["con"]
    manager, agent = ids(env)
    pid = pm.create(con, period_ym="202610", name="고칠 것", kind="weekly", windows=[("1주차", "2026-10-01", "2026-10-09")],
                    tiers=[(50000, "rate", 0.5, None)], by="t", publish=True)
    admin = client_for(env, "admin")
    html = admin.get(f"/promo/tiers?edit={pid}").text
    assert "프로모션 고치기" in html and 'value="고칠 것"' in html and 'value="50"' in html      # 비율은 퍼센트로 보여 준다
    r = admin.post("/promo/tiers", data={"promo_id": pid, "name": "고친 것", "kind": "weekly", "w_label_1": "1주차", "w_from_1": "2026-10-01", "w_to_1": "2026-10-09",
                                         "t_min_1": "70000", "t_kind_1": "fixed", "t_value_1": "30000"})
    assert r.status_code == 303
    p = pm._load(con, pid)
    assert p["name"] == "고친 것" and p["status"] == "published" and p["tiers"][0]["min_value"] == 70000 and p["tiers"][0]["reward_kind"] == "fixed"
    assert client_for(env, manager).get(f"/promo/tiers?edit={pid}").status_code == 403
    assert admin.get("/promo/tiers?edit=99999").status_code == 404


def test_post_without_csrf_token_is_refused(env):
    """토큰이 없거나 틀린 POST 는 403 이고 아무것도 바꾸지 않는다. 로그인 폼도 같다."""
    con = env["con"]
    raw = TestClient(env["app"], follow_redirects=False)
    assert raw.post("/login", data={"login_id": "admin", "password": PW}).status_code == 403          # 폼을 받지 않은 세션
    assert raw.post("/login", data={"login_id": "admin", "password": PW, "csrf_token": "x"}).status_code == 403
    before = TOKEN.search(raw.get("/login").text).group(1)
    assert raw.post("/login", data={"login_id": "admin", "password": PW, "csrf_token": before}).status_code == 303
    after = TOKEN.search(raw.get("/login").text).group(1)
    assert after != before                                                                           # 로그인하면 토큰을 새로 만든다
    assert raw.post("/login", data={"login_id": "admin", "password": PW, "csrf_token": before}).status_code == 403
    uid = con.execute("SELECT user_id FROM user_account WHERE is_main_admin=0 ORDER BY user_id DESC LIMIT 1").fetchone()[0]
    for path, data in (("/org/accounts", {"user_id": uid, "action": "suspend"}), ("/me", {"old": PW, "new": "x-new-pass-1", "new2": "x-new-pass-1"}),
                       ("/promo/publish", {"action": "delete", "promo_id": 1}), ("/promo/tiers", {"name": "x"}), ("/promo/org-awards", {"action": "save"}),
                       ("/org/heads", {"action": "end", "item_id": 1}), ("/data/staff", {"action": "contract_orgs"})):
        assert raw.post(path, data=data).status_code == 403, path
        assert raw.post(path, data={**data, "csrf_token": "wrong"}).status_code == 403, path
    assert con.execute("SELECT status FROM user_account WHERE user_id=?", (uid,)).fetchone()[0] == "active"
    assert raw.post("/org/accounts", data={"user_id": uid, "action": "unlock", "csrf_token": after}).status_code == 303
    assert raw.post("/org/accounts", data={"user_id": uid, "action": "unlock"}, headers={"X-CSRF-Token": after}).status_code == 303


# guard 없이 열리는 주소. 로그인과 나가기, 그리고 자료가 없는 브라우저 아이콘뿐이다.
# scripts/check_context.py 의 NON_MENU_ROUTES 에서 마지막 라우트를 뺀 것과 같아야 한다
OPEN_ROUTES = {"/login", "/logout", "/favicon.ico"}


def test_open_routes_match_check_context():
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_context", db.ROOT / "scripts" / "check_context.py")
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    assert cc.NON_MENU_ROUTES - {"/{rest:path}"} == OPEN_ROUTES


def test_favicon_returns_nothing(env):
    r = TestClient(env["app"], follow_redirects=False).get("/favicon.ico")
    assert r.status_code == 204 and r.content == b""


def test_every_screen_goes_through_guard(env):
    """OPEN_ROUTES 를 뺀 모든 주소가 guard 를 거친다. 라우터를 나눠도 빠지는 화면이 없어야 한다."""
    from fastapi.routing import APIRoute

    from app.web.deps import guard

    def deps_of(dep):
        for d in dep.dependencies:
            yield d.call
            yield from deps_of(d)
    routes = [r for r in env["app"].routes if isinstance(r, APIRoute)]
    assert routes[-1].path == "/{rest:path}"                                                         # 기획 화면 처리는 마지막
    for r in routes:
        if r.path not in OPEN_ROUTES:
            assert guard in set(deps_of(r.dependant)), r.path


# ---------------------------------------------------------------- 여섯 단계, 조직장과 대리, 계정 발급, 사원현황
ISSUED = re.compile(r'<code style="font-size:17px">([^<]+)</code>')


def uid_of(con, login):
    return con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login,)).fetchone()[0]


def test_sample_tree_has_six_levels_and_scopes(env):
    con = env["con"]
    levels_seen = {r[0] for r in con.execute("SELECT level FROM org_unit")}
    assert levels_seen == {"ga", "hq", "branch", "center", "team"}
    html = client_for(env, "admin").get("/org/tree").text
    assert all(w in html for w in ("샘플GA", "강남센터", "강남1팀", "센터장", "팀장", "아래 인원"))
    head = lambda role: con.execute("SELECT a.internal_code, m.org_id FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id WHERE m.role=?", (role,)).fetchone()
    team_head, team = head("team_head")
    center_head, center = head("center_head")
    t = auth.context(con, uid_of(con, team_head))
    c = auth.context(con, uid_of(con, center_head))
    assert t["role"] == c["role"] == "manager" and t["org_ids"] == {team} and c["org_ids"] == {center, team}
    assert t["agent_ids"] == {t["agent_id"]} and c["agent_ids"] == {c["agent_id"], t["agent_id"]}
    tree = client_for(env, team_head).get("/org/tree").text
    assert "강남1팀" in tree and "강남센터" not in tree and "종로지점" not in tree
    members = client_for(env, center_head).get("/team/members").text
    outside = [r[0] for r in con.execute("SELECT internal_code FROM agent WHERE internal_code NOT IN (?,?)", (team_head, center_head))]
    assert team_head in members and not any(f">{code}<" in members for code in outside)
    # 지점장은 자기 지점 아래의 센터와 팀까지 본다
    branch_head = con.execute("""SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                                 WHERE m.role='branch_head' AND m.org_id=(SELECT parent_id FROM org_unit WHERE org_id=?)""", (center,)).fetchone()[0]
    b = auth.context(con, uid_of(con, branch_head))
    assert {center, team} <= b["org_ids"] and {t["agent_id"], c["agent_id"]} <= b["agent_ids"]


def test_heads_screen_assigns_and_ends(env):
    con = env["con"]
    agent = con.execute("SELECT internal_code FROM agent WHERE agent_id NOT IN (SELECT agent_id FROM org_manager) ORDER BY agent_id LIMIT 1 OFFSET 2").fetchone()[0]
    team = con.execute("SELECT org_id FROM org_unit WHERE level='team'").fetchone()[0]
    admin = client_for(env, "admin")
    ym = auth.context(con, uid_of(con, "admin"))["ym"]
    assert "조직장과 대리" in admin.get("/org/heads").text
    for who in (agent, ids(env)[0]):                                                 # 메인 관리자만 지정한다
        assert client_for(env, who).post("/org/heads", data={"action": "assign", "org_id": team, "code": agent, "from_ym": ym}).status_code == 403
    assert auth.context(con, uid_of(con, agent))["role"] == "agent"
    assert admin.post("/org/heads", data={"action": "assign", "org_id": team, "code": agent, "from_ym": ym}).status_code == 303
    ctx = auth.context(con, uid_of(con, agent))
    assert ctx["role"] == "manager" and ctx["org_ids"] == {team}
    mid = con.execute("SELECT id FROM org_manager WHERE org_id=? AND agent_id=?", (team, ctx["agent_id"])).fetchone()[0]
    assert admin.post("/org/heads", data={"action": "end", "item_id": mid}).status_code == 303
    assert auth.context(con, uid_of(con, agent))["role"] == "agent"                 # 해제하면 따로 손대지 않아도 설계사로
    root = con.execute("SELECT org_id FROM org_unit WHERE level='ga'").fetchone()[0]
    r = admin.post("/org/heads", data={"action": "assign", "org_id": root, "code": agent, "from_ym": ym})
    assert r.status_code == 400 and "조직장이 없습니다" in r.text
    # 대리는 날짜로 지정하고 끝낸다
    from datetime import date, timedelta
    today = date.today()
    assert admin.post("/org/heads", data={"action": "deputy", "org_id": team, "code": agent, "date_from": today.isoformat(),
                                          "date_to": (today + timedelta(days=2)).isoformat(), "reason": "휴가"}).status_code == 303
    assert auth.context(con, uid_of(con, agent))["role"] == "manager"
    assert agent in admin.get("/org/heads").text and "대리 중" in admin.get("/org/heads").text
    did = con.execute("SELECT id FROM org_deputy WHERE ended_at IS NULL").fetchone()[0]
    assert admin.post("/org/heads", data={"action": "deputy_end", "item_id": did}).status_code == 303
    assert auth.context(con, uid_of(con, agent))["role"] == "agent"
    acts = [r[0] for r in con.execute("SELECT action FROM audit_log WHERE action IN ('org_head_assign','org_head_end','org_deputy_assign','org_deputy_end') ORDER BY id")]
    assert acts == ["org_head_assign", "org_head_end", "org_deputy_assign", "org_deputy_end"]
    denied = con.execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied' AND target='/org/heads'").fetchone()[0]
    assert denied >= 2


def test_account_issue_shows_password_once_and_forces_change(env):
    con = env["con"]
    branch = con.execute("SELECT org_id FROM org_unit WHERE level='branch'").fetchone()[0]
    aid = con.execute("INSERT INTO agent(internal_code, name, hire_date, status, job_type) VALUES('N0001','새설계사','2026-11-01','active','fc')").lastrowid
    con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,'202611')", (aid, branch))
    con.commit()
    admin = client_for(env, "admin")
    page_ = admin.get("/org/accounts").text
    assert "계정이 없는 재직 설계사 1명" in page_ and "N0001" in page_
    assert client_for(env, ids(env)[0]).post("/org/accounts", data={"action": "issue", "agent_id": aid}).status_code == 403
    r = admin.post("/org/accounts", data={"action": "issue", "agent_id": aid})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    pw = ISSUED.findall(r.text)
    assert len(pw) == 1 and len(pw[0]) == auth.TEMP_PW_LEN
    pw = pw[0]
    assert pw not in admin.get("/org/accounts").text                                   # 한 번만 보여 준다
    assert pw not in str(con.execute("SELECT * FROM audit_log").fetchall()) and con.execute("SELECT password_hash FROM user_account WHERE login_id='N0001'").fetchone()[0] != pw
    assert admin.post("/org/accounts", data={"action": "issue", "agent_id": aid}).status_code == 400   # 두 번 만들지 않는다
    # 첫 접속에는 비밀번호부터 바꾼다
    c = Client(env["app"], follow_redirects=False)
    r = c.post("/login", data={"login_id": "N0001", "password": pw})
    assert r.status_code == 303 and r.headers["location"] == "/me"
    assert c.get("/").headers["location"] == "/me" and c.get("/promo/list").status_code == 303
    assert c.get("/me").status_code == 200
    assert "바꿨습니다" in c.post("/me", data={"old": pw, "new": "n0001-new-pass", "new2": "n0001-new-pass"}).text
    assert c.get("/").status_code == 200
    # 재설정하면 새 초기 비밀번호가 나오고 앞의 것은 듣지 않는다
    r = admin.post("/org/accounts", data={"action": "reset", "user_id": uid_of(con, "N0001")})
    pw2 = ISSUED.findall(r.text)[0]
    c2 = Client(env["app"], follow_redirects=False)
    assert c2.post("/login", data={"login_id": "N0001", "password": "n0001-new-pass"}).status_code == 401
    assert c2.post("/login", data={"login_id": "N0001", "password": pw2}).headers["location"] == "/me"
    assert [r[0] for r in con.execute("SELECT action FROM audit_log WHERE target='N0001' AND action LIKE 'account_%' ORDER BY id")] == ["account_issue", "account_reset"]


def test_staff_upload_preview_then_apply(env, tmp_path, monkeypatch):
    from app.loader import fake_eternal as fe
    con = env["con"]
    monkeypatch.setenv("APP_UPLOAD_DIR", str(tmp_path / "up"))
    path, rows = fe.make_employee_file(tmp_path / "staff.xlsx")
    names = [r["사원명"] for r in rows]
    admin = client_for(env, "admin")
    assert "파일 올리기" in admin.get("/data/staff").text
    send = lambda c, **data: c.post("/data/staff", data={"effective_ym": "202611", **data},
                                    files={"file": ("staff.xlsx", path.read_bytes(), "application/octet-stream")})
    assert send(client_for(env, ids(env)[0]), action="preview").status_code == 403
    before = con.execute("SELECT COUNT(*) FROM agent").fetchone()[0]
    r = send(admin, action="preview")
    assert r.status_code == 200 and "미리보기" in r.text and "이대로 반영하기" in r.text
    assert con.execute("SELECT COUNT(*) FROM agent").fetchone()[0] == before                 # 미리보기는 바꾸지 않는다
    assert len(list((tmp_path / "up").iterdir())) == 1
    r = admin.post("/data/staff", data={"action": "apply", "effective_ym": "202611"})
    assert r.status_code == 200 and "반영했습니다" in r.text
    assert con.execute("SELECT COUNT(*) FROM agent").fetchone()[0] == before + len(rows)
    assert list((tmp_path / "up").iterdir()) == []                                            # 반영하면 올린 파일을 지운다
    log = con.execute("SELECT target FROM audit_log WHERE action='staff_sync'").fetchone()[0]
    assert "새 설계사 40" in log and not any(n in log for n in names)
    assert admin.post("/data/staff", data={"action": "apply", "effective_ym": "202611"}).status_code == 400   # 같은 파일을 두 번 반영하지 않는다
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not an excel file")
    r = admin.post("/data/staff", data={"action": "preview", "effective_ym": "202611"}, files={"file": ("bad.xlsx", bad.read_bytes())})
    assert r.status_code == 400 and "읽지 못했습니다" in r.text
    # 퇴사자는 계정이 없으면 후보에 없고, 새 설계사는 계정 화면의 발급 목록에 나온다
    assert "계정이 없는 재직 설계사" in admin.get("/org/accounts").text
    tree = admin.get("/org/tree").text
    assert fe.SHARED_CENTER in tree and fe.SHARED_TEAM in tree
