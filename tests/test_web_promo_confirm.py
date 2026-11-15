"""S3 프로모션 확정 기능의 화면: 확정 무르기, 확정 이력, 순위 칸, 여행 시상 명단, 13회차 판정, 창별 구간표와 13회차 조건 입력.

| 무엇 | 보는 것 |
| --- | --- |
| 무르기 (D-50 가정) | 메인 관리자만 한다. 사유 5자 이상, 확인 칸, CSRF 를 거친다. 관리자와 설계사의 POST 는 403 이다 |
| 무른 뒤 | 무름 표시, 다시 계산한 예상, 다시 확정 단추가 보인다. 무른 사유와 확정 이력은 메인 관리자에게만 보인다 |
| 순위 | 표의 순위 칸이 엔진이 재직자 전체에서 매긴 순위와 같다 |
| 여행 명단 (D-20 가정) | 메인 관리자는 전체, 관리자는 본인과 직속 팀원, 설계사는 본인만 본다. 금액 칸이 없다 |
| 13회차 (D-17) | 관리자에게는 금액 칸을 떼고 넘긴다. 계약은 증권번호 끝 네 자리, 도래일, 판정만 넘긴다. 판정 확정은 메인 관리자만, 확인 단계를 거친다 |
| 구간 입력 | 창별 구간표와 13회차 조건을 넣고 고친다. 창별 구간표는 결합 규칙이 창마다 달성일 때만 된다 |
| 이름표 | 새 행위 코드에 한국어 이름표가 있다 |

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 는 건드리지 않는다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다. 화면 주소와 번호만 넣는다.
"""
import calendar
import os
import pathlib
import re
import shutil
import tempfile
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import promotion as pm
from app.web import audit_view, auth

PW = "confirm-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
REASON = "정산 기준이 바뀌어 다시 계산합니다"


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


def _ctx(con, login_id):
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0]
    return auth.context(con, uid)


def _login_of(con, agent_id):
    return con.execute("SELECT login_id FROM user_account WHERE agent_id=?", (agent_id,)).fetchone()[0]


@pytest.fixture(scope="module")
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="promoconfirm-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    managers = []
    for (login_id,) in con.execute("SELECT login_id FROM user_account WHERE agent_id IS NOT NULL AND status='active' ORDER BY login_id").fetchall():
        c = _ctx(con, login_id)
        if c and c["role"] == "manager":
            managers.append(login_id)
    con.close()
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "app": app, "managers": managers}
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


def login(env, login_id, path):
    c = Client(env["app"], path)
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, (login_id, r.status_code)
    return c


def _ended(con, inst13=None):
    items = [p for p in pm.ended(con, ref=date.today()) if inst13 is None or p["inst13"] == inst13]
    assert items
    return items[0]


def _member_with_rows(con, p, mctx):
    """관리자 범위 안의 팀원 가운데 그 프로모션의 대상인 사람 한 명."""
    rows = pm.final_results(con, p, sorted(mctx["agent_ids"] - {mctx["agent_id"]}), date.today())["rows"]
    return rows[0]["agent_id"] if rows else None


def _manager_with_team(env, con, p):
    for login_id in env["managers"]:
        c = _ctx(con, login_id)
        a = _member_with_rows(con, p, c)
        if a:
            return login_id, c, a
    pytest.fail("팀원이 대상인 관리자가 없다")


def _post(c, pid, ym, action, **extra):
    return c.post("/promo/archive", data={"action": action, "promo_id": str(pid), "ym": ym, **extra})


