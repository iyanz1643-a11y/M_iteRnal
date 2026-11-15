"""앱 안 AI 화면: 심사 요청과 결과, 시상안 해석 초안, 시상안 묻고 답하기, AI 사용량과 한도, 꺼짐 안내, 제작지침 대기.

실제 API 는 부르지 않는다. app/ops/jobs.ai_client 를 바꿔 FakeTransport 를 넣는다. 키가 없는 경우는 진짜 AIClient 를 키 없이 쓴다.
연락처 꼴은 저장소 점검에 걸리지 않게 조각을 이어 만든다.
"""
import json
import os
import pathlib
import re
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db, masters, samples
from app.ai import plan_qa, review
from app.ai.client import AIClient, FakeTransport
from app.loader import incentive_plan
from app.ops import jobs
from app.web import ai_view, auth

PW = "test-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
PHONE = "010" + "-" + "2468" + "-" + "1357"
AI_PATHS = ("/review/requests", "/review/results", "/data/plan-qa", "/org/ai")


class Client(TestClient):
    """폼을 보낼 때 화면의 숨은 CSRF 토큰을 받아 함께 보낸다."""
    def post(self, url, data=None, **kw):
        if data is not None and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


@pytest.fixture(scope="module")
def env():
    tmp = pathlib.Path(tempfile.mkdtemp())
    path = tmp / "t.db"
    con = db.init_db(path)
    masters.seed(con)
    auth.seed_accounts(con, PW)
    incentive_plan.load(con, samples.write_incentive_plan(tmp), "202610")
    line = lambda ins, cat, lvl: con.execute(
        "SELECT line_id, plan_doc_id FROM incentive_plan_line WHERE insurer_name=? AND category_raw=? AND recipient_level=? ORDER BY row_no LIMIT 1",
        (ins, cat, lvl)).fetchone()
    hb, tier = line("한빛손해", "인보험", "fc"), line("대한생명", "업적 구간 시책", "fc")
    incentive_plan.review(con, hb[0], True, by="admin")
    saved = {k: os.environ.get(k) for k in ("APP_DB", "APP_UPLOAD_DIR", "ANTHROPIC_API_KEY")}
    os.environ["APP_DB"] = str(path)
    os.environ["APP_UPLOAD_DIR"] = str(tmp / "uploads")
    os.environ.pop("ANTHROPIC_API_KEY", None)
    from app.web.main import app
    manager = con.execute("SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id WHERE o.level='branch' LIMIT 1").fetchone()[0]
    agent = con.execute("SELECT internal_code FROM agent WHERE agent_id NOT IN (SELECT agent_id FROM org_manager) LIMIT 1").fetchone()[0]
    yield {"con": con, "app": app, "hb": hb[0], "tier": tier[0], "doc": hb[1], "manager": manager, "agent": agent, "uploads": tmp / "uploads"}
    con.close()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def client_for(env, login_id):
    c = Client(env["app"], follow_redirects=False)
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, r.text
    return c


class Fake:
    """기능마다 답을 바꿀 수 있는 가짜. 시스템 지침의 머리로 기능을 가른다."""

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


def panel(r):
    """시상안 화면에서 줄 상세 묶음만."""
    t = r.text
    return t[t.index('id="ai"'):t.index("</article>", t.index('id="ai"'))]


def html_text(r):
    return re.sub(r"<[^>]+>", " ", r.text)


