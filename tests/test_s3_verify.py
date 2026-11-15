"""S3 검증. 만든 에이전트와 따로, verifier 가 S3 의 새 화면과 운영 기반을 다시 본다.

- 세 역할이 S3 새 화면에 들어가는 범위, 범위 밖 사람 고르기 403, 상태를 바꾸는 요청의 CSRF.
- 숫자 대조: 수수료(메인 관리자 전체 보기의 사람별 값과 본인 화면), 대사 차이 건수(화면과 엔진),
  조직 유지율(웹 화면, 기존 도구의 계산, 인증 판정에 저장된 값).
- 개인정보: 정산 화면의 계약자 이름, /healthz, 오류 기록, 백업 암호화와 바뀐 파일 거절, 초대 토큰의 저장 꼴.
- 운영: 접근 기록에 초대 토큰 원문이 남는지. 지금 남는다. strict xfail 로 두었다.

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 와 data/ 아래 폴더는 건드리지 않는다.
실패 메시지에는 사람 이름과 금액을 넣지 않는다. 사람 번호, 화면 주소, 건수만 넣는다.
"""
import hashlib
import logging
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import certification, persist
from app.engine import settle as st
from app.ops import backup
from app.web import auth
from app.web import invite as iv

PW = "verify-s3-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
ROOT = pathlib.Path(db.ROOT)

# S3 에 새로 연 화면. 역할마다 기대하는 응답 코드다. config/menu.yaml 을 읽지 않고 docs/10, docs/11 과 기능기획서의 권한을 시험이 따로 적었다.
S3_SCREENS = {
    "/settle/commission": {"main": 200, "manager": 200, "agent": 200},
    "/settle/clawback": {"main": 200, "manager": 200, "agent": 200},
    "/settle/recon": {"main": 200, "manager": 403, "agent": 403},
    "/settle/adjust": {"main": 200, "manager": 403, "agent": 403},
    "/settle/close": {"main": 200, "manager": 403, "agent": 403},
    "/data/statement": {"main": 200, "manager": 403, "agent": 403},
    "/data/contracts": {"main": 200, "manager": 403, "agent": 403},
    "/data/plan": {"main": 200, "manager": 403, "agent": 403},
    "/data/quality": {"main": 200, "manager": 403, "agent": 403},
    "/data/batches": {"main": 200, "manager": 403, "agent": 403},
    "/data/jobs": {"main": 200, "manager": 403, "agent": 403},
    "/org/masters": {"main": 200, "manager": 403, "agent": 403},
    "/org/settings": {"main": 200, "manager": 200, "agent": 403},
    "/sales/card": {"main": 403, "manager": 200, "agent": 200},
    "/sales/card-approve": {"main": 200, "manager": 403, "agent": 403},     # 관리자는 승인 권한을 받기 전이라 막힌다
}
# 상태를 바꾸는 S3 주소. 토큰 없는 POST 는 모두 403 이어야 한다
S3_POSTS = ["/settle/recon", "/settle/adjust", "/settle/close", "/data/statement", "/data/contracts", "/data/plan", "/data/quality",
            "/data/batches", "/data/jobs", "/org/masters", "/org/settings", "/sales/card-approve", "/org/accounts", "/invite"]
