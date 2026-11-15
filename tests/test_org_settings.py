"""조직별 설정 /org/settings 와 app/org/settings.py 의 시험. 파트 H.

보는 것: GA 에서 아래로 상속, 아래 조직의 덮어쓰기, 메인 관리자의 막기, 적용 월, 지우지 않는 이력, 역할별 권한과 범위 밖 기록,
화면의 확인 단계와 CSRF, 알림함의 종류별 끄기.
샘플 자료의 조직: GA 아래 본부 둘, 본부마다 지점 둘, 한 지점 아래 센터와 팀이 있다. 번호는 자료에서 찾는다.
"""
import ast
import functools
import importlib
import os
import pathlib
import re
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db, masters
from app.org import settings as st
from app.periods import ym_add
from app.web import audit_view, auth
from app.web import board as bd
from app.web.routes import notify

PW = "settings-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')


class Client(TestClient):
    """폼을 보낼 때 화면의 숨은 CSRF 토큰을 받아 함께 보낸다."""
    def post(self, url, data=None, **kw):
        if data is not None and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


def _head(con, org_id):
    return con.execute("SELECT a.internal_code FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id WHERE m.org_id=?", (org_id,)).fetchone()[0]


@pytest.fixture(scope="module")
def env():
    path = pathlib.Path(tempfile.mkdtemp()) / "settings.db"
    con = db.init_db(path)
    masters.seed(con)
    auth.seed_accounts(con, PW)
    os.environ["APP_DB"] = str(path)
    from app.web.main import app
    one = lambda sql, p=(): con.execute(sql, p).fetchone()[0]
    ga = st.root_org(con)
    center = one("SELECT org_id FROM org_unit WHERE level='center' LIMIT 1")
    team = one("SELECT org_id FROM org_unit WHERE level='team' AND parent_id=?", (center,))
    branch = one("SELECT parent_id FROM org_unit WHERE org_id=?", (center,))
    hq = one("SELECT parent_id FROM org_unit WHERE org_id=?", (branch,))
    sibling = one("SELECT org_id FROM org_unit WHERE level='branch' AND parent_id=? AND org_id<>?", (hq, branch))
    other_hq = one("SELECT org_id FROM org_unit WHERE level='hq' AND org_id<>?", (hq,))
    other_branch = one("SELECT org_id FROM org_unit WHERE level='branch' AND parent_id=? LIMIT 1", (other_hq,))
    ids = dict(ga=ga, hq=hq, branch=branch, center=center, team=team, sibling=sibling, other_hq=other_hq, other_branch=other_branch)
    yield {"con": con, "app": app, **ids, "branch_head": _head(con, branch), "team_head": _head(con, team), "center_head": _head(con, center),
           "other_head": _head(con, other_branch), "first": st.first_open_ym(con)}
    con.close()


def ctx_of(env, login_id):
    con = env["con"]
    return auth.context(con, con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0])


def login(env, login_id):
    c = Client(env["app"], follow_redirects=False)
    assert c.post("/login", data={"login_id": login_id, "password": PW}).status_code == 303
    return c


def denied(env, login_id):
    return env["con"].execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied' AND actor=? AND target LIKE '/org/settings%'",
                              (login_id,)).fetchone()[0]


def val(env, org, key, ym=None):
    return st.effective(env["con"], env[org], key, ym or env["first"])


# ---------------------------------------------------------------- 상속과 덮어쓰기
def test_default_then_ga_value_flows_down(env):
    con, main = env["con"], ctx_of(env, "admin")
    key = "award.default_top_n"
    e = val(env, "team", key)
    assert e["value"] == 3 and e["source"] is None
    st.set_value(con, env["ga"], key, 5, env["first"], main)
    for org in ("ga", "hq", "branch", "center", "team", "other_branch"):
        e = val(env, org, key)
        assert (e["value"], e["source"], e["from_ym"]) == (5, env["ga"], env["first"]), org


def test_lower_org_overrides_and_sibling_keeps_parent(env):
    con, mgr = env["con"], ctx_of(env, env["branch_head"])
    key = "promo.near_pct"
    st.set_value(con, env["ga"], key, 20, env["first"], ctx_of(env, "admin"))
    st.set_value(con, env["branch"], key, 40, env["first"], mgr)
    assert val(env, "branch", key)["value"] == 40 and val(env, "team", key)["source"] == env["branch"]
    assert val(env, "team", key)["value"] == 40
    assert val(env, "sibling", key)["value"] == 20 and val(env, "hq", key)["value"] == 20
    st.set_value(con, env["branch"], key, None, env["first"], mgr)        # 위 조직 값을 따르게 돌린다
    assert val(env, "team", key)["value"] == 20 and val(env, "team", key)["source"] == env["ga"]


