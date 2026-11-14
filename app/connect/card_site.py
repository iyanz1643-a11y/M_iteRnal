"""모바일 명함 사이트 클라이언트. 1차: 읽기. D-44: 승인과 반려 쓰기. 2차: 수료 코드 발급 쓰기와 발급 기록 읽기.

명함 사이트의 API (netlify/functions/api.mjs 기준)
- GET /api/status?slug=   공개. {slug, live, liveAt, status, reason, at, decidedAt}
                          status 는 pending / approved / rejected / removed / none
- GET /api/admin/list     머리글 x-admin-key. {pending: [...], decided: [...], live: [...]}
                          요청 기록에는 이름과 연락처 전체가, 공개 기록에는 이름과 연락처 끝 네 자리가 있다
- POST /api/admin/approve {slug}           머리글 x-admin-key. 성공 {ok, slug, at}. 대기 중이 아니면 404 no_pending
- POST /api/admin/reject  {slug, reason}   머리글 x-admin-key. 성공 {ok}. reason 은 사이트가 200자로 자른다
- GET /api/admin/file?slug=&path=&stage=   머리글 x-admin-key. 신청본(stage=pending) 파일 하나의 바이트.
                          path 는 사이트의 ALLOWED 와 같은 목록만 받는다. 없으면 404, 꼴이 틀리면 400

- POST /api/admin/codes  {course, ym, people: [{name, phone}]}  머리글 x-admin-key. 수료 코드 발급.
                          course 는 영문 대문자 2자에서 6자, ym 은 YYYY-MM, people 은 1명에서 200명이다.
                          이름이 비면 사이트가 건너뛴다. phone 은 없어도 되고, 없으면 사이트가 끝자리 대신 * 로 묶는다.
                          성공 {ok, rows: [{serial, course, ym, name, phone, code, at, status}]}. 일련번호는 과정+YYMM-순번이다.
                          검사에 걸리면 400 bad_course / bad_ym / bad_people 이고 아무것도 만들지 않는다.
- GET /api/admin/codes    머리글 x-admin-key. {log: [발급 기록]}. 기록마다 이름, 연락처, 코드 원문이 있다

쓰기 규칙
- 승인, 반려, 수료 코드 발급만 만든다. 내리기는 만들지 않는다.
- 환경 변수 CARD_SITE_WRITE 가 1 일 때만 POST 를 보낸다. 아니면 보내지 않고 write_off 를 돌려준다. 기본은 꺼짐이다.
- 쓰기는 자동으로 다시 보내지 않는다. 응답을 받지 못했거나 5xx 면 공개 상태를 한 번 GET 으로 확인해 결과를 정한다.
- 화면은 이 클라이언트의 approve, reject 를 바로 부르지 않고 app/connect/card_decide.py 의 decide_card 를 부른다.
  권한, 범위, 같은 요청 막기, 변경 기록은 그쪽에 있다.

수료 코드 발급 issue_codes (S2 명함 연동 2차)
- 쓰기 규칙은 승인과 같다. 쓰기 스위치와 키를 먼저 보고, POST 는 한 번만 보내고 다시 보내지 않는다.
- 보내기 전에 발급 기록을 읽어 이미 있는 일련번호를 기억한다. 읽지 못하면 보내지 않는다.
- 응답을 받지 못했거나 5xx 거나 응답을 읽지 못하면 발급 기록을 한 번 GET 으로 읽어, 보내기 전에 없던 일련번호 가운데
  과정과 수료월과 이름이 차례대로 맞는 줄로 결과를 정한다. 그래도 모르면 unknown 으로 돌려준다.
- 명단에는 이름만 담는다. 휴대폰은 보내지 않는다. 사이트는 휴대폰이 없으면 이름과 * 로 코드를 묶는다.
- 돌려받은 코드 원문은 그 자리에서 일련번호와 해시로 바꾸고, 원문은 IssuedCode.code 에 화면에 한 번 보여 줄 값으로만 둔다.
  repr 과 to_dict 에는 원문이 없다. 이름과 휴대폰은 맞춰 보는 데만 쓰고 버린다.
- 화면은 issue_codes 를 바로 부르지 않고 app/connect/card_codes.py 의 issue_card_codes 를 부른다.
- code_log 는 발급 기록을 읽어 일련번호, 과정, 수료월, 해시, 시각만 돌려준다. 이름, 연락처, 원문은 버린다.

앱 쪽으로 넘기는 것은 주소, 공개 여부, 승인 시각, 대기와 반려 상태뿐이다.
이름은 suggest_links 안에서 설계사와 잇는 데만 쓰고 결과와 상태 기록에 남기지 않는다. 목록의 연락처는 읽지 않는다.

신청본 미리보기 (2026-11-14 대표님 결정)
- pending_file 은 승인 대기 신청본 파일 하나를 읽는다. 읽기 GET 이라 다른 읽기와 같은 재시도 규칙을 따른다.
- 가져온 바이트는 돌려주기만 하고 디스크, DB, 로그, 연결 상태 어디에도 남기지 않는다.
- 화면은 이 함수를 바로 부르지 않고 card_decide.preview_card 를 부른다. 권한, 범위, 대기 확인, HTML 다듬기, 변경 기록은 그쪽에 있다.

환경 변수: CARD_SITE_URL (사이트 주소), CARD_ADMIN_KEY (관리자 연동 키), CARD_SITE_WRITE (1 이면 쓰기 켬).
키는 관리자 호출에만 붙인다.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, NamedTuple, Protocol, Sequence

import httpx

from app.connect.base import FAILED, NOT_CONFIGURED, OK, NotConfigured, Outcome, describe
from app.connect.retry import Retryable, call_with_retry
from app.connect.state import StateStore

ENV_URL = "CARD_SITE_URL"
ENV_KEY = "CARD_ADMIN_KEY"
ENV_WRITE = "CARD_SITE_WRITE"
ADMIN_HEADER = "x-admin-key"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")     # 명함 사이트 lib.mjs 와 같은 규칙
STATES = ("pending", "approved", "rejected", "removed", "none")
STATE_KO = {"pending": "승인 대기", "approved": "승인", "rejected": "반려", "removed": "내림", "none": "신청 없음"}

CONN_STATUS = "card_site.status"
CONN_ADMIN = "card_site.admin"
CONN_WRITE = "card_site.write"
CONN_FILE = "card_site.file"
CONN_CODES = "card_site.codes"    # 수료 코드 발급 기록 읽기. 발급 POST 는 CONN_WRITE 에 적는다

# 신청본 파일. 명함 사이트 lib.mjs 의 ALLOWED, TYPES 와 같은 규칙. fullmatch 로 쓴다
FILE_RE = re.compile(r"(index\.html|card\.jpg|og\.jpg|contact\.vcf|(card|claim|kakao|blog|youtube|insta)/index\.html)")
FILE_TYPES = {"html": "text/html; charset=utf-8", "jpg": "image/jpeg", "vcf": "text/vcard; charset=utf-8"}
FILE_MAX_BYTES = 5 * 1024 * 1024  # 파일 하나의 상한. 사이트는 신청 전체를 4.5MB 로 막는다
JPEG_MAGIC = b"\xff\xd8\xff"

# 파일 읽기 결과 상태. OK, FAILED, NOT_CONFIGURED 와 함께 쓴다
NOT_FOUND = "not_found"           # 사이트에 그 신청본 파일이 없다
TOO_BIG = "too_big"               # 크기 상한을 넘어 읽다가 멈췄다

# 쓰기 결과 상태. OK, FAILED, NOT_CONFIGURED 와 함께 쓴다
WRITE_OFF = "write_off"           # CARD_SITE_WRITE 가 꺼져 있어 보내지 않았다
NO_PENDING = "no_pending"         # 사이트에 대기 중인 요청이 없다
WRITE_ACTIONS = ("approve", "reject")
TARGET_STATE = {"approve": "approved", "reject": "rejected"}
REASON_MAX = 200                  # 사이트가 자르는 길이와 같다

# 수료 코드 발급. 사이트 issueCodes 와 같은 규칙
UNKNOWN = "unknown"               # 보냈지만 결과를 확인하지 못했다. 다시 보내지 않는다
CODE_COURSE_RE = re.compile(r"^[A-Z]{2,6}$")
CODE_YM_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CODES_MAX = 200                   # 사이트가 한 번에 받는 명단 수의 상한


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
class WriteResult:
    """승인이나 반려 한 번의 결과. 사유 원문은 담지 않는다."""
    slug: str
    action: str                   # approve / reject
    decided_at: str | None = None
    confirmed_by_status: bool = False   # 응답을 받지 못해 공개 상태를 읽어 결과를 확인했다


class CardFile(NamedTuple):
    """신청본 파일 하나. (바이트, 내용 종류) 로 풀어 쓸 수 있다. repr 에는 크기만 보여 내용이 로그에 찍히지 않게 한다."""
    data: bytes
    content_type: str

    def __repr__(self) -> str:
        return f"CardFile({len(self.data)} bytes, {self.content_type!r})"

    __str__ = __repr__


def file_type(path: str) -> str:
    """사이트의 typeOf 와 같다. 내용 종류는 사이트 응답 머리글이 아니라 파일 이름으로 정한다."""
    return FILE_TYPES.get(str(path).rsplit(".", 1)[-1], "application/octet-stream")


def valid_file_path(path) -> bool:
    return isinstance(path, str) and FILE_RE.fullmatch(path) is not None


@dataclass(frozen=True)
class IssuedCode:
    """사이트가 발급한 코드 하나. 원문은 code 에만 있고 repr, 비교, to_dict 에는 빠진다. 화면에 한 번 보여 주고 버린다."""
    index: int                    # 보낸 명단에서의 자리. 0 부터
    serial: str
    course: str
    ym: str
    code_sha256: str              # education.card_code_hash 와 같은 값
    issued_at: str | None = None
    code: str = field(default="", repr=False, compare=False)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "code"}


@dataclass(frozen=True)
class CodeBatch:
    """수료 코드 발급 POST 한 번의 결과. 자리는 보낸 명단의 차례다. 이름은 담지 않는다."""
    course: str
    ym: str
    sent: int                     # 사이트에 보낸 명단 수. 보내지 않았으면 0
    issued: tuple = ()            # IssuedCode
    not_issued: tuple = ()        # 사이트가 만들지 않은 것이 확실한 자리
    unknown: tuple = ()           # 만들었는지 모르는 자리
    confirmed_by_log: bool = False   # 응답을 받지 못해 발급 기록을 읽어 정했다
    extra_serials: tuple = ()     # 새로 생겼지만 명단과 맞추지 못한 일련번호


@dataclass(frozen=True)
class SiteCode:
    """사이트 발급 기록 한 줄. 이름, 연락처, 코드 원문은 없다."""
    serial: str
    course: str
    ym: str
    code_sha256: str | None       # 원문이 없거나 비면 None
    at: str | None = None
    status: str | None = None


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
                 state: StateStore | None = None, sleep=None, write: bool | None = None):
        self.base_url = (base_url if base_url is not None else os.environ.get(ENV_URL, "")).rstrip("/")
        self._admin_key = admin_key if admin_key is not None else os.environ.get(ENV_KEY, "")
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff
        self.transport = transport
        self.state = state or StateStore()
        self._sleep = sleep
        # 쓰기 스위치. 넘기지 않으면 환경 변수 CARD_SITE_WRITE 가 정확히 1 일 때만 켠다
        self.write_enabled = (os.environ.get(ENV_WRITE, "").strip() == "1") if write is None else bool(write)

    def __repr__(self) -> str:   # 키가 로그에 찍히지 않게 한다
        return (f"CardSiteClient(base_url={self.base_url!r}, admin={'set' if self._admin_key else 'unset'}, "
                f"write={'on' if self.write_enabled else 'off'})")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def admin_configured(self) -> bool:
        return bool(self.base_url and self._admin_key)

    def card_url(self, slug: str) -> str | None:
        return f"{self.base_url}/{slug}/" if self.base_url and SLUG_RE.match(slug or "") else None

    # ---------------------------------------------------------- 읽기. GET
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

    # ---------------------------------------------------------- 신청본 파일
    def pending_file(self, slug: str, path: str, *, max_bytes: int = FILE_MAX_BYTES) -> Outcome:
        """승인 대기 신청본 파일 하나를 읽는다. 돌려주는 값은 Outcome(value=CardFile(바이트, 내용 종류)).
        상태는 ok / not_found / too_big / not_configured / failed. 5xx, 429, 연결 오류, 시간 초과만 다시 시도한다.
        slug 와 path 는 사이트와 같은 규칙으로 먼저 보고, 맞지 않으면 보내지 않는다.
        HTML 은 다듬지 않은 원본이다. 화면은 card_decide.preview_card 를 거쳐 다듬은 것만 받는다."""
        slug = str(slug or "").strip().lower()
        path = str(path or "").strip()
        if not SLUG_RE.match(slug):
            return Outcome(FAILED, reason="명함 주소는 영문 소문자, 숫자, 하이픈 2자에서 30자입니다")
        if not valid_file_path(path):
            return Outcome(FAILED, reason="명함 사이트가 받는 신청본 파일 이름이 아닙니다")
        if not self.admin_configured:
            return self._not_configured(CONN_FILE, "신청본 파일을 가져오지 않습니다")
        limit = max(1, int(max_bytes))
        ctype = file_type(path)

        def go():
            headers = {"accept": "*/*", "user-agent": "ga-app-connect/1", ADMIN_HEADER: self._admin_key}
            params = {"slug": slug, "path": path, "stage": "pending"}
            with httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport) as c:
                with c.stream("GET", "/api/admin/file", params=params, headers=headers) as r:
                    if r.status_code >= 500 or r.status_code == 429:
                        raise Retryable(f"명함 사이트가 {r.status_code} 를 돌려줬습니다")
                    if r.status_code == 401:
                        raise CardSiteError("관리자 키가 맞지 않습니다 (401)")
                    if r.status_code == 404:
                        return (NOT_FOUND, None)
                    if r.status_code >= 400:
                        raise CardSiteError(f"명함 사이트가 요청을 받지 않았습니다 ({r.status_code})")
                    size = r.headers.get("content-length", "")
                    if size.isdigit() and int(size) > limit:
                        return (TOO_BIG, None)
                    buf = bytearray()
                    for chunk in r.iter_bytes():
                        buf += chunk
                        if len(buf) > limit:
                            return (TOO_BIG, None)
            return (OK, bytes(buf))

        out = self._call(CONN_FILE, go)
        if out.status != OK:
            return out
        kind, data = out.value
        if kind == NOT_FOUND:
            return Outcome(NOT_FOUND, reason="명함 사이트에 그 신청본 파일이 없습니다. 이미 처리되었을 수 있습니다", attempts=out.attempts)
        if kind == TOO_BIG:
            return Outcome(TOO_BIG, reason=f"신청본 파일이 {limit // (1024 * 1024) or 1}MB 상한을 넘어 읽지 않았습니다", attempts=out.attempts)
        if ctype == FILE_TYPES["jpg"] and not data.startswith(JPEG_MAGIC):
            return Outcome(FAILED, reason="그림 파일이 JPEG 가 아니라 보여 주지 않습니다", attempts=out.attempts)
        return Outcome(OK, value=CardFile(data, ctype), attempts=out.attempts)

    # ---------------------------------------------------------- 쓰기. 승인과 반려만
    def approve(self, slug: str) -> Outcome:
        """승인 요청을 한 번 보낸다. 돌려주는 값은 Outcome(value=WriteResult).
        상태는 ok / no_pending / write_off / not_configured / failed. 화면은 decide_card 를 거쳐 부른다."""
        return self._write("approve", slug, {})

    def reject(self, slug: str, reason: str = "") -> Outcome:
        """반려 요청을 한 번 보낸다. 사유는 앞뒤 공백을 지우고 200자로 자른다. 사유 원문은 결과와 상태 기록에 남기지 않는다."""
        return self._write("reject", slug, {"reason": str(reason or "").strip()[:REASON_MAX]})

    def _write(self, action: str, slug, extra: dict) -> Outcome:
        slug = str(slug or "").strip().lower()
        if action not in WRITE_ACTIONS:
            return Outcome(FAILED, reason="승인과 반려만 보낼 수 있습니다")
        if not SLUG_RE.match(slug):
            return Outcome(FAILED, reason="명함 주소는 영문 소문자, 숫자, 하이픈 2자에서 30자입니다")
        gate = self.write_gate()
        if gate is not None:
            return gate
        out = self._post_once(action, slug, {"slug": slug, **extra})
        # 대기 없음은 사이트가 제대로 답한 것이라 연결은 성공으로 적는다
        self._record(CONN_WRITE, OK if out.status in (OK, NO_PENDING) else FAILED, out.reason)
        return out

    def write_gate(self) -> Outcome | None:
        """쓰기 전에 스위치와 설정을 본다. 막히면 write_off 나 not_configured 를 연결 상태에 적고 돌려준다. 열려 있으면 None."""
        if not self.write_enabled:
            out = Outcome(WRITE_OFF, reason=f"쓰기가 꺼져 있습니다. 환경 변수 {ENV_WRITE} 가 1 이 아니라 명함 사이트에 보내지 않았습니다")
            self._record(CONN_WRITE, NOT_CONFIGURED, out.reason)
            return out
        if not self.admin_configured:
            missing = [k for k, v in ((ENV_URL, self.base_url), (ENV_KEY, self._admin_key)) if not v]
            out = Outcome(NOT_CONFIGURED, reason=f"설정 안 됨: 환경 변수 {', '.join(missing)} 가 없어 보내지 않았습니다")
            self._record(CONN_WRITE, NOT_CONFIGURED, out.reason)
            return out
        return None

    def _post_once(self, action: str, slug: str, body: dict) -> Outcome:
        """POST 를 한 번만 보낸다. 다시 보내지 않는다.
        응답을 받지 못했거나 5xx 면 서버가 처리했는지 모르므로 공개 상태를 한 번 읽어 결과를 정한다."""
        headers = {"accept": "application/json", "user-agent": "ga-app-connect/1", ADMIN_HEADER: self._admin_key}
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport) as c:
                r = c.request("POST", f"/api/admin/{action}", json=body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # 연결 자체가 안 됐다. 요청이 서버에 닿지 않았으므로 처리되지 않았다
            return Outcome(FAILED, reason=f"명함 사이트에 닿지 않아 보내지 못했습니다. {describe(e)}"[:300], attempts=1)
        except httpx.TransportError as e:
            return self._confirm(action, slug, f"응답을 받지 못했습니다. {describe(e)}")
        data = {}
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if 200 <= r.status_code < 300:
            return Outcome(OK, value=WriteResult(slug=slug, action=action, decided_at=data.get("at")), attempts=1)
        if r.status_code == 404 and data.get("error") == "no_pending":
            return Outcome(NO_PENDING, value=WriteResult(slug=slug, action=action),
                           reason="대기 중인 요청이 없습니다. 이미 처리되었을 수 있습니다", attempts=1)
        if r.status_code == 401:
            return Outcome(FAILED, reason="관리자 키가 맞지 않습니다 (401)", attempts=1)
        if r.status_code == 429:
            return Outcome(FAILED, reason="명함 사이트가 요청이 많다고 받지 않았습니다 (429). 잠시 뒤 다시 해 주세요", attempts=1)
        if r.status_code >= 500:
            return self._confirm(action, slug, f"명함 사이트가 {r.status_code} 를 돌려줬습니다")
        return Outcome(FAILED, reason=f"명함 사이트가 요청을 받지 않았습니다 ({r.status_code})", attempts=1)

    def _confirm(self, action: str, slug: str, why: str) -> Outcome:
        """결과를 모를 때 공개 상태를 한 번 읽는다. 다시 보내지는 않는다."""
        try:
            d = self._get_json("/api/status", params={"slug": slug})
        except Exception as e:
            return Outcome(FAILED, reason=f"{why}. 결과를 확인하지 못했습니다. 명함 사이트 관리 화면에서 상태를 확인해 주세요 ({describe(e)})"[:300],
                           attempts=1)
        st = _state(d.get("status"))
        if st == TARGET_STATE[action]:
            return Outcome(OK, value=WriteResult(slug=slug, action=action, decided_at=d.get("decidedAt"), confirmed_by_status=True),
                           reason=f"{why}. 공개 상태로 반영을 확인했습니다"[:300], attempts=1)
        if st == "pending":
            return Outcome(FAILED, reason=f"{why}. 아직 승인 대기입니다. 다시 시도해 주세요"[:300], attempts=1)
        return Outcome(FAILED, reason=f"{why}. 지금 상태는 {STATE_KO.get(st, st)} 입니다. 명함 사이트 관리 화면에서 확인해 주세요"[:300],
                       attempts=1)

    # ---------------------------------------------------------- 수료 코드
    def code_log(self) -> Outcome:
        """발급 기록을 읽는다. 돌려주는 값은 Outcome(value=list[SiteCode]). 이름, 연락처, 코드 원문은 버리고 해시만 남긴다.
        읽기 GET 이라 다른 읽기와 같은 재시도 규칙을 따른다. 쓰기 스위치와 상관없다."""
        if not self.admin_configured:
            return self._not_configured(CONN_CODES, "발급 기록을 읽지 않습니다")
        return self._call(CONN_CODES, lambda: _site_codes(self._get_json("/api/admin/codes", admin=True)))

    def issue_codes(self, course: str, ym: str, names: Sequence[str]) -> Outcome:
        """수료 코드 발급을 한 번 보낸다. names 는 명함에 찍힐 이름의 목록이고 이름만 보낸다.
        돌려주는 값은 Outcome(status, value=CodeBatch, reason). 예외를 올리지 않는다.
        status: ok / unknown / write_off / not_configured / failed
        - ok: 사이트가 답했다. 자리마다 issued 나 not_issued 에 있다.
        - unknown: 보냈지만 결과를 확인하지 못한 자리가 있다. 다시 보내지 말고 사이트 관리 화면에서 확인한다.
        - failed: 보내지 않았거나 사이트가 받지 않았다. 모든 자리가 not_issued 다.
        화면은 card_codes.issue_card_codes 를 거쳐 부른다."""
        course = str(course or "").strip().upper()
        ym = str(ym or "").strip()
        names = [str(n or "").strip() for n in (names or [])]
        empty = CodeBatch(course=course, ym=ym, sent=0, not_issued=tuple(range(len(names))))
        if not CODE_COURSE_RE.match(course):
            return Outcome(FAILED, value=empty, reason="명함 과정 코드는 영문 대문자 2자에서 6자입니다")
        if not CODE_YM_RE.match(ym):
            return Outcome(FAILED, value=empty, reason="수료월은 YYYY-MM 으로 넣어야 합니다")
        if not names or len(names) > CODES_MAX:
            return Outcome(FAILED, value=empty, reason=f"명단은 1명에서 {CODES_MAX}명까지 보낼 수 있습니다")
        if any(not _norm_name(n) for n in names):
            return Outcome(FAILED, value=empty, reason="이름이 빈 사람이 있어 보내지 않았습니다")
        gate = self.write_gate()
        if gate is not None:
            return Outcome(gate.status, value=empty, reason=gate.reason)

        # 보내기 전에 이미 있는 일련번호를 읽는다. 결과를 모를 때 새 줄을 가려내는 데 쓴다
        base = self._call(CONN_CODES, lambda: {c.serial for c in _site_codes(self._get_json("/api/admin/codes", admin=True))})
        if base.status != OK:
            out = Outcome(FAILED, value=empty, reason=f"발급 기록을 읽지 못해 보내지 않았습니다. {base.reason}".strip()[:300], attempts=0)
            self._record(CONN_WRITE, FAILED, out.reason)
            return out
        out = self._post_codes(course, ym, names, base.value)
        self._record(CONN_WRITE, OK if out.status == OK else FAILED, out.reason)
        return out

    def _post_codes(self, course: str, ym: str, names: list, before: set) -> Outcome:
        n = len(names)
        none = CodeBatch(course=course, ym=ym, sent=n, not_issued=tuple(range(n)))
        body = {"course": course, "ym": ym, "people": [{"name": x} for x in names]}
        headers = {"accept": "application/json", "user-agent": "ga-app-connect/1", ADMIN_HEADER: self._admin_key}
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport) as c:
                r = c.request("POST", "/api/admin/codes", json=body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return Outcome(FAILED, value=none, reason=f"명함 사이트에 닿지 않아 보내지 못했습니다. {describe(e)}"[:300], attempts=1)
        except httpx.TransportError as e:
            return self._confirm_codes(course, ym, names, before, f"응답을 받지 못했습니다. {describe(e)}")
        if r.status_code >= 500:
            return self._confirm_codes(course, ym, names, before, f"명함 사이트가 {r.status_code} 를 돌려줬습니다")
        if r.status_code == 401:
            return Outcome(FAILED, value=none, reason="관리자 키가 맞지 않습니다 (401)", attempts=1)
        if r.status_code == 429:
            return Outcome(FAILED, value=none, reason="명함 사이트가 요청이 많다고 받지 않았습니다 (429). 잠시 뒤 다시 해 주세요", attempts=1)
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = None
        if r.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            err = err if isinstance(err, str) and re.fullmatch(r"[a-z_]{1,30}", err) else ""
            return Outcome(FAILED, value=none, reason=f"명함 사이트가 요청을 받지 않았습니다 ({r.status_code} {err})".replace(" )", ")"), attempts=1)
        rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return self._confirm_codes(course, ym, names, before, "명함 사이트의 응답을 읽지 못했습니다")
        batch = _match_codes(course, ym, names, rows, sure=True)
        return Outcome(OK, value=batch, reason=f"{len(batch.issued)}건 발급했습니다", attempts=1)

    def _confirm_codes(self, course: str, ym: str, names: list, before: set, why: str) -> Outcome:
        """결과를 모를 때 발급 기록을 한 번 읽는다. 다시 보내지 않는다."""
        n = len(names)
        try:
            raw = self._get_json("/api/admin/codes", admin=True)
        except Exception as e:
            return Outcome(UNKNOWN, value=CodeBatch(course=course, ym=ym, sent=n, unknown=tuple(range(n))),
                           reason=f"{why}. 결과를 확인하지 못했습니다. 명함 사이트 관리 화면의 발급 기록을 확인해 주세요 ({describe(e)})"[:300],
                           attempts=1)
        log = raw.get("log") if isinstance(raw.get("log"), list) else []
        new = [r for r in log if isinstance(r, dict) and isinstance(r.get("serial"), str) and r["serial"] not in before
               and str(r.get("course") or "").upper() == course and str(r.get("ym") or "") == ym]
        if not new:
            return Outcome(FAILED, value=CodeBatch(course=course, ym=ym, sent=n, not_issued=tuple(range(n)), confirmed_by_log=True),
                           reason=f"{why}. 발급 기록에 새 줄이 없어 발급되지 않은 것으로 확인했습니다"[:300], attempts=1)
        batch = _match_codes(course, ym, names, new, sure=False)
        status = UNKNOWN if batch.unknown or batch.extra_serials else OK
        tail = "발급 기록으로 반영을 확인했습니다" if status == OK else "발급 기록과 명단이 다 맞지 않습니다. 명함 사이트 관리 화면에서 확인해 주세요"
        return Outcome(status, value=batch, reason=f"{why}. {tail}"[:300], attempts=1)

    def _record(self, conn: str, status: str, reason: str = "") -> None:
        try:
            self.state.record(conn, status, reason=reason)
        except Exception:
            pass

    def _not_configured(self, conn: str = CONN_ADMIN, what: str = "관리자 목록을 열지 않습니다") -> Outcome:
        missing = [k for k, v in ((ENV_URL, self.base_url), (ENV_KEY, self._admin_key)) if not v]
        out = Outcome(NOT_CONFIGURED, reason=f"설정 안 됨: 환경 변수 {', '.join(missing)} 가 없어 {what}")
        try:
            self.state.record(conn, NOT_CONFIGURED, reason=out.reason)
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


def _code_hash(code) -> str | None:
    from app.engine.education import card_code_hash
    return card_code_hash(code) if isinstance(code, str) and code.strip() else None


def _site_codes(raw: dict) -> list:
    """사이트 발급 기록을 SiteCode 로 바꾼다. 이름, 연락처, 코드 원문은 옮기지 않는다."""
    out = []
    for r in raw.get("log") or []:
        if isinstance(r, dict) and isinstance(r.get("serial"), str) and r["serial"].strip():
            out.append(SiteCode(serial=r["serial"].strip(), course=str(r.get("course") or ""), ym=str(r.get("ym") or ""),
                                code_sha256=_code_hash(r.get("code")), at=r.get("at") if isinstance(r.get("at"), str) else None,
                                status=r.get("status") if isinstance(r.get("status"), str) else None))
    return out


def _match_codes(course: str, ym: str, names: list, rows: list, *, sure: bool) -> CodeBatch:
    """사이트가 돌려준 줄을 보낸 명단의 자리에 맞춘다. 사이트는 차례대로 만들고 이름이 빈 사람만 건너뛴다.
    이름은 맞춰 보는 데만 쓰고 결과에 남기지 않는다. sure 가 참이면 맞추지 못한 자리는 만들지 않은 것, 거짓이면 모르는 것이다."""
    want = [_norm_name(x) for x in names]
    issued, skipped, extra = [], [], []
    i = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        serial, code = r.get("serial"), r.get("code")
        if not (isinstance(serial, str) and serial.strip() and isinstance(code, str) and code.strip()):
            continue
        got = _norm_name(r.get("name"))
        j = i
        while j < len(want) and want[j] != got:
            j += 1
        if j >= len(want):
            extra.append(serial.strip())
            continue
        skipped.extend(range(i, j))
        issued.append(IssuedCode(index=j, serial=serial.strip(), course=course, ym=ym, code_sha256=_code_hash(code),
                                 issued_at=r.get("at") if isinstance(r.get("at"), str) else None, code=code.strip()))
        i = j + 1
    rest = skipped + list(range(i, len(want)))
    return CodeBatch(course=course, ym=ym, sent=len(names), issued=tuple(issued),
                     not_issued=tuple(rest) if sure else (), unknown=() if sure else tuple(rest),
                     confirmed_by_log=not sure, extra_serials=tuple(extra))


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
