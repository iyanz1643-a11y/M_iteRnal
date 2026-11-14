"""교육 화면과 명함 연동 1차, 2차 화면의 시험. S2 파트 E.

세 역할의 접근, 범위 밖 수료 확인 거부, 명부 미리보기와 적재, 수료 코드 원문이 DB, 세션, 다음 화면에 없음,
관리자와 설계사의 발급 POST 거부, 내 명함 상태, 홈의 교육 기한 임박 건수를 본다.
명함 사이트는 httpx.MockTransport 로 흉내 내고 실제 네트워크는 막는다. 키는 실행할 때 무작위로 만든다.
"""
import base64
import json
import os
import re
import secrets
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, masters, samples_edu
from app.connect import card_codes, card_site
from app.connect.card_link import CardLinkTable
from app.connect.state import StateStore
from app.engine import education as edu
from app.loader import fake_edu_roster as fr
from app.web import audit_view, auth, menu
from app.web.routes import card as card_route

PW = "edu-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
BASE = "https://card-site.test"
REJECT_REASON = "시험용반려사유문구"                     # 사이트가 돌려주는 반려 사유. 화면에 나오면 안 된다
EDU_PATHS = ("/my/education", "/team/education", "/edu/courses", "/edu/sessions", "/edu/completions", "/data/courses", "/sales/card")


