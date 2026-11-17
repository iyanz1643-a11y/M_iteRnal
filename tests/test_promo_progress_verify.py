"""프로모션 진척 검증 (커밋 f95e72b). 만든 쪽과 따로 본다. tests/test_promo_screen.py 가 보지 않는 곳을 채운다.

| 무엇 | 보는 것 |
| --- | --- |
| 확정 조건 | 샘플의 BOOM-UP, DB생명 여행시상 변경판, Convention 문턱, 드림+ 의 값이 대표님이 확정한 조건과 같다 |
| 범위 공격 | 본부장이 다른 본부의 org, row, who 를 주소로 넣어도 403 이나 404 이고 범위 밖 사람이 한 명도 실리지 않는다 |
| 설계사 | view=org 와 명단은 403 이고, 내 화면에는 다른 사람의 이름이 없다 |
| 금액 | 관리자의 조직별 표, 자세히, 명단, 사람 목록에 시상 금액이 없다. 메인 관리자에게는 금액과 최다 달성 본부가 있다 |
| 문턱 | 문턱 카드는 숫자만 싣고, 보는 사람이 달라도 같은 숫자이며, 조직별 표의 달성 인원과 같다 |
| 쓰기 | 판 메모, 보상 선택, 브릿지 복사는 메인 관리자만, CSRF 를 거쳐 하고 흔적이 남는다. 이상한 값은 500 이 아니다 |
| 같은 숫자 | 조직별 표의 모든 숫자가 그 명단의 인원과 같다. 줄의 합이 합계 줄과 같다. 조직별, 팀원, 내 현황의 같은 사람 값이 같다 |
| 기준일 | 오늘을 바꿔도 표와 명단이 같다. 시작 전 창에는 숫자와 명단이 없다 |
| 알려진 한계 | 지난 달 보기에서 기준일 뒤 계약이 합산과 최종 달성에 드는 것을 재현한다. 열린 결정이라 지금 동작을 적어 둔다 |
| 찾은 문제 | 아래 '찾은 문제' 묶음의 시험은 지금 실패한다. 고치면 통과한다. 끄거나 건너뛰지 않는다 |

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 는 건드리지 않는다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다. 사번과 건수만 넣는다.
"""
import html as html_lib
import json
import os
import pathlib
import re
import shutil
import tempfile
from datetime import date
from html.parser import HTMLParser
from urllib.parse import quote, unquote

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import promotion as pm
from app.web import audit_view, auth, promo_extra
from app.web.routes import promo as promo_routes

PW = "verify-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
LINK = re.compile(r'<a class="cnt" href="([^"]+)" data-who="([^"]+)" data-row="([^"]*)">(\d+)명</a>')
ETERNAL = "hq:직할본부 > branch:이터널"
BONBU = ("해온본부", "금탑본부", "H본부", "피닉스본부")
HEAD_OF = {"해온본부": "T0001", "금탑본부": "T0002", "H본부": "T0003", "피닉스본부": "T0004"}
BOOM, DBL, CONV, DREAM, DREAM_PAST = "흥국화재 BOOM-UP", "DB생명 여행시상", "DB생명 이터널 Convention 추가 시책", "삼성화재 드림+", "삼성화재 드림+ 지난 차수"
BRIDGE, BRIDGE_NEXT = "삼성화재 10~11월 연속가동 브릿지", "삼성화재 11~12월 연속가동 브릿지"
SAMPLE_PROMOS = (BOOM, DBL, CONV, DREAM, BRIDGE, BRIDGE_NEXT)
MAN = 10_000
TODAY = date(2026, 11, 17)


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
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="promoverify-"))
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


def _pid(con, name):
    return con.execute("SELECT promo_id FROM promotion WHERE name=?", (name,)).fetchone()[0]


def _org(bonbu=None, team=None):
    return ETERNAL + (f" > center:{bonbu}" if bonbu else "") + (f" > team:{team}" if team else "")


def _codes(con, ids):
    ids = list(ids)
    q = ",".join("?" * len(ids))
    return sorted(r[0] for r in con.execute(f"SELECT internal_code FROM agent WHERE agent_id IN ({q})", ids)) if ids else []


def _orgs_in_scope(con, ctx, under=ETERNAL):
    return [r["path_key"] for r in con.execute("SELECT org_id, path_key FROM org_unit WHERE path_key LIKE ? ORDER BY path_key", (under + "%",))
            if r["org_id"] in ctx["org_ids"]]


def _shown_codes(html):
    """화면에 실린 사람의 사번. 팀원 표와 명단의 줄 표시(data-agent, data-roster-agent)에서 읽는다."""
    return set(re.findall(r'data-(?:roster-)?agent="([^"]+)"', html))


def _fake_today(monkeypatch, day):
    """화면과 엔진이 보는 오늘을 바꾼다. 화면(app/web/routes/promo.py)과 엔진(app/engine/promotion.py)의 date 를 함께 바꾼다."""
    class Day(date):
        @classmethod
        def today(cls):
            return cls.fromisoformat(day)
    monkeypatch.setattr(promo_routes, "date", Day)
    monkeypatch.setattr(pm, "date", Day)


@pytest.fixture(autouse=True)
def _pinned_today(monkeypatch):
    """샘플 계약은 2026-11-16 까지다. 10월 창이 끝나고 11월 창이 진행 중인 날로 오늘을 붙들어 둔다. 날짜가 지나도 같은 것을 본다."""
    _fake_today(monkeypatch, TODAY.isoformat())


def _num(cell):
    return int(cell.split("명")[0]) if cell and cell[0].isdigit() else None


# ================================================================ 대표님이 확정한 조건과 샘플
def _floors(p):
    return {int(seq): [t["min_value"] for t in rows] for seq, rows in p["window_tiers"].items()}


def _months(p):
    return [(w["date_from"], w["date_to"]) for w in p["windows"]]


Q4 = [("2026-10-01", "2026-10-31"), ("2026-11-01", "2026-11-30"), ("2026-12-01", "2026-12-31")]


def test_sample_boom_up_matches_confirmed_terms(env):
    """흥국 BOOM-UP: 10~12월 청약 기준, 철회 제외. 월 5만, 10만, 15만 연속 그리고 합산 40만(both)."""
    con = db.connect(env["db"])
    p = pm._load(con, _pid(con, BOOM))
    assert _months(p) == Q4 and p["combine_rule"] == "both"
    assert _floors(p) == {1: [5 * MAN], 2: [10 * MAN], 3: [15 * MAN]}
    assert [t["min_value"] for t in p["tiers"]] == [40 * MAN]
    assert p["rules"]["count_basis"] == "application" and p["date_basis"] == "application_date"
    assert "withdrawn" not in pm._statuses(p) and "applied" in pm._statuses(p)
    con.close()


def test_sample_db_life_matches_confirmed_terms(env):
    """DB생명 여행시상 변경판: 1-1 은 10만, 20만, 40만. 1-2 는 10월 미달성자의 11월 25만, 12월 45만. 조건 2 는 합산 100만. 하나만 채우면 달성.
    2배 인정은 10월과 12월의 1~2주차만, 원보험료 10만원까지, 연속 조건에만."""
    con = db.connect(env["db"])
    p = pm._load(con, _pid(con, DBL))
    assert _months(p) == Q4 and p["combine_rule"] == "either"
    assert _floors(p) == {1: [10 * MAN], 2: [20 * MAN], 3: [40 * MAN]}
    assert [t["min_value"] for t in p["tiers"]] == [100 * MAN]
    alts = p["rules"]["alt_chains"]
    assert len(alts) == 1
    assert {int(k): v for k, v in alts[0]["windows"].items()} == {2: 25 * MAN, 3: 45 * MAN} and [int(x) for x in alts[0]["requires_missed"]] == [1]
    b = p["rules"]["boost"]
    assert b["factor"] == 2 and b["cap"] == 10 * MAN and b["applies_to"] == "each" and not b.get("month_days")
    assert [list(x) for x in b["ranges"]] == [["2026-10-01", "2026-10-14"], ["2026-12-01", "2026-12-14"]]      # 11월은 없다
    con.close()


