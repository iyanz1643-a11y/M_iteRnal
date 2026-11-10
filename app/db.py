"""SQLite 연결, 스키마 초기화, 공통 조회 도우미."""
import pathlib
import sqlite3
from datetime import datetime

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "schema.sql"
DEFAULT_DB = ROOT / "data" / "demo.db"


def connect(path=DEFAULT_DB) -> sqlite3.Connection:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(path=DEFAULT_DB) -> sqlite3.Connection:
    path = pathlib.Path(path)
    if path.exists():
        path.unlink()
    con = connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    return con


def df(con, sql, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_run(con, run_type, period_ym, run_by="system", rule_snapshot=None) -> int:
    """같은 유형·기간의 이전 완료 run은 superseded 처리하고 새 run을 만든다."""
    con.execute(
        "UPDATE calc_run SET status='superseded' WHERE run_type=? AND period_ym=? AND status='done'",
        (run_type, period_ym),
    )
    cur = con.execute(
        "INSERT INTO calc_run(run_type, period_ym, started_at, run_by, rule_snapshot, status) VALUES(?,?,?,?,?,'running')",
        (run_type, period_ym, now(), run_by, rule_snapshot),
    )
    return cur.lastrowid


def finish_run(con, run_id, note=None):
    con.execute("UPDATE calc_run SET status='done', finished_at=?, note=? WHERE run_id=?", (now(), note, run_id))
    con.commit()


def latest_run(con, run_type, period_ym):
    row = con.execute(
        "SELECT run_id FROM calc_run WHERE run_type=? AND period_ym=? AND status='done' ORDER BY run_id DESC LIMIT 1",
        (run_type, period_ym),
    ).fetchone()
    return row["run_id"] if row else None
