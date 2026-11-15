"""S3 파트 G 수수료·점검의 웹 화면: 수수료 내역, 환수, 대사, 조정, 마감, 정산서와 유지율 올리기.

- 세 역할의 접근은 config/menu.yaml 을 따른다. 대사, 조정, 마감, 정산서 올리기는 메인 관리자만이다.
- 수수료와 환수는 메인 관리자가 전체를, 관리자와 설계사는 본인만 본다. 관리자는 팀원의 금액을 어디서도 보지 않는다 (D-17).
- 차이 처리는 메모가 있어야 남는다. 마감은 확인 칸을 거치고 막는 조건이 없어야 하며, 해제는 사유가 있어야 한다.
- 상태를 바꾸는 요청은 CSRF 토큰이 있어야 한다.
- 정산서 미리보기는 DB 에 아무것도 쓰지 않는다. 넣기는 배치를 하나 만들고 변경 기록을 남긴다.

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 는 건드리지 않는다.
사람 이름과 금액은 실패 메시지에 넣지 않는다. 화면 주소와 칸 이름, 건수만 넣는다.
"""
import json
import os
import pathlib
import re
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import settle as st
from app.web import auth, menu

PW = "settle-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
NUM = re.compile(r"(?<![0-9A-Za-z_.,])(\d{1,3}(?:,\d{3})+|\d+)(?![0-9A-Za-z_,]|\.\d)")
MIN_AMOUNT = 1000
SETTLE = ["/settle/commission", "/settle/clawback", "/settle/recon", "/settle/adjust", "/settle/close", "/data/statement"]


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
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="websettle-"))
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
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    mctx = auth.context(con, con.execute("SELECT user_id FROM user_account WHERE agent_id=?", (mgr["agent_id"],)).fetchone()[0])
    # 관리자 범위 안 팀원 가운데 이번 달 수수료 줄이 있는 사람. 첫 사람은 설계사로 들어가는 시험에 쓴다
    users = {r[0] for r in con.execute("SELECT u.agent_id FROM user_account u JOIN agent a ON a.agent_id=u.agent_id WHERE u.status='active' AND a.status='active'")}
    members = [a for a in sorted(mctx["agent_ids"] - {mgr["agent_id"]}) if a not in heads and a in users and st.agent_month(con, [a], ym)["lines"]]
    assert members, "관리자 범위에 수수료 줄이 있는 팀원이 있어야 한다"
    agent_code = con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (members[0],)).fetchone()[0]
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "con": con, "app": app, "ym": ym, "mgr": dict(mgr), "mctx": mctx, "members": members,
           "agent": {"agent_id": members[0], "code": agent_code}}
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


def _tokens(html) -> set:
    return {int(t.replace(",", "")) for t in NUM.findall(html)}


def _amounts(con, agent_ids, ym) -> set:
    """이 사람들의 그 달 수수료 금액이 화면에 찍힐 수 있는 정수. 줄마다, 사람별 지급, 환수, 순액."""
    out = set()
    for a in agent_ids:
        m = st.agent_month(con, [a], ym)
        vals = [r["amount"] for r in m["lines"]] + [m["total"][k] for k in ("paid", "clawback", "net")]
        vals += [t["amount"] for t in m["by_type"]] + [x[k] for x in m["by_insurer"] for k in ("paid", "clawback", "net")]
        out |= {abs(int(v)) for v in vals if v is not None and abs(v) >= MIN_AMOUNT}
    return out


