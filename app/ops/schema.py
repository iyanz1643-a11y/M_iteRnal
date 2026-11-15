"""schema.sql 의 묶음을 오래된 DB 에 다시 돌리는 도우미와 스키마 버전.

새 DB 는 db.init_db 가 schema.sql 전체를 돌리므로 이 파일이 필요 없다. 이미 쓰던 DB 를 열 때만
[platform:begin] 묶음을 돌린다. 이미 있는 칸과 표는 건너뛴다. 묶음 안에는 ; 가 든 글자를 두지 않는다.

스키마 버전은 schema_migration 표의 가장 큰 version 이다. 코드가 기대하는 값은 SCHEMA_VERSION 이다.
PostgreSQL 로 옮기면 schema.sql 이 첫 마이그레이션 1 이고, 그 뒤 마이그레이션마다 한 줄씩 늘린다.
"""
from __future__ import annotations

import sqlite3

from app import db

SCHEMA_VERSION = 1
BEGIN, END = "-- [platform:begin]", "-- [platform:end]"


def block_statements(begin: str, end: str) -> list:
    """schema.sql 에서 begin 과 end 사이의 문장들. 줄 끝 주석은 뗀다."""
    text = db.SCHEMA.read_text(encoding="utf-8")
    body = text[text.index(begin) + len(begin):text.index(end)]
    body = "\n".join(line.split("--", 1)[0] for line in body.splitlines())
    return [s.strip() for s in body.split(";") if s.strip()]


def columns(con, table: str) -> set:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def run_block(con, begin: str = BEGIN, end: str = END) -> int:
    """묶음을 돌린다. ALTER TABLE ... ADD COLUMN 은 칸이 이미 있으면 건너뛴다. 돌린 문장 수를 돌려준다."""
    n = 0
    for stmt in block_statements(begin, end):
        words = stmt.split()
        if words[:2] == ["ALTER", "TABLE"] and words[3:5] == ["ADD", "COLUMN"] and words[5] in columns(con, words[2]):
            continue
        con.execute(stmt)
        n += 1
    con.commit()
    return n


def ensure(con) -> None:
    """운영 기반 묶음이 없는 DB 에서만 돌린다. 세션 버전 칸과 작업 표와 스키마 버전 표가 모두 있으면 아무것도 하지 않는다."""
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('job','schema_migration')")}
        if len(have) == 2 and "session_version" in columns(con, "user_account"):
            return
    except sqlite3.DatabaseError:
        return
    run_block(con)


def schema_version(con) -> int | None:
    """DB 에 적힌 스키마 버전. 표가 없으면 None."""
    try:
        r = con.execute("SELECT MAX(version) FROM schema_migration").fetchone()
    except sqlite3.DatabaseError:
        return None
    return r[0] if r and r[0] is not None else None
