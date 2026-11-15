"""S2 의 F 사람별 화면 가운데 새 계산이 없는 일: 변경 기록의 이름표와 휴대폰 홈 화면 추가(PWA)."""
import io
import json
import os
import pathlib
import re
import tempfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db, masters
from app.web import audit_view, auth

PW = "test-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "static"


@pytest.fixture(scope="module")
def env():
    path = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    con = db.init_db(path)
    masters.seed(con)
    auth.seed_accounts(con, PW)
    os.environ["APP_DB"] = str(path)
    from app.web.main import app
    yield {"con": con, "app": app}
    con.close()


def login(env, login_id="admin"):
    c = TestClient(env["app"], follow_redirects=False)
    tok = TOKEN.search(c.get("/login").text).group(1)
    assert c.post("/login", data={"login_id": login_id, "password": PW, "csrf_token": tok}).status_code == 303
    return c


# ---------------------------------------------------------------- 변경 기록의 이름표
def test_every_known_action_code_has_a_label():
    """기록을 남기는 모듈의 상수가 바뀌어도 이름표가 따라오는지 본다."""
    from app.connect import card_decide
    from app.org import card_grant as cg
    codes = {cg.ACTION_GRANT, cg.ACTION_REVOKE, cg.ACTION_DENIED, cg.ACTION_REQUEST, cg.ACTION_REJECT, cg.ACTION_CANCEL,
             card_decide.AUDIT_PREVIEW, *card_decide.AUDIT_ACTION.values(), "card_link", "card_unlink",
             "login", "login_fail", "password_change", "access_denied",
             *(f"account_{a}" for a in ("issue", "reset", "unlock", "suspend", "activate")),
             "org_head_assign", "org_head_end", "org_deputy_assign", "org_deputy_end", "staff_sync", "org_from_contracts",
             *(f"promotion_{s}" for s in ("draft", "published", "withdrawn", "update")),
             *(f"org_award_{s}" for s in ("create", "update", "active", "inactive", "confirm")), "incentive_approve"}
    missing = sorted(c for c in codes if c not in audit_view.ACTION_KO)
    assert not missing, missing
    assert all(re.search(r"[가-힣]", v) for v in audit_view.ACTION_KO.values())


def test_audit_screen_shows_korean_labels_not_codes(env):
    con = env["con"]
    agent_id, code = con.execute("SELECT agent_id, internal_code FROM agent ORDER BY agent_id LIMIT 1").fetchone()
    org_id = con.execute("SELECT org_id FROM org_unit WHERE level='team' LIMIT 1").fetchone()[0]
    pid = con.execute("INSERT INTO promotion(period_ym, name, kind, created_by, created_at) VALUES('202610','이름표시험 프로모션','rate','t',?)",
                      (db.now(),)).lastrowid
    did = con.execute("""INSERT INTO org_award_division(org_id, name, metric, effective_from, created_by, created_at)
                         VALUES(?, '이름표시험 부문', 'converted_premium', '202610', 't', ?)""", (org_id, db.now())).lastrowid
    con.commit()
    targets = {"promotion_published": str(pid), "promotion_withdrawn": str(pid), "promotion_update": f"{pid}:name,tiers",
               "org_award_create": str(did), "org_award_confirm": f"{did}:202610:3명", "card_grant": "user:M0001",
               "card_approve": f"agent:{agent_id}", "card_preview": "agent:none", "card_grant_denied": "grant:2",
               "org_head_assign": f"org={org_id} agent={code} 202611~", "org_head_end": f"org={org_id} agent_id={agent_id} role=team_head",
               "access_denied": "/data/contracts", "account_issue": code}
    for action in audit_view.ACTION_KO:
        auth.log(con, "admin", action, targets.get(action))
    auth.log(con, "admin", "mystery_code", "raw:1")              # 이름표가 없는 코드는 원문 그대로
    con.commit()
    html = login(env).get("/org/audit").text
    table = html[html.index("<table"):html.index("</table>")]
    shown = {c for c in audit_view.ACTION_KO if re.search(rf"(?<![\w/:]){re.escape(c)}(?![\w])", table)}
    assert not shown, shown
    for want in ("프로모션 게시", "프로모션: 이름표시험 프로모션", "바뀐 칸 이름, 구간", "조직 부문 시상: 이름표시험 부문", "2026년 10월",
                 "계정: M0001", f"설계사: {code}", "설계사: 이어지지 않은 명함", "권한 켜기 2건", "화면: 계약 엑셀", "조직장 지정", "팀장", "mystery_code"):
        assert want in table, want
    assert f"pid={pid}" not in table and f"agent:{agent_id}" not in table