# ---------------------------------------------------------------- 세 역할의 접근
def test_three_roles_follow_menu(env):
    items = {it["path"]: it for g in menu.load()["groups"] for it in g["items"]}
    for path in SETTLE:
        assert items[path]["status"] == "built", path
    for login_id, role in (("admin", "main"), (env["mgr"]["code"], "manager"), (env["agent"]["code"], "agent")):
        c = login(env, login_id)
        for path in SETTLE:
            r = c.get(path)
            want = 200 if role in items[path]["roles"] else 403
            assert r.status_code == want, (role, path, r.status_code)
            if want == 200:
                assert r.headers["Cache-Control"] == "no-store", (role, path)
    # 관리자와 설계사의 수수료와 환수는 본인 범위다
    assert items["/settle/commission"]["roles"] == {"main": "all", "manager": "self", "agent": "self"}
    assert items["/settle/clawback"]["roles"] == {"main": "all", "manager": "self", "agent": "self"}


def test_amounts_carry_basis_and_time(env):
    """금액에는 정산서 기준이나 예상을, 화면에는 기준 시각을 붙인다. 샘플은 마지막 달만 정산서가 있다."""
    con, ym = env["con"], env["ym"]
    c = login(env, "admin")
    html = c.get(f"/settle/commission?ym={ym}").text
    assert "정산서 기준" in html and "적재 기준입니다" in html
    prev = [m for m in (r[0] for r in con.execute("SELECT DISTINCT period_ym FROM calc_run WHERE run_type='commission' AND status='done' ORDER BY 1 DESC")) if m != ym][0]
    html = c.get(f"/settle/commission?ym={prev}").text
    assert 'data-basis="estimate"' in html and "계산 기준이고 예상 금액입니다" in html
    assert 'data-basis="statement"' not in html, "정산서가 없는 달에 정산서 기준이 붙었다"
    for path in ("/settle/clawback", "/settle/recon", "/settle/adjust", "/settle/close"):
        assert "기준입니다" in c.get(path).text or "들어오지 않았습니다" in c.get(path).text, path


# ---------------------------------------------------------------- 본인만 보기와 D-17
def test_self_scope_shows_only_own(env):
    con, ym = env["con"], env["ym"]
    me = env["agent"]
    c = login(env, me["code"])
    html = c.get("/settle/commission").text
    mine = st.agent_month(con, [me["agent_id"]], ym)
    assert f"{int(mine['total']['net']):,}" in html, "본인 순액이 보이지 않는다"
    assert "data-agent=" not in html, "본인 화면에 설계사 목록이 있다"
    # 다른 사람을 고르면 403 이고 범위 밖 접근 시도로 남는다
    other = next(a for a in env["members"] if a != me["agent_id"])
    before = con.execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied'").fetchone()[0]
    for path in ("/settle/commission", "/settle/clawback"):
        assert c.get(f"{path}?agent={other}").status_code == 403, path
        assert c.get(f"{path}?agent={me['agent_id']}").status_code == 200, path
    after = con.execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied'").fetchone()[0]
    assert after - before == 2


def test_d17_manager_never_sees_member_commission(env):
    """D-17. 관리자는 팀원을 골라도 403 이고, 자기 화면의 숫자에 팀원의 수수료 금액이 없다."""
    con, ym = env["con"], env["ym"]
    mgr, members = env["mgr"], env["members"]
    c = login(env, mgr["code"])
    for a in members[:5]:
        for path in ("/settle/commission", "/settle/clawback"):
            assert c.get(f"{path}?agent={a}").status_code == 403, path
    own = _amounts(con, [mgr["agent_id"]], ym)
    secret = _amounts(con, members, ym) - own
    assert len(secret) >= 5, "볼 금액이 거의 없으면 이 시험은 아무것도 지키지 못한다"
    years = set(range(1900, 2101)) | {y * 100 + m for y in range(2000, 2101) for m in range(1, 13)}
    leaks = []
    for path in ("/settle/commission", "/settle/clawback"):
        hit = (_tokens(c.get(path).text) & secret) - years
        if hit:
            leaks.append(path)
    assert not leaks, leaks
    # 대조군: 메인 관리자는 같은 팀원의 금액을 본다. 그래야 위 시험의 통과가 뜻이 있다
    admin = login(env, "admin")
    seen = set()
    for a in members[:5]:
        seen |= _tokens(admin.get(f"/settle/commission?agent={a}").text) & secret
    assert seen, "메인 관리자 화면에서 팀원 금액을 찾지 못했다"