# ================================================================ 확정 무르기
def test_revoke_only_main_with_reason_confirm_step_and_csrf(env):
    path = copy_db(env, "revoke")
    con = db.connect(path)
    p = _ended(con)
    pid, ym = p["promo_id"], p["period_ym"]
    url = f"/promo/archive?ym={ym}&p={pid}"
    main = login(env, "admin", path)
    assert _post(main, pid, ym, "confirm", agree="yes").status_code == 303
    assert pm.confirmation(con, pid)
    html = main.get(url).text
    assert f"&revoke={pid}#revoke" in html and 'name="reason"' not in html             # 첫 화면에는 단추만, 사유 칸은 다음 단계
    ask = main.get(url + f"&revoke={pid}").text
    assert 'name="reason"' in ask and f'minlength="{pm.REVOKE_MIN}"' in ask and 'name="agree" value="yes" required' in ask
    # 관리자와 설계사에게는 단추도 단계도 없고 POST 는 403 이다
    mgr_login, _, member = _manager_with_team(env, con, p)
    for who in (mgr_login, _login_of(con, member)):
        c = login(env, who, path)
        page_html = c.get(url + f"&revoke={pid}").text
        assert "확정 무르기" not in page_html and 'name="reason"' not in page_html
        assert _post(c, pid, ym, "revoke", agree="yes", reason=REASON).status_code == 403
    assert pm.confirmation(con, pid)
    # CSRF 가 없으면 403
    assert main.post("/promo/archive", data={"action": "revoke", "promo_id": str(pid), "ym": ym, "agree": "yes", "reason": REASON},
                     csrf=False).status_code == 403
    # 확인 칸이 없으면 무르지 않고 단계로 돌아간다
    r = _post(main, pid, ym, "revoke", reason=REASON)
    assert r.status_code == 303 and "err=" in r.headers["location"] and f"revoke={pid}" in r.headers["location"]
    # 사유가 짧으면 무르지 않는다
    r = _post(main, pid, ym, "revoke", agree="yes", reason="짧음 ")
    assert r.status_code == 303 and "err=" in r.headers["location"] and f"revoke={pid}" in r.headers["location"]
    assert pm.confirmation(con, pid)
    # 무른다
    r = _post(main, pid, ym, "revoke", agree="yes", reason=REASON)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert pm.confirmation(con, pid) is None
    hist = pm.confirm_history(con, pid)
    assert len(hist) == 1 and not hist[0]["live"] and hist[0]["reason"] == REASON and hist[0]["revoked_by"] == "admin"
    assert con.execute("SELECT 1 FROM audit_log WHERE action='promotion_revoke' AND actor='admin'").fetchone()
    # 무른 뒤: 무름 표시, 다시 계산한 예상, 다시 확정 단추
    html = main.get(url).text
    assert 'data-revoked="1"' in html and 'data-settlement="estimate"' in html and pm.REVOKED_NOTE in html
    assert "다시 확정" in html and f"&confirm={pid}#confirm" in html
    # 다시 확정하면 새 확정 줄이 생기고 이력에 둘이 남는다
    assert _post(main, pid, ym, "confirm", agree="yes").status_code == 303
    assert pm.confirmation(con, pid) and len(pm.confirm_history(con, pid)) == 2
    con.close()


def test_revoked_reason_and_history_only_for_main(env):
    path = copy_db(env, "history")
    con = db.connect(path)
    p = _ended(con)
    pid, ym = p["promo_id"], p["period_ym"]
    url = f"/promo/archive?ym={ym}&p={pid}"
    main = login(env, "admin", path)
    before = main.get(url).text
    assert 'data-history="1"' in before and "아직 확정한 적이 없습니다" in before
    pm.confirm_results(con, pid, "admin", date.today())
    pm.revoke_confirmation(con, pid, "admin", REASON)
    html = main.get(url).text
    assert "확정 이력" in html and 'data-history="1"' in html and 'data-reason="1"' in html and REASON in html
    r = main.get(url)
    hist = r.context["history"]
    assert len(hist) == 1 and hist[0]["reason"] == REASON and not hist[0]["live"]
    mgr_login, _, member = _manager_with_team(env, con, p)
    for who, view in ((mgr_login, ""), (mgr_login, "&view=me"), (_login_of(con, member), "")):
        r = login(env, who, path).get(url + view)
        assert r.status_code == 200
        assert REASON not in r.text and "확정 이력" not in r.text and 'data-history="1"' not in r.text and 'data-reason="1"' not in r.text, (who, view)
        assert r.context["history"] == []
        for res in r.context["results"]:                               # 화면으로 넘기는 값에도 사유와 무른 사람이 없다
            assert not res["revoked"] or ("reason" not in res["revoked"] and "revoked_by" not in res["revoked"])
    # 관리자의 팀원 결과에는 무름 표시만 보인다
    mgr_html = login(env, mgr_login, path).get(url).text
    assert 'data-revoked="1"' in mgr_html
    con.close()


