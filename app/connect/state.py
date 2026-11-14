"""연결 상태 기록. 연결마다 마지막 시도, 마지막 성공 시각, 마지막 실패 사유를 남긴다.

지금은 data/state/connections.json 한 파일에 둔다. 나중에 테이블로 옮길 때는 이 파일의
StateStore 만 바꾸면 된다. 다른 모듈은 이 파일 밖에서 상태 JSON 을 읽거나 쓰지 않는다.

JSON 모양:
    {"connections": {이름: {...}}, "watch": {감시 이름: {상대 경로: {size, mtime, hash, status}}}}
사유에는 사람 이름과 금액을 넣지 않는다.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading

from app import db
from app.connect.base import FAILED, NOT_CONFIGURED, OK

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DEFAULT_STATE_DIR = ROOT / "data" / "state"
STATE_FILE = "connections.json"

_LOCK = threading.Lock()


def default_state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CONNECT_STATE_DIR") or DEFAULT_STATE_DIR)


class StateStore:
    def __init__(self, path: str | pathlib.Path | None = None):
        p = pathlib.Path(path) if path else default_state_dir()
        self.path = p if p.suffix == ".json" else p / STATE_FILE

    # ---------------------------------------------------------- 파일 읽고 쓰기
    def _load(self) -> dict:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            doc = {}
        except (OSError, ValueError):
            # 깨진 상태 파일 때문에 연결이 멈추면 안 된다. 옆에 남기고 새로 시작한다
            try:
                self.path.replace(self.path.with_suffix(".broken.json"))
            except OSError:
                pass
            doc = {}
        doc.setdefault("connections", {})
        doc.setdefault("watch", {})
        return doc

    def _save(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    # ---------------------------------------------------------- 연결 상태
    def all(self) -> dict:
        with _LOCK:
            return self._load()["connections"]

    def get(self, name: str) -> dict | None:
        return self.all().get(name)

    def record(self, name: str, status: str, reason: str = "", detail: dict | None = None) -> dict:
        """status 는 ok / failed / not_configured. 성공이면 실패 횟수를 0 으로 되돌린다."""
        now = db.now()
        with _LOCK:
            doc = self._load()
            cur = doc["connections"].setdefault(name, {
                "name": name, "last_success_at": None, "last_failure_at": None, "last_failure_reason": "",
                "consecutive_failures": 0})
            cur["last_attempt_at"] = now
            cur["last_status"] = status
            if status == OK:
                cur["last_success_at"] = now
                cur["consecutive_failures"] = 0
            elif status == NOT_CONFIGURED:
                cur["last_failure_reason"] = reason or "설정 안 됨"
            else:
                cur["last_failure_at"] = now
                cur["last_failure_reason"] = (reason or "사유 없음")[:300]
                cur["consecutive_failures"] = int(cur.get("consecutive_failures") or 0) + 1
            if detail is not None:
                cur["detail"] = detail
            self._save(doc)
            return dict(cur)

    def record_success(self, name: str, detail: dict | None = None) -> dict:
        return self.record(name, OK, detail=detail)

    def record_failure(self, name: str, reason: str, detail: dict | None = None) -> dict:
        return self.record(name, FAILED, reason=reason, detail=detail)

    def record_outcome(self, name: str, outcome, detail: dict | None = None) -> dict:
        return self.record(name, outcome.status, reason=outcome.reason, detail=detail)

    # ---------------------------------------------------------- 폴더 감시 목록
    def watch_index(self, name: str) -> dict:
        with _LOCK:
            return self._load()["watch"].get(name, {})

    def update_watch(self, name: str, rel: str, entry: dict) -> None:
        with _LOCK:
            doc = self._load()
            doc["watch"].setdefault(name, {})[rel] = entry
            self._save(doc)
