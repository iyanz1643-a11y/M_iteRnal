"""S2 검증. 만든 에이전트와 따로 verifier 가 쓴 대조 시험이다. 2026-11-15.

보는 것
- 숫자 대조: 같은 조직과 같은 달의 인정보험료와 순건수가 홈, 기간별 업적, 구분별 집계, 조직 비교, 팀원 현황에서 같다.
  같은 사람의 숫자가 메인 관리자의 팀원 현황과 랭킹에서도 같다. 프로모션 달성 현황의 같은 지표도 같은지 본다.
- 교육 숫자 대조: 같은 사람과 같은 과정의 기한 상태가 팀 교육 진척도, 홈, 알림함, 일정에서 같다.
- 권한: S2 의 POST 주소가 CSRF 토큰 없이 거부된다. 범위 밖 조직과 과정을 직접 열면 막힌다. 퇴사자 규칙 D-42 가 S2 화면에서도 지켜진다.
- 첨부: 꼴을 속인 파일과 받지 않는 꼴을 막고, 내려받기 머리글이 맞다.

실패로 남긴 시험은 xfail(strict=True) 이다. 고쳐지면 통과로 바뀌어 strict 때문에 실패하므로 그때 표시를 뗀다.
자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 는 건드리지 않는다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다. 화면 주소, 칸 이름, 건수만 넣는다.
"""
import os
import pathlib
import re
import shutil
import tempfile
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import education as edu
from app.engine import promotion as pm
from app.web import auth
from app.web.routes import notify

PW = "s2-verify-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
PDF = b"%PDF-1.4\n% verify\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리킨다. post 는 화면의 CSRF 토큰을 넣는다. csrf=False 면 넣지 않는다."""

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


@pytest.fixture(scope="module")
def base():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="s2verify-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR", "CARD_SITE_URL", "CARD_ADMIN_KEY", "CARD_SITE_WRITE")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    for k in ("CARD_SITE_URL", "CARD_ADMIN_KEY", "CARD_SITE_WRITE"):          # 명함 사이트에는 붙지 않는다
        os.environ.pop(k, None)
    path = tmp / "base.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    con.close()
    from app.web.main import app
    yield {"tmp": tmp, "path": path, "app": app}
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def env(base, request):
    """시험마다 샘플 DB 를 복사해 쓴다. 고치는 시험이 다른 시험에 번지지 않게 한다."""
    path = base["tmp"] / f"{request.node.name[:60]}.db"
    shutil.copy(base["path"], path)
    con = db.connect(path)
    notify.clear_cache()
    ym = auth.current_ym(con)
    mgr = con.execute("""SELECT a.agent_id, a.internal_code code, m.org_id FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                         JOIN org_unit o ON o.org_id=m.org_id WHERE o.level='branch' AND m.effective_to IS NULL ORDER BY o.org_id LIMIT 1""").fetchone()
    mctx = auth.context(con, uid(con, mgr["code"]))
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    fcs = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' AND job_type='fc' ORDER BY agent_id")]
    inside = [a for a in fcs if a in mctx["agent_ids"] and a not in heads]
    outside = [a for a in fcs if a not in mctx["agent_ids"]]
    yield {"con": con, "path": path, "app": base["app"], "tmp": base["tmp"], "ym": ym, "mgr": dict(mgr), "mctx": mctx,
           "inside": inside, "outside": outside}
    notify.clear_cache()
    con.close()


def uid(con, login_id):
    return con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0]


def code_of(con, agent_id):
    return con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (agent_id,)).fetchone()[0]


def login(env, login_id):
    c = Client(env["app"], env["path"])
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, (login_id, r.status_code)
    return c


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def num(s):
    s = re.sub(r"[^\d.\-]", "", s or "")
    return float(s) if s not in ("", "-", ".") else None


def col(fragment, name):
    m = re.search(rf'data-col="{name}"[^>]*>(.*?)</(?:td|div)>', fragment, re.S)
    return num(text(m.group(1))) if m else None


def rows(html):
    return re.findall(r"<tr[^>]*>.*?</tr>", html, re.S)


