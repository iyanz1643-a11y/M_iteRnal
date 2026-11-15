"""해촉자 명함 내리기. S3 명함 연동 3차. D-44 에 내리기가 없어 메인 관리자만 하는 것으로 둔 가정이다.

화면이 부르는 함수
- removal_candidates: 명함 주소가 이어진 해촉자의 목록. 메인 관리자만 본다.
- remove_card: 메인 관리자가 확인한 명함 하나를 명함 사이트에서 내린다. POST /api/admin/remove 를 한 번 보낸다.

후보
- card_link 로 이어진 설계사 가운데 agent.status 가 active 가 아니거나 해촉일이 오늘이나 그 전인 사람이다.
- 마지막으로 확인한 상태가 공개 중도 승인 대기도 아니면 뺀다. 확인한 적이 없으면 담는다. refresh 가 참이면 공개 상태를 GET 으로 다시 읽는다.
- 저절로 내리지 않는다. 메인 관리자가 화면에서 고른 것만 remove_card 로 보낸다.

내리기의 차례
1. 두 번째 잠금. is_main 이 참이고 DB 에서도 활성 메인 관리자여야 한다.
2. 사유 검사. 사유는 명함 사이트의 공개 상태 조회로 누구나 본다. 전화번호, 주민번호, 긴 숫자, 전자우편 꼴과 이어진 설계사의 이름을 막는다.
   비면 보내지 않아 사이트가 정한 말이 들어간다.
3. 쓰기 스위치와 키. 막히면 보내지 않는다.
4. 같은 주소는 이 프로세스에서 한 번에 하나만.
5. 공개 상태를 읽어 공개 중도 승인 대기도 아니면 보내지 않는다. 읽지 못해도 보내지 않는다.
6. POST 를 한 번 보낸다. 자동으로 다시 보내지 않는다. 결과를 모르면 클라이언트가 공개 상태를 한 번 GET 으로 읽는다.
   그래도 모르면 unknown 이다. 메인 관리자가 명함 사이트 관리 화면에서 확인한 뒤 다시 할 수 있다. 사이트의 내리기는 여러 번 보내도 결과가 같다.

변경 기록 audit_log
- action card_remove, target agent:ID. 이어지지 않은 주소면 agent:none 이다.
- after_json 에는 주소, 결과, 사유 길이, 해촉 여부, 공개 상태로 확인했는지만 둔다. 이름, 사유 원문은 두지 않는다. 거부와 쓰기 꺼짐도 남긴다.
- card_link 줄은 끊지 않고 마지막 상태를 removed 로 적는다.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import date

from app import db
from app.connect import tables
from app.connect.base import FAILED, NOT_CONFIGURED, OK, Outcome
from app.connect.card_decide import DENIED, IN_FLIGHT, actor_allowed, has_pii
from app.connect.card_site import REASON_MAX, SLUG_RE, STATE_KO, UNKNOWN, WRITE_OFF, CardSiteClient, CardStatus

NOT_LIVE = "not_live"             # 공개 중도 승인 대기도 아니라 보내지 않았다
REMOVE_KO = {
    OK: "내림", DENIED: "권한 없음", IN_FLIGHT: "처리 중", NOT_LIVE: "공개 중 아님", UNKNOWN: "확인 필요",
    WRITE_OFF: "쓰기 꺼짐", NOT_CONFIGURED: "설정 안 됨", FAILED: "실패",
}
AUDIT_REMOVE = "card_remove"
MAIN_ONLY = "명함 내리기는 메인 관리자만 합니다"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_LOCK = threading.Lock()
_INFLIGHT: set = set()


@dataclass(frozen=True)
class RemovalCandidate:
    """내리기 후보 한 줄. 이름은 없다. 화면이 범위 안 이름을 따로 붙인다."""
    agent_id: int
    slug: str
    agent_status: str             # terminated / suspended / active(해촉일만 지난 경우)
    termination_date: str | None
    live: bool | None             # 마지막으로 확인한 공개 여부. 확인한 적이 없으면 None
    last_status: str | None       # pending / approved / rejected / removed / none
    last_checked_at: str | None
    url: str | None = None        # 공개 중이면 명함 주소

    @property
    def last_status_ko(self) -> str:
        return STATE_KO.get(self.last_status or "", "확인 전")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["last_status_ko"] = self.last_status_ko
        return d


@dataclass(frozen=True)
class RemoveResult:
    """내리기 한 번의 결과. 사유 원문은 없다."""
    slug: str
    agent_id: int | None
    result: str
    terminated: bool = False      # 이어진 설계사가 해촉자인가
    reason_len: int = 0
    confirmed_by_status: bool = False
    removed_at: str | None = None

    @property
    def result_ko(self) -> str:
        return REMOVE_KO.get(self.result, self.result)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["result_ko"] = self.result_ko
        return d


# ------------------------------------------------------------------ 공통
def _main_lock(con, actor, is_main, today) -> tuple[bool, str]:
    if not is_main:
        return False, MAIN_ONLY
    ok, why = actor_allowed(con, actor, True, today=today)
    return (True, "") if ok else (False, f"{why}. {MAIN_ONLY}")


def _links(con, link_table):
    if link_table is not None:
        return link_table
    from app.connect.card_link import CardLinkTable
    return CardLinkTable(con)


def _norm(text) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _terminated(con, agent_id, today: date) -> bool:
    if agent_id is None:
        return False
    r = con.execute("SELECT status, termination_date FROM agent WHERE agent_id=?", (agent_id,)).fetchone()
    if r is None:
        return False
    return r[0] != "active" or (r[1] is not None and str(r[1])[:10] <= today.isoformat())


def check_reason(con, reason, agent_id=None) -> tuple[str, str | None]:
    """내리기 사유를 다듬는다. 돌려주는 값은 (다듬은 사유, 거부 사유). 사유는 공개 상태 조회에 그대로 보인다."""
    text = str(reason or "").strip()
    if has_pii(text) or _EMAIL.search(text):
        return "", "사유에 전화번호, 주민번호, 전자우편 꼴이 있어 보내지 않았습니다. 사유는 누구나 볼 수 있으니 지우고 다시 적어 주세요"
    if agent_id is not None and text:
        r = con.execute("SELECT name FROM agent WHERE agent_id=?", (agent_id,)).fetchone()
        name = _norm(r[0]) if r else ""
        if len(name) >= 2 and name in _norm(text):
            return "", "사유에 설계사 이름이 있어 보내지 않았습니다. 사유는 누구나 볼 수 있으니 이름을 빼고 다시 적어 주세요"
    return text[:REASON_MAX], None


def _audit(con, actor, slug, agent_id, result, *, reason_len=0, terminated=False, confirmed=False) -> None:
    after = {"slug": slug, "result": result, "reason_len": reason_len, "terminated": bool(terminated)}
    if confirmed:
        after["confirmed_by_status"] = True
    with tables.write(con, "card_remove"):
        con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                    (db.now(), str(actor or "")[:64], AUDIT_REMOVE, f"agent:{agent_id if agent_id is not None else 'none'}",
                     None, json.dumps(after, ensure_ascii=False)))


def _record_check(links, slug, card) -> None:
    check = getattr(links, "record_check", None)
    if check:
        try:
            check(slug, card)
        except Exception:
            pass


# ------------------------------------------------------------------ 후보
def removal_candidates(con, *, actor: str, is_main: bool, client: CardSiteClient | None = None, refresh: bool = False,
                       include_done: bool = False, link_table=None, today: date | None = None) -> Outcome:
    """명함 주소가 이어진 해촉자의 목록. 메인 관리자만 본다. 예외를 올리지 않는다.
    돌려주는 값은 Outcome(status, value=list[RemovalCandidate], reason). status: ok / denied / failed
    refresh 가 참이고 client 가 있으면 후보마다 공개 상태를 GET 으로 읽어 card_link 에 적는다. 읽지 못한 주소는 지난 값을 둔다.
    include_done 이 참이면 이미 내려간 것도 담는다."""
    today = today or date.today()
    ok, why = _main_lock(con, actor, is_main, today)
    if not ok:
        return Outcome(DENIED, value=[], reason=why.replace(MAIN_ONLY, "명함 내리기 후보는 메인 관리자만 봅니다"))
    links = _links(con, link_table)
    try:
        rows = con.execute(
            """SELECT k.agent_id, k.slug, a.status, a.termination_date, k.last_live, k.last_status, k.last_checked_at
               FROM card_link k JOIN agent a ON a.agent_id=k.agent_id
               WHERE k.unlinked_at IS NULL AND (a.status<>'active' OR (a.termination_date IS NOT NULL AND substr(a.termination_date,1,10)<=?))
               ORDER BY a.termination_date, k.agent_id""", (today.isoformat(),)).fetchall()
    except Exception:
        return Outcome(FAILED, value=[], reason="명함 연결을 읽지 못했습니다")
    out, failed = [], 0
    for agent_id, slug, status, term, last_live, last_status, checked in rows:
        live = None if last_live is None else bool(last_live)
        if refresh and client is not None:
            got = client.status(slug)
            if got.status == OK and isinstance(got.value, CardStatus):
                _record_check(links, slug, got.value)
                live, last_status, checked = got.value.live, got.value.status, db.now()
            else:
                failed += 1
        done = live is False and last_status != "pending"
        if done and not include_done:
            continue
        url = client.card_url(slug) if client is not None and live else None
        out.append(RemovalCandidate(agent_id=agent_id, slug=slug, agent_status=status, termination_date=term, live=live,
                                    last_status=last_status, last_checked_at=checked, url=url))
    msg = f"내리기 후보 {len(out)}건입니다" + (f". 공개 상태를 읽지 못한 주소가 {failed}건 있습니다" if failed else "")
    return Outcome(OK, value=out, reason=msg)


# ------------------------------------------------------------------ 내리기
def remove_card(con, client: CardSiteClient, *, slug: str, reason: str = "", actor: str, is_main: bool,
                link_table=None, today: date | None = None) -> Outcome:
    """메인 관리자가 확인한 명함 하나를 명함 사이트에서 내린다. 예외를 올리지 않는다.
    돌려주는 값은 Outcome(status, value=RemoveResult, reason). reason 은 화면에 그대로 보여도 되는 말이다.
    status: ok / denied / in_flight / not_live / unknown / write_off / not_configured / failed"""
    today = today or date.today()
    slug = str(slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        return Outcome(FAILED, reason="명함 주소는 영문 소문자, 숫자, 하이픈 2자에서 30자입니다")
    links = _links(con, link_table)
    try:
        agent_id = links.agent_for(slug)
        gone = _terminated(con, agent_id, today)
    except Exception:
        return Outcome(FAILED, reason="명함 연결을 읽지 못했습니다")
    raw_len = len(str(reason or "").strip())

    def done(status, why="", *, reason_len=0, confirmed=False, removed_at=None, attempts=0, log=True) -> Outcome:
        if log:
            try:
                _audit(con, actor, slug, agent_id, status, reason_len=reason_len, terminated=gone, confirmed=confirmed)
            except Exception:
                if status == OK:
                    why = f"{why}. 변경 기록을 남기지 못했습니다".strip(". ")
        res = RemoveResult(slug=slug, agent_id=agent_id, result=status, terminated=gone, reason_len=reason_len,
                           confirmed_by_status=confirmed, removed_at=removed_at)
        return Outcome(status, value=res, reason=why, attempts=attempts)

    # 두 번째 잠금
    ok, why = _main_lock(con, actor, is_main, today)
    if not ok:
        return done(DENIED, why, reason_len=raw_len)

    # 사유
    text, bad = check_reason(con, reason, agent_id)
    if bad:
        return done(DENIED, bad, reason_len=raw_len)
    rlen = len(text)

    # 쓰기 스위치와 설정. 네트워크에 나가기 전에 본다
    gate = client.write_gate()
    if gate is not None:
        return done(gate.status, gate.reason, reason_len=rlen)

    with _LOCK:
        if slug in _INFLIGHT:
            return done(IN_FLIGHT, "같은 명함을 지금 내리고 있습니다. 잠시 뒤 상태를 확인해 주세요", reason_len=rlen)
        _INFLIGHT.add(slug)
    try:
        pre = client.status(slug)
        if pre.status != OK or not isinstance(pre.value, CardStatus):
            st = NOT_CONFIGURED if pre.status == NOT_CONFIGURED else FAILED
            return done(st, f"명함 사이트의 상태를 읽지 못해 보내지 않았습니다. {pre.reason}".strip()[:300], reason_len=rlen)
        cur: CardStatus = pre.value
        _record_check(links, slug, cur)
        if not cur.live and cur.status != "pending":
            return done(NOT_LIVE, f"공개 중인 명함이 없어 보내지 않았습니다. 지금 상태는 {cur.status_ko} 입니다", reason_len=rlen)

        out = client.remove(slug, text)
        res = out.value
        confirmed = bool(getattr(res, "confirmed_by_status", False))
        if out.status == OK:
            at = getattr(res, "decided_at", None)
            _record_check(links, slug, CardStatus(slug=slug, live=False, status="removed", live_at=None,
                                                  requested_at=cur.requested_at, decided_at=at))
            return done(OK, out.reason or "명함을 내렸습니다", reason_len=rlen, confirmed=confirmed, removed_at=at, attempts=1)
        if out.status == UNKNOWN:
            return done(UNKNOWN, out.reason, reason_len=rlen, attempts=1)
        status = out.status if out.status in (WRITE_OFF, NOT_CONFIGURED) else FAILED
        return done(status, out.reason, reason_len=rlen, attempts=1)
    finally:
        with _LOCK:
            _INFLIGHT.discard(slug)
