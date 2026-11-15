"""S2 검증. 만든 에이전트와 따로 verifier 가 쓴 대조 시험이다. 2026-11-15.

보는 것
- 숫자 대조: 같은 조직과 같은 달의 인정보험료와 순건수가 홈, 기간별 업적, 구분별 집계, 조직 비교, 팀원 현황에서 같다.
  같은 사람의 숫자가 메인 관리자의 팀원 현황과 랭킹에서도 같다. 프로모션 달성 현황은 D-08 가정대로 보험사 기준이라
  업적 집계와 다를 수 있고, 그 차이가 basis 규칙으로 설명되며 화면에 엔진의 기준 안내가 보이는지 본다.
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
from app.engine import orgs as org_engine
from app.engine import persist
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


def _month_window(ym):
    y, m = int(ym[:4]), int(ym[4:])
    first = date(y, m, 1)
    return first, (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _basis_on_screen(html):
    m = re.search(r'data-note="basis"[^>]*>(.*?)</p>', html, re.S)
    return text(m.group(1)) if m else None


def test_promo_progress_differs_from_period_only_by_basis_rule_and_shows_basis_note(env):
    """D-08 은 제안대로 정해졌다(가정). 프로모션은 보험사 기준이라 업적 집계와 다를 수 있고, 그때 화면에 기준 안내가 보인다.

    - 새 프로모션의 기준일 기본값은 영수일자다. 영수일자가 빈 계약은 계약일자로 센다.
    - 차이는 basis 규칙으로 모두 설명된다. 달성 현황 값 = 기준일(영수일자, 없으면 계약일자)이 기간 안이고 성립 상태인 계약의 합.
      기간별 업적 값 = 성립한 달에 더하고 철회한 달에 뺀 합. 둘의 차이는 철회 계약과 기준일이 다른 달인 계약에서만 온다.
    - 차이가 있는 화면(관리자의 팀원 현황, 설계사의 내 현황)에 엔진의 basis 글이 그대로 보인다.
    """
    con, mgr, ym = env["con"], env["mgr"], env["ym"]
    first, last = _month_window(ym)
    prev_day = (first - timedelta(days=3)).isoformat()
    # 영수일자 규칙도 차이를 만들도록 범위 안 두 계약의 영수일자를 바꾼다. 하나는 기간 밖으로, 하나는 기간 안으로
    out_c = con.execute(f"""SELECT contract_id, agent_id FROM contract WHERE agent_id IN ({','.join('?' * len(env['inside']))})
                            AND status='inforce' AND contract_date BETWEEN ? AND ? ORDER BY contract_id LIMIT 1""",
                        (*env["inside"], first.isoformat(), last.isoformat())).fetchone()
    in_c = con.execute(f"""SELECT contract_id, agent_id FROM contract WHERE agent_id IN ({','.join('?' * len(env['inside']))})
                           AND status='inforce' AND contract_date < ? ORDER BY contract_date DESC, contract_id LIMIT 1""",
                       (*env["inside"], first.isoformat())).fetchone()
    assert out_c and in_c, "영수일자를 바꿀 계약"
    con.execute("UPDATE contract SET receipt_date=? WHERE contract_id=?", (prev_day, out_c["contract_id"]))
    con.execute("UPDATE contract SET receipt_date=? WHERE contract_id=?", ((first + timedelta(days=1)).isoformat(), in_c["contract_id"]))
    con.commit()
    pid = pm.create(con, period_ym=ym, name="검증용 전 보험사 인정보험료", kind="travel", metric="converted_premium",
                    windows=[("한 달", first.isoformat(), last.isoformat())], tiers=[(1, "fixed", 1, "검증")], by="verifier", publish=True)
    p = pm._load(con, pid)
    assert p["date_basis"] == pm.DEFAULT_DATE_BASIS == "receipt_date", "기준일 기본값"
    note = pm.basis_note(p)
    for word in ("영수일자", "계약일자", "성립", "철회", "업적 집계와 다를 수 있습니다"):
        assert word in note, word

    boss = login(env, mgr["code"])
    promo = boss.get(f"/promo/progress?p={pid}").text
    period = boss.get("/perf/period").text
    names = {}
    for r in con.execute("SELECT agent_id, name FROM agent"):
        names.setdefault(r["name"], []).append(r["agent_id"])
    a_in = env["inside"]
    q = ",".join("?" * len(a_in))
    one = lambda sql, *args: {r[0]: float(r[1] or 0) for r in con.execute(sql, (*a_in, *args))}
    counted = ",".join(f"'{s}'" for s in pm.COUNTED)
    basis_day = "COALESCE(receipt_date, contract_date)"
    f, t = first.isoformat(), last.isoformat()
    rule = one(f"SELECT agent_id, SUM(converted_premium) FROM contract WHERE agent_id IN ({q}) AND status IN ({counted}) AND {basis_day} BETWEEN ? AND ? GROUP BY agent_id", f, t)
    # 차이를 만드는 네 가지
    w_added = one(f"SELECT agent_id, SUM(converted_premium) FROM contract WHERE agent_id IN ({q}) AND status='withdrawn' AND contract_date BETWEEN ? AND ? GROUP BY agent_id", f, t)
    w_taken = one(f"SELECT agent_id, SUM(converted_premium) FROM contract WHERE agent_id IN ({q}) AND status='withdrawn' AND withdrawal_date BETWEEN ? AND ? GROUP BY agent_id", f, t)
    day_out = one(f"""SELECT agent_id, SUM(converted_premium) FROM contract WHERE agent_id IN ({q}) AND status IN ({counted})
                      AND contract_date BETWEEN ? AND ? AND NOT ({basis_day} BETWEEN ? AND ?) GROUP BY agent_id""", f, t, f, t)
    day_in = one(f"""SELECT agent_id, SUM(converted_premium) FROM contract WHERE agent_id IN ({q}) AND status IN ({counted})
                     AND NOT (contract_date BETWEEN ? AND ?) AND {basis_day} BETWEEN ? AND ? GROUP BY agent_id""", f, t, f, t)
    engine = pm.progress(con, p, a_in, pm.ref_date(ym))
    checked, differ, why = 0, [], set()
    for r in rows(period):
        m = re.search(r'data-agent="(\d+)"', r)
        if not m or int(m.group(1)) not in a_in:
            continue
        aid = int(m.group(1))
        name = next(n for n, ids in names.items() if aid in ids)
        if len(names[name]) > 1:
            continue
        pr = next((x for x in rows(promo) if f'<td class="strong">{name}<' in x), None)
        if pr is None:
            continue
        cells = [text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", pr, re.S)]
        on_promo, on_period = num(cells[2]) or 0, col(r, "premium") or 0                # 성명, 소속, 지표 칸
        checked += 1
        assert abs(on_promo - rule.get(aid, 0)) < 1, ("달성 현황 값이 basis 규칙과 다르다", checked)
        assert abs(on_promo - engine[aid]["value"]) < 1, ("달성 현황 값이 엔진과 다르다", checked)
        parts = {"철회 계약의 성립 달": w_added.get(aid, 0), "철회한 달": -w_taken.get(aid, 0),
                 "기준일이 기간 밖": day_out.get(aid, 0), "기준일만 기간 안": -day_in.get(aid, 0)}
        gap = on_period - on_promo
        assert abs(gap - sum(parts.values())) < 1, ("차이가 basis 규칙으로 설명되지 않는다", checked)
        if abs(gap) >= 1:
            differ.append(aid)
            why |= {k for k, v in parts.items() if abs(v) >= 1}
    assert checked >= 3, "대조한 사람 수"
    assert differ, "샘플에서 차이가 하나도 없으면 이 시험은 뜻이 없다"
    assert {"기준일이 기간 밖", "기준일만 기간 안"} <= why, "영수일자 규칙이 차이에 드러나야 한다"
    assert why & {"철회 계약의 성립 달", "철회한 달"}, "철회 제외 규칙이 차이에 드러나야 한다"

    # 차이가 있는 화면에 엔진의 basis 글이 그대로 보인다
    assert all(engine[a]["basis"] == note for a in a_in)
    shown = _basis_on_screen(promo)
    assert shown and note in shown, "관리자 팀원 달성 현황의 기준 안내"
    me = login(env, code_of(con, differ[0]))
    mine = me.get("/promo/progress").text
    sec = re.search(r"<section[^>]*>(?:(?!</section>).)*검증용 전 보험사 인정보험료(?:(?!</section>).)*</section>", mine, re.S)
    assert sec, "설계사 내 현황의 검증용 프로모션 카드"
    shown_me = _basis_on_screen(sec.group(0))
    assert shown_me and note in shown_me, "설계사 내 현황의 기준 안내"


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
    board = lambda: con.execute("SELECT COUNT(*) FROM board_post").fetchone()[0] if \
        con.execute("SELECT 1 FROM sqlite_master WHERE name='board_post'").fetchone() else 0
    before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("edu_course", "edu_session", "edu_completion", "audit_log")}
    posts = board()                       # run_demo 의 영업 샘플이 가이드 글 셋을 넣는다(S4). 토큰 없는 POST 가 글을 늘리지 않는지 본다
    for path, data in S2_POSTS:
        assert admin.post(path, data=data, csrf=False).status_code == 403, path
        assert admin.post(path, data={**data, "csrf_token": "wrong"}, csrf=False).status_code == 403, path
    after = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after
    assert board() == posts


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


# ================================================================ S2 닫기 검증 (2026-11-15). 지난 검증 뒤 커밋 40ba9cb 에서 51517b3
PLAIN = re.compile(r"검증(?:계약자|피보험)\d+")


def _synthetic_names(con):
    """샘플 계약의 이름은 이미 가린 꼴이라 가림을 가려낼 수 없다. 시험 DB 에서만 알아볼 수 있는 가짜 이름을 넣는다."""
    con.execute("UPDATE contract SET policyholder_name='검증계약자'||contract_id, insured_name='검증피보험'||contract_id")
    con.commit()


def _upper_head(con, org_id):
    """그 조직의 바로 위 조직장(본부장)의 사번."""
    r = con.execute("""SELECT a.internal_code FROM org_unit o JOIN org_manager m ON m.org_id=o.parent_id AND m.effective_to IS NULL
                       JOIN agent a ON a.agent_id=m.agent_id WHERE o.org_id=?""", (org_id,)).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- 숫자 대조: 같은 사람, 같은 달, 여섯 화면
def test_every_person_same_on_home_summary_team_period_ranking(env):
    """지점장 범위 안 설계사 모두. 본인 홈과 실적 요약, 관리자의 팀원 현황, 기간별 업적, 랭킹, 메인 관리자의 랭킹에서
    이번 달 인정보험료가 같고, 순건수는 실적 요약, 팀원 현황, 기간별 업적에서 같다. 홈의 내 순위는 메인 관리자 랭킹의 자리와 같다."""
    con = env["con"]
    boss, admin = login(env, env["mgr"]["code"]), login(env, "admin")
    team = [r for r in rows(boss.get("/team/members").text) if 'data-col="code"' in r]
    period = [r for r in rows(boss.get("/perf/period").text) if 'data-agent="' in r]
    rk_boss = [r for r in rows(boss.get("/perf/ranking").text) if 'data-agent="' in r]
    rk_all = [r for r in rows(admin.get("/perf/ranking").text) if 'data-agent="' in r]
    bad, n = [], 0
    for aid in env["inside"]:
        code = code_of(con, aid)
        me = login(env, code)
        home = text(me.get("/").text)
        m = re.search(r"이번 달 인정보험료 ([\d,\-]+) 원", home)
        rank = re.search(r"내 순위 (\d+) 위", home)
        summ = me.get("/my/summary").text
        t = next((r for r in team if f'data-col="code">{code}<' in r), "")
        p = next((r for r in period if f'data-agent="{aid}"' in r), "")
        rb = next((r for r in rk_boss if f'data-agent="{aid}"' in r), "")
        ra = next((i, r) for i, r in enumerate(rk_all) if f'data-agent="{aid}"' in r)
        premium = {"홈": num(m.group(1)) if m else None, "실적 요약": col(summ, "premium"), "팀원 현황": col(t, "premium"),
                   "기간별 업적": col(p, "premium"), "랭킹 관리자": col(rb, "value"), "랭킹 메인": col(ra[1], "value")}
        net = {"실적 요약": col(summ, "net"), "팀원 현황": col(t, "net"), "기간별 업적": col(p, "net")}
        rank_rk = num(text(re.search(r"<td[^>]*>(.*?)</td>", ra[1], re.S).group(1)))
        n += 1
        if None in premium.values() or len(set(premium.values())) != 1:
            bad.append(("인정보험료", n, sorted(k for k, v in premium.items() if v != premium["팀원 현황"])))
        if len(set(net.values())) != 1:
            bad.append(("순건수", n, sorted(net)))
        if not rank or int(rank.group(1)) != rank_rk:
            bad.append(("순위", n))
    assert n >= 5
    assert not bad, bad                                                  # 사람은 순번으로만 낸다


# ---------------------------------------------------------------- 유지율: 기존 운영 도구와 같은 계산
def test_persistency_screens_equal_persist_rate(env):
    """유지율 화면의 사람별과 범위 전체, 조직을 고른 화면, 내 유지율이 persist.rate 와 같다. 기존 운영 도구(app/ui.py)는 조직마다
    persist.rate(orgs.members(조직)) 를 쓴다."""
    con, ym = env["con"], env["ym"]
    fmt = lambda r, d: f"{r * 100:.1f}%" if d else "자료 없음"

    def hero(html, kind):
        seg = html[html.find(f'<div class="card" data-kind="{kind}">'):][:1500]
        m = re.search(r'<div class="mid">([\d.]+)<span class="unit">%', seg)
        return f"{m.group(1)}%" if m else ("자료 없음" if "자료 없음" in seg else None)

    bad, n = [], 0
    for who in ("admin", env["mgr"]["code"]):
        c = login(env, who)
        html = c.get("/perf/persistency").text
        for aid, tr in re.findall(r'<tr data-agent="(\d+)">(.*?)</tr>', html, re.S):
            for kind in ("p13", "p25"):
                r, _, d = persist.rate(con, [int(aid)], kind, ym)
                n += 1
                if text(re.search(rf'data-kind="{kind}">(.*?)</td>', tr).group(1)) != fmt(r, d):
                    bad.append((who, "사람별", kind))
        orgs_ = [r[0] for r in con.execute("SELECT org_id FROM org_unit")] if who == "admin" else sorted(env["mctx"]["org_ids"])
        for o in orgs_:
            h = c.get(f"/perf/persistency?org={o}").text
            for kind in ("p13", "p25"):
                r, _, d = persist.rate(con, org_engine.members(con, o, ym), kind, ym)
                n += 1
                if hero(h, kind) != fmt(r, d):
                    bad.append((who, "조직", o, kind))
    for aid in env["inside"][:3]:
        h = login(env, code_of(con, aid)).get("/my/persistency").text
        for kind in ("p13", "p25"):
            r, _, d = persist.rate(con, [aid], kind, ym)
            n += 1
            if hero(h, kind) != fmt(r, d):
                bad.append(("내 유지율", kind))
    assert n > 50
    assert not bad, bad


def test_persistency_org_rate_same_as_legacy_with_leaver(env):
    """그 달 중간 퇴사자가 있어도 같은 조직, 같은 달의 13회차 유지율이 웹 유지율 화면과 기존 운영 도구에서 같다.
    둘 다 persist.org_members, 곧 재직 설계사 기준이다. 웹은 범위 안 사람만 남기고 화면에서 고른 달을 넘긴다."""
    con, ym, mgr = env["con"], env["ym"], env["mgr"]
    leaver = next(a for a in env["inside"] if persist.rate(con, [a], "p13", ym)[2])
    con.execute("UPDATE agent SET status='terminated', termination_date=? WHERE agent_id=?", (f"{ym[:4]}-{ym[4:]}-15", leaver))
    con.execute("UPDATE agent_org_history SET effective_to=? WHERE agent_id=? AND effective_to IS NULL", (ym, leaver))
    con.commit()
    html = login(env, "admin").get(f"/perf/persistency?org={mgr['org_id']}").text
    seg = html[html.find('<div class="card" data-kind="p13">'):][:1500]
    shown = re.search(r'<div class="mid">([\d.]+)<span class="unit">%', seg).group(1)
    legacy = persist.rate(con, org_engine.members(con, mgr["org_id"], ym), "p13", ym)[0]
    assert shown == f"{legacy * 100:.1f}"


# ---------------------------------------------------------------- 13회차 도래: 일정, 알림함, 내 유지율
def test_inst13_same_on_calendar_notifications_and_my_persistency(env):
    """이번 주에 13회차가 오는 계약을 만든다. 알림함의 이번 주 건수, 일정의 이번 주 날짜별 건수 합, 내 유지율의 그 달 건수,
    일정의 그 달 건수가 같다. 관리자는 범위 안 합이다."""
    con = env["con"]
    today = date.today()
    start = today - timedelta(days=(today.weekday() + 1) % 7)
    end = start + timedelta(days=6)
    who = env["inside"][0]
    cids = [r[0] for r in con.execute("SELECT contract_id FROM contract WHERE agent_id=? AND status='inforce' ORDER BY contract_id LIMIT 2", (who,))]
    for i, cid in enumerate(cids):
        day = start + timedelta(days=i * 2)
        con.execute("UPDATE contract SET contract_date=?, payment_mode='monthly' WHERE contract_id=?", (day.replace(year=day.year - 1).isoformat(), cid))
    con.commit()

    def week_on_calendar(c):
        tot = 0
        for m in sorted({start.strftime("%Y-%m"), end.strftime("%Y-%m")}):
            for blk in re.split(r'<div class="cal-date', c.get(f"/calendar?m={m}&view=list").text)[1:]:
                md = re.match(r'[^>]*>(\d+)월 (\d+)일', blk)
                if md and start <= date(int(m[:4]), int(md.group(1)), int(md.group(2))) <= end:
                    tot += sum(int(x) for x in re.findall(r"13회차 도래 (\d+)건</", blk))
        return tot

    for login_id in (code_of(con, who), env["mgr"]["code"]):
        c = login(env, login_id)
        note = re.search(r"13회차 도래 (\d+)건\(이번 주\)", c.get("/notifications").text)
        on_cal = week_on_calendar(c)
        assert on_cal >= len(cids), login_id == env["mgr"]["code"]
        assert note and int(note.group(1)) == on_cal, ("알림함과 일정", "manager" if login_id == env["mgr"]["code"] else "agent")
    me = login(env, code_of(con, who))
    mine = me.get("/my/persistency").text
    ym_now = today.strftime("%Y%m")
    card = re.search(rf'data-ym="{ym_now}"><div class="cap"[^>]*>[^<]*</div><div class="mid">(\d+)<', mine)
    month = sum(int(x) for x in re.findall(r"13회차 도래 (\d+)건</", me.get(f"/calendar?m={today.strftime('%Y-%m')}&view=list").text))
    assert card and int(card.group(1)) == month, "내 유지율과 일정의 이번 달 13회차 도래"


# ---------------------------------------------------------------- 교육 숫자: 네 화면
def test_education_soon_count_same_on_home_team_notifications_calendar(env):
    """오늘에서 사흘 뒤가 기한인 필수 과정. 관리자 홈의 교육 기한 임박 건수(늘어난 만큼), 팀 교육 진척도의 임박, 알림함, 일정이 같다."""
    con = env["con"]
    boss = login(env, env["mgr"]["code"])
    home_n = lambda: int((re.search(r"교육 기한 임박 (\d+)건", text(boss.get("/").text)) or [0, 0])[1])
    before = home_n()
    due = date.today() + timedelta(days=3)
    cid = edu.save_course(con, by="verifier", code="VF-SOON", name="검증용 임박 과정", kind="compliance", required=True,
                          target_role="fc", deadline_rule="fixed", deadline_date=due.isoformat())
    con.commit()
    notify.clear_cache()
    seen = {"홈": home_n() - before}
    team = text(boss.get(f"/team/education?course={cid}").text)
    seen["팀 교육 진척도"] = int(re.search(r"임박 (\d+) 건", team).group(1))
    m = re.search(r"검증용 임박 과정 기한 임박 팀원 (\d+)명", text(boss.get("/notifications").text))
    seen["알림함"] = int(m.group(1)) if m else 0
    cal = text(boss.get(f"/calendar?m={due.strftime('%Y-%m')}&view=list").text)
    m = re.search(r"검증용 임박 과정 기한 미수료 (\d+)명", cal)
    seen["일정"] = int(m.group(1)) if m else 0
    assert seen["팀 교육 진척도"] > 0
    assert len(set(seen.values())) == 1, sorted(seen.items())


# ---------------------------------------------------------------- 계약 원장 D-05
def test_contract_names_by_role_on_list_detail_and_csv(env):
    """목록과 CSV 에는 누구에게도 이름 원문이 없다. 상세는 담당 설계사와 직속 관리자만 원문, 위 조직 관리자는 가림,
    다른 지점 관리자와 설계사는 403, 메인 관리자는 사유를 적은 뒤에만 원문이다."""
    con, mgr = env["con"], env["mgr"]
    _synthetic_names(con)
    own = env["inside"][0]
    cid = con.execute("SELECT contract_id FROM contract WHERE agent_id=? ORDER BY contract_id DESC LIMIT 1", (own,)).fetchone()[0]
    other_branch_head = con.execute("""SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id
                                       WHERE o.level='branch' AND m.org_id<>? AND m.effective_to IS NULL ORDER BY m.org_id LIMIT 1""", (mgr["org_id"],)).fetchone()[0]
    upper = _upper_head(con, mgr["org_id"])
    expect = {code_of(con, own): "shown", mgr["code"]: "shown", upper: "masked", "admin": "masked",
              other_branch_head: 403, code_of(con, env["inside"][1]): 403}
    for who, want in expect.items():
        c = login(env, who)
        lst = c.get("/perf/contracts?ym=all")
        csv_ = c.get("/perf/contracts?export=csv&ym=all")
        assert lst.status_code == 200 and not PLAIN.search(lst.text), ("목록", want)
        assert csv_.status_code == 200 and not PLAIN.search(csv_.content.decode("utf-8-sig"))
        assert lst.headers["cache-control"] == "no-store" and csv_.headers["cache-control"] == "no-store"
        r = c.get(f"/perf/contracts?id={cid}")
        if want == 403:
            assert r.status_code == 403 and not PLAIN.search(r.text)
            continue
        assert re.search(r'data-names="(\w+)"', r.text).group(1) == want
        assert bool(PLAIN.search(r.text)) == (want == "shown")
    admin = login(env, "admin")
    r = admin.post("/perf/contracts", data={"action": "reveal", "id": str(cid), "reason": "검증 사유입니다"})
    assert r.status_code == 200 and PLAIN.search(r.text) and r.headers["cache-control"] == "no-store"
    assert not PLAIN.search(admin.get(f"/perf/contracts?id={cid}").text), "사유는 한 번만 쓴다"


def test_contract_search_box_is_touch_size():
    """계약 원장의 증권번호 칸은 type=search 이고 class sm 이다. 휴대폰 390 폭에서 높이가 21픽셀로 보였다(사진 admin_390_perf_contracts.png).

    app.css 의 글 칸 규칙에 search, email, tel, url 을 더해 고쳤다. 테두리와 54픽셀 기본 높이, sm 의 48픽셀이 함께 걸린다."""
    html = (pathlib.Path(__file__).resolve().parents[1] / "app/web/templates/perf_contracts.html").read_text()
    css = (pathlib.Path(__file__).resolve().parents[1] / "app/web/static/app.css").read_text()
    kind = re.search(r'<input class="sm" type="(\w+)" name="q"', html)
    assert kind
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    sized = [sel for sel, body in rules if re.search(r"(?:^|[;\s])(?:min-)?height:\s*4[4-9]px", body)
             and any(s.strip() in (f"input[type={kind.group(1)}].sm", f"input[type={kind.group(1)}]", "input.sm", ".sm")
                     or (f"type={kind.group(1)}" in s and "input" in s) for s in sel.split(","))]
    assert sized, f"input[type={kind.group(1)}].sm 에 48픽셀 높이 규칙이 없다"


# ---------------------------------------------------------------- 새 POST 의 CSRF 와 역할
def test_new_posts_refuse_without_csrf_and_for_other_roles(env):
    con = env["con"]
    p = pm.ended(con, ref=date.today())[0]
    tier = con.execute("SELECT tier_no FROM promotion_tier WHERE promo_id=? AND reward_kind='fixed' LIMIT 1", (p["promo_id"],)).fetchone()
    cid = con.execute("SELECT contract_id FROM contract WHERE agent_id=? LIMIT 1", (env["inside"][0],)).fetchone()[0]
    posts = (("/perf/contracts", {"action": "reveal", "id": str(cid), "reason": "토큰 없는 사유"}),
             ("/promo/tiers", {"action": "original", "promo_id": str(p["promo_id"]), f"o_tier_{tier[0] if tier else 1}": "1"}),
             ("/promo/archive", {"action": "confirm", "promo_id": str(p["promo_id"]), "ym": p["period_ym"], "agree": "yes"}))
    admin = login(env, "admin")
    views = con.execute("SELECT COUNT(*) FROM audit_log WHERE action='contract_name_view'").fetchone()[0]
    for path, data in posts:
        assert admin.post(path, data=data, csrf=False).status_code == 403, path
    for who in (env["mgr"]["code"], code_of(con, env["inside"][0])):
        c = login(env, who)
        for path, data in posts:
            assert c.post(path, data=data).status_code == 403, (path, who == env["mgr"]["code"])
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='contract_name_view'").fetchone()[0] == views
    assert pm.confirmation(con, p["promo_id"]) is None


def test_confirmed_results_stay_on_every_role_screen(env):
    """확정한 뒤 원장이 바뀌어도 메인 관리자, 관리자, 설계사의 지난 프로모션 화면 값은 확정한 값이다. 원안을 고쳐도 그대로다."""
    con = env["con"]
    p = next(x for x in pm.ended(con, ref=date.today()) if x["kind"] != "rate")
    pid, ym = p["promo_id"], p["period_ym"]
    admin = login(env, "admin")
    assert admin.post("/promo/archive", data={"action": "confirm", "promo_id": str(pid), "ym": ym, "agree": "yes"}).status_code == 303
    head = pm.confirmation(con, pid)
    stored = {r["agent_id"]: r["value"] for r in con.execute("SELECT agent_id, value FROM promotion_result WHERE confirm_id=?", (head["confirm_id"],))}
    f, t = p["date_from"], p["date_to"]
    con.execute("UPDATE contract SET status='withdrawn', withdrawal_date=? WHERE status='inforce' AND contract_date BETWEEN ? AND ?", (t, f, t))
    con.commit()
    tiers = [x["tier_no"] for x in p["tiers"] if x["reward_kind"] == "fixed"]
    if tiers:
        assert admin.post("/promo/tiers", data={"action": "original", "promo_id": str(pid), f"o_tier_{tiers[0]}": "1"}).status_code == 303
    fresh = pm.progress(con, pm._load(con, pid), list(stored), date.today())
    assert any(abs(fresh[a]["value"] - v) > 0.5 for a, v in stored.items()), "원장을 바꾸면 다시 계산한 값은 달라져야 뜻이 있다"
    url = f"/promo/archive?ym={ym}&p={pid}"
    names = {r[0]: r[1] for r in con.execute("SELECT agent_id, name FROM agent")}
    for who in ("admin", env["mgr"]["code"]):
        html = login(env, who).get(url).text
        for a, v in stored.items():
            tr = next((r for r in rows(html) if f'<td class="strong">{names[a]}<' in r), None)
            if tr is not None:
                assert f"{int(round(v)):,}" in tr, ("확정 값", "main" if who == "admin" else "manager")
    me = next(a for a in env["inside"] if stored.get(a))
    html = login(env, code_of(con, me)).get(f"/promo/archive?ym={ym}").text
    assert f"{int(round(stored[me])):,}" in html, "설계사 본인 화면의 확정 값"
    after = {x["agent_id"]: x["value"] for x in pm.final_results(con, pm._load(con, pid), list(stored))["rows"]}
    assert after == stored


# ---------------------------------------------------------------- D-17, D-42, no-store 훑기
def test_new_screens_no_store_no_commission_and_leaver_rules(env):
    con, ym = env["con"], env["ym"]
    pages = {"admin": ("/perf/contracts", "/perf/persistency", "/calendar", "/promo/archive"),
             env["mgr"]["code"]: ("/perf/contracts", "/perf/persistency", "/my/persistency", "/calendar", "/promo/archive"),
             code_of(con, env["inside"][0]): ("/perf/contracts", "/my/persistency", "/calendar", "/promo/archive")}
    for who, paths in pages.items():
        c = login(env, who)
        for path in paths:
            r = c.get(path)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-store", path
            if path in ("/perf/contracts", "/perf/persistency", "/my/persistency"):
                assert not re.search(r"수수료\s*[\d,]+|시상\s*금액\s*[\d,]+", text(r.text)), path
    # D-42: 그 달 중간 퇴사자는 계약 원장의 관리자 범위에는 남고(조직 합계와 같은 범위), 일정의 13회차 도래 범위에는 남는다
    leaver = env["inside"][0]
    n_before = con.execute("SELECT COUNT(*) FROM contract WHERE agent_id=?", (leaver,)).fetchone()[0]
    con.execute("UPDATE agent SET status='terminated', termination_date=? WHERE agent_id=?", (f"{ym[:4]}-{ym[4:]}-15", leaver))
    con.execute("UPDATE agent_org_history SET effective_to=? WHERE agent_id=? AND effective_to IS NULL", (ym, leaver))
    con.commit()
    body = login(env, env["mgr"]["code"]).get("/perf/contracts?export=csv&ym=all").content.decode("utf-8-sig")
    assert body.count(f",{code_of(con, leaver)},") == n_before
