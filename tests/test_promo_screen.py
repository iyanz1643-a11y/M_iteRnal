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
| 실적 금액 | 조직별 표의 칸마다 엔진이 낸 실적 합계가 실린다. 합산 칸의 머리는 지표 이름이고 흥국화재 BOOM-UP 은 인정보험료다 |
| 세 단계 | 현황, 본부 화면의 사람 전원, 개인 화면. 본부 표의 합이 현황 줄과 같고 개인 화면의 달별 값이 본부 표와 같다 |
| 개인 화면 | 범위 밖 사람은 403, 관리자에게는 예상 시상이 없고, 센 계약은 엔진이 센 계약과 같다 |
| 지표 고르기 | 구간과 조건 입력에서 보험료, 인정보험료, 건수를 고르고 게시 뒤에도 바꾼다 |
| 검증 뒤 고침 | 추가안 여러 개의 열쇠와 가중 여부와 구간 번호가 폼을 거쳐 그대로다. 퇴사자의 실적은 조직 합계에만 든다. 확정한 프로모션의 사람별 보기는 확정 값이다 |
| 화면 다듬기 | 기간 글이 겹치지 않고, 무효가 된 사람에게 남은 금액이 없고, 딸린 시책의 표가 부모와 같은 모양이고, 틀린 날짜의 오류가 한국어다 |
| 확정 값 | 확정한 뒤 계약을 철회로 바꿔도 조직 표, 명단, 사람 표, 개인 화면이 확정 값으로 서로 같다. 구간 근접 칸이 없고 미달성 아래에 무효와 진행 중이 있다 |
| 확정 값의 자세함 | 창별 값을 남기지 않은 확정은 달 칸과 창별 명단을 비우고 안내한다. 최종 달성과 합계는 확정 값 그대로다 |
| 센 계약 | 개인 화면의 계약 목록은 엔진의 counted_contracts 이고 합이 그 사람 값과 같다. 기준일 뒤 계약에는 표시가 붙는다 |

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
    assert "연속으로 채우면 3개월 합계 70만원(10만, 20만, 40만), 합산으로는 100만원" in r.text
    # 합산 칸의 머리는 지표 이름으로 만들고, 칸에는 합산 실적과 '100만 달성 n명' 을 둔다
    sum_col = next(x for x in cols if x["key"] == "sum")
    assert sum_col["label"] == "합산 보험료" and sum_col["sub"] == "기준 100만" and sum_col["pre"] == "100만 달성"
    assert 'data-col="sum">합산 보험료<span class="th-sub">기준 100만</span>' in r.text and '<span class="cell-pre">100만 달성</span>' in r.text
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


# ================================================================ 실적 금액, 본부 화면, 개인 화면 (대표님 지시, 11월 17일)
BOOM = "흥국화재 BOOM-UP"


def _bonbu(name):
    return f"{ETERNAL} > center:{name}"


def _amts(html, key):
    """표의 칸에 실린 금액(data-amt). 줄 순서대로."""
    return [int(x) for x in re.findall(rf'data-cell="{key}" data-amt="(-?\d+)"', html)]


def test_amount_text_shortens_without_rounding_up():
    from app.web.routes.promo import amount_text
    won, cnt = {"metric": "converted_premium"}, {"metric": "policy_count"}
    assert [amount_text(v, won) for v in (0, 9_999, 10_000, 399_999, 1_250_000, 1_234_560, 12_345_678, 252_000_000)] == \
        ["0원", "9,999원", "1만원", "39.9만원", "125만원", "123.4만원", "1,234만원", "25,200만원"]
    assert amount_text(None, won) is None and amount_text(3, cnt) == "3건" and amount_text(-25_000, won) == "-2.5만원"


def test_sample_boom_up_counts_converted_premium(env):
    """샘플의 흥국화재 BOOM-UP 은 인정보험료로 센다. 샘플 계약은 인정보험료가 보험료와 같아 달성자는 바뀌지 않는다."""
    con = db.connect(env["db"])
    p = pm._load(con, _promo(con, BOOM))
    assert p["metric"] == "converted_premium" and p["metric_ko"] == "인정보험료"
    assert not con.execute("SELECT 1 FROM contract WHERE policy_no LIKE 'PS%' AND converted_premium != premium_monthly").fetchone()
    con.close()


@pytest.mark.parametrize("who", ["admin", "head"])
def test_org_table_cells_carry_engine_sums(env, who):
    """칸마다 인원과 함께 엔진의 실적 합계가 실린다. 달 칸은 value_sum, 합산 칸은 sum_value 다. 관리자에게도 실적은 보이고 예상 시상은 없다."""
    con = db.connect(env["db"])
    login_id = "admin" if who == "admin" else _bonbu_head(con)
    c = login(env, login_id)
    pid = _promo(con, BOOM)
    r = c.get(f"/promo/progress?view=org&p={pid}")
    assert r.status_code == 200
    cols, data = r.context["phases"], r.context["data"]
    rows = [*data["rows"], data["total"]]
    assert [x["key"] for x in cols] == ["w1", "w2", "w3", "sum", "final"]
    sum_col = cols[3]
    assert sum_col["label"] == "합산 인정보험료" and sum_col["pre"] == "40만 달성"
    assert 'data-col="sum">합산 인정보험료<span class="th-sub">기준 40만</span>' in r.text
    assert "실적은 인정보험료 기준입니다. 기준일은 청약일자이고 청약 기준으로" in r.text and 'data-metric-note="1"' in r.text
    for x in rows:
        states = [ph["state"] for ph in x["phases"]]
        assert x["sums"][:3] == [ph["value_sum"] if st != "future" else None for ph, st in zip(x["phases"], states)]
        assert x["sums"][3] == x["sum_value"] and x["sums"][4] is None and x["n_missed"] == x["n_target"] - x["n_achieved"]
        assert x["sum_value"] == pytest.approx(sum(v for v in x["sums"][:3] if v is not None))     # 가중 인정이 없어 달의 합이 합산이다
    open_keys = [c2["key"] for c2, ph in zip(cols, data["total"]["phases"]) if ph["state"] != "future"]
    assert open_keys
    for i, key in enumerate(open_keys):
        assert _amts(r.text, key) == [int(x["sums"][i]) for x in rows], key
    assert _amts(r.text, "sum") == [int(x["sum_value"]) for x in rows]
    assert data["total"]["sum_value"] > 0 and f'data-total-missed="{data["total"]["n_missed"]}"' in r.text
    assert f'data-total-sum="{data["total"]["n_sum"]}"' in r.text and f'data-total-value="{int(data["total"]["value"])}"' in r.text
    assert ("예상 시상" in r.text) is (who == "admin")
    # 가중 인정이 있는 DB생명 여행시상은 달 칸에 인정한 값을, '원래' 에 합산용 값을 싣는다
    d = c.get(f"/promo/progress?view=org&p={_promo(con, 'DB생명 여행시상')}")
    tot = d.context["data"]["total"]
    boosted = [(ph["value_sum"], ph["plain_sum"]) for ph in tot["phases"] if ph["plain_sum"] is not None and ph["plain_sum"] != ph["value_sum"]]
    assert boosted and [v for v in tot["plains"] if v is not None] == [p for _v, p in boosted]
    assert [int(x) for x in re.findall(r'data-plain="(\d+)"', d.text)][-len(boosted):] == [int(p) for _v, p in boosted]
    con.close()


