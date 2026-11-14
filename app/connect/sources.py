"""출처. 원본 파일을 가져오는 곳 세 가지.

- UploadSource: 파일 직접 올리기. 항상 남겨 두는 기본 경로다.
- FolderWatchSource: 지정 폴더의 새 파일을 가져온다. 드라이브 동기화 폴더를 대신한다.
- GoogleDriveSource: 인터페이스만 있다. 실제 호출은 S3 에서 만든다.

출처의 꼴:
    name: str                         연결 상태에 쓰는 이름
    fetch() -> list[RawFile | Skipped] 실패하면 예외를 던져도 된다. 파이프라인이 재시도하고 결과로 바꾼다
    done(raw, result) -> None         선택. 파일 하나를 처리한 뒤 불린다
"""
from __future__ import annotations

import os
import pathlib
import re
import time
from typing import Protocol, runtime_checkable

from app.connect.base import DUPLICATE, FAILED, KINDS, LOADED, VIA_FOLDER, VIA_GDRIVE, VIA_UPLOAD, NotConfigured, RawFile, Skipped, valid_ym
from app.connect.state import StateStore


@runtime_checkable
class Source(Protocol):
    name: str

    def fetch(self) -> list: ...


# ------------------------------------------------------------------ 파일 직접 올리기
class UploadSource:
    name = "upload"

    def __init__(self, files):
        self.files = list(files)

    @classmethod
    def from_path(cls, path, kind=None, period_ym=None, **meta) -> UploadSource:
        return cls([RawFile.from_path(path, via=VIA_UPLOAD, kind=kind, period_ym=period_ym, **meta)])

    @classmethod
    def from_bytes(cls, name, data, kind=None, period_ym=None, **meta) -> UploadSource:
        return cls([RawFile(name=name, data=data, via=VIA_UPLOAD, kind=kind, period_ym=period_ym, meta=meta)])

    def fetch(self) -> list:
        return list(self.files)


# ------------------------------------------------------------------ 종류와 귀속월 짐작
KIND_WORDS = [("contract", ("contract", "계약")), ("plan", ("plan", "시상", "시책")), ("employee", ("employee", "사원"))]
_DATE8 = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])[0-3]\d(?!\d)")
_YM = re.compile(r"(?<!\d)(20\d{2})\s*[-_.년]?\s*(1[0-2]|0?[1-9])(?!\d)")


def infer_kind(rel_path) -> str | None:
    """폴더 이름을 먼저 보고, 없으면 파일 이름의 낱말로 짐작한다."""
    parts = [p.lower() for p in pathlib.PurePosixPath(str(rel_path).replace("\\", "/")).parts]
    for part in parts:
        if part in KINDS:
            return part
        for kind, words in KIND_WORDS:
            if any(w in part for w in words):
                return kind
    return None


def infer_ym(text) -> str | None:
    s = str(text)
    for rx in (_DATE8, _YM):
        m = rx.search(s)
        if m:
            ym = f"{m.group(1)}{int(m.group(2)):02d}"
            if valid_ym(ym):
                return ym
    return None


# ------------------------------------------------------------------ 폴더 감시
IGNORE_PREFIX = (".", "~$", "~")
IGNORE_SUFFIX = (".tmp", ".part", ".partial", ".crdownload", ".download", ".meta.json")
ALLOW_SUFFIX = (".xlsx", ".xlsm", ".xls", ".csv", ".txt", ".json")
# 한 번 끝난 파일은 바뀌지 않으면 다시 읽지 않는다. 처리기 없음과 건너뜀은 다음에 다시 본다
FINAL = (LOADED, DUPLICATE, FAILED)


class FolderWatchSource:
    def __init__(self, folder, kind=None, period_ym=None, state: StateStore | None = None, min_age: float = 2.0,
                 max_bytes: int = 50 * 1024 * 1024, rescan: bool = False, clock=time.time):
        self.folder = pathlib.Path(folder)
        self.kind = kind
        self.period_ym = period_ym
        self.state = state or StateStore()
        self.min_age = min_age
        self.max_bytes = max_bytes
        self.rescan = rescan
        self.clock = clock
        self.name = f"folder:{self.folder.resolve().as_posix()}"

    def _candidates(self):
        for p in sorted(self.folder.rglob("*")):
            n = p.name.lower()
            if not p.is_file() or n.startswith(IGNORE_PREFIX) or n.endswith(IGNORE_SUFFIX):
                continue
            if any(part.startswith(".") for part in p.relative_to(self.folder).parts[:-1]):
                continue
            if p.suffix.lower() in ALLOW_SUFFIX:
                yield p

    def fetch(self) -> list:
        if not self.folder.is_dir():
            raise ConnectionError(f"감시 폴더가 없습니다: {self.folder.name}")
        index = {} if self.rescan else self.state.watch_index(self.name)
        now = self.clock()
        out = []
        for p in self._candidates():
            rel = p.relative_to(self.folder).as_posix()
            st = p.stat()
            if now - st.st_mtime < self.min_age:
                continue                        # 아직 쓰는 중일 수 있다. 다음에 본다
            prev = index.get(rel)
            if prev and prev.get("size") == st.st_size and prev.get("mtime") == st.st_mtime and prev.get("status") in FINAL:
                continue
            if st.st_size > self.max_bytes:
                out.append(Skipped(rel, f"파일이 {self.max_bytes // (1024 * 1024)}MB 를 넘습니다. 파일 올리기로 넣어 주세요"))
                continue
            try:
                data = p.read_bytes()
            except OSError as e:
                out.append(Skipped(rel, f"읽지 못했습니다: {type(e).__name__}"))
                continue
            out.append(RawFile(name=p.name, data=data, via=VIA_FOLDER,
                               kind=self.kind or infer_kind(rel), period_ym=self.period_ym or infer_ym(rel),
                               meta={"rel": rel, "size": st.st_size, "mtime": st.st_mtime}))
        return out

    def done(self, raw: RawFile, result) -> None:
        rel = raw.meta.get("rel")
        if rel:
            self.state.update_watch(self.name, rel, {"size": raw.meta.get("size"), "mtime": raw.meta.get("mtime"),
                                                     "hash": raw.sha256, "status": result.status, "at": raw.received_at})


# ------------------------------------------------------------------ 구글 드라이브
GDRIVE_ENV_KEY = "GDRIVE_SERVICE_ACCOUNT_FILE"
GDRIVE_ENV_FOLDER = "GDRIVE_FOLDER_ID"


class GoogleDriveSource:
    """서비스 계정의 읽기 전용 키로 지정 폴더를 가져올 자리. 지금은 호출하지 않는다."""

    name = "gdrive"
    via = VIA_GDRIVE

    def __init__(self, key_file=None, folder_id=None):
        self.key_file = key_file or os.environ.get(GDRIVE_ENV_KEY)
        self.folder_id = folder_id or os.environ.get(GDRIVE_ENV_FOLDER)

    @property
    def configured(self) -> bool:
        return bool(self.key_file and self.folder_id)

    def fetch(self) -> list:
        if not self.configured:
            raise NotConfigured(f"설정 안 됨: 환경 변수 {GDRIVE_ENV_KEY}, {GDRIVE_ENV_FOLDER} 가 필요합니다")
        raise NotConfigured("구글 드라이브 가져오기는 S3 에서 만듭니다. 지금은 파일 올리기나 폴더 감시를 써 주세요")
