"""S2 C 실적·랭킹의 웹 화면: 기간별 업적, 구분별 집계, 랭킹, 조직 비교, 명예의전당, 실적 요약, 시상 진척.

- 세 역할의 접근은 config/menu.yaml 을 따른다. 범위 밖 조직을 고르면 403 이다.
- 같은 사람의 같은 달 숫자는 홈, 팀원 현황, 기간별 업적, 랭킹, 실적 요약에서 같다.
- 그 달 중간에 퇴사한 설계사는 랭킹에 없고 기간별 업적의 합계에는 있다 (D-42).
- 로그인한 화면은 Cache-Control: no-store 다. 로그인 화면과 정적 파일은 아니다.
- D-05, D-17 의 화면 훑기(tests/test_s1_decisions.py)가 새 화면을 연다. 새 화면의 다른 보기도 D-17 로 훑는다.

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 는 건드리지 않는다.
사람 이름과 금액은 실패 메시지에 넣지 않는다. 화면 주소와 칸 이름만 넣는다.
"""
import os
import pathlib
import re
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import org_award as oa
from app.periods import ym_add
from app.web import auth, menu

PW = "perf-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
NEW = ["/perf/period", "/perf/category", "/perf/ranking", "/perf/org-compare", "/perf/hall", "/my/summary", "/my/awards"]
NOT_YET = []          # 유지율 두 화면은 S2 마지막에 만들어 tests/test_web_s2_last.py 가 본다. 계약 원장은 tests/test_web_s2_finish.py 가 본다


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리킨다. 다른 시험 모듈이 APP_DB 를 바꿔 두어도 섞이지 않는다."""

    def __init__(self, app, db_path):
        super().__init__(app, follow_redirects=False)
        self.db_path = str(db_path)

    def request(self, *a, **kw):
        os.environ["APP_DB"] = self.db_path
        return super().request(*a, **kw)


@pytest.fixture(scope="module")
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="webperf-"))
    old = {k: os.environ.get(k) for k in ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR")}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"))
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    ym = auth.current_ym(con)
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    mgr = con.execute("""SELECT a.agent_id, a.internal_code code, m.org_id FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                         JOIN org_unit o ON o.org_id=m.org_id WHERE o.level='branch' AND m.effective_to IS NULL ORDER BY o.org_id LIMIT 1""").fetchone()
    mctx = auth.context(con, con.execute("SELECT user_id FROM user_account WHERE agent_id=?", (mgr["agent_id"],)).fetchone()[0])
    # 관리자 범위 안의 설계사 가운데 이번 달 인정보험료가 있는 둘. 앞 사람은 숫자 맞추기, 뒤 사람은 퇴사자다
    rows = con.execute(f"""SELECT s.agent_id FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                           JOIN agent a ON a.agent_id=s.agent_id
                           WHERE s.period_type='month' AND s.period_key=? AND s.converted_premium>0 AND a.job_type='fc' AND a.status='active'
                             AND s.agent_id IN ({','.join('?' * len(mctx['agent_ids']))})
                           ORDER BY s.converted_premium DESC, s.agent_id""", (ym, ym, *sorted(mctx["agent_ids"]))).fetchall()
    picks = [r[0] for r in rows if r[0] not in heads]
    assert len(picks) >= 2, "관리자 범위에 실적이 있는 설계사가 둘 이상 있어야 한다"
    who, leaver = picks[0], picks[1]
    day = f"{ym[:4]}-{ym[4:]}-15"
    con.execute("UPDATE agent SET status='terminated', termination_date=? WHERE agent_id=?", (day, leaver))      # 그 달 중간 퇴사
    con.execute("UPDATE agent_org_history SET effective_to=? WHERE agent_id=? AND effective_to IS NULL", (ym, leaver))
    con.commit()
    code = lambda a: con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (a,)).fetchone()[0]
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "app": app, "con": con, "ym": ym, "mgr": dict(mgr), "who": who, "who_code": code(who), "leaver": leaver}
    con.close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


def login(env, login_id):
    c = Client(env["app"], env["db"])
    tok = TOKEN.search(c.get("/login").text).group(1)
    r = c.post("/login", data={"login_id": login_id, "password": PW, "csrf_token": tok})
    assert r.status_code == 303, (login_id, r.status_code)
    return c


def ctx_of(env, login_id):
    con = env["con"]
    return auth.context(con, con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0])


def text(html):
    return re.sub(r"<[^>]+>", "", html).strip()


def row(html, attr, value):
    """data-agent 나 data-col="code" 로 찾은 표의 한 줄."""
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        if f'{attr}="{value}"' in tr or (attr == "code" and f'data-col="code">{value}<' in tr):
            return tr
    return None


def cell(fragment, col):
    m = re.search(rf'data-col="{col}"[^>]*>(.*?)</(?:td|div)>\s*(?:<|$)', fragment, re.S)
    return re.sub(r"[^\d,\-.]", "", text(m.group(1))) if m else None


# ---------------------------------------------------------------- 접근
@pytest.mark.parametrize("role", ["main", "manager", "agent"])
def test_three_roles_open_what_the_menu_allows(env, role):
    login_id = {"main": "admin", "manager": env["mgr"]["code"], "agent": env["who_code"]}[role]
    c = login(env, login_id)
    for path in NEW:
        item = menu.find(path)
        assert item and item["status"] == "built", path
        r = c.get(path)
        assert r.status_code == (200 if role in item["roles"] else 403), (role, path, r.status_code)
    # 조직 비교는 메인 관리자와 관리자만, 실적 요약과 시상 진척은 관리자와 설계사만 본다
    assert ("main" in menu.find("/perf/org-compare")["roles"]) and ("agent" not in menu.find("/perf/org-compare")["roles"])
    assert set(menu.find("/my/summary")["roles"]) == {"manager", "agent"} == set(menu.find("/my/awards")["roles"])


def test_screens_not_built_this_time_stay_unbuilt(env):
    for path in NOT_YET:
        assert menu.find(path)["status"] != "built", path


def test_filters_open_for_each_role(env):
    """거르기 단추로 가는 보기들이 모두 열린다."""
    views = ["/perf/period?pt=quarter", "/perf/period?pt=half&sector=life", "/perf/period?pt=year&sector=nonlife",
             "/perf/category?by=sector", "/perf/category?pt=year&sector=life", "/perf/ranking?metric=net_count&sector=nonlife",
             "/perf/ranking?metric=monthly_premium&pt=quarter", "/perf/hall?metric=net_count", "/perf/period?pt=nonsense&sector=bad&ym=abc"]
    for login_id in ("admin", env["mgr"]["code"], env["who_code"]):
        c = login(env, login_id)
        for v in views:
            assert c.get(v).status_code == 200, (login_id, v)
    c = login(env, env["mgr"]["code"])
    for v in ("/perf/org-compare?level=team", "/perf/org-compare?pt=year", "/my/summary?pt=year", "/my/summary?pt=quarter"):
        assert c.get(v).status_code == 200, v


def test_out_of_scope_org_is_403(env):
    con, mgr = env["con"], env["mgr"]
    mine = mgr["org_id"]
    other = con.execute("SELECT org_id FROM org_unit WHERE level='branch' AND org_id<>? ORDER BY org_id LIMIT 1", (mine,)).fetchone()[0]
    parent = con.execute("SELECT parent_id FROM org_unit WHERE org_id=?", (mine,)).fetchone()[0]
    c = login(env, mgr["code"])
    for path in (f"/perf/period?org={other}", f"/perf/category?org={other}", f"/perf/ranking?org={other}",
                 f"/perf/org-compare?parent={other}", f"/perf/org-compare?parent={parent}", f"/perf/period?org={parent}"):
        assert c.get(path).status_code == 403, ("manager", path)
    for path in (f"/perf/period?org={mine}", f"/perf/category?org={mine}", f"/perf/ranking?org={mine}", f"/perf/ranking?org={parent}",
                 f"/perf/org-compare?parent={mine}"):
        assert c.get(path).status_code == 200, ("manager", path)
    a = login(env, env["who_code"])
    for path in (f"/perf/period?org={mine}", f"/perf/category?org={mine}", f"/perf/ranking?org={other}"):
        assert a.get(path).status_code == 403, ("agent", path)
    assert a.get(f"/perf/ranking?org={mine}").status_code == 200
    n = con.execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied'").fetchone()[0]
    assert a.get("/perf/org-compare").status_code == 403
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied'").fetchone()[0] == n + 1


# ---------------------------------------------------------------- 같은 숫자
def test_same_person_same_month_same_number_everywhere(env):
    who, code, mgr = env["who"], env["who_code"], env["mgr"]
    me, boss, admin = login(env, code), login(env, mgr["code"]), login(env, "admin")
    seen = {}
    m = re.search(r'이번 달 인정보험료</div><div class="big">([\d,\-]+)<', me.get("/").text)
    seen["홈"] = m.group(1) if m else None
    team = boss.get("/team/members").text
    seen["팀원 현황"] = cell(row(team, "code", code) or "", "premium")
    seen["기간별 업적 본인"] = cell(me.get("/perf/period").text, "premium")
    seen["기간별 업적 관리자"] = cell(row(boss.get("/perf/period").text, "data-agent", who) or "", "premium")
    seen["기간별 업적 메인"] = cell(row(admin.get("/perf/period").text, "data-agent", who) or "", "premium")
    seen["랭킹 본인"] = cell(row(me.get("/perf/ranking").text, "data-agent", who) or "", "value")
    seen["랭킹 관리자"] = cell(row(boss.get(f"/perf/ranking?org={mgr['org_id']}").text, "data-agent", who) or "", "value")
    seen["실적 요약"] = cell(me.get("/my/summary").text, "premium")
    missing = sorted(k for k, v in seen.items() if not v)
    assert not missing, missing
    assert len(set(seen.values())) == 1, sorted(seen)                      # 값은 내지 않고 화면 이름만 낸다
    nets = {"팀원 현황": cell(row(team, "code", code) or "", "net"), "기간별 업적 본인": cell(me.get("/perf/period").text, "net"),
            "실적 요약": cell(me.get("/my/summary").text, "net")}
    assert all(nets.values()) and len(set(nets.values())) == 1, sorted(nets)


def test_category_total_matches_period_total(env):
    c = login(env, env["mgr"]["code"])
    for pt in ("month", "year"):
        period = cell(c.get(f"/perf/period?pt={pt}").text, "premium")
        cat = text(re.findall(r"<tr>.*?</tr>", c.get(f"/perf/category?pt={pt}").text, re.S)[-1])
        assert period and period in cat, pt


# ---------------------------------------------------------------- 퇴사자 (D-42)
def test_leaver_is_out_of_ranking_but_in_period_totals(env):
    leaver, mgr = env["leaver"], env["mgr"]
    admin, boss = login(env, "admin"), login(env, mgr["code"])
    for c, label in ((admin, "main"), (boss, "manager")):
        for path in ("/perf/ranking", f"/perf/ranking?org={mgr['org_id']}", "/perf/ranking?metric=net_count", "/perf/ranking?pt=year"):
            html = c.get(path).text
            assert f'data-agent="{leaver}"' not in html, (label, path)
            assert "퇴사한 설계사는 순위에서 빠집니다" in html, (label, path)
        period = c.get("/perf/period").text
        r = row(period, "data-agent", leaver)
        assert r and "퇴사" in r, (label, "/perf/period")
    con = env["con"]
    from app.engine import results as rs
    ids = [x[0] for x in con.execute("SELECT agent_id FROM agent")]
    total = rs.period_summary(con, ids, "month", env["ym"])
    assert total["left"] >= 1 and any(x["agent_id"] == leaver and x["left"] for x in total["rows"])


# ---------------------------------------------------------------- 캐시
def test_logged_in_pages_are_no_store(env):
    c = login(env, env["mgr"]["code"])
    for path in ["/", "/team/members", "/me", "/promo/list", *[p for p in NEW if "manager" in menu.find(p)["roles"]]]:
        r = c.get(path)
        assert r.status_code == 200 and r.headers.get("cache-control") == "no-store", path
    anon = Client(env["app"], env["db"])
    assert "no-store" not in (anon.get("/login").headers.get("cache-control") or "")
    assert "no-store" not in (anon.get("/static/app.css").headers.get("cache-control") or "")
    assert anon.get("/perf/period").status_code == 303                                     # 로그인 전에는 화면을 주지 않는다


# ---------------------------------------------------------------- D-05, D-17
def test_s1_decision_sweep_opens_the_new_screens(env):
    """tests/test_s1_decisions.py 는 메뉴에서 그 역할에게 열린 화면을 모두 연다. 새 화면이 그 안에 드는지 본다."""
    from tests import test_s1_decisions as s1
    for login_id in ("admin", env["mgr"]["code"], env["who_code"]):
        ctx = ctx_of(env, login_id)
        paths = s1._screens(env["con"], ctx)
        want = [p for p in NEW if ctx["role"] in menu.find(p)["roles"]]
        assert want and all(p in paths for p in want), (ctx["role"], [p for p in want if p not in paths])


def _perf_numbers(con, ctx, ym) -> set:
    """관리자 범위의 실적 집계로 화면에 나와도 되는 숫자. 구분별, 업권별, 달마다, 조직별 합은 _public 에 없어서 따로 모은다.
    샘플 금액은 둥근 수라 이런 실적 합이 우연히 누군가의 시상 금액과 같을 수 있다."""
    from app.engine import results as rs
    from tests import test_s1_decisions as s1
    out, ids = set(), sorted(ctx["agent_ids"])
    rows = []
    for pt in rs.PERIOD_KO:
        for sector in (None, "life", "nonlife"):
            s = rs.period_summary(con, ids, pt, ym, sector=sector)
            rows += [s["total"], *s["months"], *s["rows"]]
            rows += rs.breakdown(con, ids, pt, ym, by="category", sector=sector)["rows"]
            for lv in ("hq", "branch", "center", "team"):
                rows += rs.org_compare(con, ym, org_ids=ctx["org_ids"], level=lv, period_type=pt, sector=sector)["rows"]
        rows += rs.breakdown(con, ids, pt, ym, by="sector")["rows"]
    for r in rows:
        for k in rs.KEYS:
            out |= s1._ints(r.get(k))
    return out


def test_d17_manager_other_views_show_no_member_amounts(env):
    """기본 보기 밖의 보기(분기, 연, 업권, 조직 고르기, 조직 비교의 단계)도 관리자에게 팀원의 수수료와 시상 금액을 보이지 않는다.
    방법은 tests/test_s1_decisions.py 의 D-17 시험과 같다. 범위 안 실적 집계와 같은 값은 보지 않는다."""
    from tests import test_s1_decisions as s1
    con, ym, mgr = env["con"], env["ym"], env["mgr"]
    ctx = ctx_of(env, mgr["code"])
    shown = _perf_numbers(con, ctx, ym)
    checkable = {v: f for v, f in s1._checkable(con, ctx, ctx["agent_ids"] - {ctx["agent_id"]}, ym).items() if v not in shown}
    assert len(checkable) >= 5
    c = login(env, mgr["code"])
    views = [f"{p}?pt={pt}" for p in ("/perf/period", "/perf/category", "/perf/org-compare") for pt in ("quarter", "half", "year")]
    views += ["/perf/category?by=sector", f"/perf/period?org={mgr['org_id']}", "/perf/org-compare?level=team", "/perf/org-compare?level=center",
              "/perf/ranking?pt=year", "/perf/hall?metric=net_count", "/my/summary?pt=year"]
    pages = {v: c.get(v).text for v in views}
    assert not s1._leaks(pages, checkable, "manager")


def test_d45_hall_shows_award_content_to_everyone(env):
    """명예의전당의 확정한 조직 부문 수상. D-45 제안대로(가정) 순위별 시상 내용은 공개 상품표라 세 역할 모두 본다.
    규칙과 합계 금지는 tests/test_web_s2_finish.py 가 조직 부문 시상 화면과 함께 본다."""
    con, mgr, who = env["con"], env["mgr"], env["who"]
    prev = ym_add(env["ym"], -1)
    other = next(a for a in sorted(ctx_of(env, mgr["code"])["agent_ids"]) if a not in (who, mgr["agent_id"], env["leaver"]))
    did = oa.save(con, by="admin", org_id=mgr["org_id"], name="시험 수상 부문", metric="converted_premium", top_n=2, effective_from=prev,
                  rewards=[(1, "시험시상가"), (2, "시험시상나")])
    for rank, agent, label in ((1, who, "시험시상가"), (2, other, "시험시상나")):
        con.execute("""INSERT INTO org_award_result(division_id, period_ym, rank_no, agent_id, value, reward_label, confirmed_by, confirmed_at)
                       VALUES(?,?,?,?,?,?,?,?)""", (did, prev, rank, agent, 1000 * (3 - rank), label, "admin", db.now()))
    con.commit()
    try:
        main = login(env, "admin").get("/perf/hall").text
        boss = login(env, mgr["code"]).get("/perf/hall").text
        me = login(env, env["who_code"]).get("/perf/hall").text
        assert "시험시상가" in main and "시험시상나" in main
        assert "시험 수상 부문" in boss and "시험시상가" in boss and "시험시상나" in boss
        assert "시험시상가" in me and "시험시상나" in me
    finally:
        con.execute("DELETE FROM org_award_result WHERE division_id=?", (did,))
        con.execute("DELETE FROM org_award_reward WHERE division_id=?", (did,))
        con.execute("DELETE FROM org_award_division WHERE division_id=?", (did,))
        con.commit()


def test_my_awards_amounts_are_marked_as_estimates(env):
    html = login(env, env["who_code"]).get("/my/awards").text
    assert "예상" in html and "적재 기준" in html
    for path in ("/perf/period", "/perf/category", "/perf/ranking", "/my/summary"):
        assert "적재 기준" in login(env, env["who_code"]).get(path).text, path