def test_bonbu_page_lists_everyone_and_matches_overview_row(env):
    """본부를 누르면 지점별 표 아래에 그 본부 사람 전원이 바로 나온다. 사람 표의 합이 현황 표의 그 본부 줄과 같다. 거르기와 순서를 본다."""
    con = db.connect(env["db"])
    c = login(env, "admin")
    pid = _promo(con, BOOM)
    top = c.get(f"/promo/progress?view=org&p={pid}")
    assert top.context["people"] is None and 'id="people"' not in top.text          # 현황에는 사람 표가 없다
    checked = 0
    for row in [x for x in top.context["data"]["rows"] if not x["direct"]]:
        page = c.get(row["href"])
        assert page.status_code == 200 and 'data-org-rows="1"' in page.text and 'data-people-filter="all"' in page.text
        people, pv = page.context["people"], page.context["pv"]
        assert {x["agent_id"] for x in people} == set(row["agent_ids"]) and len(people) == row["n_target"]
        assert sum(x["total"] for x in people) == pytest.approx(row["sum_value"])
        for i, ph in enumerate(row["phases"]):
            if ph["state"] != "future":
                assert sum(x["months"][i]["value"] for x in people) == pytest.approx(ph["value_sum"]), (row["name"], i)
            else:
                assert all(x["months"][i]["value"] is None for x in people)
        assert sum(1 for x in people if x["met"]) == row["n_achieved"] and pv["counts"] == {
            "all": row["n_target"], "met": row["n_achieved"], "miss": row["n_missed"]}
        shown = pv["rows"] + pv["zero"]
        assert len(shown) == len(people) and [x["total"] for x in pv["rows"]] == sorted((x["total"] for x in pv["rows"]), reverse=True)
        assert all(not x["total"] for x in pv["zero"]) and all(x["total"] or x["met"] for x in pv["rows"])
        assert (f'data-people-zero="{len(pv["zero"])}"' in page.text) is bool(pv["zero"])
        for x in people:
            assert x["status"] in ("met", "open", "miss")
            if x["status"] == "miss":                                # 무효가 된 사람에게는 남은 금액을 보이지 않는다
                assert x["sum_left"] is None
            else:
                assert x["sum_left"] == pytest.approx(max(400_000 - x["total"], 0))
            assert f'data-person="{x["agent_id"]}"' in page.text
        for f, want in (("met", [x for x in people if x["met"]]), ("miss", [x for x in people if not x["met"]])):
            got = c.get(f"{row['href']}&f={f}").context["pv"]
            assert {x["agent_id"] for x in got["rows"] + got["zero"]} == {x["agent_id"] for x in want}, (row["name"], f)
        checked += 1
    assert checked == 4
    con.close()


def test_roster_shows_months_and_sorts_by_sum(env):
    """명단은 사람마다 달별 실적과 합산 실적, 합산 기준까지 남은 값을 싣는다. sort=sum 이면 합산 실적이 큰 순이다."""
    con = db.connect(env["db"])
    c = login(env, _bonbu_head(con))
    pid = _promo(con, BOOM)
    p = pm._load(con, pid)
    base = f"/promo/progress?view=org&p={pid}&who=w1&row=all"
    r = c.get(base)
    roster = r.context["roster"]
    people = [x for g in roster["groups"] for x in g["people"]]
    assert people and roster["sort"] == "cell" and roster["on_seq"] == 1 and roster["sum_target"] == 400_000 and 'data-roster-sort="cell"' in r.text
    res = pm.progress(con, p, [x["agent_id"] for x in people], date.fromisoformat(r.context["ref"]))
    for x in people:
        mine = res[x["agent_id"]]
        assert x["total"] == mine["value"]
        assert x["sum_left"] == (None if mine["broken"] or mine["status"] == "missed" else max(400_000 - mine["value"], 0))
        assert [m["value"] for m in x["months"]] == [w["value"] if w["state"] != "upcoming" else None for w in mine["windows"]]
        assert x["value"] == mine["windows"][0]["value"]
    assert "예상 시상" not in r.text and "합산 40만까지" in r.text
    by_sum = c.get(base + "&sort=sum").context["roster"]
    assert by_sum["sort"] == "sum"
    for g in by_sum["groups"]:
        assert [x["total"] for x in g["people"]] == sorted((x["total"] for x in g["people"]), reverse=True)
    assert {x["agent_id"] for g in by_sum["groups"] for x in g["people"]} == {x["agent_id"] for x in people}
    con.close()