# ================================================================ 메인 관리자만
def test_ai_screens_are_main_only(env, fake):
    main, mgr, agt = client_for(env, "admin"), client_for(env, env["manager"]), client_for(env, env["agent"])
    for p in AI_PATHS:
        assert main.get(p).status_code == 200, p
        assert mgr.get(p).status_code == 403, p
        assert agt.get(p).status_code == 403, p
    # 상태를 바꾸는 요청도 guard 가 막는다. 막혔으니 작업도 한도도 남지 않는다
    before_jobs = env["con"].execute("SELECT COUNT(*) FROM job").fetchone()[0]
    before_budget = env["con"].execute("SELECT COUNT(*) FROM ai_budget").fetchone()[0]
    assert mgr.post("/review/requests", data={"kind": "notice", "audience": "agent", "text": "안내입니다."}).status_code == 403
    assert mgr.post("/review/results", data={"review_id": "1", "action": "approve"}).status_code == 403
    assert mgr.post("/data/plan-qa", data={"question": "한빛손해 2026년 10월 인보험"}).status_code == 403
    assert mgr.post("/org/ai", data={"feature": "plan_qa", "limit_usd": "1"}).status_code == 403
    assert agt.post("/data/plan", data={"action": "ai_interpret", "doc": env["doc"], "line": env["tier"]}).status_code == 403
    assert mgr.get(f"/data/plan?doc={env['doc']}&line={env['tier']}").status_code == 403
    assert env["con"].execute("SELECT COUNT(*) FROM job").fetchone()[0] == before_jobs
    assert env["con"].execute("SELECT COUNT(*) FROM ai_budget").fetchone()[0] == before_budget
    # 메뉴에도 메인 관리자에게만 보인다
    assert "/data/plan-qa" in main.get("/").text and "/org/ai" in main.get("/").text
    assert "/data/plan-qa" not in mgr.get("/").text and "/review/requests" not in agt.get("/").text


# ================================================================ 키가 없으면 꺼짐
def test_no_key_shows_off_notice_and_other_screens_work(env, off):
    main = client_for(env, "admin")
    for p in AI_PATHS:
        r = main.get(p)
        assert r.status_code == 200 and ai_view.OFF in r.text, p
    # 심사 요청은 받지 않고 작업도 넣지 않는다
    n = env["con"].execute("SELECT COUNT(*) FROM job WHERE kind='review_submit'").fetchone()[0]
    r = main.post("/review/requests", data={"kind": "notice", "audience": "agent", "text": "교육 안내입니다."})
    assert r.status_code == 303
    assert ai_view.OFF in main.get("/review/requests").text
    assert env["con"].execute("SELECT COUNT(*) FROM job WHERE kind='review_submit'").fetchone()[0] == n
    # 해석 초안 단추는 막히고, 눌러도 부르지 않는다
    r = main.get(f"/data/plan?doc={env['doc']}&line={env['tier']}")
    assert r.status_code == 200 and ai_view.OFF in r.text and re.search(r'value="ai_interpret"\s+disabled', r.text)
    r = main.post("/data/plan", data={"action": "ai_interpret", "doc": env["doc"], "line": env["tier"]})
    assert ai_view.OFF in r.text
    assert not env["con"].execute("SELECT 1 FROM ai_output WHERE feature='plan_interpret' AND status='off'").fetchone()
    # 묻고 답하기는 답 대신 근거 줄만 보인다
    r = main.post("/data/plan-qa", data={"question": "한빛손해 2026년 10월 인보험 시책은요?"})
    assert r.status_code == 200 and ai_view.OFF in r.text and "원장 줄" in r.text and plan_qa.NOTICE in r.text
    # 다른 기능은 그대로
    for p in ("/", "/data/plan", "/data/jobs", "/org/audit"):
        assert main.get(p).status_code == 200, p


def test_report_and_cardnews_wait_for_guides(env, off):
    main, agt = client_for(env, "admin"), client_for(env, env["agent"])
    r = main.get("/sales/cardnews")
    assert r.status_code == 200 and "제작지침 대기" in r.text and "카드뉴스 초안" in r.text
    r = agt.get("/sales/report")
    assert r.status_code == 200 and "제작지침 대기" in r.text and "보장분석 리포트 초안" in r.text
    assert "제작지침 대기" not in main.get("/data/plan").text


# ================================================================ 심사 결재 흐름
def _submit(env, c, **data):
    before = env["con"].execute("SELECT COALESCE(MAX(job_id),0) FROM job").fetchone()[0]
    r = c.post("/review/requests", data=data)
    assert r.status_code == 303
    row = env["con"].execute("SELECT * FROM job WHERE kind='review_submit' AND job_id>?", (before,)).fetchone()
    assert row is not None, html_text(c.get("/review/requests"))
    return dict(row)


