"""공지와 자료실의 저장과 보는 사람 규칙. 화면은 app/web/routes/board.py, 알림은 app/web/routes/notify.py 가 이 파일을 부른다.

계산은 없다. 글과 첨부의 정보는 schema.sql 의 [board:begin] 묶음에, 첨부 파일은 저장소 밖 폴더에 둔다.

| 무엇 | 규칙 |
| --- | --- |
| 올리기, 고치기, 내리기 | 메인 관리자만. docs/09 2.4 에 관리자의 공지 올리기가 없어서다 |
| 대상 | 조직과 역할을 함께 고른다. 조직은 그 아래까지다. 비우면 모두다 |
| 보는 사람 | 메인 관리자는 모두. 그 밖에는 게시 중이고 대상 안인 사람만 |
| 대상 안 | 역할이 맞고, 소속 조직이나 맡은 조직이 대상 조직과 같거나 그 아래다. 관리자는 맡은 조직이 대상 조직의 위여도 대상 안이다. 지점 공지를 본부장도 본다(가정, 2026-11-15) |
| 시상안 원문 | 올릴 때 설계사에게 보여도 되는 FC 몫만 담긴 파일인지 고른다. 아니면 메인 관리자만 본다(D-18 제안) |
| 첨부 | pdf, 그림, 엑셀, 한글, 워드. 한 파일 20MB, 한 글에 5개까지. 확장자와 파일 머리의 꼴이 함께 맞아야 받는다 |
| 파일 위치 | APP_BOARD_DIR, 없으면 data/board. 이름은 내용의 SHA-256 이다. 같은 내용은 한 번만 둔다 |

변경 기록에는 글 번호와 첨부 번호만 남긴다. 본문과 파일 내용은 남기지 않는다.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import re
import secrets
import sqlite3
import zipfile
from hashlib import sha256
from urllib.parse import quote

from app import db
from app.engine import orgs

BEGIN, END = "-- [board:begin]", "-- [board:end]"
CATEGORY_KO = {"notice": "공지", "plan": "시상안 원문", "lesson": "교안", "form": "양식"}
ROLE_KO = {"manager": "관리자", "agent": "설계사"}
KIND_KO = {"pdf": "PDF", "image": "그림", "excel": "엑셀", "hwp": "한글", "word": "워드"}
MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 5
TITLE_MAX, BODY_MAX = 120, 5000
NOTICE_DAYS = 30                 # 알림함에 새 공지로 보이는 날 수
SHA = re.compile(r"^[0-9a-f]{64}$")

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"           # 예전 엑셀, 워드, 한글
ZIP = b"PK\x03\x04"                                  # xlsx, docx, hwpx


def _zip_has(prefix: str):
    def check(data: bytes) -> bool:
        if not data.startswith(ZIP):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return any(n.startswith(prefix) for n in z.namelist())
        except (zipfile.BadZipFile, ValueError):
            return False
    return check


def _hwpx(data: bytes) -> bool:
    if not data.startswith(ZIP):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            return "mimetype" in names and z.read("mimetype")[:40].startswith(b"application/hwp")
    except (zipfile.BadZipFile, ValueError, KeyError):
        return False


# 확장자: (꼴, 내려받을 때의 Content-Type, 파일 머리 검사)
TYPES = {
    ".pdf": ("pdf", "application/pdf", lambda d: d.startswith(b"%PDF-")),
    ".png": ("image", "image/png", lambda d: d.startswith(b"\x89PNG\r\n\x1a\n")),
    ".jpg": ("image", "image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image", "image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    ".gif": ("image", "image/gif", lambda d: d[:6] in (b"GIF87a", b"GIF89a")),
    ".webp": ("image", "image/webp", lambda d: d[:4] == b"RIFF" and d[8:12] == b"WEBP"),
    ".xlsx": ("excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _zip_has("xl/")),
    ".xls": ("excel", "application/vnd.ms-excel", lambda d: d.startswith(OLE)),
    ".hwp": ("hwp", "application/x-hwp", lambda d: d.startswith(OLE)),
    ".hwpx": ("hwp", "application/hwp+zip", _hwpx),
    ".docx": ("word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", _zip_has("word/")),
    ".doc": ("word", "application/msword", lambda d: d.startswith(OLE)),
}
ACCEPT = ",".join(TYPES)


# ---------------------------------------------------------------- 표와 폴더
def ensure(con) -> None:
    """게시판 표가 없는 오래된 DB 에서 schema.sql 의 게시판 묶음을 돌린다. 있으면 아무것도 하지 않는다."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='board_post'").fetchone():
        return
    text = db.SCHEMA.read_text(encoding="utf-8")
    con.executescript(text[text.index(BEGIN):text.index(END)])
    con.commit()