def test_person_page_scope_amounts_and_contracts(env):
    """개인 화면. 범위 밖 사람은 403, 관리자에게는 예상 시상이 없고 메인 관리자에게는 있다. 달별 값이 본부 표의 그 사람 값과 같고,
    센 계약은 엔진이 센 계약과 같다."""
    con = db.connect(env["db"])
    head = _bonbu_head(con)                                          # H본부 본부장
    ctx = _ctx(con, head)
    admin, c = login(env, "admin"), login(env, head)
    other = con.execute("""SELECT a.agent_id FROM agent a JOIN agent_org_history h ON h.agent_id=a.agent_id JOIN org_unit o ON o.org_id=h.org_id
                           WHERE o.path_key LIKE ? AND a.job_type='fc' ORDER BY a.agent_id LIMIT 1""", (_bonbu("금탑본부") + " > %",)).fetchone()[0]
    assert other not in ctx["agent_ids"]
    for name in (BOOM, "DB생명 여행시상", "DB생명 이터널 Convention 추가 시책", "삼성화재 드림+"):
        pid = _promo(con, name)
        p = pm._load(con, pid)
        shape = pm._parent(con, p) or p
        org = quote(_bonbu("H본부"))
        base = f"/promo/progress?view=org&p={pid}&org={org}"
        # 범위 밖 사람과 없는 사람은 403 이고 기록에 남는다
        before = con.execute("SELECT COUNT(*) FROM audit_log WHERE actor=? AND action='access_denied' AND target='/promo/progress person'", (head,)).fetchone()[0]
        for bad in (other, 99_999_999):
            assert c.get(f"/promo/progress?view=org&p={pid}&person={bad}").status_code == 403, (name, bad)
            assert c.get(f"{base}&person={bad}").status_code == 403, (name, bad)
        after = con.execute("SELECT COUNT(*) FROM audit_log WHERE actor=? AND action='access_denied' AND target='/promo/progress person'", (head,)).fetchone()[0]
        assert after - before == 4
        bonbu = c.get(base)
        people = bonbu.context["people"]
        assert people, name
        ref = date.fromisoformat(bonbu.context["ref"])
        for x in people[:3] + people[-1:]:
            assert x["href"].endswith(f"&person={x['agent_id']}")
            mine, top = c.get(x["href"]), admin.get(x["href"])
            assert mine.status_code == 200 and top.status_code == 200, (name, x["code"])
            # 달별 값과 합산이 본부 표의 그 사람 값과 같다
            assert [m["value"] for m in mine.context["months"]] == [m["value"] for m in x["months"]] or not x["months"]
            assert mine.context["r"]["value"] == x["total"] and f'data-person-total="{int(x["total"])}"' in mine.text
            assert mine.context["status"] == x["status"]
            # 관리자에게는 예상 시상이 없다. 메인 관리자에게는 있다
            assert mine.context["amount"] is None and mine.context["next_reward"] is None and mine.context["show_amount"] is False
            body = mine.text.split("<main", 1)[-1]
            assert "예상 시상" not in body and 'data-person-award="1"' not in body
            assert top.context["show_amount"] is True and 'data-person-award="1"' in top.text and "예상 시상" in top.text
            r = pm.progress(con, p, [x["agent_id"]], ref)[x["agent_id"]]
            assert top.context["amount"] == {"amount": r["amount"], "reward": r["reward"]}
            if r["reward"]:
                assert r["reward"] in top.text and r["reward"] not in body, (name, x["code"])
            # 센 계약은 엔진이 센 계약이고 센 값의 합이 합산 실적이다
            counted = [k for k in pm._counted(con, shape, [x["agent_id"]])[0]]
            shown = mine.context["contracts"]
            assert len(shown) == len(counted) and sorted(k["value"] for k in shown) == sorted(k["value"] for k in counted)
            assert sum(k["value"] for k in shown) == pytest.approx(x["total"])
            assert f'이 프로모션에 센 계약 {len(shown)}건' in mine.text
            assert [k["org_id"] for k in mine.context["crumbs"]][-1] == con.execute("SELECT org_id FROM org_unit WHERE path_key=?", (_bonbu("H본부"),)).fetchone()[0]
    # 철회와 대상이 아닌 상품은 계약 목록에 없다. 금탑본부의 한 사람이 철회 1건과 자동차보험 1건만 갖고 있다
    pid = _promo(con, BOOM)
    gone = con.execute("""SELECT c.agent_id FROM contract c JOIN product pr ON pr.product_id=c.product_id
                          WHERE pr.product_name='흥국화재 건강종합보험' AND c.status='withdrawn' LIMIT 1""").fetchone()[0]
    page = admin.get(f"/promo/progress?view=org&p={pid}&person={gone}")
    assert page.status_code == 200 and page.context["contracts"] == [] and page.context["r"]["value"] == 0 and "센 계약이 없습니다" in page.text
    # 설계사는 조직별 보기의 개인 화면을 열 수 없다. 자기 것은 내 달성 현황에서 본다
    fc, aid = _eternal_fc(con)
    assert login(env, fc).get(f"/promo/progress?view=org&p={pid}&person={aid}").status_code == 403
    con.close()


def test_tiers_form_picks_metric_after_publish(env):
    """구간과 조건 입력에서 지표(보험료, 인정보험료, 건수)를 고른다. 게시한 뒤에도 바꿀 수 있고 화면의 합산 칸 머리가 따라 바뀐다."""
    path = copy_db(env, "metric")
    con = db.connect(path)
    pid = _promo(con, BOOM)
    main = login(env, "admin", path)
    form = main.get(f"/promo/tiers?edit={pid}").text
    select = form.split('<select name="metric">', 1)[1].split("</select>", 1)[0]
    assert re.findall(r'<option value="(\w+)"', select) == list(pm.METRIC_KO) == ["premium_monthly", "converted_premium", "policy_count"]
    assert '<option value="converted_premium" selected>인정보험료</option>' in select
    p = pm._load(con, pid)
    assert p["status"] == "published"
    data = {"promo_id": str(pid), "action": "update", "name": p["name"], "kind": p["kind"], "insurer_id": str(p["insurer_id"]), "sector": p["sector"],
            "metric": "premium_monthly", "date_basis": p["date_basis"], "combine_rule": p["combine_rule"], "categories": p["category_list"],
            "window_mode": "1", "r_count_basis": "application", "r_ret_min": "95", "top_org_reward": "최다 달성 본부의 총무 참석"}
    for i, w in enumerate(p["windows"], start=1):
        data.update({f"w_label_{i}": w["label"], f"w_from_{i}": w["date_from"], f"w_to_{i}": w["date_to"]})
    for i, t in enumerate([t for t in p["tiers"] if t.get("window_seq") is None], start=1):
        data.update({f"t_min_{i}": str(int(t["min_value"])), f"t_kind_{i}": t["reward_kind"], f"t_label_{i}": t["reward_label"]})
    for seq, rows in p["window_tiers"].items():
        for i, t in enumerate(rows, start=1):
            data.update({f"wt{seq}_min_{i}": str(int(t["min_value"])), f"wt{seq}_kind_{i}": t["reward_kind"], f"wt{seq}_label_{i}": t["reward_label"]})
    r = main.post("/promo/tiers", data=data)
    assert r.status_code == 303 and "/promo/publish?msg=" in r.headers["location"], r.headers.get("location")
    after = pm._load(con, pid)
    assert after["metric"] == "premium_monthly" and after["status"] == "published" and after["combine_rule"] == "both"
    assert {seq: [t["min_value"] for t in rows] for seq, rows in after["window_tiers"].items()} == {1: [50_000], 2: [100_000], 3: [150_000]}
    page = main.get(f"/promo/progress?view=org&p={pid}")
    assert 'data-col="sum">합산 보험료<span class="th-sub">기준 40만</span>' in page.text and "실적은 보험료 기준입니다" in page.text
    con.close()


