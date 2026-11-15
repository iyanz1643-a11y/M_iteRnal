"""DB 백업과 되살리기. 명령은 scripts/backup_db.py 와 scripts/restore_check.py 다.

| 무엇 | 어떻게 |
| --- | --- |
| 백업 | SQLite 의 온라인 백업으로 쓰는 중에도 한 시점의 사본을 뜬다. 이름은 app-YYYYMMDD-HHMMSS.db, 암호화하면 .db.enc |
| 설명 파일 | 같은 이름의 .json. 형식, 만든 시각, 암호화 여부, 원본 크기와 SHA-256, 표별 건수, 스키마 버전. 자료 내용은 넣지 않는다 |
| 암호화 | BACKUP_KEY 가 있으면 기본으로 한다. AES-256-GCM 을 1MB 조각마다 걸고 조각 번호와 끝 표시를 함께 묶어 순서 바꾸기와 자르기를 막는다. 키는 scrypt 로 만든다 |
| 키 | 환경 변수 BACKUP_KEY 로만 받는다. 파일, 명령줄 인자, 설명 파일에 남기지 않는다. 12자 이상 |
| 되살리기 확인 | 임시 폴더에 풀어 SHA-256, integrity_check, 표별 건수, 스키마 버전을 설명 파일과 견준다 |
| 되살리기 | 대상 파일이 있으면 멈춘다. --force 면 있던 파일을 .before-restore-시각 으로 옮겨 두고 바꾼다. 지우지 않는다 |
| 보관 | 지우지 않는 것이 기본이다. --keep N 을 주면 그 폴더의 이 꼴 백업 가운데 최근 N 개만 남긴다 |

PostgreSQL 로 옮긴 뒤에는 관리형 DB 의 자동 백업을 쓰고, 이 스크립트는 pg_dump 사본을 같은 꼴로 암호화하는 데 쓴다(D-14 뒤에 정한다).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import struct
import tempfile
from datetime import datetime

from app.ops import schema

MAGIC = b"GABK\x01\x00"
CHUNK = 1024 * 1024
ENV_KEY = "BACKUP_KEY"
ENV_DIR = "BACKUP_DIR"
NAME_RE = re.compile(r"^app-\d{8}-\d{6}\.db(\.enc)?$")
MIN_KEY = 12


class BackupError(RuntimeError):
    pass


def default_dir() -> pathlib.Path:
    from app import db
    return pathlib.Path(os.environ.get(ENV_DIR) or db.ROOT / "data" / "backup")


def _key() -> str | None:
    k = os.environ.get(ENV_KEY) or None
    if k is not None and len(k) < MIN_KEY:
        raise BackupError(f"{ENV_KEY} 는 {MIN_KEY}자 이상이어야 합니다.")
    return k


def _derive(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode("utf-8"))


def _sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- 암호화
def encrypt_file(src: pathlib.Path, dst: pathlib.Path, passphrase: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, prefix = secrets.token_bytes(16), secrets.token_bytes(8)
    head = MAGIC + salt + prefix
    aes = AESGCM(_derive(passphrase, salt))
    size = src.stat().st_size
    with open(src, "rb") as f, open(dst, "wb") as out:
        out.write(head)
        idx, done = 0, 0
        while True:
            block = f.read(CHUNK)
            done += len(block)
            last = done >= size
            ct = aes.encrypt(prefix + struct.pack(">I", idx), block, head + struct.pack(">IB", idx, 1 if last else 0))
            out.write(struct.pack(">I", len(ct)) + ct)
            idx += 1
            if last:
                break


def decrypt_file(src: pathlib.Path, dst: pathlib.Path, passphrase: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    size = src.stat().st_size
    with open(src, "rb") as f, open(dst, "wb") as out:
        head = f.read(len(MAGIC) + 24)
        if not head.startswith(MAGIC):
            raise BackupError("이 앱의 암호화 백업 파일이 아닙니다.")
        salt, prefix = head[len(MAGIC):len(MAGIC) + 16], head[len(MAGIC) + 16:]
        aes = AESGCM(_derive(passphrase, salt))
        idx, ended = 0, False
        while f.tell() < size:
            n = struct.unpack(">I", f.read(4))[0]
            ct = f.read(n)
            if len(ct) != n:
                raise BackupError("백업 파일이 잘렸습니다.")
            last = f.tell() >= size
            try:
                out.write(aes.decrypt(prefix + struct.pack(">I", idx), ct, head + struct.pack(">IB", idx, 1 if last else 0)))
            except InvalidTag:
                raise BackupError("키가 맞지 않거나 백업 파일이 바뀌었습니다.") from None
            ended = last
            idx += 1
        if not ended:
            raise BackupError("백업 파일이 잘렸습니다.")


# ---------------------------------------------------------------- 설명
def describe_db(path: pathlib.Path) -> dict:
    """표별 건수와 스키마 버전. 내용은 읽지 않는다."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        return {"integrity": ok, "tables": counts, "schema_version": schema.schema_version(con)}
    finally:
        con.close()


