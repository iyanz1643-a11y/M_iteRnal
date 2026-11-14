"""S1 검증. 만든 쪽과 따로, 실제 양식의 가짜 파일을 연결 파이프라인으로 끝까지 넣고 화면과 엔진의 숫자를 대조한다.

흐름: 가짜 사원현황과 계약현황 → app.connect 의 Pipeline → 조직 여섯 단계와 설계사 → 계약 원장 → 업적 집계와 랭킹
     → 웹 화면(홈, 팀원 현황, 달성 현황, 조직 부문 시상) → 같은 파일 다시 넣기.
모두 임시 DB 와 임시 폴더에서 한다. 사람 이름과 금액은 출력하지 않고 같음만 본다.

xfail(strict=True) 인 시험은 검증 때 찾은 문제의 재현이다. 고치면 xfail 을 지운다.
"""
import os
import pathlib
import re
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db, masters
from app.connect import Pipeline, StateStore, UploadSource
from app.connect.base import DUPLICATE, LOADED
from app.engine import org_award as oa
from app.engine import orgs, performance, ranking
from app.engine import promotion as pm
from app.loader import fake_eternal as fe
from app.org import sync
from app.web import auth

YM = "202602"
PW = "verify-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')


def _num(text):
    """화면의 '1,234' 나 '-3' 을 숫자로. '-' 하나는 0 이다."""
    t = (text or "").strip().replace(",", "")
    return 0 if t in ("", "-") else float(t)


class Client(TestClient):
    def post(self, url, data=None, **kw):
        if data is not None and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


def _build(tmp: pathlib.Path):
    """빈 DB 에 실제 원수사와 구분 마스터만 두고, 가짜 실제 양식 두 파일을 연결 파이프라인으로 넣는다."""
    con = db.init_db(tmp / "real.db")
    for i, (code, name, elig) in enumerate(masters.CATEGORIES):
        con.execute("INSERT INTO product_category(code, name, incentive_eligible, sort_order) VALUES(?,?,?,?)", (code, name, elig, i))
    con.commit()
    masters.seed_real_insurers(con)
    roster = fe.fake_roster()
    emp, emp_rows = fe.make_employee_file(tmp / "in" / "사원현황_202602.xlsx", roster)
    ctr, ctr_rows = fe.make_contract_file(tmp / "in" / "계약현황_202602.xlsx", period_ym=YM, roster=roster)
    pipe = Pipeline(con, data_dir=tmp / "data", state=StateStore(tmp / "state"))
    first = [pipe.run(UploadSource.from_path(emp, kind="employee", period_ym=YM)),
             pipe.run(UploadSource.from_path(ctr, kind="contract", period_ym=YM))]
    performance.run(con, YM)
    ranking.run(con, YM)
    # 한 달짜리 정률 시책 하나와 GA 전체의 조직 부문 둘. 화면의 달성 현황과 조직 부문 시상을 대조하려고 둔다
    pm.create(con, period_ym=YM, name="검증 정률 시책", kind="rate", windows=[("", "2026-02-01", "2026-02-28")],
              rates=[(None, "인정보험료", 0.01, "next_month")], by="verify", metric="converted_premium", publish=True)
    root = sync.root_id(con)
    oa.save(con, by="verify", org_id=root, name="검증 인정업적", metric="converted_premium", top_n=50, effective_from=YM)
    oa.save(con, by="verify", org_id=root, name="검증 건수", metric="policy_count", top_n=50, effective_from=YM)
    auth.seed_accounts(con, PW)
    return {"con": con, "tmp": tmp, "pipe": pipe, "roster": roster, "emp": emp, "emp_rows": emp_rows, "ctr": ctr, "ctr_rows": ctr_rows,
            "first": first}