# ================================================================ 검증 뒤 고친 것 (11월 17일)
def _form_data(client, pid):
    """고치기 폼을 화면에 보이는 값 그대로 읽는다. 체크하지 않은 칸은 빼고, 고른 값이 없는 고르기 칸은 첫 값이다."""
    html = client.get(f"/promo/tiers?edit={pid}").text
    form = html.split('<form class="wide" method="post" action="/promo/tiers"', 1)[1].split("</form>", 1)[0]
    data = {}
    for tag in re.findall(r"<input\b[^>]*>", form):
        attr = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        if not attr.get("name") or attr["name"] == "csrf_token":
            continue
        if attr.get("type") in ("checkbox", "radio"):
            if re.search(r"\bchecked\b", tag):
                data.setdefault(attr["name"], []).append(attr.get("value", "on"))
        else:
            data.setdefault(attr["name"], []).append(attr.get("value", ""))
    for name, body in re.findall(r'<select name="([^"]+)"[^>]*>(.*?)</select>', form, re.S):
        opts = re.findall(r'<option value="([^"]*)"([^>]*)>', body)
        picked = [v for v, rest in opts if "selected" in rest]
        data.setdefault(name, []).append(picked[0] if picked else (opts[0][0] if opts else ""))
    for name, body in re.findall(r'<textarea name="([^"]+)"[^>]*>(.*?)</textarea>', form, re.S):
        data.setdefault(name, []).append(body.lstrip("\n"))
    out = {k: (v if len(v) > 1 else v[0]) for k, v in data.items()}
    out["action"] = "update"
    return out


def test_tiers_form_round_trips_several_alt_chains(env):
    """추가안이 둘이고 가중 인정 여부와 구간 번호가 기본값이 아니어도, 폼을 그대로 저장하면 바뀐 것이 없다. 폼에서 고친 값은 엔진에 그대로 간다."""
    path = copy_db(env, "alts")
    con = db.connect(path)
    pid = _promo(con, "DB생명 여행시상")
    import json
    first = pm._load(con, pid)["rules"]["alt_chains"][0]              # 엔진이 저장한 꼴 그대로 둘째 추가안을 붙인다
    chains = [{**first, "boost": False}, {**first, "key": "late", "label": "조건 1-3", "windows": {"3": 700_000.0}, "requires_missed": [1, 2], "boost": True}]
    con.execute("UPDATE promotion SET alt_chains_json=? WHERE promo_id=?", (json.dumps(chains, ensure_ascii=False), pid))
    con.commit()
    alts = lambda: pm._alts(pm._load(con, pid))
    before = alts()
    assert [c["key"] for c in before] == ["c12", "late"] and before[0]["boost"] is False and before[1]["requires_missed"] == [1, 2]
    main = login(env, "admin", path)
    data = _form_data(main, pid)
    assert data["r_alt_n"] == "3" and data["r_alt_key_1"] == "c12" and data["r_alt_key_2"] == "late" and data["r_alt_boost_1"] == "0"
    r = main.post("/promo/tiers", data=data)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    from urllib.parse import unquote
    assert "바뀐 것이 없습니다" in unquote(r.headers["location"])
    assert alts() == before
    # 둘째 추가안의 기준과 구간 번호를 고치고 첫째는 그대로 둔다. 바뀐 점 메모는 고친 판에 붙는다
    data = _form_data(main, pid)
    data.update({"r_alt_min_2_3": "800000", "r_alt_boost_2": "0", "version_note": "조건 1-3 기준 변경"})
    r = main.post("/promo/tiers", data=data)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    after = alts()
    assert after[0] == before[0] and after[1] == {**before[1], "windows": {3: 800_000.0}, "boost": False}
    # 바뀐 것이 없는 저장에 적은 메모는 남기지 않고 그렇게 안내한다
    n_notes = con.execute("SELECT COUNT(*) FROM promotion_version_note").fetchone()[0]
    data = _form_data(main, pid)
    data["version_note"] = "바뀐 것 없이 적은 메모"
    where = unquote(main.post("/promo/tiers", data=data).headers["location"])
    assert "바뀐 것이 없습니다" in where and "바뀐 내용이 없어 메모를 남기지 않았습니다" in where
    assert con.execute("SELECT COUNT(*) FROM promotion_version_note").fetchone()[0] == n_notes
    # 기준을 모두 비운 추가안은 지운다
    data = _form_data(main, pid)
    data["r_alt_min_2_3"] = ""
    main.post("/promo/tiers", data=data)
    assert alts() == before[:1]
    con.close()