def test_review_request_runs_in_worker_then_human_decides(env, fake):
    con = env["con"]
    main = client_for(env, "admin")
    text = "약관에 따라 보장 내용을 점검해 드립니다."
    job = _submit(env, main, kind="cardnews", audience="customer", title="보장 점검 카드뉴스", text=text)
    # 심사할 글은 작업 표에 넣지 않는다. 요청 파일의 키와 요청한 아이디만 있다
    assert text not in job["params"] and set(json.loads(job["params"])) == {"key", "by"} and job["max_attempts"] == 1
    assert job["status"] == "queued" and "대기" in main.get("/review/requests").text
    done = jobs.drain(con)
    assert [j["status"] for j in done] == ["done"]
    res = done[0]["result"]
    assert res["status"] == "passed" and res["rounds"] == 1
    assert not list(env["uploads"].rglob("*.json"))                      # 요청 파일은 심사 뒤 지운다
    rid = res["review_id"]
    page = main.get("/review/requests").text
    assert f"/review/results?id={rid}" in page and "보장 점검 카드뉴스" in page
    r = main.get(f"/review/results?id={rid}")
    body = r.text
    assert r.status_code == 200 and "다빈치" in body and "차분한 안내" in body and "테미스" in body
    assert body.count(ai_view.SEAT_WAIT) >= 6 and "1<span class=\"unit\">/ 3회전" in body
    assert review.GATE in body and "D-15" in body and "필수 관문" in body
    # 사람의 결재. 고객용 글은 고친 칸 없이 그대로 승인한다
    assert 'name="final_text"' not in body
    r = main.post("/review/results", data={"review_id": rid, "action": "approve"})
    assert r.status_code == 303
    d = review.get_review(con, rid)
    assert d["status"] == "approved" and d["decided_by"] == "admin" and d["final_text"] == text
    assert review.can_release(con, rid, text)
    assert con.execute("SELECT 1 FROM audit_log WHERE action='ai_review_approve' AND target=?", (f"review:{rid}",)).fetchone()
    assert "승인" in main.get(f"/review/results?id={rid}").text


def test_customer_text_that_did_not_pass_cannot_be_approved(env, fake):
    con = env["con"]
    main = client_for(env, "admin")
    fake.themis = lambda user: {"verdict": "revise", "quote": "안내", "reason": "더 다듬어야 합니다", "revised": None}
    _submit(env, main, kind="landing", audience="customer", text="상담 신청 안내입니다.")
    res = jobs.drain(con)[0]["result"]
    assert res["status"] == "stopped" and res["stop"] == "rounds" and res["rounds"] == 3
    rid = res["review_id"]
    body = main.get(f"/review/results?id={rid}").text
    assert "3회전" in body and "고침 제안" in body and re.search(r'value="approve"\s+disabled', body)
    assert f"/review/requests?from={rid}" in body                        # 고쳐서 다시 올리기
    main.post("/review/results", data={"review_id": rid, "action": "approve"})
    assert review.get_review(con, rid)["status"] == "stopped"
    assert review.GATE in main.get(f"/review/results?id={rid}").text      # 막힌 까닭이 그대로 보인다
    main.post("/review/results", data={"review_id": rid, "action": "reject"})
    assert review.get_review(con, rid)["status"] == "stopped"             # 반려 사유가 있어야 한다
    main.post("/review/results", data={"review_id": rid, "action": "reject", "reason": "문구를 다시 씁니다"})
    d = review.get_review(con, rid)
    assert d["status"] == "rejected" and d["decision_reason"] == "문구를 다시 씁니다"
    # 다시 올리기 폼은 앞 심사의 글과 번호를 채운다
    page = main.get(f"/review/requests?from={rid}").text
    assert "상담 신청 안내입니다." in page and f'name="parent_id" value="{rid}"' in page


def test_internal_text_can_be_edited_when_approving(env, fake):
    con = env["con"]
    main = client_for(env, "admin")
    fake.themis = lambda user: {"verdict": "revise", "quote": None, "reason": "다듬기", "revised": None}
    _submit(env, main, kind="notice", audience="internal", text="월간 회의 안내입니다.")
    rid = jobs.drain(con)[0]["result"]["review_id"]
    body = main.get(f"/review/results?id={rid}").text
    assert 'name="final_text"' in body and "선택 심사" in body
    main.post("/review/results", data={"review_id": rid, "action": "approve", "final_text": "월간 회의 안내입니다. 장소는 본부입니다."})
    d = review.get_review(con, rid)
    assert d["status"] == "approved" and d["human_edited"] == 1


