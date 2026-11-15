"""요청 횟수 제한. 로그인과 앞으로 만들 공개 화면(랜딩)에 IP 와 계정 기준으로 둔다.

| 규칙 | 열쇠 | 세는 것 | 기본 | 환경 변수 |
| --- | --- | --- | --- | --- |
| login_ip | 접속 IP | 로그인 실패 | 15분에 30번 | APP_RATE_LOGIN_IP |
| login_id | 넣은 아이디 | 로그인 실패 | 15분에 10번 | APP_RATE_LOGIN_ID |
| landing_ip | 접속 IP | 모든 요청 | 1분에 300번 | APP_RATE_LANDING_IP |

- 값은 "횟수/초" 꼴이다. 예: 30/900. 0/0 이면 그 규칙을 끈다.
- 로그인은 실패만 센다. 맞게 들어오는 사람은 막지 않고, 아이디를 바꿔 가며 찍어 보는 것과 한 아이디를 계속 찍는 것을 막는다.
  계정 잠금(비밀번호 5번 실패에 10분)은 DB 에 있고, 이 제한은 없는 아이디까지 막는 앞단이다.
- 공개 화면은 휴대폰 통신사와 사무실처럼 여러 사람이 IP 하나를 함께 쓰는 곳을 생각해 넉넉히 둔다. 지금은 초대 /invite 에 단다.
- 막히면 429 와 Retry-After 를 돌려준다. 막힌 요청은 DB 에 쓰지 않는다.
- IP 는 request.client.host 다. 프록시 뒤에서는 uvicorn 의 --proxy-headers 와 --forwarded-allow-ips 로 진짜 IP 를 받는다.
  앱이 X-Forwarded-For 를 직접 읽지 않는 것은 아무나 그 머리글을 꾸밀 수 있어서다.
- 저장은 프로세스 메모리다. uvicorn 워커를 여럿 띄우면 워커마다 따로 센다. 여럿 띄울 때 Store 를 공용 저장소 구현으로 바꾼다.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi import HTTPException, Request


class Store:
    """횟수 저장소의 인터페이스. 공용 저장소 구현은 이 네 가지만 맞추면 된다."""

    def hit(self, key: str, window: float, now: float | None = None) -> int:
        """한 번 세고, 창 안의 횟수를 돌려준다."""
        raise NotImplementedError

    def count(self, key: str, window: float, now: float | None = None) -> int:
        raise NotImplementedError

    def oldest(self, key: str, window: float, now: float | None = None) -> float | None:
        """창 안에서 가장 오래된 시각. 다시 시도할 때를 셀 때 쓴다."""
        raise NotImplementedError

    def clear(self, key: str | None = None) -> None:
        raise NotImplementedError


class MemoryStore(Store):
    """프로세스 안의 미끄럼 창. 열쇠가 너무 많아지면 오래된 것부터 버린다."""

    def __init__(self, max_keys: int = 50_000):
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()
        self.max_keys = max_keys

    def _prune(self, key, window, now):
        q = self._hits.get(key)
        if q is None:
            return None
        while q and q[0] <= now - window:
            q.popleft()
        if not q:
            del self._hits[key]
            return None
        return q

    def hit(self, key, window, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._prune(key, window, now)
            if q is None:
                if len(self._hits) >= self.max_keys:
                    self._hits.pop(next(iter(self._hits)))
                q = self._hits[key] = deque()
            q.append(now)
            return len(q)

    def count(self, key, window, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._prune(key, window, now)
            return len(q) if q else 0

    def oldest(self, key, window, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._prune(key, window, now)
            return q[0] if q else None

    def clear(self, key=None):
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


@dataclass
class Rule:
    name: str
    limit: int
    window: float

    @property
    def on(self) -> bool:
        return self.limit > 0 and self.window > 0


DEFAULTS = {"login_ip": "30/900", "login_id": "10/900", "landing_ip": "300/60"}
ENV = {"login_ip": "APP_RATE_LOGIN_IP", "login_id": "APP_RATE_LOGIN_ID", "landing_ip": "APP_RATE_LANDING_IP"}


def parse(text: str, default: str) -> tuple:
    try:
        n, sec = (text or default).split("/")
        return max(int(n), 0), max(float(sec), 0.0)
    except ValueError:
        n, sec = default.split("/")
        return int(n), float(sec)


def rule(name: str) -> Rule:
    n, sec = parse(os.environ.get(ENV[name], ""), DEFAULTS[name])
    return Rule(name, n, sec)


class Limiter:
    def __init__(self, store: Store | None = None):
        self.store = store or MemoryStore()

    def _key(self, r: Rule, who: str) -> str:
        return f"{r.name}:{who}"

    def blocked(self, name: str, who: str, now: float | None = None) -> int:
        """막혀 있으면 다시 시도할 수 있을 때까지의 초, 아니면 0."""
        r = rule(name)
        if not r.on or not who:
            return 0
        k = self._key(r, who)
        if self.store.count(k, r.window, now) < r.limit:
            return 0
        first = self.store.oldest(k, r.window, now)
        now = time.monotonic() if now is None else now
        start = first if first is not None else now
        return max(1, int(start + r.window - now) + 1)

    def hit(self, name: str, who: str, now: float | None = None) -> int:
        r = rule(name)
        if not r.on or not who:
            return 0
        return self.store.hit(self._key(r, who), r.window, now)

    def take(self, name: str, who: str, now: float | None = None) -> int:
        """한 번 세고 넘쳤으면 기다릴 초를 돌려준다. 모든 요청을 세는 규칙에 쓴다."""
        wait = self.blocked(name, who, now)
        if wait:
            return wait
        self.hit(name, who, now)
        return 0

    def reset(self, name: str | None = None, who: str | None = None) -> None:
        if name and who:
            self.store.clear(self._key(rule(name), who))
        else:
            self.store.clear()


LIMITER = Limiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def account_key(login_id: str) -> str:
    return (login_id or "").strip().lower()[:64]


def login_wait(request: Request, login_id: str) -> int:
    """로그인을 막아야 하면 기다릴 초. IP 와 아이디 가운데 긴 쪽이다."""
    return max(LIMITER.blocked("login_ip", client_ip(request)), LIMITER.blocked("login_id", account_key(login_id)))


def login_failed(request: Request, login_id: str) -> None:
    LIMITER.hit("login_ip", client_ip(request))
    LIMITER.hit("login_id", account_key(login_id))


def landing(request: Request) -> None:
    """공개 화면에 다는 의존성. Depends(ratelimit.landing). 넘치면 429."""
    wait = LIMITER.take("landing_ip", client_ip(request))
    if wait:
        raise HTTPException(429, "요청이 많아 잠시 막았습니다. 잠시 뒤에 다시 시도해 주세요.", headers={"Retry-After": str(wait)})