def test_leaver_value_stays_in_org_total_and_out_of_team_rows(env):
    """D-42. 퇴사자의 퇴사 전 실적은 조직별 진척의 실적 합계에 넣고 대상, 달성에서는 뺀다. 사람별 표에는 줄이 없고 아래 안내에 인원과 합계가 있다.
    관리자에게도 같고, 다른 본부의 관리자에게는 보이지 않는다."""
    path = copy_db(env, "leaver2")
    con = db.connect(path)
    pid = _promo(con, BOOM)
    p = pm._load(con, pid)
    head, other_head = _bonbu_head(con, "H본부"), _bonbu_head(con, "금탑본부")
    ctx = _ctx(con, head)
    main, c = login(env, "admin", path), login(env, head, path)
    before_main = main.get(f"/promo/progress?view=org&p={pid}").context["data"]["total"]
    before_head = c.get(f"/promo/progress?view=org&p={pid}").context["data"]["total"]
    before_other = login(env, other_head, path).get(f"/promo/progress?view=org&p={pid}").context["data"]["total"]
    ref = date.fromisoformat(main.get(f"/promo/progress?view=org&p={pid}").context["ref"])
    res = pm.progress(con, p, sorted(a for a in ctx["agent_ids"]), ref)
    leaver = next(a for a, r in sorted(res.items()) if r["value"] > 0 and a != ctx["agent_id"])
    row = con.execute("SELECT internal_code, name FROM agent WHERE agent_id=?", (leaver,)).fetchone()
    con.execute("UPDATE agent SET status='terminated', termination_date=? WHERE agent_id=?", (ref.isoformat(), leaver))
    con.commit()
    for client, before in ((main, before_main), (c, before_head)):
        page = client.get(f"/promo/progress?view=org&p={pid}")
        after = page.context["data"]["total"]
        assert after["value"] == pytest.approx(before["value"]) and after["n_left"] == 1 and after["n_target"] == before["n_target"] - 1
        assert after["value_left"] == pytest.approx(res[leaver]["value"]) and after["sum_value"] == pytest.approx(before["sum_value"] - res[leaver]["value"])
        assert "퇴사 1명의 퇴사 전 실적 포함" in page.text
        team = client.get(f"/promo/progress?p={pid}")
        assert leaver not in {x["agent_id"] for x in team.context["rows"]} and f'data-agent="{row["internal_code"]}"' not in team.text
        s = team.context["summary"]
        assert s["n_left"] == 1 and s["value_left"] == pytest.approx(res[leaver]["value"])
        assert 'data-left="1"' in team.text and row["name"] not in team.text.split("<main", 1)[-1]
        if client is c:                                              # 본부장은 사람별 표와 조직별 표의 범위가 같다
            assert s["n"] == after["n_target"]
            assert sum(x["r"]["value"] for x in team.context["rows"]) + s["value_left"] == pytest.approx(after["value"])
        # 퇴사자의 개인 화면과 명단 줄은 없다
        assert client.get(f"/promo/progress?view=org&p={pid}&person={leaver}").status_code == 403
        roster = client.get(f"/promo/progress?view=org&p={pid}&who=target&row=all").context["roster"]
        assert leaver not in {x["agent_id"] for g in roster["groups"] for x in g["people"]}
    other = login(env, other_head, path).get(f"/promo/progress?view=org&p={pid}").context["data"]["total"]
    assert (other["value"], other["n_left"], other["n_target"]) == (before_other["value"], 0, before_other["n_target"])
    con.close()


def test_confirmed_promotion_team_view_shows_confirmed_values(env):
    """확정한 프로모션의 사람별 보기는 다시 계산하지 않고 확정 값을 보인다. 지난 프로모션 화면과 같은 숫자이고 '확정' 표시가 붙는다.
    관리자에게는 금액 칸이 없다. 조직별 보기에는 확정 안내가 붙는다."""
    path = copy_db(env, "confirmed2")
    con = db.connect(path)
    pid = _promo(con, "삼성화재 드림+ 지난 차수")
    assert pm.confirmation(con, pid)
    main = login(env, "admin", path)
    archive = {x["agent_id"]: x for x in main.get(f"/promo/archive?ym=202609&p={pid}").context["sel"]["rows"]}
    hit = [a for a, x in archive.items() if x["tier_no"]]
    assert len(hit) >= 3 and len({archive[a]["tier_no"] for a in hit}) >= 3         # 샘플: 1, 2, 3단계 달성자
    con.execute("UPDATE contract SET status='withdrawn' WHERE agent_id=? AND contract_date BETWEEN '2026-07-19' AND '2026-09-30'", (hit[0],))
    con.commit()
    page = main.get(f"/promo/progress?p={pid}&ym=202609")
    rows = {x["agent_id"]: x["r"] for x in page.context["rows"]}
    assert set(rows) == set(archive)
    for a, x in archive.items():
        assert rows[a]["value"] == x["value"] and (rows[a]["tier"] or {}).get("tier_no") == x["tier_no"] and rows[a]["amount"] == x["amount"]
    s = page.context["summary"]
    assert s["confirmed"] and s["achieved"] == len(hit) and s["n"] == len(archive)
    assert 'data-confirmed="1"' in page.text and "확정 시상" in page.text and "예상 시상" not in page.text.split("<main", 1)[-1]
    assert "메인 관리자가 확정한 결과입니다" in page.text
    org = main.get(f"/promo/progress?view=org&p={pid}&ym=202609")
    assert org.status_code == 200 and 'data-confirmed-org="1"' in org.text
    head = login(env, _bonbu_head(con), path).get(f"/promo/progress?p={pid}&ym=202609")
    assert head.status_code == 200 and head.context["summary"]["confirmed"] and "확정 시상" not in head.text
    assert {x["agent_id"] for x in head.context["rows"]} <= set(_ctx(con, _bonbu_head(con))["agent_ids"])
    con.close()


def test_period_text_does_not_repeat_the_window_name():
    from app.web.deps import _period_text
    q4 = {"date_from": "2026-10-01", "date_to": "2026-12-31", "windows": [
        {"label": "10월", "date_from": "2026-10-01", "date_to": "2026-10-31"}, {"label": "11월", "date_from": "2026-11-01", "date_to": "2026-11-30"}]}
    assert _period_text(q4) == "10월 1일~10월 31일 · 11월 1일~11월 30일"
    mixed = {"date_from": "2026-10-19", "date_to": "2026-11-30", "windows": [
        {"label": "10월 19일에서 31일", "date_from": "2026-10-19", "date_to": "2026-10-31"}, {"label": "2주차", "date_from": "2026-11-08", "date_to": "2026-11-14"},
        {"label": "", "date_from": "2026-11-15", "date_to": "2026-11-30"}]}
    assert _period_text(mixed) == "10월 19일에서 31일 · 2주차 11월 8일~11월 14일 · 11월 15일~11월 30일"


def test_void_person_sees_no_remaining_amount(env):
    """BOOM-UP 에서 10월 기준을 놓쳐 무효가 된 사람에게는 '남았습니다' 가 없다. 본부 표와 개인 화면에도 남은 금액이 없다."""
    con = db.connect(env["db"])
    pid = _promo(con, BOOM)
    p = pm._load(con, pid)
    ids = [a for (a,) in con.execute("SELECT agent_id FROM agent WHERE internal_code LIKE 'T01%'")]
    res = pm.progress(con, p, ids, date.today())
    void = next(a for a in ids if res[a]["broken"] and res[a]["value"] > 0)
    code = con.execute("SELECT internal_code FROM agent WHERE agent_id=?", (void,)).fetchone()[0]
    html = login(env, code).get("/promo/progress").text
    section = html.split(f"{BOOM}</div>", 1)[1].split("</section>", 1)[0]
    assert "무효" in section and "남았습니다" not in section and "data-left=" not in section and "data-sum-left=" not in section
    page = login(env, "admin").get(f"/promo/progress?view=org&p={pid}&person={void}")
    assert page.context["status"] == "miss" and page.context["todo"] == [] and "남았습니다" not in page.text and "남은 길이 없습니다" in page.text
    assert all(m["left"] is None for m in page.context["months"]) and page.context["sum_row"]["left"] is None
    con.close()


