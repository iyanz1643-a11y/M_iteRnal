"""파일 저장 추상화. 원본(data/raw), 공지 첨부(data/board), 화면 업로드를 한 인터페이스로 모은다.

지금은 로컬 폴더 구현 LocalStorage 하나다. 객체 저장소를 붙일 때는 Storage 의 메서드를 같은 뜻으로 구현하고
APP_STORAGE 로 고른다. 폴더와 파일 이름은 예전과 같다.

| 구역 area | 폴더 | 이름 | 지우기 |
| --- | --- | --- | --- |
| raw | CONNECT_DATA_DIR/raw, 없으면 data/raw | <종류>/<YYYYMM>/<해시 앞 16자>_<파일 이름> | 하지 않는다. 원본이다 |
| board | APP_BOARD_DIR, 없으면 data/board | 내용의 SHA-256 | 하지 않는다 |
| uploads | APP_UPLOAD_DIR, 없으면 data/raw/uploads | 화면이 정한 이름 | 한다. 미리보기만 하고 버린 파일이다 |

- 키는 구역 안의 상대 경로다. 위로 올라가는 .. 와 절대 경로는 받지 않는다.
- 같은 내용을 두 번 넣으면 다시 쓰지 않고 앞의 키를 돌려준다(put_hashed, put_named).
- 쓸 때는 임시 파일에 쓰고 이름을 바꿔 반쯤 쓴 파일이 보이지 않게 한다.
- 적재기는 파일 경로를 받으므로 local_path 를 둔다. 객체 저장소 구현은 임시 폴더로 내려받아 그 경로를 준다.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import secrets

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
AREAS = ("raw", "board", "uploads")
KEEP = {"raw", "board"}                 # 지우지 않는 구역


class StorageError(ValueError):
    pass


def default_roots() -> dict:
    data = ROOT / "data"
    return {
        "raw": pathlib.Path(os.environ.get("CONNECT_DATA_DIR") or data) / "raw",
        "board": pathlib.Path(os.environ.get("APP_BOARD_DIR") or data / "board"),
        "uploads": pathlib.Path(os.environ.get("APP_UPLOAD_DIR") or data / "raw" / "uploads"),
    }


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Storage:
    """저장소 인터페이스. 키는 구역 안의 / 로 이은 상대 경로다."""

    def put_hashed(self, area: str, data: bytes) -> str:
        """내용의 SHA-256 을 이름으로 둔다. 키는 해시 그대로다."""
        raise NotImplementedError

    def put_named(self, area: str, folder: str, name: str, data: bytes) -> tuple:
        """folder/<해시 앞 16자>_<name> 에 둔다. (키, 새로 썼는가). 같은 해시가 그 폴더에 있으면 그 키를 돌려준다."""
        raise NotImplementedError

    def put_at(self, area: str, key: str, data: bytes) -> str:
        """정한 키에 그대로 쓴다. 이미 있으면 덮어쓴다. 원본 구역에는 쓰지 않는다."""
        raise NotImplementedError

    def get(self, area: str, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, area: str, key: str) -> bool:
        raise NotImplementedError

    def local_path(self, area: str, key: str) -> pathlib.Path:
        raise NotImplementedError

    def keys(self, area: str, folder: str = "") -> list:
        raise NotImplementedError

    def delete(self, area: str, key: str) -> bool:
        raise NotImplementedError

    def root(self, area: str) -> pathlib.Path:
        """로컬 구현에서만 뜻이 있다. 예전 코드가 폴더를 직접 받던 자리에 쓴다."""
        raise NotImplementedError


class LocalStorage(Storage):
    def __init__(self, roots: dict | None = None):
        self._roots = {k: pathlib.Path(v) for k, v in (roots or {}).items()}

    # ---------------------------------------------------------- 경로
    def root(self, area):
        if area not in AREAS:
            raise StorageError(f"없는 구역입니다: {area}")
        return self._roots.get(area) or default_roots()[area]

    def _path(self, area, key) -> pathlib.Path:
        key = str(key or "").replace("\\", "/")
        parts = [p for p in key.split("/") if p not in ("", ".")]
        if not parts or key.startswith("/") or any(p == ".." for p in parts) or ":" in parts[0]:
            raise StorageError("저장 위치 키가 올바르지 않습니다.")
        return self.root(area).joinpath(*parts)

    def _write(self, target: pathlib.Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.{secrets.token_hex(4)}.part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target)

    def _key(self, area, path: pathlib.Path) -> str:
        return path.relative_to(self.root(area)).as_posix()

    # ---------------------------------------------------------- 쓰기
    def put_hashed(self, area, data):
        digest = sha256(data)
        p = self._path(area, digest)
        if not p.is_file():
            self._write(p, data)
        return digest

    def put_named(self, area, folder, name, data):
        prefix = sha256(data)[:16]
        base = self._path(area, f"{folder}/{prefix}_{name}") if folder else self._path(area, f"{prefix}_{name}")
        existing = sorted(p for p in base.parent.glob(f"{prefix}_*") if not p.name.endswith(".meta.json")) if base.parent.is_dir() else []
        if existing:
            return self._key(area, existing[0]), False
        self._write(base, data)
        return self._key(area, base), True

    def put_at(self, area, key, data):
        if area in KEEP:
            raise StorageError("원본 구역에는 정한 이름으로 덮어쓰지 않습니다.")
        p = self._path(area, key)
        self._write(p, data)
        return self._key(area, p)

    def put_side(self, area, key, suffix, text: str) -> str:
        """원본 옆의 설명 파일(.meta.json 같은). 원본과 같은 폴더에 둔다."""
        p = self._path(area, key)
        side = p.with_name(p.name + suffix)
        self._write(side, text.encode("utf-8"))
        return self._key(area, side)

    # ---------------------------------------------------------- 읽기
    def get(self, area, key):
        p = self._path(area, key)
        if not p.is_file():
            raise FileNotFoundError(key)
        return p.read_bytes()

    def exists(self, area, key):
        try:
            return self._path(area, key).is_file()
        except StorageError:
            return False

    def local_path(self, area, key):
        return self._path(area, key)

    def keys(self, area, folder=""):
        base = self._path(area, folder) if folder else self.root(area)
        if not base.is_dir():
            return []
        return sorted(self._key(area, p) for p in base.rglob("*") if p.is_file() and not p.name.startswith("."))

    def delete(self, area, key):
        if area in KEEP:
            raise StorageError("원본과 첨부는 지우지 않습니다.")
        p = self._path(area, key)
        if p.is_file():
            p.unlink()
            return True
        return False


def get_storage(roots: dict | None = None) -> Storage:
    """APP_STORAGE 로 구현을 고른다. 지금은 local 하나다. 객체 저장소는 D-14 가 정해진 뒤에 붙인다."""
    kind = (os.environ.get("APP_STORAGE") or "local").strip().lower()
    if kind != "local":
        raise StorageError(f"저장소 구현이 아직 없습니다: {kind}. APP_STORAGE 를 비우거나 local 로 두세요.")
    return LocalStorage(roots)