# ---------------------------------------------------------------- 숫자 대조: 조직 합계
def test_org_total_same_on_home_period_category_compare_and_team(env):
    """지점장 범위의 이번 달 인정보험료와 순건수. 홈, 기간별 업적, 구분별 집계, 조직 비교(관리자와 메인 관리자), 팀원 현황 합."""
    mgr = env["mgr"]
    boss, admin = login(env, mgr["code"]), login(env, "admin")
    premium, net = {}, {}
    home = boss.get("/").text
    m = re.search(r'<div class="k">인정보험료</div><div class="big">([\d,\-]+)<span class="unit">원</span></div><div class="cap">순건수 ([\d.]+)건', home)
    assert m, "홈의 조직 인정보험료 카드"
    premium["홈"], net["홈"] = num(m.group(1)), num(m.group(2))
    period = boss.get("/perf/period").text
    premium["기간별 업적"], net["기간별 업적"] = col(period, "premium"), col(period, "net")
    team = boss.get("/team/members").text
    tr = [r for r in rows(team) if 'data-col="premium"' in r]
    premium["팀원 현황 합"] = sum(col(r, "premium") or 0 for r in tr)
    net["팀원 현황 합"] = sum(col(r, "net") or 0 for r in tr)
    cat = boss.get("/perf/category").text
    last = text(rows(cat)[-1])
    premium["구분별 집계 합계"] = premium["기간별 업적"] if f"{int(premium['기간별 업적']):,}" in last else -1
    org_name = env["con"].execute("SELECT name FROM org_unit WHERE org_id=?", (mgr["org_id"],)).fetchone()[0]
    for who, c in (("관리자", boss), ("메인 관리자", admin)):
        html = c.get("/perf/org-compare?level=branch").text
        hit = [r for r in rows(html) if f'<td class="strong">{org_name}' in r]
        assert len(hit) == 1, ("조직 비교", who)
        cells = [text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", hit[0], re.S)]
        premium[f"조직 비교 {who}"], net[f"조직 비교 {who}"] = num(cells[-1]), num(cells[-3])
    assert len(set(premium.values())) == 1, sorted(premium)                    # 값은 내지 않고 화면 이름만 낸다
    assert len(set(net.values())) == 1, sorted(net)


def test_person_same_on_main_team_members_and_main_ranking(env):
    """기존 시험(test_web_perf)이 보지 않는 메인 관리자의 팀원 현황과 전체 랭킹, 관리자의 시상 진척 화면까지 같은 사람의 같은 숫자."""
    con, who = env["con"], env["inside"][0]
    code = code_of(con, who)
    me, admin = login(env, code), login(env, "admin")
    seen = {"실적 요약": col(me.get("/my/summary").text, "premium"),
            "기간별 업적 본인": col(me.get("/perf/period").text, "premium")}
    team = admin.get("/team/members").text
    r = next((r for r in rows(team) if f">{code}<" in r or f"data-agent=\"{who}\"" in r), None)
    if r is None:                                     # 팀원 현황 줄에는 사번 칸이 없어 이름으로 찾는다
        name = con.execute("SELECT name FROM agent WHERE agent_id=?", (who,)).fetchone()[0]
        r = next((r for r in rows(team) if f'<td class="strong">{name}' in r), "")
    seen["팀원 현황 메인"] = col(r, "premium")
    rk = admin.get("/perf/ranking").text
    seen["랭킹 메인"] = col(next((r for r in rows(rk) if f'data-agent="{who}"' in r), ""), "value")
    assert all(v is not None for v in seen.values()), sorted(k for k, v in seen.items() if v is None)
    assert len(set(seen.values())) == 1, sorted(seen)


@pytest.mark.xfail(strict=True, reason=(
    "프로모션 달성 현황의 인정보험료와 건수는 계약일자에 든 계약 가운데 지금 성립 상태인 것을 센다(app/engine/promotion.py _values, COUNTED). "
    "업적 집계는 성립한 달에 더하고 철회한 달에 뺀다(app/engine/results.py). 같은 달 전 보험사 인정보험료 프로모션을 만들면 "
    "같은 사람의 같은 달 숫자가 기간별 업적과 다르다. 기준일은 D-08 이라 돈 계산 기준이다. calc-engine 과 대표님 결정이 필요하다"))
def test_promo_progress_same_metric_matches_period(env):
    con, mgr, ym = env["con"], env["mgr"], env["ym"]
    y, m = int(ym[:4]), int(ym[4:])
    first = date(y, m, 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    pid = pm.create(con, period_ym=ym, name="검증용 전 보험사 인정보험료", kind="travel", metric="converted_premium",
                    windows=[("한 달", first.isoformat(), last.isoformat())], tiers=[(1, "fixed", 1, "검증")], by="verifier", publish=True)
    boss = login(env, mgr["code"])
    promo = boss.get(f"/promo/progress?p={pid}").text
    period = boss.get("/perf/period").text
    names = {r["name"]: r["agent_id"] for r in con.execute("SELECT agent_id, name FROM agent")}
    differ = 0
    checked = 0
    for r in rows(period):
        if 'data-agent="' not in r:
            continue
        aid = int(re.search(r'data-agent="(\d+)"', r).group(1))
        if aid not in env["inside"]:
            continue
        name = next(n for n, a in names.items() if a == aid)
        pr = next((x for x in rows(promo) if f'<td class="strong">{name}<' in x), None)
        if pr is None:
            continue
        cells = [text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", pr, re.S)]
        value = num(cells[2])                          # 성명, 소속, 지표 칸
        checked += 1
        differ += (value or 0) != (col(r, "premium") or 0)
    assert checked >= 3, "대조한 사람 수"
    assert differ == 0, f"{checked}명 가운데 {differ}명이 다르다"


# ---------------------------------------------------------------- 교육 숫자 대조
def _course_due_soon_by_data_month(env):
    """자료 기준 달 말일에서 사흘 뒤가 기한인 필수 과정을 만든다. 오늘(11월 15일 이후)에는 이미 지난 기한이다."""
    con = env["con"]
    ref = pm.ref_date(env["ym"])
    today = date.today()
    due = ref + timedelta(days=3)
    assert ref < due < today, "이 시험은 오늘이 자료 기준 달 말일보다 나흘 이상 뒤일 때 뜻이 있다"
    cid = edu.save_course(con, by="verifier", code="VF-DUE", name="검증용 기한 과정", kind="compliance", required=True,
                          target_role="fc", deadline_rule="fixed", deadline_date=due.isoformat())
    return cid, ref, due


def test_education_due_state_same_on_team_home_notifications_calendar(env):
    cid, ref, due = _course_due_soon_by_data_month(env)
    boss = login(env, env["mgr"]["code"])
    team = text(boss.get(f"/team/education?course={cid}").text)
    assert "검증용 기한 과정" in team
    overdue_on_team = re.search(r"지남 (\d+) 건", team)
    soon_on_team = re.search(r"임박 (\d+) 건", team)
    notes = text(boss.get("/notifications").text)
    soon_note = re.search(r"검증용 기한 과정 기한 임박 팀원 (\d+)명", notes)
    cal = boss.get(f"/calendar?m={due.strftime('%Y-%m')}").text
    states = {
        "팀 교육 진척도": "overdue" if overdue_on_team and int(overdue_on_team.group(1)) > 0 and not (soon_on_team and int(soon_on_team.group(1))) else "soon",
        "알림함": "soon" if soon_note else "overdue",
        "일정": "soon" if re.search(r'class="[^"]*\bwarn\b[^"]*"[^>]*>[^<]*(?:<[^>]+>[^<]*){0,4}검증용 기한 과정 기한', cal) else "overdue",
    }
    assert len(set(states.values())) == 1, sorted(states.items())


def test_confirmed_completion_disappears_from_notifications(env):
    cid, ref, due = _course_due_soon_by_data_month(env)
    con, who = env["con"], env["inside"][0]
    edu.record_completion(con, agent_id=who, course_id=cid, by="verifier", agent_ids=[who], completed_on=date.today().isoformat())
    con.commit()
    boss = login(env, env["mgr"]["code"])
    team_rows = boss.get(f"/team/education?course={cid}").text
    assert f"?confirm={who}-{cid}" not in team_rows                  # 팀 교육 진척도에서는 수료로 빠졌다
    n_team_not_done = len(re.findall(rf"\?confirm=\d+-{cid}\b", team_rows))
    m = re.search(r"검증용 기한 과정 기한 임박 팀원 (\d+)명", text(boss.get("/notifications").text))
    n_note = int(m.group(1)) if m else 0
    assert n_note == n_team_not_done or (n_note == 0), ("알림함 미수료 수", n_note, "팀 교육 진척도 미수료 수", n_team_not_done)


# ---------------------------------------------------------------- 권한
S2_POSTS = (("/board", {"action": "save", "category": "notice", "title": "토큰 없는 글"}),
            ("/edu/courses", {"action": "save", "code": "NO-TOKEN", "name": "토큰 없는 과정", "kind": "compliance"}),
            ("/edu/sessions", {"action": "save", "course_id": "1", "date_from": "2026-12-01"}),
            ("/edu/completions", {"action": "card_pick", "cid": "1"}),
            ("/team/education", {"action": "confirm", "agent_id": "1", "course_id": "1", "confirm": "1"}),
            ("/notifications", {"action": "read_all"}),
            ("/sales/card-approve", {"action": "approve", "slug": "x", "confirm": "1"}))


def test_s2_posts_without_csrf_are_refused_for_admin(env):
    con = env["con"]
    admin = login(env, "admin")
    before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("edu_course", "edu_session", "edu_completion", "audit_log")}
    for path, data in S2_POSTS:
        assert admin.post(path, data=data, csrf=False).status_code == 403, path
        assert admin.post(path, data={**data, "csrf_token": "wrong"}, csrf=False).status_code == 403, path
    after = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='board_post'").fetchone() or \
        con.execute("SELECT COUNT(*) FROM board_post").fetchone()[0] == 0


def test_main_only_writes_are_refused_for_manager_and_agent(env):
    """docs/09 2.4 와 D-46: 과정, 차수, 명부, 수료 코드, 공지는 S2 에 메인 관리자만 쓴다. 관리자와 설계사는 토큰이 있어도 403."""
    con = env["con"]
    for who in (env["mgr"]["code"], code_of(con, env["inside"][0])):
        c = login(env, who)
        for path, data in S2_POSTS[:4]:
            r = c.post(path, data=data)
            assert r.status_code == 403, (path, "manager" if who == env["mgr"]["code"] else "agent")
    assert con.execute("SELECT COUNT(*) FROM edu_course WHERE code='NO-TOKEN'").fetchone()[0] == 0


def test_out_of_scope_direct_urls_are_blocked(env):
    con, mgr = env["con"], env["mgr"]
    other_org = con.execute(f"""SELECT org_id FROM org_unit WHERE level='branch' AND org_id NOT IN ({','.join('?' * len(env['mctx']['org_ids']))})
                                ORDER BY org_id LIMIT 1""", tuple(env["mctx"]["org_ids"])).fetchone()[0]
    boss = login(env, mgr["code"])
    for path in (f"/perf/period?org={other_org}", f"/perf/ranking?org={other_org}", f"/perf/org-compare?parent={other_org}",
                 f"/perf/category?org={other_org}"):
        r = boss.get(path)
        assert r.status_code == 403 or (r.status_code == 200 and f'value="{other_org}" selected' not in r.text), path
    # 다른 지점이 연 과정은 관리자와 설계사의 과정 목록과 차수 화면에 없다
    cid = edu.save_course(con, by="verifier", code="VF-OTHER", name="검증용 다른 지점 과정", kind="product", required=True,
                          owner_org_id=other_org, deadline_rule="none")
    edu.save_session(con, by="verifier", course_id=cid, date_from="2026-12-10", place="검증 교육장")
    con.commit()
    agent = login(env, code_of(con, env["inside"][0]))
    for c, label in ((boss, "manager"), (agent, "agent")):
        assert "검증용 다른 지점 과정" not in c.get("/edu/courses").text, label
        assert "검증용 다른 지점 과정" not in c.get(f"/edu/sessions?course={cid}").text, label
        assert "검증용 다른 지점 과정" not in c.get("/calendar?m=2026-12").text, label
    # 범위 밖 설계사의 수료 확인 화면은 열리지 않는다
    out = env["outside"][0]
    r = boss.get(f"/team/education?confirm={out}-{cid}")
    assert r.status_code in (403, 404) or "이 수료를 확인 처리할까요?" not in r.text


def test_board_notice_out_of_target_post_and_file_are_404(env):
    con = env["con"]
    other_org = con.execute(f"""SELECT org_id FROM org_unit WHERE level='hq' AND org_id NOT IN ({','.join('?' * len(env['mctx']['org_ids']))})
                                AND org_id NOT IN (SELECT parent_id FROM org_unit WHERE org_id=?)
                                ORDER BY org_id LIMIT 1""", (*env["mctx"]["org_ids"], env["mgr"]["org_id"])).fetchone()[0]
    admin = login(env, "admin")
    r = admin.post("/board", data={"action": "save", "category": "notice", "title": "검증용 다른 본부 공지", "target_org_id": str(other_org)},
                   files=[("files", ("안내.pdf", PDF, "application/pdf"))])
    assert r.status_code == 303
    pid = con.execute("SELECT post_id FROM board_post WHERE title='검증용 다른 본부 공지'").fetchone()[0]
    fid = con.execute("SELECT file_id FROM board_file WHERE post_id=?", (pid,)).fetchone()[0]
    dl = admin.get(f"/board?file={fid}")
    assert dl.status_code == 200 and dl.content == PDF
    assert dl.headers["content-disposition"].startswith("attachment;")
    assert dl.headers["x-content-type-options"] == "nosniff" and dl.headers["cache-control"] == "no-store"
    for who in (env["mgr"]["code"], code_of(con, env["inside"][0])):
        c = login(env, who)
        assert "검증용 다른 본부 공지" not in c.get("/board").text
        assert c.get(f"/board?post={pid}").status_code == 404
        assert c.get(f"/board?file={fid}").status_code == 404
        assert "검증용 다른 본부 공지" not in c.get("/notifications").text


@pytest.mark.parametrize("name,data", [
    ("page.html", b"<html><script>alert(1)</script></html>"),
    ("image.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
    ("fake.pdf", b"<html><script>alert(1)</script></html>"),
    ("fake.png", PDF),
    ("run.exe", b"MZ\x90\x00"),
    ("macro.xlsm", b"PK\x03\x04"),
    ("nodot", PDF),
])
def test_board_rejects_disguised_or_unlisted_files(env, name, data):
    admin = login(env, "admin")
    r = admin.post("/board", data={"action": "save", "category": "form", "title": f"검증 {name}"}, files=[("files", (name, data, "application/octet-stream"))])
    assert r.status_code == 400, name
    assert env["con"].execute("SELECT COUNT(*) FROM board_post WHERE title=?", (f"검증 {name}",)).fetchone()[0] == 0


def test_board_rejects_file_over_20mb(env):
    admin = login(env, "admin")
    big = PDF + b"0" * (20 * 1024 * 1024)
    r = admin.post("/board", data={"action": "save", "category": "form", "title": "검증 큰 파일"}, files=[("files", ("big.pdf", big, "application/pdf"))])
    assert r.status_code == 400 and "20MB" in r.text


# ---------------------------------------------------------------- 퇴사자 D-42
def test_d42_leaver_rules_on_s2_screens(env):
    """그 달 중간 퇴사자: 조직 비교와 팀원 현황 합계에는 넣고 퇴사 표시, 랭킹과 프로모션 달성 대상에서는 뺀다."""
    con, ym, mgr = env["con"], env["ym"], env["mgr"]
    leaver = next(a for a in env["inside"] if (con.execute(
        """SELECT s.converted_premium FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
           WHERE s.agent_id=? AND s.period_type='month' AND s.period_key=?""", (ym, a, ym)).fetchone() or [0])[0])
    boss = login(env, mgr["code"])
    before = text(boss.get("/perf/org-compare?level=branch").text)
    day = f"{ym[:4]}-{ym[4:]}-15"
    con.execute("UPDATE agent SET status='terminated', termination_date=? WHERE agent_id=?", (day, leaver))
    con.execute("UPDATE agent_org_history SET effective_to=? WHERE agent_id=? AND effective_to IS NULL", (ym, leaver))
    con.commit()
    after = boss.get("/perf/org-compare?level=branch").text
    tot = lambda t: re.search(r"인정보험료 합계 확정 ([\d,]+) 원", t).group(1)
    assert tot(before) == tot(text(after)), "조직 비교 합계"
    assert "퇴사 1명" in after
    team = boss.get("/team/members").text
    name = con.execute("SELECT name FROM agent WHERE agent_id=?", (leaver,)).fetchone()[0]
    row = next((r for r in rows(team) if f'<td class="strong">{name}' in r), "")
    assert "퇴사" in row, "팀원 현황 퇴사 표시"
    assert f'data-agent="{leaver}"' not in boss.get("/perf/ranking").text
    promo = boss.get("/promo/progress").text
    assert f'<td class="strong">{name}<' not in promo, "프로모션 달성 대상"


# ---------------------------------------------------------------- 캐시
def test_error_and_redirect_pages_of_logged_in_user_do_not_leak(env):
    """로그인한 사람이 받는 403 과 404 화면에는 이름이나 실적이 없다. 그래서 no-store 가 없어도 된다."""
    con = env["con"]
    agent = login(env, code_of(con, env["inside"][0]))
    name = con.execute("SELECT name FROM agent WHERE agent_id=?", (env["inside"][0],)).fetchone()[0]
    for path in ("/team/members", "/data/contracts", "/board?file=999999"):
        r = agent.get(path)
        assert r.status_code in (403, 404), path
        assert name not in r.text, path