def board_dir() -> pathlib.Path:
    """첨부 파일 폴더. 만들지는 않는다. 쓸 때 save_blob 이 만든다."""
    return pathlib.Path(os.environ.get("APP_BOARD_DIR") or db.ROOT / "data" / "board")


def blob_path(digest: str) -> pathlib.Path | None:
    if not isinstance(digest, str) or not SHA.match(digest):
        return None
    p = board_dir() / digest
    return p if p.is_file() else None


def save_blob(data: bytes) -> str:
    """내용을 폴더에 두고 SHA-256 을 돌려준다. 같은 내용이 이미 있으면 다시 쓰지 않는다."""
    digest = sha256(data).hexdigest()
    d = board_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / digest
    if not p.is_file():
        tmp = d / f".{digest}.{secrets.token_hex(4)}.part"
        tmp.write_bytes(data)
        os.replace(tmp, p)
    return digest


# ---------------------------------------------------------------- 첨부 검사
def safe_name(name: str) -> str:
    """올린 파일의 이름. 경로를 떼고 위험한 글자를 바꾼다. 확장자는 작은 글자로."""
    base = pathlib.PurePosixPath(str(name or "").replace("\\", "/")).name.strip()
    stem, suffix = base.rsplit(".", 1) if "." in base else (base, "")
    stem = re.sub(r"[^0-9A-Za-z가-힣._ ()-]", "_", stem)[:80].strip(" .") or "file"
    return stem + ("." + suffix.lower() if suffix else "")


def check_file(name: str, data: bytes) -> tuple:
    """(안전한 이름, 꼴). 받지 않는 파일이면 ValueError. 확장자와 파일 머리가 함께 맞아야 한다."""
    clean = safe_name(name)
    suffix = pathlib.PurePosixPath(clean).suffix.lower()
    if suffix not in TYPES:
        raise ValueError(f"{clean}: PDF, 그림, 엑셀, 한글, 워드 파일만 받습니다.")
    if not data:
        raise ValueError(f"{clean}: 빈 파일입니다.")
    if len(data) > MAX_BYTES:
        raise ValueError(f"{clean}: 파일 하나는 {MAX_BYTES // (1024 * 1024)}MB 까지 받습니다.")
    kind, _, ok = TYPES[suffix]
    if not ok(data):
        raise ValueError(f"{clean}: 확장자와 파일 내용이 맞지 않습니다.")
    return clean, kind


def content_type(name: str) -> str:
    return TYPES.get(pathlib.PurePosixPath(name).suffix.lower(), (None, "application/octet-stream"))[1]


def disposition(name: str) -> str:
    """내려받기 머리글. 늘 attachment 로 보낸다. 한글 이름은 filename* 로 함께 보낸다."""
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "file"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


# ---------------------------------------------------------------- 보는 사람
def viewer_orgs(con, ctx) -> set:
    """대상 조직을 따질 때 쓰는 그 사람의 조직과 그 위 전부. 소속 조직과 맡은 조직(조직장, 대리)에서 시작한다."""
    start = set()
    if ctx.get("home_org"):
        start.add(ctx["home_org"]["org_id"])
    for h in ctx.get("heads") or []:
        start.add(h["org_id"])
    out = set()
    for o in start:
        out |= orgs.ancestors(con, o)
    return out