def test_sample_convention_opens_at_15_achievers_of_all_eternal(env):
    """DB생명 Convention: 이터널 전체 달성 15명 이상이면 열린다. 여행시상에 딸린 시책이다."""
    con = db.connect(env["db"])
    p = pm._load(con, _pid(con, CONV))
    r = p["rules"]
    assert r["gate_min_achievers"] == 15 and r["gate_org_path"] == ETERNAL and r["parent_promo_id"] == _pid(con, DBL)
    g = pm.org_gate(con, p, TODAY)
    assert g["min"] == 15 and g["open"] is (g["n"] >= 15) and g["left"] == max(15 - g["n"], 0)
    con.close()


def test_sample_dream_plus_windows_rule_and_exclusions(env):
    """삼성 드림+: 창은 10월 19일에서 31일, 11월, 12월. 창마다 다섯 단계이고 낮은 쪽 단계다. 제외 상품, 품보, 철회는 세지 않는다."""
    con = db.connect(env["db"])
    p = pm._load(con, _pid(con, DREAM))
    assert _months(p) == [("2026-10-19", "2026-10-31"), ("2026-11-01", "2026-11-30"), ("2026-12-01", "2026-12-31")]
    assert p["combine_rule"] == "all" and {seq: len(v) for seq, v in _floors(p).items()} == {1: 5, 2: 5, 3: 5}
    assert set(p["rules"]["product_exclude"]) >= {"단체", "실손", "펫", "캣", "재물"}
    assert "cancelled" not in pm._statuses(p) and "withdrawn" not in pm._statuses(p)
    con.close()


# ================================================================ 범위 공격
@pytest.mark.parametrize("bonbu", ["해온본부", "H본부"])
def test_manager_cannot_reach_other_orgs_by_address(env, bonbu):
    """본부장이 다른 본부, 위 조직, 없는 조직을 org 로 넣으면 403 이고 거부 기록이 남는다. row 와 who 로 돌려도 범위 밖 사람은 실리지 않는다."""
    con = db.connect(env["db"])
    head = HEAD_OF[bonbu]
    ctx = _ctx(con, head)
    scope = set(_codes(con, ctx["agent_ids"]))
    c = login(env, head)
    others = [b for b in BONBU if b != bonbu]
    leaf = {b: con.execute("SELECT name FROM org_unit WHERE path_key LIKE ? ORDER BY path_key LIMIT 1", (_org(b) + " > team:%",)).fetchone()[0] for b in BONBU}
    own, own_leaf = _org(bonbu), _org(bonbu, leaf[bonbu])
    denied_before = con.execute("SELECT COUNT(*) FROM audit_log WHERE actor=? AND action='access_denied'", (head,)).fetchone()[0]
    n_denied = 0
    for name in (BOOM, DBL, CONV, DREAM):
        pid = _pid(con, name)
        base = f"/promo/progress?view=org&p={pid}"
        # 범위 밖 조직과 없는 조직
        outside = [_org(b) for b in others] + [_org(b, leaf[b]) for b in others] + [ETERNAL, "hq:직할본부", "없는조직", "' OR 1=1 --"]
        for o in outside:
            for tail in ("", "&direct=1", "&who=target&row=all", f"&who=final&row={quote(own)}", "&ym=202610&who=target"):
                r = c.get(f"{base}&org={quote(o)}{tail}")
                assert r.status_code == 403, (head, name, o, tail, r.status_code)
                n_denied += 1
        # 자기 조직에서 row 를 범위 밖 조직으로 돌린다. 줄이 없으니 404 다
        for o in ("", f"&org={quote(own)}", f"&org={quote(own_leaf)}"):
            for row in [_org(b) for b in others] + [_org(b, leaf[b]) for b in others] + [ETERNAL, "zzz"]:
                for who in ("target", "final", "active", "w1", "sum"):
                    r = c.get(f"{base}{o}&who={who}&row={quote(row)}")
                    assert r.status_code == 404, (head, name, o, row, who, r.status_code)
        # 없는 명단 열쇠
        for who in ("nope", "w0", "w9", "alt_w9", "chain", "target'--", "W1"):
            assert c.get(f"{base}&who={quote(who)}&row=all").status_code == 404, (head, name, who)
        # 열리는 명단에는 범위 안 사람만 있다
        for who in ("target", "active", "inactive", "near", "final", "missed"):
            for o in ("", f"&org={quote(own_leaf)}"):
                r = c.get(f"{base}{o}&who={who}&row=all")
                assert r.status_code == 200, (head, name, who, o)
                assert _shown_codes(r.text) <= scope, (head, name, who, sorted(_shown_codes(r.text) - scope))
                for other in others:                                # 다른 본부의 이름도 화면에 없다
                    assert other not in r.text, (head, name, who, other)
    after = con.execute("SELECT COUNT(*) FROM audit_log WHERE actor=? AND action='access_denied' AND target='/promo/progress org'", (head,)).fetchone()[0]
    assert after - denied_before >= n_denied
    con.close()


def test_manager_other_views_ignore_roster_keys(env):
    """팀원 현황과 내 현황에 who, row, org 를 붙여도 명단이 열리지 않고 범위 밖 사람이 실리지 않는다."""
    con = db.connect(env["db"])
    head = HEAD_OF["해온본부"]
    scope = set(_codes(con, _ctx(con, head)["agent_ids"]))
    c = login(env, head)
    pid = _pid(con, DBL)
    for url in (f"/promo/progress?p={pid}&who=target&row=all&org={quote(_org('금탑본부'))}", f"/promo/progress?view=me&who=target&row=all&org={quote(ETERNAL)}",
                f"/promo/progress?view=team&p={pid}&who=final&direct=1"):
        r = c.get(url)
        assert r.status_code == 200, url
        assert "roster" not in r.context or not r.context["roster"]
        assert 'id="roster"' not in r.text and _shown_codes(r.text) <= scope, url
    con.close()


