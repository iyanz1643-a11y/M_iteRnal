"""S2 의 마지막 화면 일: 유지율 두 화면, 일정과 알림의 13회차 도래, 프로모션 화면 정리(기준일 기본값, 지급 시점, 원안 입력, 결과 확정).

| 무엇 | 보는 것 |
| --- | --- |
| 권한 | 유지율은 메인 관리자와 관리자, 내 유지율은 관리자와 설계사만 연다. 범위 밖 조직을 고르면 403 이다 |
| 범위 | 관리자의 유지율에는 범위 안 사람만 있고 값이 엔진(persist)과 같다. 내 유지율은 본인 한 사람의 값이다 |
| 자료 없음 | 대상이 0건이면 0% 가 아니라 자료 없음으로 보인다 |
| D-05 | 위험 계약 목록에는 증권번호 끝 네 자리와 단계만 있다. 고객 이름과 증권번호 전체는 없다 |
| D-17 | 유지율 화면에는 금액이 없다. 확정한 뒤에도 관리자는 팀원의 시상 금액을 보지 않는다 |
| 13회차 도래 | 일정에 날짜별 건수가 있다. 알림함에는 설계사 본인과 관리자 범위의 이번 주 건수 하나만 있다 |
| 원안 (D-21) | 메인 관리자만 넣는다. 관리자와 설계사의 POST 는 403 이다. CSRF 를 거친다 |
| 확정 | 메인 관리자만 한다. 경고와 확인 칸을 거친다. 확정한 뒤에는 계약이 바뀌어도 값이 그대로다 |
| 기준일 | 새 프로모션의 기준일 기본값은 엔진의 DEFAULT_DATE_BASIS 다 |

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 는 건드리지 않는다.
사람 이름과 금액은 실패 메시지에 넣지 않는다. 화면 주소와 번호만 넣는다.
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
from app.engine import persist as ps
from app.engine import promotion as pm
from app.periods import ym_add
from app.web import auth, menu

PW = "last-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
MONEY = re.compile(r"\d[\d,]*\s*(?:<span class=\"unit\">)?원")


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리킨다. 다른 시험 모듈이 APP_DB 를 바꿔 두어도 섞이지 않는다."""

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


def _ctx(con, agent_id=None, login_id=None):
    if login_id is None:
        login_id = con.execute("SELECT login_id FROM user_account WHERE agent_id=?", (agent_id,)).fetchone()[0]
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0]
    return auth.context(con, uid)


@pytest.fixture(scope="module")
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="s2last-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    # 가리지 않은 가짜 이름. 위험 목록에 이름이 새지 않는지 보려면 알아볼 수 있는 이름이 있어야 한다
    con.execute("UPDATE contract SET policyholder_name='시험고객' || printf('%04d', contract_id) || '님', insured_name='시험피보' || printf('%04d', contract_id) || '님'")
    ym = auth.current_ym(con)
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    # 설계사 한 명. 계약이 많은 재직 설계사다. 월납 유지 계약 둘을 밀리게 해 미납과 실효 위험을 만든다
    who = next(r[0] for r in con.execute("""SELECT a.agent_id FROM agent a JOIN user_account u ON u.agent_id=a.agent_id
                                            JOIN contract c ON c.agent_id=a.agent_id AND c.status='inforce'
                                            WHERE a.status='active' GROUP BY a.agent_id ORDER BY COUNT(*) DESC, a.agent_id""") if r[0] not in heads)
    live = [r[0] for r in con.execute("""SELECT contract_id FROM contract WHERE agent_id=? AND status='inforce' AND COALESCE(payment_mode,'monthly')='monthly'
                                         AND policy_no IS NOT NULL AND substr(replace(contract_date,'-',''),1,6) < ? ORDER BY contract_id""", (who, ym))]
    assert len(live) >= 2
    con.execute("UPDATE contract SET last_paid_ym=? WHERE contract_id=?", (ym_add(ym, -3), live[0]))      # 2회 밀림: 실효 위험
    con.execute("UPDATE contract SET last_paid_ym=? WHERE contract_id=?", (ym_add(ym, -2), live[1]))      # 1회 밀림: 미납
    con.commit()
    # 그 설계사를 범위에 넣는 관리자 가운데 범위가 가장 좁은 사람과, 그 관리자 범위 밖의 설계사
    mgrs = []
    for (a,) in con.execute("SELECT DISTINCT agent_id FROM org_manager WHERE effective_to IS NULL"):
        c = _ctx(con, a)
        if c and c["role"] == "manager" and who in c["agent_ids"]:
            mgrs.append((len(c["agent_ids"]), a))
    mgr = sorted(mgrs)[0][1]
    mctx = _ctx(con, mgr)
    outsider = next(r[0] for r in con.execute("""SELECT a.agent_id FROM agent a JOIN user_account u ON u.agent_id=a.agent_id
                                                 WHERE a.status='active' ORDER BY a.agent_id""") if r[0] not in mctx["agent_ids"] and r[0] not in heads)
    from app.web.main import app
    e = {"tmp": tmp, "db": path, "app": app, "con": con, "ym": ym, "who": who, "mgr": mgr, "outsider": outsider, "risky": live[:2]}
    yield e
    con.close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


