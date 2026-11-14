"""계정, 비밀번호, 역할과 조직 범위.

역할은 계정에 적어 두지 않는다. 메인 관리자 표시가 있으면 main, 지금 조직장이나 대리로 지정돼 있으면 manager, 아니면 agent 다.
조직 범위는 조직장이나 대리로 지정된 조직과 그 아래 전부다. 계층은 여섯 단계이고 이름은 config/org_levels.yaml 을 따른다.
조직장은 자료 기준 달로, 대리는 오늘 날짜로 따진다.
명함 승인과 반려 권한 card_approver 는 조직장이 되었다고 생기지 않는다. 메인 관리자가 켠다. app/org/card_grant.py 를 본다.
"""
import hashlib
import hmac
import os
import secrets
from datetime import date, datetime, timedelta

from app import db
from app.org import card_grant, levels

ITER = 200_000
MAX_FAIL, LOCK_MINUTES = 5, 10
ROLE_KO = {"main": "메인 관리자", "manager": "관리자", "agent": "설계사"}
LEVEL_KO = levels.level_ko()          # {ga: GA, hq: 지사·본부, branch: 지점, center: 센터, team: 팀}
HEAD_KO = levels.head_ko()            # {hq_head: 지사장·본부장, branch_head: 지점장, center_head: 센터장, team_head: 팀장, deputy: 대리}
TEMP_PW_CHARS = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 헷갈리는 글자(0, O, 1, l, I)는 뺀다
TEMP_PW_LEN = 12


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), ITER).hex()
    return f"pbkdf2${ITER}${salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
    return hmac.compare_digest(calc, h)


def log(con, actor, action, target=None):
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), actor, action, target))
    con.commit()


