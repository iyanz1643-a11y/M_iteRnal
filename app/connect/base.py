"""연결 어댑터의 공통 형식. 원본 파일 하나, 결과, 상태 값.

- RawFile: 바깥에서 들어온 원본 파일 하나. 이름, 바이트, 들어온 길, 종류, 귀속월과 해시를 담는다.
- Outcome: 바깥 호출 한 번의 결과. 예외 대신 이 값으로 돌려준다.
- IngestResult: 파일 하나를 보관하고 적재기로 넘긴 결과.
결과 메시지에는 사람 이름과 금액을 넣지 않는다.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app import db

# 종류. 키는 명령줄과 data/raw 폴더 이름에 쓴다
KINDS = {"contract": "계약현황", "plan": "시상안", "employee": "사원현황"}

# 들어온 길. import_batch.received_via 에 그대로 적는다
VIA_UPLOAD = "upload"
VIA_FOLDER = "folder"
VIA_GDRIVE = "gdrive"

# 결과 상태
OK = "ok"
LOADED = "loaded"
DUPLICATE = "duplicate"
NO_HANDLER = "no_handler"
FAILED = "failed"
NOT_CONFIGURED = "not_configured"
SKIPPED = "skipped"

STATUS_KO = {
    OK: "성공", LOADED: "적재함", DUPLICATE: "이미 넣은 파일", NO_HANDLER: "처리기 없음",
    FAILED: "실패", NOT_CONFIGURED: "설정 안 됨", SKIPPED: "건너뜀",
}

YM_RE = re.compile(r"^(20\d{2})(0[1-9]|1[0-2])$")


class NotConfigured(Exception):
    """키나 주소가 없어 연결을 시작하지 않았다. 재시도하지 않는다."""


def valid_ym(ym: str | None) -> bool:
    return bool(ym) and bool(YM_RE.match(str(ym)))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def describe(exc: BaseException | None) -> str:
    """예외를 한 줄 사유로. 길이를 자른다."""
    if exc is None:
        return ""
    msg = str(exc).strip().replace("\n", " ")
    name = type(exc).__name__
    return (f"{name}: {msg}" if msg else name)[:300]


@dataclass
class RawFile:
    name: str
    data: bytes = field(repr=False)
    via: str
    kind: str | None = None
    period_ym: str | None = None
    received_at: str = field(default_factory=db.now)
    meta: dict = field(default_factory=dict)
    sha256: str = field(init=False)

    def __post_init__(self):
        self.name = pathlib.Path(str(self.name)).name or "file"
        self.sha256 = sha256(self.data)

    @property
    def size(self) -> int:
        return len(self.data)

    @classmethod
    def from_path(cls, path, via=VIA_UPLOAD, kind=None, period_ym=None, **meta) -> RawFile:
        p = pathlib.Path(path)
        return cls(name=p.name, data=p.read_bytes(), via=via, kind=kind, period_ym=period_ym, meta=meta)


@dataclass(frozen=True)
class Skipped:
    """출처가 읽지 못하고 건너뛴 파일. 너무 크거나 읽기 오류가 난 경우."""
    name: str
    reason: str


@dataclass
class Outcome:
    status: str
    value: Any = None
    reason: str = ""
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == OK


@dataclass
class IngestResult:
    status: str
    file_name: str
    kind: str | None = None
    period_ym: str | None = None
    via: str | None = None
    file_hash: str | None = None
    raw_path: str | None = None       # data 아래 상대 경로
    reason: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def status_ko(self) -> str:
        return STATUS_KO.get(self.status, self.status)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status_ko"] = self.status_ko
        return d