class FakeSite:
    """명함 사이트. 공개 상태, 수료 코드 발급과 발급 기록을 받는다. 받은 요청을 모두 남긴다."""

    def __init__(self, key):
        self.key = key
        self.status = {}                                 # slug -> (status, live)
        self.log = []                                    # 발급 기록. 코드 원문이 있다
        self.requests = []

    def posts(self):
        return [r for r in self.requests if r.method == "POST"]

    def codes(self):
        return [r["code"] for r in self.log]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/api/admin/") and request.headers.get("x-admin-key") != self.key:
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method == "GET" and path == "/api/status":
            slug = request.url.params.get("slug")
            st, live = self.status.get(slug, ("none", False))
            return httpx.Response(200, json={"slug": slug, "live": live, "status": st, "reason": REJECT_REASON if st == "rejected" else "",
                                             "liveAt": "2026-10-01T00:00:00Z" if live else None, "at": None, "decidedAt": None})
        if request.method == "GET" and path == "/api/admin/codes":
            return httpx.Response(200, json={"log": list(self.log)})
        if request.method == "POST" and path == "/api/admin/codes":
            body = json.loads(request.content or b"{}")
            rows = []
            for p in body["people"]:
                n = len(self.log) + 1
                code = f"ABC-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
                row = {"serial": f"{body['course']}{body['ym'][2:4]}{body['ym'][5:7]}-{n:03d}", "course": body["course"], "ym": body["ym"],
                       "name": p["name"], "phone": "", "code": code, "at": "2026-11-14T00:00:00Z", "status": "issued"}
                self.log.append(row)
                rows.append(row)
            return httpx.Response(200, json={"ok": True, "rows": rows})
        return httpx.Response(404, json={"error": "not_found"})


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리키고, 폼에는 화면의 CSRF 토큰을 넣는다."""

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
    monkeypatch.setenv("APP_UPLOAD_DIR", str(tmp_path / "uploads"))

    def _no_network(self, request):
        raise AssertionError("시험이 실제 네트워크를 부르려 했습니다")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _no_network)
    card_route.clear_cache()
    yield
    card_route.clear_cache()
    card_codes._INFLIGHT.clear()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """샘플 조직과 교육 샘플. 기한이 사흘 남은 필수 과정을 하나 더 두어 기한 임박이 생기게 한다."""
    path = tmp_path / "edu.db"
    con = db.init_db(path)
    masters.seed(con)
    samples_edu.seed(con)
    soon = edu.save_course(con, by="t", code="SOON-1", name="임박 시험 과정", kind="compliance", required=True, target_role="all",
                           deadline_rule="fixed", deadline_date=(date.today() + timedelta(days=3)).isoformat())
    auth.seed_accounts(con, PW)
    uid = lambda login: con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login,)).fetchone()[0]
    manager = con.execute("""SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id
                             WHERE o.level='branch' ORDER BY a.internal_code LIMIT 1""").fetchone()[0]
    mctx = auth.context(con, uid(manager))
    assert mctx["role"] == "manager"
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    active = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' AND job_type='fc' ORDER BY agent_id")]
    inside = next(a for a in active if a in mctx["agent_ids"] and a not in heads)
    outside = next(a for a in active if a not in mctx["agent_ids"])
    code = lambda a: con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (a,)).fetchone()[0]
    key = secrets.token_hex(8)
    state = {"site": FakeSite(key), "write": True, "key": key, "base": BASE}

    def fake_client(c, *, quick=False):
        return card_site.CardSiteClient(state["base"], state["key"], transport=httpx.MockTransport(state["site"]), state=StateStore(con=c),
                                        attempts=1, write=state["write"], sleep=lambda s: None)
    monkeypatch.setattr(card_route, "card_client", fake_client)
    from app.web.main import app
    yield {"con": con, "path": path, "app": app, "state": state, "tmp": tmp_path, "soon": soon,
           "manager": manager, "manager_agent": mctx["agent_id"], "manager_ids": mctx["agent_ids"],
           "inside": inside, "outside": outside, "code_in": code(inside), "code_out": code(outside)}
    con.close()


def login(env, login_id):
    c = Client(env["app"], env["path"])
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, r.status_code
    return c


def _section(html, start, end=None):
    i = html.index(start)
    j = html.find(end, i + 1) if end else -1
    return html[i: j if j != -1 else len(html)]


def _dump(con) -> str:
    return "\n".join(con.iterdump())


def _session_text(client) -> str:
    """세션 쿠키의 내용. starlette 의 세션은 base64 로 감싼 JSON 에 서명을 붙인 값이다."""
    raw = client.cookies.get("session") or ""
    body = raw.split(".")[0]
    return base64.b64decode(body + "=" * (-len(body) % 4)).decode("utf-8", errors="replace") if body else ""


def _agent_login(env, agent_id):
    return env["con"].execute("SELECT login_id FROM user_account WHERE agent_id=?", (agent_id,)).fetchone()[0]


# ---------------------------------------------------------------- 접근
def test_three_roles_reach_only_their_education_screens(env):
    agent = _agent_login(env, env["inside"])
    for login_id, role in (("admin", "main"), (env["manager"], "manager"), (agent, "agent")):
        c = login(env, login_id)
        for p in EDU_PATHS:
            it = menu.find(p)
            assert it["status"] == "built", p
            want = 200 if role in it["roles"] else 403
            assert c.get(p).status_code == want, (role, p)
    # 과정과 차수 쓰기, 명부 올리기는 메인 관리자만
    for login_id in (env["manager"], agent):
        c = login(env, login_id)
        assert c.post("/edu/courses", data={"action": "save", "code": "X1", "name": "x", "kind": "product"}).status_code == 403
        assert c.post("/edu/sessions", data={"action": "save", "course_id": "1", "date_from": "2026-12-01", "online": "1"}).status_code == 403
        assert c.post("/edu/completions", data={"action": "preview"}).status_code == 403
    assert not env["con"].execute("SELECT 1 FROM edu_course WHERE code='X1'").fetchone()
    # 관리자와 설계사는 과정을 보기만 한다
    assert 'value="save"' not in login(env, env["manager"]).get("/edu/courses?new=1").text
    assert "과정 등록" in login(env, "admin").get("/edu/courses").text


def test_main_creates_closes_course_and_adds_session(env):
    con, c = env["con"], login(env, "admin")
    r = c.post("/edu/courses", data={"action": "save", "code": "PR-9", "name": "상품 시험 과정", "kind": "product", "target_role": "fc",
                                     "deadline_rule": "hire_days", "deadline_days": "30", "required": "1"})
    assert r.status_code == 303
    cid = con.execute("SELECT course_id FROM edu_course WHERE code='PR-9'").fetchone()[0]
    assert "상품 시험 과정" in c.get("/edu/courses").text
    bad = c.post("/edu/courses", data={"action": "save", "code": "PR-9", "name": "겹침", "kind": "product"})
    assert bad.status_code == 400 and "같은 과정 코드가 이미 있습니다" in bad.text
    assert c.post("/edu/sessions", data={"action": "save", "course_id": str(cid), "date_from": "2026-12-05", "online": "1",
                                         "status": "planned"}).status_code == 303
    assert "상품 시험 과정" in _section(c.get("/edu/sessions").text, "다가오는 차수", "지난 차수")
    assert c.post("/edu/courses", data={"action": "close", "course_id": str(cid)}).status_code == 303
    assert edu.course(con, cid)["status"] == "closed"
    assert "상품 시험 과정" not in c.get("/edu/courses").text and "상품 시험 과정" in c.get("/edu/courses?closed=1").text
    acts = [r[0] for r in con.execute("SELECT action FROM audit_log WHERE action LIKE 'edu_%' AND actor='admin' ORDER BY id")]
    assert acts == ["edu_course_create", "edu_session_create", "edu_course_closed"]


# ---------------------------------------------------------------- 수료 확인
def test_manager_confirms_in_scope_and_out_of_scope_is_refused(env):
    con, soon = env["con"], env["soon"]
    c = login(env, env["manager"])
    html = c.get("/team/education").text
    assert "교육 진척도" in html and "기준" in html
    rows = _section(html, 'id="not-done"')
    assert env["code_in"] in rows and env["code_out"] not in rows
    live = lambda a: con.execute("SELECT source FROM edu_completion WHERE agent_id=? AND course_id=? AND voided_at IS NULL", (a, soon)).fetchone()
    # 확인 단계 없이 보내면 확인 화면으로 돌린다
    r = c.post("/team/education", data={"action": "confirm", "agent_id": str(env["inside"]), "course_id": str(soon)})
    assert r.status_code == 303 and "confirm=" in r.headers["location"] and live(env["inside"]) is None
    ask = c.get(f"/team/education?confirm={env['inside']}-{soon}").text
    assert "이 수료를 확인 처리할까요?" in ask and 'name="confirm" value="1"' in ask
    # CSRF 없이 보내면 막힌다
    assert c.post("/team/education", data={"action": "confirm", "agent_id": str(env["inside"]), "course_id": str(soon), "confirm": "1"},
                  csrf=False).status_code == 403
    assert live(env["inside"]) is None
    r = c.post("/team/education", data={"action": "confirm", "agent_id": str(env["inside"]), "course_id": str(soon), "confirm": "1",
                                        "completed_on": date.today().isoformat()})
    assert r.status_code == 303 and live(env["inside"])["source"] == "manual"
    assert "확인 처리했습니다" in c.get("/team/education").text
    # 범위 밖과 본인은 거부되고 아무것도 들어가지 않는다
    for aid in (env["outside"], env["manager_agent"]):
        r = c.post("/team/education", data={"action": "confirm", "agent_id": str(aid), "course_id": str(soon), "confirm": "1"})
        assert r.status_code == 403, aid
        assert live(aid) is None
    assert "확인할 수 있는 미수료 줄이 아닙니다" in c.get(f"/team/education?confirm={env['outside']}-{soon}").text
    denied = [r[0] for r in con.execute("SELECT target FROM audit_log WHERE action='access_denied' AND actor=?", (env["manager"],))]
    assert "/team/education confirm" in denied and "/team/education confirm_self" in denied
    # 메인 관리자는 범위 밖이던 사람도 확인한다. 변경 기록 화면에는 한국어 이름표가 보인다
    a = login(env, "admin")
    no_date = a.post("/team/education", data={"action": "confirm", "agent_id": str(env["outside"]), "course_id": str(soon), "confirm": "1"})
    assert no_date.status_code == 400 and "수료일을 넣어야 합니다" in no_date.text
    assert a.post("/team/education", data={"action": "confirm", "agent_id": str(env["outside"]), "course_id": str(soon), "confirm": "1",
                                           "completed_on": date.today().isoformat()}).status_code == 303
    assert live(env["outside"]) is not None
    audit = a.get("/org/audit").text
    assert "교육 수료 확인" in audit and "edu_completion_confirm" not in audit


# ---------------------------------------------------------------- 명부
def _roster(env, name, **kw):
    con = env["con"]
    people = fr.agents_from_db(con)
    sessions = fr.sessions_from_db(con, "CP-2026")
    path, rows = fr.make_roster_file(env["tmp"] / "in" / name, people, {"code": "CP-2026", "name": "2026 준법 교육"}, sessions, **kw)
    return path, rows


def test_roster_preview_then_load_and_replace(env):
    con, c = env["con"], login(env, "admin")
    path, rows = _roster(env, "roster_a.xlsx", dirty=True)
    n_before = con.execute("SELECT COUNT(*) FROM edu_completion").fetchone()[0]
    with path.open("rb") as f:
        r = c.post("/edu/completions", data={"action": "preview"}, files={"file": ("명부_11월.xlsx", f, "application/octet-stream")})
    assert r.status_code == 200, r.text[-400:]
    pv = _section(r.text, "미리보기 ·", '<h2 class="sec">수료자 명부 올리기')
    assert "새로 넣을 수료" in pv and "아직 아무것도 넣지 않았습니다" in pv and 'value="load"' in pv
    assert fr.UNKNOWN_AGENT_CODE in pv                                        # 문제는 사번으로 보인다
    names = {x["성명"] for x in rows if x["성명"]}
    assert not [n for n in names if n in pv]                                   # 이름은 미리보기에 없다
    assert fr.MISMATCH_NAME not in r.text and fr.UNKNOWN_NAME not in r.text
    assert fr.FAKE_PHONE_PREFIX not in r.text
    assert con.execute("SELECT COUNT(*) FROM edu_completion").fetchone()[0] == n_before
    assert not con.execute("SELECT 1 FROM import_batch WHERE data_type='edu_roster'").fetchone()
    # 넣기
    r = c.post("/edu/completions", data={"action": "load"})
    assert r.status_code == 200 and "넣었습니다" in r.text
    b1 = con.execute("SELECT batch_id, file_name, status FROM import_batch WHERE data_type='edu_roster'").fetchone()
    assert b1["file_name"] == "명부_11월.xlsx" and b1["status"] == "active"
    n_roster = con.execute("SELECT COUNT(*) FROM edu_completion WHERE batch_id=? AND voided_at IS NULL", (b1["batch_id"],)).fetchone()[0]
    assert n_roster > 0
    assert fr.MISMATCH_NAME not in _dump(con) and fr.FAKE_PHONE_PREFIX not in _dump(con)
    # 미리보기 없이 다시 넣으려 하면 막는다
    assert c.post("/edu/completions", data={"action": "load"}).status_code == 400
    # 고친 명부로 앞 배치를 바꾼다
    path2, _ = _roster(env, "roster_b.csv", n=4)
    page = c.get("/edu/completions").text
    assert f'<option value="{b1["batch_id"]}">' in page
    with path2.open("rb") as f:
        r = c.post("/edu/completions", data={"action": "preview", "replaces": str(b1["batch_id"])},
                   files={"file": ("명부_고침.csv", f, "text/csv")})
    assert r.status_code == 200 and f"배치 {b1['batch_id']} 의 수료를 빼고" in r.text
    r = c.post("/edu/completions", data={"action": "load"})
    assert r.status_code == 200
    assert con.execute("SELECT status FROM import_batch WHERE batch_id=?", (b1["batch_id"],)).fetchone()[0] == "superseded"
    assert con.execute("SELECT COUNT(*) FROM edu_completion WHERE batch_id=? AND voided_at IS NULL", (b1["batch_id"],)).fetchone()[0] == 0
    # 같은 파일은 미리보기에서 막는다
    with path2.open("rb") as f:
        r = c.post("/edu/completions", data={"action": "preview"}, files={"file": ("명부_고침.csv", f, "text/csv")})
    assert r.status_code == 400 and "이미 넣은 파일입니다" in r.text
    # 데이터 묶음의 수료자 명부는 이 화면으로 안내한다
    d = c.get("/data/courses").text
    assert 'href="/edu/completions"' in d and "명부_고침.csv" in d


# ---------------------------------------------------------------- 명함 수료 코드
def test_card_code_shown_once_and_not_kept(env):
    con, site = env["con"], env["state"]["site"]
    c = login(env, "admin")
    cand = edu.card_code_candidates(con, [r[0] for r in con.execute("SELECT agent_id FROM agent")])[0]
    page = c.get("/edu/completions").text
    assert f'name="cid" value="{cand["completion_id"]}"' in page
    # 확인 단계. 사이트에 아무것도 보내지 않는다
    r = c.post("/edu/completions", data={"action": "card_pick", "cid": str(cand["completion_id"])})
    assert r.status_code == 200 and "수료 코드를 발급할까요?" in r.text and not site.posts()
    r = c.post("/edu/completions", data={"action": "card_issue", "cid": str(cand["completion_id"])})
    assert "수료 코드를 발급할까요?" in r.text and not site.posts()             # confirm 없이는 다시 확인 단계
    r = c.post("/edu/completions", data={"action": "card_issue", "cid": str(cand["completion_id"]), "confirm": "1"})
    assert r.status_code == 200
    assert [p.url.path for p in site.posts()] == ["/api/admin/codes"]
    assert json.loads(site.posts()[0].content)["people"] == [{"name": cand["name"]}]
    code = site.codes()[0]
    assert code in r.text and "다시 볼 수 없습니다" in r.text and "설계사에게 전할 안내" in r.text
    assert r.headers["cache-control"] == "no-store"
    # 원문은 DB, 세션, 다음 화면에 없다. 앱에는 일련번호와 해시만 남는다
    row = con.execute("SELECT status, serial, code_sha256 FROM edu_card_code WHERE completion_id=?", (cand["completion_id"],)).fetchone()
    assert row["status"] == "issued" and row["serial"] == site.log[0]["serial"] and row["code_sha256"] == edu.card_code_hash(code)
    dump = _dump(con)
    assert code not in dump and code.replace("-", "") not in dump
    assert code not in _session_text(c)
    nxt = c.get("/edu/completions").text
    assert code not in nxt and site.log[0]["serial"] in nxt
    assert code not in c.get("/org/audit").text
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='card_code_issue'").fetchone()[0] == 1
    # 같은 수료를 다시 보내도 사이트에 두 번 가지 않는다
    c.post("/edu/completions", data={"action": "card_issue", "cid": str(cand["completion_id"]), "confirm": "1"})
    assert len(site.posts()) == 1
    # 대조는 사이트 기록과 앱 기록이 맞는다고 보인다
    r = c.post("/edu/completions", data={"action": "card_reconcile"})
    assert r.status_code == 200 and "사이트와 앱의 발급 기록이 맞습니다" in r.text and code not in r.text


def test_manager_and_agent_issue_post_is_403(env):
    con, site = env["con"], env["state"]["site"]
    cand = edu.card_code_candidates(con, [r[0] for r in con.execute("SELECT agent_id FROM agent")])[0]
    for login_id in (env["manager"], _agent_login(env, env["inside"])):
        c = login(env, login_id)
        for action in ("card_issue", "card_pick", "card_reconcile", "card_release"):
            r = c.post("/edu/completions", data={"action": action, "cid": str(cand["completion_id"]), "confirm": "1", "issue_id": "1"})
            assert r.status_code == 403, (login_id, action)
    assert 'name="cid"' not in login(env, env["manager"]).get("/edu/completions").text
    assert not site.posts()
    assert not con.execute("SELECT 1 FROM edu_card_code").fetchone()


def test_write_off_shows_notice_only_and_release_unknown_request(env):
    con, site, state = env["con"], env["state"]["site"], env["state"]
    state["write"] = False
    c = login(env, "admin")
    page = c.get("/edu/completions").text
    assert "쓰기가 아직 꺼져 있어" in page and 'name="cid"' not in page
    cand = edu.card_code_candidates(con, [r[0] for r in con.execute("SELECT agent_id FROM agent")])[0]
    r = c.post("/edu/completions", data={"action": "card_issue", "cid": str(cand["completion_id"]), "confirm": "1"})
    assert r.status_code == 200 and "쓰기가 꺼져 있어 보내지 않았습니다" in r.text
    assert not site.posts() and not con.execute("SELECT 1 FROM edu_card_code").fetchone()
    # 결과를 모르는 요청은 확인 단계를 거쳐 푼다
    iid = edu.request_card_code(con, cand["completion_id"], by="admin")
    page = c.get("/edu/completions").text
    assert "결과를 모르는 요청 1건" in page
    r = c.post("/edu/completions", data={"action": "card_release", "issue_id": str(iid)})
    assert r.status_code == 303 and r.headers["location"].startswith(f"/edu/completions?release={iid}")
    assert "이 발급 요청을 실패로 돌릴까요?" in c.get(f"/edu/completions?release={iid}").text
    assert c.post("/edu/completions", data={"action": "card_release", "issue_id": str(iid), "confirm": "1"}).status_code == 303
    assert con.execute("SELECT status FROM edu_card_code WHERE issue_id=?", (iid,)).fetchone()[0] == "failed"
    assert "다시 발급할 수 있습니다" in c.get("/edu/completions").text
    # 키가 없으면 안내만 보인다
    state["key"] = ""
    assert "명함 사이트 연동 키가 없어" in c.get("/edu/completions").text


# ---------------------------------------------------------------- 내 명함
def test_my_card_states(env):
    con, site, state = env["con"], env["state"]["site"], env["state"]
    agent = _agent_login(env, env["inside"])
    c = login(env, agent)
    html = c.get("/sales/card").text
    assert "메인 관리자에게 연결을 요청하세요" in html and f'href="{BASE}/make/"' in html
    CardLinkTable(con).link(env["inside"], "my-card", by="admin")
    for st, live, want in (("approved", True, "공개"), ("pending", False, "대기"), ("rejected", False, "반려됨"), ("none", False, "없음")):
        site.status["my-card"] = (st, live)
        r = c.get("/sales/card")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        big = re.search(r'<div class="big">([^<]+)</div>', r.text).group(1)
        assert big == want, (st, big)
        assert "명함 사이트 기준" in r.text and REJECT_REASON not in r.text
        assert (f"{BASE}/my-card/" in r.text) == live
    row = CardLinkTable(con).get(env["inside"])
    assert row["last_status"] == "none" and row["last_checked_at"]
    assert not site.posts()                                                     # 읽기만 한다
    # 사이트 주소가 없으면 안내와 마지막 확인 상태를 보인다
    state["base"] = ""
    html = c.get("/sales/card").text
    assert "연동 설정이 아직 없어" in html and "마지막으로 확인한 상태" in html
    # 관리자도 본인 명함을 본다. 메인 관리자에게는 열리지 않는다
    assert login(env, env["manager"]).get("/sales/card").status_code == 200
    assert login(env, "admin").get("/sales/card").status_code == 403


# ---------------------------------------------------------------- 홈
def test_home_shows_education_due_soon_count(env):
    con = env["con"]
    agent = _agent_login(env, env["inside"])
    n_agent = len(edu.due_soon(con, [env["inside"]]))
    n_mgr = len(edu.due_soon(con, sorted(env["manager_ids"])))
    assert n_agent >= 1 and n_mgr > n_agent
    html = login(env, agent).get("/").text
    assert f"교육 기한 임박 {n_agent}건" in html and 'href="/my/education"' in html
    html = login(env, env["manager"]).get("/").text
    assert f"교육 기한 임박 {n_mgr}건" in html and 'href="/team/education#not-done"' in html
    assert "교육 기한 임박" not in login(env, "admin").get("/").text
    # 내 교육 화면의 임박 수와 홈의 수가 같다
    me = login(env, agent).get("/my/education").text
    assert "임박 시험 과정" in me and "내 교육" in me


def test_education_audit_actions_have_labels():
    src = (db.ROOT / "app" / "engine" / "education.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'_audit\(con, by, "(\w+)"', src))
    codes |= set(re.findall(r'"(edu_\w+)" if before else "(edu_\w+)"', src)[0]) if '" if before else "' in src else set()
    codes |= {f"edu_course_{s}" for s in ("active", "closed")} | {card_codes.AUDIT_ISSUE}
    assert {"edu_completion_confirm", "edu_card_code_request", "edu_batch_void"} <= codes
    missing = sorted(c for c in codes if c not in audit_view.ACTION_KO)
    assert not missing, missing
