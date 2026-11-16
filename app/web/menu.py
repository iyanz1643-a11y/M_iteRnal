"""config/menu.yaml 을 읽어 역할별 메뉴를 만든다. 메뉴와 권한 검사의 원본은 그 파일 하나다."""
import functools

import yaml

from app import db

MENU_FILE = db.ROOT / "config" / "menu.yaml"
SCOPES = {"all": "전체", "org": "자기 조직", "self": "본인"}
PHASES = {1: "1단계 운영 기반", 2: "2단계 사람별 화면", 3: "3단계 제도와 제안"}
STATUS = {"built": "새 화면에 있음", "legacy": "기존 운영 도구에 있음", "planned": "기획"}
# 항목의 requires 값. 역할로 열린 항목이라도 로그인 문맥(auth.context)의 이 플래그가 참이어야 보이고 열린다.
# 플래그는 guard 와 메뉴가 함께 본다. 메인 관리자도 같은 규칙을 따르며 auth.context 가 메인 관리자에게는 늘 참으로 준다
REQUIRES = {"card_approver": "명함 승인 권한"}


@functools.lru_cache(maxsize=1)
def load() -> dict:
    cfg = yaml.safe_load(MENU_FILE.read_text(encoding="utf-8"))
    seen_id, seen_path = set(), set()
    for g in cfg["groups"]:
        for it in g["items"]:
            assert it["id"] not in seen_id and it["path"] not in seen_path, f"메뉴 중복: {it['id']} {it['path']}"
            assert set(it["roles"]) <= set(cfg["roles"]) and set(it["roles"].values()) <= set(SCOPES), f"역할 오류: {it['id']}"
            assert it["phase"] in PHASES and it["status"] in STATUS, f"단계·상태 오류: {it['id']}"
            assert it.get("requires") in (None, *REQUIRES), f"requires 오류: {it['id']}"
            seen_id.add(it["id"])
            seen_path.add(it["path"])
            it["group"] = g["id"]
    tabs = cfg["mobile_tabs"]                           # 역할별 아래 탭. 네 칸 뒤에 더보기가 붙는다
    gids = {g["id"] for g in cfg["groups"]}
    assert isinstance(tabs, dict) and set(tabs) <= set(cfg["roles"]), "mobile_tabs 는 역할별로 적는다"
    assert all(len(v) <= 4 and set(v) <= gids for v in tabs.values()), "mobile_tabs 는 있는 묶음 네 칸까지다"
    return cfg


def allowed(item: dict, ctx: dict) -> bool:
    """이 사람에게 이 항목이 열리는가. 역할이 맞고, requires 가 있으면 문맥의 그 플래그가 참이어야 한다."""
    if ctx["role"] not in item["roles"]:
        return False
    need = item.get("requires")
    return not need or bool(ctx.get(need))


def for_ctx(ctx: dict) -> list:
    """그 사람에게 보이는 묶음과 항목만 돌려준다. 화면의 메뉴는 이 함수를 쓴다."""
    out = []
    for g in load()["groups"]:
        items = [it for it in g["items"] if allowed(it, ctx)]
        if items:
            out.append({**g, "items": items})
    return out


def for_role(role: str) -> list:
    """그 역할에게 역할만으로 열리는 묶음과 항목. requires 가 걸린 항목은 플래그를 모르므로 뺀다."""
    out = []
    for g in load()["groups"]:
        items = [it for it in g["items"] if role in it["roles"] and not it.get("requires")]
        if items:
            out.append({**g, "items": items})
    return out


def entry(group: dict) -> str:
    """묶음을 누르면 갈 화면. 그 사람에게 보이는 항목 가운데 만들어진(built) 첫 화면이고, 없으면 첫 항목이다.

    menu.yaml 의 순서는 기획의 순서라 그대로 두고, 위 메뉴와 아래 탭이 아직 만들지 않은 안내 화면으로 먼저 가지 않게 한다.
    group 은 for_ctx 가 돌려준 묶음이라 이미 권한으로 걸러져 있다.
    """
    items = group["items"]
    return next((it["path"] for it in items if it["status"] == "built"), items[0]["path"])


def find(path: str):
    for g in load()["groups"]:
        for it in g["items"]:
            if it["path"] == path:
                return it
    return None


def scope_of(item: dict, role: str):
    return item["roles"].get(role)
