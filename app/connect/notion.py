"""노션 읽기 전용 연결. D-13, D-115.

대표님이 시상안과 시책을 노션 데이터베이스 `메타리치 CA 시책·시상 DB (월별·주차별)` 에 정리해 두신다. 앱은 그 정리본을 읽어 온다.
이 파일은 읽는 일만 한다. 노션에 쓰는 호출은 코드에 없다. 확인 표시는 늘 unverified 이고 앱이 올리지 않는다.

흐름
1. NotionClient 가 데이터 소스를 질의해 페이지 속성을 읽고, 페이지마다 본문 블록을 읽는다.
2. clean_page 가 정리된 dict 로 바꾼다. content_hash 는 정리된 내용의 해시다. 편집 시각만 바뀐 것은 다시 처리하지 않는다.
3. import_plan_pages 가 같은 content_hash 면 건너뛰고, 새 것이나 바뀐 것만 원본 JSON 을 data/raw 에 보관한 뒤 notion_plan_page 에 넣는다.
4. 연결 상태는 이름 notion 으로 StateStore 에 남긴다. 키가 없으면 설정 안 됨(키 없음)으로 끝나고 다른 일은 그대로 돈다.

읽기 전용을 지키는 방법
- 요청은 _ALLOWED 목록에 있는 것만 나간다. 페이지 읽기, 블록 자식 읽기, 데이터 소스 질의뿐이다. 데이터 소스 질의는 POST 이지만 읽기다.
- 목록에 없는 방법이나 주소는 보내기 전에 NotionReadOnlyError 로 막는다. 클라이언트에는 쓰는 메서드가 없다.

키
- 환경 변수 NOTION_TOKEN 으로만 받는다. 파일, 로그, 오류 글, 연결 상태, DB 어디에도 남기지 않는다. repr 에도 없다.
- 오류 글에는 우리가 정한 문장과 상태 번호만 넣고, 노션 응답 본문의 글은 넣지 않는다. 혹시 키 값이 글에 섞이면 _scrub 이 가린다.
- NOTION_API_VERSION 은 Notion-Version 머리글이다. 비우면 DEFAULT_API_VERSION 이다. 2025-09-03 이상이면 data_sources 주소로, 이전이면 databases 주소로 질의한다.
- NOTION_PLAN_DB 는 읽을 데이터 소스 ID 다. collection:// 앞붙임은 떼고 읽는다. 비우면 DEFAULT_PLAN_DB 다. ID 는 키가 아니다.

시간 제한과 재시도
- 요청마다 retry.call_with_retry 로 부른다. 시간 초과, 연결 끊김, 429, 5xx 는 다시 하고, 401, 403, 404, 400 은 다시 하지 않는다.
- 가져오기 전체에는 시간 예산이 있다. 넘으면 읽은 만큼만 넣고 실패 사유를 남긴다. 월 마감과 다른 기능은 이 일과 따로 돈다.

가정
- 열 이름은 PROPERTY_NAMES 의 한국어 이름이다. 띄어쓰기와 가운뎃점은 무시하고 맞춘다. 없는 열은 missing_columns 로 알린다.
- 원문이식은 체크박스면 그 값, 아니면 글이 비어 있지 않고 부정어로 시작하지 않을 때 참이다.
- 연월은 연월 열에서, 없으면 시책기간 글에서 YYYYMM 을 읽는다.
- 원본링크는 http 나 https 로 시작할 때만 담는다.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

from app import db
from app.connect import tables
from app.connect.base import FAILED, NOT_CONFIGURED, OK, VIA_NOTION, NotConfigured, Outcome, RawFile
from app.connect.raw_store import default_data_dir, store_raw
from app.connect.retry import Retryable, call_with_retry
from app.connect.sources import infer_ym
from app.connect.state import StateStore

ENV_TOKEN = "NOTION_TOKEN"
ENV_VERSION = "NOTION_API_VERSION"
ENV_DB = "NOTION_PLAN_DB"

API_BASE = "https://api.notion.com/v1"
DEFAULT_API_VERSION = "2025-09-03"
DATA_SOURCE_VERSION = "2025-09-03"       # 이 버전부터 질의 주소가 data_sources 다
DEFAULT_PLAN_DB = "4215b77e-5d18-8186-934f-000b5cabc040"
USER_AGENT = "ga-ops-connect"

CONN_NAME = "notion"
KIND_RAW = "notion_plan"                 # data/raw 아래 폴더 이름
VERIFY_STATUS = "unverified"             # 앱에서 원문과 한 번 더 대조하기 전까지 늘 이 값이다

MAX_PAGES = 1000                         # 한 번에 읽는 페이지 수의 상한
MAX_BLOCKS = 2000                        # 페이지 하나에서 읽는 블록 수의 상한
MAX_DEPTH = 4                            # 접힌 블록을 내려가는 깊이
MAX_BODY_CHARS = 200_000
MAX_REPLY_BYTES = 10 * 1024 * 1024
BUDGET_SECONDS = 600.0                   # 가져오기 한 번의 시간 예산

BEGIN, END = "-- [notion:begin]", "-- [notion:end]"
TABLE = "notion_plan_page"

# 정리된 칸 이름 -> 노션 열 이름. 앞의 것이 기본이다
PROPERTY_NAMES = {
    "title": ("제목",),
    "insurer": ("보험사",),
    "kind": ("구분",),
    "ym": ("연월",),
    "period_text": ("시책기간",),
    "doc_type": ("자료유형",),
    "week": ("주차",),
    "products": ("대상상품",),
    "payback_text": ("지급·환수조건",),
    "core_text": ("핵심시책",),
    "source_link": ("원본링크",),
    "transcribed": ("원문이식",),
    "processed_on": ("처리일",),
}
CLEAN_FIELDS = ("page_id", "title", "insurer", "kind", "doc_type", "ym", "week", "period_text", "products", "payback_text", "core_text",
                "source_link", "transcribed", "processed_on", "last_edited", "body_text", "content_hash")
HASHED_OUT = ("last_edited", "content_hash")     # 내용 해시에 넣지 않는 칸. 편집 시각만 바뀐 것은 같은 내용이다


# ------------------------------------------------------------------ 오류
class NotionError(Exception):
    """노션 호출 실패. 글에는 키와 응답 본문을 넣지 않는다."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class NotionAuthError(NotionError):
    """키가 틀렸거나 통합에 그 데이터베이스가 공유되지 않았다. 다시 해도 소용없다."""