def test_union_item_adds_and_cannot_remove_parent(env):
    con = env["con"]
    cid = con.execute("SELECT course_id FROM edu_course WHERE status='active' LIMIT 1").fetchone()
    if cid is None:
        con.execute("INSERT INTO edu_course(code, name, kind, created_by, created_at) VALUES('T-ST1','설정 시험 과정','product','t',?)", (db.now(),))
        con.execute("INSERT INTO edu_course(code, name, kind, created_by, created_at) VALUES('T-ST2','설정 시험 과정 둘','product','t',?)", (db.now(),))
        con.commit()
    a, b = [r[0] for r in con.execute("SELECT course_id FROM edu_course WHERE status='active' ORDER BY course_id LIMIT 2")]
    key = "edu.extra_required"
    st.set_value(con, env["hq"], key, [a], env["first"], ctx_of(env, "admin"))
    st.set_value(con, env["branch"], key, [b], env["first"], ctx_of(env, env["branch_head"]))
    assert val(env, "team", key)["value"] == sorted({a, b})
    assert val(env, "sibling", key)["value"] == [a]
    with pytest.raises(st.SettingError):
        st.set_value(con, env["branch"], key, [999999], env["first"], ctx_of(env, env["branch_head"]))


# ---------------------------------------------------------------- 막기
def test_lock_forces_value_down_and_blocks_managers(env):
    con, main, mgr = env["con"], ctx_of(env, "admin"), ctx_of(env, env["branch_head"])
    key = "notify.promo_due_days"
    st.set_value(con, env["branch"], key, 10, env["first"], mgr)
    assert val(env, "team", key)["value"] == 10
    st.set_value(con, env["hq"], key, 2, env["first"], main)
    st.lock(con, env["hq"], key, env["first"], main)
    assert st.locked(con, env["team"], key, env["first"])["org_id"] == env["hq"]
    assert st.locked(con, env["other_branch"], key, env["first"]) is None
    e = val(env, "team", key)
    assert (e["value"], e["source"], e["locked_at"]) == (2, env["hq"], env["hq"])   # 막은 아래의 값은 쓰지 않는다
    before = denied(env, mgr["login_id"])
    with pytest.raises(st.SettingDenied):
        st.set_value(con, env["branch"], key, 7, env["first"], mgr)
    assert denied(env, mgr["login_id"]) == before + 1
    with pytest.raises(st.SettingError):                                   # 메인 관리자도 막은 아래에는 두지 못한다. 먼저 푼다
        st.set_value(con, env["branch"], key, 7, env["first"], main)
    with pytest.raises(st.SettingError):
        st.lock(con, env["hq"], key, env["first"], main)
    with pytest.raises(st.SettingDenied):
        st.lock(con, env["branch"], key, env["first"], mgr)
    st.unlock(con, env["hq"], key, env["first"], main)
    assert val(env, "team", key)["value"] == 10 and st.locked(con, env["team"], key, env["first"]) is None
    st.set_value(con, env["branch"], key, 7, env["first"], mgr)
    assert val(env, "team", key)["value"] == 7


def test_main_only_item(env):
    con = env["con"]
    with pytest.raises(st.SettingDenied):
        st.set_value(con, env["branch"], "board.manager_post", True, env["first"], ctx_of(env, env["branch_head"]))
    st.set_value(con, env["branch"], "board.manager_post", True, env["first"], ctx_of(env, "admin"))
    assert val(env, "team", "board.manager_post")["value"] is True and val(env, "sibling", "board.manager_post")["value"] is False


# ---------------------------------------------------------------- 적용 월과 이력
def test_from_month_keeps_past_values(env):
    con, main = env["con"], ctx_of(env, "admin")
    key, nxt, prev = "notify.edu_due_days", ym_add(env["first"], 1), ym_add(env["first"], -1)
    st.set_value(con, env["other_hq"], key, 10, env["first"], main)
    st.set_value(con, env["other_hq"], key, 14, nxt, main)
    assert val(env, "other_branch", key, prev)["value"] == 7            # 시작 전 달은 기본값 그대로
    assert val(env, "other_branch", key)["value"] == 10
    assert val(env, "other_branch", key, nxt)["value"] == 14
    assert val(env, "other_branch", key, ym_add(nxt, 5))["value"] == 14
    with pytest.raises(st.SettingError):                                 # 지난 달은 고치지 않는다
        st.set_value(con, env["other_hq"], key, 1, prev, main)
    with pytest.raises(st.SettingError):
        st.set_value(con, env["other_hq"], key, 99, env["first"], main)  # 범위 밖 값
    assert val(env, "other_branch", key, prev)["value"] == 7


