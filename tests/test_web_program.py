"""S4 파트 I 제도의 웹 화면: 커리어패스 운영, 인증과 MDRT, 임원제도, 영업우수, 내 커리어패스, 내 인증.

- 세 역할의 접근은 config/menu.yaml 을 따른다. 제도 묶음은 메인 관리자와 관리자, 내 진척의 두 화면은 관리자와 설계사 본인이다.
- 관리자는 자기 조직과 그 아래 사람만 본다. 범위 밖 사람을 고르면 403 이고 변경 기록에 남는다.
- 회차 열기, 승인, 반려, 무르기, 선발은 메인 관리자만 한다. 확인 칸과 사유와 CSRF 토큰이 있어야 한다.
- 연말 값과 연말 달성에는 예상이 붙고, 인증의 연 보험료 지표에는 D-33 기준 설명이 붙는다.
- 판정 화면마다 '이 판정에 쓴 기준: 기관 연도 판'이 보인다.
- 내 커리어패스의 진행 중 기간 실적은 실적 요약과 같은 숫자다.
- D-17. 관리자는 팀원의 수수료 지표 값을 보지 않는다. 엔진이 가리고 화면은 가림 표지만 붙인다.

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 는 건드리지 않는다.
사람 이름과 금액은 실패 메시지에 넣지 않는다. 화면 주소와 칸 이름, 건수만 넣는다.
"""
import os
import pathlib
import re
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import career, standards
from app.engine import certification as ce
from app.web import auth, menu

