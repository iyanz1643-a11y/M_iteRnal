"""인별실적 화면 시험. 데이터 묶음의 올리기 /data/person-perf 와 실적 요약, 팀원 현황의 인별실적 묶음.

자료는 run_demo 샘플 DB 를 임시 폴더에 만들고, 그 설계사로 가짜 인별실적 파일(app/loader/fake_person_perf.py)을 만든다.
data/demo.db 와 data/real 은 열지 않는다. 실패 메시지에는 역할, 주소, 사번, 건수만 넣고 이름과 금액은 넣지 않는다.

- 메인 관리자만 올린다. 관리자와 설계사는 GET 과 POST 모두 403 이다.
- 미리보기는 건수, 사번 매칭, 막힌 줄을 보이고 이름과 금액은 보이지 않는다. 원래 DB 는 바뀌지 않는다.
- 적재는 CSRF 토큰이 있어야 하고 연결 흐름(종류 person_perf)으로 원본을 보관한다.
- 관리자는 범위 안 사람만, 설계사는 본인만 본다. 무실적 명단은 건수 0 과 파일에 없음을 나눈다.
- 인별실적 묶음은 원천(인별실적 파일), 기간, 적재 시각을 붙이고 계약 원장 표시와 따로 있다.
- 관리자 화면에 팀원의 수수료와 시상 금액이 나오지 않는다(D-17). 인별실적의 월납보험료는 실적이라 허용한다.
"""
import os
import pathlib
import re
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db
from app.loader import fake_person_perf as fp
from app.web import auth
from app.web import person_perf_view as ppv

PW = "pp-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
START, END = "202603", "202605"
FILE_NAME = f"{START}-05_인별실적.xlsx"
UNMAPPED = ["ZZ900001", "ZZ900002"]


class Client(TestClient):
    def __init__(self, app, db_path):
        super().__init__(app, follow_redirects=False)
        self.db_path = str(db_path)

    def request(self, *a, **kw):
        os.environ["APP_DB"] = self.db_path
        return super().request(*a, **kw)

    def token(self, path="/login"):
        return TOKEN.search(self.get(path).text).group(1)


def roster_from_db(con, skip_every=7):
    """DB 의 재직 설계사로 가짜 인별실적 명부를 만든다. skip_every 번째마다 파일에서 뺀다. 마스터에 없는 사번 둘을 더한다."""
    out, skipped = [], []
    for i, r in enumerate(con.execute("SELECT agent_id, internal_code, name FROM agent WHERE status='active' ORDER BY internal_code")):
        if i % skip_every == skip_every - 1:
            skipped.append(r["agent_id"])
            continue
        out.append({"code": r["internal_code"], "name": r["name"], "position": "FC", "path": ("가상", "가상본부", "가상지점", "", "")})
    for k, code in enumerate(UNMAPPED):
        out.append({"code": code, "name": f"가상{k}", "position": "FC", "path": ("가상", "가상본부", "가상지점", "", "")})
    return out, skipped


def make_file(con, path, skip_every=7):
    roster, skipped = roster_from_db(con, skip_every)
    people = fp.fake_people(roster, zero_every=4)
    fp.write_person_perf(path, people)
    return people, skipped


@pytest.fixture(scope="module")
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="web-pp-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"))
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    people, skipped = make_file(con, tmp / FILE_NAME)
    con.close()
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "app": app, "file": tmp / FILE_NAME, "people": people, "skipped": skipped}
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


def login(env, login_id, path=None):
    c = Client(env["app"], path or env["db"])
    r = c.post("/login", data={"login_id": login_id, "password": PW, "csrf_token": c.token()})
    assert r.status_code == 303, (login_id, r.status_code)
    return c


def _ctx(con, login_id):
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0]
    return auth.context(con, uid)


def _manager(con):
    """범위가 전체보다 작은 관리자 한 명."""
    total = con.execute("SELECT COUNT(*) FROM agent").fetchone()[0]
    for (code,) in con.execute("""SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN user_account u ON u.agent_id=a.agent_id
                                  WHERE m.effective_to IS NULL AND a.status='active' ORDER BY a.internal_code""").fetchall():
        ctx = _ctx(con, code)
        if ctx["role"] == "manager" and 2 < len(ctx["agent_ids"]) < total:
            return code, ctx
    raise AssertionError("범위가 좁은 관리자가 없다")


def _agent(con, codes_in_file):
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    for r in con.execute("""SELECT a.agent_id, a.internal_code FROM agent a JOIN user_account u ON u.agent_id=a.agent_id
                            WHERE a.status='active' ORDER BY a.internal_code""").fetchall():
        if r["agent_id"] not in heads and r["internal_code"] in codes_in_file:
            return r["internal_code"], r["agent_id"]
    raise AssertionError("파일에 있는 설계사 계정이 없다")


