"""config/org_levels.yaml 을 읽는다. 계층의 수와 이름, 조직장 역할, 파일 열과 계층의 짝은 그 파일 하나가 원본이다."""
import functools

import yaml

from app import db

LEVELS_FILE = db.ROOT / "config" / "org_levels.yaml"
DEPUTY = "deputy"                      # 기간제 대리. org_manager 가 아니라 org_deputy 에 둔다


@functools.lru_cache(maxsize=1)
def load() -> dict:
    cfg = yaml.safe_load(LEVELS_FILE.read_text(encoding="utf-8"))
    codes = [lv["code"] for lv in cfg["levels"]]
    roles = [lv["head_role"] for lv in cfg["levels"] if lv.get("head_role")]
    assert len(set(codes)) == len(codes) and len(set(roles)) == len(roles), "계층 코드나 조직장 역할이 겹칩니다"
    assert not cfg["levels"][0].get("head_role"), "맨 위 계층은 하나뿐인 뿌리라 조직장이 없습니다"
    for src, cols in cfg["sources"].items():
        assert set(cols.values()) <= set(codes), f"{src}: 없는 계층"
    assert set(cfg["positions"].values()) <= set(roles), "직위가 없는 조직장 역할을 가리킵니다"
    return cfg


def codes() -> list:
    return [lv["code"] for lv in load()["levels"]]


def root_code() -> str:
    return codes()[0]


def depth(level: str) -> int:
    """위에서부터의 순서. 모르는 계층은 맨 아래 뒤로 보낸다."""
    c = codes()
    return c.index(level) if level in c else len(c)


def level_ko() -> dict:
    return {lv["code"]: lv["name"] for lv in load()["levels"]}


def head_ko() -> dict:
    out = {lv["head_role"]: lv["head_name"] for lv in load()["levels"] if lv.get("head_role")}
    out[DEPUTY] = "대리"
    return out


def head_role(level: str):
    return next((lv.get("head_role") for lv in load()["levels"] if lv["code"] == level), None)


def level_of_role(role: str):
    return next((lv["code"] for lv in load()["levels"] if lv.get("head_role") == role), None)


def source(name: str) -> dict:
    """{파일 열: 계층}. 계층 순서대로."""
    cols = load()["sources"][name]
    return dict(sorted(cols.items(), key=lambda kv: depth(kv[1])))


def position_role(position):
    return load()["positions"].get((position or "").strip())
