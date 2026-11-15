"""S4 파트 J 영업 바탕과 화면, 팀 코칭과 활동, 상담 신청 랜딩.

- 범위: 내 DB 와 고객은 본인 것만, 배정 화면은 메뉴 범위 안만 연다. 범위 밖은 404 와 access_denied 기록이다.
- 가림: 목록의 연락처는 늘 가린다. 원문은 담당과 직속 관리자, 사유를 적은 메인 관리자만 보고 본 일은 sales_contact_view 로 남는다. D-12 가정.
- 배정과 회수: 지역 매칭, 보유 한도, 범위 밖 사람에게 배정 막기, 방치 DB 만 회수.
- 해촉자 고객 넘기기: 직속 관리자에게 넘기고 준법 확인 전에는 받은 사람에게도 가린다. D-24 가정.
- 랜딩: 스위치 꺼짐, 동의 필수, 꿀단지, 신청 횟수 제한, 중복, CSRF. D-29 가정.
- 면담 기록과 활동 깔때기, 알림에 고객 이름과 연락처가 들어가지 않는 것.
연락처는 모두 가운데가 0000 인 가짜 번호다.
"""
import os
import pathlib
import re
import tempfile
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db, masters
from app.ops import ratelimit
from app.sales import coaching, core
from app.sales import customers as cs
from app.sales import leads as ld
from app.web import auth, menu

PW = "test-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
NEW_PATHS = ["/sales/leads", "/sales/assign", "/sales/customers", "/sales/guide", "/team/coaching", "/team/activity"]


def ph(i: int) -> str:
    """가짜 번호. 가운데가 0000 이다."""
    return "010" + "-0000-" + f"{i:04d}"


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
    code = lambda aid: con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (aid,)).fetchone()[0]
    head = lambda name: con.execute("""SELECT m.agent_id FROM org_manager m JOIN org_unit o ON o.org_id=m.org_id WHERE o.name=?""", (name,)).fetchone()[0]
    branch = con.execute("SELECT org_id FROM org_unit WHERE name='강남지점'").fetchone()[0]
    members = [r[0] for r in con.execute(
        """SELECT h.agent_id FROM agent_org_history h WHERE h.org_id=? AND h.effective_to IS NULL
           AND h.agent_id NOT IN (SELECT agent_id FROM org_manager) ORDER BY h.agent_id""", (branch,))]
    other_branch = con.execute("SELECT org_id FROM org_unit WHERE name='해운대지점'").fetchone()[0]
    outsider = con.execute("""SELECT agent_id FROM agent_org_history WHERE org_id=? AND effective_to IS NULL
                              AND agent_id NOT IN (SELECT agent_id FROM org_manager) ORDER BY agent_id LIMIT 1""", (other_branch,)).fetchone()[0]
    e = {"con": con, "app": app, "branch": branch, "a": members[0], "b": members[1], "c": members[2], "d": members[3], "outsider": outsider,
         "head": head("강남지점"), "upper": head("서울본부"), "other": head("해운대지점"), "team_head": head("강남1팀"), "other_branch": other_branch}
    e["code"] = {k: code(v) for k, v in e.items() if k in ("a", "b", "c", "d", "outsider", "head", "upper", "other", "team_head")}
    yield e
    con.close()


def login(env, who):
    c = Client(env["app"], follow_redirects=False)
    lid = who if who == "admin" else env["code"][who]
    r = c.post("/login", data={"login_id": lid, "password": PW})
    assert r.status_code == 303, r.text
    return c


def lead_for(env, agent_key, i, region="서울", days_ago=0):
    con = env["con"]
    lid = ld.create(con, name=f"고객{'가나다라마바사아자차카타파하'[i % 14]}{'하파타카'[i % 4]}", phone=ph(i), region=region, source="company",
                    by="test", consent=True, pool_org_id=env["branch"])
    if agent_key:
        ld.assign(con, lid, env[agent_key], "test")
        if days_ago:
            t = (date.today() - timedelta(days=days_ago)).isoformat() + " 09:00:00"
            con.execute("UPDATE sales_lead SET assigned_at=? WHERE lead_id=?", (t, lid))
    con.commit()
    return lid