def _upload(c, env, path=None, **form):
    path = path or env["file"]
    data = {"action": "preview", "csrf_token": c.token("/data/person-perf"), **form}
    return c.post("/data/person-perf", data=data, files={"file": (path.name, path.read_bytes(), "application/octet-stream")})


def _section(html, start, end=None):
    a = html.index(start)
    b = html.index(end, a) if end else len(html)
    return html[a:b]


def _amounts(people):
    """파일의 금액 칸 값 가운데 화면에 숫자로 찍힐 수 있는 것(1,000 이상)."""
    out = set()
    for p in people:
        for v in list(p["values"].values()) + [x for pair in p["insurers"].values() for x in pair]:
            if v and v >= 1000:
                out.add(f"{int(v):,}")
    return out


def _loaded_db(env, name):
    """적재까지 마친 사본. 시험마다 따로 쓴다."""
    dst = env["tmp"] / f"{name}.db"
    shutil.copy(env["db"], dst)
    c = login(env, "admin", dst)
    assert _upload(c, env).status_code == 200
    r = c.post("/data/person-perf", data={"action": "apply", "csrf_token": c.token("/data/person-perf")})
    assert r.status_code == 200 and "배치" in r.text
    return dst


@pytest.fixture(scope="module")
def loaded(env):
    return _loaded_db(env, "loaded")


# ================================================================ 올리기는 메인 관리자만
def test_only_main_admin_uploads(env):
    con = db.connect(env["db"])
    mgr, _ = _manager(con)
    codes = {p["사원번호"] for p in env["people"]}
    agent, _ = _agent(con, codes)
    con.close()
    for login_id in (mgr, agent):
        c = login(env, login_id)
        assert c.get("/data/person-perf").status_code == 403, login_id
        tok = c.token("/me")
        r = c.post("/data/person-perf", data={"action": "preview", "csrf_token": tok},
                   files={"file": (FILE_NAME, env["file"].read_bytes(), "application/octet-stream")})
        assert r.status_code == 403, login_id
        r = c.post("/data/person-perf", data={"action": "apply", "csrf_token": tok})
        assert r.status_code == 403, login_id
        # 메뉴에도 보이지 않는다
        assert 'href="/data/person-perf"' not in c.get("/").text
    c = login(env, "admin")
    r = c.get("/data/person-perf")
    assert r.status_code == 200 and "인별실적" in r.text


# ================================================================ 미리보기
def test_preview_shows_counts_and_hides_names_and_amounts(env):
    con = db.connect(env["db"])
    before = con.execute("SELECT COUNT(*) FROM import_batch WHERE data_type='person_perf'").fetchone()[0]
    c = login(env, "admin")
    r = _upload(c, env)
    assert r.status_code == 200, r.status_code
    html = r.text
    people = env["people"]
    mapped = len(people) - len(UNMAPPED)

    def col(name):
        m = re.search(rf'data-col="{name}">(\d+)<', html)
        assert m, name
        return int(m.group(1))
    assert col("file_rows") == len(people) and col("loaded") == len(people) and col("skipped") == 0
    assert col("matched") == mapped and col("unmapped") == len(UNMAPPED) and col("name_mismatch") == 0
    assert "2026년 3월 ~ 5월" in html and "이대로 적재하기" in html
    # 이름과 금액은 보이지 않는다
    names = {p["사원명"] for p in people if len(p["사원명"]) >= 2}
    assert not [n for n in names if n in html], "미리보기에 사원명이 보인다"
    shown = set(re.findall(r"\d{1,3}(?:,\d{3})+", html))
    assert not (shown & _amounts(people)), "미리보기에 금액이 보인다"
    # 원래 DB 는 그대로다
    after = con.execute("SELECT COUNT(*) FROM import_batch WHERE data_type='person_perf'").fetchone()[0]
    con.close()
    assert after == before
    c.post("/data/person-perf", data={"action": "cancel", "csrf_token": c.token("/data/person-perf")})


def test_preview_lists_blocked_rows_by_row_number(env):
    """사번이 겹치는 줄은 막힌 줄로 줄 번호와 까닭만 보인다."""
    con = db.connect(env["db"])
    roster, _ = roster_from_db(con)
    con.close()
    people = fp.fake_people(roster, zero_every=4)
    people.append(dict(people[1]))                                   # 같은 사번이 한 번 더
    path = fp.write_person_perf(env["tmp"] / "dup" / FILE_NAME, people)
    c = login(env, "admin")
    r = _upload(c, env, path)
    assert r.status_code == 200
    blocked = _section(r.text, "막힌 줄 1줄", "</table>")
    assert "겹칩니다" in blocked and re.search(r'<td class="num">\d+</td>', blocked)
    c.post("/data/person-perf", data={"action": "cancel", "csrf_token": c.token("/data/person-perf")})


