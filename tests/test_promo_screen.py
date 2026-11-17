"""프로모션 화면: 조직별 진척(view=org), 문턱 카드, 내 달성 현황의 조건 확장, 보상 선택 기록, 구간과 조건 입력의 조건 확장.

| 무엇 | 보는 것 |
| --- | --- |
| 같은 숫자 | 조직별 보기의 합계가 팀원 달성 현황의 대상, 달성, 근접과 같다. 맨 아래 사람 목록도 같은 값이다 |
| 보는 범위 (가정) | 관리자에게는 금액 칸과 금액 값이 없고 범위 밖 조직이 없다. 범위 밖 조직을 열면 403 이다 |
| 설계사 | view=org 는 403 이고, 내 달성 현황에는 Convention 문턱 숫자만 보인다 |
| 보상 선택 | 메인 관리자만, CSRF 를 거쳐, 확정한 달성자에게만 남는다. 변경 기록에 사번으로 남는다 |
| 계층 이름 | 메인 관리자의 조직별 보기는 이터널총괄본부에서 시작하고 예하 본부 넷이 '본부' 로 보인다 |
| 조건 입력 | rules 칸을 넣고 고치면 엔진 값이 바뀌고 변경 기록 이름표에 '조건 확장' 이 보인다 |
| 표 모양 | 달마다 대상자 칸, 조건 1-1, 1-2, 2 묶음, 최종 달성. 시작 전 달은 숫자 없이 '시작 전' 이다 |
| 명단 | 표의 숫자를 누르면 나오는 명단의 인원이 그 숫자와 같고, 범위 밖 사람이 없고, 관리자 명단에 금액이 없다 |
| 브릿지 | 내 달성 현황의 상태, 남은 날, 다음 단계까지 남은 값이 엔진 결과와 같다 |
| 복사 | 브릿지 다음 달 복사는 메인 관리자만, CSRF 를 거쳐 초안을 만들고 옮기지 않은 값을 안내한다 |
| 판 이력 | DB생명 여행시상의 두 판과 바뀐 점, 카드의 변경 표시, 가중 인정 문구, 조건 1-2 안내 |

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 는 건드리지 않는다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import os
import pathlib
import re
import shutil
import tempfile
from datetime import date, timedelta
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import promotion as pm
from app.web import audit_view, auth, org_labels, promo_extra

PW = "screen-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
ETERNAL = "hq:직할본부 > branch:이터널"


class Client(TestClient):
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
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="promoscreen-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    con.close()
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "app": app}
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


def _ctx(con, login_id):
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0]
    return auth.context(con, uid)


def _promo(con, name):
    return con.execute("SELECT promo_id FROM promotion WHERE name=?", (name,)).fetchone()[0]


def _bonbu_head(con, bonbu="H본부"):
    """샘플 본부장의 계정. 조직장 지정으로 정해진 관리자다."""
    return con.execute("""SELECT u.login_id FROM org_manager m JOIN org_unit o ON o.org_id=m.org_id JOIN user_account u ON u.agent_id=m.agent_id
                          WHERE o.path_key=?""", (f"{ETERNAL} > center:{bonbu}",)).fetchone()[0]


def _eternal_fc(con, bonbu="H본부"):
    return con.execute("""SELECT u.login_id, a.agent_id FROM agent a JOIN agent_org_history h ON h.agent_id=a.agent_id JOIN org_unit o ON o.org_id=h.org_id
                          JOIN user_account u ON u.agent_id=a.agent_id
                          WHERE o.path_key LIKE ? AND a.job_type='fc' ORDER BY a.agent_id LIMIT 1""", (f"{ETERNAL} > center:{bonbu} > %",)).fetchone()


# ================================================================ 같은 숫자
@pytest.mark.parametrize("who", ["admin", "head"])
def test_org_totals_match_team_view(env, who):
    con = db.connect(env["db"])
    login_id = "admin" if who == "admin" else _bonbu_head(con)
    ctx = _ctx(con, login_id)
    c = login(env, login_id)
    root = con.execute("SELECT path_key FROM org_unit WHERE parent_id IS NULL").fetchone()[0]
    start = root if who == "admin" else con.execute("SELECT path_key FROM org_unit WHERE org_id=?", (ctx["heads"][0]["org_id"],)).fetchone()[0]
    names = ("흥국화재 BOOM-UP", "DB생명 여행시상", "DB생명 이터널 Convention 추가 시책", "삼성화재 드림+")
    for name in names:
        pid = _promo(con, name)
        team = c.get(f"/promo/progress?p={pid}")
        org = c.get(f"/promo/progress?view=org&p={pid}&org={quote(start)}")
        assert team.status_code == 200 and org.status_code == 200, pid
        s, t = team.context["summary"], org.context["data"]["total"]
        assert (t["n_target"], t["n_achieved"], t["n_near"]) == (s["n"], s["achieved"], s["near"]), (who, pid)
        assert t["value"] == pytest.approx(sum(x["r"]["value"] for x in team.context["rows"])), (who, pid)
    # 맨 아래 조직(지점)의 사람 목록은 팀원 달성 현황의 같은 사람과 같은 값이다
    pid = _promo(con, "DB생명 여행시상")
    leaf = con.execute("SELECT path_key FROM org_unit WHERE path_key LIKE ? ORDER BY path_key LIMIT 1", (f"{ETERNAL} > center:H본부 > team:%",)).fetchone()[0]
    page = c.get(f"/promo/progress?view=org&p={pid}&org={quote(leaf)}")
    team_rows = {x["agent_id"]: x["r"] for x in c.get(f"/promo/progress?p={pid}").context["rows"]}
    people = page.context["people"]
    assert people, leaf
    for x in people:
        assert x["r"]["value"] == team_rows[x["agent_id"]]["value"] and bool(x["r"]["tier"]) == bool(team_rows[x["agent_id"]]["tier"])
    con.close()


# ================================================================ 보는 범위
def test_manager_sees_no_amounts_and_nothing_outside_scope(env):
    con = db.connect(env["db"])
    head = _bonbu_head(con)
    ctx = _ctx(con, head)
    c = login(env, head)
    pid = _promo(con, "DB생명 여행시상")
    r = c.get(f"/promo/progress?view=org&p={pid}")
    assert r.status_code == 200
    data = r.context["data"]
    assert "amounts" not in data and r.context["show_amount"] is False and r.context["top"] is None
    assert "예상 시상" not in r.text and 'data-top-org="1"' not in r.text
    paths = {x["path_key"] for x in data["rows"]}
    allowed = {x[0] for x in con.execute(f"SELECT path_key FROM org_unit WHERE org_id IN ({','.join('?' * len(ctx['org_ids']))})",
                                         tuple(ctx["org_ids"]))}
    assert paths and paths <= allowed
    # 다른 본부와 맨 위 조직은 열 수 없다
    for other in (f"{ETERNAL} > center:금탑본부", ETERNAL):
        assert c.get(f"/promo/progress?view=org&p={pid}&org={quote(other)}").status_code == 403, other
    assert con.execute("SELECT 1 FROM audit_log WHERE actor=? AND action='access_denied' AND target='/promo/progress org'", (head,)).fetchone()
    # 사람 목록에도 금액 칸이 없다
    leaf = con.execute("SELECT path_key FROM org_unit WHERE path_key LIKE ? ORDER BY path_key LIMIT 1", (f"{ETERNAL} > center:H본부 > team:%",)).fetchone()[0]
    page = c.get(f"/promo/progress?view=org&p={pid}&org={quote(leaf)}")
    assert page.status_code == 200 and page.context["show_amount"] is False and "예상 시상" not in page.text
    con.close()


def test_main_starts_at_eternal_with_four_bonbu_and_sees_top_org(env):
    con = db.connect(env["db"])
    c = login(env, "admin")
    pid = _promo(con, "흥국화재 BOOM-UP")
    r = c.get(f"/promo/progress?view=org&p={pid}")
    assert r.context["org"]["path_key"] == ETERNAL and org_labels.title(r.context["org"]) == "이터널총괄본부"
    rows = [x for x in r.context["data"]["rows"] if not x["direct"]]
    assert sorted(x["name"] for x in rows) == sorted(["해온본부", "금탑본부", "H본부", "피닉스본부"])
    assert {x["level_ko"] for x in rows} == {"본부"} and "이터널총괄본부" in r.text
    assert "amounts" in r.context["data"]
    # 최다 달성 조직: Convention 은 딸린 시책이라 부모(DB생명 여행시상)의 달성 인원으로 견준다
    conv = c.get(f"/promo/progress?view=org&p={_promo(con, 'DB생명 이터널 Convention 추가 시책')}")
    top = conv.context["top"]
    assert top and top["rows"] and all(x["rank"] == 1 for x in top["rows"]) and 'data-top-org="1"' in conv.text and top["by_parent"]
    # 한 단계 내려가면 지점이다
    down = c.get(f"/promo/progress?view=org&p={pid}&org={quote(ETERNAL + ' > center:H본부')}")
    kids = [x for x in down.context["data"]["rows"] if not x["direct"]]
    assert kids and {x["level_ko"] for x in kids} == {"지점"}
    assert [x["org_id"] for x in down.context["crumbs"]][0] == r.context["org"]["org_id"]
    con.close()


# ================================================================ 설계사
def test_agent_cannot_open_org_view_and_sees_gate_numbers_only(env):
    con = db.connect(env["db"])
    login_id, aid = _eternal_fc(con)
    c = login(env, login_id)
    assert c.get("/promo/progress?view=org").status_code == 403
    me = c.get("/promo/progress")
    assert me.status_code == 200
    gates = me.context["gates"]
    assert gates and gates[0]["min"] == 15 and gates[0]["n"] < 15
    assert f"이터널총괄본부 전체 달성 {gates[0]['n']}명 / 15명" in me.text
    assert set(gates[0]) <= {"org_path", "org_name", "min", "n", "open", "left", "ym", "note", "promo_id", "name", "org_title"}
    # 조건을 묶는 결합은 두 조건과 결합 문구를 보인다
    assert 'data-paths="both"' in me.text and 'data-paths="either"' in me.text
    assert pm.COMBINE_KO and "모두 채워야" in me.text and "하나만 채우면" in me.text
    con.close()


# ================================================================ 보상 선택 기록
def test_reward_choice_main_only_with_csrf_and_audit(env):
    path = copy_db(env, "choice")
    con = db.connect(path)
    pid = _promo(con, "삼성화재 드림+ 지난 차수")
    assert pm.confirmation(con, pid)
    url = f"/promo/archive?ym=202609&p={pid}"
    main = login(env, "admin", path)
    page = main.get(url)
    choice = page.context["choice"]
    assert choice and choice["ready"] and choice["rows"] and 'data-choice-form="1"' in page.text
    aid = choice["rows"][0]["agent_id"]
    code = choice["rows"][0]["code"]
    data = {"action": "reward_choice", "promo_id": str(pid), "ym": "202609", f"choice_{aid}": "travel1", f"attend_{aid}": "yes"}
    # 관리자와 설계사는 403
    head = _bonbu_head(con)
    fc, _ = _eternal_fc(con)
    for who in (head, fc):
        assert login(env, who, path).post("/promo/archive", data=data).status_code == 403, who
        assert 'data-choice-form="1"' not in login(env, who, path).get(url).text
    # CSRF 가 없으면 403
    assert main.post("/promo/archive", data=data, csrf=False).status_code == 403
    assert not promo_extra.choices(con, pid)
    # 달성자가 아닌 사람은 남지 않는다
    outsider = con.execute("SELECT agent_id FROM agent WHERE agent_id NOT IN (%s) LIMIT 1" % ",".join(str(x["agent_id"]) for x in choice["rows"])).fetchone()[0]
    r = main.post("/promo/archive", data={**data, f"choice_{outsider}": "gold"})
    assert r.status_code == 303 and "err=" in r.headers["location"] and not promo_extra.choices(con, pid)
    # 남긴다
    r = main.post("/promo/archive", data=data)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    got = promo_extra.choices(con, pid)
    assert got[aid]["choice"] == "travel1" and got[aid]["attend"] == "yes" and got[aid]["recorded_by"] == "admin"
    log = con.execute("SELECT * FROM audit_log WHERE action='promotion_reward_choice' ORDER BY id DESC LIMIT 1").fetchone()
    assert log and code in log["after_json"] and audit_view.action_ko("promotion_reward_choice") != "promotion_reward_choice"
    # 금을 고르면 참석 여부는 비운다
    main.post("/promo/archive", data={**data, f"choice_{aid}": "gold"})
    assert promo_extra.choices(con, pid)[aid]["attend"] is None
    assert promo_extra.CASH_NOTE in main.get(url).text
    con.close()


# ================================================================ 구간과 조건 입력
def test_tiers_form_saves_rules_and_audit_label(env):
    path = copy_db(env, "tiers")
    con = db.connect(path)
    pid = _promo(con, "DB생명 여행시상")
    main = login(env, "admin", path)
    form = main.get(f"/promo/tiers?edit={pid}").text
    assert 'data-rules-form="1"' in form and 'name="r_boost_factor"' in form and 'value="2"' in form
    p = pm._load(con, pid)
    data = {"promo_id": str(pid), "action": "update", "name": p["name"], "kind": p["kind"], "insurer_id": str(p["insurer_id"]), "sector": p["sector"],
            "metric": p["metric"], "date_basis": p["date_basis"], "combine_rule": p["combine_rule"], "categories": p["category_list"],
            "window_mode": "1", "r_count_basis": "issued", "r_boost_factor": "2", "r_boost_day_from": "1", "r_boost_day_to": "14", "r_boost_cap": "100000",
            "r_boost_cap_scope": "contract", "r_boost_applies": "each", "r_exclude_self": "1", "r_ret_min": "95", "r_exclude": "실손\n단체",
            "top_org_reward": "최다 달성 본부장 행사 동참"}
    for i, w in enumerate(p["windows"], start=1):
        data.update({f"w_label_{i}": w["label"], f"w_from_{i}": w["date_from"], f"w_to_{i}": w["date_to"]})
    for i, t in enumerate(p["tiers"], start=1):
        data.update({f"t_min_{i}": str(int(t["min_value"])), f"t_kind_{i}": t["reward_kind"], f"t_label_{i}": t["reward_label"] or "여행"})
    for seq, rows in p["window_tiers"].items():
        for i, t in enumerate(rows, start=1):
            data.update({f"wt{seq}_min_{i}": str(int(t["min_value"])), f"wt{seq}_kind_{i}": t["reward_kind"], f"wt{seq}_label_{i}": t["reward_label"] or "여행"})
    r = main.post("/promo/tiers", data=data)
    assert r.status_code == 303 and "/promo/publish?msg=" in r.headers["location"], r.headers.get("location")
    after = pm._load(con, pid)
    assert after["rules"]["retention_min"] == pytest.approx(0.95) and after["rules"]["product_exclude"] == ["실손", "단체"]
    assert after["rules"]["boost"]["applies_to"] == "each" and after["combine_rule"] == "either"
    assert promo_extra.screen(after)["top_org_reward"] == "최다 달성 본부장 행사 동참"
    log = con.execute("SELECT target FROM audit_log WHERE action='promotion_update' ORDER BY id DESC LIMIT 1").fetchone()
    shown = audit_view.target_ko("promotion_update", log["target"], audit_view._Names(con))
    assert "조건 확장" in shown
    assert con.execute("SELECT 1 FROM audit_log WHERE action='promotion_screen'").fetchone()
    # 관리자는 입력 화면을 열 수 없다
    assert login(env, _bonbu_head(con), path).get(f"/promo/tiers?edit={pid}").status_code == 403
    con.close()


# ================================================================ 표 모양과 명단
def _who_links(html):
    return re.findall(r'<a class="cnt" href="([^"]+)" data-who="([^"]+)" data-row="([^"]*)">(\d+)명</a>', html)


@pytest.mark.parametrize("who", ["admin", "head"])
def test_org_table_numbers_open_matching_rosters(env, who):
    """조직별 표의 인원 숫자마다 명단이 열리고, 명단의 인원이 그 숫자와 같다. 관리자 명단에는 범위 밖 사람과 금액이 없다."""
    con = db.connect(env["db"])
    login_id = "admin" if who == "admin" else _bonbu_head(con)
    ctx = _ctx(con, login_id)
    scope = set(ctx["agent_ids"]) if who == "head" else None
    c = login(env, login_id)
    checked = 0
    for name in ("흥국화재 BOOM-UP", "DB생명 여행시상", "삼성화재 드림+"):
        pid = _promo(con, name)
        page = c.get(f"/promo/progress?view=org&p={pid}")
        assert page.status_code == 200
        links = _who_links(page.text)
        assert links, name
        for href, key, row, n in links:
            r = c.get(href.replace("&amp;", "&"))
            assert r.status_code == 200, (name, key, row)
            roster = r.context["roster"]
            assert roster["n"] == int(n) == sum(len(g["people"]) for g in roster["groups"]), (name, key, row)
            ids = {x["agent_id"] for g in roster["groups"] for x in g["people"]}
            if scope is not None:
                assert ids <= scope, (name, key)
                assert "예상 시상" not in r.text
            checked += 1
    assert checked >= 20
    # 모르는 명단 열쇠는 404 다
    assert c.get(f"/promo/progress?view=org&p={_promo(con, 'DB생명 여행시상')}&who=nope").status_code == 404
    con.close()


def test_org_table_columns_follow_engine_phases(env):
    """DB생명 여행시상의 칸은 달마다 대상자, 조건 1-1, 1-2, 2 묶음, 최종 달성이다. 시작 전 달은 숫자가 없다. 칸 수는 엔진의 창과 추가안을 따른다."""
    con = db.connect(env["db"])
    c = login(env, "admin")
    pid = _promo(con, "DB생명 여행시상")
    r = c.get(f"/promo/progress?view=org&p={pid}")
    cols, data = r.context["phases"], r.context["data"]
    groups = [x["group_label"] for x in cols if x["gspan"]]
    assert groups[:3] == ["조건 1-1", "조건 1-2", "조건 2"] and cols[-1]["label"] == "최종 달성"
    tot = data["total"]
    assert sum(1 for x in cols if x["group"] == "main") == len(tot["phases"])
    assert sum(1 for x in cols if x["group"] == "alt") == 1 + len(tot["alt"]["phases"])
    for col, ph in zip([x for x in cols if x["group"] == "main"], tot["phases"]):
        cell = tot["cells"][cols.index(col)]
        if ph["state"] == "future":
            assert cell == "시작 전"
        else:
            assert cell.startswith(f"{ph['n_met']}명") and (("진행 중" in cell) == (ph["state"] == "open"))
    assert "합산 100만 달성자" in r.text and "연속으로 채우면 3개월 합계 70만원(10만, 20만, 40만), 합산으로는 100만원" in r.text
    con.close()


# ================================================================ 내 달성 현황: 판 이력, 가중 인정, 조건 1-2, 브릿지
def _code_agent(con, code):
    return con.execute("SELECT agent_id FROM agent WHERE internal_code=?", (code,)).fetchone()[0]


def test_my_view_versions_boost_and_alt(env):
    con = db.connect(env["db"])
    pid = _promo(con, "DB생명 여행시상")
    p = pm._load(con, pid)
    ref = date.today()
    ids = [a for (a,) in con.execute("SELECT agent_id FROM agent WHERE internal_code LIKE 'T01%'")]
    res = pm.progress(con, p, ids, ref)
    eligible = [a for a in ids if (res[a]["paths"].get("alt") or {}).get("eligible")]
    kept = [a for a in ids if res[a]["windows"][0]["tier"]]
    assert eligible and kept
    code = lambda a: con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (a,)).fetchone()[0]
    for a, want in ((eligible[0], True), (kept[0], False)):
        html = login(env, code(a)).get("/promo/progress").text
        section = html.split("DB생명 여행시상</div>", 1)[1].split("</section>", 1)[0]
        assert ("조건 1-2 대상입니다" in section or "조건 1-2로 달성했습니다" in section or "조건 1-2도 놓쳤습니다" in section) is want, a
        assert "10월과 12월의 1~2주차 가동은 2배로 인정합니다(원보험료 10만원까지). 11월은 2배 인정이 없습니다." in section
        assert 'data-versions="2"' in section and "1차 원안" in section and "2차 변경" in section and "조건 1-2 신설" in section
    lst = login(env, "admin").get("/promo/list").text
    assert 'data-revised="1"' in lst
    con.close()


def test_my_view_bridge_state_matches_engine(env):
    """브릿지 카드의 상태, 남은 날, 다음 단계까지 남은 값이 엔진(pm.progress 의 bridge)과 같다. 금액은 원문 확인이다."""
    path = copy_db(env, "bridge")
    con = db.connect(path)
    src = _promo(con, "삼성화재 10~11월 연속가동 브릿지")
    today = date.today()
    d = lambda n: (today + timedelta(days=n)).isoformat()
    # 오늘을 둘째 창 안에 두는 브릿지를 하나 더 만든다. 첫 창을 채운 사람은 진행 중, 놓친 사람은 무효다
    new = pm.copy_bridge(con, src, period_ym="203001", windows=[(d(-12), d(-2)), (d(-1), d(6))], by="admin", name="브릿지 시험")
    pm.set_status(con, new["promo_id"], "published", "admin")
    p = pm._load(con, new["promo_id"])
    sf = con.execute("SELECT product_id, insurer_id FROM product WHERE product_name='삼성화재 건강보험'").fetchone()
    fcs = [a for (a,) in con.execute("""SELECT agent_id FROM agent WHERE internal_code LIKE 'T01%' AND agent_id NOT IN
                                           (SELECT agent_id FROM contract WHERE insurer_id=?) ORDER BY agent_id LIMIT 2""", (sf["insurer_id"],))]
    for i, (a, v) in enumerate(zip(fcs, (150_000, 50_000))):
        con.execute("""INSERT INTO contract(insurer_id, policy_no, product_id, agent_id, application_no, application_date, contract_date, premium,
                       premium_monthly, converted_premium, policy_count, status, status_date, payment_mode, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,'inforce',?,'monthly',?)""",
                    (sf["insurer_id"], f"BRT{i}", sf["product_id"], a, f"BRTA{i}", d(-5), d(-5), v, v, v, d(-5), db.now()))
    con.commit()
    res = pm.progress(con, p, fcs, today)
    states = {res[a]["bridge"]["state"] for a in fcs}
    assert states == {"open", "void"}, states
    for a in fcs:
        b = res[a]["bridge"]
        code = con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (a,)).fetchone()[0]
        html = login(env, code, path).get("/promo/progress").text
        section = html.split("브릿지 시험</div>", 1)[1].split("</section>", 1)[0]
        assert f'data-bridge-state="{b["state"]}"' in section and b["state_ko"] in section
        if b["state"] == "open":
            assert f'data-days-left="{b["days_left"]}"' in section
            if b["next"]:
                assert f'data-bridge-gap="{int(b["next"]["gap"])}"' in section
        assert "원문 확인" in section and "예상" in section
    con.close()


# ================================================================ 브릿지 다음 달 복사
def test_copy_bridge_main_only_with_csrf(env):
    path = copy_db(env, "copy")
    con = db.connect(path)
    src = _promo(con, "삼성화재 10~11월 연속가동 브릿지")
    data = {"action": "copy_bridge", "promo_id": str(src), "period_ym": "2026-12", "c_from_1": "2026-12-16", "c_to_1": "2026-12-31",
            "c_from_2": "2027-01-01", "c_to_2": "2027-01-15", "name": "복사 시험"}
    main = login(env, "admin", path)
    assert 'data-copy-form="1"' in main.get(f"/promo/tiers?edit={src}").text
    for who in (_bonbu_head(con), _eternal_fc(con)[0]):
        assert login(env, who, path).post("/promo/tiers", data=data).status_code == 403, who
    assert main.post("/promo/tiers", data=data, csrf=False).status_code == 403
    assert not con.execute("SELECT 1 FROM promotion WHERE name='복사 시험'").fetchone()
    r = main.post("/promo/tiers", data=data)
    assert r.status_code == 303 and "/promo/tiers?edit=" in r.headers["location"]
    row = con.execute("SELECT promo_id, status, copied_from, period_ym FROM promotion WHERE name='복사 시험'").fetchone()
    assert row and row["status"] == "draft" and row["copied_from"] == src and row["period_ym"] == "202612"
    page = main.get(r.headers["location"])
    assert f'data-copied-from="{src}"' in page.text and "초안을 만들었습니다" in page.text
    assert con.execute("SELECT 1 FROM audit_log WHERE action='promotion_copy' AND target=?", (f"{row['promo_id']}:{src}",)).fetchone()
    # 같은 달로 다시 복사하면 엔진이 막고 화면은 오류를 보인다
    again = main.post("/promo/tiers", data=data)
    assert again.status_code == 303 and "err=" in again.headers["location"]
    con.close()