# ---------------------------------------------------------------- 백업
def backup(db_path, out_dir=None, *, encrypt: bool | None = None, now: datetime | None = None, keep: int | None = None) -> dict:
    """백업 파일과 설명 파일을 만들고 설명을 돌려준다. encrypt 가 None 이면 BACKUP_KEY 가 있을 때 암호화한다."""
    src = pathlib.Path(db_path)
    if not src.is_file():
        raise BackupError(f"DB 파일이 없습니다: {src.name}")
    key = _key()
    if encrypt is None:
        encrypt = key is not None
    if encrypt and not key:
        raise BackupError(f"암호화하려면 환경 변수 {ENV_KEY} 가 있어야 합니다.")
    out = pathlib.Path(out_dir) if out_dir else default_dir()
    out.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    name = f"app-{stamp}.db" + (".enc" if encrypt else "")
    target = out / name
    if target.exists():
        raise BackupError(f"같은 이름의 백업이 이미 있습니다: {name}")
    with tempfile.TemporaryDirectory(prefix="gabk-") as tmp:
        snap = pathlib.Path(tmp) / "snap.db"
        s, d = sqlite3.connect(str(src)), sqlite3.connect(str(snap))
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        info = describe_db(snap)
        if info["integrity"] != "ok":
            raise BackupError("원본 DB 의 무결성 검사가 실패해 백업하지 않았습니다.")
        manifest = {"format": 1, "kind": "sqlite", "file": name, "created_at": (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
                    "encrypted": bool(encrypt), "size": snap.stat().st_size, "sha256": _sha256_file(snap),
                    "schema_version": info["schema_version"], "tables": info["tables"]}
        part = out / f".{name}.part"
        if encrypt:
            encrypt_file(snap, part, key)
        else:
            shutil.copyfile(snap, part)
        os.replace(part, target)
    target.with_name(name + ".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    if keep:
        manifest["removed"] = prune(out, keep)
    return manifest


def prune(out_dir, keep: int) -> list:
    """이 꼴의 백업 가운데 최근 keep 개만 남긴다. 지운 이름 목록."""
    out = pathlib.Path(out_dir)
    files = sorted((p for p in out.iterdir() if NAME_RE.match(p.name)), key=lambda p: p.name)
    gone = []
    for p in files[:-keep] if keep > 0 else []:
        p.unlink()
        side = p.with_name(p.name + ".json")
        if side.exists():
            side.unlink()
        gone.append(p.name)
    return gone


# ---------------------------------------------------------------- 되살리기
def _manifest(backup_file: pathlib.Path) -> dict:
    side = backup_file.with_name(backup_file.name + ".json")
    if not side.exists():
        raise BackupError("설명 파일(.json)이 없어 확인할 수 없습니다.")
    return json.loads(side.read_text(encoding="utf-8"))


def unpack(backup_file, dst: pathlib.Path) -> dict:
    """백업을 dst 에 풀고 설명 파일과 견준다. 맞지 않으면 BackupError."""
    bf = pathlib.Path(backup_file)
    m = _manifest(bf)
    if m.get("encrypted"):
        key = _key()
        if not key:
            raise BackupError(f"암호화한 백업입니다. 환경 변수 {ENV_KEY} 가 있어야 합니다.")
        decrypt_file(bf, dst, key)
    else:
        shutil.copyfile(bf, dst)
    if _sha256_file(dst) != m["sha256"]:
        raise BackupError("푼 파일의 SHA-256 이 설명 파일과 다릅니다.")
    info = describe_db(dst)
    problems = []
    if info["integrity"] != "ok":
        problems.append("integrity_check")
    if info["tables"] != m["tables"]:
        problems.append("표별 건수")
    if info["schema_version"] != m.get("schema_version"):
        problems.append("스키마 버전")
    if problems:
        raise BackupError("되살린 DB 가 설명 파일과 다릅니다: " + ", ".join(problems))
    return {"file": bf.name, "ok": True, "tables": len(info["tables"]), "rows": sum(info["tables"].values()),
            "schema_version": info["schema_version"], "encrypted": bool(m.get("encrypted"))}


def verify(backup_file) -> dict:
    """되살리기 확인. 임시 폴더에 풀어 보고 지운다. 원래 DB 는 건드리지 않는다."""
    with tempfile.TemporaryDirectory(prefix="gabk-") as tmp:
        return unpack(backup_file, pathlib.Path(tmp) / "restore.db")


def restore(backup_file, target, *, force: bool = False) -> dict:
    """백업을 target 으로 되살린다. 있던 파일은 지우지 않고 옆으로 옮긴다."""
    target = pathlib.Path(target)
    if target.exists() and not force:
        raise BackupError("대상 파일이 이미 있습니다. 바꾸려면 --force 를 주세요. 있던 파일은 옆으로 옮겨 둡니다.")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.restore.part")
    out = unpack(backup_file, part)
    if target.exists():
        moved = target.with_name(f"{target.name}.before-restore-{datetime.now():%Y%m%d-%H%M%S}")
        os.replace(target, moved)
        out["moved"] = moved.name
    os.replace(part, target)
    out["restored_to"] = str(target)
    return out