def views(con, target):
    return [r["after_json"] for r in con.execute("SELECT after_json FROM audit_log WHERE action='sales_contact_view' AND target=?", (target,))]


def denied(con, needle):
    return con.execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied' AND target LIKE ?", (f"%{needle}%",)).fetchone()[0]


# ================================================================ 메뉴와 guard
def test_new_screens_are_built_and_guarded(env):
    from app.web.deps import guard
    from tests.test_web_access import OPEN_ROUTES, api_routes
    for p in NEW_PATHS:
        assert menu.find(p)["status"] == "built", p
    assert "/consult" in OPEN_ROUTES

    def deps_of(dep):
        for d in dep.dependencies:
            yield d.call
            yield from deps_of(d)
    seen = set()
    for r in api_routes(env["app"].routes):
        if r.path in NEW_PATHS:
            seen.add(r.path)
            assert guard in set(deps_of(r.dependant)), r.path
    assert seen == set(NEW_PATHS)


def test_roles_open_only_their_screens(env):
    want = {"admin": {"/sales/assign", "/sales/guide", "/team/activity"},
            "head": set(NEW_PATHS), "a": {"/sales/leads", "/sales/customers", "/sales/guide"}}
    for who, ok in want.items():
        c = login(env, who)
        for p in NEW_PATHS:
            assert c.get(p).status_code == (200 if p in ok else 403), (who, p)


# ================================================================ 범위, 가림, 원문 보기 기록
def test_own_lead_list_masks_and_detail_logs(env):
    con = env["con"]
    lid = lead_for(env, "a", 1)
    c = login(env, "a")
    html = c.get("/sales/leads").text
    assert ph(1) not in html and "010-****-0001" in html
    r = c.get(f"/sales/leads?id={lid}")
    assert r.status_code == 200 and ph(1) in r.text and r.headers["cache-control"] == "no-store"
    assert any('"own"' in v for v in views(con, f"lead:{lid}"))
    # 다른 설계사는 없는 것과 같다. 시도는 남는다
    before = denied(con, f"lead:{lid}")
    assert login(env, "b").get(f"/sales/leads?id={lid}").status_code == 404
    assert denied(con, f"lead:{lid}") == before + 1
    assert login(env, "b").post("/sales/leads", data={"action": "activity", "lead_id": lid, "kind": "call", "done_on": date.today().isoformat()}).status_code == 404


def test_direct_head_sees_plain_upper_and_main_masked(env):
    con = env["con"]
    lid = lead_for(env, "a", 2)
    r = login(env, "head").get(f"/sales/assign?lead={lid}")
    assert r.status_code == 200 and ph(2) in r.text
    assert any('"head"' in v for v in views(con, f"lead:{lid}"))
    n = len(views(con, f"lead:{lid}"))
    r = login(env, "upper").get(f"/sales/assign?lead={lid}")              # 위 조직의 관리자는 가린 채로
    assert r.status_code == 200 and ph(2) not in r.text and "010-****-0002" in r.text
    assert len(views(con, f"lead:{lid}")) == n
    assert login(env, "other").get(f"/sales/assign?lead={lid}").status_code == 404     # 범위 밖 관리자
    admin = login(env, "admin")
    r = admin.get(f"/sales/assign?lead={lid}")
    assert ph(2) not in r.text and "조회 사유" in r.text
    assert admin.post("/sales/assign", data={"action": "reveal", "lead_id": lid, "reason": "짧음"}).status_code == 400
    r = admin.post("/sales/assign", data={"action": "reveal", "lead_id": lid, "reason": "민원 확인 요청으로 조회"})
    assert r.status_code == 200 and ph(2) in r.text
    assert any("민원 확인" in v for v in views(con, f"lead:{lid}"))
    # 관리자는 사유 조회를 하지 못한다
    assert login(env, "upper").post("/sales/assign", data={"action": "reveal", "lead_id": lid, "reason": "민원 확인 요청으로 조회"}).status_code == 403