def _unreviewed_review(env, fake, main, audience):
    """테미스가 회전마다 다른 고친 문안을 내 3회전째 고침이 받힌 채 멈춘 심사를 만든다."""
    a, b = "정기 점검 안내를 드립니다.", "정기 점검을 안내해 드립니다."
    fake.themis = lambda user: {"verdict": "revise", "quote": None, "reason": "다듬기", "revised": b if a in user else a}
    _submit(env, main, kind="notice", audience=audience, text="정기 점검 안내입니다.")
    res = jobs.drain(env["con"])[0]["result"]
    assert res["status"] == "stopped" and res["stop"] == "rounds"
    d = review.get_review(env["con"], res["review_id"])
    assert d["unreviewed"] and d["unreviewed_ko"] == review.UNREVIEWED
    return res["review_id"]


def test_unreviewed_internal_text_needs_confirm(env, fake):
    con = env["con"]
    main = client_for(env, "admin")
    rid = _unreviewed_review(env, fake, main, "internal")
    body = main.get(f"/review/results?id={rid}").text
    assert review.UNREVIEWED in body and 'name="confirm_unreviewed"' in body
    assert body.index('name="confirm_unreviewed"') < body.index('value="approve"')   # 승인 단추 위에 있다
    # 확인 없이 승인하면 거절된다
    r = main.post("/review/results", data={"review_id": rid, "action": "approve"})
    assert r.status_code == 303 and review.get_review(con, rid)["status"] == "stopped"
    assert re.search(r'class="alert bad"[^>]*>' + re.escape(review.UNREVIEWED), main.get(f"/review/results?id={rid}").text)
    # CSRF 토큰이 틀리거나 없으면 확인을 붙여도 받지 않는다
    bad = {"review_id": rid, "action": "approve", "confirm_unreviewed": "1"}
    assert main.post("/review/results", data={**bad, "csrf_token": "wrong"}).status_code == 403
    assert TestClient.post(main, "/review/results", data=bad).status_code == 403
    assert review.get_review(con, rid)["status"] == "stopped"
    # 확인하면 승인된다
    r = main.post("/review/results", data=bad)
    assert r.status_code == 303
    d = review.get_review(con, rid)
    assert d["status"] == "approved" and d["decided_by"] == "admin" and review.can_release(con, rid)
    assert con.execute("SELECT 1 FROM audit_log WHERE action='ai_review_approve' AND target=?", (f"review:{rid}",)).fetchone()


def test_unreviewed_customer_text_stays_blocked_with_confirm(env, fake):
    con = env["con"]
    main = client_for(env, "admin")
    rid = _unreviewed_review(env, fake, main, "customer")
    body = main.get(f"/review/results?id={rid}").text
    assert review.UNREVIEWED in body and re.search(r'value="approve"\s+disabled', body)
    assert 'name="confirm_unreviewed"' not in body                     # 승인할 수 없는 글에는 확인 칸을 두지 않는다
    r = main.post("/review/results", data={"review_id": rid, "action": "approve", "confirm_unreviewed": "1"})
    assert r.status_code == 303 and review.get_review(con, rid)["status"] == "stopped"
    assert review.GATE in main.get(f"/review/results?id={rid}").text and not review.can_release(con, rid)


def test_review_request_refuses_contact_and_bad_kind(env, fake):
    con = env["con"]
    main = client_for(env, "admin")
    n = con.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    r = main.post("/review/requests", data={"kind": "notice", "audience": "agent", "text": f"문의는 {PHONE} 로 주세요."})
    assert r.status_code == 303 and review.PII_FOUND in main.get("/review/requests").text
    assert main.post("/review/requests", data={"kind": "report", "audience": "agent", "text": "x"}).status_code == 400
    assert main.post("/review/requests", data={"kind": "notice", "audience": "nobody", "text": "x"}).status_code == 400
    assert con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == n
    assert not list(env["uploads"].rglob("*.json"))