def test_preview_needs_period_and_right_header(env):
    c = login(env, "admin")
    bad = env["tmp"] / "x.xlsx"
    shutil.copy(env["file"], bad)
    r = _upload(c, env, bad)
    assert r.status_code == 400 and "기간" in r.text                # 이름에 기간이 없고 칸도 비었다
    r = _upload(c, env, bad, start_ym=START, end_ym=END)
    assert r.status_code == 200 and "2026년 3월 ~ 5월" in r.text    # 칸으로 정하면 된다
    other = fp.write_person_perf(env["tmp"] / "other" / FILE_NAME, env["people"], sheet="다른시트", work_sheets=False)
    r = _upload(c, env, other)
    assert r.status_code == 400 and "인별실적 양식이 아닙니다" in r.text
    c.post("/data/person-perf", data={"action": "cancel", "csrf_token": c.token("/data/person-perf")})


# ================================================================ CSRF 와 적재
def test_apply_needs_csrf_and_loads_through_connect(env):
    dst = env["tmp"] / "apply.db"
    shutil.copy(env["db"], dst)
    c = login(env, "admin", dst)
    assert _upload(c, env).status_code == 200
    r = c.post("/data/person-perf", data={"action": "apply"})
    assert r.status_code == 403                                      # 토큰 없음
    r = c.post("/data/person-perf", data={"action": "apply", "csrf_token": "x" * 43})
    assert r.status_code == 403                                      # 다른 토큰
    r = c.post("/data/person-perf", data={"action": "apply", "csrf_token": c.token("/data/person-perf")})
    assert r.status_code == 200 and "okmsg" in r.text
    con = db.connect(dst)
    b = con.execute("SELECT batch_id, status, received_via, file_path FROM import_batch WHERE data_type='person_perf'").fetchall()
    assert len(b) == 1 and b[0]["status"] == "active" and b[0]["received_via"] == "upload"
    assert b[0]["file_path"].startswith("raw/person_perf/")
    assert (pathlib.Path(os.environ["CONNECT_DATA_DIR"]) / b[0]["file_path"]).is_file()
    n = con.execute("SELECT COUNT(*) FROM v_agent_period_perf WHERE start_ym=? AND end_ym=?", (START, END)).fetchone()[0]
    assert n == len(env["people"])
    con.close()
    # 같은 파일은 다시 받지 않는다
    r = _upload(c, env)
    assert r.status_code == 409
    # 올린 파일은 지웠다
    assert not list(pathlib.Path(os.environ["APP_UPLOAD_DIR"]).glob("person_perf_*"))


def test_preview_post_without_csrf_is_refused(env):
    c = login(env, "admin")
    r = c.post("/data/person-perf", data={"action": "preview"},
               files={"file": (FILE_NAME, env["file"].read_bytes(), "application/octet-stream")})
    assert r.status_code == 403


# ================================================================ 범위
def _codes_in(html):
    return set(re.findall(r'data-code="([^"]+)"', html))


def test_manager_sees_only_scope(env, loaded):
    con = db.connect(loaded)
    code, ctx = _manager(con)
    scope_codes = {r[0] for r in con.execute(f"SELECT internal_code FROM agent WHERE agent_id IN ({','.join('?' * len(ctx['agent_ids']))})",
                                             sorted(ctx["agent_ids"]))}
    file_codes = {p["사원번호"] for p in env["people"]}
    html = login(env, code, loaded).get("/team/members").text
    sec = _section(html, 'id="person-perf"')
    shown = _codes_in(sec)
    assert shown and shown <= scope_codes, sorted(shown - scope_codes)
    assert not (set(UNMAPPED) & shown)
    outside = (file_codes - scope_codes) - set(UNMAPPED)
    assert outside and not (outside & shown)
    # 화면의 구분 분포는 같은 범위로 읽은 값과 같다
    t = ppv.team(con, ctx["agent_ids"], ctx["ym"])
    for k in ppv.TIERS:
        assert f'data-tier="{k}">{t["dist"][k]}<' in sec, k
    assert "설계사 마스터에 없는 사번" not in sec                   # 메인 관리자에게만 보이는 건수
    con.close()


def test_agent_sees_only_self_and_no_team_screen(env, loaded):
    con = db.connect(loaded)
    codes = {p["사원번호"] for p in env["people"]}
    code, aid = _agent(con, codes)
    c = login(env, code, loaded)
    assert c.get("/team/members").status_code == 403
    html = c.get("/my/summary").text
    sec = _section(html, 'id="person-perf"')
    mine = next(p for p in env["people"] if p["사원번호"] == code)
    prem = mine["values"][("total", "월납보험료")]
    assert f'data-col="pp-premium">{int(prem):,}<' in sec
    assert f'data-col="pp-count">{mine["values"][("total", "건수")]}<' in sec
    # 남의 금액 합계는 없다
    others = {f'{int(p["values"][("total", "월납보험료")]):,}' for p in env["people"] if p["사원번호"] != code
              and p["values"][("total", "월납보험료")] >= 1000 and p["values"][("total", "월납보험료")] != prem}
    assert not (set(re.findall(r'data-col="pp-premium">([\d,]+)<', html)) & others)
    con.close()


