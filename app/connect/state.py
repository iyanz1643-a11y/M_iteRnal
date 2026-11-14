"""연결 상태 기록. 연결마다 마지막 시도, 마지막 성공 시각, 마지막 실패 사유를 남긴다.

상태는 DB 의 connection_state 표에, 폴더 감시 목록은 connection_watch 표에 둔다. 표의 정의는 schema/schema.sql 이다.
다른 모듈은 이 파일 밖에서 두 표를 쓰지 않는다. 화면은 all() 과 get() 으로 읽는다.

어느 DB 에 쓰는가
- con 을 주면 그 연결에 쓴다. Pipeline 은 자기 DB 연결을 넘긴다.
- db_path 를 주면 그 파일을 그때그때 열어 쓴다.
- 둘 다 없으면 처음 쓸 때 정한다. Pipeline 이 bind() 로 자기 연결을 붙이거나, 아니면 환경 변수 CONNECT_STATE_DB, 그것도 없으면 data/demo.db 다.

예전 상태 파일
- 지난 차례에는 data/state/connections.json 에 두었다. 첫 인자 path 는 그 파일이나 폴더를 가리킨다. 없으면 CONNECT_STATE_DIR 이다.
- 그 파일이 있으면 처음 쓸 때 한 번 읽어 표로 옮긴다. 표에 이미 있는 줄은 표의 것을 둔다. 파일은 지우지도 옮기지도 않는다.

사유에는 사람 이름과 금액을 넣지 않는다.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import threading
from contextlib import contextmanager

from app import db
from app.connect import tables
from app.connect.base import FAILED, NOT_CONFIGURED, OK

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DEFAULT_STATE_DIR = ROOT / "data" / "state"
STATE_FILE = "connections.json"
ENV_STATE_DIR = "CONNECT_STATE_DIR"
ENV_STATE_DB = "CONNECT_STATE_DB"

_LOCK = threading.RLock()
_MIGRATED: set = set()          # 이 프로세스에서 이미 옮긴 (DB, 파일) 짝

_FIELDS = ("name", "last_status", "last_attempt_at", "last_success_at", "last_failure_at", "last_failure_reason",
           "consecutive_failures", "detail_json", "updated_at", "migrated_from")
# 감시 목록의 키와 열
_WATCH = (("size", "file_size"), ("mtime", "file_mtime"), ("hash", "file_hash"), ("status", "status"), ("at", "seen_at"))


def default_state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get(ENV_STATE_DIR) or DEFAULT_STATE_DIR)


def default_state_db() -> pathlib.Path:
    return pathlib.Path(os.environ.get(ENV_STATE_DB) or db.DEFAULT_DB)


def _db_file(con) -> str:
    """연결이 가리키는 파일. 메모리 DB 면 빈 문자열."""
    try:
        row = con.execute("PRAGMA database_list").fetchone()
        return str(row[2] or "")
    except sqlite3.Error:
        return ""


class StateStore:
    def __init__(self, path: str | pathlib.Path | None = None, *, con=None, db_path: str | pathlib.Path | None = None,
                 create: bool = True):
        p = pathlib.Path(path) if path else default_state_dir()
        self.path = p if p.suffix == ".json" else p / STATE_FILE      # 예전 JSON 상태 파일. 옮기기에만 쓴다
        self._con = con
        self._db_path = pathlib.Path(db_path) if db_path else None
        self.create = create                                          # False 면 DB 파일이 없을 때 새로 만들지 않는다
        self._used = False

    def __repr__(self) -> str:
        where = "con" if self._con is not None else (str(self._db_path) if self._db_path else "unbound")
        return f"StateStore(db={where})"

    # ---------------------------------------------------------- 어느 DB 인가
    def bind(self, con) -> StateStore:
        """DB 를 정하지 않은 채 만든 상태 기록을 이 연결에 붙인다. 이미 정했거나 쓰기 시작했으면 그대로 둔다."""
        if con is not None and self._con is None and self._db_path is None and not self._used:
            self._con = con
        return self

    @property
    def db_path(self) -> pathlib.Path | None:
        if self._con is not None:
            f = _db_file(self._con)
            return pathlib.Path(f) if f else None
        return self._db_path or default_state_db()

    @contextmanager
    def _db(self, write: bool = False):
        """표를 준비한 연결을 내준다. 쓰기는 끝에 확정하고, 바깥 거래 안이면 저장점으로 감싸 그 거래를 건드리지 않는다."""
        with _LOCK:
            self._used = True
            own = self._con is None
            if own:
                path = self._db_path or default_state_db()
                if not self.create and not path.exists():
                    raise FileNotFoundError(f"DB 가 없습니다: {path.name}")
                con = db.connect(path)
            else:
                con = self._con
            try:
                tables.ensure(con)
                self._migrate_once(con)
                if not write:
                    yield con
                else:
                    with tables.write(con, "connect_state"):
                        yield con
            finally:
                if own:
                    con.close()

    # ---------------------------------------------------------- 예전 JSON 옮기기
    def _migrate_once(self, con) -> None:
        key = (_db_file(con) or f"mem:{id(con)}", str(self.path.resolve()))
        if key in _MIGRATED:
            return
        _MIGRATED.add(key)
        if self.path.is_file():
            try:
                self._import_json(con)
            except Exception:  # 옮기지 못해도 연결은 멈추지 않는다. 파일은 그대로 있으니 migrate_json() 으로 다시 한다
                _MIGRATED.discard(key)

    def migrate_json(self, path: str | pathlib.Path | None = None) -> dict:
        """예전 상태 파일을 표로 옮긴다. 몇 번 불러도 같다. 돌려주는 값은 옮긴 건수."""
        if path:
            p = pathlib.Path(path)
            self.path = p if p.suffix == ".json" else p / STATE_FILE
        with self._db() as con:
            return self._import_json(con)

    def _import_json(self, con) -> dict:
        out = {"connections": 0, "watch": 0, "file": self.path.name}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return out
        except (OSError, ValueError):
            out["error"] = "상태 파일을 읽지 못했습니다. 파일은 그대로 둡니다"
            return out
        if not isinstance(doc, dict):
            return out
        began = not con.in_transaction
        try:
            for name, cur in (doc.get("connections") or {}).items():
                if not isinstance(cur, dict):
                    continue
                detail = cur.get("detail")
                n = con.execute(
                    "INSERT OR IGNORE INTO connection_state(name, last_status, last_attempt_at, last_success_at, last_failure_at,"
                    " last_failure_reason, consecutive_failures, detail_json, updated_at, migrated_from) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (str(name), cur.get("last_status"), cur.get("last_attempt_at"), cur.get("last_success_at"),
                     cur.get("last_failure_at"), str(cur.get("last_failure_reason") or "")[:300],
                     int(cur.get("consecutive_failures") or 0),
                     json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail is not None else None,
                     cur.get("last_attempt_at") or db.now(), self.path.name)).rowcount
                out["connections"] += n
            for name, idx in (doc.get("watch") or {}).items():
                for rel, entry in (idx or {}).items() if isinstance(idx, dict) else ():
                    if isinstance(entry, dict):
                        out["watch"] += con.execute(
                            "INSERT OR IGNORE INTO connection_watch(name, rel_path, file_size, file_mtime, file_hash, status, seen_at)"
                            " VALUES(?,?,?,?,?,?,?)", (str(name), str(rel), *(entry.get(k) for k, _ in _WATCH))).rowcount
            if began:
                con.commit()
        except Exception:
            if began:
                con.rollback()
            raise
        return out

    # ---------------------------------------------------------- 연결 상태
    @staticmethod
    def _row(r) -> dict:
        d = {k: r[k] for k in _FIELDS if k not in ("detail_json", "updated_at", "migrated_from")}
        d["consecutive_failures"] = int(d["consecutive_failures"] or 0)
        if r["detail_json"] is not None:
            try:
                d["detail"] = json.loads(r["detail_json"])
            except ValueError:
                pass
        if r["migrated_from"]:
            d["migrated_from"] = r["migrated_from"]
        return d

    def all(self) -> dict:
        with self._db() as con:
            return {r["name"]: self._row(r) for r in con.execute("SELECT * FROM connection_state ORDER BY name")}

    def get(self, name: str) -> dict | None:
        with self._db() as con:
            r = con.execute("SELECT * FROM connection_state WHERE name=?", (name,)).fetchone()
            return self._row(r) if r else None

    def record(self, name: str, status: str, reason: str = "", detail: dict | None = None) -> dict:
        """status 는 ok / failed / not_configured. 성공이면 실패 횟수를 0 으로 되돌린다."""
        now = db.now()
        if status == OK:
            reason_v = None                                   # 성공해도 마지막 실패 사유는 남긴다
        elif status == NOT_CONFIGURED:
            reason_v = (reason or "설정 안 됨")[:300]
        else:
            status = status or FAILED
            reason_v = (reason or "사유 없음")[:300]
        detail_v = json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str) if detail is not None else None
        with self._db(write=True) as con:
            con.execute(
                """INSERT INTO connection_state(name, last_status, last_attempt_at, last_success_at, last_failure_at,
                       last_failure_reason, consecutive_failures, detail_json, updated_at)
                   VALUES(:name, :status, :now,
                          CASE WHEN :status = :ok THEN :now END,
                          CASE WHEN :status NOT IN (:ok, :nc) THEN :now END,
                          COALESCE(:reason, ''),
                          CASE WHEN :status NOT IN (:ok, :nc) THEN 1 ELSE 0 END,
                          :detail, :now)
                   ON CONFLICT(name) DO UPDATE SET
                       last_status = excluded.last_status,
                       last_attempt_at = excluded.last_attempt_at,
                       last_success_at = CASE WHEN :status = :ok THEN :now ELSE last_success_at END,
                       last_failure_at = CASE WHEN :status NOT IN (:ok, :nc) THEN :now ELSE last_failure_at END,
                       last_failure_reason = COALESCE(:reason, last_failure_reason),
                       consecutive_failures = CASE WHEN :status = :ok THEN 0
                                                   WHEN :status = :nc THEN consecutive_failures
                                                   ELSE consecutive_failures + 1 END,
                       detail_json = COALESCE(:detail, detail_json),
                       updated_at = :now""",
                {"name": name, "status": status, "now": now, "ok": OK, "nc": NOT_CONFIGURED, "reason": reason_v, "detail": detail_v})
            return self._row(con.execute("SELECT * FROM connection_state WHERE name=?", (name,)).fetchone())

    def record_success(self, name: str, detail: dict | None = None) -> dict:
        return self.record(name, OK, detail=detail)

    def record_failure(self, name: str, reason: str, detail: dict | None = None) -> dict:
        return self.record(name, FAILED, reason=reason, detail=detail)

    def record_outcome(self, name: str, outcome, detail: dict | None = None) -> dict:
        return self.record(name, outcome.status, reason=outcome.reason, detail=detail)

    # ---------------------------------------------------------- 폴더 감시 목록
    def watch_index(self, name: str) -> dict:
        with self._db() as con:
            return {r["rel_path"]: {k: r[c] for k, c in _WATCH}
                    for r in con.execute("SELECT * FROM connection_watch WHERE name=?", (name,))}

    def update_watch(self, name: str, rel: str, entry: dict) -> None:
        vals = [entry.get(k) for k, _ in _WATCH]
        with self._db(write=True) as con:
            con.execute(
                "INSERT INTO connection_watch(name, rel_path, file_size, file_mtime, file_hash, status, seen_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(name, rel_path) DO UPDATE SET file_size=excluded.file_size, file_mtime=excluded.file_mtime,"
                " file_hash=excluded.file_hash, status=excluded.status, seen_at=excluded.seen_at",
                (name, rel, *vals))
