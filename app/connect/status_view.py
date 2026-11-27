"""연결 상태 화면용 줄 만들기. /data/jobs 의 연결 상태 구역이 쓴다.

StateStore 의 줄에 사람이 읽는 이름과 상태 글을 붙인다. 노션은 기록이 없어도 늘 한 줄을 보이고, 키가 없으면 키 없음으로 보인다.
이름과 금액은 담지 않는다. 읽기만 하고 연결 표를 고치지 않는다.
"""
from __future__ import annotations

import os

from app.connect.base import FAILED, NOT_CONFIGURED, OK
from app.connect.notion import CONN_NAME as NOTION
from app.connect.notion import ENV_TOKEN
from app.connect.state import StateStore

NO_KEY = "no_key"
NEVER = "never"
LABELS = {
    "upload": "파일 직접 올리기", "gdrive": "구글 드라이브", NOTION: "노션 시책·시상 정리본",
    "card_site.status": "명함 사이트 상태 읽기", "card_site.admin": "명함 사이트 관리자 읽기", "card_site.write": "명함 사이트 쓰기",
    "card_site.file": "명함 신청본 읽기", "card_site.codes": "수료 코드 기록 읽기",
}
STATE_KO = {OK: "성공", FAILED: "실패", NOT_CONFIGURED: "설정 안 됨", NO_KEY: "키 없음", NEVER: "아직 안 함"}
STATE_CHIP = {OK: "good", FAILED: "bad", NOT_CONFIGURED: "warn", NO_KEY: "warn", NEVER: ""}
ALWAYS = (NOTION,)


def _label(name: str) -> str:
    if name.startswith("folder:"):
        return "폴더 가져오기"
    return LABELS.get(name, name)


def _summary(name: str, detail) -> str:
    """결과 건수 한 줄. 노션만 푼다. 건수 말고는 담지 않는다."""
    if name != NOTION or not isinstance(detail, dict):
        return ""
    parts = [(k, detail.get(k)) for k in ("seen", "new", "changed", "unchanged", "failed", "gone")]
    ko = {"seen": "읽음", "new": "새 것", "changed": "바뀜", "unchanged": "그대로", "failed": "실패", "gone": "사라짐"}
    return ", ".join(f"{ko[k]} {v}" for k, v in parts if isinstance(v, int) and (v or k == "seen"))


def rows(con, env=None) -> list:
    """연결마다 한 줄. 키 없음과 아직 안 함은 기록이 없어도 보인다."""
    env = os.environ if env is None else env
    cur = StateStore(con=con).all()
    out = []
    for name in sorted(set(cur) | set(ALWAYS), key=lambda n: (n not in ALWAYS, n)):
        c = cur.get(name) or {}
        status = c.get("last_status") or NEVER
        reason = c.get("last_failure_reason") or ""
        if name == NOTION and not env.get(ENV_TOKEN):
            status, reason = NO_KEY, f"환경 변수 {ENV_TOKEN} 이 없습니다. 다른 기능은 그대로 돕니다"
        elif status == OK:
            reason = ""                      # 성공한 줄에 옛 실패 사유를 되풀이하지 않는다. 옛 사유는 마지막 실패 시각과 함께 남아 있다
        out.append({"name": name, "label": _label(name), "status": status, "status_ko": STATE_KO.get(status, status),
                    "chip": STATE_CHIP.get(status, ""), "last_attempt_at": c.get("last_attempt_at"),
                    "last_success_at": c.get("last_success_at"), "last_failure_at": c.get("last_failure_at"),
                    "reason": reason, "consecutive_failures": c.get("consecutive_failures", 0),
                    "summary": _summary(name, c.get("detail"))})
    return out
