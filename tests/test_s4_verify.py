"""S4 검증. 만든 에이전트와 따로, verifier 가 S4 의 제도, 영업, 상담 신청, 앱 AI 를 다시 본다.

- 흐름: 상담 신청 스위치를 켠 임시 환경에서 /consult 신청, 지역 매칭 배정, 설계사의 활동 기록과 고객 바꾸기,
  리포트 앞까지. AI 산출물은 가짜 클라이언트로 심사 요청, 회전, 사람 결재, 기록까지. 고객용 글은 통과해야만 승인된다.
- 개인정보: AI 에 넣는 컨텍스트와 ai_call 기록, 근거에 없는 숫자 막기, 영업 연락처 가림과 원문 보기 기록, 상담 신청의 IP 와 동의와 꺼짐.
- 권한: 세 역할의 S4 화면, 메인 관리자 전용 쓰기, 범위 밖, CSRF, D-05 와 D-12 의 원문 보기, D-17.
- 한도: 월 한도에 닿으면 부르지 않고 멈춘다. 키가 없으면 AI 화면은 꺼짐 안내이고 다른 화면은 된다.
- 숫자 대조: 제도 화면의 실적과 목표 역산이 실적 요약과 같은지, 영업 체결률과 활동 깔때기가 기록과 맞는지.

실제 API 는 부르지 않는다. app/ops/jobs.ai_client 를 바꿔 FakeTransport 를 넣는다.
자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 와 data/ 아래 폴더는 건드리지 않는다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다. 사람 번호, 화면 주소, 건수만 넣는다.
연락처와 주민번호 꼴은 저장소 점검에 걸리지 않게 조각을 이어 만든다.
xfail(strict=True) 인 시험은 검증 때 찾은 문제의 재현이다. 고치면 xfail 을 지운다.
"""
import json
import math
import os
import pathlib
import re
import shutil
import tempfile
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db
from app.ai import context as aictx
from app.ai import plan_qa, review
from app.ai.client import BUDGET_MESSAGE, AIClient, FakeTransport
from app.engine import career, standards
from app.engine import certification as ce
from app.engine import results as rs
from app.ops import jobs, ratelimit
from app.sales import core
from app.web import ai_view, auth

PW = "verify-s4-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
FAKE_PHONE = "010" + "-" + "0000" + "-" + "7788"
TEST_REGION = "강원"                     # 샘플이 맡기지 않은 지역. 이 시험의 설계사만 맡게 한다

# S4 새 화면. 역할마다 기대하는 응답 코드다. menu.yaml 을 읽지 않고 docs/10, docs/11 과 기능기획서의 권한을 시험이 따로 적었다.
S4_SCREENS = {
    "/program/career": {"main": 200, "manager": 200, "agent": 403},
    "/program/cert": {"main": 200, "manager": 200, "agent": 403},
    "/program/executive": {"main": 200, "manager": 200, "agent": 403},
    "/program/excellence": {"main": 200, "manager": 200, "agent": 403},
    "/my/career": {"main": 403, "manager": 200, "agent": 200},
    "/my/cert": {"main": 403, "manager": 200, "agent": 200},
    "/sales/leads": {"main": 403, "manager": 200, "agent": 200},
    "/sales/customers": {"main": 403, "manager": 200, "agent": 200},
    "/sales/assign": {"main": 200, "manager": 200, "agent": 403},
    "/sales/guide": {"main": 200, "manager": 200, "agent": 200},
    "/sales/report": {"main": 403, "manager": 200, "agent": 200},
    "/sales/cardnews": {"main": 200, "manager": 403, "agent": 403},
    "/team/coaching": {"main": 403, "manager": 200, "agent": 403},
    "/team/activity": {"main": 200, "manager": 200, "agent": 403},
    "/review/requests": {"main": 200, "manager": 403, "agent": 403},
    "/review/results": {"main": 200, "manager": 403, "agent": 403},
    "/data/plan-qa": {"main": 200, "manager": 403, "agent": 403},          # D-27
    "/org/ai": {"main": 200, "manager": 403, "agent": 403},
}
S4_POSTS = ["/program/career", "/program/excellence", "/sales/leads", "/sales/assign", "/sales/customers", "/team/coaching",
            "/review/requests", "/review/results", "/data/plan-qa", "/org/ai", "/consult"]