def can_see(ctx, post: dict, up: set) -> bool:
    """이 사람이 이 글과 그 첨부를 보는가. up 은 viewer_orgs 의 값이다.

    대상 조직의 아래에 있는 사람은 역할이 맞으면 본다. 대상 조직의 위에 있는 사람은 관리자만 본다.
    설계사는 지금처럼 자기 소속이 대상 안일 때만 본다.
    """
    if ctx["role"] == "main":
        return True
    if post["status"] != "posted":
        return False
    if post["category"] == "plan" and not post["fc_visible"]:
        return False
    if post["target_role"] and post["target_role"] != ctx["role"]:
        return False
    if post["target_org_id"] is not None and post["target_org_id"] not in up:
        # 관리자는 대상 조직의 위에서 맡고 있어도 본다. ctx['org_ids'] 는 맡은 조직과 그 아래 전부다(가정)
        return ctx["role"] == "manager" and post["target_org_id"] in (ctx.get("org_ids") or set())
    return True


def target_text(post: dict, org_text: dict) -> str:
    parts = []
    if post["target_org_id"] is not None:
        parts.append(f"{org_text.get(post['target_org_id'], '없는 조직')} · 그 아래 포함")
    if post["target_role"]:
        parts.append(ROLE_KO.get(post["target_role"], post["target_role"]))
    out = " · ".join(parts) if parts else "전체"
    if post["category"] == "plan":
        out += " · 설계사에게 보임" if post["fc_visible"] else " · 메인 관리자만"
    return out


def _rows(con, where="1=1", params=()) -> list:
    return [dict(r) for r in con.execute(
        f"""SELECT p.*, (SELECT COUNT(*) FROM board_file f WHERE f.post_id=p.post_id AND f.removed_at IS NULL) n_files
            FROM board_post p WHERE {where} ORDER BY p.posted_at DESC, p.post_id DESC""", params)]


def visible_posts(con, ctx, include_withdrawn=False) -> list:
    """이 사람에게 보이는 글. 메인 관리자도 내린 글은 include_withdrawn 일 때만 받는다."""
    up = viewer_orgs(con, ctx)
    rows = _rows(con) if include_withdrawn else _rows(con, "p.status='posted'")
    return [p for p in rows if can_see(ctx, p, up)]


def post(con, post_id) -> dict | None:
    rows = _rows(con, "p.post_id=?", (post_id,))
    return rows[0] if rows else None


def files(con, post_id, include_removed=False) -> list:
    extra = "" if include_removed else " AND removed_at IS NULL"
    return [dict(r) for r in con.execute(f"SELECT * FROM board_file WHERE post_id=?{extra} ORDER BY file_id", (post_id,))]


def file_row(con, file_id) -> dict | None:
    r = con.execute("SELECT * FROM board_file WHERE file_id=?", (file_id,)).fetchone()
    return dict(r) if r else None


def recent_notices(con, ctx, since: str) -> list:
    """알림함의 새 공지. 이 사람에게 보이는 게시 중 공지 가운데 since(YYYY-MM-DD) 이후 올린 것. 본인이 올린 것은 뺀다."""
    try:
        rows = _rows(con, "p.status='posted' AND p.category='notice' AND p.posted_at>=?", (since,))
    except sqlite3.OperationalError:                         # 표가 없는 오래된 DB
        con.rollback()
        return []
    up = viewer_orgs(con, ctx)
    return [p for p in rows if p["posted_by"] != ctx["login_id"] and can_see(ctx, p, up)]


# ---------------------------------------------------------------- 쓰기. 메인 관리자만 부른다. 커밋은 부르는 쪽이 한다
def clean_fields(con, form: dict) -> dict:
    """폼 값을 검사해 저장할 칸으로 바꾼다. 잘못되면 ValueError."""
    cat = (form.get("category") or "").strip()
    if cat not in CATEGORY_KO:
        raise ValueError("분류를 고르세요.")
    title = re.sub(r"\s+", " ", form.get("title") or "").strip()
    if not title:
        raise ValueError("제목을 적어 주세요.")
    if len(title) > TITLE_MAX:
        raise ValueError(f"제목은 {TITLE_MAX}자까지입니다.")
    body = (form.get("body") or "").replace("\r\n", "\n").strip()
    if len(body) > BODY_MAX:
        raise ValueError(f"본문은 {BODY_MAX}자까지입니다.")
    org_raw = str(form.get("target_org_id") or "").strip()
    org_id = None
    if org_raw:
        if not org_raw.isdigit() or not con.execute("SELECT 1 FROM org_unit WHERE org_id=?", (int(org_raw),)).fetchone():
            raise ValueError("대상 조직을 다시 고르세요.")
        org_id = int(org_raw)
    role = (form.get("target_role") or "").strip() or None
    if role is not None and role not in ROLE_KO:
        raise ValueError("대상 역할을 다시 고르세요.")
    fc = None
    if cat == "plan":
        v = str(form.get("fc_visible") or "")
        if v not in ("0", "1"):
            raise ValueError("시상안 원문은 설계사에게 보여도 되는 파일인지 골라 주세요.")
        fc = int(v)
    return {"category": cat, "title": title, "body": body or None, "target_org_id": org_id, "target_role": role, "fc_visible": fc}


