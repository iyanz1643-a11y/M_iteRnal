"""영업 바탕의 공통 값과 개인정보 규칙.

| 무엇 | 규칙 |
| --- | --- |
| 표 | schema.sql 의 [sales:begin] 묶음. 오래된 DB 는 ensure 가 그 묶음만 돌린다 |
| 목록 | 이름은 원문을 볼 수 있는 사람에게만 원문, 그 밖에는 가린다. 연락처는 누구에게나 가운데를 가린다 |
| 상세 | 담당 설계사와 직속 관리자는 원문을 본다. 메인 관리자는 사유를 적은 뒤 본다. 위 조직의 관리자는 가린 채로 본다 |
| 기록 | 원문을 보인 일은 역할과 상관없이 audit_log 에 sales_contact_view 로 남긴다. 연락처 값은 남기지 않는다 |
| 직속 관리자 | 자료 기준 달에 담당 설계사가 속한 조직의 조직장과 그 달의 대리다. 계약 원장 D-05 와 같다 |

D-12 제안대로 둔 가정이다. 메인 관리자의 사유 조회는 docs/09 2.3 원칙 3 과 계약 원장 D-05 와 같은 방식이다.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date

from app import db
from app.engine import orgs
from app.ops import schema as ops_schema

BEGIN, END = "-- [sales:begin]", "-- [sales:end]"

REGIONS = ["서울", "경기", "인천", "부산", "대구", "광주", "대전", "울산", "세종", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"]
TIMES = {"weekday_am": "평일 오전", "weekday_pm": "평일 오후", "weekday_eve": "평일 저녁", "weekend": "주말", "any": "아무 때나"}
SOURCE_KO = {"landing": "상담 신청", "self": "직접 등록", "company": "회사 DB", "referral": "소개", "event": "행사"}
STATUS_KO = {"new": "미배정", "assigned": "배정", "working": "진행 중", "won": "체결", "lost": "종료"}
STATUS_TONE = {"new": "", "assigned": "blue", "working": "blue", "won": "good", "lost": ""}
OPEN = ("assigned", "working")                       # 담당이 들고 있는 DB
KIND_KO = {"call": "통화", "consult": "상담", "proposal": "제안", "application": "청약"}
KINDS = list(KIND_KO)                                 # 깔때기 순서
KIND_STATUS = {"call": "working", "consult": "working", "proposal": "working", "application": "working"}
HANDOVER_KO = {"pending": "준법 확인 대기", "confirmed": "준법 확인"}
CONSENT_VERSION = "draft-1"                           # 준법 확인 전 문구. 확인을 받으면 판을 올린다. D-29
NAME_MAX, MEMO_MAX, RESULT_MAX = 20, 200, 200
REASON_MIN, REASON_MAX = 5, 200

# 배정 규칙의 기본값. 조직별 설정에 DB 배정 항목이 아직 없어 환경 변수로만 바꾼다. 가정이다(docs/09 4.9)
DEFAULTS = {"hold": 30, "stale": 7, "warn": 5, "new_days": 3}
ENV = {"hold": "APP_SALES_HOLD", "stale": "APP_SALES_STALE_DAYS", "warn": "APP_SALES_WARN_DAYS", "new_days": "APP_SALES_NEW_DAYS"}


def setting(name: str) -> int:
    """hold 보유 한도, stale 방치로 보는 날 수, warn 방치 예고 날 수, new_days 새 배정 알림 날 수."""
    try:
        v = int(os.environ.get(ENV[name], "") or DEFAULTS[name])
    except ValueError:
        v = DEFAULTS[name]
    return max(v, 1)


def ensure(con) -> None:
    """영업 표가 없는 오래된 DB 에서 schema.sql 의 영업 묶음을 돌린다. 있으면 아무것도 하지 않는다."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_coaching'").fetchone():
        return
    ops_schema.run_block(con, BEGIN, END)


def today() -> date:
    return date.today()


# ---------------------------------------------------------------- 값 다듬기
PHONE_RE = re.compile(r"^01[016789][0-9]{7,8}$")


def clean_phone(raw) -> str | None:
    """휴대폰 번호를 세 마디 줄표 꼴로. 아니면 None."""
    digits = re.sub(r"[^0-9]", "", str(raw or ""))
    if not PHONE_RE.match(digits):
        return None
    return f"{digits[:3]}-{digits[3:-4]}-{digits[-4:]}"


def clean_name(raw) -> str | None:
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not (1 < len(s) <= NAME_MAX) or not re.fullmatch(r"[가-힣A-Za-z .]+", s):
        return None
    return s


def clean_text(raw, limit: int) -> str | None:
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    return s[:limit] or None