def test_history_never_deletes(env):
    con, main = env["con"], ctx_of(env, "admin")
    key = "notify.promo_new"
    org = env["sibling"]
    n0 = con.execute("SELECT COUNT(*) FROM org_setting").fetchone()[0]
    st.set_value(con, org, key, False, env["first"], main, note="시험")
    st.set_value(con, org, key, True, env["first"], main)
    st.set_value(con, org, key, None, env["first"], main)
    st.lock(con, org, key, env["first"], main)
    st.unlock(con, org, key, ym_add(env["first"], 1), main)
    assert con.execute("SELECT COUNT(*) FROM org_setting").fetchone()[0] == n0 + 3
    h = [x for x in st.history(con, org) if x["key"] == key]
    assert [x["what"] for x in h] == ["unlock", "lock", "inherit", "set", "set"]
    assert h[-1]["value"] is False and h[-1]["note"] == "시험" and h[-1]["by"] == "admin"
    lk = con.execute("SELECT from_ym, to_ym FROM org_setting_lock WHERE org_id=? AND key=?", (org, key)).fetchone()
    assert tuple(lk) == (env["first"], env["first"])                    # 풀어도 줄은 남고 끝 달만 찬다
    assert st.locked(con, org, key, env["first"]) and not st.locked(con, org, key, ym_add(env["first"], 1))
    acts = [r[0] for r in con.execute("SELECT action FROM audit_log WHERE action LIKE 'org_setting_%' AND target LIKE ?", (f"org={org} key={key}%",))]
    assert acts == ["org_setting_set", "org_setting_set", "org_setting_inherit", "org_setting_lock", "org_setting_unlock"]
    rows = audit_view.rows_ko(con, [{"at": "", "actor": "admin", "action": "org_setting_lock", "target": f"org={org} key={key} {env['first']}~"}])
    assert rows[0]["action_ko"] == "조직별 설정 막음" and "새 프로모션 알림" in rows[0]["target_ko"]


# ---------------------------------------------------------------- 권한
def test_who_can_write(env):
    con = env["con"]
    assert st.write_orgs(con, ctx_of(env, env["branch_head"])) == {env["branch"], env["center"], env["team"]}
    assert st.write_orgs(con, ctx_of(env, env["team_head"])) == set()      # docs/09 2.4: 팀장은 없음
    assert st.write_orgs(con, ctx_of(env, env["center_head"])) == set()    # 표에 칸이 없어 막아 둔 가정
    agent = con.execute("SELECT internal_code FROM agent WHERE agent_id NOT IN (SELECT agent_id FROM org_manager) AND status='active' LIMIT 1").fetchone()[0]
    assert st.write_orgs(con, ctx_of(env, agent)) == set()
    with pytest.raises(st.SettingDenied):
        st.set_value(con, env["sibling"], "promo.near_pct", 10, env["first"], ctx_of(env, env["branch_head"]))
    with pytest.raises(st.SettingDenied):
        st.set_value(con, env["team"], "promo.near_pct", 10, env["first"], ctx_of(env, env["team_head"]))


def test_screen_roles_scope_and_denials(env):
    con = env["con"]
    agent = con.execute("SELECT internal_code FROM agent WHERE agent_id NOT IN (SELECT agent_id FROM org_manager) AND status='active' LIMIT 1").fetchone()[0]
    assert login(env, agent).get("/org/settings").status_code == 403
    admin = login(env, "admin")
    r = admin.get(f"/org/settings?org={env['team']}")
    assert r.status_code == 200 and "조직별 설정" in r.text and "새 프로모션 알림" in r.text and r.headers["cache-control"] == "no-store"
    m = login(env, env["branch_head"])
    html = m.get("/org/settings").text
    assert f'value="{env["branch"]}" selected' in html
    assert f'value="{env["sibling"]}"' not in html and f'value="{env["other_branch"]}"' not in html   # 범위 밖 조직은 고를 거리에도 없다
    n = denied(env, env["branch_head"])
    assert m.get(f"/org/settings?org={env['other_branch']}").status_code == 403
    assert m.post("/org/settings", data={"org_id": env["sibling"], "key": "promo.near_pct", "action": "set", "value": "10",
                                         "from_ym": env["first"], "confirm": "1"}).status_code == 403
    assert denied(env, env["branch_head"]) == n + 2
    # 팀장은 자기 팀을 보기만 한다
    t = login(env, env["team_head"])
    page = t.get("/org/settings")
    assert page.status_code == 200 and "볼 수만 있습니다" in page.text
    n = denied(env, env["team_head"])
    assert t.post("/org/settings", data={"org_id": env["team"], "key": "promo.near_pct", "action": "set", "value": "10",
                                         "from_ym": env["first"]}).status_code == 403
    assert denied(env, env["team_head"]) == n + 1
    # 관리자는 막기를 못 한다
    assert m.post("/org/settings", data={"org_id": env["branch"], "key": "promo.near_pct", "action": "lock", "from_ym": env["first"]}).status_code == 403