def test_unknown_target_shapes_stay_as_they_are(env):
    names = audit_view._Names(env["con"])
    assert audit_view.target_ko("promotion_published", "999999", names) == "999999"
    assert audit_view.target_ko("card_approve", "agent:999999", names) == "agent:999999"
    assert audit_view.target_ko("something_new", "x=1", names) == "x=1"
    assert audit_view.action_ko("something_new") == "something_new"


# ---------------------------------------------------------------- 휴대폰 홈 화면 추가
def test_manifest_opens_without_login(env):
    c = TestClient(env["app"], follow_redirects=False)
    r = c.get("/static/manifest.webmanifest")
    assert r.status_code == 200 and "manifest+json" in r.headers["content-type"]
    m = json.loads(r.content)
    assert m["name"] == "GA 업무 앱" and m["start_url"] == "/" and m["scope"] == "/" and m["display"] == "standalone"
    sizes = set()
    for ic in m["icons"]:
        got = c.get(ic["src"])
        assert got.status_code == 200 and got.headers["content-type"] == "image/png", ic["src"]
        w, h = Image.open(io.BytesIO(got.content)).size
        assert f"{w}x{h}" == ic["sizes"], ic["src"]
        sizes.add(ic["sizes"])
    assert {"192x192", "512x512"} <= sizes
    page = c.get("/login").text
    assert 'rel="manifest" href="/static/manifest.webmanifest"' in page and "/static/pwa.js" in page


def test_service_worker_scope_and_offline_page(env):
    c = TestClient(env["app"], follow_redirects=False)
    r = c.get("/static/sw.js")
    assert r.status_code == 200 and r.headers["service-worker-allowed"] == "/" and r.headers["cache-control"] == "no-cache"
    off = c.get("/static/offline.html")
    assert off.status_code == 200 and "연결 없음" in off.text and "{{" not in off.text
    assert "service-worker-allowed" not in c.get("/static/app.css").headers


def test_service_worker_keeps_only_static_files():
    """코드 검사. 보관 목록은 /static/ 아래뿐이고, 화면 요청의 응답은 보관하지 않는다."""
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)                                 # 머리말을 뺀 코드만 본다
    listed = re.findall(r"'([^']*)'", re.search(r"const STATIC = \[(.*?)\];", code, re.S).group(1))
    assert listed and all(p.startswith("/static/") for p in listed), listed
    assert "/static/offline.html" in listed and not any(p.endswith((".db", ".json", ".xlsx")) for p in listed)
    for bad in ("'/login'", '"/login"', "'/'", '"/"', "'/me'", "/org/", "/team/", "/promo/"):
        assert bad not in code, bad
    # 보관은 addAll(STATIC) 하나와 STATIC 검사 뒤의 put 하나뿐이다
    assert code.count("addAll(") == 1 and "addAll(STATIC)" in code
    assert code.count(".put(") == 1
    guard_at, put_at, nav_at = code.index("if (STATIC.indexOf(url.pathname) < 0) return;"), code.index(".put("), code.index("req.mode === 'navigate'")
    assert nav_at < guard_at < put_at
    nav_block = code[nav_at:guard_at]
    assert "put" not in nav_block and "caches.open" not in nav_block and "caches.match(OFFLINE)" in nav_block
    assert "req.method !== 'GET'" in code


def test_static_folder_holds_no_data():
    """guard 를 거치지 않는 /static 에는 자료 파일을 두지 않는다."""
    allowed = {".css", ".js", ".png", ".html", ".webmanifest"}
    files = [p for p in STATIC.rglob("*") if p.is_file()]
    assert files and all(p.suffix in allowed for p in files), [p.name for p in files if p.suffix not in allowed]


# ---------------------------------------------------------------- 누르는 크기 48픽셀(docs/12 1장 5)
CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
TAPPABLE = re.compile(r"(?:^|[\s,>+~(])(?:a|button)(?:[.:\[\s]|$)|\.ghost\b|\.pickbox\b|label\b")


def _css_rules():
    """app.css 의 규칙을 (선택자 하나, 선언 문자열) 로 편다. @media 안의 규칙도 같은 꼴로 나온다."""
    src = re.sub(r"/\*.*?\*/", "", (STATIC / "app.css").read_text(encoding="utf-8"), flags=re.S)
    src = re.sub(r"@media[^{]*\{", "", src)                                  # 바깥 괄호를 벗겨 안쪽 규칙만 남긴다
    out = []
    for sel, body in CSS_RULE.findall(src):
        for one in sel.split(","):
            out.append((" ".join(one.split()), body))
    return out


def _min_height(body):
    m = re.search(r"(?:^|;)\s*min-height:\s*(\d+)px", body) or re.search(r"(?:^|;)\s*height:\s*(\d+)px", body)
    return int(m.group(1)) if m else None


