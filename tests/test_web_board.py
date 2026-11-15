"""공지와 자료실 /board 와 신인 정착 /team/onboarding 의 시험. S2 파트 F.

보는 것: 대상 밖 사람은 글과 첨부를 못 본다, 첨부의 꼴과 크기 막기, 내려받기 머리글, 메인 관리자만 올린다,
시상안 원문은 올릴 때 고른 대로 보인다, 새 공지가 알림함에 뜬다, 신인 정착의 범위와 체크, 변경 기록의 이름표.

첨부 폴더는 APP_BOARD_DIR 로 시험마다 임시 폴더를 쓴다. 저장소의 data/board 는 건드리지 않는다.
"""
import io
import os
import re
import zipfile
from datetime import date, timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from app import db, masters, samples_edu
from app.web import audit_view, auth
from app.web import board as bd
from app.web.routes import notify
from app.web.routes import team as team_route

PW = "board-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
PDF = b"%PDF-1.4\n% board test\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def _zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in entries.items():
            z.writestr(k, v)
    return buf.getvalue()


XLSX = _zip({"[Content_Types].xml": "<x/>", "xl/workbook.xml": "<x/>"})
DOCX = _zip({"[Content_Types].xml": "<x/>", "word/document.xml": "<x/>"})


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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_BOARD_DIR", str(tmp_path / "board"))
    monkeypatch.setenv("APP_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CONNECT_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "board.db"
    con = db.init_db(path)
    masters.seed(con)
    samples_edu.seed(con)
    auth.seed_accounts(con, PW)
    notify.clear_cache()
    uid = lambda login: con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login,)).fetchone()[0]
    branch_heads = con.execute("""SELECT a.internal_code, m.org_id FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id
                                  WHERE o.level='branch' ORDER BY a.internal_code""").fetchall()
    manager, org_x = branch_heads[0]
    mctx = auth.context(con, uid(manager))
    other = next(code for code, oid in branch_heads if oid != org_x and not ({oid} & mctx["org_ids"])
                 and org_x not in auth.context(con, uid(code))["org_ids"])
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    fcs = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' AND job_type='fc' ORDER BY agent_id")]
    inside = [a for a in fcs if a in mctx["agent_ids"] and a not in heads]
    outside = [a for a in fcs if a not in mctx["agent_ids"]]
    login_of = lambda a: con.execute("SELECT login_id FROM user_account WHERE agent_id=?", (a,)).fetchone()[0]
    assert bd.board_dir() == tmp_path / "board"            # 저장소의 data/board 를 쓰지 않는다
    from app.web.main import app
    yield {"con": con, "path": path, "app": app, "tmp": tmp_path, "manager": manager, "other": other, "org_x": org_x,
           "mctx": mctx, "inside": inside, "outside": outside, "in_login": login_of(inside[0]), "out_login": login_of(outside[0]),
           "login_of": login_of}
    notify.clear_cache()
    con.close()


def login(env, login_id):
    c = Client(env["app"], env["path"])
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, r.status_code
    return c


def _post(c, *, title, category="notice", body="본문입니다", org="", role="", fc=None, files=(), post_id="", remove=()):
    data = {"action": "save", "category": category, "title": title, "body": body, "target_org_id": org, "target_role": role, "post_id": post_id}
    if fc is not None:
        data["fc_visible"] = fc
    if remove:
        data["remove"] = list(remove)
    return c.post("/board", data=data, files=[("files", f) for f in files] or None)


def _pid(env, title):
    r = env["con"].execute("SELECT post_id FROM board_post WHERE title=?", (title,)).fetchone()
    return r[0] if r else None


def _fid(env, pid):
    return env["con"].execute("SELECT file_id FROM board_file WHERE post_id=? AND removed_at IS NULL ORDER BY file_id", (pid,)).fetchone()[0]


# ---------------------------------------------------------------- 접근과 올리기 권한
def test_three_roles_open_board_and_only_main_and_manager_open_onboarding(env):
    for who, board, onboard in (("admin", 200, 200), (env["manager"], 200, 200), (env["in_login"], 200, 403)):
        c = login(env, who)
        assert c.get("/board").status_code == board, who
        assert c.get("/team/onboarding").status_code == onboard, who


