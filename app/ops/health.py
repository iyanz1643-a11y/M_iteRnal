"""상태 점검 /healthz. DB 에 닿는지와 스키마 버전만 본다. 자료는 내지 않는다.

돌려주는 칸은 status, db, schema_version, schema_expected 넷뿐이다. 건수, 경로, DB 주소, 사람, 금액은 넣지 않는다.
DB 파일이 없으면 만들지 않고 missing 으로 답한다. 읽기 전용으로 연다.
guard 를 거치지 않는 주소라 tests/test_web_access.py 의 OPEN_ROUTES 와 scripts/check_context.py 의 NON_MENU_ROUTES 에 함께 적었다.
"""
from __future__ import annotations

import pathlib
import sqlite3

from app.ops import schema

OK, DOWN, MISSING = "ok", "down", "missing"


def check(path) -> dict:
    p = pathlib.Path(path)
    out = {"status": DOWN, "db": DOWN, "schema_version": None, "schema_expected": schema.SCHEMA_VERSION}
    if not p.exists():
        out["db"] = MISSING
        return out
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
        try:
            con.execute("SELECT 1").fetchone()
            out["db"] = OK
            out["schema_version"] = schema.schema_version(con)
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return out
    out["status"] = OK if out["schema_version"] == schema.SCHEMA_VERSION else "degraded"
    return out