def test_attached_promotion_table_follows_parent_shape(env):
    """딸린 시책(Convention)의 조직별 표는 부모(DB생명 여행시상)와 같은 칸이다. 합산 칸이 있고 추가안 대상 칸의 이름을 부모의 추가안에서 읽는다."""
    con = db.connect(env["db"])
    c = login(env, "admin")
    conv = c.get(f"/promo/progress?view=org&p={_promo(con, 'DB생명 이터널 Convention 추가 시책')}")
    parent = c.get(f"/promo/progress?view=org&p={_promo(con, 'DB생명 여행시상')}")
    strip = lambda cols: [(x["key"], x["label"], x["sub"], x["group_label"]) for x in cols]
    assert strip(conv.context["phases"]) == strip(parent.context["phases"])
    keys = [x["key"] for x in conv.context["phases"]]
    assert "sum" in keys and "a0" in keys
    a0 = next(x for x in conv.context["phases"] if x["key"] == "a0")
    assert a0["label"] == "10월 미달성자" and "앞 창 미달성자" not in conv.text
    tot_c, tot_p = conv.context["data"]["total"], parent.context["data"]["total"]
    assert tot_c["cells"][:-1] == tot_p["cells"][:-1] and tot_c["sums"][:-1] == tot_p["sums"][:-1]      # 최종 달성만 문턱 때문에 다르다
    assert "연속으로 채우면 3개월 합계 70만원(10만, 20만, 40만), 합산으로는 100만원" in conv.text
    con.close()


def test_copy_bridge_bad_date_and_audit_label_read_in_korean(env):
    """브릿지 복사에 틀린 날짜를 넣으면 한국어 안내가 나온다. 복사의 변경 기록은 한국어 이름과 원본 이름으로 읽힌다."""
    path = copy_db(env, "copyko")
    con = db.connect(path)
    src = _promo(con, "삼성화재 10~11월 연속가동 브릿지")
    main = login(env, "admin", path)
    from urllib.parse import unquote
    data = {"action": "copy_bridge", "promo_id": str(src), "period_ym": "2026-12", "c_from_1": "2026-12-16", "c_to_1": "2026-12-41",
            "c_from_2": "2027-01-01", "c_to_2": "2027-01-15", "name": "틀린 날짜"}
    r = main.post("/promo/tiers", data=data)
    where = unquote(r.headers["location"])
    assert r.status_code == 303 and "err=" in where
    msg = where.split("err=", 1)[1].split("#")[0]
    assert "창의 날짜를 읽지 못했습니다" in msg and not re.search(r"[A-Za-z]{4,}", msg)
    assert not con.execute("SELECT 1 FROM promotion WHERE name='틀린 날짜'").fetchone()
    row = con.execute("SELECT target FROM audit_log WHERE action='promotion_copy' ORDER BY id DESC LIMIT 1").fetchone()
    shown = audit_view.target_ko("promotion_copy", row["target"], audit_view._Names(con))
    assert audit_view.action_ko("promotion_copy") == "프로모션 다음 달 복사"
    assert shown.startswith("프로모션: 삼성화재 11~12월 연속가동 브릿지") and "원본: 삼성화재 10~11월 연속가동 브릿지" in shown and "바뀐 칸" not in shown
    con.close()


# ================================================================ 확정 값으로 맞춘 조직 표, 명단, 개인 화면
PAST = "삼성화재 드림+ 지난 차수"
COUNT_KEYS = ("n_target", "n_active", "n_achieved", "n_missed", "n_lost", "n_open", "n_near", "n_inactive", "value", "sum_value")


def _main_body(html):
    return html.split("<main", 1)[-1]