MAIN_ONLY_POSTS = ["/settle/recon", "/settle/adjust", "/settle/close", "/data/statement", "/data/contracts", "/data/plan", "/data/quality",
                   "/data/batches", "/data/jobs", "/org/masters"]


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리킨다. 폼에는 세션의 CSRF 토큰을 붙인다. raw=True 면 붙이지 않는다."""

    def __init__(self, app, db_path, **kw):
        super().__init__(app, follow_redirects=False, **kw)
        self.db_path = str(db_path)

    def request(self, *a, **kw):
        os.environ["APP_DB"] = self.db_path
        return super().request(*a, **kw)

    def post(self, url, data=None, raw=False, **kw):
        if data is not None and not raw and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


@pytest.fixture(scope="module")
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="s3verify-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR", "CARD_SITE_WRITE", "BACKUP_KEY", "BACKUP_DIR")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    os.environ.pop("CARD_SITE_WRITE", None)
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    ym = auth.current_ym(con)
    mgr = con.execute("""SELECT a.agent_id, a.internal_code code, m.org_id FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                         JOIN org_unit o ON o.org_id=m.org_id WHERE o.level='branch' AND m.effective_to IS NULL ORDER BY o.org_id LIMIT 1""").fetchone()
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    mctx = auth.context(con, con.execute("SELECT user_id FROM user_account WHERE agent_id=?", (mgr["agent_id"],)).fetchone()[0])
    users = {r[0]: r[1] for r in con.execute("""SELECT u.agent_id, u.login_id FROM user_account u JOIN agent a ON a.agent_id=u.agent_id
                                                 WHERE u.status='active' AND a.status='active'""")}
    members = [a for a in sorted(mctx["agent_ids"] - {mgr["agent_id"]}) if a not in heads and a in users]
    outside = [a for a in sorted(users) if a not in mctx["agent_ids"] and a not in heads]
    assert members and outside, "관리자 범위 안과 밖에 설계사가 있어야 한다"
    from app.web.main import app
    yield {"tmp": tmp, "db": path, "con": con, "app": app, "ym": ym, "mgr": dict(mgr), "mctx": mctx, "members": members,
           "outside": outside, "users": users, "agent_login": users[members[0]]}
    con.close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


def login(env, login_id, db_path=None, **kw):
    c = Client(env["app"], db_path or env["db"], **kw)
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, f"{login_id} 로그인"
    return c


def roles(env):
    return {"main": login(env, "admin"), "manager": login(env, env["mgr"]["code"]), "agent": login(env, env["agent_login"])}


# ---------------------------------------------------------------- 접근
def test_s3_screens_three_roles(env):
    clients = roles(env)
    wrong = []
    for path, want in S3_SCREENS.items():
        for role, code in want.items():
            got = clients[role].get(path).status_code
            if got != code:
                wrong.append(f"{role} {path}: {got} (기대 {code})")
    assert not wrong, wrong


def test_s3_main_only_posts_refuse_manager_and_agent_with_token(env):
    """토큰이 맞아도 메인 관리자 전용 쓰기는 관리자와 설계사에게 403 이다. 화면이 아니라 guard 가 막는다."""
    clients = roles(env)
    wrong = []
    for role in ("manager", "agent"):
        for path in MAIN_ONLY_POSTS:
            got = clients[role].post(path, data={"action": "close", "ym": env["ym"]}).status_code
            if got != 403:
                wrong.append(f"{role} {path}: {got}")
    assert not wrong, wrong


def test_s3_pick_other_person_is_403_outside_main(env):
    """수수료와 환수에서 다른 사람을 고르는 agent 값. 관리자는 팀원도 볼 수 없다(D-17). 설계사는 남을 볼 수 없다. 메인 관리자는 본다."""
    clients = roles(env)
    member, other = env["members"][0], env["outside"][0]
    before = env["con"].execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied'").fetchone()[0]
    wrong = []
    for path in ("/settle/commission", "/settle/clawback"):
        for role, agent_id, code in (("manager", member, 403), ("manager", other, 403), ("agent", other, 403), ("main", member, 200)):
            got = clients[role].get(f"{path}?ym={env['ym']}&agent={agent_id}").status_code
            if got != code:
                wrong.append(f"{role} {path} agent={agent_id}: {got} (기대 {code})")
    assert not wrong, wrong
    after = env["con"].execute("SELECT COUNT(*) FROM audit_log WHERE action='access_denied'").fetchone()[0]
    assert after - before >= 6, "범위 밖 고르기가 변경 기록에 남아야 한다"


def _write_counts(con) -> tuple:
    tables = ["close_period", "recon_diff_action", "job", "import_batch", "quality_issue", "user_invite", "audit_log"]
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = []
    for t in tables:
        out.append(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in have else None)
    out.append(con.execute("SELECT COALESCE(SUM(resolved),0) FROM quality_issue").fetchone()[0])
    return tuple(out)


def test_s3_posts_without_csrf_are_403_and_write_nothing(env):
    main = login(env, "admin")
    before = _write_counts(env["con"])
    wrong = []
    for path in S3_POSTS:
        r = main.post(path, data={"action": "close", "ym": env["ym"], "agree": "yes", "user_id": "1", "ids": "1"}, raw=True)
        if r.status_code != 403:
            wrong.append(f"{path}: {r.status_code}")
    assert not wrong, wrong
    assert _write_counts(env["con"]) == before, "토큰 없는 요청이 DB 에 무언가 썼다"


# ---------------------------------------------------------------- 개인정보
def _fill_holder_names(path) -> list:
    """사본의 계약자와 피보험자 칸에 화면 문구와 겹치지 않는 가짜 이름을 넣는다."""
    con = db.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(contract)")}
    names = []
    for i, (cid,) in enumerate(con.execute("SELECT contract_id FROM contract ORDER BY contract_id").fetchall()):
        n = f"검증계약자{i:05d}"
        names.append(n)
        con.execute("UPDATE contract SET policyholder_name=? WHERE contract_id=?", (n, cid))
        if "insured_name" in cols:
            con.execute("UPDATE contract SET insured_name=? WHERE contract_id=?", (f"검증피보험{i:05d}", cid))
    con.commit()
    con.close()
    return names


def test_s3_settle_and_data_screens_show_no_holder_names(env):
    """D-05, D-17. 정산 화면과 데이터 화면은 계약자 이름을 읽지 않는다. 세 역할 모두 본다. 계약은 증권번호 끝 네 자리로만 가리킨다."""
    copy = env["tmp"] / "holder.db"
    shutil.copy(env["db"], copy)
    _fill_holder_names(copy)
    ym = env["ym"]
    member = env["members"][0]
    screens = {
        "main": ["/settle/commission", f"/settle/commission?ym={ym}&agent={member}", "/settle/clawback", "/settle/recon", "/settle/adjust",
                 "/settle/close", "/data/statement", "/data/contracts", "/data/plan", "/data/quality", "/data/batches", "/data/jobs", "/org/masters"],
        "manager": ["/settle/commission", "/settle/clawback"],
        "agent": ["/settle/commission", "/settle/clawback"],
    }
    who = {"main": "admin", "manager": env["mgr"]["code"], "agent": env["agent_login"]}
    leaks = []
    for role, paths in screens.items():
        c = login(env, who[role], copy)
        for p in paths:
            r = c.get(p)
            assert r.status_code == 200, f"{role} {p}: {r.status_code}"
            if "검증계약자" in r.text or "검증피보험" in r.text:
                leaks.append(f"{role} {p}")
    # 조정 화면에서 차이 하나를 고른 상세
    con = db.connect(copy)
    d = st.diff_list(con, [r[0] for r in con.execute("SELECT agent_id FROM agent")], ym, unmapped=True)
    con.close()
    if d["rows"]:
        r = login(env, "admin", copy).get(f"/settle/adjust?ym={ym}&diff={d['rows'][0]['diff_id']}")
        if "검증계약자" in r.text or "검증피보험" in r.text:
            leaks.append("main /settle/adjust?diff")
    assert not leaks, leaks


def test_s3_healthz_gives_only_status_fields(env):
    c = Client(env["app"], env["db"])
    r = c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "db", "schema_version", "schema_expected"}
    assert body["status"] == "ok" and body["db"] == "ok"
    assert str(env["tmp"]) not in r.text and "sample.db" not in r.text
    assert r.headers.get("cache-control") == "no-store"


class _Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_s3_error_log_keeps_no_personal_values(env, monkeypatch):
    """잡지 못한 오류. 기록에는 예외 메시지, 주소 뒤 꼬리, 폼 값이 남지 않고 화면에는 요청 번호만 보인다."""
    secret_name, secret_amount = "검증오류이름", "9,876,543"

    def boom(*a, **k):
        raise ValueError(f"{secret_name} {secret_amount}원")
    monkeypatch.setattr(st, "agent_month", boom)
    grab = _Grab()
    lg = logging.getLogger("app.ops")
    lg.addHandler(grab)
    try:
        c = login(env, "admin", raise_server_exceptions=False)
        r = c.get(f"/settle/commission?ym={env['ym']}&q={secret_name}")
    finally:
        lg.removeHandler(grab)
    assert r.status_code == 500
    assert secret_name not in r.text and secret_amount not in r.text
    assert r.headers.get("x-request-id")
    errors = [x for x in grab.lines if '"event": "error"' in x]
    assert errors, "오류 기록이 남아야 한다"
    for x in grab.lines:
        assert secret_name not in x and secret_amount not in x and "q=" not in x
    assert any("/settle/commission" in x and "ValueError" in x for x in errors)


def test_s3_backup_is_encrypted_and_changed_file_is_refused(env, monkeypatch):
    out = env["tmp"] / "bk"
    monkeypatch.setenv("BACKUP_KEY", "verify-backup-key-1234")
    m = backup.backup(env["db"], out, encrypt=True)
    f = out / m["file"]
    data = f.read_bytes()
    code = env["con"].execute("SELECT internal_code FROM agent ORDER BY agent_id LIMIT 1").fetchone()[0]
    assert m["encrypted"] and f.name.endswith(".enc")
    assert b"SQLite format 3" not in data and code.encode() not in data and "admin".encode() not in data
    manifest = (out / (f.name + ".json")).read_text(encoding="utf-8")
    assert code not in manifest and "verify-backup-key" not in manifest
    assert backup.verify(f)
    bad_dir = env["tmp"] / "bk_bad"
    bad_dir.mkdir()
    bad = bad_dir / f.name
    b = bytearray(data)
    b[len(b) // 2] ^= 1
    bad.write_bytes(bytes(b))
    shutil.copy(out / (f.name + ".json"), bad_dir / (f.name + ".json"))
    with pytest.raises(backup.BackupError):
        backup.verify(bad)
    monkeypatch.setenv("BACKUP_KEY", "another-wrong-key-123")
    with pytest.raises(backup.BackupError):
        backup.verify(f)


def test_s3_invite_token_is_kept_only_as_hash(env):
    con = db.connect(env["db"])
    uid = con.execute("SELECT user_id FROM user_account WHERE agent_id=?", (env["outside"][-1],)).fetchone()[0]
    con.execute("UPDATE user_account SET must_change_password=1 WHERE user_id=?", (uid,))
    con.commit()
    token, _ = iv.create(con, uid, "admin")
    dump = "\n".join(con.iterdump())
    con.close()
    assert token not in dump
    assert hashlib.sha256(token.encode()).hexdigest() in dump
    # 링크로 열어도 화면에 토큰이 다시 나오지 않는다
    c = Client(env["app"], env["db"])
    r = c.get(f"/invite?t={token}")
    assert r.status_code == 303 and r.headers["location"] == "/invite"
    assert token not in c.get("/invite").text


def test_s3_card_remove_is_main_only_and_write_off_sends_nothing(env, monkeypatch):
    """명함 내리기. 승인 권한이 있는 관리자도 내리기는 403 이다. 메인 관리자라도 쓰기 꺼짐이면 사이트로 보내지 않는다."""
    from app.connect import card_site
    from app.org import card_grant
    con = db.connect(env["db"])
    card_grant.grant(con, [env["mgr"]["code"]], by="admin")
    con.commit()
    con.close()
    sent = []
    monkeypatch.setattr(card_site.CardSiteClient, "_post_once", lambda self, *a, **k: sent.append(a) or None, raising=False)
    monkeypatch.delenv("CARD_SITE_WRITE", raising=False)
    mgr = login(env, env["mgr"]["code"])
    assert mgr.get("/sales/card-approve").status_code == 200, "승인 권한을 켠 관리자는 승인 화면을 연다"
    r = mgr.post("/sales/card-approve", data={"action": "remove", "slug": "verify-slug", "reason": "해촉", "confirm": "1"})
    assert r.status_code == 403
    assert sent == []


# ---------------------------------------------------------------- 숫자 대조
HERO_NET = re.compile(r'data-hero="net">.*?<div class="big">(-?[0-9,]+)<', re.S)
ROW = re.compile(r'<tr data-agent="(\d+)">(.*?)</tr>', re.S)
NUM_TD = re.compile(r'<td class="num[^"]*">(-?[0-9,]+)</td>')


def _int(s):
    return int(s.replace(",", ""))


def test_s3_commission_main_rows_equal_each_persons_own_screen(env):
    """메인 관리자 전체 보기의 사람별 순액은 그 사람이 본인 화면에서 보는 순액과 같다. 전체 순액은 사람별 합과 같다."""
    ym = env["ym"]
    main = login(env, "admin")
    html = main.get(f"/settle/commission?ym={ym}").text
    rows = {int(a): [_int(x) for x in NUM_TD.findall(body)] for a, body in ROW.findall(html)}
    assert len(rows) >= 5, "수수료 줄이 있는 설계사가 여럿이어야 시험이 뜻이 있다"
    hero = _int(HERO_NET.search(html).group(1))
    assert hero == sum(v[2] for v in rows.values()), "전체 순액과 사람별 순액의 합이 다르다"
    checked, wrong = 0, []
    for agent_id, (_paid, _claw, net) in sorted(rows.items()):
        login_id = env["users"].get(agent_id)
        if not login_id:
            continue
        mine = login(env, login_id).get(f"/settle/commission?ym={ym}").text
        m = HERO_NET.search(mine)
        if not m or _int(m.group(1)) != net:
            wrong.append(agent_id)
        own = main.get(f"/settle/commission?ym={ym}&agent={agent_id}").text
        m2 = HERO_NET.search(own)
        if not m2 or _int(m2.group(1)) != net:
            wrong.append(f"main-pick {agent_id}")
        checked += 1
        if checked >= 12:
            break
    assert checked >= 5
    assert not wrong, f"순액이 다른 사람 번호: {wrong}"


COUNT = re.compile(r'data-count="(open|checked|asked|resolved)">.*?<div class="mid">(\d+)<', re.S)


def test_s3_recon_counts_on_screen_equal_engine_and_close_check(env):
    ym = env["ym"]
    con = db.connect(env["db"])
    everyone = [r[0] for r in con.execute("SELECT agent_id FROM agent")]
    eng = st.diff_list(con, everyone, ym, unmapped=True)["counts"]
    chk = st.close_check(con, ym)["counts"]
    con.close()
    assert sum(eng.values()) > 0, "샘플에 대사 차이가 있어야 한다"
    html = login(env, "admin").get(f"/settle/recon?ym={ym}").text
    shown = {k: int(v) for k, v in COUNT.findall(html)}
    assert shown == eng
    assert chk == eng


HERO_P13 = re.compile(r'<div class="card" data-kind="p13">.*?유지 (\d+) / 대상 (\d+)건', re.S)


def test_s3_org_persistency_web_legacy_and_certification_agree(env):
    """조직 13회차 유지율. 웹 유지율 화면, 기존 도구의 조직 화면이 부르는 계산, 인증 판정에 저장된 실적이 같다.
    기존 도구는 certification.org_members 와 persist.rate 로 센다(app/ui.py 의 조직 화면). 인증은 판정 때의 값을 metric_json 에 남긴다."""
    import json
    con = db.connect(env["db"])
    rows = con.execute("""SELECT s.org_id, s.as_of_ym, s.metric_json FROM agent_certification_status s
                          WHERE s.org_id IS NOT NULL AND s.metric_json LIKE '%org_persistency_13%'""").fetchall()
    assert rows, "샘플에 조직 유지율 인증 판정이 있어야 한다"
    main = login(env, "admin")
    wrong, seen = [], set()
    for org_id, ym, mj in rows:
        if (org_id, ym) in seen:
            continue
        seen.add((org_id, ym))
        rate, kept, total = persist.rate(con, certification.org_members(con, org_id, ym), "p13", ym)
        stored = next(v["실적"] for k, v in json.loads(mj).items() if k.endswith("org_persistency_13"))
        if abs(stored - round(rate, 3)) > 1e-9:
            wrong.append(f"인증 {org_id}")
        m = HERO_P13.search(main.get(f"/perf/persistency?ym={ym}&org={org_id}").text)
        web = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        if web != (kept, total):
            wrong.append(f"웹 {org_id}")
    # 관리자가 자기 조직을 볼 때도 메인 관리자가 그 조직을 볼 때와 같다
    org = env["mgr"]["org_id"]
    ym = env["ym"]
    a = HERO_P13.search(main.get(f"/perf/persistency?ym={ym}&org={org}").text)
    b = HERO_P13.search(login(env, env["mgr"]["code"]).get(f"/perf/persistency?ym={ym}&org={org}").text)
    con.close()
    assert (a and a.groups()) == (b and b.groups()), "관리자와 메인 관리자의 같은 조직 유지율이 다르다"
    assert not wrong, wrong


# ---------------------------------------------------------------- 저장소와 운영
def test_s3_repo_has_no_data_files_and_no_env():
    def ls(*args):
        return subprocess.run(["git", "ls-files", *args], cwd=ROOT, capture_output=True, text=True).stdout.split()
    if not (ROOT / ".git").exists():
        pytest.skip("git 저장소가 아니다")
    assert ls("data") == []
    assert [p for p in ls() if pathlib.PurePosixPath(p).name == ".env"] == []
    assert ".env.example" in ls()
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert line.endswith("="), "환경 변수 예시에는 값을 두지 않는다"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.xfail(strict=True, reason="platform: 운영 실행(Dockerfile CMD)의 uvicorn 접근 기록이 /invite?t=<토큰> 을 그대로 남긴다. 쓰지 않은 초대 링크를 기록을 읽는 사람이 쓸 수 있다")
def test_s3_invite_token_not_in_server_access_log(env):
    """Dockerfile 의 실행 줄과 같은 uvicorn 설정으로 서버를 띄우고 초대 링크를 연다. 표준 출력과 오류에 토큰 원문이 없어야 한다.
    고치는 길: 접근 기록을 끄거나(--no-access-log), uvicorn.access 기록에서 주소 뒤 꼬리를 지우는 거르개를 앱이 켤 때 단다."""
    cmd = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd = next(line for line in cmd.splitlines() if line.startswith("CMD"))
    port = _free_port()
    args = [sys.executable, "-m", "uvicorn", "app.web.main:app", "--host", "127.0.0.1", "--port", str(port)]
    for flag in ("--proxy-headers", "--no-access-log"):
        if flag in cmd:
            args.append(flag)
    m = re.search(r"--log-config\s+(\S+)", cmd)
    if m:
        args += ["--log-config", m.group(1).strip("\"'")]
    log = env["tmp"] / "access.log"
    token = "VerifyToken" + "x" * 30
    environ = {**os.environ, "APP_DB": str(env["db"]), "APP_SECRET": "verify"}
    with open(log, "wb") as fh:
        p = subprocess.Popen(args, cwd=ROOT, env=environ, stdout=fh, stderr=subprocess.STDOUT)
        try:
            for _ in range(80):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                    break
                except OSError:
                    time.sleep(0.25)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/invite?t={token}")
            try:
                urllib.request.urlopen(req, timeout=5)
            except OSError:
                pass
            time.sleep(0.5)
        finally:
            p.terminate()
            p.wait(timeout=10)
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "/invite" in text or "Started server" in text, "서버가 뜨지 않았다"
    assert token not in text