class NotionReadOnlyError(NotionError):
    """읽기 목록에 없는 요청. 보내지 않는다."""


class NotionRetryable(NotionError, Retryable):
    """429 와 5xx. 다시 해 볼 만하다."""


# ------------------------------------------------------------------ 바닥 전송
class Reply(NamedTuple):
    status: int
    headers: dict
    body: bytes


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> Reply:
    """표준 라이브러리로 보낸다. 시험은 같은 꼴의 가짜 함수를 넣는다."""
    if not url.startswith(API_BASE + "/"):
        raise NotionReadOnlyError("노션 API 주소가 아닌 곳으로는 보내지 않습니다")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Reply(r.status, dict(r.headers.items()), r.read(MAX_REPLY_BYTES + 1))
    except urllib.error.HTTPError as e:
        try:
            data = e.read(MAX_REPLY_BYTES)
        except Exception:
            data = b""
        return Reply(e.code, dict(e.headers.items()) if e.headers else {}, data)
    except (OSError, http.client.HTTPException) as e:
        if isinstance(e, TimeoutError | socket.timeout) or (isinstance(e, urllib.error.URLError) and isinstance(e.reason, TimeoutError | socket.timeout)):
            raise TimeoutError("노션 응답 시간이 넘었습니다") from None
        raise ConnectionError("노션에 연결하지 못했습니다") from None


