"""초대 링크. 메인 관리자가 발급한 계정에 한 번 쓰는 링크를 만들고, 그 링크에서 첫 비밀번호를 정한다. docs/10 6.1.

- 토큰은 secrets.token_urlsafe(32) 로 만들고 SHA-256 해시만 저장한다. 원문은 만든 화면에 한 번만 보이고 어디에도 남지 않는다.
  256비트 무작위 값이라 느린 해시가 필요 없다. 비밀번호와 달리 사람이 고른 값이 아니기 때문이다.
- 유효기간은 TTL_DAYS 일이다. 쓰면 곧 무효가 된다. 같은 계정에 새 링크를 만들면 앞의 링크는 거둔다.
- 초기 비밀번호를 쓰고 있는 계정에만 만든다. 메인 관리자 계정에는 만들지 않는다. 쓰는 순간에도 다시 본다.
  초대를 기다리는 사이에 초기 비밀번호로 들어와 비밀번호를 바꿨다면 링크는 쓸 수 없다.
- 맞지 않는 토큰, 지난 토큰, 쓴 토큰, 거둔 토큰은 모두 같은 말로 거절한다. 어느 쪽인지 알려 주지 않는다.
- 표는 schema/schema.sql 의 [invite:begin] 묶음이다. 오래된 DB 에서는 ensure 가 그 묶음을 돌린다.

비밀번호 규칙은 password_error 한 곳에 둔다. 내 정보의 비밀번호 바꾸기는 아직 8자 규칙만 본다. 맞추는 일은 열린 일로 남겼다.
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta

from app import db
from app.web import auth

TTL_DAYS = 7
TOKEN_BYTES = 32
BEGIN, END = "-- [invite:begin]", "-- [invite:end]"
ACTION_CREATE, ACTION_USED = "account_invite", "account_invite_used"
PW_MIN, PW_MAX = 8, 64
INVALID = "이 링크는 쓸 수 없습니다. 기간이 지났거나 이미 쓴 링크입니다. 메인 관리자에게 새 링크를 받아 주세요."


class InviteError(ValueError):
    pass


def ensure(con) -> None:
    """초대 표가 없는 오래된 DB 에서 schema.sql 의 초대 묶음을 돌린다. 있으면 아무것도 하지 않는다."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_invite'").fetchone():
        return
    text = db.SCHEMA.read_text(encoding="utf-8")
    con.executescript(text[text.index(BEGIN):text.index(END)])
    con.commit()


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _at(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def create(con, user_id: int, by: str, now: datetime | None = None) -> tuple:
    """(토큰 원문, 만료 시각). 커밋한다. 원문은 돌려주기만 하고 저장하지 않는다."""
    ensure(con)
    u = con.execute("SELECT user_id, login_id, status, is_main_admin, must_change_password FROM user_account WHERE user_id=?",
                    (user_id,)).fetchone()
    if not u:
        raise InviteError("없는 계정입니다.")
    if u["is_main_admin"]:
        raise InviteError("메인 관리자 계정에는 초대 링크를 만들지 않습니다.")
    if u["status"] != "active":
        raise InviteError("중지된 계정입니다. 다시 사용으로 바꾼 뒤 만들어 주세요.")
    if not u["must_change_password"]:
        raise InviteError("이미 비밀번호를 정한 계정입니다. 잊었다면 재설정을 써 주세요.")
    now = now or datetime.now()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires = _at(now + timedelta(days=TTL_DAYS))
    con.execute("UPDATE user_invite SET revoked_at=? WHERE user_id=? AND used_at IS NULL AND revoked_at IS NULL", (_at(now), user_id))
    con.execute("INSERT INTO user_invite(user_id, token_hash, created_by, created_at, expires_at) VALUES(?,?,?,?,?)",
                (user_id, digest(token), by, _at(now), expires))
    con.commit()
    auth.log(con, by, ACTION_CREATE, u["login_id"])
    return token, expires


def lookup(con, token: str, now: datetime | None = None) -> dict | None:
    """쓸 수 있는 초대면 {invite_id, user_id, login_id, expires_at}, 아니면 None."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", token):
        return None
    ensure(con)
    r = con.execute("""SELECT i.invite_id, i.user_id, i.expires_at, u.login_id FROM user_invite i JOIN user_account u ON u.user_id=i.user_id
                       WHERE i.token_hash=? AND i.used_at IS NULL AND i.revoked_at IS NULL AND i.expires_at>?
                         AND u.status='active' AND u.is_main_admin=0 AND u.must_change_password=1""",
                    (digest(token), _at(now or datetime.now()))).fetchone()
    return dict(r) if r else None


def password_error(pw: str, pw2: str, login_id: str = "") -> str | None:
    """비밀번호 규칙. 맞으면 None, 아니면 고칠 말을 돌려준다."""
    if not pw or not pw2:
        return "두 칸을 모두 넣어 주세요."
    if pw != pw2:
        return "두 칸이 서로 다릅니다."
    if len(pw) < PW_MIN or len(pw) > PW_MAX:
        return f"비밀번호는 {PW_MIN}자 이상 {PW_MAX}자 이하로 정해 주세요."
    if pw != pw.strip():
        return "비밀번호의 앞뒤에 빈칸을 넣지 말아 주세요."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"[0-9]", pw):
        return "영문과 숫자를 함께 넣어 주세요."
    if login_id and login_id.lower() in pw.lower():
        return "사번이 들어간 비밀번호는 쓸 수 없습니다."
    if len(set(pw)) < 4:
        return "같은 글자를 되풀이한 비밀번호는 쓸 수 없습니다."
    return None


def redeem(con, token: str, pw: str, pw2: str, now: datetime | None = None) -> str:
    """첫 비밀번호를 정하고 초대를 무효로 한다. 로그인 아이디를 돌려준다. 커밋한다.

    쓸 수 없는 링크면 InviteError(INVALID), 규칙에 맞지 않으면 InviteError(고칠 말). 규칙에 맞지 않을 때는 링크를 그대로 둔다."""
    inv = lookup(con, token, now)
    if not inv:
        raise InviteError(INVALID)
    err = password_error(pw, pw2, inv["login_id"])
    if err:
        raise InviteError(err)
    at = _at(now or datetime.now())
    cur = con.execute("UPDATE user_invite SET used_at=? WHERE invite_id=? AND used_at IS NULL AND revoked_at IS NULL", (at, inv["invite_id"]))
    if cur.rowcount != 1:                               # 같은 링크를 두 번 동시에 보낸 경우
        con.rollback()
        raise InviteError(INVALID)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL WHERE user_id=?",
                (auth.hash_password(pw), inv["user_id"]))
    con.execute("UPDATE user_invite SET revoked_at=? WHERE user_id=? AND used_at IS NULL AND revoked_at IS NULL", (at, inv["user_id"]))
    con.commit()
    auth.log(con, inv["login_id"], ACTION_USED, inv["login_id"])
    return inv["login_id"]


def pending(con, now: datetime | None = None) -> dict:
    """{user_id: 만료 시각}. 계정 화면에 아직 쓰지 않은 링크가 있는지 보이려고 쓴다. 토큰은 없다."""
    ensure(con)
    return {r["user_id"]: r["expires_at"] for r in con.execute(
        "SELECT user_id, MAX(expires_at) expires_at FROM user_invite WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at>? GROUP BY user_id",
        (_at(now or datetime.now()),))}