def test_agent_gets_no_org_view_no_roster_and_no_other_names(env):
    """설계사는 조직별 보기와 명단을 열 수 없다. 내 화면에는 다른 사람의 이름과 사번이 없다."""
    con = db.connect(env["db"])
    code = "T0113"
    me = con.execute("SELECT agent_id, name FROM agent WHERE internal_code=?", (code,)).fetchone()
    c = login(env, code)
    pid = _pid(con, DBL)
    for tail in ("", f"&p={pid}", f"&p={pid}&who=target&row=all", f"&org={quote(ETERNAL)}", f"&org={quote(_org('H본부'))}&who=final", "&direct=1", "&ym=202610"):
        assert c.get(f"/promo/progress?view=org{tail}").status_code == 403, tail
    assert con.execute("SELECT 1 FROM audit_log WHERE actor=? AND action='access_denied' AND target='/promo/progress org'", (code,)).fetchone()
    others = {r["name"] for r in con.execute("SELECT name FROM agent WHERE agent_id!=?", (me["agent_id"],))} - {me["name"]}
    other_codes = {r[0] for r in con.execute("SELECT internal_code FROM agent WHERE agent_id!=?", (me["agent_id"],))}
    for url in ("/promo/progress", f"/promo/progress?who=target&row=all&org={quote(ETERNAL)}&p={pid}", "/promo/progress?view=team", "/promo/progress?view=me&direct=1",
                "/promo/progress?ym=202610", "/promo/list", "/"):
        r = c.get(url)
        assert r.status_code == 200, url
        assert 'id="roster"' not in r.text and "data-org-rows" not in r.text and not _shown_codes(r.text), url
        assert not [1 for n in others if n in r.text], (url, "다른 사람의 이름이 있다")
        assert not [x for x in other_codes if re.search(rf"\b{x}\b", r.text)], url
    con.close()


def test_unpublished_and_other_month_promo_ids_fall_back(env):
    """게시하지 않은 초안과 그 달에 없는 프로모션의 번호를 넣으면 첫 프로모션으로 돌아간다. 초안의 이름과 숫자는 보이지 않는다."""
    path = copy_db(env, "draft")
    con = db.connect(path)
    new = pm.copy_bridge(con, _pid(con, BRIDGE), period_ym="202612", windows=[("2026-12-16", "2026-12-31"), ("2027-01-01", "2027-01-15")], by="admin",
                         name="검증 초안 브릿지")
    for who in (HEAD_OF["H본부"], "T0113"):
        c = login(env, who, path)
        for url in (f"/promo/progress?p={new['promo_id']}", f"/promo/progress?view=org&p={new['promo_id']}", f"/promo/progress?view=me&p={new['promo_id']}",
                    f"/promo/progress?p={_pid(con, DREAM_PAST)}", "/promo/progress?p=999999", "/promo/list"):
            r = c.get(url)
            if who == "T0113" and "view=org" in url:
                assert r.status_code == 403
                continue
            assert r.status_code == 200, (who, url)
            assert "검증 초안 브릿지" not in r.text, (who, url)
            sel = r.context.get("sel")
            if sel:
                assert sel["promo_id"] not in (new["promo_id"], _pid(con, DREAM_PAST)) and sel["status"] == "published", (who, url)
    con.close()


# ================================================================ 금액과 최다 달성 본부
def test_manager_org_pages_carry_no_amounts_but_main_does(env):
    """금액이 있는 정률 시책으로 본다. 메인 관리자의 조직별 표에는 조직마다 예상 시상이 있고, 같은 조직을 보는 관리자의 표, 자세히, 명단,
    사람 목록에는 그 금액도 사람별 금액도 없다."""
    con = db.connect(env["db"])
    head = "M0001"                                                   # 기존 샘플의 본부장. 범위 안에 정률 시책의 예상 금액이 있다
    ctx = _ctx(con, head)
    own = con.execute("SELECT path_key FROM org_unit WHERE org_id=?", (ctx["heads"][0]["org_id"],)).fetchone()[0]
    ref = pm.ref_date(ctx["ym"])
    admin, c = login(env, "admin"), login(env, head)
    checked = 0
    for pid in [r[0] for r in con.execute("SELECT promo_id FROM promotion WHERE kind='rate' AND status='published' AND recipient_level='fc' ORDER BY promo_id")]:
        p = pm._load(con, pid)
        res = pm.progress(con, p, sorted(ctx["agent_ids"]), ref)
        person = {f"{int(r['amount']):,}원" for r in res.values() if r["amount"]}
        if not person:
            continue
        url = f"/promo/progress?view=org&p={pid}&org={quote(own)}"
        top = admin.get(url)
        assert top.status_code == 200 and top.context["show_amount"] is True
        amts = top.context["data"]["amounts"]
        org_amounts = {f"{int(v):,}원" for v in list(amts["by_org"].values()) + [amts["total"]] if v}
        assert org_amounts and all(f"예상 {a}" in top.text for a in org_amounts), pid
        mine = c.get(url)
        assert mine.status_code == 200 and mine.context["sel"]["promo_id"] == pid
        data = mine.context["data"]
        assert "amounts" not in data and mine.context["show_amount"] is False
        pages = [mine]
        for row in data["rows"]:
            pages.append(c.get(html_lib.unescape(row["href"])))                                          # 한 단계 아래와 사람 목록
        for who in ("target", "active", "final", "near", "inactive"):
            pages.append(c.get(f"{url}&who={who}&row=all"))                                               # 명단
        # 실적 값과 글자가 같은 금액은 가려낼 수 없어 뺀다. 화면에 실리는 실적은 사람별 실적과 조직 줄의 합계다
        shown = {r["value"] for r in res.values()}
        for pg in pages:
            d = pg.context["data"]
            shown |= {x["value"] for x in d["rows"]} | {d["total"]["value"]}
        money = (org_amounts | person) - {f"{int(v):,}원" for v in shown}
        assert money, pid
        for pg in pages:
            assert pg.status_code == 200 and "amounts" not in pg.context["data"] and pg.context["show_amount"] is False, pid
            body = pg.text.split("<main", 1)[-1]
            assert "예상 시상" not in body, pid
            assert not [a for a in money if a in body], (pid, "관리자 화면에 금액이 있다")
            checked += 1
        assert [a for a in money if a in top.text], pid                                                  # 같은 금액이 메인 관리자 화면에는 있다
    assert checked >= 6
    con.close()


def test_top_org_and_other_bonbu_show_to_main_only(env):
    """최다 달성 본부는 메인 관리자에게만 보인다. 본부장의 화면에는 최다 달성 칸도 다른 본부의 이름도 없다."""
    con = db.connect(env["db"])
    admin = login(env, "admin")
    for name in (BOOM, CONV):
        pid = _pid(con, name)
        top = admin.get(f"/promo/progress?view=org&p={pid}")
        assert top.context["top"] and 'data-top-org="1"' in top.text and "메인 관리자만 봅니다" in top.text, name
        winners = {x["name"] for x in top.context["top"]["rows"]}
        for bonbu, head in HEAD_OF.items():
            c = login(env, head)
            for url in (f"/promo/progress?view=org&p={pid}", f"/promo/progress?p={pid}", "/promo/progress?view=me", "/"):
                r = c.get(url)
                assert r.status_code == 200, (head, url)
                assert r.context.get("top") is None and 'data-top-org="1"' not in r.text, (head, url)
                body = r.text.split("<main", 1)[-1].split("</main>")[0]
                for other in set(BONBU) - {bonbu}:
                    assert other not in body, (head, url, other, other in winners)
    con.close()