def test_confirmed_org_table_roster_and_person_agree_after_withdrawal(env):
    """확정한 뒤 계약을 철회로 바꿔도 조직 표의 숫자, 명단의 인원, 사람 표와 개인 화면의 값이 확정 값으로 서로 같다.
    조직 표에 '확정' 표시가 붙고 구간 근접 칸과 카드, 다시 센 값이라는 안내가 없다. 미달성 아래에 무효와 진행 중 인원이 있다.
    지난 달을 보다가 조직을 눌러도 같은 달, 같은 프로모션에 머문다. 확정 뒤에 퇴사한 사람은 숫자와 명단에 남고 개인 화면은 열리지 않는다."""
    path = copy_db(env, "confirmed3")
    con = db.connect(path)
    pid = _promo(con, PAST)
    p = pm._load(con, pid)
    head = _bonbu_head(con)
    main, c = login(env, "admin", path), login(env, head, path)
    base = f"/promo/progress?view=org&p={pid}&ym=202609"
    archive = {x["agent_id"]: x for x in main.get(f"/promo/archive?ym=202609&p={pid}").context["sel"]["rows"]}
    hit = [a for a, x in archive.items() if x["tier_no"]]
    before = {who: cl.get(base).context["data"] for who, cl in (("admin", main), ("head", c))}
    assert before["admin"]["confirmed"] and before["admin"]["detail"] == "full" and before["admin"]["total"]["n_achieved"] == len(hit) >= 3
    # 달성자 한 사람의 계약을 모두 철회로 바꾼다. 다시 세면 값이 줄어드는 것을 먼저 본다
    gone = next(a for a in hit if a in _ctx(con, head)["agent_ids"])
    con.execute("UPDATE contract SET status='withdrawn' WHERE agent_id=? AND contract_date BETWEEN '2026-07-19' AND '2026-09-30'", (gone,))
    con.commit()
    assert pm.progress(con, p, [gone], date(2026, 11, 1))[gone]["value"] < archive[gone]["value"]
    for who, cl in (("admin", main), ("head", c)):
        scope = set(_ctx(con, head)["agent_ids"]) if who == "head" else set(archive)
        page = cl.get(base)
        data, body = page.context["data"], _main_body(page.text)
        tot = data["total"]
        # 조직 표: 철회 뒤에도 확정 값 그대로다
        assert data["confirmed"] and data["detail"] == "full", who
        for x, y in zip([*data["rows"], tot], [*before[who]["rows"], before[who]["total"]]):
            assert [x[k] for k in COUNT_KEYS] == [y[k] for k in COUNT_KEYS], who
            assert [ph["n_met"] for ph in x["phases"]] == [ph["n_met"] for ph in y["phases"]], who
            assert x["n_missed"] == x["n_lost"] + x["n_open"] == x["n_target"] - x["n_achieved"] and x["n_open"] == 0 and x["n_near"] == 0
        assert tot["n_achieved"] == len([a for a in hit if a in scope]) and tot["value"] == pytest.approx(sum(archive[a]["value"] for a in scope if a in archive))
        # 확정 표시, 지운 안내, 숨긴 구간 근접, 무효와 진행 중
        assert 'data-confirmed-org="1"' in body and '<span class="chip good">확정</span>' in body and "메인 관리자가 확정한 결과입니다" in body
        assert "다시 센 값" not in body and "확정 값과 다를 수 있습니다" not in body and "근접" not in body and "data-near-card" not in body
        assert "예상 시상" not in body and (("확정 시상" in body) is (who == "admin"))
        assert f'data-total-missed="{tot["n_missed"]}"' in body and f'data-total-lost="{tot["n_lost"]}" data-total-open="0">무효 {tot["n_lost"]}명 · 진행 중 0명' in body
        assert [(int(a), int(b)) for a, b in re.findall(r'data-lost="(\d+)" data-open="(\d+)"', body)] == [(x["n_lost"], x["n_open"]) for x in [*data["rows"], tot]]
        # 명단: 표의 숫자마다 인원이 같고 값이 확정 값이다
        links = _who_links(page.text)
        assert {k for _h, k, _r, _n in links} >= {"w1", "w2", "w3", "final", "missed", "target"} and "near" not in {k for _h, k, _r, _n in links}
        for href, key, row, n in links:
            assert "ym=202609" in href, key
            r = cl.get(href.replace("&amp;", "&"))
            roster = r.context["roster"]
            people = [x for g in roster["groups"] for x in g["people"]]
            assert r.status_code == 200 and roster["confirmed"] and roster["n"] == int(n) == len(people), (who, key, row)
            assert {x["agent_id"] for x in people} <= scope and all(x["total"] == archive[x["agent_id"]]["value"] for x in people), (who, key)
            assert "예상" not in _main_body(r.text).split('id="roster"', 1)[1].split("</table>", 1)[0]
        final = cl.get(f"{base}&who=final&row=all").context["roster"]
        assert {x["agent_id"] for g in final["groups"] for x in g["people"]} == {a for a in hit if a in scope}
        assert all(len(x["months"]) == 3 and all(m["met"] for m in x["months"]) for g in final["groups"] for x in g["people"])
        # 조직을 눌러도 같은 달, 같은 프로모션이다. 사람 표와 개인 화면도 확정 값이다
        row = next(x for x in data["rows"] if gone in x["agent_ids"] and not x["direct"])
        assert "ym=202609" in row["href"]
        down = cl.get(row["href"])
        assert down.status_code == 200 and down.context["sel"]["promo_id"] == pid and down.context["data"]["confirmed"]
        people = down.context["people"]
        if people is None:                                             # 본부 위에서 본 줄이면 한 단계 더 내려가 사람 표를 본다
            sub = next(x for x in down.context["data"]["rows"] if gone in x["agent_ids"])
            down = cl.get(sub["href"])
            people = down.context["people"]
        mine = {x["agent_id"]: x for x in people}
        assert gone in mine and all(x["total"] == archive[a]["value"] and x["met"] == bool(archive[a]["tier_no"]) for a, x in mine.items())
        some_miss = max((a for a, x in mine.items() if not x["met"]), key=lambda a: mine[a]["total"])   # 실적이 있는 미달성자가 있으면 그 사람
        for a in (gone, some_miss):
            person = cl.get(mine[a]["href"])
            ctx, text = person.context, _main_body(person.text)
            assert person.status_code == 200 and ctx["sel"]["promo_id"] == pid and ctx["confirmed"] and ctx["r"]["confirmed"], (who, a == gone)
            assert ctx["r"]["value"] == archive[a]["value"] and ctx["status"] == ("met" if a == gone else "miss") and ctx["todo"] == []
            assert f'data-person-total="{int(archive[a]["value"])}"' in text and 'data-confirmed-person="1"' in text and "예상" not in text
            assert [m["value"] for m in ctx["months"]] == [w["value"] for w in ctx["r"]["windows"]] and len(ctx["months"]) == 3
            # 계약 목록은 확정 때 센 계약이고 합이 확정 값이다. 철회한 계약도 남아 지금 상태로 보인다
            assert ctx["contract_source"] == "confirm" and sum(k["value"] for k in ctx["contracts"]) == pytest.approx(archive[a]["value"])
            assert all(not k["after_ref"] for k in ctx["contracts"]) and "기준일 뒤 계약" not in text
            if a == gone:
                assert ctx["contracts"] and {k["status"] for k in ctx["contracts"]} == {"withdrawn"} and 'data-contract-status="withdrawn">철회<' in text
            assert (ctx["amount"] is None) is (who == "head")
            if who == "admin":
                assert ctx["amount"] == {"amount": archive[a]["amount"], "reward": archive[a]["reward"]} and "확정 시상" in text
    # 확정한 뒤에 퇴사한 사람: 숫자와 명단에는 남고(확정 때의 재직자) 개인 화면은 열리지 않는다
    con.execute("UPDATE agent SET status='terminated', termination_date='2026-11-10' WHERE agent_id=?", (gone,))
    con.commit()
    after = main.get(base)
    assert [after.context["data"]["total"][k] for k in COUNT_KEYS] == [before["admin"]["total"][k] for k in COUNT_KEYS]
    r = main.get(f"{base}&who=final&row=all")
    people = {x["agent_id"]: x for g in r.context["roster"]["groups"] for x in g["people"]}
    assert r.context["roster"]["n"] == len(hit) and people[gone]["gone"] and people[gone]["href"] is None and f'data-person-left="{gone}"' in r.text
    assert f'data-person="{gone}"' not in r.text and main.get(f"{base}&person={gone}").status_code == 403
    con.close()