def test_no_list_shows_plain_contact(env):
    lid = lead_for(env, "a", 3)
    lead_for(env, None, 4)
    for who in ("admin", "head", "upper", "a"):
        c = login(env, who)
        for p in ("/sales/assign", "/sales/leads", "/sales/customers", "/notifications"):
            r = c.get(p)
            if r.status_code == 200:
                assert ph(3) not in r.text and ph(4) not in r.text, (who, p)
    assert lid


# ================================================================ 배정과 회수
def test_region_matching_and_hold_limit(env, monkeypatch):
    con = env["con"]
    ld.set_regions(con, env["c"], ["제주"], "test")
    ld.set_regions(con, env["d"], ["제주"], "test")
    con.commit()
    head = login(env, "head")
    l1, l2, l3 = (lead_for(env, None, 10 + i, region="제주") for i in range(3))
    monkeypatch.setenv("APP_SALES_HOLD", "1")
    assert head.post("/sales/assign", data={"action": "auto"}).status_code == 303
    got = {ld.get(con, x)["agent_id"] for x in (l1, l2, l3)}
    assert got == {env["c"], env["d"], None}                                    # 한도 1 이라 한 사람에 하나씩, 하나는 남는다
    assert {r["action"] for r in con.execute("SELECT action FROM sales_assign_log WHERE lead_id IN (?,?)", (l1, l2))} == {"auto"}
    left = next(x for x in (l1, l2, l3) if ld.get(con, x)["agent_id"] is None)
    head.post("/sales/assign", data={"action": "assign", "lead_id": left, "agent_id": env["c"]})
    assert ld.get(con, left)["status"] == "new"                                # 한도가 차서 막힌다
    monkeypatch.delenv("APP_SALES_HOLD")
    # 범위 밖 사람에게는 배정하지 않는다
    assert head.post("/sales/assign", data={"action": "assign", "lead_id": left, "agent_id": env["outsider"]}).status_code == 403
    assert head.post("/sales/assign", data={"action": "assign", "lead_id": left, "agent_id": env["b"]}).status_code == 303
    assert ld.get(con, left)["agent_id"] == env["b"]


def test_pool_scope_and_main_only_actions(env):
    con = env["con"]
    far = ld.create(con, name="풀고객", phone=ph(20), region="부산", source="company", by="test", consent=True, pool_org_id=env["other_branch"])
    hq = ld.create(con, name="본부고객", phone=ph(21), region="부산", source="company", by="test", consent=True)
    con.commit()
    head = login(env, "head")
    assert head.get(f"/sales/assign?lead={far}").status_code == 404
    assert head.get(f"/sales/assign?lead={hq}").status_code == 404           # 본부 풀은 메인 관리자만
    assert head.post("/sales/assign", data={"action": "assign", "lead_id": far, "agent_id": env["a"]}).status_code == 404
    for action in ("pool", "add", "handover", "confirm"):
        assert head.post("/sales/assign", data={"action": action}).status_code == 403, action
    assert login(env, "a").post("/sales/assign", data={"action": "auto"}).status_code == 403
    admin = login(env, "admin")
    assert admin.post("/sales/assign", data={"action": "pool", "lead_id": hq, "org_id": env["branch"]}).status_code == 303
    assert ld.get(con, hq)["pool_org_id"] == env["branch"]
    assert head.get(f"/sales/assign?lead={hq}").status_code == 200


def test_reclaim_only_stale(env):
    con = env["con"]
    fresh = lead_for(env, "a", 30, days_ago=1)
    old = lead_for(env, "a", 31, days_ago=core.setting("stale") + 2)
    head = login(env, "head")
    head.post("/sales/assign", data={"action": "reclaim", "lead_id": fresh})
    assert ld.get(con, fresh)["status"] == "assigned"
    assert head.post("/sales/assign", data={"action": "reclaim", "lead_id": old}).status_code == 303
    r = ld.get(con, old)
    assert r["status"] == "new" and r["agent_id"] is None and r["reclaim_count"] == 1 and r["pool_org_id"] == env["branch"]
    assert con.execute("SELECT from_agent_id FROM sales_assign_log WHERE lead_id=? AND action='reclaim'", (old,)).fetchone()[0] == env["a"]
    # 회수한 DB 는 담당이 더 열지 못한다
    assert login(env, "a").get(f"/sales/leads?id={old}").status_code == 404