# ================================================================ 문턱 카드
def test_gate_card_is_numbers_only_and_same_for_every_viewer(env):
    """Convention 문턱 카드는 숫자만 싣는다. 메인 관리자, 본부장, 설계사에게 같은 숫자이고 조직별 표의 여행시상 달성 인원과 같다."""
    con = db.connect(env["db"])
    names = {r["name"] for r in con.execute("SELECT name FROM agent")}
    admin = login(env, "admin")
    org = admin.get(f"/promo/progress?view=org&p={_pid(con, DBL)}")
    achieved = org.context["data"]["total"]["n_achieved"]
    seen = set()
    for who, urls in (("admin", ("/", "/promo/progress", "/promo/progress?view=org")), (HEAD_OF["피닉스본부"], ("/promo/progress", "/promo/progress?view=org", "/promo/progress?view=me")),
                      ("T0113", ("/promo/progress",)), ("T0124", ("/promo/progress",))):
        c = login(env, who)
        for url in urls:
            r = c.get(url)
            assert r.status_code == 200, (who, url)
            gates = r.context["gates"] if "gates" in r.context else r.context["d"]["gates"]
            assert len(gates) == 1, (who, url)
            g = gates[0]
            assert set(g) <= {"org_path", "org_name", "min", "n", "open", "left", "ym", "note", "promo_id", "name", "org_title"}, (who, url)
            seen.add((g["n"], g["min"], g["open"], g["left"]))
            block = r.text.split('data-gates="1"', 1)[1].split("</div>\n</div>", 1)[0]
            assert f"{g['n']}명 / 15명" in block and not [1 for n in names if n in block], (who, url)
    assert seen == {(achieved, 15, achieved >= 15, max(15 - achieved, 0))}, seen
    con.close()


# ================================================================ 쓰기: 판 메모
class _Forms(HTMLParser):
    """화면의 폼을 사람이 누른 그대로 읽는다. [{action, fields: [(이름, 값)]}]"""

    def __init__(self):
        super().__init__()
        self.forms, self.cur, self.sel, self.area = [], None, None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.cur = {"action": a.get("action"), "fields": []}
            self.forms.append(self.cur)
        if self.cur is None:
            return
        if tag == "input" and a.get("name"):
            kind = a.get("type", "text")
            if kind in ("checkbox", "radio"):
                if "checked" in a:
                    self.cur["fields"].append((a["name"], a.get("value", "on")))
            elif kind not in ("submit", "button"):
                self.cur["fields"].append((a["name"], a.get("value", "")))
        elif tag == "select":
            self.sel = [a.get("name"), None, None]
        elif tag == "option" and self.sel is not None:
            if self.sel[2] is None:
                self.sel[2] = a.get("value", "")
            if "selected" in a:
                self.sel[1] = a.get("value", "")
        elif tag == "textarea":
            self.area = [a.get("name"), ""]

    def handle_data(self, data):
        if self.area is not None:
            self.area[1] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.cur = None
        elif tag == "select" and self.sel is not None and self.cur is not None:
            self.cur["fields"].append((self.sel[0], self.sel[1] if self.sel[1] is not None else (self.sel[2] or "")))
            self.sel = None
        elif tag == "textarea" and self.area is not None and self.cur is not None:
            self.cur["fields"].append((self.area[0], self.area[1].lstrip("\n")))
            self.area = None


def _edit_form(client, pid) -> dict:
    """구간과 조건 입력의 고치기 폼을 화면에 보이는 값 그대로 읽는다. 원안 폼과 복사 폼은 뺀다."""
    page = client.get(f"/promo/tiers?edit={pid}")
    assert page.status_code == 200
    parser = _Forms()
    parser.feed(page.text)
    forms = [f for f in parser.forms if f["action"] == "/promo/tiers" and not any(k == "action" and v in ("copy_bridge", "original") for k, v in f["fields"])]
    assert len(forms) == 1
    data = {}
    for k, v in forms[0]["fields"]:
        data.setdefault(k, []).append(v)
    data = {k: (v if len(v) > 1 else v[0]) for k, v in data.items() if k != "csrf_token"}
    data.setdefault("action", "update")
    return data


def _terms(p) -> dict:
    """프로모션의 조건. 줄 번호처럼 다시 쓰면 바뀌는 것은 뺀다."""
    row = lambda t: (t["tier_no"], t["min_value"], t["reward_kind"], t["reward_value"], t["reward_label"])
    return {"name": p["name"], "kind": p["kind"], "combine_rule": p["combine_rule"], "metric": p["metric"], "date_basis": p["date_basis"],
            "windows": [(w["seq"], w["label"], w["date_from"], w["date_to"]) for w in p["windows"]], "tiers": [row(t) for t in p["tiers"]],
            "window_tiers": {int(s): [row(t) for t in rows] for s, rows in p["window_tiers"].items()}, "rules": p["rules"]}


def test_version_note_main_only_with_csrf_and_trace(env):
    """판 메모는 메인 관리자가 고칠 때만 붙는다. 관리자와 설계사의 POST 는 403 이고, CSRF 가 없으면 403 이다.
    메모는 고친 사람과 함께 그 고치기의 변경 기록 한 줄에 붙고, 화면에는 글자 그대로 보인다(태그로 풀리지 않는다)."""
    path = copy_db(env, "note")
    con = db.connect(path)
    pid = _pid(con, BOOM)
    main = login(env, "admin", path)
    data = _edit_form(main, pid)
    note = "검증 메모 <b>굵게</b> 11월 기준 확인"
    data.update(caution="검증에서 고친 유의사항입니다.", version_note=note)
    n_notes = lambda: con.execute("SELECT COUNT(*) FROM promotion_version_note").fetchone()[0]
    n_updates = lambda: con.execute("SELECT COUNT(*) FROM audit_log WHERE action='promotion_update'").fetchone()[0]
    before = (n_notes(), n_updates(), pm._load(con, pid)["caution"])
    for who in (HEAD_OF["H본부"], "T0113"):
        c = login(env, who, path)
        assert c.post("/promo/tiers", data=data).status_code == 403, who
        assert c.get(f"/promo/tiers?edit={pid}").status_code == 403, who
        assert con.execute("SELECT 1 FROM audit_log WHERE actor=? AND action='access_denied' AND target='/promo/tiers'", (who,)).fetchone(), who
    assert main.post("/promo/tiers", data=data, csrf=False).status_code == 403
    assert (n_notes(), n_updates(), pm._load(con, pid)["caution"]) == before
    r = main.post("/promo/tiers", data=data)
    assert r.status_code == 303 and "/promo/publish?msg=" in r.headers["location"], unquote(r.headers.get("location", ""))
    row = con.execute("SELECT * FROM promotion_version_note WHERE promo_id=? ORDER BY note_id DESC LIMIT 1", (pid,)).fetchone()
    assert row and row["note"] == note and row["written_by"] == "admin"
    log = con.execute("SELECT * FROM audit_log WHERE id=?", (row["audit_id"],)).fetchone()
    assert log and log["action"] == "promotion_update" and log["actor"] == "admin" and log["target"].startswith(f"{pid}:") and "caution" in log["target"]
    # 화면: 판 이력에 메모가 글자 그대로 보인다. 설계사에게도 같다
    for who in ("admin", "T0113"):
        text = login(env, who, path).get(f"/promo/progress?p={pid}").text
        assert html_lib.escape(note) in text and "<b>굵게</b>" not in text, who
    # 바뀐 것이 없는 고치기는 판을 만들지 않고 메모도 붙지 않는다
    again = _edit_form(main, pid)
    again["version_note"] = "바뀐 것 없는 메모"
    r = main.post("/promo/tiers", data=again)
    assert r.status_code == 303 and "바뀐 것이 없습니다" in unquote(r.headers["location"])
    assert n_notes() == before[0] + 1 and not con.execute("SELECT 1 FROM promotion_version_note WHERE note='바뀐 것 없는 메모'").fetchone()
    con.close()