# ---------------------------------------------------------------- 차이 처리
def test_adjust_requires_note_and_keeps_history(env):
    path = _copy(env, "adjust")
    con = db.connect(path)
    ym = env["ym"]
    diff = con.execute("""SELECT d.diff_id FROM reconciliation_diff d JOIN calc_run r ON r.run_id=d.run_id AND r.status='done'
                          WHERE d.period_ym=? ORDER BY d.diff_id LIMIT 1""", (ym,)).fetchone()[0]
    st.ensure(con)
    n0 = con.execute("SELECT COUNT(*) FROM recon_diff_action").fetchone()[0]
    c = login(env, "admin", path)
    r = c.post("/settle/adjust", data={"ym": ym, "diff_id": diff, "status": "checked", "note": "   "})
    assert r.status_code == 400 and "메모" in r.text
    r = c.post("/settle/adjust", data={"ym": ym, "diff_id": diff, "status": "nope", "note": "근거 메일"})
    assert r.status_code == 400
    assert con.execute("SELECT COUNT(*) FROM recon_diff_action").fetchone()[0] == n0, "메모 없이 처리 기록이 남았다"
    r = c.post("/settle/adjust", data={"ym": ym, "diff_id": diff, "status": "asked", "note": "보험사 담당에게 메일로 문의"})
    assert r.status_code == 303
    r = c.post("/settle/adjust", data={"ym": ym, "diff_id": diff, "status": "resolved", "note": "보험사 답변 확인, 다음 달 정산서에 반영"})
    assert r.status_code == 303
    rows = con.execute("SELECT status, note, acted_by FROM recon_diff_action ORDER BY action_id").fetchall()
    assert [x["status"] for x in rows[-2:]] == ["asked", "resolved"] and all(x["acted_by"] == "admin" for x in rows[-2:])
    html = c.get(f"/settle/adjust?ym={ym}&diff={diff}").text
    assert "보험사 담당에게 메일로 문의" in html and "다음 달 정산서에 반영" in html and "처리 이력 2건" in html
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='diff_update' AND target=?", (f"diff:{diff}",)).fetchone()[0] == 2
    # 대사 화면에도 같은 상태가 보인다
    assert f'data-diff="{diff}" data-status="resolved"' in c.get(f"/settle/recon?ym={ym}").text
    con.close()