def test_rank_column_matches_engine(env):
    path = copy_db(env, "rank")
    con = db.connect(path)
    p = _ended(con)
    pid, ym = p["promo_id"], p["period_ym"]
    html = login(env, "admin", path).get(f"/promo/archive?ym={ym}&p={pid}").text
    assert '<th class="num hide-sm">순위</th>' in html
    everyone = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active'")]
    want = {x["code"]: x["rank"] for x in pm.final_results(con, p, everyone, date.today())["rows"]}
    got = {code: (int(rank) if rank else None) for code, rank in re.findall(r'<tr data-code="([^"]*)"><td class="num hide-sm" data-rank="([^"]*)">', html)}
    assert got and got == want
    assert pm.RANK_NOTE in html
    con.close()


# ================================================================ 여행 시상 명단 (D-20)
def _travel(con):
    """샘플 자료로 끝난 여행 시상 하나를 만든다. 낮은 구간은 물품, 높은 구간은 여행이다."""
    today = date.today()
    last_day = con.execute("SELECT MAX(contract_date) FROM contract").fetchone()[0]          # 샘플 계약이 있는 마지막 달. 끝나지 않았으면 그 앞 달
    y, m = int(last_day[:4]), int(last_day[5:7])
    if date(y, m, calendar.monthrange(y, m)[1]) >= today:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    first, last = f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
    vals = sorted(v for (v,) in con.execute("""SELECT SUM(premium_monthly) FROM contract WHERE contract_date BETWEEN ? AND ?
                                                AND status IN ('inforce','lapsed','cancelled','matured') GROUP BY agent_id""", (first, last)) if v)
    assert len(vals) >= 4
    low, high = vals[len(vals) // 4], vals[len(vals) * 3 // 4]
    return pm.create(con, period_ym=f"{y}{m:02d}", name="여행 명단 시험", kind="travel", windows=[("한 달", first, last)],
                     tiers=[(low, "goods", None, "건강검진권"), (high, "travel", None, "오사카 3박 4일")], by="admin", publish=True)


def _codes(html, attr):
    return set(re.findall(rf'{attr}="([^"]*)"', html))


def test_travel_roster_scope_and_no_amount(env):
    path = copy_db(env, "roster")
    con = db.connect(path)
    pid = _travel(con)
    p = pm._load(con, pid)
    ym = p["period_ym"]
    url = f"/promo/archive?ym={ym}&p={pid}"
    today = date.today()
    everyone = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active'")]
    achievers = {x["code"] for x in pm.travel_roster(con, p, _ctx(con, "admin"), everyone, today)["rows"]}
    assert len(achievers) >= 3
    r = login(env, "admin", path).get(url)
    main_html = r.text
    assert 'data-roster="1"' in main_html and _codes(main_html, "data-roster-code") == achievers
    assert "오사카 3박 4일" in main_html
    head = main_html.split('data-roster="1"', 1)[1].split("</tr>", 1)[0]
    assert "금액" not in head and "시상" not in head                                         # 금액 칸이 없다
    assert all("amount" not in x for x in r.context["roster"]["rows"])
    narrower = 0
    for login_id in env["managers"]:
        mctx = _ctx(con, login_id)
        want = {x["code"] for x in pm.travel_roster(con, p, mctx, mctx["agent_ids"], today)["rows"]}
        in_scope = {x["code"] for x in pm.travel_roster(con, p, _ctx(con, "admin"), mctx["agent_ids"], today)["rows"]}
        html = login(env, login_id, path).get(url).text
        assert _codes(html, "data-roster-code") == want, login_id
        # 결과 표도 D-20 범위다. 직속이 아닌 사람의 달성 여부는 보이지 않는다
        visible = set(pm.roster_viewable(con, mctx, mctx["agent_ids"], ym))
        codes = {r[0]: r[1] for r in con.execute("SELECT agent_id, internal_code FROM agent")}
        assert _codes(html, "data-code") <= {codes[a] for a in visible}, login_id
        if want < in_scope:
            narrower += 1
    assert narrower, "범위 안이어도 직속이 아니라 명단에서 빠지는 관리자가 있어야 이 시험이 뜻이 있다"
    # 설계사는 본인만
    agent = next(a for a in everyone if pm.roster_viewable(con, {"role": "agent", "agent_id": a}, everyone, ym) and
                 next((x for x in pm.travel_roster(con, p, _ctx(con, "admin"), [a], today)["rows"]), None) and
                 _ctx(con, _login_of(con, a))["role"] == "agent")
    ahtml = login(env, _login_of(con, agent), path).get(url).text
    own = {r[0] for r in con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (agent,))}
    assert _codes(ahtml, "data-roster-code") <= own and _codes(ahtml, "data-code") <= own
    assert not (_codes(ahtml, "data-roster-code") | _codes(ahtml, "data-code")) & (achievers - own)
    con.close()