def test_screen_confirm_step_and_csrf(env):
    con = env["con"]
    m = login(env, env["branch_head"])
    key, org = "award.default_top_n", env["center"]
    before = con.execute("SELECT COUNT(*) FROM org_setting WHERE org_id=? AND key=?", (org, key)).fetchone()[0]
    form = {"org_id": org, "key": key, "action": "set", "value": "8", "from_ym": env["first"]}
    raw = TestClient(env["app"], follow_redirects=False)
    raw.cookies = m.cookies
    assert raw.post("/org/settings", data={**form, "confirm": "1"}).status_code == 403           # CSRF 토큰이 없으면 막는다
    r = m.post("/org/settings", data=form)
    assert r.status_code == 200 and "확인" in r.text and 'name="confirm" value="1"' in r.text and "8명" in r.text
    assert con.execute("SELECT COUNT(*) FROM org_setting WHERE org_id=? AND key=?", (org, key)).fetchone()[0] == before   # 확인 전에는 쓰지 않는다
    r = m.post("/org/settings", data={**form, "confirm": "1"})
    assert r.status_code == 303 and r.headers["location"] == f"/org/settings?org={org}"
    assert val(env, "center", key)["value"] == 8
    page = m.get(f"/org/settings?org={org}").text
    assert "바꿨습니다" in page and "이 조직" in page and "8명" in page
    bad = m.post("/org/settings", data={**form, "value": "0"})
    assert bad.status_code == 400 and "사이로" in bad.text
    past = m.post("/org/settings", data={**form, "from_ym": ym_add(env["first"], -1)})
    assert past.status_code == 400 and "지난 달" in past.text


# ---------------------------------------------------------------- 알림 끄기
def test_notification_kind_off_applies_to_inbox(env):
    con = env["con"]
    pid = bd.create(con, "admin", {"category": "notice", "title": "설정 시험 공지", "body": "모두에게 보이는 시험 공지입니다.",
                                   "target_org_id": None, "target_role": None, "fc_visible": None}, [])
    con.commit()
    notify.clear_cache()
    inside = con.execute("""SELECT a.internal_code FROM agent a JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_to IS NULL
                            WHERE h.org_id=? AND a.agent_id NOT IN (SELECT agent_id FROM org_manager) LIMIT 1""", (env["team"],)).fetchone()
    inside = inside[0] if inside else con.execute(
        """SELECT a.internal_code FROM agent a JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_to IS NULL
           WHERE h.org_id IN (?,?,?) AND a.agent_id NOT IN (SELECT agent_id FROM org_manager) LIMIT 1""", (env["branch"], env["center"], env["team"])).fetchone()[0]
    outside = con.execute("""SELECT a.internal_code FROM agent a JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_to IS NULL
                             WHERE h.org_id=? AND a.agent_id NOT IN (SELECT agent_id FROM org_manager) LIMIT 1""", (env["other_branch"],)).fetchone()[0]
    note = f"board_new:{pid}"
    keys = lambda who: {n["key"] for n in notify.collect(con, ctx_of(env, who))}
    assert note in keys(inside) and note in keys(outside)
    # 지점장이 자기 지점에서 새 공지 알림을 끈다. 화면으로 확인 단계를 거친다
    m = login(env, env["branch_head"])
    form = {"org_id": env["branch"], "key": "notify.board_new", "action": "set", "value": "0", "from_ym": env["first"]}
    assert m.post("/org/settings", data=form).status_code == 200
    assert m.post("/org/settings", data={**form, "confirm": "1"}).status_code == 303
    assert "board_new" in st.notify_off(con, ctx_of(env, inside))
    assert note not in keys(inside) and note in keys(outside)
    assert "설정 시험 공지" not in login(env, inside).get("/notifications").text
    assert "설정 시험 공지" in login(env, outside).get("/notifications").text
    # 다른 종류는 그대로이고, 위 조직 값으로 돌리면 다시 뜬다
    assert st.notify_off(con, ctx_of(env, inside)) == {"board_new"}
    assert m.post("/org/settings", data={**form, "action": "inherit", "confirm": "1"}).status_code == 303
    assert note in keys(inside)
    # 메인 관리자가 GA 에서 끄면 모두에게서 빠진다. 메인 관리자 본인은 GA 값을 따른다
    main = ctx_of(env, "admin")
    st.set_value(con, env["ga"], "notify.board_new", False, env["first"], main)
    notify.clear_cache()
    assert note not in keys(inside) and note not in keys(outside)
    assert "board_new" in st.notify_off(con, main)
    st.set_value(con, env["ga"], "notify.board_new", True, env["first"], main)
    assert note in keys(outside)