# ================================================================ 쓰기: 보상 선택과 참석
def test_reward_choice_refuses_other_roles_bad_values_and_unconfirmed(env):
    """보상 선택과 참석 기록. 범위 안 달성자가 있는 본부장도 403 이고 거부 기록이 남는다. 확정하지 않았거나 진행 중인 프로모션, 모르는 값,
    없는 번호는 기록하지 않고 500 도 아니다. 변경 기록에는 사번만 있고 이름은 없다."""
    path = copy_db(env, "choice2")
    con = db.connect(path)
    pid = _pid(con, DREAM_PAST)
    main = login(env, "admin", path)
    rows = main.get(f"/promo/archive?ym=202609&p={pid}").context["choice"]["rows"]
    assert rows
    first = rows[0]
    bonbu = next(b for b in BONBU if first["agent_id"] in _ctx(con, HEAD_OF[b])["agent_ids"])          # 그 달성자가 범위 안인 본부장
    good = {"action": "reward_choice", "promo_id": str(pid), "ym": "202609", f"choice_{first['agent_id']}": "travel2", f"attend_{first['agent_id']}": "no"}
    for who in (HEAD_OF[bonbu], first["code"]):                                                   # 본부장과 달성자 본인
        c = login(env, who, path)
        assert c.post("/promo/archive", data=good).status_code == 403, who
        assert con.execute("SELECT 1 FROM audit_log WHERE actor=? AND action='access_denied' AND target='/promo/archive reward_choice'", (who,)).fetchone(), who
        page = c.get(f"/promo/archive?ym=202609&p={pid}")
        assert page.status_code == 200 and 'data-choice-form="1"' not in page.text and "보상 선택 기록" not in page.text, who
    assert not promo_extra.choices(con, pid)
    bad = [({"action": "reward_choice"}, 400), ({"action": "reward_choice", "promo_id": "x"}, 400), ({"action": "reward_choice", "promo_id": "999999"}, 303),
           ({"action": "reward_choice", "promo_id": str(_pid(con, DREAM)), f"choice_{first['agent_id']}": "gold"}, 303),          # 진행 중
           ({**good, f"choice_{first['agent_id']}": "cash"}, 303), ({**good, f"attend_{first['agent_id']}": "maybe"}, 303),
           ({**good, "choice_999999": "gold"}, 303)]
    for data, want in bad:
        r = main.post("/promo/archive", data=data)
        assert r.status_code == want, (sorted(data), r.status_code)
        if want == 303:
            assert "err=" in r.headers["location"], sorted(data)
    assert not promo_extra.choices(con, pid) and not con.execute("SELECT 1 FROM audit_log WHERE action='promotion_reward_choice'").fetchone()
    assert main.post("/promo/archive", data=good).status_code == 303
    log = con.execute("SELECT * FROM audit_log WHERE action='promotion_reward_choice' ORDER BY id DESC LIMIT 1").fetchone()
    assert log["actor"] == "admin" and log["target"] == f"{pid}:1명"
    assert first["code"] in log["before_json"] and first["code"] in log["after_json"] and "travel2" in log["after_json"]
    assert first["name"] not in log["before_json"] + log["after_json"] + log["target"]
    # 확정을 무르면 기록 폼이 닫히고 POST 도 받지 않는다
    assert main.post("/promo/archive", data={"action": "revoke", "promo_id": str(pid), "ym": "202609", "agree": "yes", "reason": "검증에서 무름"}).status_code == 303
    assert not pm.confirmation(con, pid)
    r = main.post("/promo/archive", data={**good, f"choice_{first['agent_id']}": "gold"})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert promo_extra.choices(con, pid)[first["agent_id"]]["choice"] == "travel2"
    assert 'data-choice-form="1"' not in main.get(f"/promo/archive?ym=202609&p={pid}").text
    con.close()


# ================================================================ 쓰기: 브릿지 복사
def test_copy_bridge_bad_input_makes_nothing_and_good_copy_is_traced(env):
    """브릿지 복사에 없는 번호, 브릿지가 아닌 프로모션, 틀린 날짜, 겹치는 창을 넣으면 초안이 생기지 않고 오류 안내로 돌아간다.
    만든 초안은 미게시라 설계사와 관리자에게 보이지 않고, 변경 기록에 만든 사람과 원본이 남는다."""
    path = copy_db(env, "copy2")
    con = db.connect(path)
    src = _pid(con, BRIDGE)
    main = login(env, "admin", path)
    base = {"action": "copy_bridge", "promo_id": str(src), "period_ym": "2027-02", "c_from_1": "2027-02-16", "c_to_1": "2027-02-28",
            "c_from_2": "2027-03-01", "c_to_2": "2027-03-15", "name": "검증 복사"}
    n = lambda: con.execute("SELECT COUNT(*) FROM promotion").fetchone()[0]
    start = n()
    bad = [{**base, "promo_id": "999999"}, {**base, "promo_id": "abc"}, {**base, "promo_id": str(_pid(con, DBL))}, {**base, "c_from_1": "2027-13-40"},
           {**base, "c_from_1": "2027-02-28", "c_to_1": "2027-02-16"}, {**base, "c_from_2": "2027-02-20"}, {**base, "period_ym": ""}, {**base, "period_ym": "20-27"},
           {**base, "c_to_2": ""}, {k: v for k, v in base.items() if k != "promo_id"}]
    for data in bad:
        r = main.post("/promo/tiers", data=data)
        assert r.status_code == 303 and "err=" in r.headers["location"], (sorted(data.items()), r.status_code)
    assert n() == start and not con.execute("SELECT 1 FROM audit_log WHERE action='promotion_copy' AND actor='admin'").fetchone()
    r = main.post("/promo/tiers", data=base)
    assert r.status_code == 303 and "/promo/tiers?edit=" in r.headers["location"]
    new = con.execute("SELECT promo_id, status FROM promotion WHERE name='검증 복사'").fetchone()
    assert new["status"] == "draft"
    log = con.execute("SELECT * FROM audit_log WHERE action='promotion_copy' AND target=?", (f"{new['promo_id']}:{src}",)).fetchone()
    assert log and log["actor"] == "admin" and '"status": "draft"' in log["after_json"]
    for who in (HEAD_OF["H본부"], "T0113"):
        c = login(env, who, path)
        for url in ("/promo/list", "/promo/progress", "/promo/progress?ym=202610"):
            assert "검증 복사" not in c.get(url).text, (who, url)
    con.close()