# ================================================================ 13회차 판정 (D-17)
def _inst13_ready(con):
    """13회차 조건이 붙은 끝난 프로모션을 확정한다. 판정할 달성자가 있는 것을 고른다."""
    for p in pm.ended(con, ref=date.today()):
        if not p["inst13"]:
            continue
        pm.confirm_results(con, p["promo_id"], "admin", date.today())
        rows = pm.inst13_results(con, p, [r[0] for r in con.execute("SELECT agent_id FROM agent")], date.today())["rows"]
        if len(rows) < 2:
            continue
        # 샘플 계약은 아직 13회차가 오지 않아 모두 판정 전이다. 한 사람은 13회차까지 낸 것으로, 한 사람은 실효로 바꿔 지급과 환수를 만든다
        paid, gone = rows[0]["agent_id"], rows[1]["agent_id"]
        snap = lambda a, pid=p["promo_id"]: [r[0] for r in con.execute(
            "SELECT contract_id FROM promotion_result_contract WHERE promo_id=? AND agent_id=?", (pid, a))]
        con.executemany("UPDATE contract SET last_paid_ym='209912' WHERE contract_id=?", [(c,) for c in snap(paid)])
        con.executemany("UPDATE contract SET status='lapsed' WHERE contract_id=?", [(c,) for c in snap(gone)])
        con.commit()
        return pm._load(con, p["promo_id"])
    pytest.fail("판정할 달성자가 있는 13회차 프로모션이 없다")


def test_inst13_hides_amounts_from_manager_and_keeps_three_contract_fields(env):
    path = copy_db(env, "inst13")
    con = db.connect(path)
    p = _inst13_ready(con)
    pid, ym = p["promo_id"], p["period_ym"]
    url = f"/promo/archive?ym={ym}&p={pid}"
    main = login(env, "admin", path).get(url)
    i13 = main.context["sel"]["i13"]
    assert i13["available"] and i13["rows"] and i13["money"]
    assert all("amount" in y and "clawback_amount" in y for y in i13["rows"])
    assert "환수 금액" in main.text and "결과 금액" in main.text and 'data-inst13="1"' in main.text
    keep = set(("policy_tail", "due_date", "judgement", "judgement_ko"))
    for y in i13["rows"]:
        for c in y["contracts"]:
            assert set(c) == keep
    # 관리자: 판정은 보이고 금액 칸은 화면에도, 넘기는 값에도 없다
    seen_rows = 0
    for login_id in env["managers"]:
        r = login(env, login_id, path).get(url)
        sel = r.context["sel"]
        if not sel or not sel.get("i13"):
            continue
        m13 = sel["i13"]
        assert not m13["money"]
        for y in m13["rows"]:
            assert not set(y) & set(("amount", "amount_kept", "original_amount", "original_amount_kept", "clawback_amount", "reward_kept")), login_id
            assert all(set(c) == keep for c in y["contracts"])
        assert not set(m13["summary"]) & {"amount", "amount_kept", "clawback_amount"}
        assert "환수 금액" not in r.text and "결과 금액" not in r.text and "유지 계약 기준" not in r.text
        assert "확정 시상" not in r.text and "예상 시상" not in r.text
        assert "13회차 판정 남기기" not in r.text
        seen_rows += len(m13["rows"])
    assert seen_rows, "팀원의 13회차 판정을 보는 관리자가 있어야 이 시험이 뜻이 있다"
    # 설계사: 본인 것만
    mine_ids = {y["agent_id"] for y in i13["rows"]}
    shown = 0
    for a in sorted(mine_ids):
        lid = _login_of(con, a)
        if _ctx(con, lid)["role"] != "agent":
            continue
        r = login(env, lid, path).get(f"/promo/archive?ym={ym}")
        res = next(x for x in r.context["results"] if x["promo"]["promo_id"] == pid)
        assert {y["agent_id"] for y in res["i13"]["rows"]} == {a}
        assert all(not set(y) & {"original_amount", "original_amount_kept"} for y in res["i13"]["rows"])
        shown += 1
        if shown >= 2:
            break
    assert shown
    con.close()