@pytest.mark.parametrize("selector", [".nav-groups a", ".nav-right a", ".nav-right button", ".subnav a", ".seg a", "button.ghost", "a.ghost",
                                      ".notif-title a", ".tabbar a", ".tabbar button", ".sheet a.row", "button.ghost.notif-all",
                                      ".notif-act button.ghost", ".cal-bar .seg a", ".promo-acts .ghost"])
def test_tap_targets_are_48px(selector):
    """묶음 메뉴, 거르기 알약, 종과 내 정보, 표 안 단추, 알림 제목 링크, 아래 탭, 더보기 목록의 누르는 곳이 48픽셀 이상이다."""
    hits = [_min_height(b) for s, b in _css_rules() if s == selector and _min_height(b) is not None]
    assert hits and min(hits) >= 48, (selector, hits)


def test_no_tappable_rule_sets_height_under_48px():
    """누르는 요소에 48픽셀보다 작은 높이를 다시 주는 규칙이 없다. 미디어 쿼리 안도 본다."""
    small = [(s, _min_height(b)) for s, b in _css_rules() if TAPPABLE.search(s) and (_min_height(b) or 48) < 48]
    assert not small, small


def test_widened_pills_keep_their_look():
    """알약과 표 안 단추는 보이는 크기를 그대로 두고 투명 테두리로 누르는 곳만 넓힌다. 바탕색이 테두리에 칠해지면 커 보인다."""
    rules = dict(_css_rules())
    for sel in (".subnav a", ".seg a", "button.ghost", "a.ghost"):
        assert "border:" in rules[sel] and "transparent" in rules[sel] and "background-clip:padding-box" in rules[sel], sel


# ---------------------------------------------------------------- 아래 탭과 위 메뉴는 만들어진 첫 화면으로 간다
def _role_logins(env):
    con = env["con"]
    manager = con.execute("""SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id JOIN org_unit o ON o.org_id=m.org_id
                             WHERE o.level='branch' AND m.effective_to IS NULL LIMIT 1""").fetchone()[0]
    agent = con.execute("SELECT internal_code FROM agent WHERE status='active' AND agent_id NOT IN (SELECT agent_id FROM org_manager) LIMIT 1").fetchone()[0]
    return {"main": "admin", "manager": manager, "agent": agent}


@pytest.mark.parametrize("role,sales", [("agent", "/sales/card"), ("manager", "/sales/card"), ("main", "/sales/card-approve")])
def test_tabbar_goes_to_first_built_screen(env, role, sales):
    from app.web import menu
    c = login(env, _role_logins(env)[role])
    html = c.get("/").text
    tabbar = re.search(r'<nav class="tabbar"[^>]*>(.*?)</nav>', html, re.S).group(1)
    hrefs = re.findall(r'href="([^"]+)"', tabbar)
    assert sales in hrefs, (role, hrefs)
    assert "/sales/leads" not in hrefs and "/sales/assign" not in hrefs, (role, hrefs)
    for h in hrefs:                                                            # 아래 탭은 모두 만들어진 화면이고 그 사람에게 열린다
        it = menu.find(h)
        assert it and it["status"] == "built", (role, h)
        assert c.get(h).status_code == 200, (role, h)
    top = re.findall(r'href="([^"]+)"', re.search(r'<nav class="nav-groups"[^>]*>(.*?)</nav>', html, re.S).group(1))
    assert sales in top, (role, top)


def test_group_entry_falls_back_to_first_item_when_nothing_built():
    from app.web import menu
    g = {"items": [{"path": "/a", "status": "planned"}, {"path": "/b", "status": "legacy"}]}
    assert menu.entry(g) == "/a"
    g["items"].append({"path": "/c", "status": "built"})
    assert menu.entry(g) == "/c"


# ---------------------------------------------------------------- 실적에는 기준 시각을 붙인다
def test_hall_and_team_members_show_reference_time_in_words(env):
    c = login(env)
    hall = c.get("/perf/hall").text
    line = re.search(r'<p class="cap perf-fresh">(.*?)</p>', hall, re.S)
    assert line, "명예의전당 기준 시각 줄"
    assert re.search(r"계산한 순위가 기준입니다|순위는 아직 계산하지 않았습니다", line.group(1))
    assert re.search(r"적재 기준입니다|적재한 계약 자료가 없습니다", line.group(1))
    team = c.get("/team/members").text
    assert not re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d(:\d\d)? 적재 기준", team)            # 날짜 꼴 그대로 적지 않는다
    assert re.search(r"\d+월 \d+일 \d+시 \d\d분 적재 기준입니다\.|적재한 계약 자료가 없습니다", team)