# ================================================================ 같은 숫자: 표의 숫자와 명단
def _check_org_page(c, con, url, scope, problems, team_cache):
    """조직별 표 한 장. 줄의 합과 합계 줄, 표의 숫자와 명단 인원, 명단의 사람이 그 줄의 대상자인지, 팀원 달성 현황과 같은지를 본다.
    어긋난 것을 problems 에 (주소, 무엇, 열쇠) 로 쌓고 따라간 명단 수를 돌려준다."""
    page = c.get(url)
    if page.status_code != 200:
        problems.append((url, "status", page.status_code))
        return 0
    data, cols, sel = page.context["data"], page.context["phases"], page.context["sel"]
    tot, rows = data["total"], data["rows"]
    for k in ("n_target", "n_active", "n_achieved", "n_near", "n_inactive"):
        if sum(x[k] for x in rows) != tot[k]:
            problems.append((url, "줄의 합", k))
    if tot["n_active"] + tot["n_inactive"] != tot["n_target"]:
        problems.append((url, "가동과 미가동의 합", ""))
    for i, col in enumerate(cols):
        vals, total = [_num(x["cells"][i]) for x in rows], _num(tot["cells"][i])
        if rows and ((total is None) != all(v is None for v in vals) or (total is not None and sum(v or 0 for v in vals) != total)):
            problems.append((url, "칸의 합", col["key"]))
    members = {x["path_key"] + ("|d" if x["direct"] else ""): set(x["agent_ids"]) for x in rows}
    everyone = set().union(*members.values()) if members else set()
    if scope is not None and not everyone <= scope:
        problems.append((url, "범위 밖 대상", len(everyone - scope)))
    # 팀원 달성 현황의 같은 사람
    mq = "&ym=" + page.context["head"]["ym"] if not page.context["head"]["now"] else ""
    key = (sel["promo_id"], mq)
    if key not in team_cache:
        team = c.get(f"/promo/progress?p={sel['promo_id']}{mq}")
        team_cache[key] = {x["agent_id"]: x["r"] for x in team.context["rows"]}
    team = team_cache[key]
    if not everyone <= set(team):
        problems.append((url, "팀원 현황에 없는 사람", len(everyone - set(team))))
    else:
        hit = sum(1 for a in everyone if team[a]["tier"] or (sel["kind"] == "rate" and team[a]["value"] > 0))
        if hit != tot["n_achieved"]:
            problems.append((url, "팀원 현황의 달성 수", ""))
        if abs(sum(team[a]["value"] for a in everyone) - tot["value"]) > 1e-6:
            problems.append((url, "팀원 현황의 실적 합", ""))
    n = 0
    for href, who, row, shown in LINK.findall(page.text):
        row = html_lib.unescape(row)
        r = c.get(html_lib.unescape(href))
        n += 1
        if r.status_code != 200:
            problems.append((url, "명단 status", (who, r.status_code)))
            continue
        roster = r.context["roster"]
        got = [x["agent_id"] for g in roster["groups"] for x in g["people"]]
        if not (roster["n"] == int(shown) == len(got) == len(set(got))):
            problems.append((url, "명단 인원", (who, row == "all")))
        pool = everyone if row == "all" else set().union(*[v for k, v in members.items() if k.split("|")[0] == row] or [set()])
        if not set(got) <= pool:
            problems.append((url, "그 줄의 대상이 아닌 사람", who))
        if scope is not None and not set(got) <= scope:
            problems.append((url, "범위 밖 사람", who))
        if f"{roster['title']} {roster['n']}명" not in html_lib.unescape(r.text):
            problems.append((url, "명단 제목의 인원", who))
    # 표에 없는 열쇠도 엔진 숫자와 같다
    for who, want in (("target", tot["n_target"]), ("final", tot["n_achieved"]), ("missed", tot["n_target"] - tot["n_achieved"]),
                      ("active", tot["n_active"]), ("inactive", tot["n_inactive"]), ("near", tot["n_near"])):
        r = c.get(f"{url}&who={who}&row=all")
        n += 1
        if r.status_code != 200 or r.context["roster"]["n"] != want:
            problems.append((url, "합계 명단", who))
    return n


@pytest.mark.parametrize("who", ["admin", "T0001", "T0003", "T0004"])
def test_every_number_in_the_org_table_matches_its_roster(env, who):
    """조직별 표의 모든 숫자(달 대상자, 조건 1-2, 합산, 최종 달성, 자세히의 대상, 가동, 근접, 미가동)를 눌러 나온 명단의 인원이 그 숫자와 같다.
    본부 줄과 합계 줄, 한 단계 아래 지점 줄, 바로 소속 줄을 모두 본다. 본부 줄의 합은 합계 줄과 같고 팀원 달성 현황과도 같다."""
    con = db.connect(env["db"])
    ctx = _ctx(con, who)
    scope = None if who == "admin" else set(ctx["agent_ids"])
    c = login(env, who)
    orgs = _orgs_in_scope(con, ctx)
    if who == "admin":                                             # 메인 관리자는 맨 위와 본부 넷. 지점 줄은 본부장으로 본다
        orgs = [ETERNAL] + [_org(b) for b in BONBU]
    problems, followed, cache = [], 0, {}
    for name in SAMPLE_PROMOS:
        pid = _pid(con, name)
        for o in orgs:
            url = f"/promo/progress?view=org&p={pid}&org={quote(o)}"
            followed += _check_org_page(c, con, url, scope, problems, cache)
            if who != "admin" and o == orgs[0]:                    # 바로 소속 줄을 눌러 들어간 화면의 숫자도 같다
                followed += _check_org_page(c, con, url + "&direct=1", scope, problems, cache)
    assert not problems, problems[:10]
    assert followed >= (300 if who == "admin" else 60), followed
    con.close()


def test_old_sample_promotions_also_match_their_rosters(env):
    """정률, 주차, 연계, 여행, 가동 시상처럼 조건 확장이 없는 기존 프로모션도 표의 숫자와 명단이 같다."""
    con = db.connect(env["db"])
    c = login(env, "admin")
    root = con.execute("SELECT path_key FROM org_unit WHERE parent_id IS NULL ORDER BY org_id LIMIT 1").fetchone()[0]
    problems, followed, cache = [], 0, {}
    for (pid,) in con.execute("SELECT promo_id FROM promotion WHERE promo_id < ? AND status='published' AND recipient_level='fc'", (_pid(con, BOOM),)).fetchall():
        followed += _check_org_page(c, con, f"/promo/progress?view=org&p={pid}&org={quote(root)}&ym=202610", None, problems, cache)
    assert not problems, problems[:10]
    assert followed >= 40
    con.close()


@pytest.mark.parametrize("day", ["2026-10-10", "2026-10-31", "2026-11-01", "2026-12-01", "2027-01-05"])
def test_table_and_roster_agree_on_other_days(env, monkeypatch, day):
    """오늘을 바꿔도 표의 숫자와 명단이 같다. 창이 시작 전, 진행 중, 끝남으로 바뀌는 날을 고른다. 화면이 엔진과 같은 조건을 사람별 결과에 적용하는지 본다.
    시작 전 창에는 숫자가 없고 '시작 전' 만 있으며 그 명단은 404 다. 진행 중 표시는 오늘이 그 창 안일 때만 붙는다."""
    _fake_today(monkeypatch, day)
    con = db.connect(env["db"])
    today = date.fromisoformat(day)
    problems, followed, cache = [], 0, {}
    for who, orgs in (("admin", [ETERNAL]), ("T0003", [_org("H본부")])):
        scope = None if who == "admin" else set(_ctx(con, who)["agent_ids"])
        c = login(env, who)
        for name in (BOOM, DBL, CONV, DREAM, BRIDGE):
            pid = _pid(con, name)
            for o in orgs:
                url = f"/promo/progress?view=org&p={pid}&org={quote(o)}"
                followed += _check_org_page(c, con, url, scope, problems, cache)
                page = c.get(url)
                assert page.context["ref"] == day, (who, name)
                p = pm._load(con, pm._r(page.context["sel"])["parent_promo_id"] or pid)
                tot = page.context["data"]["total"]
                for col, cell in zip(page.context["phases"], tot["cells"]):
                    key = col["key"]
                    if not (key[0] in "wa" and key[1:].isdigit() and key != "a0"):
                        continue
                    w = next(x for x in p["windows"] if x["seq"] == int(key[1:]))
                    f, t = date.fromisoformat(w["date_from"]), date.fromisoformat(w["date_to"])
                    roster = c.get(f"{url}&who={promo_routes.cell_who(key)}&row=all").status_code
                    if today < f:
                        assert cell == "시작 전" and roster == 404, (day, who, name, key, cell, roster)
                    else:
                        assert _num(cell) is not None and roster == 200, (day, who, name, key, cell, roster)
                        assert cell.endswith(" 진행 중") is (f <= today <= t), (day, who, name, key, cell)
    assert not problems, problems[:10]
    assert followed >= 60, followed
    con.close()


