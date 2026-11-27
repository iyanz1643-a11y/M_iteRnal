"""화면의 조직 계층 표시 이름. 원본은 config/org_labels.yaml 이고, 없는 조직은 config/org_levels.yaml 의 이름을 쓴다.

내부 계층 코드는 바꾸지 않는다. 화면이 조직 이름 옆에 붙이는 계층 이름과 조직 자체의 표시 이름만 바꾼다.
조직은 path_key 로 고른다. 같은 이름의 조직이 여러 부모 아래에 있을 수 있기 때문이다.

화면에 조직 이름과 계층 이름과 선택 상자를 내는 곳은 모두 이 파일의 함수를 거친다. 템플릿이 LEVEL_KO 로 직접 찍지 않는다.
- texts(con)    조직 이름 글 {org_id: 글}. 시작 조직 아래는 시작 조직 바로 아래부터 이은 경로(본부 이름이 앞에 붙어 같은 이름의 지점이 구별된다)
- choose(con, rows)  선택 상자의 줄. 시작 조직의 위쪽 조직은 뺀다. 줄마다 label, indent, text 를 붙인다
- chip(org)     계층 이름 알약. 시작 조직 자체는 비어 있다
- hidden_ids(con)  시작 조직의 위쪽 조직. 화면에서만 숨긴다. 권한과 범위 검사는 그대로이고 주소로 직접 열어도 guard 를 거친다
규칙이 없거나 규칙의 조직이 DB 에 없으면 모든 함수가 예전 화면과 같은 값을 낸다.
"""
import functools

import yaml

from app import db
from app.org import levels

LABELS_FILE = db.ROOT / "config" / "org_labels.yaml"
SEP = " > "
HIDDEN_TEXT = "위 조직"            # 시작 조직의 위쪽 조직을 이름 대신 가리킬 때


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


# ---------------------------------------------------------------- 화면에 보이는 조직 글
def chip(org: dict) -> str:
    """계층 이름 알약의 글. 규칙의 조직 자체(이터널총괄본부)는 계층 이름이 없어 빈 글이다."""
    key = org.get("path_key") or ""
    if any(key == r["under"] for r in rules()):
        return ""
    return level_name(org)


def view(con) -> dict:
    """{org_id: {org_id, parent_id, name, level, path_key, hidden, under, title, chip, text}}. 조직 전체를 한 번 읽어 만든다.

    hidden: 시작 조직의 위쪽 조직이다. under: 시작 조직 자체이거나 그 아래다.
    text: 시작 조직 아래는 시작 조직 바로 아래부터의 경로, 그 밖은 뿌리를 뺀 경로(예전 org_texts 와 같다), 숨긴 조직은 HIDDEN_TEXT."""
    rows = {r["org_id"]: dict(r) for r in con.execute("SELECT org_id, parent_id, name, level, path_key FROM org_unit")}
    start_key = start_path(con)
    start = next((o for o in rows.values() if o["path_key"] == start_key), None) if start_key else None
    hidden = set()
    if start:
        up = start["parent_id"]
        while up in rows and up not in hidden:
            hidden.add(up)
            up = rows[up]["parent_id"]
    root = levels.root_code()
    out = {}
    for oid, o in rows.items():
        chain, cur, seen = [], o, set()
        while cur is not None and cur["org_id"] not in seen:
            seen.add(cur["org_id"])
            chain.append(cur)
            cur = rows.get(cur["parent_id"])
        chain.reverse()
        under = bool(start) and any(c["org_id"] == start["org_id"] for c in chain)
        if under:
            i = next(i for i, c in enumerate(chain) if c["org_id"] == start["org_id"])
            names = chain[i:] if o is start else chain[i + 1:]
        else:
            names = [c for c in chain if c["level"] != root and c["org_id"] not in hidden]
        out[oid] = {**o, "hidden": oid in hidden, "under": under, "title": title(o), "chip": chip(o),
                    "text": HIDDEN_TEXT if oid in hidden else (SEP.join(title(c) for c in names) or title(o))}
    return out


