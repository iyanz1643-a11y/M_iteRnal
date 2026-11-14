"""명함 승인과 반려의 판단. D-44 (2026-11-14): 메인 관리자와 관리자가 승인과 반려를 한다.

- 관리자는 본부장, 지점장, 센터장, 팀장과 그 대리다. 자기 범위 안 설계사에 이어진 명함만 다룬다.
- 범위는 화면의 guard 가 준 설계사 목록(scope_agent_ids)으로만 판단한다. 이 파일은 조직 트리를 다시 읽지 않는다.
- 두 번째 잠금으로 actor 계정이 활성이고, 메인 관리자이거나 지금 조직장 또는 대리인지 DB 에서 한 번 더 본다.
- card_link 로 이어지지 않은 주소는 메인 관리자만 다룬다.
- 내리기와 수료 코드 발급은 여기 없다. 메인 관리자만 하는 일이고 이번에 만들지 않았다.

같은 요청을 두 번 보내지 않는 장치
1. 클라이언트는 POST 를 자동으로 다시 보내지 않는다.
2. 이 프로세스 안에서 같은 주소의 판단은 한 번에 하나만 돈다. 겹치면 in_flight 로 돌려준다.
3. 보내기 전에 공개 상태를 읽어 대기가 아니면 보내지 않는다.
4. 같은 신청(주소와 신청 시각)에 이미 성공한 판단이 audit_log 에 있으면 보내지 않는다.

변경 기록 audit_log
- action 은 card_approve 나 card_reject, target 은 agent:ID. 이어지지 않은 주소면 agent:none 이다.
- after_json 에는 주소, 결과, 신청 시각, 범위 종류, 반려 사유의 길이만 둔다. 이름, 연락처, 사유 원문은 두지 않는다.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Iterable

from app import db
from app.connect import tables
from app.connect.base import FAILED, NOT_CONFIGURED, OK, Outcome
from app.connect.card_site import NO_PENDING, REASON_MAX, SLUG_RE, STATE_KO, WRITE_ACTIONS, WRITE_OFF, CardEntry, CardSiteClient, CardStatus

# 판단 결과 상태. card_site 의 OK, NO_PENDING, WRITE_OFF, NOT_CONFIGURED, FAILED 와 함께 쓴다
DENIED = "denied"                 # 권한이나 범위 밖, 사유에 개인정보 꼴
ALREADY = "already_done"          # 같은 신청을 이미 처리했다. 보내지 않았다
IN_FLIGHT = "in_flight"           # 같은 주소를 지금 처리하는 중이다. 보내지 않았다

DECIDE_KO = {
    OK: "처리함", DENIED: "권한 없음", ALREADY: "이미 처리함", IN_FLIGHT: "처리 중", NO_PENDING: "대기 없음",
    WRITE_OFF: "쓰기 꺼짐", NOT_CONFIGURED: "설정 안 됨", FAILED: "실패",
}
ACTION_KO = {"approve": "승인", "reject": "반려"}
AUDIT_ACTION = {"approve": "card_approve", "reject": "card_reject"}

# 반려 사유에 있으면 거부하는 꼴. 사유는 명함 사이트로 나가므로 연락처와 주민번호를 막는다
_PII = (
    re.compile(r"(?<!\d)(?:\+?82[-.\s]?)?0?1[016789][-.\s)]*\d{3,4}[-.\s]*\d{4}(?!\d)"),   # 휴대폰
    re.compile(r"(?<!\d)0\d{1,2}[-.\s)]+\d{3,4}[-.\s]*\d{4}(?!\d)"),                        # 지역 번호
    re.compile(r"(?<!\d)\d{6}\s*-?\s*[1-8]\d{6}(?!\d)"),                                     # 주민번호, 외국인등록번호
    re.compile(r"(?<!\d)\d{10,}(?!\d)"),                                                     # 붙여 쓴 긴 숫자
)

_LOCK = threading.Lock()
_INFLIGHT: set = set()


@dataclass(frozen=True)
class CardDecision:
    """판단 한 번의 결과. 화면에 그대로 보여도 되는 값만 담는다."""
    slug: str
    action: str                   # approve / reject
    agent_id: int | None          # 이어진 설계사. 없으면 None
    result: str                   # 위 상태 값
    decided_at: str | None = None
    reason_len: int = 0           # 보낸 반려 사유의 길이. 원문은 담지 않는다

    @property
    def result_ko(self) -> str:
        return DECIDE_KO.get(self.result, self.result)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["result_ko"] = self.result_ko
        d["action_ko"] = ACTION_KO.get(self.action, self.action)
        return d


@dataclass(frozen=True)
class PendingCard:
    """범위 안 승인 대기 한 줄. 이름과 연락처는 없다."""
    slug: str
    agent_id: int | None          # 이어진 설계사. 이어지지 않았으면 None
    live: bool                    # 지금 공개 중인 명함이 있는가
    requested_at: str | None
    replaces_live: bool           # 공개 중인 명함을 바꾸려는 신청인가
    conflict: bool                # 공개 중인 명함과 신청자 정보가 다르다고 사이트가 판정했는가
    live_url: str | None = None   # 공개 중이면 지금 명함 주소

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ 반려 사유
def clean_reason(reason) -> tuple[str, str | None]:
    """반려 사유를 다듬는다. 돌려주는 값은 (다듬은 사유, 거부 사유). 거부 사유가 있으면 보내지 않는다."""
    text = str(reason or "").strip()
    if any(p.search(text) for p in _PII):
        return "", "반려 사유에 전화번호나 주민번호 꼴이 있어 보내지 않았습니다. 지우고 다시 적어 주세요"
    return text[:REASON_MAX], None


# ------------------------------------------------------------------ 두 번째 잠금
def _current_ym(con) -> str:
    """app/web/auth.py 의 current_ym 과 같은 규칙. 연결 어댑터가 화면 모듈을 부르지 않도록 여기 둔다."""
    r = con.execute("SELECT MAX(period_ym) FROM calc_run WHERE status='done'").fetchone()
    return (r[0] if r else None) or datetime.now().strftime("%Y%m")


def actor_allowed(con, actor: str, is_main: bool, today: date | None = None) -> tuple[bool, str]:
    """actor 계정이 활성이고, 메인 관리자이거나 지금 조직장 또는 대리인지 DB 에서 본다.
    is_main 이 참이면 DB 에서도 메인 관리자여야 한다. 범위는 여기서 보지 않는다."""
    u = con.execute("SELECT agent_id, is_main_admin FROM user_account WHERE login_id=? AND status='active'", (str(actor or ""),)).fetchone()
    if u is None:
        return False, "활성 계정이 아니라 처리하지 않았습니다"
    agent_id, main = u[0], bool(u[1])
    if main:
        return True, ""
    if is_main:
        return False, "메인 관리자 계정이 아니라 처리하지 않았습니다"
    if agent_id is None:
        return False, "조직장이나 대리가 아니라 처리하지 않았습니다"
    ym = _current_ym(con)
    head = con.execute("SELECT 1 FROM org_manager WHERE agent_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?) LIMIT 1",
                       (agent_id, ym, ym)).fetchone()
    if head is None:
        d = (today or date.today()).isoformat()
        head = con.execute("SELECT 1 FROM org_deputy WHERE agent_id=? AND ended_at IS NULL AND date_from<=? AND date_to>=? LIMIT 1",
                           (agent_id, d, d)).fetchone()
    if head is None:
        return False, "조직장이나 대리가 아니라 처리하지 않았습니다"
    return True, ""


# ------------------------------------------------------------------ 변경 기록
def _audit(con, actor, action, slug, agent_id, result, *, requested_at=None, scope=None, reason_len=None) -> None:
    after = {"slug": slug, "result": result}
    if requested_at:
        after["requested_at"] = requested_at
    if scope:
        after["scope"] = scope
    if reason_len is not None:
        after["reason_len"] = reason_len
    with tables.write(con, "card_decide"):
        con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                    (db.now(), str(actor or "")[:64], AUDIT_ACTION.get(action, "card_decide"),
                     f"agent:{agent_id if agent_id is not None else 'none'}", None, json.dumps(after, ensure_ascii=False)))


def _already_done(con, slug: str, requested_at: str | None) -> bool:
    """같은 신청(주소와 신청 시각)에 성공한 승인이나 반려가 이미 있는가."""
    if not requested_at:
        return False
    r = con.execute(
        """SELECT 1 FROM audit_log WHERE action IN ('card_approve', 'card_reject')
             AND json_extract(after_json, '$.slug')=? AND json_extract(after_json, '$.requested_at')=?
             AND json_extract(after_json, '$.result')=? LIMIT 1""", (slug, requested_at, OK)).fetchone()
    return r is not None


def _links(con, link_table):
    if link_table is not None:
        return link_table
    from app.connect.card_link import CardLinkTable
    return CardLinkTable(con)


# ------------------------------------------------------------------ 판단
def decide_card(con, client: CardSiteClient, *, slug: str, action: str, actor: str, scope_agent_ids: Iterable,
                is_main: bool, reason: str = "", link_table=None, today: date | None = None) -> Outcome:
    """명함 승인이나 반려를 판단하고, 허용되면 명함 사이트에 한 번 보낸다.

    돌려주는 값은 Outcome(status, value=CardDecision, reason). 예외를 올리지 않는다.
    status: ok / denied / already_done / in_flight / no_pending / write_off / not_configured / failed
    reason 은 화면에 그대로 보여도 되는 말이다. 반려 사유 원문은 담지 않는다."""
    action = str(action or "").strip().lower()
    if action not in WRITE_ACTIONS:
        return Outcome(FAILED, reason="승인이나 반려만 할 수 있습니다")
    slug = str(slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        return Outcome(FAILED, reason="명함 주소는 영문 소문자, 숫자, 하이픈 2자에서 30자입니다")
    links = _links(con, link_table)
    agent_id = links.agent_for(slug)
    scope = "main" if is_main else "manager"

    def done(status, why="", *, requested_at=None, decided_at=None, reason_len=None, log=True) -> Outcome:
        if log:
            _audit(con, actor, action, slug, agent_id, status, requested_at=requested_at, scope=scope, reason_len=reason_len)
        dec = CardDecision(slug=slug, action=action, agent_id=agent_id, result=status, decided_at=decided_at, reason_len=reason_len or 0)
        return Outcome(status, value=dec, reason=why, attempts=1 if status in (OK, NO_PENDING, FAILED) else 0)

    # 반려 사유
    text, bad = ("", None)
    if action == "reject":
        text, bad = clean_reason(reason)
        if bad:
            return done(DENIED, bad, reason_len=len(str(reason or "").strip()))
    rlen = len(text) if action == "reject" else None

    # 두 번째 잠금과 범위
    ok, why = actor_allowed(con, actor, is_main, today=today)
    if not ok:
        return done(DENIED, why, reason_len=rlen)
    if agent_id is None and not is_main:
        return done(DENIED, "설계사와 이어지지 않은 명함은 메인 관리자만 처리합니다", reason_len=rlen)
    if not is_main and agent_id not in set(scope_agent_ids or ()):
        return done(DENIED, "범위 밖 설계사의 명함입니다", reason_len=rlen)

    # 쓰기 스위치와 설정. 네트워크에 나가기 전에 본다
    if not client.write_enabled:
        out = client.approve(slug) if action == "approve" else client.reject(slug, text)   # 보내지 않고 상태 기록만 남긴다
        return done(WRITE_OFF, out.reason, reason_len=rlen)
    if not client.admin_configured:
        out = client.approve(slug) if action == "approve" else client.reject(slug, text)
        return done(NOT_CONFIGURED, out.reason, reason_len=rlen)

    # 같은 주소는 한 번에 하나만
    with _LOCK:
        if slug in _INFLIGHT:
            return done(IN_FLIGHT, "같은 명함을 지금 처리하고 있습니다. 잠시 뒤 상태를 확인해 주세요", reason_len=rlen)
        _INFLIGHT.add(slug)
    try:
        pre = client.status(slug)
        if pre.status != OK or pre.value is None:
            return done(FAILED if pre.status != NOT_CONFIGURED else NOT_CONFIGURED,
                        f"명함 사이트의 상태를 읽지 못해 보내지 않았습니다. {pre.reason}".strip()[:300], reason_len=rlen)
        cur: CardStatus = pre.value
        _record_check(links, slug, cur)
        if cur.status != "pending":
            return done(NO_PENDING, f"대기 중인 요청이 없어 보내지 않았습니다. 지금 상태는 {STATE_KO.get(cur.status, cur.status)} 입니다",
                        requested_at=cur.requested_at, reason_len=rlen)
        if _already_done(con, slug, cur.requested_at):
            return done(ALREADY, "이 신청은 이미 처리했습니다. 명함 사이트에 다시 보내지 않았습니다", requested_at=cur.requested_at, reason_len=rlen)

        out = client.approve(slug) if action == "approve" else client.reject(slug, text)
        if out.status == OK:
            decided_at = out.value.decided_at if out.value else None
            after = CardStatus(slug=slug, live=True if action == "approve" else cur.live,
                               status="approved" if action == "approve" else "rejected",
                               live_at=(decided_at or cur.live_at) if action == "approve" else cur.live_at,
                               requested_at=cur.requested_at, decided_at=decided_at)
            _record_check(links, slug, after)
            word = ACTION_KO[action]
            return done(OK, out.reason or f"{word}했습니다", requested_at=cur.requested_at, decided_at=decided_at, reason_len=rlen)
        if out.status == NO_PENDING:
            return done(NO_PENDING, out.reason, requested_at=cur.requested_at, reason_len=rlen)
        status = out.status if out.status in (WRITE_OFF, NOT_CONFIGURED) else FAILED
        return done(status, out.reason, requested_at=cur.requested_at, reason_len=rlen)
    finally:
        with _LOCK:
            _INFLIGHT.discard(slug)


def _record_check(links, slug, card) -> None:
    check = getattr(links, "record_check", None)
    if check:
        try:
            check(slug, card)
        except Exception:
            pass


# ------------------------------------------------------------------ 범위 안 대기 목록
def pending_for_scope(client: CardSiteClient, link_table, scope_agent_ids: Iterable, is_main: bool) -> Outcome:
    """승인 대기 목록을 범위로 거른다. 돌려주는 값은 Outcome(value={"linked": [PendingCard], "unlinked": [PendingCard]}).

    - 관리자: 범위 안 설계사에 이어진 대기만 linked 에 둔다. unlinked 는 늘 빈 목록이다.
    - 메인 관리자: 이어진 대기는 범위 목록과 상관없이 모두 linked 에, 이어지지 않은 대기는 unlinked 에 둔다.
    - 이름, 연락처, 끝 네 자리, 사유는 담지 않는다. 사이트 응답에 있어도 넘기지 않는다."""
    out = client.admin_list()
    if out.status != OK:
        return Outcome(out.status, value={"linked": [], "unlinked": []}, reason=out.reason, attempts=out.attempts)
    keep = set(scope_agent_ids or ())
    linked, unlinked = [], []
    for e in out.value or []:
        if not isinstance(e, CardEntry) or e.request_status != "pending":
            continue
        agent_id = link_table.agent_for(e.slug)
        row = PendingCard(slug=e.slug, agent_id=agent_id, live=e.live, requested_at=e.requested_at,
                          replaces_live=e.replaces_live, conflict=e.conflict, live_url=client.card_url(e.slug) if e.live else None)
        if agent_id is None:
            if is_main:
                unlinked.append(row)
        elif is_main or agent_id in keep:
            linked.append(row)
    return Outcome(OK, value={"linked": linked, "unlinked": unlinked}, reason=out.reason, attempts=out.attempts)
