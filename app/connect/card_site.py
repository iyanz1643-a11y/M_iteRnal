"""모바일 명함 사이트 클라이언트. 1차: 읽기만.

명함 사이트의 API (netlify/functions/api.mjs 기준)
- GET /api/status?slug=   공개. {slug, live, liveAt, status, reason, at, decidedAt}
                          status 는 pending / approved / rejected / removed / none
- GET /api/admin/list     머리글 x-admin-key. {pending: [...], decided: [...], live: [...]}
                          요청 기록에는 이름과 연락처 전체가, 공개 기록에는 이름과 연락처 끝 네 자리가 있다
승인, 반려, 내리기, 수료 코드 발급은 이 파일에 만들지 않는다. 2차와 3차에서 메인 관리자 확인과 함께 만든다.
그래서 이 클라이언트는 GET 만 보낸다.

앱 쪽으로 넘기는 것은 주소, 공개 여부, 승인 시각, 대기와 반려 상태뿐이다.
이름은 suggest_links 안에서 설계사와 잇는 데만 쓰고 결과와 상태 기록에 남기지 않는다. 연락처는 읽지 않는다.

환경 변수: CARD_SITE_URL (사이트 주소), CARD_ADMIN_KEY (관리자 연동 키). 키는 관리자 호출에만 붙인다.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import Iterable, Protocol

import httpx

from app.connect.base import FAILED, NOT_CONFIGURED, OK, NotConfigured, Outcome
from app.connect.retry import Retryable, call_with_retry
from app.connect.state import StateStore

ENV_URL = "CARD_SITE_URL"
ENV_KEY = "CARD_ADMIN_KEY"
ADMIN_HEADER = "x-admin-key"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")     # 명함 사이트 lib.mjs 와 같은 규칙
STATES = ("pending", "approved", "rejected", "removed", "none")
STATE_KO = {"pending": "승인 대기", "approved": "승인", "rejected": "반려", "removed": "내림", "none": "신청 없음"}

CONN_STATUS = "card_site.status"
CONN_ADMIN = "card_site.admin"


class CardSiteError(Exception):
    """다시 해도 소용없는 실패. 키가 틀렸거나 요청이 잘못됐다."""


def _norm_name(n) -> str:
    return re.sub(r"\s+", "", str(n or ""))


def _state(v) -> str:
    v = str(v or "none")
    return v if v in STATES else "none"


# ------------------------------------------------------------------ 앱 쪽으로 넘기는 형식
@dataclass(frozen=True)
class CardStatus:
    slug: str
    live: bool                    # 공개 중인가
    status: str                   # pending / approved / rejected / removed / none
    live_at: str | None = None    # 공개본이 승인된 시각
    requested_at: str | None = None
    decided_at: str | None = None
    url: str | None = None        # 공개 중이면 명함 주소

    @property
    def status_ko(self) -> str:
        return STATE_KO.get(self.status, self.status)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status_ko"] = self.status_ko
        return d


@dataclass(frozen=True)
class CardEntry:
    """관리자 목록의 한 줄. 이름과 연락처는 없다."""
    slug: str
    live: bool
    request_status: str           # pending / approved / rejected / removed / none
    live_at: str | None = None
    requested_at: str | None = None
    decided_at: str | None = None
    replaces_live: bool = False   # 공개 중인 명함을 바꾸려는 신청인가
    conflict: bool = False        # 공개 중인 명함과 신청자 정보가 다른가. 사이트가 판정한 값


@dataclass(frozen=True)
class LinkSuggestion:
    """명함 주소와 이을 만한 설계사. 메인 관리자가 확인하고 잇는다. 이름은 담지 않는다."""
    slug: str
    agent_ids: tuple              # 이름이 같은 설계사. 둘 이상이면 메인 관리자가 고른다
    live: bool
    request_status: str


# ------------------------------------------------------------------ 클라이언트
class CardSiteClient:
    def __init__(self, base_url: str | None = None, admin_key: str | None = None, *, timeout: float = 10.0,
                 attempts: int = 3, backoff: float = 1.0, transport: httpx.BaseTransport | None = None,
                 state: StateStore | None = None, sleep=None):
        self.base_url = (base_url if base_url is not None else os.environ.get(ENV_URL, "")).rstrip("/")
        self._admin_key = admin_key if admin_key is not None else os.environ.get(ENV_KEY, "")
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff
        self.transport = transport
        self.state = state or StateStore()
        self._sleep = sleep

    def __repr__(self) -> str:   # 키가 로그에 찍히지 않게 한다
        return f"CardSiteClient(base_url={self.base_url!r}, admin={'set' if self._admin_key else 'unset'})"

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def admin_configured(self) -> bool:
        return bool(self.base_url and self._admin_key)

    def card_url(self, slug: str) -> str | None:
        return f"{self.base_url}/{slug}/" if self.base_url and SLUG_RE.match(slug or "") else None

    # ---------------------------------------------------------- 보내기. GET 만
    def _get_json(self, path: str, params: dict | None = None, admin: bool = False) -> dict:
        if not self.base_url:
            raise NotConfigured(f"설정 안 됨: 환경 변수 {ENV_URL} 가 없습니다")
        headers = {"accept": "application/json", "user-agent": "ga-app-connect/1"}
        if admin:
            if not self._admin_key:
                raise NotConfigured(f"설정 안 됨: 환경 변수 {ENV_KEY} 가 없어 관리자 목록을 열지 않습니다")
            headers[ADMIN_HEADER] = self._admin_key
        with httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport) as c:
            r = c.request("GET", path, params=params, headers=headers)
        if r.status_code >= 500 or r.status_code == 429:
            raise Retryable(f"명함 사이트가 {r.status_code} 를 돌려줬습니다")
        if r.status_code == 401:
            raise CardSiteError("관리자 키가 맞지 않습니다 (401)")
        if r.status_code >= 400:
            raise CardSiteError(f"명함 사이트가 요청을 받지 않았습니다 ({r.status_code})")
        try:
            data = r.json()
        except ValueError:
            raise CardSiteError("명함 사이트의 응답이 JSON 이 아닙니다") from None
        if not isinstance(data, dict):
            raise CardSiteError("명함 사이트의 응답 모양이 다릅니다")
        return data

    def _call(self, conn: str, fn) -> Outcome:
        kw = {"sleep": self._sleep} if self._sleep else {}
        # 시간 제한은 httpx 가 건다. 스레드로 한 번 더 감싸지 않는다
        out = call_with_retry(fn, attempts=self.attempts, timeout=None, backoff=self.backoff, **kw)
        try:
            self.state.record(conn, out.status, reason=out.reason)
        except Exception:
            pass
        return out

    # ---------------------------------------------------------- 공개 상태
    def status(self, slug: str) -> Outcome:
        """명함 하나의 공개 상태. 돌려주는 값은 Outcome(value=CardStatus)."""
        slug = str(slug or "").strip().lower()
        if not SLUG_RE.match(slug):
            return Outcome(FAILED, reason="명함 주소는 영문 소문자, 숫자, 하이픈 2자에서 30자입니다")

        def go():
            d = self._get_json("/api/status", params={"slug": slug})
            live = bool(d.get("live"))
            return CardStatus(slug=slug, live=live, status=_state(d.get("status")), live_at=d.get("liveAt"),
                              requested_at=d.get("at"), decided_at=d.get("decidedAt"), url=self.card_url(slug) if live else None)
        return self._call(CONN_STATUS, go)

    # ---------------------------------------------------------- 관리자 목록
    def _admin_raw(self) -> dict:
        return self._get_json("/api/admin/list", admin=True)

    def admin_list(self) -> Outcome:
        """관리자 목록을 주소별로 합친다. 돌려주는 값은 Outcome(value=list[CardEntry]). 이름과 연락처는 버린다."""
        if not self.admin_configured:
            return self._not_configured()
        return self._call(CONN_ADMIN, lambda: _entries(self._admin_raw()))

    def suggest_links(self, agents: Iterable) -> Outcome:
        """agents 는 (agent_id, 이름) 의 목록. 명함 신청자 이름이 같은 설계사를 찾아 주소별로 돌려준다.
        이름은 이 함수 안에서만 쓰고 결과에는 agent_id 만 남긴다. 돌려주는 값은 Outcome(value=list[LinkSuggestion])."""
        if not self.admin_configured:
            return self._not_configured()
        by_name: dict = {}
        for agent_id, name in agents:
            n = _norm_name(name)
            if n:
                by_name.setdefault(n, []).append(agent_id)

        def go():
            raw = self._admin_raw()
            names: dict = {}
            for rec in _records(raw):
                slug = rec.get("slug")
                if isinstance(slug, str) and SLUG_RE.match(slug):
                    n = _norm_name(rec.get("name"))
                    if n:
                        names.setdefault(slug, set()).add(n)
            entries = {e.slug: e for e in _entries(raw)}
            out = []
            for slug, ns in sorted(names.items()):
                ids = tuple(sorted({a for n in ns for a in by_name.get(n, [])}))
                if ids and slug in entries:
                    e = entries[slug]
                    out.append(LinkSuggestion(slug=slug, agent_ids=ids, live=e.live, request_status=e.request_status))
            return out
        return self._call(CONN_ADMIN, go)

    def _not_configured(self) -> Outcome:
        missing = [k for k, v in ((ENV_URL, self.base_url), (ENV_KEY, self._admin_key)) if not v]
        out = Outcome(NOT_CONFIGURED, reason=f"설정 안 됨: 환경 변수 {', '.join(missing)} 가 없어 관리자 목록을 열지 않습니다")
        try:
            self.state.record(CONN_ADMIN, NOT_CONFIGURED, reason=out.reason)
        except Exception:
            pass
        return out


def _records(raw: dict):
    for key in ("pending", "decided", "live"):
        for rec in raw.get(key) or []:
            if isinstance(rec, dict):
                yield rec


def _entries(raw: dict) -> list:
    """사이트의 관리자 목록을 주소별 CardEntry 로 바꾼다. 이름, 연락처, 제목, 사유, 파일 목록은 옮기지 않는다."""
    req: dict = {}
    for key in ("pending", "decided"):
        for rec in raw.get(key) or []:
            if isinstance(rec, dict) and SLUG_RE.match(str(rec.get("slug") or "")):
                cur = req.get(rec["slug"])
                if cur is None or str(rec.get("at") or "") > str(cur.get("at") or ""):
                    req[rec["slug"]] = rec
    live = {rec["slug"]: rec for rec in raw.get("live") or []
            if isinstance(rec, dict) and SLUG_RE.match(str(rec.get("slug") or ""))}
    out = []
    for slug in sorted(set(req) | set(live)):
        r, lv = req.get(slug) or {}, live.get(slug)
        out.append(CardEntry(
            slug=slug, live=lv is not None,
            request_status=_state(r.get("status")) if r else ("approved" if lv else "none"),
            live_at=(lv or {}).get("at"), requested_at=r.get("at"), decided_at=r.get("decidedAt"),
            replaces_live=bool(r.get("replaces")), conflict=bool(r.get("conflict"))))
    return out


def pending_entries(entries: Iterable[CardEntry]) -> list:
    """메인 관리자가 앱에서 보는 승인 대기 목록."""
    return [e for e in entries if e.request_status == "pending"]


# ------------------------------------------------------------------ 설계사와 명함 주소를 잇는 표
class CardLinkStore(Protocol):
    """설계사와 명함 주소를 잇는 표의 꼴. DB 구현은 app/connect/card_link.py 의 CardLinkTable 이다.
    잇기와 끊기는 앱 안의 기록이고 메인 관리자만 한다. record_check 는 있으면 쓰는 선택 함수다."""

    def slug_for(self, agent_id) -> str | None: ...
    def agent_for(self, slug: str): ...
    def link(self, agent_id, slug: str, by: str) -> None: ...
    def unlink(self, agent_id, by: str, reason: str = "") -> None: ...


class MemoryLinkStore:
    """시험에서 쓰는 메모리 표."""

    def __init__(self, pairs: dict | None = None):
        self._by_agent = dict(pairs or {})

    def slug_for(self, agent_id):
        return self._by_agent.get(agent_id)

    def agent_for(self, slug):
        return next((a for a, s in self._by_agent.items() if s == slug), None)

    def link(self, agent_id, slug, by):
        if not SLUG_RE.match(slug or ""):
            raise ValueError("명함 주소 꼴이 아닙니다")
        other = self.agent_for(slug)
        if other is not None and other != agent_id:
            raise ValueError("이미 다른 설계사와 이어진 주소입니다")
        self._by_agent[agent_id] = slug

    def unlink(self, agent_id, by, reason=""):
        self._by_agent.pop(agent_id, None)


def my_card(client: CardSiteClient, links: CardLinkStore, agent_id) -> Outcome:
    """내 명함 화면용. 이어진 주소가 없으면 value=None 인 성공으로 돌려준다.
    표가 record_check 를 갖고 있으면 읽은 상태와 시각을 표에도 적는다. 적지 못해도 화면은 그대로 보인다."""
    slug = links.slug_for(agent_id)
    if not slug:
        return Outcome(OK, value=None, reason="이어진 명함 주소가 없습니다")
    out = client.status(slug)
    check = getattr(links, "record_check", None)
    if check and out.status == OK and out.value is not None:
        try:
            check(slug, out.value)
        except Exception:
            pass
    return out
