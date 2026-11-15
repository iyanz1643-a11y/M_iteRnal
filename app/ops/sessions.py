"""서버 쪽 세션 끊기. 계정마다 세션 버전을 두고 쿠키에 그 값을 함께 적는다.

세션 쿠키는 서명만 되어 있고 서버에 따로 남지 않는다. 그래서 로그아웃해도 미리 복사해 둔 쿠키는 만료 전까지 다시 쓸 수 있었다.
이제 user_account.session_version 을 쿠키의 sv 와 견주어 다르면 로그인 전으로 돌린다.

| 언제 | 버전 | 그 사람의 다른 기기 |
| --- | --- | --- |
| 로그인 | 그대로. 쿠키에 지금 값을 적는다 | 그대로 |
| 로그아웃 | 올린다 | 함께 끊긴다 |
| 본인의 비밀번호 바꾸기 | 올리고 지금 기기의 쿠키만 새 값으로 적는다 | 끊긴다 |
| 메인 관리자의 비밀번호 재설정, 계정 중지 | 올린다 | 끊긴다 |

로그아웃하면 그 사람의 모든 기기에서 나간다. 계정마다 버전 하나라서다. 기기별로 끊는 일은 세션 표가 필요하고 지금은 하지 않는다(가정).
쿠키에 sv 가 없는 예전 세션은 한 번 로그인 화면으로 간다.
"""
from __future__ import annotations

import sqlite3

from app.ops import schema

KEY = "sv"


def _read(con, user_id) -> int | None:
    r = con.execute("SELECT session_version FROM user_account WHERE user_id=?", (user_id,)).fetchone()
    return None if r is None else int(r[0] or 0)


def version(con, user_id) -> int | None:
    """그 계정의 지금 세션 버전. 계정이 없으면 None. 칸이 없는 오래된 DB 면 운영 기반 묶음을 돌린 뒤 다시 읽는다."""
    try:
        return _read(con, user_id)
    except sqlite3.OperationalError:
        con.rollback()
        schema.ensure(con)
        return _read(con, user_id)


def bump(con, user_id) -> int | None:
    """버전을 올린다. 커밋하지 않는다. 올린 뒤의 값을 돌려준다."""
    version(con, user_id)                                   # 칸이 없으면 여기서 만든다
    con.execute("UPDATE user_account SET session_version=session_version+1 WHERE user_id=?", (user_id,))
    return _read(con, user_id)


def stamp(request, con, user_id) -> None:
    """지금 쿠키에 그 계정의 버전을 적는다. 로그인 직후와 본인이 비밀번호를 바꾼 직후에 부른다."""
    request.session[KEY] = version(con, user_id)


def matches(request, con, user_id) -> bool:
    """쿠키의 버전이 DB 의 버전과 같은가. 쿠키에 버전이 없거나 계정이 없으면 거짓이다."""
    sent = request.session.get(KEY)
    if not isinstance(sent, int) or isinstance(sent, bool):
        return False
    return version(con, user_id) == sent
