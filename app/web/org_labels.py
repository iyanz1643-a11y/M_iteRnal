"""화면의 조직 계층 표시 이름. 원본은 config/org_labels.yaml 이고, 없는 조직은 config/org_levels.yaml 의 이름을 쓴다.

내부 계층 코드는 바꾸지 않는다. 화면이 조직 이름 옆에 붙이는 계층 이름과 조직 자체의 표시 이름만 바꾼다.
조직은 path_key 로 고른다. 같은 이름의 조직이 여러 부모 아래에 있을 수 있기 때문이다.
"""
import functools

import yaml

from app import db
from app.org import levels

LABELS_FILE = db.ROOT / "config" / "org_labels.yaml"
SEP = " > "


@functools.lru_cache(maxsize=1)
def rules() -> list:
    if not LABELS_FILE.exists():
        return []
    cfg = yaml.safe_load(LABELS_FILE.read_text(encoding="utf-8")) or {}
    out = []
    for r in cfg.get("rules") or []:
        if r.get("under"):
            out.append({"under": str(r["under"]), "title": r.get("title"), "depth_labels": list(r.get("depth_labels") or []),
                        "start": bool(r.get("start"))})
    return out


def _depth(path_key: str) -> int:
    return 0 if not path_key or SEP not in path_key and ":" not in path_key else path_key.count(SEP) + 1


def level_name(org: dict) -> str:
    """조직 하나의 계층 표시 이름. org 는 level 과 path_key 를 가진 dict 다."""
    key = org.get("path_key") or ""
    for r in rules():
        if key.startswith(r["under"] + SEP):
            i = _depth(key) - _depth(r["under"]) - 1
            if 0 <= i < len(r["depth_labels"]):
                return r["depth_labels"][i]
            break
    return levels.level_ko().get(org.get("level"), org.get("level") or "")


def title(org: dict) -> str:
    """조직 하나의 표시 이름. 규칙의 title 이 있으면 그것, 아니면 조직 이름이다."""
    key = org.get("path_key") or ""
    for r in rules():
        if key == r["under"] and r["title"]:
            return r["title"]
    return org.get("name") or ""


def start_path(con) -> str | None:
    """메인 관리자의 조직별 진척 시작 조직. 규칙에 start 가 있고 그 조직이 DB 에 있으면 그 path_key, 아니면 None."""
    for r in rules():
        if r["start"] and con.execute("SELECT 1 FROM org_unit WHERE path_key=?", (r["under"],)).fetchone():
            return r["under"]
    return None