def copy_db(env, name):
    dst = env["tmp"] / f"{name}.db"
    shutil.copy(env["db"], dst)
    return dst


def login(env, login_id, path=None):
    c = Client(env["app"], path or env["db"])
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, (login_id, r.status_code)
    return c


def login_agent(env, agent_id, path=None):
    return login(env, env["con"].execute("SELECT login_id FROM user_account WHERE agent_id=?", (agent_id,)).fetchone()[0], path)


def pct(rate):
    return f"{rate * 100:.1f}"


def hero(html, kind):
    """큰 숫자 카드 하나의 HTML."""
    m = re.search(rf'<div class="card" data-kind="{kind}">(.*?)</div>\s*(?:<div class="card" data-kind|</div>)', html, re.S)
    assert m, kind
    return m.group(1)


# ================================================================ 메뉴와 권한
def test_menu_marks_both_screens_built():
    for path in ("/perf/persistency", "/my/persistency"):
        assert menu.find(path)["status"] == "built", path


def test_persistency_screens_open_by_role(env):
    main, mgr, agent = login(env, "admin"), login_agent(env, env["mgr"]), login_agent(env, env["who"])
    assert main.get("/perf/persistency").status_code == 200
    assert main.get("/my/persistency").status_code == 403                   # 메인 관리자에게는 내 진척 묶음이 없다
    assert mgr.get("/perf/persistency").status_code == 200 and mgr.get("/my/persistency").status_code == 200
    assert agent.get("/perf/persistency").status_code == 403 and agent.get("/my/persistency").status_code == 200
    anon = Client(env["app"], env["db"])
    for path in ("/perf/persistency", "/my/persistency"):
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_manager_sees_only_scope_and_engine_values(env):
    con, ym = env["con"], env["ym"]
    mctx = _ctx(con, env["mgr"])
    html = login_agent(env, env["mgr"]).get("/perf/persistency").text
    shown = {int(x) for x in re.findall(r'data-agent="(\d+)"', html)}
    assert shown and shown <= set(mctx["agent_ids"])
    assert env["outsider"] not in shown
    want = ps.rates(con, sorted(mctx["agent_ids"]), ym)
    for k in want["kinds"]:
        part = hero(html, k["kind"])
        assert (pct(k["rate"]) in part) if k["rate"] is not None else ("자료 없음" in part), k["kind"]
    people = {p["agent_id"]: p for p in ps.by_agent(con, sorted(mctx["agent_ids"]), ym)}
    assert shown == set(people)
    # 범위 밖 조직을 고르면 403
    outside = next(r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE active=1 ORDER BY org_id") if r[0] not in mctx["org_ids"])
    assert login_agent(env, env["mgr"]).get(f"/perf/persistency?org={outside}").status_code == 403


def test_main_sees_everyone(env):
    con, ym = env["con"], env["ym"]
    everyone = [r[0] for r in con.execute("SELECT agent_id FROM agent")]
    html = login(env, "admin").get("/perf/persistency").text
    for k in ps.rates(con, everyone, ym)["kinds"]:
        assert k["rate"] is not None and pct(k["rate"]) in hero(html, k["kind"]), k["kind"]
    risk = ps.risk(con, everyone, ym)
    assert f"위험 계약 {sum(risk['counts'][x] for x in ('lapse_risk', 'unpaid', 'lapsed'))}건" in html


def test_my_persistency_is_self_only(env):
    con, ym, who = env["con"], env["ym"], env["who"]
    html = login_agent(env, who).get("/my/persistency").text
    o = ps.overview(con, [who], ym)
    for k in o["rates"]["kinds"]:
        part = hero(html, k["kind"])
        assert (pct(k["rate"]) in part) if k["rate"] is not None else ("자료 없음" in part), k["kind"]
    risk = o["risk"]
    assert risk["counts"]["lapse_risk"] >= 1 and risk["counts"]["unpaid"] >= 1          # 시험이 만든 밀린 계약 둘
    assert f"위험 계약 {sum(risk['counts'][x] for x in ('lapse_risk', 'unpaid', 'lapsed'))}건" in html
    assert f"13회차 도래 {o['due']['total']}건" in html
    assert 'data-agent="' not in html                                                    # 사람별 표는 본인 화면에 없다


def test_screens_carry_time_stamps_and_no_amounts(env):
    for c, path in ((login(env, "admin"), "/perf/persistency"), (login_agent(env, env["mgr"]), "/perf/persistency"),
                    (login_agent(env, env["mgr"]), "/my/persistency"), (login_agent(env, env["who"]), "/my/persistency")):
        html = c.get(path).text
        assert "유지율 현황 기준입니다" in html and "계약 적재 기준입니다" in html, path
        body = html.split('<main', 1)[-1]
        assert not MONEY.search(body), path                                              # D-17: 금액이 없다


# ================================================================ 자료 없음
def test_none_rate_shows_no_data_not_zero(env):
    path = copy_db(env, "none")
    con = db.connect(path)
    who, ym = env["who"], env["ym"]
    con.execute("DELETE FROM persistency WHERE agent_id=?", (who,))
    ins = con.execute("SELECT insurer_id FROM insurer ORDER BY insurer_id LIMIT 1").fetchone()[0]
    con.execute("INSERT INTO persistency(agent_id, insurer_id, period_ym, kind, numerator, denominator, rate) VALUES(?,?,?,?,0,0,0)", (who, ins, ym, "p13"))
    con.commit()
    o = ps.overview(con, [who], ym)
    assert all(k["rate"] is None for k in o["rates"]["kinds"])
    html = login_agent(env, who, path).get("/my/persistency").text
    for kind in ("p13", "p25"):
        part = hero(html, kind)
        assert "자료 없음" in part and "0.0" not in part and "0%" not in part, kind
    # 관리자의 사람별 표에서도 그 사람은 자료 없음이다
    html = login_agent(env, env["mgr"], path).get("/perf/persistency").text
    row = re.search(rf'<tr data-agent="{who}">(.*?)</tr>', html, re.S).group(1)
    assert row.count("자료 없음") >= 2 and "0.0%" not in row
    con.close()


# ================================================================ D-05 위험 목록
def test_risk_list_has_tail_and_stage_only(env):
    con, ym = env["con"], env["ym"]
    mctx = _ctx(con, env["mgr"])
    pages = {("agent", "/my/persistency"): (login_agent(env, env["who"]), [env["who"]]),
             ("manager", "/perf/persistency"): (login_agent(env, env["mgr"]), sorted(mctx["agent_ids"])),
             ("main", "/perf/persistency"): (login(env, "admin"), [r[0] for r in con.execute("SELECT agent_id FROM agent")])}
    names = {n for r in con.execute("SELECT policyholder_name, insured_name FROM contract") for n in r if n}
    for (label, path), (c, ids) in pages.items():
        html = c.get(path).text
        items = ps.risk(con, ids, ym)["items"]
        assert items, label
        ids_in = [x["contract_id"] for x in items]
        q = ",".join("?" * len(ids_in))
        full = {r[0] for r in con.execute(f"SELECT policy_no FROM contract WHERE contract_id IN ({q}) AND policy_no IS NOT NULL", ids_in)}
        assert full and all(len(p) > 4 for p in full)
        table = re.search(r'<table class="risk-list">(.*?)</table>', html, re.S).group(1)
        assert re.findall(r"<th>(.*?)</th>", table) == ["증권번호 끝 네 자리", "단계"], label
        for x in items[:60]:
            assert x["policy_tail"] in table, label
        assert not any(p in html for p in full), label
        assert not any(n in html for n in names), label
        assert "시험고객" not in html and "시험피보" not in html, label


# ================================================================ 일정과 알림의 13회차 도래
def _due_month(con, agent_id):
    """그 설계사의 13회차 도래가 있는 달 하나(YYYY-MM)."""
    for (d,) in con.execute("""SELECT contract_date FROM contract WHERE agent_id=? AND status='inforce' AND COALESCE(payment_mode,'')!='single'
                               AND contract_date IS NOT NULL ORDER BY contract_date DESC""", (agent_id,)):
        return ps.due13(d).strftime("%Y-%m")
    return None


def test_calendar_shows_inst13_counts_by_scope(env):
    con, ym = env["con"], env["ym"]
    month = _due_month(con, env["who"])
    assert month
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    mctx = _ctx(con, env["mgr"])
    cases = [(login_agent(env, env["who"]), [env["who"]]), (login_agent(env, env["mgr"]), sorted(mctx["agent_ids"])),
             (login(env, "admin"), [r[0] for r in con.execute("SELECT agent_id FROM agent ORDER BY agent_id")])]
    for c, ids in cases:
        html = c.get(f"/calendar?m={month}&view=list").text
        due = ps.inst13_due(con, ids, first, last, ym=ym)
        assert due["days"]
        for d in due["days"]:
            assert f"13회차 도래 {d['count']}건" in html, d["date"]
        assert len(re.findall(r"13회차 도래 \d+건", html)) == len(due["days"])
    # 일정에는 계약을 가리키는 것이 없다
    html = cases[0][0].get(f"/calendar?m={month}&view=list").text
    tails = {r[0][-4:] for r in con.execute("SELECT policy_no FROM contract WHERE agent_id=? AND policy_no IS NOT NULL", (env["who"],))}
    assert not any(f">{t}<" in html for t in tails)


def test_notifications_carry_this_week_inst13_count(env):
    from app.web.routes import notify
    path = copy_db(env, "due")
    con = db.connect(path)
    today = date.today()
    first, last = notify.this_week(today)
    assert first.weekday() == 6 and (last - first).days == 6 and first <= today <= last
    # 설계사의 유지 계약 하나를 이번 주 안에 13회차가 오게 옮긴다
    cid = con.execute("""SELECT contract_id FROM contract WHERE agent_id=? AND status='inforce' AND COALESCE(payment_mode,'')!='single'
                         ORDER BY contract_id LIMIT 1""", (env["who"],)).fetchone()[0]
    day = first + timedelta(days=3)
    con.execute("UPDATE contract SET contract_date=? WHERE contract_id=?", (date(day.year - 1, day.month, min(day.day, 28)).isoformat(), cid))
    con.commit()
    notify.clear_cache()
    mctx = _ctx(con, env["mgr"])
    mine = ps.inst13_due(con, [env["who"]], first, last)["total"]
    team = ps.inst13_due(con, sorted(mctx["agent_ids"]), first, last)["total"]
    out = ps.inst13_due(con, [env["outsider"]], first, last)["total"]
    assert mine >= 1 and team >= mine
    pat = re.compile(r"13회차 도래 (\d+)건\(이번 주\)")
    html = login_agent(env, env["who"], path).get("/notifications").text
    assert pat.findall(html) == [str(mine)]
    html = login_agent(env, env["mgr"], path).get("/notifications").text
    assert pat.findall(html) == [str(team)]
    html = login_agent(env, env["outsider"], path).get("/notifications").text
    assert pat.findall(html) == ([str(out)] if out else [])
    assert not pat.findall(login(env, "admin", path).get("/notifications").text)          # 메인 관리자에게는 띄우지 않는다
    # 읽음 표시가 된다. 알림 열쇠의 꼴이 읽음 POST 의 검사를 지난다
    c = login_agent(env, env["who"], path)
    c.get("/notifications")
    key = f"inst13_due:{first.isoformat()}:{mine}"
    assert notify.KEY_RE.match(key)
    assert c.post("/notifications", data={"action": "read", "key": key}).status_code == 303
    assert con.execute("SELECT 1 FROM notification_read WHERE note_key=?", (key,)).fetchone()
    con.close()
    notify.clear_cache()


# ================================================================ 프로모션: 기준일과 지급 시점
def test_new_promotion_defaults_to_engine_date_basis(env):
    from app.web.routes import promo
    d = promo._tier_form_defaults(env["con"], {}, None, None)
    assert d["date_basis"] == pm.DEFAULT_DATE_BASIS
    path = copy_db(env, "basis")
    c = login(env, "admin", path)
    html = c.get("/promo/tiers").text
    assert re.search(rf'<option value="{pm.DEFAULT_DATE_BASIS}" selected>', html)
    r = c.post("/promo/tiers", data={"name": "기준일 시험", "kind": "weekly", "metric": "premium_monthly", "w_from_1": "2026-11-01", "w_to_1": "2026-11-07",
                                     "t_min_1": "100000", "t_kind_1": "fixed", "t_value_1": "10000", "action": "draft"})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    con = db.connect(path)
    assert con.execute("SELECT date_basis FROM promotion WHERE name='기준일 시험'").fetchone()[0] == pm.DEFAULT_DATE_BASIS
    con.close()


def test_home_uses_engine_timing_words():
    from app.web.routes import home
    assert not hasattr(home, "TIMING_KO")
    assert "TIMING_KO =" not in pathlib.Path(home.__file__).read_text(encoding="utf-8")          # 사본을 두지 않는다
    assert "pm.timing_ko(" in pathlib.Path(home.__file__).read_text(encoding="utf-8")


def test_timing_select_on_screen(env):
    c = login(env, "admin")
    for url in ["/promo/tiers"] + [f"/promo/tiers?edit={r[0]}" for r in env["con"].execute("SELECT promo_id FROM promotion ORDER BY promo_id")]:
        html = c.get(url).text
        sel = re.search(r'<select name="pay_timing">(.*?)</select>', html, re.S).group(1)
        labels = re.findall(r"<option[^>]*>(.*?)</option>", sel)
        assert labels.count("표기 없음") == 1, url
        assert len(labels) == len(set(labels)), url
        assert all(lab == "표기 없음" or lab == pm.timing_ko(code) for code, lab in re.findall(r'<option value="([^"]*)"[^>]*>(.*?)</option>', sel)), url


# ================================================================ 원안 입력 (D-21)
def _money_promo(con):
    """금액 구간(정액)이 있는 프로모션과 정률 시책 하나씩."""
    tier = con.execute("""SELECT t.promo_id, t.tier_no FROM promotion_tier t WHERE t.reward_kind='fixed' ORDER BY t.promo_id, t.tier_no LIMIT 1""").fetchone()
    rate = con.execute("SELECT promo_id, seq FROM promotion_rate ORDER BY promo_id, seq LIMIT 1").fetchone()
    return tier, rate


def test_original_plan_only_main_with_csrf(env):
    path = copy_db(env, "orig")
    con = db.connect(path)
    tier, rate = _money_promo(con)
    assert tier and rate
    main = login(env, "admin", path)
    html = main.get(f"/promo/tiers?edit={tier['promo_id']}").text
    assert 'id="original"' in html and f'name="o_tier_{tier["tier_no"]}"' in html
    # 관리자와 설계사는 화면도 POST 도 403
    for c in (login_agent(env, env["mgr"], path), login_agent(env, env["who"], path)):
        assert c.get(f"/promo/tiers?edit={tier['promo_id']}").status_code == 403
        r = c.post("/promo/tiers", data={"action": "original", "promo_id": str(tier["promo_id"]), f"o_tier_{tier['tier_no']}": "999000"})
        assert r.status_code == 403
    # CSRF 토큰이 없으면 403
    r = main.post("/promo/tiers", data={"action": "original", "promo_id": str(tier["promo_id"]), f"o_tier_{tier['tier_no']}": "777000"}, csrf=False)
    assert r.status_code == 403
    pm.ensure(con)
    assert con.execute("SELECT original_value FROM promotion_tier WHERE promo_id=? AND tier_no=?", (tier["promo_id"], tier["tier_no"])).fetchone()[0] is None
    # 메인 관리자는 넣는다. 변경 기록에 남는다
    r = main.post("/promo/tiers", data={"action": "original", "promo_id": str(tier["promo_id"]), f"o_tier_{tier['tier_no']}": "123000"})
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert con.execute("SELECT original_value FROM promotion_tier WHERE promo_id=? AND tier_no=?", (tier["promo_id"], tier["tier_no"])).fetchone()[0] == 123000
    assert con.execute("SELECT 1 FROM audit_log WHERE action='promotion_original' AND actor='admin'").fetchone()
    # 정률 시책의 지급률은 퍼센트로 받아 비율로 남긴다
    r = main.post("/promo/tiers", data={"action": "original", "promo_id": str(rate["promo_id"]), f"o_rate_{rate['seq']}": "150"})
    assert r.status_code == 303
    got = con.execute("SELECT original_rate FROM promotion_rate WHERE promo_id=? AND seq=?", (rate["promo_id"], rate["seq"])).fetchone()[0]
    assert abs(got - 1.5) < 1e-9
    assert 'value="150"' in main.get(f"/promo/tiers?edit={rate['promo_id']}").text
    con.close()


# ================================================================ 결과 확정
def _ended(con):
    p = pm.ended(con, ref=date.today())
    assert p
    return p[0]


def test_confirm_only_main_with_warning_step(env):
    path = copy_db(env, "confirm")
    con = db.connect(path)
    p = _ended(con)
    pid, ym = p["promo_id"], p["period_ym"]
    url = f"/promo/archive?ym={ym}&p={pid}"
    main = login(env, "admin", path)
    html = main.get(url).text
    assert "결과 확정" in html and 'name="agree"' not in html                        # 첫 화면에는 단추만, 확인 칸은 다음 단계
    assert "확정 기능은 다음 단계에서" not in html
    ask = main.get(url + f"&confirm={pid}").text
    assert "되돌릴 수 없습니다" in ask and 'name="agree" value="yes" required' in ask
    # 관리자와 설계사에게는 단추가 없고 POST 는 403
    for c in (login_agent(env, env["mgr"], path), login_agent(env, env["who"], path)):
        page_html = c.get(url + f"&confirm={pid}").text
        assert "결과 확정" not in page_html and 'name="agree"' not in page_html
        assert c.post("/promo/archive", data={"action": "confirm", "promo_id": str(pid), "ym": ym, "agree": "yes"}).status_code == 403
    assert pm.confirmation(con, pid) is None
    # CSRF 가 없으면 403, 확인 칸이 없으면 확정하지 않는다
    assert main.post("/promo/archive", data={"action": "confirm", "promo_id": str(pid), "ym": ym, "agree": "yes"}, csrf=False).status_code == 403
    r = main.post("/promo/archive", data={"action": "confirm", "promo_id": str(pid), "ym": ym})
    assert r.status_code == 303 and "err=" in r.headers["location"] and f"confirm={pid}" in r.headers["location"]
    assert pm.confirmation(con, pid) is None
    # 확정한다
    r = main.post("/promo/archive", data={"action": "confirm", "promo_id": str(pid), "ym": ym, "agree": "yes"})
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    head = pm.confirmation(con, pid)
    assert head and head["confirmed_by"] == "admin"
    html = main.get(url).text
    assert 'data-settlement="confirmed"' in html and "에 확정했습니다" in html
    name = con.execute("SELECT COALESCE(display_name, login_id) FROM user_account WHERE login_id='admin'").fetchone()[0]
    assert f"{name} 님이" in html
    t = head["confirmed_at"]
    assert f"{int(t[5:7])}월 {int(t[8:10])}일 {int(t[11:13])}시 {t[14:16]}분" in html
    assert 'href="/promo/archive?ym=' + ym + "&p=" + str(pid) + "&confirm=" not in html               # 확정한 뒤에는 단추가 없다
    # 다시 확정하면 막힌다
    r = main.post("/promo/archive", data={"action": "confirm", "promo_id": str(pid), "ym": ym, "agree": "yes"})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert con.execute("SELECT COUNT(*) FROM promotion_confirm WHERE promo_id=?", (pid,)).fetchone()[0] == 1
    # 관리자는 확정 표시를 보되 팀원의 시상 금액은 보지 않는다 (D-17)
    mgr_html = login_agent(env, env["mgr"], path).get(url).text
    assert 'data-settlement="confirmed"' in mgr_html and "확정 시상" not in mgr_html and "예상 시상" not in mgr_html
    con.close()


def test_confirmed_values_stay_fixed(env):
    path = copy_db(env, "frozen")
    con = db.connect(path)
    p = _ended(con)
    pid, ym = p["promo_id"], p["period_ym"]
    everyone = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active'")]
    before = {x["agent_id"]: (x["value"], x["outcome"]) for x in pm.final_results(con, p, everyone)["rows"]}
    top = max(before, key=lambda a: before[a][0])
    assert before[top][0] > 0
    main = login(env, "admin", path)
    assert main.post("/promo/archive", data={"action": "confirm", "promo_id": str(pid), "ym": ym, "agree": "yes"}).status_code == 303
    # 확정한 뒤 그 사람의 기간 안 계약을 모두 철회로 바꾼다. 다시 계산하면 값이 바뀌지만 확정 결과는 그대로다
    con.execute("UPDATE contract SET status='withdrawn' WHERE agent_id=? AND status IN ('inforce','lapsed','cancelled','matured')", (top,))
    con.commit()
    fresh = pm.progress(con, pm._load(con, pid), [top], date.today())[top]["value"]
    assert fresh != before[top][0]
    after = {x["agent_id"]: (x["value"], x["outcome"]) for x in pm.final_results(con, pm._load(con, pid), everyone)["rows"]}
    assert after == before
    html = main.get(f"/promo/archive?ym={ym}&p={pid}").text
    assert 'data-settlement="confirmed"' in html
    con.close()


# ================================================================ 기존 D-05, D-17 시험이 새 화면을 연다
def test_d05_d17_sweeps_cover_new_screens():
    """tests/test_s1_decisions.py 의 화면 훑기는 그 역할의 메뉴 화면을 모두 연다. 새 두 화면은 이제 기획 안내가 아니라 실제 화면으로 훑인다."""
    from tests import test_s1_decisions as s1
    items = {it["path"]: it for role in ("main", "manager", "agent") for it in s1._from_menu(role)}
    for path in ("/perf/persistency", "/my/persistency"):
        assert path in items and items[path]["status"] == "built", path