def clean_md(raw) -> str | None:
    """기념일 MM-DD. 날짜 칸의 YYYY-MM-DD 도 받는다. 틀리면 ValueError, 비면 None."""
    s = str(raw or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(?:\d{4}-)?(\d{1,2})-(\d{1,2})", s)
    if not m:
        raise ValueError("날짜를 다시 골라 주세요.")
    mm, dd = int(m.group(1)), int(m.group(2))
    try:
        date(2000, mm, dd)                               # 윤년이라 2월 29일도 받는다
    except ValueError:
        raise ValueError("날짜를 다시 골라 주세요.") from None
    return f"{mm:02d}-{dd:02d}"


def clean_date(raw, required=False) -> str | None:
    s = str(raw or "").strip()
    if not s:
        if required:
            raise ValueError("날짜를 골라 주세요.")
        return None
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        raise ValueError("날짜를 다시 골라 주세요.") from None


# ---------------------------------------------------------------- 가리기
def mask_name(name) -> str:
    """홍길동은 홍*동, 남궁민수는 남**수, 두 글자는 홍* 다. 계약 원장의 가리기와 같다."""
    s = re.sub(r"\s+", "", str(name or ""))
    if not s:
        return "-"
    if len(s) <= 2:
        return s[0] + "*" * (len(s) - 1) if len(s) > 1 else "*"
    return s[0] + "*" * (len(s) - 2) + s[-1]


def mask_phone(phone) -> str:
    """가운데 자리를 가린다. 끝 네 자리만 남긴다."""
    digits = re.sub(r"[^0-9]", "", str(phone or ""))
    if len(digits) < 8:
        return "-"
    return f"{digits[:3]}-****-{digits[-4:]}"


# ---------------------------------------------------------------- 원문을 보는 규칙
def home_org(con, agent_id, ym) -> int | None:
    r = con.execute("""SELECT org_id FROM agent_org_history WHERE agent_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)
                       ORDER BY effective_from DESC LIMIT 1""", (agent_id, ym, ym)).fetchone()
    return r["org_id"] if r else None


def last_org(con, agent_id) -> int | None:
    """가장 최근 소속. 해촉자처럼 지금 소속이 없는 사람에게 쓴다."""
    r = con.execute("SELECT org_id FROM agent_org_history WHERE agent_id=? ORDER BY effective_from DESC, id DESC LIMIT 1", (agent_id,)).fetchone()
    return r["org_id"] if r else None


def heads_of(con, org_id, ym, day: date | None = None) -> set:
    """그 조직의 조직장(자료 기준 달)과 대리(오늘)."""
    if org_id is None:
        return set()
    out = {r["agent_id"] for r in con.execute(
        "SELECT agent_id FROM org_manager WHERE org_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)", (org_id, ym, ym))}
    d = (day or today()).isoformat()
    out |= {r["agent_id"] for r in con.execute(
        "SELECT agent_id FROM org_deputy WHERE org_id=? AND ended_at IS NULL AND date_from<=? AND date_to>=?", (org_id, d, d))}
    return out


def direct_heads(con, agent_id, ym) -> set:
    """담당 설계사의 직속 관리자. 계약 원장 D-05 의 직속 관리자와 같은 뜻이라 engine.orgs.direct_heads 를 부른다. 본인은 뺀다."""
    if not agent_id:
        return set()
    return orgs.direct_heads(con, agent_id, ym) - {agent_id}


def access(con, ctx, agent_id) -> str:
    """이 사람이 이 담당의 DB 와 고객의 원문을 어떻게 보는가.

    own 담당 본인, head 직속 관리자, reason 메인 관리자는 사유를 적고, masked 가림.
    """
    me = ctx.get("agent_id")
    if me and agent_id == me:
        return "own"
    if me and agent_id and ctx["role"] == "manager" and me in direct_heads(con, agent_id, ctx["ym"]):
        return "head"
    if ctx["role"] == "main":
        return "reason"
    return "masked"


def log_view(con, ctx, target: str, why: str, reason: str | None = None) -> None:
    """원문을 보인 일. 커밋한다. 이름과 연락처는 남기지 않는다."""
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (db.now(), ctx["login_id"], "sales_contact_view", target,
                 json.dumps({"why": why, **({"reason": reason} if reason else {})}, ensure_ascii=False)))
    con.commit()


def audit(con, by: str, action: str, target: str, after: dict | None = None) -> None:
    """영업 쓰기의 변경 기록. 번호와 상태만 남기고 이름과 연락처는 남기지 않는다. 커밋은 부르는 쪽이 한다."""
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (db.now(), by, action, target, json.dumps(after, ensure_ascii=False) if after is not None else None))


def clean_reason(raw) -> str | None:
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    return s if REASON_MIN <= len(s) <= REASON_MAX else None