@pytest.mark.parametrize("detail", ["values", "none"])
def test_confirmed_without_window_detail_leaves_month_cells_empty(env, detail):
    """창별 판정을 낼 수 없는 확정(규칙을 남기지 않았거나 센 계약도 남기지 않았다)은 달 칸의 인원과 창별 명단, 개인 화면의 달별 실적을 비우고 안내한다.
    최종 달성과 합계, 사람별 값은 확정 값 그대로다."""
    path = copy_db(env, f"confirmed-{detail}")
    con = db.connect(path)
    pid = _promo(con, PAST)
    main = login(env, "admin", path)
    base = f"/promo/progress?view=org&p={pid}&ym=202609"
    full = main.get(base).context["data"]["total"]
    con.execute("UPDATE promotion_confirm SET rule_json=NULL WHERE promo_id=?", (pid,))
    if detail == "none":
        con.execute("DELETE FROM promotion_result_contract WHERE promo_id=?", (pid,))
    con.commit()
    page = main.get(base)
    data, body = page.context["data"], _main_body(page.text)
    tot = data["total"]
    assert page.status_code == 200 and data["confirmed"] and data["detail"] == detail
    assert [tot[k] for k in COUNT_KEYS] == [full[k] for k in COUNT_KEYS]
    assert tot["cells"][:3] == ["", "", ""] and tot["cells"][3] == f'{full["n_achieved"]}명'
    assert (tot["sums"][:3] == full["sums"][:3]) is (detail == "values")          # 센 계약이 남아 있으면 달의 실적 합계는 보인다
    note = "확정 때의 창별 값이 남아 있지 않습니다" if detail == "none" else "확정 때의 창별 판정이 남아 있지 않습니다"
    assert 'data-window-note="1"' in body and note in body
    assert not {k for _h, k, _r, _n in _who_links(page.text)} & {"w1", "w2", "w3"}
    # 창별 명단은 사람 없이 안내만 나온다. 화면에서 대신 세지 않는다
    r = main.get(f"{base}&who=w1&row=all")
    assert r.status_code == 200 and r.context["roster"]["n"] == 0 and r.context["roster"]["groups"] == [] and note in r.context["roster"]["unavailable"]
    assert 'data-roster-unavailable="1"' in r.text and "data-roster-agent" not in r.text
    # 최종 달성 명단은 확정 값으로 나오고 달 칸이 없다
    r = main.get(f"{base}&who=final&row=all")
    roster = r.context["roster"]
    people = [x for g in roster["groups"] for x in g["people"]]
    assert roster["n"] == full["n_achieved"] == len(people) and roster["windows"] == [] and all(x["months"] == [] for x in people)
    assert note in roster["window_note"] and 'data-window-note="roster"' in r.text
    # 개인 화면: 달별 실적 대신 안내, 값과 상태는 확정 값
    person = main.get(people[0]["href"])
    ctx = person.context
    assert person.status_code == 200 and ctx["months"] == [] and note in ctx["window_note"] and 'data-window-note="person"' in person.text
    assert ctx["r"]["value"] == people[0]["total"] and ctx["status"] == "met" and "data-person-months" not in person.text
    if detail == "values":
        assert ctx["contract_source"] == "confirm" and sum(k["value"] for k in ctx["contracts"]) == pytest.approx(ctx["r"]["value"])
    else:
        assert ctx["contract_source"] is None and ctx["contracts"] == [] and 'data-contracts-none="1"' in person.text
    con.close()


def test_person_contract_list_is_engine_list_and_marks_contracts_after_ref(env):
    """개인 화면의 센 계약은 엔진의 counted_contracts 를 옮긴 것이다. 센 값의 합이 그 사람 값과 같고, 창마다 달 기준에 대는 값의 합이 그 달 실적과 같다.
    기준일 뒤 날짜의 계약에는 '기준일 뒤 계약' 표시가 붙는다. 10월 말 기준으로 보면 11월 계약이 그렇다."""
    con = db.connect(env["db"])
    c = login(env, "admin")
    ref = date(2026, 10, 31)
    checked = marked = 0
    for name in (BOOM, "DB생명 여행시상", "DB생명 이터널 Convention 추가 시책", "삼성화재 드림+"):
        pid = _promo(con, name)
        p = pm._load(con, pid)
        ids = [a for (a,) in con.execute("SELECT agent_id FROM agent WHERE internal_code LIKE 'T%' AND status='active' ORDER BY agent_id")]
        res = pm.progress(con, p, ids, ref)
        busy = [a for a in ids if res[a]["value"] > 0 and res[a].get("target", True)]
        late = [a for a in busy if any(x["after_ref"] for x in pm.counted_contracts(con, p, a, ref)["rows"])]
        for a in busy[:3] + late[:2]:
            page = c.get(f"/promo/progress?view=org&p={pid}&ym=202610&person={a}")
            assert page.status_code == 200 and page.context["ref"] == ref.isoformat() and page.context["confirmed"] is None, name
            ctx, text = page.context, _main_body(page.text)
            want = pm.counted_contracts(con, p, a, ref)
            shown = ctx["contracts"]
            assert ctx["contract_source"] == "ledger" and ctx["contracts_n"] == want["n"] and len(shown) == len(want["rows"]), name
            assert [(k["date"], k["value"], k["status"], k["after_ref"]) for k in shown] == [(k["date"], k["value"], k["status"], k["after_ref"]) for k in want["rows"]]
            assert sum(k["value"] for k in shown) == pytest.approx(ctx["r"]["value"]) == pytest.approx(res[a]["value"]), name
            assert f'data-person-total="{int(ctx["r"]["value"])}"' in text and f'이 프로모션에 센 계약 {want["n"]}건' in text
            assert sum(int(v) for v in re.findall(r'data-counted="(-?\d+)"', text)) == pytest.approx(ctx["r"]["value"], abs=len(shown))
            for m in ctx["months"]:                                    # 달 기준에 대는 값. 가중 인정이 들면 value_each 다
                if m["value"] is not None:
                    each = sum(k["value_each"] if k["value_each"] is not None else k["value"] for k in shown if k["window"] == m["label"])
                    assert each == pytest.approx(m["value"]), (name, m["seq"])
            assert all(k["status_ko"] in ("청약", "정상", "실효", "해지", "만기") for k in shown), name
            n_late = sum(1 for k in shown if k["after_ref"])
            assert text.count('data-after-ref="1"') == n_late == text.count('<span class="chip warn tiny">기준일 뒤 계약</span>')
            assert all(k["date"] > ref.isoformat() for k in shown if k["after_ref"]) and all(k["date"] <= ref.isoformat() for k in shown if not k["after_ref"])
            marked += n_late
            checked += 1
    assert checked >= 8 and marked >= 1
    con.close()