def _snap(p: dict, names: list) -> dict:
    return {k: p.get(k) for k in ("category", "title", "target_org_id", "target_role", "fc_visible", "status")} | {"files": names}


def audit(con, by, action, target, before=None, after=None) -> None:
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), by, action, str(target),
                 json.dumps(before, ensure_ascii=False) if before is not None else None,
                 json.dumps(after, ensure_ascii=False) if after is not None else None))


def _add_files(con, post_id, by, uploads: list) -> list:
    now = db.now()
    out = []
    for name, kind, data in uploads:
        digest = save_blob(data)
        con.execute("""INSERT INTO board_file(post_id, name, size_bytes, sha256, kind, uploaded_by, uploaded_at)
                       VALUES(?,?,?,?,?,?,?)""", (post_id, name, len(data), digest, kind, by, now))
        out.append(name)
    return out


def create(con, by, fields: dict, uploads: list) -> int:
    """uploads 는 check_file 을 지난 (이름, 꼴, 내용) 목록이다."""
    if len(uploads) > MAX_FILES:
        raise ValueError(f"첨부는 한 글에 {MAX_FILES}개까지입니다.")
    cur = con.execute("""INSERT INTO board_post(category, title, body, target_org_id, target_role, fc_visible, status, posted_by, posted_at)
                         VALUES(?,?,?,?,?,?,'posted',?,?)""",
                      (fields["category"], fields["title"], fields["body"], fields["target_org_id"], fields["target_role"],
                       fields["fc_visible"], by, db.now()))
    pid = cur.lastrowid
    names = _add_files(con, pid, by, uploads)
    audit(con, by, "board_post", pid, after=_snap({**fields, "status": "posted"}, names))
    return pid


def update(con, post_id, by, fields: dict, uploads: list, remove_ids) -> None:
    before = post(con, post_id)
    if not before or before["status"] != "posted":
        raise ValueError("게시 중인 글이 아닙니다.")
    old = files(con, post_id)
    remove = {int(i) for i in remove_ids if str(i).isdigit()} & {f["file_id"] for f in old}
    if len(old) - len(remove) + len(uploads) > MAX_FILES:
        raise ValueError(f"첨부는 한 글에 {MAX_FILES}개까지입니다.")
    now = db.now()
    con.execute("""UPDATE board_post SET category=?, title=?, body=?, target_org_id=?, target_role=?, fc_visible=?, updated_by=?, updated_at=?
                   WHERE post_id=?""", (fields["category"], fields["title"], fields["body"], fields["target_org_id"], fields["target_role"],
                                        fields["fc_visible"], by, now, post_id))
    for fid in sorted(remove):
        con.execute("UPDATE board_file SET removed_by=?, removed_at=? WHERE file_id=?", (by, now, fid))
    _add_files(con, post_id, by, uploads)
    after_names = [f["name"] for f in files(con, post_id)]
    audit(con, by, "board_update", post_id, before=_snap(before, [f["name"] for f in old]), after=_snap({**fields, "status": "posted"}, after_names))


def withdraw(con, post_id, by) -> None:
    p = post(con, post_id)
    if not p or p["status"] != "posted":
        raise ValueError("게시 중인 글이 아닙니다.")
    con.execute("UPDATE board_post SET status='withdrawn', withdrawn_by=?, withdrawn_at=? WHERE post_id=?", (by, db.now(), post_id))
    audit(con, by, "board_withdraw", post_id, before={"status": "posted"}, after={"status": "withdrawn"})