# ---------------------------------------------------------------- 마감
def test_close_needs_confirm_and_reopen_needs_reason(env):
    path = _copy(env, "close")
    con = db.connect(path)
    ym = env["ym"]
    c = login(env, "admin", path)
    closed = lambda: (con.execute("SELECT status FROM close_period WHERE period_ym=?", (ym,)).fetchone() or {"status": None})["status"]   # noqa: E731
    # 막는 조건이 있으면 확인 칸이 있어도 마감하지 않는다
    r = c.post("/settle/close", data={"action": "close", "ym": ym, "agree": "yes"})
    assert r.status_code == 400 and closed() != "closed"
    assert "처리하지 않은 대사 차이" in c.get(f"/settle/close?ym={ym}").text
    # 막는 조건을 푼다. 품질 문제는 기존 도구에서 처리했다고 보고, 차이는 웹에서 처리한다
    con.execute("UPDATE quality_issue SET resolved=1 WHERE severity='block'")
    con.commit()
    for row in st.diff_list(con, [r[0] for r in con.execute("SELECT agent_id FROM agent")], ym, unmapped=True)["rows"]:
        assert c.post("/settle/adjust", data={"ym": ym, "diff_id": row["diff_id"], "status": "checked", "note": "확인함"}).status_code == 303
    html = c.get(f"/settle/close?ym={ym}&ask=close").text
    assert 'name="agree"' in html and "되돌리기 어렵" in html, "확인 단계가 보이지 않는다"
    r = c.post("/settle/close", data={"action": "close", "ym": ym})
    assert r.status_code == 400 and closed() != "closed", "확인 칸 없이 마감했다"
    r = c.post("/settle/close", data={"action": "close", "ym": ym, "agree": "yes"})
    assert r.status_code == 303 and closed() == "closed"
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='close' AND target=?", (ym,)).fetchone()[0] == 1
    # 마감한 달은 처리, 대사 다시 돌리기가 막힌다
    diff = st.diff_list(con, [r[0] for r in con.execute("SELECT agent_id FROM agent")], ym, unmapped=True)["rows"][0]["diff_id"]
    assert c.post("/settle/adjust", data={"ym": ym, "diff_id": diff, "status": "resolved", "note": "x"}).status_code == 400
    assert c.post("/settle/recon", data={"action": "rerun", "ym": ym}).status_code == 400
    assert c.post("/settle/close", data={"action": "close", "ym": ym, "agree": "yes"}).status_code == 400
    # 해제는 사유가 있어야 한다
    r = c.post("/settle/close", data={"action": "reopen", "ym": ym, "reason": "  "})
    assert r.status_code == 400 and "사유" in r.text and closed() == "closed"
    r = c.post("/settle/close", data={"action": "reopen", "ym": ym, "reason": "보험사 정산서 재수신"})
    assert r.status_code == 303 and closed() == "open"
    row = con.execute("SELECT after_json FROM audit_log WHERE action='close_reopen' AND target=?", (ym,)).fetchone()
    assert row and json.loads(row["after_json"])["reason"] == "보험사 정산서 재수신"
    html = c.get(f"/settle/close?ym={ym}").text
    assert "보험사 정산서 재수신" in html and "마감과 해제 기록" in html
    # 변경 기록 화면의 이름표
    assert "월 마감 해제" in c.get("/org/audit").text
    con.close()


def test_writes_are_main_only(env):
    c = login(env, env["mgr"]["code"])
    for path, data in (("/settle/close", {"action": "close", "ym": env["ym"], "agree": "yes"}),
                       ("/settle/adjust", {"ym": env["ym"], "diff_id": 1, "status": "checked", "note": "x"}),
                       ("/settle/recon", {"action": "rerun", "ym": env["ym"]}),
                       ("/data/statement", {"action": "cancel"})):
        assert c.post(path, data=data).status_code == 403, path


def test_csrf_required(env):
    path = _copy(env, "csrf")
    con = db.connect(path)
    c = login(env, "admin", path)
    ym = env["ym"]
    for url, data in (("/settle/close", {"action": "close", "ym": ym, "agree": "yes"}),
                      ("/settle/adjust", {"ym": ym, "diff_id": 1, "status": "checked", "note": "x"}),
                      ("/settle/recon", {"action": "rerun", "ym": ym}),
                      ("/data/statement", {"action": "cancel"})):
        assert c.post(url, data=data, raw=True).status_code == 403, url
        assert c.post(url, data={**data, "csrf_token": "wrong"}, raw=True).status_code == 403, url
    st.ensure(con)
    assert con.execute("SELECT COUNT(*) FROM recon_diff_action").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM close_period").fetchone()[0] == 0
    con.close()