# 보낼 수 있는 요청. 방법과 주소 꼴이 모두 맞아야 한다
_ID = r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}"
_ALLOWED = (
    ("GET", re.compile(rf"^/pages/{_ID}$")),
    ("GET", re.compile(rf"^/blocks/{_ID}/children$")),
    ("POST", re.compile(rf"^/(?:data_sources|databases)/{_ID}/query$")),      # 질의는 POST 이지만 읽기다
)
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")
_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_id(value) -> str:
    """collection:// 앞붙임과 하이픈 유무를 맞춰 소문자 UUID 로."""
    s = str(value or "").strip().lower()
    if s.startswith("collection://"):
        s = s[len("collection://"):]
    s = s.replace("-", "")
    if not re.fullmatch(r"[0-9a-f]{32}", s):
        raise NotionError("노션 ID 꼴이 아닙니다. 32자리 16진수여야 합니다")
    return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"


def _plain_reason(reason: str) -> str:
    """call_with_retry 가 붙인 오류 종류 이름을 우리 문장 앞에서는 뗀다."""
    for prefix in ("NotionAuthError: ", "NotionRetryable: ", "NotionReadOnlyError: ", "NotionError: "):
        if reason.startswith(prefix):
            return reason[len(prefix):]
    return reason


# ------------------------------------------------------------------ 클라이언트
class NotionClient:
    def __init__(self, token: str | None = None, *, version: str | None = None, timeout: float = 15.0, attempts: int = 3,
                 backoff: float = 1.0, transport=None, sleep=None, min_interval: float = 0.35, clock=time.monotonic):
        self._token = (token if token is not None else os.environ.get(ENV_TOKEN, "")).strip()
        self.version = (version or os.environ.get(ENV_VERSION) or DEFAULT_API_VERSION).strip()
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff
        self.transport = transport or urllib_transport
        self._sleep = sleep or time.sleep
        self.min_interval = min_interval           # 노션은 초당 평균 3건까지다. 요청 사이에 이만큼 쉰다
        self._clock = clock
        self._last = None

    def __repr__(self) -> str:   # 키가 로그에 찍히지 않게 한다
        return f"NotionClient(version={self.version!r}, token={'set' if self._token else 'unset'})"

    __str__ = __repr__

    @property
    def configured(self) -> bool:
        return bool(self._token)

    @property
    def use_data_sources(self) -> bool:
        return self.version >= DATA_SOURCE_VERSION

    # ---------------------------------------------------------- 한 번 보내기
    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token and self._token in text else text

    def _headers(self, has_body: bool) -> dict:
        if not _TOKEN_RE.match(self._token):
            raise NotionAuthError("키의 꼴이 올바르지 않습니다. 공백이나 줄바꿈이 없어야 합니다")
        if not _VERSION_RE.match(self.version):
            raise NotionError("NOTION_API_VERSION 은 YYYY-MM-DD 꼴이어야 합니다")
        h = {"Authorization": f"Bearer {self._token}", "Notion-Version": self.version, "Accept": "application/json", "User-Agent": USER_AGENT}
        if has_body:
            h["Content-Type"] = "application/json"
        return h

    @staticmethod
    def _error_code(reply: Reply) -> str:
        try:
            code = json.loads(reply.body.decode("utf-8")).get("code")
        except Exception:
            return ""
        return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else ""

    def _once(self, method: str, path: str, query: dict | None, body: dict | None) -> dict:
        if not self._token:
            raise NotConfigured(f"키 없음: 환경 변수 {ENV_TOKEN} 이 필요합니다")
        if not any(m == method and rx.match(path) for m, rx in _ALLOWED):
            raise NotionReadOnlyError("읽기 목록에 없는 요청이라 보내지 않습니다")
        url = API_BASE + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        if self._last is not None and self.min_interval > 0:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        try:
            reply = self.transport(method, url, self._headers(data is not None), data, self.timeout)
        finally:
            self._last = self._clock()
        s = reply.status
        if 200 <= s < 300:
            try:
                out = json.loads(reply.body.decode("utf-8"))
            except ValueError:
                raise NotionError("응답을 읽지 못했습니다", s) from None
            if not isinstance(out, dict):
                raise NotionError("응답의 꼴이 다릅니다", s)
            return out
        if s == 401:
            raise NotionAuthError("인증 실패(401): NOTION_TOKEN 키가 올바른지 확인해 주세요", s)
        if s == 403:
            raise NotionAuthError("권한 없음(403): 통합에 이 데이터베이스를 연결했는지 확인해 주세요", s)
        if s == 404:
            raise NotionError("찾지 못함(404): 데이터베이스나 페이지 ID 와 통합 연결을 확인해 주세요", s)
        if s == 429:
            raise NotionRetryable("요청이 너무 많습니다(429)", s)
        if s >= 500:
            raise NotionRetryable(f"노션 서버 오류({s})", s)
        code = self._error_code(reply)
        raise NotionError(f"노션이 요청을 받지 않았습니다({s}{', ' + code if code else ''})", s)

    def _request(self, method: str, path: str, *, query: dict | None = None, body: dict | None = None) -> dict:
        """시간 제한과 재시도 안에서 한 번 보낸다. 실패하면 키를 가린 사유로 NotionError 나 NotConfigured 를 던진다."""
        out = call_with_retry(lambda: self._once(method, path, query, body), attempts=self.attempts, timeout=self.timeout + 5,      # 소켓 시간 제한이 먼저 나오게 한다
                              backoff=self.backoff, sleep=self._sleep)
        if out.ok:
            return out.value
        reason = self._scrub(_plain_reason(out.reason))
        if out.status == NOT_CONFIGURED:
            raise NotConfigured(reason)
        err = NotionError(reason)
        err.attempts = out.attempts
        raise err

    # ---------------------------------------------------------- 읽기
    def query_pages(self, data_source_id: str | None = None, *, max_pages: int = MAX_PAGES) -> tuple:
        """데이터 소스의 페이지를 편집 시각이 늦은 것부터 읽는다. (페이지 목록, 상한에 걸려 남았는가)."""
        ds = normalize_id(data_source_id or os.environ.get(ENV_DB) or DEFAULT_PLAN_DB)
        base = "data_sources" if self.use_data_sources else "databases"
        pages, cursor, seen = [], None, set()
        while True:
            body = {"page_size": 100, "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}]}
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", f"/{base}/{ds}/query", body=body)
            pages += [p for p in data.get("results") or [] if isinstance(p, dict) and p.get("object") == "page"]
            more = bool(data.get("has_more")) and isinstance(data.get("next_cursor"), str)
            if len(pages) >= max_pages:
                return pages[:max_pages], more or len(pages) > max_pages
            cursor = data.get("next_cursor") if more else None
            if not cursor or cursor in seen:
                return pages, False
            seen.add(cursor)

    def get_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{normalize_id(page_id)}")

    def block_children(self, block_id: str) -> list:
        """블록 하나의 바로 아래 자식 전부. 쪽을 넘겨 읽는다."""
        bid, cursor, out, seen = normalize_id(block_id), None, [], set()
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            data = self._request("GET", f"/blocks/{bid}/children", query=query)
            out += [b for b in data.get("results") or [] if isinstance(b, dict)]
            cursor = data.get("next_cursor") if data.get("has_more") else None
            if not isinstance(cursor, str) or cursor in seen:
                return out
            seen.add(cursor)

    def read_body(self, page_id: str, *, max_depth: int = MAX_DEPTH, max_blocks: int = MAX_BLOCKS) -> tuple:
        """페이지 본문을 자식까지 내려가며 읽는다. (블록 트리, 상한에 걸려 잘렸는가). 트리의 각 블록에 children 이 붙는다."""
        state = {"n": 0, "cut": False}

        def walk(bid: str, depth: int) -> list:
            out = []
            for b in self.block_children(bid):
                if state["n"] >= max_blocks:
                    state["cut"] = True
                    break
                state["n"] += 1
                node = dict(b)
                if b.get("has_children") and b.get("type") not in ("child_page", "child_database") and isinstance(b.get("id"), str):
                    if depth + 1 >= max_depth:
                        state["cut"] = True
                    else:
                        node["children"] = walk(b["id"], depth + 1)
                out.append(node)
            return out

        tree = walk(page_id, 0)
        return tree, state["cut"]


# ------------------------------------------------------------------ 정리
def _nfc(s) -> str:
    return unicodedata.normalize("NFC", str(s or ""))


def _key(name) -> str:
    return re.sub(r"[\s·.,/_\-()]+", "", _nfc(name)).lower()


def plain(rich) -> str:
    return "".join(str(t.get("plain_text") or "") for t in rich or [] if isinstance(t, dict))


def _date_text(v) -> str:
    if not isinstance(v, dict) or not v.get("start"):
        return ""
    return f"{v['start']} ~ {v['end']}" if v.get("end") else str(v["start"])


def prop_text(p) -> str:
    """속성 하나를 글로. 사람, 파일, 관계는 읽지 않는다."""
    if not isinstance(p, dict):
        return ""
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return plain(v).strip()
    if t in ("select", "status"):
        return str((v or {}).get("name") or "").strip()
    if t == "multi_select":
        return ", ".join(str(x.get("name") or "").strip() for x in v or [] if isinstance(x, dict) and x.get("name"))
    if t == "date":
        return _date_text(v)
    if t == "number":
        return "" if v is None else f"{v:g}"
    if t == "url":
        return str(v or "").strip()
    if t == "checkbox":
        return "예" if v else "아니오"
    if t == "formula" and isinstance(v, dict) and v.get("type"):
        return prop_text({"type": v["type"], v["type"]: v.get(v["type"])})
    return ""


_NO_EXACT = ("n", "no", "x", "0", "false")
_NO_PREFIX = ("미", "아니", "대기")


def _truthy(p) -> bool:
    if isinstance(p, dict) and p.get("type") == "checkbox":
        return bool(p.get("checkbox"))
    s = prop_text(p).strip().lower()
    return bool(s) and s not in _NO_EXACT and not s.startswith(_NO_PREFIX)


def _props_by_key(page: dict) -> dict:
    return {_key(name): p for name, p in (page.get("properties") or {}).items() if isinstance(p, dict)}


def missing_columns(page: dict) -> list:
    have = _props_by_key(page)
    return [names[0] for field, names in PROPERTY_NAMES.items() if field != "title" and not any(_key(n) in have for n in names)]


def _lines(blocks: list, depth: int = 0) -> list:
    out, n = [], 0
    for b in blocks:
        t = b.get("type")
        d = b.get(t) if isinstance(b.get(t), dict) else {}
        n = n + 1 if t == "numbered_list_item" else 0
        if t == "table_row":
            s = " | ".join(plain(c) for c in d.get("cells") or [])
        elif t in ("bookmark", "embed", "link_preview"):
            s = str(d.get("url") or "")
        else:
            s = plain(d.get("rich_text"))
        s = _nfc(s).strip()
        if s:
            pre = {"bulleted_list_item": "- ", "heading_1": "# ", "heading_2": "## ", "heading_3": "### "}.get(t, "")
            if t == "numbered_list_item":
                pre = f"{n}. "
            elif t == "to_do":
                pre = "[x] " if d.get("checked") else "[ ] "
            out.append("  " * depth + pre + s)
        out += _lines(b.get("children") or [], depth + 1)
    return out


def body_text_of(blocks: list) -> str:
    """본문 글자 전체. 접힌 블록 안의 글자까지 들여써서 잇는다."""
    return "\n".join(_lines(blocks))[:MAX_BODY_CHARS]


def content_hash(clean: dict) -> str:
    payload = {k: v for k, v in clean.items() if k not in HASHED_OUT}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def clean_page(page: dict, blocks: list) -> dict:
    """노션 페이지 한 쪽을 정리된 dict 로. 사람 속성은 읽지 않는다."""
    props = _props_by_key(page)

    def get(field):
        for n in PROPERTY_NAMES[field]:
            if _key(n) in props:
                return props[_key(n)]
        return None

    title = prop_text(get("title"))
    if not title:
        title = next((plain(p.get("title")).strip() for p in (page.get("properties") or {}).values() if isinstance(p, dict) and p.get("type") == "title"), "")
    text = {f: _nfc(prop_text(get(f))) for f in PROPERTY_NAMES if f not in ("title", "transcribed")}
    link = text["source_link"]
    out = {
        "page_id": normalize_id(page.get("id")),
        "title": _nfc(title),
        "insurer": text["insurer"], "kind": text["kind"], "doc_type": text["doc_type"],
        "ym": infer_ym(text["ym"]) or infer_ym(text["period_text"]),
        "week": text["week"], "period_text": text["period_text"], "products": text["products"],
        "payback_text": text["payback_text"], "core_text": text["core_text"],
        "source_link": link if re.match(r"^https?://", link, re.I) else "",
        "transcribed": _truthy(get("transcribed")),
        "processed_on": text["processed_on"],
        "last_edited": str(page.get("last_edited_time") or ""),
        "body_text": body_text_of(blocks),
    }
    out["content_hash"] = content_hash(out)
    return out


# ------------------------------------------------------------------ 보관과 넣기
def ensure_table(con) -> None:
    tables.ensure_block(con, BEGIN, END, (TABLE,))


def _canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")


def upsert_page(con, clean: dict, raw_path: str | None, now: str) -> None:
    with tables.write(con, "notion_page"):
        con.execute(
            f"""INSERT INTO {TABLE}(page_id, title, insurer, kind, doc_type, ym, week, period_text, products, payback_text, core_text,
                    source_link, transcribed, processed_on, last_edited, body_text, content_hash, raw_path, verify_status,
                    first_seen_at, changed_at, fetched_at, gone_at)
                VALUES(:page_id, :title, :insurer, :kind, :doc_type, :ym, :week, :period_text, :products, :payback_text, :core_text,
                    :source_link, :transcribed, :processed_on, :last_edited, :body_text, :content_hash, :raw_path, :verify, :now, :now, :now, NULL)
                ON CONFLICT(page_id) DO UPDATE SET
                    title=excluded.title, insurer=excluded.insurer, kind=excluded.kind, doc_type=excluded.doc_type, ym=excluded.ym,
                    week=excluded.week, period_text=excluded.period_text, products=excluded.products, payback_text=excluded.payback_text,
                    core_text=excluded.core_text, source_link=excluded.source_link, transcribed=excluded.transcribed,
                    processed_on=excluded.processed_on, last_edited=excluded.last_edited, body_text=excluded.body_text,
                    content_hash=excluded.content_hash, raw_path=excluded.raw_path, verify_status=:verify,
                    changed_at=:now, fetched_at=:now, gone_at=NULL""",
            {**clean, "transcribed": int(clean["transcribed"]), "raw_path": raw_path, "verify": VERIFY_STATUS, "now": now})


def touch_page(con, page_id: str, last_edited: str, now: str) -> None:
    """내용이 같아 건너뛴 쪽. 읽은 시각과 편집 시각만 고친다. 사라졌다 돌아왔으면 표시를 지운다."""
    with tables.write(con, "notion_page"):
        con.execute(f"UPDATE {TABLE} SET fetched_at=?, last_edited=?, gone_at=NULL WHERE page_id=?", (now, last_edited, page_id))


def import_plan_pages(con, client: NotionClient | None = None, *, data_dir=None, state: StateStore | None = None, max_pages: int = MAX_PAGES,
                      budget_seconds: float = BUDGET_SECONDS, clock=time.monotonic) -> Outcome:
    """정리본을 읽어 notion_plan_page 에 넣는다. 예외는 위로 새지 않고 Outcome 으로 돌아온다.

    Outcome.status 는 ok, failed, not_configured. value 는 건수만 담은 dict 다(읽은 쪽, 새 것, 바뀐 것, 그대로, 실패, 사라진 것, 빠진 열 이름).
    페이지가 하나라도 못 읽혔거나 시간 예산과 상한에 걸려 다 못 읽었으면 failed 이고, 넣은 것은 그대로 남는다.
    """
    client = client or NotionClient()
    state = (state or StateStore()).bind(con)
    summary = {"seen": 0, "new": 0, "changed": 0, "unchanged": 0, "failed": 0, "gone": 0, "trashed": 0, "truncated_pages": 0, "missing_columns": []}

    def done(status: str, reason: str = "") -> Outcome:
        reason = client._scrub(reason)
        try:
            state.record(CONN_NAME, status, reason=reason, detail=summary)
        except Exception:      # 상태 기록 실패로 다른 일이 멈추면 안 된다
            pass
        return Outcome(status, value=summary, reason=reason)

    if not client.configured:
        return done(NOT_CONFIGURED, f"키 없음: 환경 변수 {ENV_TOKEN} 이 필요합니다. 다른 기능은 그대로 돕니다")
    try:
        ensure_table(con)
        pages, more = client.query_pages(max_pages=max_pages)
    except NotConfigured as e:
        return done(NOT_CONFIGURED, str(e))
    except Exception as e:
        return done(FAILED, _plain_reason(str(e)) or type(e).__name__)

    data_dir = data_dir or default_data_dir()
    t0, partial, first_fail, seen_ids = clock(), False, "", set()
    for page in pages:
        if page.get("in_trash") or page.get("archived"):
            summary["trashed"] += 1
            continue
        try:
            pid = normalize_id(page.get("id"))
        except NotionError:
            summary["failed"] += 1
            first_fail = first_fail or "페이지 ID 를 읽지 못했습니다"
            continue
        seen_ids.add(pid)
        if clock() - t0 > budget_seconds:
            partial = True
            break
        summary["seen"] += 1
        try:
            tree, cut = client.read_body(pid)
            clean = clean_page(page, tree)
            summary["truncated_pages"] += int(cut)
            for c in missing_columns(page):
                if c not in summary["missing_columns"]:
                    summary["missing_columns"].append(c)
            old = con.execute(f"SELECT content_hash FROM {TABLE} WHERE page_id=?", (pid,)).fetchone()
            now = db.now()
            if old and old[0] == clean["content_hash"]:          # 같은 해시는 다시 처리하지 않는다
                touch_page(con, pid, clean["last_edited"], now)
                summary["unchanged"] += 1
                continue
            raw = RawFile(name=f"notion_{clean['content_hash'][:16]}.json", data=_canonical({"page": page, "blocks": tree}), via=VIA_NOTION,
                          kind=KIND_RAW, period_ym=clean["ym"], meta={"content_hash": clean["content_hash"]})
            _, rel = store_raw(raw, data_dir)
            upsert_page(con, clean, rel, now)
            summary["changed" if old else "new"] += 1
        except NotionAuthError as e:                              # 키나 공유가 바뀌었다. 나머지도 같다
            summary["failed"] += 1
            return done(FAILED, _plain_reason(str(e)))
        except Exception as e:
            try:
                con.rollback()
            except Exception:
                pass
            summary["failed"] += 1
            first_fail = first_fail or (_plain_reason(str(e)) or type(e).__name__)

    complete = not more and not partial
    if complete:                                                  # 다 읽었을 때만 사라진 쪽을 표시한다. 지우지는 않는다
        try:
            gone = [r[0] for r in con.execute(f"SELECT page_id FROM {TABLE} WHERE gone_at IS NULL") if r[0] not in seen_ids]
            with tables.write(con, "notion_gone"):
                con.executemany(f"UPDATE {TABLE} SET gone_at=? WHERE page_id=?", [(db.now(), g) for g in gone])
            summary["gone"] = len(gone)
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
    problems = []
    if summary["failed"]:
        problems.append(f"{summary['failed']}쪽을 읽거나 넣지 못했습니다" + (f": {first_fail}" if first_fail else ""))
    if partial:
        problems.append(f"시간 예산 {budget_seconds:g}초를 넘어 일부만 읽었습니다")
    if more:
        problems.append(f"페이지가 {max_pages}개를 넘어 앞부분만 읽었습니다")
    return done(FAILED, " / ".join(problems)) if problems else done(OK)