def test_confirm_inst13_only_main_with_confirm_step(env):
    path = copy_db(env, "inst13confirm")
    con = db.connect(path)
    p = _inst13_ready(con)
    pid, ym = p["promo_id"], p["period_ym"]
    url = f"/promo/archive?ym={ym}&p={pid}"
    main = login(env, "admin", path)
    r = main.get(url)
    ready = r.context["sel"]["i13"]["ready"]
    count = lambda: con.execute("SELECT COUNT(*) FROM promotion_inst13 WHERE promo_id=?", (pid,)).fetchone()[0]
    assert ready >= 2, "지급과 환수가 정해진 사람이 있어야 한다"
    decisions = {y["decision"] for y in r.context["sel"]["i13"]["rows"]}
    assert {"pay", "clawback"} <= decisions
    assert f"&inst13={pid}#inst13-confirm" in r.text and 'value="inst13"' not in r.text
    ask = main.get(url + f"&inst13={pid}").text
    assert 'value="inst13"' in ask and 'name="agree" value="yes" required' in ask
    for login_id in env["managers"][:2]:
        assert _post(login(env, login_id, path), pid, ym, "inst13", agree="yes").status_code == 403
    assert main.post("/promo/archive", data={"action": "inst13", "promo_id": str(pid), "ym": ym, "agree": "yes"}, csrf=False).status_code == 403
    r = _post(main, pid, ym, "inst13")
    assert r.status_code == 303 and "err=" in r.headers["location"] and f"inst13={pid}" in r.headers["location"]
    assert count() == 0
    r = _post(main, pid, ym, "inst13", agree="yes")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert count() == ready
    after = main.get(url).context["sel"]["i13"]
    assert after["ready"] == 0 and sum(1 for y in after["rows"] if y["settlement"] == "confirmed") == ready
    assert con.execute("SELECT 1 FROM audit_log WHERE action='promotion_inst13' AND actor='admin'").fetchone()
    con.close()


def test_inst13_waits_for_confirmation(env):
    path = copy_db(env, "inst13wait")
    con = db.connect(path)
    p = _ended(con, inst13=True)
    r = login(env, "admin", path).get(f"/promo/archive?ym={p['period_ym']}&p={p['promo_id']}")
    assert not r.context["sel"]["i13"]["available"] and "결과를 먼저 확정해야 13회차를 판정합니다" in r.text
    con.close()


