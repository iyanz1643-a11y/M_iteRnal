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
