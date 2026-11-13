"""역할별 메뉴와 조직 범위가 서버에서 강제되는지 확인한다."""
import os
import pathlib
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db, masters
from app.web import auth, menu

PW = "test-pass-1234"


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
    c = TestClient(env["app"], follow_redirects=False)
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
    c = TestClient(env["app"], follow_redirects=False)
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
    c = TestClient(env["app"], follow_redirects=False)
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
    c2 = TestClient(env["app"], follow_redirects=False)
    assert c2.post("/login", data={"login_id": agent, "password": PW}).status_code == 401
    assert c2.post("/login", data={"login_id": agent, "password": "new-pass-5678"}).status_code == 303