@pytest.fixture(scope="module")
def env():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="s1verify-"))
    old = {k: os.environ.get(k) for k in ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR")}
    os.environ.update(APP_DB=str(tmp / "real.db"), CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"))
    e = _build(tmp)
    from app.web.main import app
    e["app"] = app
    yield e
    e["con"].close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def login(env, login_id):
    os.environ["APP_DB"] = str(env["tmp"] / "real.db")         # 다른 시험 모듈이 바꿔 놓았을 수 있다
    c = Client(env["app"], follow_redirects=False)
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, r.status_code
    return c


def _perf(con):
    run = db.latest_run(con, "performance", YM)
    return {r["agent_id"]: dict(r) for r in con.execute(
        "SELECT * FROM agent_performance_summary WHERE run_id=? AND period_type='month' AND period_key=?", (run, YM))}


def _ranking(con, metric):
    run = db.latest_run(con, "ranking", YM)
    return {r["agent_id"]: r["value"] for r in con.execute(
        "SELECT agent_id, value FROM ranking_snapshot WHERE run_id=? AND period_type='month' AND period_key=? AND metric=? AND scope='all'",
        (run, YM, metric))}


def _rollup(con, org_id):
    return con.execute("""SELECT converted_premium, net_count, agent_count FROM v_org_performance_rollup
                          WHERE as_of_ym=? AND period_type='month' AND period_key=? AND org_id=?""", (YM, YM, org_id)).fetchone()


def _team_rows(html):
    """팀원 현황 표에서 {사번: (순건수, 인정보험료)}."""
    out = {}
    for tr in html.split("<tr>")[2:]:
        m = re.search(r'<td class="hide-sm">([^<]+)</td>.*?<td class="num">([^<]*)</td>\s*<td class="num"><span class="minibar[^>]*><i[^>]*></i></span>([^<]*)</td>',
                      tr, re.S)
        if m:
            out[m.group(1)] = (_num(m.group(2)), _num(m.group(3)))
    return out


def _codes(con):
    return {r["agent_id"]: r["internal_code"] for r in con.execute("SELECT agent_id, internal_code FROM agent")}


# ------------------------------------------------------------------ 1. 실제 양식 흐름
def test_files_go_through_connect_pipeline(env):
    con = env["con"]
    emp_run, ctr_run = env["first"]
    assert emp_run.ok and ctr_run.ok
    assert [r.status for r in emp_run.results] == [LOADED] and [r.status for r in ctr_run.results] == [LOADED]
    by_level = dict(con.execute("SELECT level, COUNT(*) FROM org_unit GROUP BY level").fetchall())
    assert set(by_level) == {"ga", "hq", "branch", "center", "team"} and by_level["ga"] == 1
    assert by_level["hq"] == 4 and by_level["branch"] == 8
    # 같은 이름의 센터와 팀이 부모마다 따로 있다
    for name in (fe.SHARED_CENTER, fe.SHARED_TEAM):
        assert con.execute("SELECT COUNT(*) FROM org_unit WHERE name=?", (name,)).fetchone()[0] == 2
    # 사람은 경로 맨 아래 조직에 붙는다
    for a in env["roster"]:
        steps = [(lv, n) for lv, n in zip(("hq", "branch", "center", "team"), a["path"][1:]) if n]
        org = con.execute("""SELECT o.path_key FROM agent_org_history h JOIN agent g ON g.agent_id=h.agent_id JOIN org_unit o ON o.org_id=h.org_id
                             WHERE g.internal_code=? ORDER BY h.id DESC LIMIT 1""", (a["code"],)).fetchone()[0]
        assert org == sync.key_of(steps)
    assert con.execute("SELECT COUNT(*) FROM agent").fetchone()[0] == len(env["roster"])
    # 계약은 원본 행 수와 인정보험료 합계가 같다
    src = sum(int(str(r["인정보험료"]).replace(",", "")) for r in env["ctr_rows"])
    n, total = con.execute("SELECT COUNT(*), SUM(converted_premium) FROM contract").fetchone()
    assert n == len(env["ctr_rows"]) and total == src
    assert con.execute("SELECT COUNT(*) FROM contract WHERE agent_id IS NULL").fetchone()[0] == 1        # 마스터에 없는 사번 한 건
    assert dict(con.execute("SELECT data_type, received_via FROM import_batch").fetchall()) == {"employee": "upload", "org_contract": "upload"}


def test_same_files_again_are_skipped(env):
    con = env["con"]
    tables = ("org_unit", "agent", "agent_org_history", "org_manager", "import_batch", "contract", "contract_status_history", "raw_row")
    before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    for kind, path in (("employee", env["emp"]), ("contract", env["ctr"])):
        run = env["pipe"].run(UploadSource.from_path(path, kind=kind, period_ym=YM))
        assert [r.status for r in run.results] == [DUPLICATE], kind
    assert {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables} == before


# ------------------------------------------------------------------ 2. 같은 사람의 같은 달 숫자
def test_each_agent_same_number_on_every_screen(env):
    """설계사 본인으로 들어가 홈, 달성 현황, 조직 부문 시상의 숫자를 업적 집계와 랭킹과 대조한다."""
    con = env["con"]
    perf, rk_rp, rk_cnt = _perf(con), _ranking(con, "recognized_premium"), _ranking(con, "net_count")
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    codes = _codes(con)
    active = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' AND job_type='fc'")]
    picked = [a for a in active if a not in heads][:12]
    assert any(perf[a]["withdrawn_count"] for a in active) or any(perf[a]["converted_premium"] for a in picked)
    for a in picked:
        p = perf[a]
        assert rk_rp[a] == p["converted_premium"] and rk_cnt[a] == p["net_count"]
        c = login(env, codes[a])
        home = c.get("/").text
        assert _num(re.search(r'이번 달 인정보험료</div><div class="big">([^<]*)<', home).group(1)) == p["converted_premium"]
        assert _num(re.search(r'<div class="k">순건수</div><div class="mid">([^<]*)<', home).group(1)) == p["net_count"]
        prog = c.get("/promo/progress").text
        assert _num(re.search(r"대상 실적 ([^<원]*)원", prog).group(1)) == p["converted_premium"]
        award = c.get("/promo/org-awards").text
        mine = re.findall(r"내 순위 (\d+)위 · ([^<원건]*)(원|건)", award)
        got = {unit: _num(v) for _, v, unit in mine}
        if p["converted_premium"] > 0:
            assert got["원"] == p["converted_premium"]
        if p["net_count"] > 0:
            assert got["건"] == p["net_count"]


def test_team_screen_and_org_rollup_agree(env):
    """메인 관리자의 팀원 현황과 홈, 본부와 지점의 조직 집계, 엔진의 org_rollup 이 같다."""
    con = env["con"]
    perf = _perf(con)
    codes = _codes(con)
    c = login(env, "admin")
    rows = _team_rows(c.get("/team/members").text)
    active = {codes[r[0]] for r in con.execute("SELECT agent_id FROM agent WHERE status='active'")}
    assert set(rows) == active
    for aid, code in codes.items():
        if code in rows:
            assert rows[code] == (perf[aid]["net_count"], perf[aid]["converted_premium"])
    code_to_id = {v: k for k, v in codes.items()}
    for org_id, level in con.execute("SELECT org_id, level FROM org_unit WHERE level IN ('ga','hq','branch','center')").fetchall():
        members = {codes[a] for a in orgs.members(con, org_id, YM)}
        hand = sum(rows[m][1] for m in members)
        v = _rollup(con, org_id)
        assert v["converted_premium"] == hand == performance.org_rollup(con, org_id, YM)["converted_premium"], level
        assert v["net_count"] == sum(rows[m][0] for m in members)
        assert v["agent_count"] == len(members) == len({m for m in members if code_to_id[m] in perf})
    home = c.get("/").text
    m = re.search(r'<div class="k">인정보험료</div><div class="big">([^<]*)<span class="unit">원</span></div><div class="cap">순건수 ([^건]*)건', home)
    ga = _rollup(con, sync.root_id(con))
    assert (_num(m.group(1)), _num(m.group(2))) == (ga["converted_premium"], ga["net_count"])
    # GA 합계 + 설계사가 빈 계약 + 소속이 없는 사람 = 원장 전체 (업적 규칙으로)
    orphan = sum(p["converted_premium"] for a, p in perf.items() if p["org_id"] is None)
    led = sum(p["converted_premium"] for p in perf.values()) + performance.unassigned(con, YM, YM)["converted_premium"]
    assert ga["converted_premium"] + orphan + performance.unassigned(con, YM, YM)["converted_premium"] == led


def test_manager_sees_only_own_branch_and_totals_match(env):
    con = env["con"]
    codes = _codes(con)
    perf = _perf(con)
    head = con.execute("""SELECT m.agent_id, m.org_id FROM org_manager m JOIN org_unit o ON o.org_id=m.org_id
                          WHERE o.level='branch' ORDER BY m.agent_id LIMIT 1""").fetchone()
    c = login(env, codes[head["agent_id"]])
    rows = _team_rows(c.get("/team/members").text)
    inside = {codes[a] for a in orgs.members(con, head["org_id"], YM)}
    assert set(rows) == inside | {codes[head["agent_id"]]}
    outside = set(codes.values()) - set(rows)
    tree = c.get("/org/tree").text
    award = c.get("/promo/org-awards").text
    prog = c.get("/promo/progress").text
    for code in outside:
        assert f">{code}<" not in tree and f">{code}<" not in award and f">{code}<" not in prog
    v = _rollup(con, head["org_id"])
    assert v["converted_premium"] == sum(r[1] for code, r in rows.items() if code in inside)
    home = c.get("/").text
    m = re.search(r'<div class="k">인정보험료</div><div class="big">([^<]*)<span class="unit">원</span>', home)
    assert _num(m.group(1)) == sum(perf[a]["converted_premium"] for a in orgs.members(con, head["org_id"], YM))
    # 범위 밖 주소
    for path in ("/org/heads", "/org/accounts", "/org/audit", "/data/staff", "/promo/publish", "/promo/tiers"):
        assert c.get(path).status_code == 403, path


def test_agent_cannot_open_manager_or_admin_screens(env):
    con = env["con"]
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    aid, code = next((a, c) for a, c in _codes(con).items()
                     if a not in heads and con.execute("SELECT status FROM agent WHERE agent_id=?", (a,)).fetchone()[0] == "active")
    c = login(env, code)
    for path in ("/team/members", "/org/tree", "/org/heads", "/org/accounts", "/org/audit", "/data/staff", "/promo/publish", "/promo/tiers"):
        assert c.get(path).status_code == 403, path
    # CSRF 토큰 없는 POST
    raw = TestClient(env["app"], follow_redirects=False)
    assert raw.post("/login", data={"login_id": code, "password": PW}).status_code == 403
    assert c.post("/me", data={"old": PW, "new": "x-new-pass-1", "new2": "x-new-pass-1", "csrf_token": "wrong"}).status_code == 403


def test_login_survives_browser_favicon_request(env):
    """크롬이 로그인 화면을 열며 부르는 /favicon.ico 때문에 로그인 폼의 CSRF 토큰이 지워지던 문제의 재현."""
    os.environ["APP_DB"] = str(env["tmp"] / "real.db")
    c = TestClient(env["app"], follow_redirects=False)
    token = TOKEN.search(c.get("/login").text).group(1)
    c.get("/favicon.ico")                                                    # 크롬과 사파리가 페이지를 열면 따로 부른다
    assert c.post("/login", data={"login_id": "admin", "password": PW, "csrf_token": token}).status_code == 303


def test_unknown_path_before_login_keeps_csrf_but_not_login(env):
    """로그인 전 아무 주소나 열어도 로그인 폼의 토큰은 남는다. 로그아웃 뒤에는 화면이 열리지 않고 범위 밖은 여전히 403 이다."""
    os.environ["APP_DB"] = str(env["tmp"] / "real.db")
    c = TestClient(env["app"], follow_redirects=False)
    token = TOKEN.search(c.get("/login").text).group(1)
    for path in ("/apple-touch-icon.png", "/robots.txt", "/team/members"):     # 마지막 라우트와 guard 를 거치는 주소
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert c.post("/login", data={"login_id": "admin", "password": PW, "csrf_token": token}).status_code == 303
    assert c.get("/org/tree").status_code == 200
    assert c.get("/logout").status_code == 303
    r = c.get("/org/tree")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    con = env["con"]
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    code = next(c for a, c in _codes(con).items()
                if a not in heads and con.execute("SELECT status FROM agent WHERE agent_id=?", (a,)).fetchone()[0] == "active")
    assert login(env, code).get("/org/tree").status_code == 403


# ------------------------------------------------------------------ 3. 민감 정보
def test_no_sensitive_employee_values_in_db(env):
    con = env["con"]
    dump = []
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        dump += ["|".join(str(v) for v in tuple(r)) for r in con.execute(f"SELECT * FROM {t}")]
    text = "\n".join(dump)
    for h in ("주민번호", "보험업계고유번호", "계좌번호", "소득(전년)", "이행보증보험금액"):
        vals = {str(r[h]) for r in env["emp_rows"] if r.get(h)}
        assert vals and not any(v in text for v in vals), h
        assert not any(v.replace(",", "") in text for v in vals if len(v.replace(",", "")) >= 6), h
    assert con.execute("SELECT COUNT(*) FROM raw_row r JOIN import_batch b ON b.batch_id=r.batch_id WHERE b.data_type='employee'").fetchone()[0] == 0


def test_card_link_has_no_contact_column(env):
    con = env["con"]
    from app.connect import tables
    tables.ensure(con)
    cols = [r[1] for r in con.execute("PRAGMA table_info(card_link)")]
    assert cols
    assert not [c for c in cols if re.search(r"phone|mobile|tel|email|contact|name", c, re.I)], cols


# ------------------------------------------------------------------ 재현: 찾은 문제
@pytest.mark.xfail(strict=True, reason="그 달 중간에 퇴사한 설계사는 조직 집계(v_org_performance_rollup, org_rollup)에는 들고 "
                                       "팀원 현황, 홈, 랭킹, 조직 부문 시상에서는 빠진다. 본부 합계가 화면의 설계사 합계와 달라진다")
def test_mid_month_leaver_counts_the_same_on_screen_and_rollup(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "real.db"))
    e = _build(tmp_path)
    con = e["con"]
    perf = _perf(con)
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    codes = _codes(con)
    leaver = next(a for a, p in perf.items() if p["converted_premium"] > 0 and a not in heads and p["org_id"] is not None)
    rows = [dict(r) for r in e["emp_rows"]]
    for r in rows:
        if r["사원번호"] == codes[leaver]:
            r["퇴사일자"] = "20260215"
    path = fe.write_xlsx(tmp_path / "in" / "사원현황_퇴사.xlsx", fe.EMPLOYEE_HEADERS, rows, sheet="조회")
    assert e["pipe"].run(UploadSource.from_path(path, kind="employee", period_ym=YM)).ok
    performance.run(con, YM)
    ranking.run(con, YM)
    hq = next(o for o in orgs.ancestors(con, performance.org_at(con, leaver, YM))
              if con.execute("SELECT level FROM org_unit WHERE org_id=?", (o,)).fetchone()[0] == "hq")
    from app.web.main import app
    c = Client(app, follow_redirects=False)
    assert c.post("/login", data={"login_id": "admin", "password": PW}).status_code == 303
    screen = _team_rows(c.get("/team/members").text)
    under = {r["internal_code"] for r in con.execute(
        f"""SELECT DISTINCT a.internal_code FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id
            WHERE h.org_id IN ({','.join('?' * len(orgs.descendants(con, hq)))}) AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)""",
        [*orgs.descendants(con, hq), YM, YM])}
    on_screen = sum(v[1] for code, v in screen.items() if code in under)
    con.close()
    assert _rollup_of(tmp_path, hq) == on_screen


def _rollup_of(tmp_path, org_id):
    con = db.connect(tmp_path / "real.db")
    try:
        return _rollup(con, org_id)["converted_premium"]
    finally:
        con.close()


def test_run_demo_keeps_connector_originals(tmp_path, monkeypatch):
    """run_demo 가 지우려는 폴더만 가로채 본다. 실제로 지우지 않는다."""
    import run_demo
    from app.connect import raw_store

    monkeypatch.delenv("CONNECT_DATA_DIR", raising=False)                      # 운영 기본값 그대로
    removed = []

    class Stop(Exception):
        pass

    def fake_rmtree(path, *a, **kw):
        removed.append(pathlib.Path(path).resolve())
        raise Stop()
    monkeypatch.setattr(run_demo.shutil, "rmtree", fake_rmtree)
    with pytest.raises(Stop):
        run_demo.main(tmp_path / "demo.db")
    keep = [(raw_store.default_data_dir() / "raw").resolve(),                  # 연결 어댑터가 원본을 보관하는 곳
            (db.ROOT / "data" / "raw" / "uploads").resolve()]                 # 사원현황 화면의 업로드 폴더 (APP_UPLOAD_DIR 기본값)
    for target in removed:
        for k in keep:
            assert not (k == target or target in k.parents), (target, k)