# ================================================================ 활동과 고객
def test_activity_convert_and_link_own_contract_only(env):
    con = env["con"]
    lid = lead_for(env, "a", 40)
    a = login(env, "a")
    today = date.today()
    a.post("/sales/leads", data={"action": "activity", "lead_id": lid, "kind": "call", "done_on": (today + timedelta(days=1)).isoformat()})
    assert con.execute("SELECT COUNT(*) FROM sales_activity WHERE lead_id=?", (lid,)).fetchone()[0] == 0
    assert a.post("/sales/leads", data={"action": "activity", "lead_id": lid, "kind": "call", "done_on": today.isoformat(), "result": "연결"}).status_code == 303
    assert ld.get(con, lid)["status"] == "working"
    r = a.post("/sales/leads", data={"action": "convert", "lead_id": lid})
    cid = int(r.headers["location"].split("id=")[1])
    assert ld.get(con, lid)["status"] == "won"
    ins = con.execute("SELECT insurer_id FROM insurer LIMIT 1").fetchone()[0]
    mine = con.execute("INSERT INTO contract(insurer_id, policy_no, agent_id, contract_date, status, created_at) VALUES(?,?,?,?,?,?)",
                       (ins, "TEST-S4-1", env["a"], "2025-11-20", "inforce", db.now())).lastrowid
    theirs = con.execute("INSERT INTO contract(insurer_id, policy_no, agent_id, contract_date, status, created_at) VALUES(?,?,?,?,?,?)",
                         (ins, "TEST-S4-2", env["b"], "2025-11-20", "inforce", db.now())).lastrowid
    con.commit()
    page = a.get(f"/sales/customers?id={cid}").text
    assert "TEST-S4-1" in page and "TEST-S4-2" not in page
    assert a.post("/sales/customers", data={"action": "link", "customer_id": cid, "contract_id": theirs}).status_code == 403
    assert a.post("/sales/customers", data={"action": "link", "customer_id": cid, "contract_id": mine}).status_code == 303
    nxt = (today + timedelta(days=2)).isoformat()
    a.post("/sales/customers", data={"action": "plan", "customer_id": cid, "next_action": "결과 설명", "next_on": nxt, "birth_md": "2000-" + nxt[5:]})
    ev = cs.upcoming(con, env["a"], today)
    assert {e["kind"] for e in ev if e["customer_id"] == cid} >= {"결과 설명", "생일"}
    assert login(env, "b").get(f"/sales/customers?id={cid}").status_code == 404
    assert any('"own"' in v for v in views(con, f"customer:{cid}"))


# ================================================================ 해촉자 고객 넘기기
def test_handover_to_direct_head_with_compliance(env):
    con = env["con"]
    lid = lead_for(env, "d", 50)
    cid = ld.convert(con, lid, env["d"], "test")
    open_lead = lead_for(env, "d", 51)
    con.commit()
    admin = login(env, "admin")
    r = admin.post("/sales/assign", data={"action": "handover", "agent_id": env["d"], "confirm": "1"})
    assert r.status_code == 303 and cs.get(con, cid)["agent_id"] == env["d"]       # 재직자는 넘기지 않는다
    con.execute("UPDATE agent SET status='terminated', termination_date=? WHERE agent_id=?", (date.today().isoformat(), env["d"]))
    con.commit()
    assert any(h["agent_id"] == env["d"] and h["to_agent_id"] == env["head"] for h in cs.handover_candidates(con, auth.current_ym(con)))
    r = admin.post("/sales/assign", data={"action": "handover", "agent_id": env["d"]})
    assert r.status_code == 303 and "ask=" in r.headers["location"] and cs.get(con, cid)["agent_id"] == env["d"]
    assert admin.post("/sales/assign", data={"action": "handover", "agent_id": env["d"], "confirm": "1"}).status_code == 303
    cust = cs.get(con, cid)
    assert cust["agent_id"] == env["head"] and cust["handover_status"] == "pending" and cust["handed_from_agent_id"] == env["d"]
    assert ld.get(con, open_lead)["status"] == "new"
    head = login(env, "head")
    r = head.get(f"/sales/customers?id={cid}")
    assert r.status_code == 200 and ph(50) not in r.text and "준법 확인" in r.text
    assert head.post("/sales/customers", data={"action": "plan", "customer_id": cid, "next_action": "x"}).status_code == 403
    assert admin.post("/sales/assign", data={"action": "confirm", "customer_id": cid}).status_code == 303
    assert ph(50) in head.get(f"/sales/customers?id={cid}").text
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='sales_handover_confirm'").fetchone()[0] == 1
    con.execute("UPDATE agent SET status='active', termination_date=NULL WHERE agent_id=?", (env["d"],))
    con.commit()