# ================================================================ 해석 초안 승인은 원장을 바꾸지 않는다
def _ledger(con):
    lines = [tuple(r) for r in con.execute("SELECT * FROM incentive_plan_line ORDER BY line_id")]
    comps = [tuple(r) for r in con.execute("SELECT * FROM incentive_plan_component ORDER BY component_id")]
    return lines, comps


def test_interpret_draft_approve_does_not_change_ledger(env, fake):
    con = env["con"]
    main = client_for(env, "admin")
    doc, line = env["doc"], env["hb"]
    before = _ledger(con)
    detail = main.get(f"/data/plan?doc={doc}&line={line}")
    assert detail.status_code == 200 and "AI 해석 초안" in detail.text and ai_view.LEDGER_SAFE in detail.text
    fake.interpret = json.dumps({"line_kind": "rate_promo", "pay_timing": "next_month", "period_from": "2026-10-01", "period_to": "2026-10-31",
                                 "components": [{"basis": "premium", "rate": 200, "pay_timing": "next_month"}], "quote": "200% (익월)"},
                                ensure_ascii=False)
    r = main.post("/data/plan", data={"action": "ai_interpret", "doc": doc, "line": line})
    assert r.status_code == 200 and "초안을 만들었습니다" in r.text
    assert "정률 시책" in r.text and "200%" in r.text and "200% (익월)" in r.text and "[1]" in r.text
    oid = con.execute("SELECT MAX(output_id) FROM ai_output WHERE feature='plan_interpret' AND ref_id=?", (line,)).fetchone()[0]
    r = main.post("/data/plan", data={"action": "ai_decide", "decision": "approve", "doc": doc, "line": line, "output": oid})
    assert r.status_code == 200 and "원장 값은 바뀌지 않았습니다" in r.text
    assert con.execute("SELECT decision, decided_by FROM ai_output WHERE output_id=?", (oid,)).fetchone()[:] == ("approved", "admin")
    assert _ledger(con) == before                                        # 원장 줄과 지급률 조각이 그대로다
    assert con.execute("SELECT 1 FROM audit_log WHERE action='ai_plan_approve'").fetchone()
    # 두 번 결재하지 않는다. 다른 줄의 초안 번호로는 결재하지 못한다
    r = main.post("/data/plan", data={"action": "ai_decide", "decision": "reject", "reason": "x", "doc": doc, "line": line, "output": oid})
    assert r.status_code == 400 and "이미 결재한" in r.text
    r = main.post("/data/plan", data={"action": "ai_decide", "decision": "approve", "doc": doc, "line": env["tier"], "output": oid})
    assert r.status_code == 400


def test_interpret_blocked_and_source_check(env, fake):
    con = env["con"]
    main = client_for(env, "admin")
    doc = env["doc"]
    # 지어낸 지급률은 막히고 내용이 보이지 않는다
    fake.interpret = json.dumps({"line_kind": "rate_promo", "components": [{"basis": "premium", "rate": 250}], "quote": "200% (익월)"}, ensure_ascii=False)
    r = main.post("/data/plan", data={"action": "ai_interpret", "doc": doc, "line": env["hb"]})
    box = panel(r)
    assert "자동 점검" in r.text and "250" in box and "× 250%" not in box and 'value="ai_decide"' not in box
    # 금액이 그림에만 있는 줄은 원문 확인으로 보인다
    fake.interpret = json.dumps({"line_kind": "tier", "components": [{"basis": "premium", "rate": 300}], "quote": "업적 구간 시책"}, ensure_ascii=False)
    r = main.post("/data/plan", data={"action": "ai_interpret", "doc": doc, "line": env["tier"]})
    box = panel(r)
    assert "원문 확인" in box and "× 300%" not in box
    # 반려는 사유가 있어야 한다
    oid = con.execute("SELECT MAX(output_id) FROM ai_output WHERE feature='plan_interpret'").fetchone()[0]
    r = main.post("/data/plan", data={"action": "ai_decide", "decision": "reject", "doc": doc, "line": env["tier"], "output": oid})
    assert r.status_code == 400
    r = main.post("/data/plan", data={"action": "ai_decide", "decision": "reject", "reason": "구간표를 원문으로 다시 봅니다", "doc": doc, "line": env["tier"], "output": oid})
    assert r.status_code == 200 and "반려했습니다" in r.text
    # 원장 표의 줄 상세 링크는 사람 확인이 필요한 줄에 붙는다
    assert f"line={env['tier']}#ai" in main.get(f"/data/plan?doc={doc}").text