def test_only_main_posts_edits_and_withdraws(env):
    con = env["con"]
    for who in (env["manager"], env["in_login"]):
        c = login(env, who)
        assert _post(c, title="관리자가 올린 글", org=env["org_x"]).status_code == 403
        assert c.get("/board?new=1").status_code == 403
    assert con.execute("SELECT COUNT(*) FROM board_post").fetchone()[0] == 0
    denied = [r[0] for r in con.execute("SELECT target FROM audit_log WHERE action='access_denied' AND target LIKE '/board%'")]
    assert len(denied) == 4
    admin = login(env, "admin")
    assert _post(admin, title="첫 공지").status_code == 303
    pid = _pid(env, "첫 공지")
    for who in (env["manager"], env["in_login"]):
        c = login(env, who)
        assert c.post("/board", data={"action": "withdraw", "post_id": pid, "confirm": "1"}).status_code == 403
        assert c.get(f"/board?edit={pid}").status_code == 403
    assert con.execute("SELECT status FROM board_post WHERE post_id=?", (pid,)).fetchone()[0] == "posted"
    r = admin.post("/board", data={"action": "withdraw", "post_id": pid})            # 확인 단계를 한 번 거친다
    assert r.status_code == 303 and "ask=withdraw" in r.headers["location"]
    assert con.execute("SELECT status FROM board_post WHERE post_id=?", (pid,)).fetchone()[0] == "posted"
    assert admin.post("/board", data={"action": "withdraw", "post_id": pid, "confirm": "1"}).status_code == 303
    assert con.execute("SELECT status FROM board_post WHERE post_id=?", (pid,)).fetchone()[0] == "withdrawn"
    assert "첫 공지" not in login(env, env["in_login"]).get("/board").text
    assert "내린 글 1건" in admin.get("/board").text


# ---------------------------------------------------------------- 대상
def test_people_outside_the_target_do_not_see_posts(env):
    admin = login(env, "admin")
    x = env["org_x"]
    for title, org, role in (("지점X 공지", x, ""), ("관리자 공지", "", "manager"), ("전체 공지", "", ""), ("지점X 설계사 공지", x, "agent")):
        assert _post(admin, title=title, org=org, role=role).status_code == 303
    want = {
        env["in_login"]: {"지점X 공지", "전체 공지", "지점X 설계사 공지"},
        env["out_login"]: {"전체 공지"},
        env["manager"]: {"지점X 공지", "관리자 공지", "전체 공지"},          # 맡은 조직이 대상 조직이다. 역할이 설계사인 글은 못 본다
        env["other"]: {"관리자 공지", "전체 공지"},                           # 다른 지점의 관리자
        "admin": {"지점X 공지", "관리자 공지", "전체 공지", "지점X 설계사 공지"},
    }
    titles = {"지점X 공지", "관리자 공지", "전체 공지", "지점X 설계사 공지"}
    for who, seen in want.items():
        html = login(env, who).get("/board").text
        assert {t for t in titles if f">{t}<" in html} == seen, who
    # 대상 밖 사람이 번호로 열면 없는 것과 같고, 시도가 기록에 남는다
    pid = _pid(env, "지점X 공지")
    out = login(env, env["out_login"])
    r = out.get(f"/board?post={pid}")
    assert r.status_code == 404 and "지점X 공지" not in r.text
    assert env["con"].execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied' AND target=?", (f"/board post:{pid}",)).fetchone()[0] == 1
    assert login(env, env["in_login"]).get(f"/board?post={pid}").status_code == 200


def test_target_below_the_org_includes_its_children(env):
    """대상 조직은 그 아래를 포함한다. 지점 아래 팀의 설계사도 지점 공지를 본다."""
    con = env["con"]
    x = env["org_x"]
    child = con.execute("SELECT org_id FROM org_unit WHERE parent_id=? LIMIT 1", (x,)).fetchone()
    if child is None:
        pytest.skip("샘플 지점에 아래 조직이 없다")
    deep = next((a for a in env["inside"] if con.execute("""SELECT 1 FROM agent_org_history WHERE agent_id=? AND org_id<>? AND effective_to IS NULL""",
                                                          (a, x)).fetchone()), None)
    admin = login(env, "admin")
    assert _post(admin, title="지점 아래 포함", org=x).status_code == 303
    assert _post(admin, title="아래 조직 하나만", org=child[0]).status_code == 303
    mgr = login(env, env["manager"]).get("/board").text
    assert ">지점 아래 포함<" in mgr and ">아래 조직 하나만<" not in mgr                 # 대상 조직보다 위의 관리자는 대상 밖이다
    if deep:
        assert ">지점 아래 포함<" in login(env, env["login_of"](deep)).get("/board").text