# ---------------------------------------------------------------- 정산서 올리기
def test_statement_preview_writes_nothing_then_load(env):
    from app import samples
    path = _copy(env, "stmt")
    con = db.connect(path)
    ym = [m for m in (r[0] for r in con.execute("SELECT DISTINCT period_ym FROM calc_run WHERE run_type='commission' AND status='done' ORDER BY 1 DESC")) if m != env["ym"]][0]
    files, _ = samples.write_statement_files(con, ym, env["tmp"] / "stmt_raw")
    f = files[0]
    count = lambda: con.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0]   # noqa: E731
    lines = lambda: con.execute("SELECT COUNT(*) FROM commission_statement_line").fetchone()[0]   # noqa: E731
    b0, l0 = count(), lines()
    c = login(env, "admin", path)
    data = f["path"].read_bytes()
    r = c.post("/data/statement", data={"action": "preview", "kind": "statement", "insurer": f["icode"], "ym": ym},
               files={"file": ("statement.xlsx", data, "application/octet-stream")})
    assert r.status_code == 200 and "미리보기" in r.text and "이대로 넣기" in r.text
    assert (count(), lines()) == (b0, l0), "미리보기가 DB 에 썼다"
    # 미리보기에는 금액이 없다. 파일의 금액이 화면 숫자에 없다
    money = {abs(int(v)) for (v,) in con.execute("SELECT amount FROM commission_statement_line") if v and abs(v) >= MIN_AMOUNT}
    r2 = c.post("/data/statement", data={"action": "load"})
    assert r2.status_code == 200 and "넣은 결과" in r2.text
    assert count() == b0 + 1 and lines() > l0
    new = {abs(int(v)) for (v,) in con.execute("SELECT amount FROM commission_statement_line WHERE period_ym=?", (ym,)) if v and abs(v) >= MIN_AMOUNT} - money
    years = set(range(1900, 2101)) | {y * 100 + m for y in range(2000, 2101) for m in range(1, 13)}
    assert not ((_tokens(r.text) | _tokens(r2.text)) & new) - years, "미리보기나 결과에 금액이 보인다"
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='statement_load'").fetchone()[0] == 1
    # 같은 파일을 다시 올리면 미리보기에서 막는다
    r = c.post("/data/statement", data={"action": "preview", "kind": "statement", "insurer": f["icode"], "ym": ym},
               files={"file": ("statement.xlsx", data, "application/octet-stream")})
    assert r.status_code == 400 and "이미 넣은 파일" in r.text
    # 미리보기 없이 넣기는 받지 않는다
    assert c.post("/data/statement", data={"action": "load"}).status_code == 400
    # 그 달 수수료 화면은 이제 그 보험사를 정산서 기준으로 보인다
    assert 'data-basis="statement"' in c.get(f"/settle/commission?ym={ym}").text
    # 마감한 달에는 넣지 않는다
    con.execute("INSERT OR REPLACE INTO close_period(period_ym, status, closed_at, closed_by) VALUES(?,?,?,?)", (ym, "closed", db.now(), "admin"))
    con.commit()
    r = c.post("/data/statement", data={"action": "preview", "kind": "statement", "insurer": files[1]["icode"], "ym": ym},
               files={"file": ("statement.xlsx", files[1]["path"].read_bytes(), "application/octet-stream")})
    assert r.status_code == 400 and "마감한 달" in r.text
    con.close()


def test_statement_rejects_bad_input(env):
    c = login(env, "admin", _copy(env, "stmt_bad"))
    ym = env["ym"]
    r = c.post("/data/statement", data={"action": "preview", "kind": "statement", "insurer": "", "ym": ym},
               files={"file": ("a.xlsx", b"x", "application/octet-stream")})
    assert r.status_code == 400 and "보험사" in r.text
    r = c.post("/data/statement", data={"action": "preview", "kind": "persistency", "ym": "2026"},
               files={"file": ("a.xlsx", b"x", "application/octet-stream")})
    assert r.status_code == 400 and "귀속월" in r.text
    r = c.post("/data/statement", data={"action": "preview", "kind": "persistency", "ym": "202511"},
               files={"file": ("a.csv", b"x", "text/csv")})
    assert r.status_code == 400 and "xlsx" in r.text
    r = c.post("/data/statement", data={"action": "preview", "kind": "persistency", "ym": ym},
               files={"file": ("a.xlsx", b"not an excel file", "application/octet-stream")})
    assert r.status_code == 400 and "읽지 못했습니다" in r.text