# ================================================================ 묻고 답하기 근거 표시
def test_plan_qa_shows_answer_with_evidence_lines(env, fake):
    main = client_for(env, "admin")
    q = "한빛손해 2026년 10월 인보험 시책은 어떻게 되나요?"
    fake.qa = "인보험 시상은 200% 이고 익월에 지급합니다 [1]"
    r = main.post("/data/plan-qa", data={"question": q})
    body = r.text
    assert r.status_code == 200 and "인보험 시상은 200% 이고 익월에 지급합니다 [1]" in body
    assert f"원장 줄 {env['hb']}번" in body and "[1]" in body and plan_qa.NOTICE in body
    assert "한빛손해" in html_text(r) and "2026년 10월" in body
    # 근거에 없는 숫자는 막히고 답 대신 근거 줄만 보인다
    fake.qa = "인보험은 250% 를 드립니다 [1]"
    r = main.post("/data/plan-qa", data={"question": q})
    assert plan_qa.BLOCKED in r.text and "250% 를 드립니다" not in r.text and f"원장 줄 {env['hb']}번" in r.text
    # 근거가 없으면 부르지 않고 모른다고 한다
    calls = len(fake.transport.calls)
    r = main.post("/data/plan-qa", data={"question": "없는보험 2026년 5월 치아보험 시책은요?"})
    assert plan_qa.NO_EVIDENCE in r.text and len(fake.transport.calls) == calls
    assert main.post("/data/plan-qa", data={"question": " "}).status_code == 400


# ================================================================ 한도 수정과 CSRF
def test_limit_change_needs_csrf_token(env, fake):
    con = env["con"]
    main = client_for(env, "admin")
    n = con.execute("SELECT COUNT(*) FROM ai_budget").fetchone()[0]
    r = main.post("/org/ai", data={"feature": "plan_qa", "limit_usd": "7.5", "csrf_token": "wrong"})
    assert r.status_code == 403
    r = TestClient.post(main, "/org/ai", data={"feature": "plan_qa", "limit_usd": "7.5"})
    assert r.status_code == 403
    assert con.execute("SELECT COUNT(*) FROM ai_budget").fetchone()[0] == n
    r = main.post("/org/ai", data={"feature": "plan_qa", "limit_usd": "7.5"})
    assert r.status_code == 303
    row = con.execute("SELECT monthly_limit_usd, set_by FROM ai_budget WHERE feature='plan_qa' ORDER BY budget_id DESC LIMIT 1").fetchone()
    assert tuple(row) == (7.5, "admin")
    page = main.get("/org/ai").text
    assert "7.5 달러로 바꿨습니다" in page and "/ 7.5달러" in page
    assert con.execute("SELECT 1 FROM audit_log WHERE action='ai_limit_set' AND target='plan_qa=7.5'").fetchone()
    # 잘못된 값과 없는 기능
    main.post("/org/ai", data={"feature": "plan_qa", "limit_usd": "-1"})
    assert "사이의 숫자" in main.get("/org/ai").text
    assert main.post("/org/ai", data={"feature": "nothing", "limit_usd": "1"}).status_code == 400
    assert con.execute("SELECT COUNT(*) FROM ai_budget").fetchone()[0] == n + 1
    # 변경 기록에는 한국어 이름표로 보인다
    assert "AI 월 한도 바꿈" in main.get("/org/audit").text


def test_usage_counts_calls_and_shows_estimate(env, fake):
    main = client_for(env, "admin")
    fake.qa = "인보험 시상은 200% 이고 익월에 지급합니다 [1]"
    main.post("/data/plan-qa", data={"question": "한빛손해 2026년 10월 인보험 시책은요?"})
    body = main.get("/org/ai").text
    assert "시상안 묻고 답하기" in body and "비용 추정" in body and "추정" in body and "제작지침 대기" in body
    assert "AI_KEY" not in body and "x-api-key" not in body
