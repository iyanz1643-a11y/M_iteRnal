"""원본 보관. data/raw/<종류>/<YYYYMM>/<해시 앞 16자>_<파일 이름> 에 바이트 그대로 둔다.

- 같은 해시의 파일이 이미 있으면 다시 쓰지 않고 그 경로를 돌려준다.
- 옆에 .meta.json 을 두고 전체 해시, 들어온 길, 받은 시각, 크기를 적는다.
- 원본은 지우지 않는다.
"""
from __future__ import annotations

import json
import os
import pathlib
import re

from app.connect.base import RawFile
from app.ops.storage import LocalStorage

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = ROOT / "data"
UNKNOWN_YM = "unknown"

_UNSAFE = re.compile(r"[^\w.\-가-힣]+")


def default_data_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CONNECT_DATA_DIR") or DEFAULT_DATA_DIR)


def safe_name(name: str) -> str:
    p = pathlib.Path(name)
    stem = _UNSAFE.sub("_", p.stem).strip("._") or "file"
    suffix = _UNSAFE.sub("", p.suffix.lower())[:10]
    return stem[:80] + suffix


def store_raw(raw: RawFile, data_dir: str | pathlib.Path | None = None) -> tuple[pathlib.Path, str]:
    """원본을 보관하고 (절대 경로, data 아래 상대 경로) 를 돌려준다. 쓰기는 app/ops/storage.py 의 raw 구역이 한다."""
    data_dir = pathlib.Path(data_dir) if data_dir else default_data_dir()
    st = LocalStorage({"raw": data_dir / "raw"})
    folder = f"{raw.kind or 'unknown'}/{raw.period_ym or UNKNOWN_YM}"
    key, new = st.put_named("raw", folder, safe_name(raw.name), raw.data)
    if new:
        meta = {"sha256": raw.sha256, "file_name": raw.name, "via": raw.via, "kind": raw.kind,
                "period_ym": raw.period_ym, "received_at": raw.received_at, "size": raw.size}
        st.put_side("raw", key, ".meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
    return st.local_path("raw", key), f"raw/{key}"
