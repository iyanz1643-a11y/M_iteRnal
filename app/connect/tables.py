"""연결 표 준비. schema/schema.sql 의 [connect:begin] 에서 [connect:end] 사이 묶음을 그대로 읽어 돌린다.

표의 정의는 schema.sql 한 곳에만 둔다. 이 파일은 그 묶음을 찾아, 표가 없는 오래된 DB 에서도 연결 상태를 쓸 수 있게 한다.
묶음의 문은 모두 IF NOT EXISTS 라서 여러 번 돌려도 된다. executescript 는 열린 거래를 끝내 버리므로 쓰지 않고 한 문씩 돌린다.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager

from app import db

BEGIN, END = "-- [connect:begin]", "-- [connect:end]"
TABLES = ("connection_state", "connection_watch", "card_link")

_LOCK = threading.Lock()
_STATEMENTS: list | None = None


def statements() -> list:
    """schema.sql 의 연결 묶음을 문 하나씩 나눠 돌려준다."""
    global _STATEMENTS
    if _STATEMENTS is None:
        text = db.SCHEMA.read_text(encoding="utf-8")
        i, j = text.find(BEGIN), text.find(END)
        if i < 0 or j < i:
            raise RuntimeError("schema.sql 에 연결 표 묶음이 없습니다")
        body = "\n".join(re.sub(r"--.*$", "", line) for line in text[i + len(BEGIN):j].splitlines())
        out, buf = [], ""
        for part in body.split(";"):
            buf += part + ";"
            if sqlite3.complete_statement(buf):
                if buf.strip(" \n;"):
                    out.append(buf.strip())
                buf = ""
        _STATEMENTS = out
    return list(_STATEMENTS)


def ensure(con) -> None:
    """연결 표가 없으면 만든다. 이미 있으면 아무것도 하지 않는다."""
    with _LOCK:
        have = {r[0] for r in con.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({','.join('?' * len(TABLES))})", TABLES)}
        if have == set(TABLES):
            return
        for sql in statements():
            con.execute(sql)


@contextmanager
def write(con, name: str = "connect_write"):
    """쓰기 한 묶음. 열린 거래가 없으면 끝에 확정하고, 바깥 거래 안이면 저장점으로 감싸 그 거래를 확정하지 않는다."""
    if con.in_transaction:
        con.execute(f"SAVEPOINT {name}")
        try:
            yield con
        except BaseException:
            con.execute(f"ROLLBACK TO {name}")
            con.execute(f"RELEASE {name}")
            raise
        con.execute(f"RELEASE {name}")
    else:
        try:
            yield con
        except BaseException:
            con.rollback()
            raise
        con.commit()
