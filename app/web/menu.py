"""config/menu.yaml 을 읽어 역할별 메뉴를 만든다. 메뉴와 권한 검사의 원본은 그 파일 하나다.

모듈: 기본 상품 core(프로모션 달성관리)와 추가기능 모듈이 있다. 꺼진 모듈의 항목은 for_ctx 와 탭에서 빠지고 guard 가 404 로 막는다.
켜고 끄기는 menu.yaml 의 modules.*.enabled 와 배포마다 정하는 환경 변수 APP_MODULES_OFF(쉼표로 모듈 키)다. core 는 끌 수 없다.
"""
import functools
import logging
import os

import yaml

from app import db

MENU_FILE = db.ROOT / "config" / "menu.yaml"
SCOPES = {"all": "전체", "org": "자기 조직", "self": "본인"}
PHASES = {1: "1단계 운영 기반", 2: "2단계 사람별 화면", 3: "3단계 제도와 제안"}
STATUS = {"built": "새 화면에 있음", "legacy": "기존 운영 도구에 있음", "planned": "기획"}
# 항목의 requires 값. 역할로 열린 항목이라도 로그인 문맥(auth.context)의 이 플래그가 참이어야 보이고 열린다.
# 플래그는 guard 와 메뉴가 함께 본다. 메인 관리자도 같은 규칙을 따르며 auth.context 가 메인 관리자에게는 늘 참으로 준다
REQUIRES = {"card_approver": "명함 승인 권한"}
CORE = "core"                                       # 기본 상품. 끌 수 없다
MODULES_ENV = "APP_MODULES_OFF"
TAB_MAX = 4                                         # 아래 탭의 칸 수. 그 뒤에 늘 더보기가 붙는다
log = logging.getLogger(__name__)


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
    mods = cfg.get("modules") or {}
    assert CORE in mods, "modules 에 core 가 있어야 한다"
    for g in cfg["groups"]:
        assert g.get("module") in mods, f"묶음의 module 오류: {g['id']}"
        for it in g["items"]:
            it["module"] = it.get("module") or g["module"]
            assert it["module"] in mods, f"항목의 module 오류: {it['id']}"
    items = {it["id"] for g in cfg["groups"] for it in g["items"]}
    tabs = cfg["mobile_tabs"]                           # 역할별 아래 탭. 항목 id 또는 {id, name}. 네 칸 뒤에 더보기가 붙는다
    assert isinstance(tabs, dict) and set(tabs) <= set(cfg["roles"]), "mobile_tabs 는 역할별로 적는다"
    for role, row in tabs.items():
        tabs[role] = [t if isinstance(t, dict) else {"id": t} for t in row]
        assert len(tabs[role]) <= TAB_MAX, f"mobile_tabs 는 {TAB_MAX}칸까지다: {role}"
        assert all(t["id"] in items for t in tabs[role]), f"mobile_tabs 는 있는 항목 id 로 적는다: {role}"
    return cfg


@functools.lru_cache(maxsize=8)
def _off_from(raw: str) -> frozenset:
    """APP_MODULES_OFF 의 값과 menu.yaml 의 enabled 로 꺼진 모듈을 정한다. 같은 값이면 경고를 한 번만 남긴다."""
    mods = load()["modules"]
    off = {k.strip() for k in raw.split(",") if k.strip()}
    off |= {k for k, m in mods.items() if not (m or {}).get("enabled", True)}
    if CORE in off:
        log.warning("기본 상품 모듈 core 는 끌 수 없어 켜 둡니다.")
        off.discard(CORE)
    unknown = off - set(mods)
    if unknown:
        log.warning("없는 모듈 키를 무시합니다: %s", ", ".join(sorted(unknown)))
    return frozenset(off & set(mods))


def modules_off() -> frozenset:
    """지금 꺼진 모듈 키. 요청마다 환경 변수를 다시 읽는다."""
    return _off_from(os.environ.get(MODULES_ENV, ""))


def module_on(key: str) -> bool:
    return key not in modules_off()


def item_on(item: dict) -> bool:
    """이 항목의 모듈이 켜져 있는가. guard 가 이 값으로 꺼진 모듈의 화면을 404 로 막는다."""
    return module_on(item["module"])


def path_on(path: str) -> bool:
    """메뉴에 있는 주소면 그 모듈이 켜져 있는가. 메뉴에 없는 주소는 참이다. 홈처럼 다른 화면을 가리키는 링크를 감출 때 쓴다."""
    it = find(path)
    return it is None or item_on(it)


def allowed(item: dict, ctx: dict) -> bool:
    """이 사람에게 이 항목이 열리는가. 역할이 맞고, requires 가 있으면 문맥의 그 플래그가 참이어야 한다."""
    if ctx["role"] not in item["roles"]:
        return False
    need = item.get("requires")
    return not need or bool(ctx.get(need))


def visible(item: dict, ctx: dict) -> bool:
    """메뉴와 링크에 보일 항목인가. 모듈이 켜져 있고 이 사람에게 열린다."""
    return item_on(item) and allowed(item, ctx)


def for_ctx(ctx: dict) -> list:
    """그 사람에게 보이는 묶음과 항목만 돌려준다. 꺼진 모듈의 항목은 뺀다. 화면의 메뉴는 이 함수를 쓴다."""
    out = []
    for g in load()["groups"]:
        items = [it for it in g["items"] if visible(it, ctx)]
        if items:
            out.append({**g, "items": items})
    return out


def for_role(role: str) -> list:
    """그 역할에게 역할만으로 열리는 묶음과 항목. requires 가 걸린 항목은 플래그를 모르므로 뺀다."""
    out = []
    for g in load()["groups"]:
        items = [it for it in g["items"] if role in it["roles"] and not it.get("requires") and item_on(it)]
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


def tabs_for(ctx: dict, groups: list | None = None) -> list:
    """그 사람의 아래 탭. menu.yaml 의 mobile_tabs 순서대로, 보이지 않거나 모듈이 꺼진 항목은 건너뛴다.

    돌려주는 칸은 항목에 탭 이름 label 을 더한 것이다. groups 를 주면 그 목록(for_ctx 의 결과)에서 찾는다.
    """
    shown = {it["id"]: it for g in (groups if groups is not None else for_ctx(ctx)) for it in g["items"]}
    out = []
    for t in load()["mobile_tabs"].get(ctx["role"], []):
        it = shown.get(t["id"])
        if it:
            out.append({**it, "label": t.get("name") or it["name"]})
    return out[:TAB_MAX]


NO_GROUP_TAB = {"common"}                           # 공통 묶음은 탭에 알림이 있어도 일정과 공지를 더보기에서 연다


def tab_of(item: dict | None, tabs: list):
    """지금 화면에서 켤 탭. 화면이 탭이면 그 탭, 아니면 같은 묶음의 첫 탭이다. 공통 묶음과 탭이 없는 묶음은 None 이고 더보기를 켠다."""
    if not item:
        return None
    for t in tabs:
        if t["id"] == item["id"]:
            return t
    if item["group"] in NO_GROUP_TAB:
        return None
    return next((t for t in tabs if t["group"] == item["group"]), None)


def find(path: str):
    for g in load()["groups"]:
        for it in g["items"]:
            if it["path"] == path:
                return it
    return None


def scope_of(item: dict, role: str):
    return item["roles"].get(role)