# ================================================================ 무실적 명단 두 가지
def test_no_perf_list_splits_zero_count_and_absent(env, loaded):
    con = db.connect(loaded)
    html = login(env, "admin", loaded).get("/team/members").text
    none_sec = _section(html, 'class="card pp-none"', 'class="card pp-absent"')
    absent_sec = _section(html, 'class="card pp-absent"', "<!-- pp-lists-end -->")
    file_zero = {p["사원번호"] for p in env["people"] if p["values"][("total", "건수")] == 0} - set(UNMAPPED)
    active = {r[0]: r[1] for r in con.execute("SELECT internal_code, agent_id FROM agent WHERE status='active'")}
    skipped_codes = {c for c, a in active.items() if a in set(env["skipped"])}
    assert file_zero and skipped_codes
    assert _codes_in(none_sec) == file_zero
    assert _codes_in(absent_sec) == skipped_codes
    # 금액 없이 사람과 건수만: 건수 0 은 0, 파일에 없음은 '-'
    assert re.findall(r'<td class="num">([^<]*)</td>', none_sec) == ["0"] * len(file_zero)
    assert set(re.findall(r'<td class="num">([^<]*)</td>', absent_sec)) == {"-"}
    assert not re.search(r"\d{1,3}(?:,\d{3})+", none_sec + absent_sec)
    # 메인 관리자에게는 마스터에 없는 사번 수가 보인다
    assert f"설계사 마스터에 없는 사번 {len(UNMAPPED)}명" in html
    con.close()


# ================================================================ 원천 표시
def test_source_period_and_load_time_are_marked(env, loaded):
    con = db.connect(loaded)
    loaded_at = con.execute("SELECT loaded_at FROM import_batch WHERE data_type='person_perf' AND status='active'").fetchone()[0]
    when = f"{int(loaded_at[5:7])}월 {int(loaded_at[8:10])}일 {int(loaded_at[11:13])}시 {loaded_at[14:16]}분"
    code, _ = _manager(con)
    con.close()
    for login_id, path in ((code, "/team/members"), (code, "/my/summary"), ("admin", "/team/members")):
        html = login(env, login_id, loaded).get(path).text
        sec = _section(html, 'id="person-perf"')
        assert "인별실적 파일 기준 3개월 실적" in sec, (login_id, path)
        assert '<span class="chip blue">인별실적 파일</span> 2026년 3월 ~ 5월' in sec, (login_id, path)
        assert f"{when} 적재 기준" in sec, (login_id, path)
        assert "원천과 기간이 달라" in sec, (login_id, path)
        # 계약 원장 쪽은 따로 표시하고 인별실적 묶음 앞에 있다
        ledger = html.index('<span class="chip pp-ledger">계약 원장</span>')
        assert ledger < html.index('id="person-perf"'), (login_id, path)


def test_screens_without_file_say_so(env):
    c = login(env, "admin")
    html = c.get("/team/members").text
    assert "아직 들어온 인별실적 파일이 없습니다" in html


# ================================================================ D-17
def test_d17_manager_pages_with_person_perf_have_no_member_commission_or_award(env, loaded):
    """인별실적을 붙인 화면에서도 관리자에게 팀원의 수수료, 시상, 예상 시상 금액이 나오지 않는다.
    알아보는 법은 tests/test_s1_decisions.py 의 D-17 시험과 같다. 인별실적의 금액은 실적이라 나와도 되는 숫자에 넣는다."""
    from tests.test_s1_decisions import _checkable, _leaks
    con = db.connect(loaded)
    ym = auth.current_ym(con)
    allowed = {int(a.replace(",", "")) for a in _amounts(env["people"])}
    leaks, checked = [], 0
    for (code,) in con.execute("""SELECT DISTINCT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                                  JOIN user_account u ON u.agent_id=a.agent_id WHERE m.effective_to IS NULL AND a.status='active'
                                  ORDER BY a.internal_code LIMIT 4""").fetchall():
        ctx = _ctx(con, code)
        if ctx["role"] != "manager":
            continue
        checkable = {v: f for v, f in _checkable(con, ctx, ctx["agent_ids"] - {ctx["agent_id"]}, ym).items() if v not in allowed}
        checked += len(checkable)
        c = login(env, code, loaded)
        pages = {p: c.get(p).text for p in ("/team/members", "/my/summary")}
        leaks += _leaks(pages, checkable, code)
    con.close()
    assert checked >= 5
    assert not leaks, leaks