def head_name(org_id, role, v) -> str:
    """조직장 역할의 화면 이름. v 는 view(con) 의 결과다. 시작 조직 아래는 계층 이름에 '장'을 붙인다(본부장, 지점장).
    시작 조직 자체는 조직장이라고만 쓴다. 규칙이 없거나 대리이면 org_levels.yaml 의 이름이다."""
    base = levels.head_ko().get(role, role)
    x = v.get(org_id)
    if role == levels.DEPUTY or not x or not x["under"]:
        return base
    return x["chip"] + "장" if x["chip"] else "조직장"


def active(con) -> bool:
    """시작 조직이 있어 위쪽 조직을 숨기는 DB 인가."""
    return start_path(con) is not None


def hidden_ids(con) -> set:
    """시작 조직의 위쪽 조직 id. 규칙이 없으면 빈 집합이다."""
    return {oid for oid, v in view(con).items() if v["hidden"]}


def start_org_id(con):
    """시작 조직의 org_id. 없으면 None."""
    key = start_path(con)
    row = con.execute("SELECT org_id FROM org_unit WHERE path_key=?", (key,)).fetchone() if key else None
    return row["org_id"] if row else None


def texts(con) -> dict:
    """{org_id: 화면에 낼 조직 글}. app.org.sync.org_texts 를 대신한다. 규칙이 없으면 그것과 같다."""
    return {oid: v["text"] for oid, v in view(con).items()}


def shown(con, org) -> str:
    """조직 하나의 화면 글. org 는 org_id 이거나 org_id 를 가진 dict 다."""
    oid = org if isinstance(org, int) else org.get("org_id")
    v = view(con).get(oid)
    if v:
        return v["text"] if v["under"] or v["hidden"] else title(v)
    return (org.get("name") if isinstance(org, dict) else "") or ""


def choose(con, rows, keep=()) -> list:
    """선택 상자에 낼 조직 줄. 줄은 org_id 를 가진 dict 다(level, depth 는 있으면 쓴다).

    시작 조직의 위쪽 조직은 뺀다. 지금 고른 조직(keep)은 숨긴 조직이어도 남긴다.
    줄마다 label(이름과 계층 알약), indent, text 를 붙인다. 시작 조직 아래는 본부 이름이 앞에 붙은 경로이고 들여쓰기가 없다.
    그 밖은 예전처럼 이름이고 계층 깊이만큼 들여쓴다."""
    v = view(con)
    out = []
    for r in rows:
        x = v.get(r["org_id"])
        if x is None:
            out.append({**r, "text": r.get("name") or "", "label": r.get("name") or "", "indent": ""})
            continue
        if x["hidden"] and r["org_id"] not in keep:
            continue
        text = x["text"] if (x["under"] or x["hidden"]) else x["title"]
        indent = "" if (x["under"] or x["hidden"]) else "\u3000" * int(r.get("depth") or 0)
        out.append({**r, "text": text, "indent": indent, "label": f"{text} · {x['chip']}" if x["chip"] else text})
    return out


def options(con, keep=()) -> list:
    """[(org_id, 글)] 이름 순. 조직을 고르는 단순한 상자용이다. 숨긴 조직은 keep 에 든 것만 남긴다."""
    v = view(con)
    return sorted(((oid, x["text"]) for oid, x in v.items() if not x["hidden"] or oid in keep), key=lambda t: t[1])


def level_names(con, codes) -> dict:
    """{계층 코드: 화면 계층 이름}. 시작 조직 아래에서는 그 계층 조직이 쓰는 이름(본부, 지점)이고 없으면 org_levels.yaml 의 이름이다."""
    ko = dict(levels.level_ko())
    if not active(con):
        return {c: ko.get(c, c) for c in codes}
    shown = sorted((x for x in view(con).values() if not x["hidden"] and x["chip"]), key=lambda x: (not x["under"], x["org_id"]))
    out = {}
    for c in codes:
        first = next((x for x in shown if x["level"] == c), None)
        out[c] = first["chip"] if first else ko.get(c, c)
    return out


def visible_levels(con, codes) -> list:
    """고를 수 있는 계층 코드. 시작 조직이 있으면 조직이 실제로 있는 계층만 남긴다(숨긴 위쪽 조직과 시작 조직 자체의 계층은 뺀다)."""
    if not active(con):
        return list(codes)
    have = {x["level"] for x in view(con).values() if not x["hidden"] and x["chip"]}
    return [c for c in codes if c in have]