AI_SCREENS = ("/review/requests", "/review/results", "/data/plan-qa", "/org/ai")


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리킨다. 폼에는 세션의 CSRF 토큰을 붙인다. raw=True 면 붙이지 않는다."""

    def __init__(self, app, db_path, **kw):
        super().__init__(app, follow_redirects=False, **kw)
        self.db_path = str(db_path)

    def request(self, *a, **kw):
        os.environ["APP_DB"] = self.db_path
        return super().request(*a, **kw)

    def post(self, url, data=None, raw=False, **kw):
        if data is not None and not raw and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


@pytest.fixture(scope="module")
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="s4verify-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR", "CARD_SITE_WRITE",
            "ANTHROPIC_API_KEY", "APP_CONSULT_OPEN", "APP_RATE_CONSULT_IP")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    for k in ("CARD_SITE_WRITE", "ANTHROPIC_API_KEY", "APP_CONSULT_OPEN", "APP_RATE_CONSULT_IP"):
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
    # 직속 팀원이 있는 지점장. 그 지점의 위 본부장. 범위 밖 설계사
    mgr = None
    for r in con.execute("""SELECT a.agent_id, a.internal_code code, m.org_id FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                            JOIN org_unit o ON o.org_id=m.org_id WHERE o.level='branch' AND m.effective_to IS NULL ORDER BY o.org_id"""):
        mctx = auth.context(con, con.execute("SELECT user_id FROM user_account WHERE agent_id=?", (r["agent_id"],)).fetchone()[0])
        direct = [a for a in sorted(mctx["agent_ids"] - {r["agent_id"]}) if a not in heads and a in users
                  and r["agent_id"] in core.direct_heads(con, a, ym)]
        if direct:
            mgr = {**dict(r), "ctx": mctx, "direct": direct}
            break
    assert mgr, "직속 팀원이 있는 지점장이 있어야 한다"
    org, upper = mgr["org_id"], None
    while upper is None:
        org = con.execute("SELECT parent_id FROM org_unit WHERE org_id=?", (org,)).fetchone()[0]
        assert org is not None, "지점 위에 조직장이 있어야 한다"
        h = con.execute("""SELECT a.agent_id, a.internal_code code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                           WHERE m.org_id=? AND m.effective_to IS NULL LIMIT 1""", (org,)).fetchone()
        upper = dict(h) if h else None
    outside = [a for a in sorted(users) if a not in mgr["ctx"]["agent_ids"] and a not in heads]
    assert outside, "관리자 범위 밖 설계사가 있어야 한다"
    main_ctx = auth.context(con, con.execute("SELECT user_id FROM user_account WHERE login_id='admin'").fetchone()[0])
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "con": con, "app": app, "ym": ym, "mgr": mgr, "upper": upper, "agent": mgr["direct"][0],
           "outside": outside, "users": users, "main_ctx": main_ctx}
    con.close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


def login(env, login_id, **kw):
    c = Client(env["app"], env["db"], **kw)
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, f"{login_id} 로그인"
    return c


def roles(env):
    return {"main": login(env, "admin"), "manager": login(env, env["mgr"]["code"]), "agent": login(env, env["users"][env["agent"]])}


def anon(env):
    return Client(env["app"], env["db"])


def count(con, sql, *args):
    return con.execute(sql, args).fetchone()[0]


class Fake:
    """기능마다 답을 바꿀 수 있는 가짜. 시스템 지침의 머리로 기능을 가른다. 받은 요청은 transport.calls 에 남는다."""

    def __init__(self):
        self.themis = lambda user: {"verdict": "pass", "quote": None, "reason": "문제 없음", "revised": None}
        self.interpret = "{}"
        self.qa = ""
        self.transport = FakeTransport(self.reply)

    def reply(self, system, user):
        if "<역할>\n다빈치" in system:
            return "읽는 사람에게 차분한 안내로 그려집니다."
        if "<역할>\n테미스" in system:
            return json.dumps(self.themis(user), ensure_ascii=False)
        if "시상안 원장의 한 줄" in system:
            return self.interpret
        if "승인된 시상안 줄만 근거로" in system:
            return self.qa
        raise AssertionError("모르는 기능")


@pytest.fixture()
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(jobs, "ai_client", lambda con: AIClient(con, f.transport))
    return f


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(jobs, "ai_client", lambda con: AIClient(con))


def _row_of(html: str, code: str) -> str:
    """표에서 사번이 든 한 줄."""
    m = re.search(r"<tr>(?:(?!</tr>).)*req-code\">" + re.escape(code) + r"<(?:(?!</tr>).)*</tr>", html, re.S)
    return m.group(0) if m else ""


def _approved_fc_line(con):
    return con.execute("""SELECT l.*, d.period_ym FROM incentive_plan_line l JOIN incentive_plan_doc d ON d.plan_doc_id=l.plan_doc_id
                          WHERE l.review_status='approved' AND l.recipient_level='fc' AND d.status!='superseded'
                          ORDER BY l.line_id LIMIT 1""").fetchone()


def _qa_question(line) -> str:
    ym = line["period_ym"]
    return f"{line['insurer_name']} {ym[:4]}년 {int(ym[4:])}월 시책은 어떻게 되나요?"


# ================================================================ 1. 접근과 CSRF
def test_s4_screens_three_roles(env):
    clients = roles(env)
    wrong = []
    for path, want in S4_SCREENS.items():
        for role, code in want.items():
            got = clients[role].get(path).status_code
            if got != code:
                wrong.append(f"{role} {path}: {got} (기대 {code})")
    assert not wrong, wrong
    # 공개 상담 신청은 로그인 없이 열리고 꺼져 있으면 폼이 없다
    r = anon(env).get("/consult")
    assert r.status_code == 200 and 'name="consent"' not in r.text and "신청을 받지 않습니다" in r.text


def test_s4_posts_without_token_are_403(env):
    main = login(env, "admin")
    wrong = [p for p in S4_POSTS if main.post(p, data={"action": "open", "agree": "yes"}, raw=True).status_code != 403]
    wrong += [f"anon {p}" for p in ("/consult",) if anon(env).post(p, data={"consent": "1"}, raw=True).status_code != 403]
    assert not wrong, wrong


def _writes(con) -> dict:
    return {t: count(con, f"SELECT COUNT(*) FROM {t}") for t in
            ("ai_budget", "ai_call", "ai_output", "ai_review", "job", "career_round", "career_decision", "excellence_selection",
             "sales_lead", "sales_assign_log", "sales_customer")
            if con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()}


def test_s4_main_only_writes_refuse_manager_and_agent(env, fake):
    """승인, 선발, 무르기, 한도 수정, 심사 요청과 결재, 묻고 답하기, 배정 화면의 메인 관리자 일은 토큰이 맞아도 관리자와 설계사에게 403 이다."""
    con = env["con"]
    clients = roles(env)
    agent_lead = con.execute("SELECT lead_id FROM sales_lead WHERE agent_id=? LIMIT 1", (env["agent"],)).fetchone()
    posts = [("/program/career", {"action": "open", "agree": "yes"}),
             ("/program/career", {"action": "approve", "agree": "yes", "note": "시험 승인", "round": "1", "agent": str(env["agent"])}),
             ("/program/career", {"action": "revoke", "agree": "yes", "note": "시험 무르기", "decision": "1"}),
             ("/program/excellence", {"action": "select", "agree": "yes", "note": "시험 선발", "agent": str(env["agent"]), "prog": "X", "year": "2026"}),
             ("/program/excellence", {"action": "revoke", "agree": "yes", "note": "시험 무르기", "sel": "1"}),
             ("/org/ai", {"feature": "plan_qa", "limit_usd": "999"}),
             ("/review/requests", {"kind": "notice", "audience": "internal", "text": "내부 안내입니다."}),
             ("/review/results", {"review_id": "1", "action": "approve"}),
             ("/data/plan-qa", {"question": "시책은 어떻게 되나요?"})]
    posts += [("/sales/assign", {"action": a, "lead_id": str(agent_lead[0] if agent_lead else 1), "org_id": str(env["mgr"]["org_id"]),
                                 "agent_id": str(env["agent"]), "reason": "사유를 적습니다", "confirm": "1", "customer_id": "1"})
              for a in ("pool", "add", "handover", "confirm", "reveal")]
    before = _writes(con)
    wrong = []
    for role in ("manager", "agent"):
        for path, data in posts:
            got = clients[role].post(path, data=data).status_code
            if got != 403:
                wrong.append(f"{role} {path} {data.get('action', '')}: {got}")
    assert not wrong, wrong
    assert _writes(con) == before, "막힌 요청이 표를 바꾸면 안 된다"
    assert not fake.transport.calls, "막힌 요청이 AI 를 부르면 안 된다"


def test_s4_out_of_scope_is_refused(env):
    con = env["con"]
    clients = roles(env)
    mgr, agent = clients["manager"], clients["agent"]
    other = env["outside"][0]
    wrong = []
    for path in ("/program/cert", "/program/executive"):
        if mgr.get(f"{path}?who={other}").status_code != 403:
            wrong.append(f"manager {path} who={other}")
    if mgr.get(f"/team/coaching?agent={other}").status_code != 404:
        wrong.append("manager /team/coaching 범위 밖 팀원")
    # 범위 밖 사람의 DB
    lid = con.execute("SELECT lead_id FROM sales_lead WHERE agent_id=? LIMIT 1", (other,)).fetchone()
    if lid is None:
        lid = con.execute("SELECT lead_id FROM sales_lead WHERE agent_id IS NOT NULL AND agent_id NOT IN (%s) LIMIT 1"
                          % ",".join(str(a) for a in env["mgr"]["ctx"]["agent_ids"])).fetchone()
    assert lid, "범위 밖 DB 가 샘플에 있어야 한다"
    if mgr.get(f"/sales/assign?lead={lid[0]}").status_code != 404:
        wrong.append(f"manager /sales/assign lead={lid[0]}")
    mine = {r[0] for r in con.execute("SELECT lead_id FROM sales_lead WHERE agent_id=?", (env["agent"],))}
    theirs = con.execute("SELECT lead_id FROM sales_lead WHERE agent_id IS NOT NULL AND agent_id<>? LIMIT 1", (env["agent"],)).fetchone()[0]
    assert theirs not in mine
    if agent.get(f"/sales/leads?id={theirs}").status_code != 404:
        wrong.append(f"agent /sales/leads id={theirs}")
    # 범위 밖 사람에게 배정, 범위 밖 사람의 지역 정하기
    new = con.execute("SELECT lead_id FROM sales_lead WHERE status='new' AND pool_org_id IS NOT NULL LIMIT 1").fetchone()
    if new and mgr.post("/sales/assign", data={"action": "assign", "lead_id": str(new[0]), "agent_id": str(other)}).status_code not in (403, 404):
        wrong.append("manager 범위 밖 사람에게 배정")
    if mgr.post("/sales/assign", data={"action": "regions", "agent_id": str(other), "region": "제주"}).status_code != 403:
        wrong.append("manager 범위 밖 사람의 지역")
    assert not wrong, wrong
    # 관리자 화면에 범위 밖 사번이 나오지 않는다
    codes = {r[0] for r in con.execute(f"SELECT internal_code FROM agent WHERE agent_id IN ({','.join(map(str, env['outside']))})")}
    leaks = []
    for path in ("/program/career", "/program/cert", "/program/executive", "/program/excellence", "/sales/assign", "/team/activity", "/team/coaching"):
        html = mgr.get(path).text
        leaks += [f"{path} 범위 밖 {sum(1 for c in codes if re.search(r'(?<![A-Z0-9])' + c + r'(?![0-9])', html))}건"] \
            if any(re.search(r"(?<![A-Z0-9])" + c + r"(?![0-9])", html) for c in codes) else []
    assert not leaks, leaks


# ================================================================ 2. 배정에서 리포트 앞까지 한 건
def test_s4_flow_consult_assign_activity_customer(env, monkeypatch):
    con = env["con"]
    aid = env["agent"]
    code = con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (aid,)).fetchone()[0]
    ratelimit.LIMITER.reset()
    # 꺼짐: 폼도 신청도 없다
    monkeypatch.delenv("APP_CONSULT_OPEN", raising=False)
    n0 = count(con, "SELECT COUNT(*) FROM sales_lead")
    c = anon(env)
    form = {"name": "검증고객", "phone": FAKE_PHONE, "region": TEST_REGION, "time": "weekday_pm", "consent": "1", "website": ""}
    assert c.post("/consult", data=form).status_code == 403
    assert count(con, "SELECT COUNT(*) FROM sales_lead") == n0
    # 관리자가 이 설계사의 맡을 지역을 정한다. 다른 사람은 그 지역을 맡지 않는다
    assert count(con, "SELECT COUNT(*) FROM sales_region_pref WHERE region=?", TEST_REGION) == 0
    mgr = login(env, env["mgr"]["code"])
    assert mgr.post("/sales/assign", data={"action": "regions", "agent_id": str(aid), "region": TEST_REGION}).status_code == 303
    assert [r[0] for r in con.execute("SELECT region FROM sales_region_pref WHERE agent_id=?", (aid,))] == [TEST_REGION]
    # 켬: 동의가 없으면 받지 않는다
    monkeypatch.setenv("APP_CONSULT_OPEN", "1")
    ratelimit.LIMITER.reset()
    c = anon(env)
    page = c.get("/consult")
    assert page.status_code == 200 and 'name="consent"' in page.text and "준법 확인 전" in page.text
    assert c.post("/consult", data={**form, "consent": ""}).status_code == 400
    assert count(con, "SELECT COUNT(*) FROM sales_lead") == n0
    r = c.post("/consult", data=form)
    assert r.status_code == 200 and "신청을 받았습니다" in r.text
    assert code not in r.text and "배정" not in r.text, "신청 화면에 담당이나 배정을 내보내지 않는다"
    lead = con.execute("SELECT * FROM sales_lead WHERE phone=?", (core.clean_phone(FAKE_PHONE),)).fetchone()
    assert lead and lead["source"] == "landing" and lead["region"] == TEST_REGION and lead["consent_at"]
    assert lead["agent_id"] == aid and lead["status"] == "assigned", "지역 매칭으로 그 지역을 맡은 설계사에게 배정"
    log = con.execute("SELECT action, by_login FROM sales_assign_log WHERE lead_id=?", (lead["lead_id"],)).fetchall()
    assert [tuple(x) for x in log] == [("auto", "consult")]
    # 접속 IP 는 어디에도 저장하지 않는다. 시험 클라이언트의 주소는 testclient 다
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        cols = [x[1] for x in con.execute(f"PRAGMA table_info({t})")]
        for col in cols:
            hit = con.execute(f'SELECT COUNT(*) FROM {t} WHERE CAST("{col}" AS TEXT) LIKE ?', ("%testclient%",)).fetchone()[0]
            assert hit == 0, f"{t}.{col} 에 접속 주소가 남았다"
    lid = lead["lead_id"]
    # 설계사: 목록은 연락처를 가리고, 상세는 원문과 원문 보기 기록
    agent = login(env, env["users"][aid])
    lst = agent.get("/sales/leads").text
    assert FAKE_PHONE not in lst and core.mask_phone(FAKE_PHONE) in lst
    det = agent.get(f"/sales/leads?id={lid}").text
    assert FAKE_PHONE in det
    v = con.execute("SELECT after_json FROM audit_log WHERE action='sales_contact_view' AND target=? ORDER BY id DESC", (f"lead:{lid}",)).fetchone()
    assert v and json.loads(v[0])["why"] == "own" and "0000" not in v[0]
    # 활동 기록과 고객으로 바꾸기
    today = date.today().isoformat()
    for kind in ("call", "consult", "proposal"):
        assert agent.post("/sales/leads", data={"action": "activity", "lead_id": str(lid), "kind": kind, "done_on": today,
                                                "result": "검증 활동"}).status_code == 303
    assert con.execute("SELECT status FROM sales_lead WHERE lead_id=?", (lid,)).fetchone()[0] == "working"
    r = agent.post("/sales/leads", data={"action": "convert", "lead_id": str(lid)})
    assert r.status_code == 303 and r.headers["location"].startswith("/sales/customers?id=")
    cid = int(r.headers["location"].split("=")[1])
    cust = con.execute("SELECT * FROM sales_customer WHERE customer_id=?", (cid,)).fetchone()
    assert cust["lead_id"] == lid and cust["agent_id"] == aid
    assert con.execute("SELECT status FROM sales_lead WHERE lead_id=?", (lid,)).fetchone()[0] == "won"
    cpage = agent.get("/sales/customers").text
    assert FAKE_PHONE not in cpage, "고객 목록은 연락처를 가린다"
    assert agent.get(f"/sales/customers?id={cid}").status_code == 200
    # 리포트는 제작지침 대기다. 그 앞까지 이어졌는지만 본다
    rep = agent.get("/sales/report")
    assert rep.status_code == 200 and "제작지침 대기" in rep.text
    # 숫자 대조 1: 배정 화면의 체결률이 기록과 같다
    total = count(con, "SELECT COUNT(*) FROM sales_lead WHERE agent_id=?", aid)
    won = count(con, "SELECT COUNT(*) FROM sales_lead WHERE agent_id=? AND status='won'", aid)
    row = _row_of(mgr.get("/sales/assign").text, code)
    assert row, "배정 화면에 그 설계사의 줄이 있어야 한다"
    assert f"{won}/{total}건" in row and (f"{round(won / total * 100, 1):g}%" in row)
    # 숫자 대조 2: 활동 깔때기의 사람별 건수가 활동 기록과 같다
    ym = date.today().strftime("%Y-%m")
    want = {k: count(con, "SELECT COUNT(*) FROM sales_activity WHERE agent_id=? AND substr(done_on,1,7)=? AND kind=?", aid, ym, k)
            for k in ("call", "consult", "proposal", "application")}
    want_won = count(con, """SELECT COUNT(*) FROM sales_customer WHERE agent_id=? AND lead_id IS NOT NULL AND handed_from_agent_id IS NULL
                             AND substr(created_at,1,7)=?""", aid, ym)
    arow = _row_of(mgr.get("/team/activity").text, code)
    assert arow, "활동 깔때기에 그 설계사의 줄이 있어야 한다"
    assert f"통화 {want['call']} · 상담 {want['consult']} · 제안 {want['proposal']} · 청약 {want['application']}" in arow
    cells = re.findall(r'<td class="num(?: hide-sm)?">(\d+)', arow)
    assert cells[:5] == [str(want["call"]), str(want["consult"]), str(want["proposal"]), str(want["application"]), str(want_won)]
    # 메인 관리자 전체 합계도 같은 기록을 센다
    main = login(env, "admin")
    tot = {k: count(con, "SELECT COUNT(*) FROM sales_activity WHERE substr(done_on,1,7)=? AND kind=?", ym, k) for k in ("call",)}
    assert f"/ {tot['call']}건" in main.get("/team/activity").text


# ================================================================ 3. 영업 원문 보기 D-12, D-05 와 같은 직속 관리자
def test_s4_contact_reveal_rules_and_log(env):
    con = env["con"]
    lid = con.execute("SELECT lead_id, phone FROM sales_lead WHERE agent_id=? ORDER BY lead_id LIMIT 1", (env["agent"],)).fetchone()
    assert lid
    lead_id, phone = lid
    url = f"/sales/assign?lead={lead_id}"
    head_html = login(env, env["mgr"]["code"]).get(url).text
    upper_html = login(env, env["upper"]["code"]).get(url).text
    main = login(env, "admin")
    main_html = main.get(url).text
    assert phone in head_html, "직속 관리자는 원문을 본다"
    assert phone not in upper_html and core.mask_phone(phone) in upper_html, "위 조직 관리자는 가린 채로 본다"
    assert phone not in main_html, "메인 관리자는 사유를 적기 전에는 가린다"
    n = count(con, "SELECT COUNT(*) FROM audit_log WHERE action='sales_contact_view' AND target=?", f"lead:{lead_id}")
    assert main.post("/sales/assign", data={"action": "reveal", "lead_id": str(lead_id), "reason": "짧음"}).status_code == 400
    r = main.post("/sales/assign", data={"action": "reveal", "lead_id": str(lead_id), "reason": "민원 확인을 위한 조회"})
    assert r.status_code == 200 and phone in r.text
    logs = [json.loads(x[0]) for x in con.execute(
        "SELECT after_json FROM audit_log WHERE action='sales_contact_view' AND target=? ORDER BY id", (f"lead:{lead_id}",))]
    assert len(logs) == n + 1 and logs[-1] == {"why": "main", "reason": "민원 확인을 위한 조회"}
    assert all(phone not in json.dumps(x, ensure_ascii=False) for x in logs), "원문 보기 기록에 연락처를 남기지 않는다"


# ================================================================ 4. AI 산출물: 심사, 회전, 사람 결재, 기록
def test_s4_ai_review_rounds_gate_and_records(env, fake):
    con = env["con"]
    main = login(env, "admin")
    text = "보장 내용을 3가지로 점검해 드립니다."
    revised = "보장 내용을 3가지로 차분히 점검해 드립니다."
    seen = []

    def themis(user):
        seen.append("지난 회전의 의견" in user)
        return ({"verdict": "revise", "quote": text, "reason": "어조를 다듬습니다", "revised": revised} if len(seen) == 1
                else {"verdict": "pass", "quote": None, "reason": "문제 없음", "revised": None})
    fake.themis = themis
    assert main.post("/review/requests", data={"kind": "cardnews", "audience": "customer", "title": "검증 카드뉴스", "text": text}).status_code == 303
    job = con.execute("SELECT params FROM job WHERE kind='review_submit' ORDER BY job_id DESC LIMIT 1").fetchone()[0]
    assert text not in job, "작업 표에 심사할 글을 넣지 않는다"
    done = jobs.drain(con)
    res = done[-1]["result"]
    assert res["status"] == "passed" and res["rounds"] == 2
    assert seen == [False, True], "첫 회전은 다른 자리 의견 없이, 둘째 회전부터 앞 회전 의견과 함께"
    rid = res["review_id"]
    d = review.get_review(con, rid)
    assert d["draft_text"] == text and d["current_text"] == revised and d["required"] == 1
    assert d["input_hash"] and d["guide_version"] and d["panel_version"] and d["forbidden_version"]
    assert con.execute("SELECT adopted FROM ai_review_opinion WHERE review_id=? AND round_no=1 AND seat_key='themis'", (rid,)).fetchone()[0] == 1
    # 다빈치의 형상화가 회전마다 먼저 나온다
    order = [r[0] for r in con.execute("SELECT seat_key FROM ai_review_opinion WHERE review_id=? ORDER BY round_no, opinion_id", (rid,))]
    assert order == ["davinci", "themis", "davinci", "themis"]
    # 고객용 글을 사람이 고쳐 승인하려 하면 막힌다
    main.post("/review/results", data={"review_id": rid, "action": "approve", "final_text": revised + " 꼭 확인하세요."})
    assert review.get_review(con, rid)["status"] == "passed"
    assert main.post("/review/results", data={"review_id": rid, "action": "approve"}).status_code == 303
    d = review.get_review(con, rid)
    assert d["status"] == "approved" and d["final_text"] == revised and d["decided_by"] == "admin"
    assert review.can_release(con, rid, revised) and not review.can_release(con, rid, text)
    assert con.execute("SELECT 1 FROM audit_log WHERE action='ai_review_approve' AND target LIKE ?", (f"review:{rid}%",)).fetchone()
    # 통과하지 못한 고객용 글은 승인할 수 없다. 반려는 사유가 있어야 한다
    fake.themis = lambda user: {"verdict": "revise", "quote": None, "reason": "더 다듬어야 합니다", "revised": None}
    main.post("/review/requests", data={"kind": "landing", "audience": "customer", "text": "상담 신청 안내입니다."})
    res = jobs.drain(con)[-1]["result"]
    assert res["status"] == "stopped" and res["rounds"] == 3
    rid2 = res["review_id"]
    main.post("/review/results", data={"review_id": rid2, "action": "approve"})
    assert review.get_review(con, rid2)["status"] == "stopped" and not review.can_release(con, rid2)
    main.post("/review/results", data={"review_id": rid2, "action": "reject"})
    assert review.get_review(con, rid2)["status"] == "stopped"
    main.post("/review/results", data={"review_id": rid2, "action": "reject", "reason": "문구 다시 쓰기"})
    assert review.get_review(con, rid2)["status"] == "rejected"
    # 내부용 글은 D-15 제안대로 멈춘 채로도 사람이 승인할 수 있다
    main.post("/review/requests", data={"kind": "notice", "audience": "internal", "text": "내부 안내입니다."})
    rid3 = jobs.drain(con)[-1]["result"]["review_id"]
    assert review.get_review(con, rid3)["status"] == "stopped"
    main.post("/review/results", data={"review_id": rid3, "action": "approve"})
    assert review.get_review(con, rid3)["status"] == "approved"
    # 호출 기록: 토큰과 비용만 있고 글은 없다
    cols = {r[1] for r in con.execute("PRAGMA table_info(ai_call)")}
    assert not cols & {"prompt", "system", "user", "text", "output_text", "response", "api_key"}
    blob = json.dumps([dict(r) for r in con.execute("SELECT * FROM ai_call")], ensure_ascii=False, default=str)
    assert text not in blob and "차분한 안내" not in blob


# ================================================================ 5. AI 컨텍스트의 개인정보
def test_s4_ai_context_has_no_names_or_contacts(env, fake):
    con = env["con"]
    main = login(env, "admin")
    line = _approved_fc_line(con)
    assert line, "샘플에 승인된 FC 시상안 줄이 있어야 한다"
    fake.qa = "원안을 확인해 주세요 [1]"
    assert main.post("/data/plan-qa", data={"question": _qa_question(line)}).status_code == 200
    manual = con.execute("SELECT line_id, plan_doc_id FROM incentive_plan_line WHERE parse_status='manual' LIMIT 1").fetchone()
    if manual:
        fake.interpret = json.dumps({"line_kind": "note", "components": [], "amount_status": "원문 확인", "quote": None})
        main.post("/data/plan", data={"action": "ai_interpret", "doc": str(manual[1]), "line": str(manual[0])})
    main.post("/review/requests", data={"kind": "notice", "audience": "internal", "text": "보장 점검 안내입니다."})
    jobs.drain(con)
    assert fake.transport.calls, "가짜 클라이언트를 거쳐야 한다"
    sent = "\n".join(c["system"] + "\n" + c["user"] for c in fake.transport.calls)
    names = {r[0] for r in con.execute("SELECT name FROM agent WHERE length(name)>=2")}
    names |= {r[0] for r in con.execute("SELECT name FROM sales_lead UNION SELECT name FROM sales_customer")}
    phones = {r[0] for r in con.execute("SELECT phone FROM sales_lead UNION SELECT phone FROM sales_customer") if r[0]}
    assert sum(1 for n in names if n in sent) == 0, "AI 에 사람 이름이 들어갔다"
    assert sum(1 for p in phones if p in sent or p.replace("-", "") in sent) == 0, "AI 에 연락처가 들어갔다"
    assert not aictx.RRN.search(sent) and not aictx.PHONE.search(sent)
    stored = json.dumps([r[0] for r in con.execute("SELECT context_json FROM ai_output")], ensure_ascii=False)
    assert sum(1 for n in names if n in stored) == 0 and not aictx.PHONE.search(stored)


def test_s4_review_request_refuses_contact(env, fake):
    con = env["con"]
    main = login(env, "admin")
    n = count(con, "SELECT COUNT(*) FROM job WHERE kind='review_submit'")
    r = main.post("/review/requests", data={"kind": "notice", "audience": "customer", "text": f"문의는 {FAKE_PHONE} 로 주세요."})
    assert r.status_code == 303 and count(con, "SELECT COUNT(*) FROM job WHERE kind='review_submit'") == n


RRN_PLAIN = "900101" + "1234567"                    # 줄표 없는 주민번호 꼴
RRN_FOREIGN = "900101" + "-" + "5234567"            # 외국인 등록번호 꼴. 뒷자리 첫 숫자 5 에서 8
PHONE_INTL = "+82 " + "10-" + "0000" + "-" + "7788"  # 국가 번호를 붙인 휴대폰


def test_mask_catches_rrn_without_hyphen():
    m = aictx.Masker()
    m(f"주민번호 {RRN_PLAIN}")
    assert m.count == 1


def test_mask_catches_foreigner_registration_number():
    m = aictx.Masker()
    m(f"등록번호 {RRN_FOREIGN}")
    assert m.count == 1


def test_mask_catches_international_phone():
    m = aictx.Masker()
    m(f"연락처 {PHONE_INTL}")
    assert m.count == 1


def test_review_submit_refuses_rrn_without_hyphen(env, fake):
    con = env["con"]
    with pytest.raises(ValueError):
        review.submit(con, env["main_ctx"] | {"role": "main", "login_id": "admin"}, kind="notice", audience="internal",
                      text=f"고객 확인용 {RRN_PLAIN} 입니다.", client=AIClient(con, fake.transport))
    assert not any(RRN_PLAIN in c["user"] for c in fake.transport.calls)


# ================================================================ 6. 근거에 없는 숫자
def test_s4_qa_number_not_in_evidence_is_blocked(env, fake):
    con = env["con"]
    main = login(env, "admin")
    line = _approved_fc_line(con)
    q = _qa_question(line)
    fake.qa = "이 시책은 987% 를 드립니다 [1]"
    r = main.post("/data/plan-qa", data={"question": q})
    assert r.status_code == 200 and plan_qa.BLOCKED in r.text and "987%" not in r.text
    row = con.execute("SELECT status, problems_json FROM ai_output WHERE feature='plan_qa' ORDER BY output_id DESC LIMIT 1").fetchone()
    assert row["status"] == "blocked" and "987" in row["problems_json"]
    fake.qa = "원안을 확인해 주세요 [1]"
    r = main.post("/data/plan-qa", data={"question": q})
    assert "원안을 확인해 주세요 [1]" in r.text and plan_qa.NOTICE in r.text


def test_qa_number_equal_to_today_is_still_blocked(env):
    con = env["con"]
    line = _approved_fc_line(con)
    q = _qa_question(line)
    ctx = env["main_ctx"] | {"role": "main", "login_id": "admin"}
    filters = plan_qa.parse_question(con, q, date(2026, 10, 1))
    ev = "\n".join(plan_qa._line_text(r) for r in plan_qa.find_lines(con, filters))
    allowed_by_source = aictx.numbers(ev + "\n" + q)
    day = next(d for d in range(28, 1, -1) if str(d) not in allowed_by_source)
    out = plan_qa.ask(con, ctx, q, today=date(2026, 10, day), client=AIClient(con, FakeTransport([f"이 시책은 {day}% 를 드립니다 [1]"])))
    assert out["status"] == "blocked", "근거 줄에 없는 숫자"


# ================================================================ 7. 한도와 키
def test_s4_monthly_limit_stops_ai(env, fake):
    con = env["con"]
    main = login(env, "admin")
    line = _approved_fc_line(con)
    fake.qa = "원안을 확인해 주세요 [1]"
    try:
        for f in ("plan_qa", "review"):
            assert main.post("/org/ai", data={"feature": f, "limit_usd": "0"}).status_code == 303
        assert con.execute("SELECT set_by FROM ai_budget WHERE feature='plan_qa' ORDER BY budget_id DESC LIMIT 1").fetchone()[0] == "admin"
        calls = len(fake.transport.calls)
        r = main.post("/data/plan-qa", data={"question": _qa_question(line)})
        assert BUDGET_MESSAGE in r.text and len(fake.transport.calls) == calls
        assert con.execute("SELECT status FROM ai_call WHERE feature='plan_qa' ORDER BY call_id DESC LIMIT 1").fetchone()[0] == "budget"
        main.post("/review/requests", data={"kind": "cardnews", "audience": "customer", "text": "보장 점검 안내입니다."})
        res = jobs.drain(con)[-1]["result"]
        assert res["status"] == "stopped" and len(fake.transport.calls) == calls
        rid = res["review_id"]
        assert review.get_review(con, rid)["stop_reason"] == "budget"
        main.post("/review/results", data={"review_id": rid, "action": "approve"})
        assert review.get_review(con, rid)["status"] == "stopped", "한도로 멈춘 고객용 글은 승인할 수 없다"
        assert "한도" in main.get("/org/ai").text
    finally:
        for f in ("plan_qa", "review"):
            main.post("/org/ai", data={"feature": f, "limit_usd": "20"})


def test_s4_no_key_turns_off_ai_only(env, off):
    con = env["con"]
    main = login(env, "admin")
    for p in AI_SCREENS:
        r = main.get(p)
        assert r.status_code == 200 and ai_view.OFF in r.text, p
    line = _approved_fc_line(con)
    r = main.get(f"/data/plan?doc={line['plan_doc_id']}&line={line['line_id']}")
    assert r.status_code == 200 and ai_view.OFF in r.text
    n = count(con, "SELECT COUNT(*) FROM job WHERE kind='review_submit'")
    main.post("/review/requests", data={"kind": "notice", "audience": "internal", "text": "내부 안내입니다."})
    assert count(con, "SELECT COUNT(*) FROM job WHERE kind='review_submit'") == n
    r = main.post("/data/plan-qa", data={"question": _qa_question(line)})
    assert ai_view.OFF in r.text
    clients = roles(env)
    for role, paths in {"main": ("/", "/program/career", "/sales/assign", "/team/activity", "/data/plan"),
                        "manager": ("/", "/program/cert", "/sales/leads", "/team/coaching", "/my/career"),
                        "agent": ("/", "/my/career", "/my/cert", "/sales/leads", "/sales/customers")}.items():
        for p in paths:
            assert clients[role].get(p).status_code == 200, f"{role} {p}"


# ================================================================ 8. 숫자 대조
def _basis_end(ptype, key, ym):
    if ptype == "quarter":
        return f"{key[:4]}{int(key[-1]) * 3:02d}"
    if ptype == "month":
        return key
    return ym


def test_s4_program_numbers_match_results(env):
    """커리어 판정의 실적과 목표 역산이 실적 요약(results.period_summary)과 같다. 역산은 다시 계산한 값과 같다."""
    con, ym = env["con"], env["ym"]
    ids = [r[0] for r in con.execute("SELECT agent_id FROM agent")]
    main = {"role": "main", "agent_id": None}
    col = {"new_count": "net_count"}
    bad, n = [], 0
    for r in career.career_status(con, ids, ym, viewer=main):
        for c in r["criteria"]:
            if c["metric"] in ("converted_premium", "net_count", "monthly_premium", "new_count") and c["actual"] is not None:
                s = rs.period_summary(con, [r["agent_id"]], c["period_type"], _basis_end(c["period_type"], c["basis_key"], ym))["total"]
                n += 1
                if abs((s[col.get(c["metric"], c["metric"])] or 0) - c["actual"]) > 0.5:
                    bad.append(f"agent {r['agent_id']} {c['metric']} {c['basis_key']}")
            b = c.get("backsolve")
            if b:
                if b["start_ym"] <= ym:
                    s = rs.period_summary(con, [r["agent_id"]], b["period_type"], ym)["total"]
                    if abs(s[col.get(c["metric"], c["metric"])] - b["so_far"]) > 0.5:
                        bad.append(f"agent {r['agent_id']} 역산 실적")
                exp = math.ceil(b["remaining"] / b["months_left"]) if b["remaining"] > 0 and b["months_left"] > 0 else 0
                if b["remaining"] != max(0, b["target"] - b["so_far"]) or b["monthly_needed"] != exp:
                    bad.append(f"agent {r['agent_id']} 역산 계산")
    assert n > 0 and not bad, bad
    goals = career.goal_backsolve(con, ids, ym, viewer=main)
    assert goals, "샘플에 이번 기간 목표가 있어야 한다"
    for g in goals:
        if not g["supported"]:
            continue
        s = rs.period_summary(con, [g["agent_id"]], g["period_type"], ym)["total"][g["metric"]]
        rem = max(0, g["target"] - g["so_far"])
        exp = math.ceil(rem / g["months_left"]) if rem and g["months_left"] > 0 else (0 if rem == 0 else None)
        if abs(s - g["so_far"]) > 0.5 or g["remaining"] != rem or g["monthly_needed"] != exp:
            bad.append(f"goal {g['goal_id']}")
    assert not bad, bad


def test_s4_my_career_and_summary_same_on_screen(env):
    """설계사 본인 화면에서 커리어패스의 분기 실적과 실적 요약의 분기 인정보험료가 같다."""
    con, ym = env["con"], env["ym"]
    main = {"role": "main", "agent_id": None}
    rows = career.career_status(con, list(env["users"]), ym, viewer=main)
    target = next((r for r in rows for c in r["criteria"] if c["metric"] == "converted_premium" and c["backsolve"]
                   and c["backsolve"]["start_ym"] <= ym and c["backsolve"]["so_far"]), None)
    assert target, "역산이 있는 설계사가 샘플에 있어야 한다"
    c = login(env, env["users"][target["agent_id"]])
    mine = c.get("/my/career").text
    so_far = re.search(r'data-col="so-far">([\d,]+)원', mine)
    summary = re.search(r'data-col="premium">([\d,]+)<', c.get(f"/my/summary?pt=quarter&ym={ym}").text)
    assert so_far and summary and so_far.group(1) == summary.group(1), f"agent {target['agent_id']}"


def test_s4_cert_d33_difference_is_labelled(env):
    """인증의 연 인정보험료는 D-33 가정으로 계약일 기준이라 실적 요약과 다를 수 있다. 다르면 화면에 기준 설명이 있어야 한다."""
    con, ym = env["con"], env["ym"]
    main = {"role": "main", "agent_id": None}
    rows = ce.cert_progress(con, list(env["users"]), viewer=main)["rows"]
    differ = []
    for r in rows:
        for m in r["metrics"]:
            if m["metric"] == "annual_recognized_premium" and m["actual"] is not None:
                s = rs.period_summary(con, [r["agent_id"]], "year", ym)["total"]["converted_premium"]
                if abs(s - m["actual"]) > 0.5:
                    differ.append(r["agent_id"])
    if differ:
        aid = differ[0]
        assert standards.BASIS_NOTE[:20] in login(env, env["users"][aid]).get("/my/cert").text
        assert standards.BASIS_NOTE[:20] in login(env, "admin").get("/program/cert").text


# ================================================================ 9. D-17
def test_s4_manager_never_sees_member_commission(env):
    con = env["con"]
    main = {"role": "main", "agent_id": None}
    members = sorted(env["mgr"]["ctx"]["agent_ids"] - {env["mgr"]["agent_id"]})
    secrets_ = set()
    for r in ce.cert_progress(con, members, viewer=main)["rows"] + ce.executive_status(con, members, viewer=main)["rows"]:
        for m in r["metrics"]:
            if m["metric"] in ce.MONEY_HIDDEN and m["actual"] and m["actual"] >= 10000:
                secrets_.add(f"{int(m['actual']):,}")
    assert secrets_, "샘플 팀원에게 수수료 지표가 있어야 한다"
    mgr = login(env, env["mgr"]["code"])
    leaks = []
    pages = ["/program/career", "/program/excellence", "/team/activity", "/sales/assign"]
    pages += [f"/program/cert?who={a}" for a in members[:5]] + [f"/program/executive?who={a}" for a in members[:5]]
    for p in pages:
        html = mgr.get(p).text
        n = sum(1 for s in secrets_ if s in html)
        if n:
            leaks.append(f"{p} {n}건")
    assert not leaks, leaks