def test_old_db_without_tables_uses_defaults(tmp_path):
    con = db.init_db(tmp_path / "old.db")
    masters.seed(con)
    con.execute("DROP TABLE org_setting")
    con.execute("DROP TABLE org_setting_lock")
    con.commit()
    ga = st.root_org(con)
    assert st.effective(con, ga, "notify.tier_near", "202611")["value"] is True
    assert st.history(con, ga) == [] and st.notify_off(con, {"role": "main", "ym": "202611"}) == set()
    st.ensure(con)
    assert con.execute("SELECT COUNT(*) FROM org_setting").fetchone()[0] == 0
    con.close()


# ---------------------------------------------------------------- 쓰이는 곳
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _code_names(path: pathlib.Path) -> set:
    """파일의 코드에 나오는 이름과 점 이름, 글자 상수. 주석과 문서 글은 빼고 본다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Name, ast.Attribute)):
            out.add(ast.unparse(n))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            out.add(repr(n.value))
    return out


def _resolve(file: str, reads: str):
    """reads 를 그 파일의 모듈 안에서 푼 값. 글자 상수면 그 글자다."""
    if reads[:1] in "'\"":
        return ast.literal_eval(reads)
    mod = importlib.import_module(file[:-3].replace("/", "."))
    first, *rest = reads.split(".")
    return functools.reduce(getattr, rest, vars(mod)[first])


def test_every_used_by_reads_its_key_in_code():
    assert st.ITEMS, "설정 항목이 없습니다"
    for key, it in st.ITEMS.items():
        assert isinstance(it["used_by"], list), key
        for u in it["used_by"]:
            assert set(u) == {"where", "file", "reads"} and u["where"].strip(), (key, u)
            path = ROOT / u["file"]
            assert path.is_file(), (key, u["file"])
            names = _code_names(path)
            reads = u["reads"] if u["reads"][:1] not in "'\"" else repr(ast.literal_eval(u["reads"]))
            assert reads in names, f"{key}: {u['file']} 의 코드에 {u['reads']} 가 없습니다"
            got = _resolve(u["file"], u["reads"])
            if got is st.notify_off:           # 알림 켜고 끄기는 notify_off 가 NOTIFY_KINDS 로 읽고, 알림함이 그 종류를 낸다
                kind = key.removeprefix("notify.")
                assert key.startswith("notify.") and kind in st.NOTIFY_KINDS, key
                assert 'f"notify.{k}"' in (ROOT / "app/org/settings.py").read_text(encoding="utf-8")
                assert repr(kind) in names, f"{key}: {u['file']} 가 {kind} 알림을 내지 않습니다"
            else:
                assert got == key, f"{key}: {u['file']} 의 {u['reads']} 는 {got!r} 입니다"


def test_unwired_items_are_not_read_anywhere():
    """used_by 가 빈 항목은 정말 아무 데도 읽지 않는다. 누가 읽기 시작하면 used_by 를 채워야 이 시험이 통과한다."""
    empty = [k for k, it in st.ITEMS.items() if not it["used_by"]]
    assert "board.manager_post" in empty
    for path in (ROOT / "app").rglob("*.py"):
        if path == ROOT / "app/org/settings.py":
            continue
        names = _code_names(path)
        for key in empty:
            assert repr(key) not in names, f"{key} 를 {path.relative_to(ROOT)} 가 읽습니다. used_by 를 채워 주세요"


def test_screen_shows_where_each_value_is_used(env):
    html = login(env, "admin").get(f"/org/settings?org={env['ga']}").text
    assert html.count("이 값이 쓰이는 곳:") >= len(st.ITEMS)
    assert st.NOT_WIRED in html
    for it in st.ITEMS.values():
        for u in it["used_by"]:
            assert u["where"] in html
    edit = login(env, "admin").get(f"/org/settings?org={env['ga']}&edit=board.manager_post").text
    assert edit.count(st.NOT_WIRED) >= 2      # 표와 바꾸기 칸 둘 다