# ================================================================ 구간과 조건 입력
def _form(**kw):
    base = {"name": "창별 시험", "kind": "linked", "metric": "premium_monthly", "date_basis": pm.DEFAULT_DATE_BASIS, "combine_rule": "all",
            "w_label_1": "1주", "w_from_1": "2026-10-01", "w_to_1": "2026-10-07", "w_label_2": "2주", "w_from_2": "2026-10-08", "w_to_2": "2026-10-14",
            "t_min_1": "100000", "t_kind_1": "fixed", "t_value_1": "50000", "t_min_2": "300000", "t_kind_2": "fixed", "t_value_2": "150000",
            "window_mode": "1",
            "wt1_min_1": "100000", "wt1_kind_1": "fixed", "wt1_value_1": "0", "wt1_min_2": "200000", "wt1_kind_2": "fixed", "wt1_value_2": "0",
            "wt2_min_1": "150000", "wt2_kind_1": "fixed", "wt2_value_1": "0", "wt2_min_2": "300000", "wt2_kind_2": "fixed", "wt2_value_2": "0",
            "inst13_required": "1", "action": "draft"}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def test_window_tiers_and_inst13_condition_input(env):
    path = copy_db(env, "tiers")
    con = db.connect(path)
    main = login(env, "admin", path)
    form = main.get("/promo/tiers").text
    assert 'name="window_mode"' in form and 'name="wt2_min_1"' in form and 'name="inst13_required"' in form
    assert "결합 규칙이 창마다 달성일 때만 고를 수 있습니다" in form
    n0 = con.execute("SELECT COUNT(*) FROM promotion").fetchone()[0]
    # 합산 규칙에서는 창별 구간표를 고를 수 없다
    r = main.post("/promo/tiers", data=_form(combine_rule="sum"))
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert con.execute("SELECT COUNT(*) FROM promotion").fetchone()[0] == n0
    # 창마다 달성이면 들어간다
    r = main.post("/promo/tiers", data=_form())
    assert r.status_code == 303 and "msg=" in r.headers["location"], r.headers["location"]
    pid = con.execute("SELECT MAX(promo_id) FROM promotion").fetchone()[0]
    p = pm._load(con, pid)
    assert p["combine_rule"] == "all" and p["inst13_required"] and p["inst13"]
    assert sorted(p["window_tiers"]) == [1, 2]
    assert [t["min_value"] for t in pm.tiers_for(p, p["windows"][1])] == [150000, 300000]
    assert [t["min_value"] for t in p["tiers"]] == [100000, 300000]                    # 보상은 공통 구간표에서 읽는다
    # 고치기 폼에 창별 구간표와 13회차 조건이 그대로 채워진다
    edit = main.get(f"/promo/tiers?edit={pid}").text
    assert re.search(r'name="wt2_min_1"[^>]*value="150000"', edit) and re.search(r'name="window_mode" value="1"[^>]*checked', edit)
    assert re.search(r'name="inst13_required" value="1"[^>]*checked', edit)
    assert re.search(r'name="t_min_1"[^>]*value="100000"', edit) and not re.search(r'name="t_min_3"[^>]*value="\d', edit)
    # 창별 구간표를 끄고 13회차 조건을 지우면 공통 구간표만 남는다
    r = main.post("/promo/tiers", data=_form(promo_id=str(pid), window_mode=None, inst13_required=None, action="update"))
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    p = pm._load(con, pid)
    assert p["window_tiers"] == {} and not p["inst13_required"]
    log = con.execute("SELECT target FROM audit_log WHERE action='promotion_update' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert "window_tiers" in log and "inst13_required" in log
    # 관리자는 구간 입력 화면도 POST 도 쓰지 못한다
    mgr = login(env, env["managers"][0], path)
    assert mgr.get("/promo/tiers").status_code == 403
    assert mgr.post("/promo/tiers", data=_form()).status_code == 403
    con.close()


# ================================================================ 이름표와 문구
def test_audit_labels_for_new_actions(env):
    path = copy_db(env, "audit")
    con = db.connect(path)
    for code in ("promotion_confirm", "promotion_revoke", "promotion_inst13", "promotion_original"):
        assert audit_view.action_ko(code) != code, code
    p = _ended(con)
    names = audit_view._Names(con)
    assert audit_view.target_ko("promotion_revoke", f"{p['promo_id']}:7", names) == f"프로모션: {p['name']} · 확정 7번"
    assert audit_view.target_ko("promotion_confirm", f"{p['promo_id']}:12명", names).endswith("· 12명")
    assert audit_view.target_ko("promotion_original", f"{p['promo_id']}:rate1,tier2", names).endswith("원안 칸 지급률 1번, 2구간")
    assert "창별 구간표" in audit_view.target_ko("promotion_update", f"{p['promo_id']}:window_tiers,inst13_required", names)
    con.close()


def test_contract_upload_note_names_home_and_team():
    text = (pathlib.Path(__file__).resolve().parents[1] / "app/web/templates/data_contracts.html").read_text(encoding="utf-8")
    assert "홈과 팀원 현황의 숫자는 워커가 업적 run 을 마친 뒤 바뀝니다" in text
    assert "실적 화면의 숫자는 워커가" not in text