def login(con, login_id: str, pw: str):
    """(계정 행 또는 None, 실패 사유)"""
    u = con.execute("SELECT * FROM user_account WHERE login_id=?", (login_id.strip(),)).fetchone()
    if not u:
        log(con, login_id, "login_fail", "없는 계정")
        return None, "아이디 또는 비밀번호가 맞지 않습니다."
    if u["status"] != "active":
        log(con, login_id, "login_fail", "중지된 계정")
        return None, "중지된 계정입니다. 메인 관리자에게 문의하세요."
    if u["locked_until"] and u["locked_until"] > db.now():
        return None, f"여러 번 틀려 잠겼습니다. {u['locked_until'][11:16]} 이후에 다시 시도하세요."
    if not verify_password(pw, u["password_hash"]):
        fails = u["failed_count"] + 1
        lock = (datetime.now() + timedelta(minutes=LOCK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S") if fails >= MAX_FAIL else None
        con.execute("UPDATE user_account SET failed_count=?, locked_until=? WHERE user_id=?", (0 if lock else fails, lock, u["user_id"]))
        log(con, login_id, "login_fail", "잠금" if lock else f"비밀번호 {fails}회")
        return None, "아이디 또는 비밀번호가 맞지 않습니다."
    con.execute("UPDATE user_account SET failed_count=0, locked_until=NULL, last_login_at=? WHERE user_id=?", (db.now(), u["user_id"]))
    log(con, login_id, "login")
    return u, None


def current_ym(con) -> str:
    r = con.execute("SELECT MAX(period_ym) ym FROM calc_run WHERE status='done'").fetchone()
    return r["ym"] or datetime.now().strftime("%Y%m")


def _descendants(con, org_ids) -> set:
    out, frontier = set(org_ids), set(org_ids)
    while frontier:
        q = ",".join("?" * len(frontier))
        frontier = {r["org_id"] for r in con.execute(f"SELECT org_id FROM org_unit WHERE parent_id IN ({q})", tuple(frontier))} - out
        out |= frontier
    return out


def context(con, user_id: int) -> dict | None:
    """로그인한 사람의 역할, 맡은 조직, 볼 수 있는 조직과 설계사."""
    u = con.execute("SELECT * FROM user_account WHERE user_id=? AND status='active'", (user_id,)).fetchone()
    if not u:
        return None
    ym = current_ym(con)
    heads = con.execute(
        """SELECT m.org_id, m.role, o.name, o.level FROM org_manager m JOIN org_unit o ON o.org_id=m.org_id
           WHERE m.agent_id=? AND m.effective_from<=? AND (m.effective_to IS NULL OR m.effective_to>=?)""",
        (u["agent_id"], ym, ym)).fetchall() if u["agent_id"] else []
    today = date.today().isoformat()
    heads += con.execute(
        """SELECT d.org_id, 'deputy' role, o.name, o.level FROM org_deputy d JOIN org_unit o ON o.org_id=d.org_id
           WHERE d.agent_id=? AND d.ended_at IS NULL AND d.date_from<=? AND d.date_to>=?""",
        (u["agent_id"], today, today)).fetchall() if u["agent_id"] else []
    home = con.execute(
        """SELECT o.org_id, o.name, o.level FROM agent_org_history h JOIN org_unit o ON o.org_id=h.org_id
           WHERE h.agent_id=? AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)""",
        (u["agent_id"], ym, ym)).fetchone() if u["agent_id"] else None
    role = "main" if u["is_main_admin"] else ("manager" if heads else "agent")
    if role == "main":
        org_ids = {r["org_id"] for r in con.execute("SELECT org_id FROM org_unit")}
    elif role == "manager":
        org_ids = _descendants(con, [h["org_id"] for h in heads])
    else:
        org_ids = set()
    if org_ids:
        q = ",".join("?" * len(org_ids))
        agent_ids = {r["agent_id"] for r in con.execute(
            f"""SELECT agent_id FROM agent_org_history WHERE org_id IN ({q}) AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)""",
            (*org_ids, ym, ym))}
    else:
        agent_ids = set()
    if u["agent_id"]:
        agent_ids.add(u["agent_id"])                       # 본인 것은 언제나 본다
    # 명함 승인과 반려 권한. 메인 관리자는 늘, 관리자는 메인 관리자가 켜 주고 지금도 조직장이거나 대리일 때만, 설계사는 없다
    card_approver = role == "main" or (role == "manager" and card_grant.is_granted(con, u["login_id"]))
    return {"user_id": u["user_id"], "login_id": u["login_id"], "name": u["display_name"], "agent_id": u["agent_id"],
            "role": role, "role_ko": ROLE_KO[role], "ym": ym, "must_change_password": bool(u["must_change_password"]),
            "heads": [dict(h) for h in heads], "home_org": dict(home) if home else None,
            "org_ids": org_ids, "agent_ids": agent_ids, "card_approver": card_approver}


def can_see_agent(ctx: dict, agent_id: int) -> bool:
    return agent_id in ctx["agent_ids"]


def temp_password() -> str:
    return "".join(secrets.choice(TEMP_PW_CHARS) for _ in range(TEMP_PW_LEN))


def issue_account(con, agent_id: int) -> tuple:
    """계정이 없는 재직 설계사에게 계정을 만든다. 아이디는 사번, 초기 비밀번호는 무작위이고 첫 접속 때 바꾸게 한다.
    돌려주는 것: (아이디, 초기 비밀번호). 비밀번호는 저장하지 않으므로 화면에서 한 번만 보여 준다. 커밋하지 않는다."""
    a = con.execute("SELECT agent_id, internal_code, name FROM agent WHERE agent_id=? AND status='active'", (agent_id,)).fetchone()
    if not a:
        raise ValueError("재직 중인 설계사가 아닙니다.")
    if con.execute("SELECT 1 FROM user_account WHERE agent_id=? OR login_id=?", (agent_id, a["internal_code"])).fetchone():
        raise ValueError("이미 계정이 있습니다.")
    pw = temp_password()
    con.execute("INSERT INTO user_account(login_id, password_hash, agent_id, display_name, must_change_password, created_at) VALUES(?,?,?,?,1,?)",
                (a["internal_code"], hash_password(pw), agent_id, a["name"], db.now()))
    return a["internal_code"], pw


def reset_password(con, user_id: int) -> str:
    """새 초기 비밀번호를 만들고 잠금을 풀며 첫 접속 때 바꾸게 한다. 커밋하지 않는다."""
    pw = temp_password()
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=1, failed_count=0, locked_until=NULL WHERE user_id=?",
                (hash_password(pw), user_id))
    return pw


def seed_accounts(con, password: str | None = None) -> int:
    """샘플 계정을 만든다: 메인 관리자 admin, 설계사와 관리자는 사번. 운영에서는 쓰지 않는다."""
    password = password or os.environ.get("DEMO_PASSWORD", "demo1234")
    n = 0
    if not con.execute("SELECT 1 FROM user_account WHERE login_id='admin'").fetchone():
        con.execute("INSERT INTO user_account(login_id, password_hash, display_name, is_main_admin, must_change_password, created_at) VALUES('admin',?,?,1,0,?)",
                    (hash_password(password), "메인 관리자", db.now()))
        n += 1
    shared = hash_password(password)                        # 샘플이라 같은 값을 쓴다. 운영 계정은 사람마다 따로 만든다
    for a in con.execute("SELECT agent_id, internal_code, name FROM agent WHERE status='active'").fetchall():
        if con.execute("SELECT 1 FROM user_account WHERE agent_id=?", (a["agent_id"],)).fetchone():
            continue
        con.execute("INSERT INTO user_account(login_id, password_hash, agent_id, display_name, must_change_password, created_at) VALUES(?,?,?,?,0,?)",
                    (a["internal_code"], shared, a["agent_id"], a["name"], db.now()))
        n += 1
    con.commit()
    return n