# ---------------------------------------------------------------- 첨부
def test_file_type_and_size_are_checked(env, monkeypatch):
    con = env["con"]
    admin = login(env, "admin")
    bad = [("run.exe", b"MZ" + b"\x00" * 64, "application/octet-stream"),
           ("note.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
           ("fake.pdf", PNG, "application/pdf"),                          # 확장자와 내용이 다르다
           ("sheet.xlsx", DOCX, "application/octet-stream"),               # zip 이지만 엑셀이 아니다
           ("empty.pdf", b"", "application/pdf")]
    for f in bad:
        r = _post(admin, title=f"막힐 글 {f[0]}", files=[f])
        assert r.status_code == 400, f[0]
        assert "첨부 파일은 다시 골라 주세요" in r.text
    with monkeypatch.context() as m:                       # undo 를 쓰면 APP_BOARD_DIR 까지 풀린다. 이 줄만 바꾼다
        m.setattr(bd, "MAX_BYTES", 1024)
        r = _post(admin, title="큰 파일", files=[("big.pdf", PDF + b"0" * 2048, "application/pdf")])
    assert r.status_code == 400 and "MB" in r.text
    assert bd.board_dir() == env["tmp"] / "board"
    six = [(f"f{i}.pdf", PDF + bytes([i]), "application/pdf") for i in range(6)]
    assert _post(admin, title="여섯 개", files=six).status_code == 400
    assert con.execute("SELECT COUNT(*) FROM board_post").fetchone()[0] == 0
    assert not (env["tmp"] / "board").exists() or not any((env["tmp"] / "board").iterdir())    # 막힌 파일은 쓰지 않는다
    good = [("안내문.pdf", PDF, "application/pdf"), ("사진.png", PNG, "image/png"), ("양식.xlsx", XLSX, "application/octet-stream"),
            ("교안.hwp", OLE, "application/octet-stream"), ("서식.docx", DOCX, "application/octet-stream")]
    assert _post(admin, title="다섯 개", category="form", files=good).status_code == 303
    rows = con.execute("SELECT name, size_bytes, sha256, kind FROM board_file ORDER BY file_id").fetchall()
    assert [r["kind"] for r in rows] == ["pdf", "image", "excel", "hwp", "word"]
    for (name, data, _), r in zip(good, rows):
        assert r["name"] == name and r["size_bytes"] == len(data) and r["sha256"] == sha256(data).hexdigest()
        p = env["tmp"] / "board" / r["sha256"]                                  # 저장소 밖 폴더에 해시 이름으로 둔다
        assert p.read_bytes() == data
    cols = {r[1] for r in con.execute("PRAGMA table_info(board_file)")}
    assert not cols & {"content", "data", "blob"}                               # DB 에는 내용을 두지 않는다


def test_download_headers_and_only_people_in_the_target(env):
    con = env["con"]
    admin = login(env, "admin")
    assert _post(admin, title="지점X 자료", category="lesson", org=env["org_x"], files=[("교안 1주차.pdf", PDF, "application/pdf")]).status_code == 303
    pid = _pid(env, "지점X 자료")
    fid = _fid(env, pid)
    c = login(env, env["in_login"])
    r = c.get(f"/board?file={fid}")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"].startswith("application/pdf")
    logged = con.execute("SELECT actor, target FROM audit_log WHERE action='board_download'").fetchall()
    assert [(x["actor"], x["target"]) for x in logged] == [(env["in_login"], f"{pid}:{fid}")]
    out = login(env, env["out_login"])
    r = out.get(f"/board?file={fid}")
    assert r.status_code == 404 and r.content != PDF
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied' AND target=?", (f"/board file:{fid}",)).fetchone()[0] == 1
    assert out.get("/board?file=99999").status_code == 404
    assert login(env, env["manager"]).get(f"/board?file={fid}").status_code == 200
    # 고치면서 뺀 첨부는 아무도 받지 못하고, 파일과 줄은 남는다
    assert _post(admin, title="지점X 자료", category="lesson", org=env["org_x"], post_id=pid, remove=[fid],
                 files=[("교안 2주차.pdf", PDF + b"2", "application/pdf")]).status_code == 303
    assert c.get(f"/board?file={fid}").status_code == 404
    assert con.execute("SELECT removed_at IS NOT NULL FROM board_file WHERE file_id=?", (fid,)).fetchone()[0] == 1
    new_fid = _fid(env, pid)
    assert new_fid != fid and c.get(f"/board?file={new_fid}").status_code == 200
    # 내린 글의 첨부는 메인 관리자만 받는다
    assert admin.post("/board", data={"action": "withdraw", "post_id": pid, "confirm": "1"}).status_code == 303
    assert c.get(f"/board?file={new_fid}").status_code == 404
    assert admin.get(f"/board?file={new_fid}").status_code == 200


def test_download_name_is_safe():
    assert bd.safe_name("../../etc/passwd.PDF") == "passwd.pdf"
    assert bd.safe_name("C:\\temp\\교안<1>.hwp") == "교안_1_.hwp"
    d = bd.disposition("교안 \"1\".pdf")
    assert d.startswith('attachment; filename="') and '"1"' not in d.split(";")[1]


# ---------------------------------------------------------------- 시상안 원문
def test_plan_original_needs_a_choice_and_stays_with_main_unless_fc_only(env):
    admin = login(env, "admin")
    r = _post(admin, title="시상안 고르지 않음", category="plan", files=[("plan.pdf", PDF, "application/pdf")])
    assert r.status_code == 400 and "설계사에게 보여도 되는 파일인지" in r.text
    assert _post(admin, title="시상안 전체 몫", category="plan", fc="0", files=[("plan.pdf", PDF, "application/pdf")]).status_code == 303
    assert _post(admin, title="시상안 FC 몫", category="plan", fc="1", files=[("fc.pdf", PDF + b"fc", "application/pdf")]).status_code == 303
    whole, fc = _pid(env, "시상안 전체 몫"), _pid(env, "시상안 FC 몫")
    whole_file = _fid(env, whole)
    for who in (env["in_login"], env["manager"]):
        c = login(env, who)
        html = c.get("/board").text
        assert ">시상안 FC 몫<" in html and ">시상안 전체 몫<" not in html, who
        assert c.get(f"/board?post={whole}").status_code == 404
        assert c.get(f"/board?file={whole_file}").status_code == 404
        assert c.get(f"/board?post={fc}").status_code == 200
    main_html = admin.get("/board?cat=plan").text
    assert ">시상안 전체 몫<" in main_html and "메인 관리자만" in main_html
    assert admin.get(f"/board?file={whole_file}").status_code == 200


# ---------------------------------------------------------------- 알림
def test_new_notice_shows_in_notifications_for_people_in_the_target(env):
    admin = login(env, "admin")
    assert _post(admin, title="지점X 새 공지", org=env["org_x"], body="이번 주 금요일 교육장 변경 안내입니다.").status_code == 303
    assert _post(admin, title="교안만 올림", category="lesson", org=env["org_x"]).status_code == 303
    pid = _pid(env, "지점X 새 공지")
    inside = login(env, env["in_login"])
    html = inside.get("/notifications").text
    assert "새 공지" in html and "지점X 새 공지" in html and f"/board?post={pid}" in html
    assert "교안만 올림" not in html                                            # 새 공지는 공지 분류만이다
    assert "지점X 새 공지" not in login(env, env["out_login"]).get("/notifications").text
    assert "지점X 새 공지" not in admin.get("/notifications").text              # 올린 사람에게는 뜨지 않는다
    assert "지점X 새 공지" in login(env, env["manager"]).get("/notifications").text
    # 읽음 표시와 종 아이콘
    ctx = auth.context(env["con"], env["con"].execute("SELECT user_id FROM user_account WHERE login_id=?", (env["in_login"],)).fetchone()[0])
    keys = {n["key"] for n in notify.collect(env["con"], ctx)}
    assert f"board_new:{pid}" in keys
    before = notify.unread_count(env["con"], ctx)
    assert inside.post("/notifications", data={"action": "read", "key": f"board_new:{pid}"}).status_code == 303
    assert notify.unread_count(env["con"], ctx) == before - 1
    # 내리면 알림에서도 빠진다
    assert admin.post("/board", data={"action": "withdraw", "post_id": pid, "confirm": "1"}).status_code == 303
    assert f"board_new:{pid}" not in {n["key"] for n in notify.collect(env["con"], ctx)}


# ---------------------------------------------------------------- 변경 기록
def test_board_actions_are_logged_with_labels(env):
    con = env["con"]
    admin = login(env, "admin")
    assert _post(admin, title="기록 시험", files=[("a.pdf", PDF, "application/pdf")]).status_code == 303
    pid = _pid(env, "기록 시험")
    assert _post(admin, title="기록 시험 고침", post_id=pid, org=env["org_x"]).status_code == 303
    assert login(env, env["in_login"]).get(f"/board?file={_fid(env, pid)}").status_code == 200
    assert admin.post("/board", data={"action": "withdraw", "post_id": pid, "confirm": "1"}).status_code == 303
    acts = [r[0] for r in con.execute("SELECT action FROM audit_log WHERE action LIKE 'board_%' ORDER BY id")]
    assert acts == ["board_post", "board_update", "board_download", "board_withdraw"]
    upd = con.execute("SELECT before_json, after_json FROM audit_log WHERE action='board_update'").fetchone()
    assert '"기록 시험"' in upd[0] and '"기록 시험 고침"' in upd[1] and "본문" not in (upd[0] + upd[1])
    for a in ("board_post", "board_update", "board_withdraw", "board_download"):
        assert re.search(r"[가-힣]", audit_view.ACTION_KO[a])
    html = admin.get("/org/audit").text
    assert "공지·자료 올림" in html and "게시물: 기록 시험 고침" in html and "첨부 a.pdf" in html
    assert "board_post" not in html


# ---------------------------------------------------------------- 신인 정착
def _contract(con, agent_id, status, day, n=1):
    ins, prod = con.execute("SELECT insurer_id, product_id FROM product ORDER BY product_id LIMIT 1").fetchone()
    for _ in range(n):
        con.execute("""INSERT INTO contract(insurer_id, product_id, agent_id, application_date, contract_date, status, policy_count,
                                            premium, premium_monthly, converted_premium, created_at)
                       VALUES(?,?,?,?,?,?,1,987654,987654,1234567,?)""",
                    (ins, prod, agent_id, day, day if status != "applied" else None, status, db.now()))
    con.commit()


def test_onboarding_scope_checklist_and_no_amounts(env):
    con = env["con"]
    today = date.today()
    new_in, new_in2, old_in = env["inside"][:3]
    new_out = env["outside"][0]
    for a, days in ((new_in, 10), (new_in2, 40), (old_in, 200), (new_out, 5)):
        con.execute("UPDATE agent SET hire_date=? WHERE agent_id=?", ((today - timedelta(days=days)).isoformat(), a))
    con.commit()
    _contract(con, new_in, "inforce", today.replace(day=1).isoformat(), n=2)
    _contract(con, new_in2, "applied", today.replace(day=1).isoformat())
    name = lambda a: con.execute("SELECT name FROM agent WHERE agent_id=?", (a,)).fetchone()[0]
    html = login(env, env["manager"]).get("/team/onboarding").text
    assert name(new_in) in html and name(new_in2) in html
    assert name(old_in) not in html and name(new_out) not in html               # 90일이 지난 사람과 범위 밖 사람은 없다
    assert "1,234,567" not in html and "1234567" not in html and "987,654" not in html     # 금액은 보이지 않는다
    assert "청약 1건 · 예정" in html and "성립 2건" in html
    main = login(env, "admin").get("/team/onboarding").text
    assert name(new_out) in main and name(new_in) in main and name(old_in) not in main
    # 줄의 값. 메인 관리자 문맥으로 본다
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id='admin'").fetchone()[0]
    ctx = {**auth.context(con, uid), "scope": "all"}
    rows = {r["agent_id"]: r for r in team_route.onboarding_rows(con, ctx, today)}
    assert set(rows) == {new_in, new_in2, new_out}
    ym = today.strftime("%Y%m")
    assert rows[new_in]["first_contract"] == ym and rows[new_in]["first_active"] == ym and rows[new_in]["new_count"] == 2
    assert rows[new_in2]["first_contract"] is None and rows[new_in2]["first_active"] is None and rows[new_in2]["applied"] == 1
    assert rows[new_out]["first_contract"] is None
    assert not any(k for r in rows.values() for k in r if "premium" in k or "amount" in k)
    # 관리자 문맥은 범위 안만
    mctx = {**auth.context(con, env["mctx"]["user_id"]), "scope": "org"}
    assert {r["agent_id"] for r in team_route.onboarding_rows(con, mctx, today)} == {new_in, new_in2}


def test_onboarding_first_active_follows_withdrawals(env):
    """같은 달에 성립하고 그 달에 철회하면 첫 계약은 있지만 순건수가 0이라 첫 가동은 아니다. 업적 집계 규칙과 같다."""
    con = env["con"]
    today = date.today()
    a = env["inside"][0]
    con.execute("UPDATE agent SET hire_date=? WHERE agent_id=?", ((today - timedelta(days=3)).isoformat(), a))
    _contract(con, a, "withdrawn", today.replace(day=1).isoformat())
    con.execute("UPDATE contract SET withdrawal_date=? WHERE agent_id=?", (today.replace(day=1).isoformat(), a))
    con.commit()
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id='admin'").fetchone()[0]
    rows = {r["agent_id"]: r for r in team_route.onboarding_rows(con, {**auth.context(con, uid), "scope": "all"}, today)}
    assert rows[a]["first_contract"] == today.strftime("%Y%m") and rows[a]["first_active"] is None