# ================================================================ 상담 신청 랜딩
@pytest.fixture()
def landing(env, monkeypatch):
    ratelimit.LIMITER.reset()
    monkeypatch.setenv("APP_CONSULT_OPEN", "1")
    yield Client(env["app"], follow_redirects=False)
    ratelimit.LIMITER.reset()


def _apply(c, i, **kw):
    data = {"name": "신청고객", "phone": ph(i), "region": "세종", "time": "weekday_pm", "consent": "1", "website": ""}
    data.update(kw)
    return c.post("/consult", data=data)


def test_landing_switch_off_by_default(env, monkeypatch):
    ratelimit.LIMITER.reset()
    monkeypatch.delenv("APP_CONSULT_OPEN", raising=False)
    c = Client(env["app"], follow_redirects=False)
    r = c.get("/consult")
    assert r.status_code == 200 and "지금은 신청을 받지 않습니다" in r.text and 'name="phone"' not in r.text
    assert r.headers["x-robots-tag"].startswith("noindex")
    n = env["con"].execute("SELECT COUNT(*) FROM sales_lead").fetchone()[0]
    assert _apply(c, 60).status_code == 403
    assert env["con"].execute("SELECT COUNT(*) FROM sales_lead").fetchone()[0] == n


def test_landing_consent_required_and_assigns(env, landing):
    con = env["con"]
    ld.set_regions(con, env["b"], ["세종"], "test")
    con.commit()
    r = landing.get("/consult")
    assert r.status_code == 200 and "준법 확인 전" in r.text and 'name="consent"' in r.text and 'name="website"' in r.text
    n = con.execute("SELECT COUNT(*) FROM sales_lead").fetchone()[0]
    assert _apply(landing, 61, consent="").status_code == 400
    assert con.execute("SELECT COUNT(*) FROM sales_lead").fetchone()[0] == n
    r = _apply(landing, 61)
    assert r.status_code == 200 and "신청을 받았습니다" in r.text
    row = con.execute("SELECT * FROM sales_lead WHERE phone=?", (ph(61),)).fetchone()
    assert row["source"] == "landing" and row["agent_id"] == env["b"] and row["consent_at"] and row["consent_version"] == core.CONSENT_VERSION
    name_b = con.execute("SELECT name FROM agent WHERE agent_id=?", (env["b"],)).fetchone()[0]
    assert name_b not in r.text                                                 # 담당을 내보내지 않는다
    assert _apply(landing, 61).status_code == 200                               # 같은 번호가 하루 안에 다시 오면 새로 만들지 않는다
    assert con.execute("SELECT COUNT(*) FROM sales_lead WHERE phone=?", (ph(61),)).fetchone()[0] == 1


def test_landing_honeypot_and_rate_limit(env, landing, monkeypatch):
    con = env["con"]
    n = con.execute("SELECT COUNT(*) FROM sales_lead").fetchone()[0]
    r = _apply(landing, 70, website="http://spam.example")
    assert r.status_code == 200 and "신청을 받았습니다" in r.text
    assert con.execute("SELECT COUNT(*) FROM sales_lead").fetchone()[0] == n
    monkeypatch.setenv("APP_RATE_CONSULT_IP", "2/3600")
    ratelimit.LIMITER.reset()
    assert _apply(landing, 71).status_code == 200
    assert _apply(landing, 72).status_code == 200
    r = _apply(landing, 73)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert con.execute("SELECT COUNT(*) FROM sales_lead WHERE phone=?", (ph(73),)).fetchone()[0] == 0