# ================================================================ 같은 숫자: 조직별, 팀원, 내 현황의 같은 사람
def _person(r):
    return (round(r["value"], 6), tuple(round(w["value"], 6) for w in r["windows"]), (r["tier"] or {}).get("tier_no"), r["status"], bool(r["broken"]),
            round(r["next"]["gap"], 6) if r["next"] else None)


def test_same_person_same_numbers_on_org_team_and_my_view(env):
    """같은 사람의 실적, 창별 실적, 구간, 상태, 다음 구간까지 남은 값이 조직별 보기의 사람 목록, 팀원 달성 현황, 본인의 내 달성 현황에서 같다.
    메인 관리자와 본부장이 보는 값도 같다."""
    con = db.connect(env["db"])
    admin = login(env, "admin")
    pids = {name: _pid(con, name) for name in SAMPLE_PROMOS}
    team = {pid: {x["code"]: _person(x["r"]) for x in admin.get(f"/promo/progress?p={pid}").context["rows"]} for pid in pids.values()}
    checked = 0
    for bonbu, head in HEAD_OF.items():
        hc = login(env, head)
        leaves = [r[0] for r in con.execute("SELECT path_key FROM org_unit WHERE path_key LIKE ? ORDER BY path_key", (_org(bonbu) + " > team:%",))]
        for pid in pids.values():
            for x in hc.get(f"/promo/progress?p={pid}").context["rows"]:
                assert _person(x["r"]) == team[pid][x["code"]], (head, pid, x["code"])
            for leaf in leaves:
                for c in (admin, hc):
                    people = c.get(f"/promo/progress?view=org&p={pid}&org={quote(leaf)}").context["people"]
                    assert people is not None, (pid, leaf)
                    for x in people:
                        assert _person(x["r"]) == team[pid][x["code"]], (pid, leaf, x["code"])
                        checked += 1
    for (code,) in con.execute("SELECT internal_code FROM agent WHERE internal_code LIKE 'T01%' ORDER BY internal_code").fetchall():
        items = {c["promo_id"]: c for c in login(env, code).get("/promo/progress").context["items"]}
        for pid in pids.values():
            assert _person(items[pid]["r"]) == team[pid][code], (code, pid)
            checked += 1
            # 내 현황의 달별 줄은 엔진의 창 값을 그대로 옮긴다
            for row, w in zip(items[pid]["phase_rows"], items[pid]["r"]["windows"]):
                assert row["value"] == w["value"] and row["met"] is bool(w["tier"]), (code, pid, row["seq"])
    assert checked >= 24 * len(pids) * 2
    con.close()


# ================================================================ 기준일: 지금과 지난 달
def test_future_windows_show_no_numbers_now_and_in_past_month(env):
    """지금 보기와 10월 보기 모두 시작 전 창에는 '시작 전' 만 있고 숫자와 명단 링크가 없다. 10월 보기의 기준일은 10월 31일이다."""
    con = db.connect(env["db"])
    today = TODAY
    c = login(env, "admin")
    for name in (BOOM, DBL, CONV, DREAM):
        pid = _pid(con, name)
        p = pm._load(con, pm._load(con, pid)["rules"]["parent_promo_id"] or pid)
        for mq, ref in (("", today), ("&ym=202610", date(2026, 10, 31))):
            page = c.get(f"/promo/progress?view=org&p={pid}{mq}")
            assert page.status_code == 200 and page.context["ref"] == ref.isoformat(), (name, mq)
            links = {(k, row) for _h, k, row, _n in LINK.findall(page.text)}
            for x in [*page.context["data"]["rows"], page.context["data"]["total"]]:
                for col, cell in zip(page.context["phases"], x["cells"]):
                    key = col["key"]
                    if not (key[0] in "wa" and key[1:].isdigit() and key != "a0"):
                        continue
                    w = next(y for y in p["windows"] if y["seq"] == int(key[1:]))
                    if ref < date.fromisoformat(w["date_from"]):
                        assert cell == "시작 전", (name, mq, key, cell)
                        assert (promo_routes.cell_who(key), x.get("path_key", "all")) not in links, (name, mq, key)
                    else:
                        assert _num(cell) is not None, (name, mq, key, cell)
            if ref < date(2026, 12, 1):
                assert c.get(f"/promo/progress?view=org&p={pid}{mq}&who=w3&row=all").status_code == 404, (name, mq)
    con.close()


def test_known_limit_past_month_counts_contracts_dated_after_the_ref_date(env):
    """알려진 한계의 재현이다. 열린 결정이라 지금 동작을 적어 둔다. 결정이 나서 엔진이 기준일 뒤 계약을 세지 않게 되면 이 시험을 고친다.

    10월 보기(기준일 10월 31일)에서 11월 창은 '시작 전' 인데, 11월 계약이 합산 달성과 최종 달성에 들어 있다.
    DB생명 여행시상은 10월 계약만으로 합산 100만을 채운 사람이 없는데 합산 달성자가 지금 보기와 같은 수로 나온다.
    브릿지는 둘째 창(11월 1일에서 15일)이 '시작 전' 인데 최종 달성이 0명이 아니다.
    10월 창은 기준일이 그 창의 마지막 날이라 지난 달 보기에서도 '진행 중' 으로 보인다."""
    con = db.connect(env["db"])
    c = login(env, "admin")
    pid = _pid(con, DBL)
    p = pm._load(con, pid)
    now = c.get(f"/promo/progress?view=org&p={pid}").context["data"]["total"]
    past_page = c.get(f"/promo/progress?view=org&p={pid}&ym=202610")
    past, cols = past_page.context["data"]["total"], [x["key"] for x in past_page.context["phases"]]
    ids = [a for x in past_page.context["data"]["rows"] for a in x["agent_ids"]]
    res = pm.progress(con, p, ids, date(2026, 10, 31))
    october_only = sum(1 for a in ids if res[a]["windows"][0]["plain"] >= past["sum_target"])        # 10월 31일까지의 계약만으로 합산을 채운 사람
    assert past["cells"][cols.index("w2")] == "시작 전" and past["cells"][cols.index("w3")] == "시작 전"
    assert october_only == 0
    assert past["n_sum"] == now["n_sum"] > 0 and past["n_achieved"] == now["n_achieved"] > 0           # 기준일 뒤 계약이 들어 있다
    assert past["cells"][cols.index("w1")].endswith(" 진행 중")                                      # 지난 달 보기의 10월이 진행 중으로 보인다
    bridge = c.get(f"/promo/progress?view=org&p={_pid(con, BRIDGE)}&ym=202610")
    cells = bridge.context["data"]["total"]["cells"]
    assert cells[1] == "시작 전" and _num(cells[-1]) > 0
    con.close()


