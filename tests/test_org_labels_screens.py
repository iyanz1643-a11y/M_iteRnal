"""조직 이름이 화면마다 같게 보이는가: config/org_labels.yaml 의 규칙을 모든 조직 화면이 org_labels 한 곳으로 따른다.

| 무엇 | 보는 것 |
| --- | --- |
| 규칙이 있는 DB | 조직 화면에 내부 계층 이름(직할본부, 지사·본부, 센터, 팀)과 맨 위 회사 이름이 한 번도 나오지 않는다 |
| 본부 알약 | 본부 알약은 예하 본부 넷에만 붙고, 그 아래는 지점 알약이 붙는다. 시작 조직 자체에는 알약이 없다 |
| 시작 조직 | 조직 선택 상자, 조직 트리, 경로 줄에서 시작 조직의 위쪽 조직이 숨고 맨 위 조직부터 단추는 시작 조직으로 간다 |
| 같은 이름의 지점 | 이름이 같은 지점이 두 본부 아래에 있어도 선택 상자의 글에 본부 이름이 앞에 붙어 구별된다. 값은 org_id 이다 |
| 권한 | 위쪽 조직 id 로 직접 열어도 범위 검사는 그대로다. 관리자는 범위 밖 조직을 열면 403 이고 선택 상자에 범위 밖 조직이 없다 |
| 규칙이 없는 DB | 시작 조직이 없으면 함수가 예전과 같은 값을 내고, 조직 화면에 예전 계층 이름이 그대로 나온다 |

자료는 가상 이름으로 임시 폴더에 새로 만든다. 실제 파일의 이름은 쓰지 않는다. 실패 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
import os
import pathlib
import re
import shutil
import tempfile
from html import unescape
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import db, masters
from app.org import heads, sync
from app.org.sync import org_texts
from app.web import auth, org_labels

PW = "label-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
ROOT = "가상총괄회사"                                          # 맨 위 회사. 규칙이 있는 DB 에서 화면에 나오면 안 된다
START = [("hq", "직할본부"), ("branch", "이터널")]               # config/org_labels.yaml 의 시작 조직 경로
START_KEY = "hq:직할본부 > branch:이터널"
BONBU = {"가상A본부": ["직할지점", "가상A1지점"], "가상B본부": ["직할지점", "가상B1지점"], "가상C본부": ["가상C1지점"], "가상D본부": ["가상D1지점"]}
OTHER = [("hq", "가상갑본부"), ("branch", "가상을지점")]          # 규칙이 없는 DB 의 맨 위 조직 경로
FORBID = ("직할본부", "지사·본부", "센터장", "팀장", ROOT, "메타리치")
BARE = {"센터", "팀", "지사·본부"}                                # 알약, 머리글, 단추, 선택 상자 끝말로 홀로 나오면 안 되는 말


class Client(TestClient):
    def __init__(self, app, db_path):
        super().__init__(app, follow_redirects=False)
        self.db_path = str(db_path)

    def request(self, *a, **kw):
        os.environ["APP_DB"] = self.db_path
        return super().request(*a, **kw)

    def post(self, url, data=None, **kw):
        if data is not None and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


def build(path, start, bonbu):
    """가상 조직 DB. start 아래에 본부와 지점을 만들고 본부마다 설계사와 본부장을 둔다."""
    con = db.init_db(path)
    masters.seed_real_insurers(con)
    sync.ensure_path(con, start, root_name=ROOT)
    n = 0
    for i, (name, teams) in enumerate(bonbu.items(), start=1):
        bon = sync.ensure_path(con, start + [("center", name)], root_name=ROOT)
        head = con.execute("INSERT INTO agent(internal_code, name, hire_date, status, job_type) VALUES(?,?,?,?,?)",
                           (f"L{i:04d}", f"가상장{i}", "2020-01-01", "active", "manager")).lastrowid
        con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (head, bon, "202601"))
        heads.assign_head(con, bon, head, "202601", by="시험")
        for t in teams:
            team = sync.ensure_path(con, start + [("center", name), ("team", t)], root_name=ROOT)
            n += 1
            aid = con.execute("INSERT INTO agent(internal_code, name, hire_date, status, job_type) VALUES(?,?,?,?,?)",
                              (f"L{100 + n:04d}", f"가상설계{n}", "2023-03-01", "active", "fc")).lastrowid
            con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (aid, team, "202601"))
    con.commit()
    auth.seed_accounts(con, PW)
    con.close()


@pytest.fixture(scope="module")
def env():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="orglabels-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    ruled, plain = tmp / "ruled.db", tmp / "plain.db"
    build(ruled, START, BONBU)
    build(plain, OTHER, BONBU)
    from app.web.main import app
    yield {"tmp": tmp, "ruled": ruled, "plain": plain, "app": app}
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


def login(env, which, login_id="admin"):
    c = Client(env["app"], env[which])
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, (login_id, r.status_code)
    return c


def main_html(text: str) -> str:
    m = re.search(r'<main class="wrap">(.*?)</main>', text, re.S)
    assert m, "본문을 찾지 못했습니다"
    return m.group(1)


def segments(html: str) -> list:
    """보이는 글 조각. 태그 사이의 글과 ' · ' 로 이은 끝말을 따로 낸다."""
    out = []
    for seg in re.split(r"<[^>]+>", html):
        seg = unescape(seg).strip()
        if seg:
            out.append(seg)
            if " · " in seg:
                out.append(seg.rsplit(" · ", 1)[1].strip())
    return out


def leaks(html: str) -> list:
    segs = segments(main_html(html))                              # 보이는 글만 본다. 선택지의 값(path_key)은 주소 열쇠라 글이 아니다
    shown = "\n".join(segs)
    bad = [w for w in FORBID if w in shown]
    bad += [s for s in segs if s in BARE]
    return bad


SCREENS = ["/org/tree", "/org/heads", "/org/settings", "/perf/org-compare", "/perf/org-compare?level=team", "/perf/period", "/perf/category",
           "/perf/contracts", "/perf/persistency", "/perf/ranking", "/promo/tiers", "/team/education", "/team/education?level=team",
           "/sales/assign", "/sales/assign?level=team", "/perf/hall", "/edu/courses", "/board", "/team/members", "/team/onboarding", "/team/activity"]


# ================================================================ 규칙이 있는 DB
@pytest.mark.parametrize("url", SCREENS)
def test_ruled_screens_have_no_internal_names(env, url):
    c = login(env, "ruled")
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code)
    assert leaks(r.text) == [], url


def test_org_selects_option_value_is_org_id_and_hides_above(env):
    c = login(env, "ruled")
    con = db.connect(env["ruled"])
    hidden = org_labels.hidden_ids(con)
    assert len(hidden) == 2                                          # 맨 위 회사와 직할본부
    for url in ("/perf/period", "/perf/ranking", "/perf/org-compare", "/org/heads", "/org/settings"):
        html = main_html(c.get(url).text)
        values = {int(v) for v in re.findall(r'<option value="(\d+)"', html)}
        assert values and not (values & hidden), url
    con.close()


def test_same_name_branches_are_told_apart_by_bonbu(env):
    c = login(env, "ruled")
    html = main_html(c.get("/perf/period").text)
    opts = [unescape(o) for o in re.findall(r"<option[^>]*>([^<]*)</option>", html)]
    texts = [o.strip("　 ") for o in opts]
    assert "가상A본부 > 직할지점 · 지점" in texts and "가상B본부 > 직할지점 · 지점" in texts
    assert "가상A본부 · 본부" in texts and "이터널총괄본부" in texts
    assert not any(t.startswith("직할지점") for t in texts)           # 이름만으로는 어느 본부인지 모른다
    # 값은 이름이 아니라 org_id 라서 같은 이름의 두 지점이 따로 고른다
    con = db.connect(env["ruled"])
    ids = [r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE name='직할지점'")]
    con.close()
    assert len(ids) == 2 and all(f'<option value="{i}"' in html for i in ids)


def test_chip_bonbu_only_on_the_four(env):
    c = login(env, "ruled")
    con = db.connect(env["ruled"])
    bonbu = {r[0] for r in con.execute("SELECT org_id FROM org_unit WHERE level='center'")}
    assert len(bonbu) == 4
    for o in con.execute("SELECT org_id, name, level, path_key FROM org_unit").fetchall():
        chip = org_labels.chip(dict(o))
        assert (chip == "본부") == (o["org_id"] in bonbu), "본부 알약은 예하 본부 넷에만 붙는다"
        if o["level"] == "team":
            assert chip == "지점"
        if o["path_key"] == START_KEY:
            assert chip == ""
    con.close()
    tree = main_html(c.get("/org/tree").text)
    chips = re.findall(r'<span class="chip">([^<]*)</span>', tree)
    assert chips.count("본부") == 4 and chips.count("지점") == 6
    heads_ko = re.findall(r'<span class="chip ?">([^<]*장)</span>', tree)
    assert set(heads_ko) == {"본부장"}      # 조직장이 있는 조직은 본부 넷이다. 같은 줄이 휴대폰용과 PC용으로 두 번 그려진다   # 조직장 이름도 화면 계층 이름을 따른다
    assert "이터널총괄본부" in tree and "직할본부" not in tree
    # 계층 카드도 화면 이름이다
    assert re.search(r'<div class="k">본부</div><div class="mid">4', tree) and re.search(r'<div class="k">지점</div><div class="mid">6', tree)
    # 트리는 시작 조직에서 시작한다
    first = re.search(r'<span class="tree-name">([^<]*)<', tree).group(1)
    assert first == "이터널총괄본부"


def test_compare_levels_are_display_names(env):
    c = login(env, "ruled")
    r = c.get("/perf/org-compare")
    assert r.context["lv_opts"] == ["center", "team"] and r.context["LEVEL_NAMES"]["center"] == "본부" and r.context["LEVEL_NAMES"]["team"] == "지점"
    assert r.context["level"] == "center" and r.context["level_ko"] == "본부"
    names = {x["name"] for x in r.context["c"]["rows"]}
    assert names == set(BONBU)
    t = c.get("/perf/org-compare?level=team")
    assert {x["name"] for x in t.context["c"]["rows"]} >= {"가상A본부 > 직할지점", "가상B본부 > 직할지점"}


def test_ranking_and_settings_start_at_start_org(env):
    c = login(env, "ruled")
    r = c.get("/perf/ranking")
    assert r.context["r"]["org"]["path_key"] == START_KEY and r.context["r"]["org"]["text"] == "이터널총괄본부"
    s = c.get("/org/settings")
    assert s.context["org_text"] == "이터널총괄본부"


def test_progress_org_top_button_goes_to_start(env):
    from app.engine import promotion as pm
    con = db.connect(env["ruled"])
    pid = pm.create(con, period_ym="202611", name="시험 프로모션", kind="travel", windows=[("11월", "2026-11-01", "2026-11-30")],
                    tiers=[(100_000, "travel", None, "여행")], by="t", publish=True)
    root = con.execute("SELECT path_key FROM org_unit WHERE parent_id IS NULL").fetchone()[0]
    con.close()
    c = login(env, "ruled")
    r = c.get(f"/promo/progress?view=org&p={pid}")
    assert r.status_code == 200 and r.context["org"]["path_key"] == START_KEY and leaks(r.text) == []
    assert r.context["root_path"] == START_KEY and "맨 위 조직부터" not in r.text
    # 위쪽 조직 주소로 직접 열어도 범위 검사는 그대로(메인 관리자는 열린다). 맨 위 조직부터 단추는 시작 조직으로 간다
    up = c.get(f"/promo/progress?view=org&p={pid}&org={quote(root)}")
    assert up.status_code == 200 and f"org={quote(START_KEY)}" in up.text and "맨 위 조직부터" in up.text


def test_manager_scope_unchanged_and_hidden_url_still_guarded(env):
    con = db.connect(env["ruled"])
    head = con.execute("""SELECT u.login_id, m.org_id FROM org_manager m JOIN user_account u ON u.agent_id=m.agent_id
                          JOIN org_unit o ON o.org_id=m.org_id WHERE o.name='가상A본부'""").fetchone()
    up = con.execute("SELECT org_id FROM org_unit WHERE path_key=?", (START_KEY,)).fetchone()[0]
    other = con.execute("SELECT org_id FROM org_unit WHERE name='가상B본부'").fetchone()[0]
    con.close()
    c = login(env, "ruled", head["login_id"])
    for url in ("/perf/period", "/perf/org-compare", "/org/settings"):
        r = c.get(url)
        assert r.status_code in (200, 403), url
        if r.status_code == 200:
            values = {int(v) for v in re.findall(r'<option value="(\d+)"', main_html(r.text))}
            assert other not in values and up not in values, url
    assert c.get(f"/perf/period?org={other}").status_code == 403           # 범위 밖 조직은 열리지 않는다
    assert c.get(f"/perf/period?org={head['org_id']}").status_code == 200
    assert c.get(f"/org/settings?org={up}").status_code == 403


def test_main_can_still_open_hidden_org_id_directly(env):
    con = db.connect(env["ruled"])
    root = con.execute("SELECT org_id FROM org_unit WHERE parent_id IS NULL").fetchone()[0]
    con.close()
    c = login(env, "ruled")
    for url in (f"/perf/period?org={root}", f"/perf/ranking?org={root}", f"/org/settings?org={root}"):
        r = c.get(url)
        assert r.status_code == 200, url
        assert ROOT not in main_html(r.text), url                      # 열리지만 이름은 가려진다


# ================================================================ 규칙이 없는 DB
def test_plain_db_is_unchanged(env):
    con = db.connect(env["plain"])
    assert org_labels.start_path(con) is None and not org_labels.active(con) and org_labels.hidden_ids(con) == set()
    assert org_labels.texts(con) == org_texts(con)
    assert org_labels.level_names(con, ["hq", "branch", "center", "team"]) == {"hq": "지사·본부", "branch": "지점", "center": "센터", "team": "팀"}
    assert org_labels.visible_levels(con, ["hq", "branch", "center", "team"]) == ["hq", "branch", "center", "team"]
    rows = [dict(r, depth=i) for i, r in enumerate(con.execute("SELECT org_id, name, level, path_key FROM org_unit ORDER BY org_id"))]
    got = org_labels.choose(con, rows)
    ko = {"ga": "GA", "hq": "지사·본부", "branch": "지점", "center": "센터", "team": "팀"}
    assert [(g["indent"], g["label"]) for g in got] == [("　" * r["depth"], f"{r['name']} · {ko[r['level']]}") for r in rows]
    con.close()
    c = login(env, "plain")
    tree = main_html(c.get("/org/tree").text)
    assert "지사·본부" in tree and "센터" in tree and "팀" in tree and "가상갑본부" in tree and ROOT in tree
    comp = c.get("/perf/org-compare")
    assert comp.status_code == 200 and comp.context["LEVEL_NAMES"]["center"] == "센터" and comp.context["LEVEL_NAMES"]["hq"] == "지사·본부"
    period = main_html(c.get("/perf/period").text)
    assert "가상갑본부 · 지사·본부" in period
    rank = c.get("/perf/ranking")
    assert rank.status_code == 200 and rank.context["choices"][0]["name"] == ROOT         # 맨 위 조직부터 예전 그대로
    for url in SCREENS:
        assert c.get(url).status_code == 200, url