def test_csrf_blocks_landing_and_sales_posts(env, landing):
    raw = Client(env["app"], follow_redirects=False)
    data = {"name": "신청고객", "phone": ph(80), "region": "세종", "time": "any", "consent": "1"}
    assert raw.post("/consult", data=data, csrf=False).status_code == 403
    assert raw.post("/consult", data={**data, "csrf_token": "wrong"}, csrf=False).status_code == 403
    a = login(env, "a")
    lid = lead_for(env, "a", 81)
    for path, d in (("/sales/leads", {"action": "close", "lead_id": lid}), ("/sales/customers", {"action": "plan", "customer_id": 1}),
                    ("/team/coaching", {"action": "add"})):
        assert a.post(path, data=d, csrf=False).status_code == 403, path
    assert ld.get(env["con"], lid)["status"] == "assigned"
    head = login(env, "head")
    assert head.post("/sales/assign", data={"action": "auto"}, csrf=False).status_code == 403


# ================================================================ 면담 기록, 활동 깔때기, 알림
def test_coaching_scope_and_due_notice(env):
    con = env["con"]
    head = login(env, "head")
    today = date.today()
    ok = {"action": "add", "agent_id": env["a"], "met_on": today.isoformat(), "promises": "통화 20건", "next_on": (today + timedelta(days=2)).isoformat()}
    assert head.post("/team/coaching", data=ok).status_code == 303
    assert head.post("/team/coaching", data={**ok, "agent_id": env["outsider"]}).status_code == 403
    assert head.post("/team/coaching", data={**ok, "agent_id": env["head"]}).status_code == 403        # 본인은 뺀다
    head.post("/team/coaching", data={**ok, "next_on": (today - timedelta(days=1)).isoformat()})      # 다음 면담이 이번보다 앞이면 남기지 않는다
    assert con.execute("SELECT COUNT(*) FROM team_coaching WHERE agent_id=?", (env["a"],)).fetchone()[0] == 1
    assert "통화 20건" in head.get(f"/team/coaching?agent={env['a']}").text
    assert head.get(f"/team/coaching?agent={env['outsider']}").status_code == 404
    assert [d["agent_id"] for d in coaching.due(con, [env["a"]], today)] == [env["a"]]
    assert "다음 면담 1명" in head.get("/notifications").text
    assert "통화 20건" not in login(env, "upper").get("/notifications").text


def test_activity_funnel_counts(env):
    con = env["con"]
    lid = lead_for(env, "c", 90)
    today = date.today().isoformat()
    for kind in ("call", "call", "consult", "proposal"):
        ld.add_activity(con, lid, env["c"], kind, today, None, "test")
    con.commit()
    html = login(env, "head").get("/team/activity").text
    name_c = con.execute("SELECT name FROM agent WHERE agent_id=?", (env["c"],)).fetchone()[0]
    body = html.split('<main class="wrap">', 1)[1].split("</main>", 1)[0]
    assert name_c in body and "앞 단계의" in body and "보험료" not in body and "수수료" not in body and "원</" not in body   # 건수만이고 금액은 없다(D-17)
    assert name_c not in login(env, "other").get("/team/activity").text


def test_guide_reuses_board(env):
    admin = login(env, "admin")
    r = admin.post("/board", data={"action": "save", "category": "script", "title": "TA 스크립트 시험", "body": "본문", "target_org_id": "", "target_role": ""})
    assert r.status_code == 303
    for who in ("admin", "a", "head"):
        assert "TA 스크립트 시험" in login(env, who).get("/sales/guide").text
    assert login(env, "a").post("/board", data={"action": "save", "category": "guide", "title": "x"}).status_code == 403
    assert "TA 스크립트 시험" in login(env, "a").get("/sales/guide?cat=script").text
    assert "TA 스크립트 시험" not in login(env, "a").get("/sales/guide?cat=needs").text


def test_sales_notices_have_no_customer_data(env):
    con = env["con"]
    lid = lead_for(env, "b", 95, days_ago=core.setting("warn"))
    name = ld.get(con, lid)["name"]
    from app.web.routes import notify
    notify.clear_cache()
    html = login(env, "b").get("/notifications").text
    assert "회수 대상" in html and name not in html and ph(95) not in html