# ================================================================ 찾은 문제 (지금 실패한다. 고치면 통과한다)
def test_found_dream_plus_sample_stage_floors_match_confirmed_table(env):
    """찾은 문제 1. 삼성 드림+ 의 단계 기준이 대표님이 확정한 표와 다르다.
    확정: 창별 5단계가 10/20/20, 20/30/30, 30/50/50, 50/100/100, 80/150/150만이다. 샘플(app/samples_promo.py 의 DREAM_STEPS)은 다른 숫자다."""
    con = db.connect(env["db"])
    want = {1: [10 * MAN, 20 * MAN, 30 * MAN, 50 * MAN, 80 * MAN], 2: [20 * MAN, 30 * MAN, 50 * MAN, 100 * MAN, 150 * MAN],
            3: [20 * MAN, 30 * MAN, 50 * MAN, 100 * MAN, 150 * MAN]}
    for name in (DREAM, DREAM_PAST):
        assert _floors(pm._load(con, _pid(con, name))) == want, name
    con.close()


@pytest.mark.parametrize("name", [BOOM, DBL, CONV, DREAM, BRIDGE])
def test_found_saving_the_edit_form_unchanged_keeps_the_terms(env, name):
    """찾은 문제 2. 구간과 조건 입력을 열어 아무것도 바꾸지 않고 저장하면 조건이 그대로여야 하고 판도 늘지 않아야 한다.
    DB생명 여행시상은 추가안의 열쇠가 바뀌어 고친 판이 하나 더 생긴다. 폼이 추가안의 열쇠, 가중 인정 여부, 구간 번호를 싣지 않는다.
    브릿지는 금액을 비운 정액 구간(원문 확인)이라 저장 자체가 막힌다."""
    path = copy_db(env, f"noop-{SAMPLE_PROMOS.index(name)}")
    con = db.connect(path)
    pid = _pid(con, name)
    main = login(env, "admin", path)
    before, versions = _terms(pm._load(con, pid)), len(promo_extra.versions(con, pm._load(con, pid)))
    r = main.post("/promo/tiers", data=_edit_form(main, pid))
    where = unquote(r.headers.get("location", ""))
    assert r.status_code == 303 and "err=" not in where, (name, where.split("err=")[-1][:60])
    after = _terms(pm._load(con, pid))
    assert [k for k in before if before[k] != after[k]] == [], name
    assert "바뀐 것이 없습니다" in where and len(promo_extra.versions(con, pm._load(con, pid))) == versions, name
    con.close()


def test_found_edit_form_keeps_alt_chain_boost_and_tier(env):
    """찾은 문제 2의 돈에 닿는 쪽. 추가안에 가중 인정을 쓰지 않도록(boost 거짓) 둔 프로모션을 폼으로 열어 유의사항만 고치면
    추가안의 가중 인정 여부가 그대로여야 한다. 지금은 기본값(참)으로 돌아가 12월 1~2주차 계약의 인정값이 달라진다 (D-96)."""
    path = copy_db(env, "altboost")
    con = db.connect(path)
    pid = _pid(con, DBL)
    p = pm._load(con, pid)
    rules = {**p["rules"], "alt_chains": [{**p["rules"]["alt_chains"][0], "boost": False}]}
    con.execute("UPDATE promotion SET alt_chains_json=? WHERE promo_id=?", (json.dumps(rules["alt_chains"], ensure_ascii=False), pid))
    con.commit()
    assert pm._load(con, pid)["rules"]["alt_chains"][0]["boost"] is False
    main = login(env, "admin", path)
    data = _edit_form(main, pid)
    data["caution"] = "검증에서 유의사항만 고쳤습니다."
    assert main.post("/promo/tiers", data=data).status_code == 303
    after = pm._load(con, pid)
    assert after["caution"] == "검증에서 유의사항만 고쳤습니다."
    assert after["rules"]["alt_chains"][0]["boost"] is False
    con.close()


def test_found_org_note_promises_leavers_value_but_screen_drops_it(env):
    """찾은 문제 3. 조직별 진척의 안내는 '퇴사자의 퇴사 전 실적은 실적 합계에 넣고 대상, 달성, 순위에서는 뺍니다' 라고 한다.
    화면은 재직자만 엔진에 넘겨서 퇴사자의 실적이 합계에서 빠진다. 안내와 숫자가 서로 맞아야 한다."""
    path = copy_db(env, "leaver")
    con = db.connect(path)
    pid = _pid(con, DBL)
    c = login(env, "admin", path)
    before = c.get(f"/promo/progress?view=org&p={pid}").context["data"]["total"]
    res = pm.progress(con, pm._load(con, pid), [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE internal_code LIKE 'T01%'")], TODAY)
    leaver = next(a for a, r in res.items() if r["value"] > 0 and not r["tier"])
    con.execute("UPDATE agent SET status='terminated' WHERE agent_id=?", (leaver,))
    con.commit()
    page = c.get(f"/promo/progress?view=org&p={pid}")
    after = page.context["data"]["total"]
    assert "퇴사자의 퇴사 전 실적은 실적 합계에 넣고" in page.text
    assert after["n_target"] == before["n_target"] - 1 and after["n_achieved"] == before["n_achieved"]
    kept = abs(after["value"] - before["value"]) < 1e-6
    assert kept and after["n_left"] == 1, "퇴사자의 실적이 합계에서 빠졌다"
    con.close()


def test_found_past_month_progress_of_a_confirmed_promotion_uses_confirmed_values(env):
    """찾은 문제 4. 달 고르기로 지난 달의 확정한 프로모션을 열면 확정한 값이 아니라 다시 계산한 값이 보인다.
    확정한 뒤 계약이 바뀌면 지난 프로모션 화면(확정 값)과 달성 현황의 지난 달 보기(다시 계산)가 같은 사람을 다르게 보인다."""
    path = copy_db(env, "confirmed")
    con = db.connect(path)
    pid = _pid(con, DREAM_PAST)
    assert pm.confirmation(con, pid)
    c = login(env, "admin", path)
    rows = c.get(f"/promo/archive?ym=202609&p={pid}").context["sel"]["rows"]
    one = next(x for x in rows if x["tier_no"])
    con.execute("UPDATE contract SET status='withdrawn' WHERE agent_id=? AND contract_date BETWEEN '2026-07-19' AND '2026-09-30'", (one["agent_id"],))
    con.commit()
    archive = {x["agent_id"]: x for x in c.get(f"/promo/archive?ym=202609&p={pid}").context["sel"]["rows"]}
    assert archive[one["agent_id"]]["tier_no"] == one["tier_no"]                                   # 지난 프로모션은 확정 값 그대로다
    team = {x["agent_id"]: x["r"] for x in c.get(f"/promo/progress?p={pid}&ym=202609").context["rows"]}
    org = c.get(f"/promo/progress?view=org&p={pid}&ym=202609").context["data"]["total"]
    assert (team[one["agent_id"]]["tier"] or {}).get("tier_no") == one["tier_no"], one["code"]
    assert org["n_achieved"] == sum(1 for x in archive.values() if x["tier_no"])
    con.close()


def test_found_copy_bridge_audit_row_reads_in_korean(env):
    """찾은 문제 5. 브릿지 복사의 변경 기록(promotion_copy)이 변경 기록 화면에서 한국어 이름표로 보여야 한다.
    지금은 동작 이름이 코드 그대로이고, 대상의 원본 번호가 '바뀐 칸' 으로 읽힌다."""
    con = db.connect(env["db"])
    row = con.execute("SELECT target FROM audit_log WHERE action='promotion_copy' ORDER BY id DESC LIMIT 1").fetchone()
    assert row
    assert audit_view.action_ko("promotion_copy") != "promotion_copy"
    assert "바뀐 칸" not in audit_view.target_ko("promotion_copy", row["target"], audit_view._Names(con))
    con.close()