PW = "program-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
PROGRAM = ["/program/career", "/program/cert", "/program/executive", "/program/excellence"]
MINE = ["/my/career", "/my/cert"]
MAIN = {"role": "main"}


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리킨다. 폼을 보낼 때 화면의 CSRF 토큰을 함께 보낸다. raw=True 면 토큰 없이 보낸다."""

    def __init__(self, app, db_path):
        super().__init__(app, follow_redirects=False)
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
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="webprogram-"))
    old = {k: os.environ.get(k) for k in ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR")}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"))
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    ym = auth.current_ym(con)
    mgr = con.execute("""SELECT a.agent_id, a.internal_code code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                         JOIN org_unit o ON o.org_id=m.org_id WHERE o.level='branch' AND m.effective_to IS NULL ORDER BY o.org_id LIMIT 1""").fetchone()
    mctx = auth.context(con, con.execute("SELECT user_id FROM user_account WHERE agent_id=?", (mgr["agent_id"],)).fetchone()[0])
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    users = {r[0]: r[1] for r in con.execute(
        "SELECT u.agent_id, u.login_id FROM user_account u JOIN agent a ON a.agent_id=u.agent_id WHERE u.status='active' AND a.status='active'")}
    members = [a for a in sorted(mctx["agent_ids"] - {mgr["agent_id"]}) if a not in heads and a in users]
    everyone = [r[0] for r in con.execute("SELECT agent_id FROM agent")]
    outside = [a for a in everyone if a not in mctx["agent_ids"] and a in users and a not in heads]
    assert members and outside, "관리자 범위 안팎에 설계사가 있어야 한다"
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "con": con, "app": app, "ym": ym, "mgr": dict(mgr), "mctx": mctx, "members": members,
           "outside": outside, "users": users, "everyone": everyone, "agent": members[0], "agent_login": users[members[0]]}
    con.close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


def _copy(env, name):
    dst = env["tmp"] / f"{name}.db"
    shutil.copy(env["db"], dst)
    return dst


def login(env, login_id, path=None):
    c = Client(env["app"], path or env["db"])
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, (login_id, r.status_code)
    return c


def _agents_on(html) -> set:
    return {int(x) for x in re.findall(r'data-agent="(\d+)"', html)}


def _money(v) -> str:
    return f"{int(v):,}"


# ---------------------------------------------------------------- 세 역할의 접근과 범위
def test_menu_built_and_three_roles_follow_menu(env):
    items = {it["path"]: it for g in menu.load()["groups"] for it in g["items"]}
    for path in PROGRAM + MINE:
        assert items[path]["status"] == "built", path
    for login_id, role in (("admin", "main"), (env["mgr"]["code"], "manager"), (env["agent_login"], "agent")):
        c = login(env, login_id)
        for path in PROGRAM + MINE:
            want = 200 if role in items[path]["roles"] else 403
            assert c.get(path).status_code == want, (role, path)
        for view in ("rounds", "results", "standards"):
            want = 200 if role in items["/program/career"]["roles"] else 403
            assert c.get(f"/program/career?view={view}").status_code == want, (role, view)


def test_manager_sees_only_own_org(env):
    c = login(env, env["mgr"]["code"])
    allowed = env["mctx"]["agent_ids"]
    for path in ("/program/career", "/program/cert", "/program/cert?prog=EXCELLENT_FC_LIFE", "/program/executive?prog=EXEC_FC_DIRECTOR",
                 "/program/excellence"):
        html = c.get(path).text
        assert _agents_on(html) <= allowed, path
    main_html = login(env, "admin").get("/program/cert").text
    assert _agents_on(main_html) - allowed, "메인 관리자는 범위 밖 사람도 본다"
    out = env["outside"][0]
    r = c.get(f"/program/cert?prog=MDRT&who={out}")
    assert r.status_code == 403
    row = env["con"].execute("SELECT target FROM audit_log WHERE action='access_denied' AND actor=? ORDER BY id DESC LIMIT 1",
                             (env["mgr"]["code"],)).fetchone()
    assert row and row[0].startswith("/program/cert")
    assert c.get(f"/program/cert?prog=MDRT&who={env['members'][0]}").status_code == 200


def test_my_screens_show_only_self(env):
    c = login(env, env["agent_login"])
    for path in MINE:
        html = c.get(path).text
        assert "data-agent=" not in html or _agents_on(html) <= {env["agent"]}, path
    html = c.get("/my/cert").text
    others = {r["program_code"] for r in ce.cert_progress(env["con"], [env["agent"]], viewer=MAIN)["rows"]}
    assert set(re.findall(r'data-prog="([A-Z_]+)"', html)) == others


# ---------------------------------------------------------------- 쓰기는 메인 관리자만, 확인 칸과 사유와 CSRF
def test_career_round_open_needs_main_confirm_and_csrf(env):
    path = _copy(env, "open")
    con = db.connect(path)
    try:
        mc = login(env, env["mgr"]["code"], path)
        assert mc.post("/program/career", data={"action": "open", "agree": "yes"}).status_code == 403
        assert "name=\"action\" value=\"open\"" not in mc.get("/program/career?view=rounds&ask=open").text
        admin = login(env, "admin", path)
        assert admin.post("/program/career", data={"action": "open", "agree": "yes"}, raw=True).status_code == 403
        r = admin.post("/program/career", data={"action": "open"})
        assert r.status_code == 400 and "확인 칸" in r.text
        assert not con.execute("SELECT COUNT(*) FROM career_round").fetchone()[0]
        page = admin.get("/program/career?view=rounds&ask=open").text
        assert 'name="action" value="open"' in page and 'name="agree"' in page
        r = admin.post("/program/career", data={"action": "open", "agree": "yes"})
        assert r.status_code == 303 and "done=open" in r.headers["location"]
        assert con.execute("SELECT COUNT(*) FROM career_round WHERE status='open'").fetchone()[0] == 1
        denied = con.execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied' AND target LIKE '/program/career career_open'").fetchone()[0]
        assert denied == 1
        assert con.execute("SELECT actor FROM audit_log WHERE action='career_round_open'").fetchone()[0] == "admin"
    finally:
        con.close()


def test_career_approve_reject_revoke_flow(env):
    path = _copy(env, "decide")
    con = db.connect(path)
    try:
        admin = login(env, "admin", path)
        assert admin.post("/program/career", data={"action": "open", "agree": "yes"}).status_code == 303
        rid = con.execute("SELECT round_id FROM career_round WHERE status='open'").fetchone()[0]
        detail = career.round_detail(con, rid, env["everyone"], viewer=MAIN)
        ok = next(m for m in detail["members"] if m["eligible"])
        weak = next(m for m in detail["members"] if not m["eligible"])
        html = admin.get(f"/program/career?view=rounds&round={rid}").text
        assert f"ask=approve&agent={ok['agent_id']}" in html and f"ask=approve&agent={weak['agent_id']}" not in html
        base = {"action": "approve", "round": rid, "agent": ok["agent_id"]}
        # 관리자는 승인하지 못한다. 확인 단계도 보이지 않는다
        mc = login(env, env["mgr"]["code"], path)
        assert mc.post("/program/career", data={**base, "agree": "yes", "note": "기준 충족"}).status_code == 403
        assert 'value="approve"' not in mc.get(f"/program/career?view=rounds&round={rid}&ask=approve&agent={ok['agent_id']}").text
        # 사유와 확인 칸이 있어야 한다
        assert admin.post("/program/career", data={**base, "agree": "yes"}).status_code == 400
        assert admin.post("/program/career", data={**base, "note": "기준 충족"}).status_code == 400
        assert admin.post("/program/career", data={**base, "agree": "yes", "note": "기준 충족", "effective_ym": "2026-11"}).status_code == 400
        assert admin.post("/program/career", data={**base, "agree": "yes", "note": "기준 충족"}, raw=True).status_code == 403
        assert not con.execute("SELECT COUNT(*) FROM career_decision").fetchone()[0]
        confirm = admin.get(f"/program/career?view=rounds&round={rid}&ask=approve&agent={ok['agent_id']}").text
        assert 'name="note"' in confirm and 'name="agree"' in confirm and "이 판정에 쓴 기준" in confirm
        r = admin.post("/program/career", data={**base, "agree": "yes", "note": "분기 기준을 모두 채웠습니다"})
        assert r.status_code == 303 and "done=approve" in r.headers["location"]
        d = con.execute("SELECT decision_id, decision, note FROM career_decision WHERE agent_id=?", (ok["agent_id"],)).fetchone()
        assert d["decision"] == "approved" and d["note"]
        # 기준 밖 사람은 확인 단계를 지나도 엔진이 막는다
        r = admin.post("/program/career", data={"action": "approve", "round": rid, "agent": weak["agent_id"], "agree": "yes", "note": "시험"})
        assert r.status_code == 400
        # 반려
        r = admin.post("/program/career", data={"action": "reject", "round": rid, "agent": weak["agent_id"], "agree": "yes", "note": "다음 회차에 봅니다"})
        assert r.status_code == 303
        # 무르기: 관리자 403, 짧은 사유 400, 메인 관리자와 사유면 무른다
        rv = {"action": "revoke", "decision": d["decision_id"], "from": "results"}
        assert mc.post("/program/career", data={**rv, "agree": "yes", "note": "기준 자료를 다시 봅니다"}).status_code == 403
        r = admin.post("/program/career", data={**rv, "agree": "yes", "note": "짧음"})
        assert r.status_code == 400 and f"{standards.REVOKE_MIN}자" in r.text
        assert admin.post("/program/career", data={**rv, "note": "기준 자료를 다시 봅니다"}).status_code == 400
        r = admin.post("/program/career", data={**rv, "agree": "yes", "note": "기준 자료를 다시 봅니다"})
        assert r.status_code == 303 and "view=results" in r.headers["location"]
        assert con.execute("SELECT voided_at FROM career_decision WHERE decision_id=?", (d["decision_id"],)).fetchone()[0]
        assert career.current_level(con, ok["agent_id"], "202611")["level_id"] == ok["from_level"]["level_id"]
        res = admin.get("/program/career?view=results").text
        assert "무름" in res and "기준 자료를 다시 봅니다" in res
        actions = [r[0] for r in con.execute("SELECT action FROM audit_log WHERE actor='admin' AND action LIKE 'career_%' ORDER BY id")]
        assert actions == ["career_round_open", "career_approve", "career_reject", "career_revoke"]
    finally:
        con.close()


def test_excellence_select_and_revoke_main_only(env):
    path = _copy(env, "excellence")
    con = db.connect(path)
    try:
        x = ce.excellence(con, env["everyone"], viewer=MAIN)
        cand = next(r for r in x["candidates"] if r["program_code"] == "SALES_EXCELLENT")
        data = {"action": "select", "agent": cand["agent_id"], "prog": "SALES_EXCELLENT", "year": x["year"]}
        admin = login(env, "admin", path)
        mc = login(env, env["mgr"]["code"], path)
        assert mc.post("/program/excellence", data={**data, "agree": "yes", "note": "연간 선발"}).status_code == 403
        assert "?ask=select" not in mc.get("/program/excellence").text
        assert admin.post("/program/excellence", data={**data, "note": "연간 선발"}).status_code == 400
        assert admin.post("/program/excellence", data={**data, "agree": "yes"}).status_code == 400
        assert admin.post("/program/excellence", data={**data, "agree": "yes", "note": "연간 선발"}, raw=True).status_code == 403
        assert not con.execute("SELECT COUNT(*) FROM excellence_selection").fetchone()[0]
        assert f"ask=select&agent={cand['agent_id']}" in admin.get("/program/excellence").text
        r = admin.post("/program/excellence", data={**data, "agree": "yes", "note": "연간 선발"})
        assert r.status_code == 303
        sid = con.execute("SELECT selection_id FROM excellence_selection WHERE agent_id=?", (cand["agent_id"],)).fetchone()[0]
        html = admin.get("/program/excellence").text
        assert f'data-sel="{sid}"' in html and "앱 선발" in html
        rv = {"action": "revoke", "sel": sid}
        assert mc.post("/program/excellence", data={**rv, "agree": "yes", "note": "선발 인원을 다시 봅니다"}).status_code == 403
        assert admin.post("/program/excellence", data={**rv, "agree": "yes", "note": "짧음"}).status_code == 400
        r = admin.post("/program/excellence", data={**rv, "agree": "yes", "note": "선발 인원을 다시 봅니다"})
        assert r.status_code == 303
        assert con.execute("SELECT voided_at FROM excellence_selection WHERE selection_id=?", (sid,)).fetchone()[0]
        assert "무른 선발" in admin.get("/program/excellence").text
    finally:
        con.close()


# ---------------------------------------------------------------- 예상과 기준 설명, 기준 버전
def test_estimates_and_d33_basis_are_shown(env):
    admin, con, ids = login(env, "admin"), env["con"], env["everyone"]
    for path, res in (("/program/cert", ce.cert_progress(con, ids, viewer=MAIN)), ("/program/executive", ce.executive_status(con, ids, viewer=MAIN)),
                      ("/program/excellence", ce.excellence(con, ids, viewer=MAIN))):
        html = admin.get(path).text
        assert 'data-estimate="1"' in html and "예상" in html, path
        # 기준 설명은 엔진이 D-33 지표가 있다고 준 화면에만 붙는다
        assert (standards.BASIS_NOTE in html and 'data-basis="D-33"' in html) == bool(res["basis"]), path
    assert ce.cert_progress(con, ids, viewer=MAIN)["basis"], "인증에는 D-33 지표가 있어야 한다"
    me = login(env, env["agent_login"]).get("/my/cert").text
    assert ce.PROJECTED_NOTE in me and standards.BASIS_NOTE in me
    assert re.search(r"연말 [^<]*<span class=\"chip blue\" data-estimate=\"1\">예상</span>", me), "연말 값 옆에 예상이 붙어야 한다"


def test_version_labels_on_every_judgement_screen(env):
    path = _copy(env, "versions")
    admin = login(env, "admin", path)
    for p in ("/program/cert", "/program/executive", "/program/excellence"):
        html = admin.get(p).text
        assert re.search(r"이 판정에 쓴 기준: [^<]+ 2026년 기준 \d+판", html), p
    assert "이 판정에 쓴 기준: MDRT 2026년 기준 1판" in admin.get("/program/cert?prog=MDRT").text
    assert admin.post("/program/career", data={"action": "open", "agree": "yes"}).status_code == 303
    rounds = admin.get("/program/career?view=rounds").text
    assert f"이 판정에 쓴 기준: {standards.CAREER_ISSUER} 2026년 기준 1판" in rounds
    std = admin.get("/program/career?view=standards").text
    assert "2026년 기준 1판" in std and "남은 판" in std
    climber = next(r["agent_id"] for r in career.career_status(env["con"], list(env["users"]), env["ym"], viewer=MAIN)
                   if r["next_level"] and r["agent_id"] != env["mgr"]["agent_id"])
    me = login(env, env["users"][climber], path).get("/my/career").text
    assert f"이 판정에 쓴 기준: {standards.CAREER_ISSUER} 2026년 기준 1판" in me
    assert re.search(r"이 판정에 쓴 기준: [^<]+ 2026년 기준 \d+판", login(env, env["agent_login"], path).get("/my/cert").text)


# ---------------------------------------------------------------- 같은 사람의 같은 숫자
def test_my_career_matches_my_summary(env):
    con = env["con"]
    rows = career.career_status(con, [a for a in env["users"] if a not in {env["mgr"]["agent_id"]}], env["ym"], viewer=MAIN)
    target = next(r for r in rows for c in r["criteria"]
                  if c["metric"] == "converted_premium" and c["backsolve"] and c["backsolve"]["period_type"] == "quarter"
                  and c["backsolve"]["start_ym"] <= env["ym"] and c["backsolve"]["so_far"])
    c = login(env, env["users"][target["agent_id"]])
    mine = c.get("/my/career").text
    so_far = re.search(r'data-col="so-far">([\d,]+)원', mine).group(1)
    summary = c.get(f"/my/summary?pt=quarter&ym={env['ym']}").text
    same = re.search(r'data-col="premium">([\d,]+)<', summary).group(1)
    assert so_far == same, "내 커리어패스의 분기 실적이 실적 요약과 다릅니다"
    b = next(x for x in target["criteria"] if x["metric"] == "converted_premium")["backsolve"]
    assert f"남은 {b['months_left']}달 동안 월 {_money(b['monthly_needed'])}원" in mine


# ---------------------------------------------------------------- D-17
def test_manager_never_sees_member_commission(env):
    con = env["con"]
    full = ce.cert_progress(con, env["members"], viewer=MAIN)["rows"]
    row = next(r for r in full if r["program_code"] == "MDRT" and any(m["metric"] == "annual_commission" and m["actual"] for m in r["metrics"]))
    m = next(m for m in row["metrics"] if m["metric"] == "annual_commission")
    secret = _money(m["actual"])
    url = f"/program/cert?prog=MDRT&who={row['agent_id']}"
    mgr_html = login(env, env["mgr"]["code"]).get(url).text
    assert 'data-hidden="1"' in mgr_html and secret not in mgr_html
    assert secret in login(env, "admin").get(url).text
    if row["agent_id"] in env["users"]:
        assert secret in login(env, env["users"][row["agent_id"]]).get("/my/cert").text
