"""운영 연결 어댑터. 바깥과 잇는 코드는 이 폴더에만 둔다.

흐름: 출처에서 가져오기, data/raw 에 원본과 해시 보관, 이미 넣은 해시면 건너뛰기, 종류에 맞는 적재기로 넘기기.
명령줄: python -m app.connect --help
"""
from app.connect.base import KINDS, IngestResult, NotConfigured, Outcome, RawFile, Skipped
from app.connect.handlers import DEFAULT_REGISTRY, Registry, register_handler
from app.connect.pipeline import Pipeline, SourceRun
from app.connect.sources import FolderWatchSource, GoogleDriveSource, UploadSource
from app.connect.state import StateStore

__all__ = [
    "KINDS", "IngestResult", "NotConfigured", "Outcome", "RawFile", "Skipped",
    "DEFAULT_REGISTRY", "Registry", "register_handler",
    "Pipeline", "SourceRun",
    "FolderWatchSource", "GoogleDriveSource", "UploadSource",
    "StateStore",
]
