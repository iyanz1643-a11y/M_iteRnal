"""Claude API 를 부르는 얇은 층.

- 키는 환경 변수 ANTHROPIC_API_KEY 로만 받는다. 없으면 기능이 꺼지고 OFF_MESSAGE 를 돌려준다.
- 모델은 AI_MODEL, 기본값은 DEFAULT_MODEL 이다. API 버전은 AI_API_VERSION 이다.
- D-26 제안대로 기능별 월 한도를 둔다. 한도는 ai_budget 의 마지막 줄, 없으면 AI_MONTHLY_LIMIT_USD 다.
  부르기 전에 이번 달 쓴 비용과 이번 호출의 최대 추정 비용을 더해 한도를 넘으면 부르지 않는다.
- 부를 때마다 ai_call 에 쓴 토큰과 비용 추정을 남긴다. 키, 프롬프트 본문, 응답 본문은 남기지 않는다.
- 시간 제한 AI_TIMEOUT_SECONDS, 재시도 AI_MAX_RETRIES. 재시도는 시간 초과, 연결 실패, 408, 429, 5xx, 529 에서만 한다.
- AI_ 환경 변수가 없거나 비었거나 공백뿐이면 기본값을 쓴다. 숫자 자리에 숫자가 아니거나 범위 밖의 값이 오면
  기본값을 쓰고 변수 이름만 담아 경고를 한 번 남긴다. 값 자체는 로그에 남기지 않는다.
- 시험에서는 FakeTransport 를 넣는다. 실제 API 는 부르지 않는다.

anthropic 패키지는 설치되어 있지 않아 httpx 로 Messages API 를 직접 부른다.
모델 이름, 가격, API 버전은 2026-11-15 에 공식 문서의 모델 개요와 API 버전 쪽에서 확인했다.
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from app import db
from app.ai import store

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_API_VERSION = "2026-10-21"
DEFAULT_LIMIT_USD = 20.0            # 기능별 월 한도의 기본값. D-26 의 금액은 정해지지 않아 둔 가정이다
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 2

# 백만 토큰당 달러. (입력, 출력). 표에 없는 모델은 PRICE_FALLBACK 으로 넉넉하게 잡는다.
PRICES = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
}
PRICE_FALLBACK = (8.0, 40.0)

FEATURES = {
    "plan_interpret": "시상안 해석 보조",
    "plan_qa": "시상안 묻고 답하기",
    "review": "7나이츠 심사",
    "report": "보장분석 리포트 초안",
    "cardnews": "카드뉴스 초안",
    "monthly_report": "월간 보고서 초안",
}

OFF_MESSAGE = "AI 기능이 꺼져 있습니다"
BUDGET_MESSAGE = "이번 달 AI 사용 한도에 닿아 멈췄습니다. 사람이 처리해 주세요."
ERROR_MESSAGE = "AI 를 부르지 못했습니다. 잠시 뒤 다시 시도하거나 사람이 처리해 주세요."

RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}

log = logging.getLogger(__name__)
_warned: set = set()


def env_str(name: str, default: str | None = None) -> str | None:
    """없거나 비었거나 공백뿐이면 default. 앞뒤 공백은 지운다."""
    raw = (os.environ.get(name) or "").strip()
    return raw or default


def env_number(name: str, default, cast=float, *, low: float = 0.0, low_open: bool = False):
    """숫자 환경 변수. 비면 default, 숫자가 아니거나 유한하지 않거나 low 아래면 default 와 경고 한 번.

    low_open 이면 low 자체도 받지 않는다. 한도가 nan 이면 비교가 늘 거짓이 되어 멈추지 못하므로 막는다.
    """
    raw = env_str(name)
    if raw is None:
        return default
    try:
        val = cast(raw)
    except (TypeError, ValueError):
        val = None
    if val is not None and math.isfinite(val) and (val > low if low_open else val >= low):
        return val
    if (name, raw) not in _warned:
        _warned.add((name, raw))
        log.warning("환경 변수 %s 의 값을 쓸 수 없어 기본값 %s 을 씁니다", name, default)
    return default


class TransportError(Exception):
    """API 호출 실패. 메시지에는 종류와 상태 번호만 둔다. 응답 본문과 키는 담지 않는다."""

    def __init__(self, kind: str, attempts: int = 1):
        super().__init__(kind)
        self.kind = kind
        self.attempts = attempts


@dataclass
class Reply:
    status: str                 # ok / off / budget / error
    text: str = ""
    message: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    call_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def price(model: str) -> tuple:
    env_in, env_out = env_number("AI_PRICE_INPUT_PER_MTOK", None), env_number("AI_PRICE_OUTPUT_PER_MTOK", None)
    if env_in is not None and env_out is not None:
        return env_in, env_out
    return PRICES.get(model, PRICE_FALLBACK)


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = price(model)
    return round(input_tokens * p_in / 1_000_000 + output_tokens * p_out / 1_000_000, 6)


def estimate_tokens(text: str) -> int:
    """넉넉한 추정. 한국어는 글자 하나가 토큰 하나를 넘는 일이 드물어 글자 수를 그대로 쓴다."""
    return len(text or "")


# ------------------------------------------------------------------ 보내는 층
class HttpTransport:
    """Messages API 를 httpx 로 부른다. 생각 블록은 버리고 text 블록만 잇는다."""

    def __init__(self, api_key: str, *, timeout: float | None = None, retries: int | None = None, api_version: str | None = None,
                 http=None, sleep=time.sleep):
        import httpx

        self._key = api_key
        self.timeout = timeout if timeout is not None else env_number("AI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT, low_open=True)
        self.retries = retries if retries is not None else env_number("AI_MAX_RETRIES", DEFAULT_RETRIES, int)
        self.api_version = api_version or env_str("AI_API_VERSION", DEFAULT_API_VERSION)
        self._httpx = httpx
        self._http = http or httpx.Client(timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)))
        self._sleep = sleep

    def send(self, *, model: str, system: str, user: str, max_tokens: int) -> dict:
        httpx = self._httpx
        headers = {"x-api-key": self._key, "anthropic-version": self.api_version, "content-type": "application/json"}
        body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
        attempts = 0
        while True:
            attempts += 1
            wait = None
            try:
                r = self._http.post(API_URL, headers=headers, json=body, timeout=self.timeout)
            except httpx.TimeoutException:
                kind = "timeout"
            except httpx.TransportError:
                kind = "connect"
            else:
                if r.status_code == 200:
                    data = r.json()
                    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                    usage = data.get("usage") or {}
                    return {"text": text, "model": data.get("model") or model, "attempts": attempts,
                            "input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": int(usage.get("output_tokens") or 0),
                            "stop_reason": data.get("stop_reason")}
                kind = f"http_{r.status_code}"
                if r.status_code not in RETRY_STATUS:
                    raise TransportError(kind, attempts)
                ra = r.headers.get("retry-after")
                if ra and ra.replace(".", "", 1).isdigit():
                    wait = min(float(ra), 30.0)
            if attempts > self.retries:
                raise TransportError(kind, attempts)
            self._sleep(wait if wait is not None else min(2 ** (attempts - 1), 8))


class FakeTransport:
    """시험과 회귀 평가용. replies 는 글의 목록이거나 (system, user) 를 받아 글을 돌려주는 함수다.

    글 대신 예외를 넣으면 그 차례에 그 예외를 낸다. 받은 요청은 calls 에 남는다.
    """

    def __init__(self, replies=None, *, input_tokens: int = 100, output_tokens: int = 50):
        self.replies = replies if replies is not None else []
        self.calls: list = []
        self.input_tokens, self.output_tokens = input_tokens, output_tokens

    def send(self, *, model: str, system: str, user: str, max_tokens: int) -> dict:
        self.calls.append({"model": model, "system": system, "user": user, "max_tokens": max_tokens})
        if callable(self.replies):
            out = self.replies(system, user)
        else:
            if not self.replies:
                raise TransportError("fake_empty")
            out = self.replies.pop(0)
        if isinstance(out, Exception):
            raise out
        return {"text": out, "model": model, "attempts": 1, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


# ------------------------------------------------------------------ 한도와 기록
def this_ym() -> str:
    return datetime.now().strftime("%Y%m")


def limit_for(con, feature: str) -> float:
    store.ensure(con)
    r = con.execute("SELECT monthly_limit_usd FROM ai_budget WHERE feature=? ORDER BY budget_id DESC LIMIT 1", (feature,)).fetchone()
    if r:
        return float(r[0])
    return float(env_number("AI_MONTHLY_LIMIT_USD", DEFAULT_LIMIT_USD))


def set_limit(con, feature: str, usd: float, by: str) -> None:
    """메인 관리자가 기능별 월 한도를 바꾼다. 지우지 않고 줄을 더한다."""
    if feature not in FEATURES:
        raise ValueError("없는 기능입니다")
    if usd < 0:
        raise ValueError("한도는 0 이상이어야 합니다")
    store.ensure(con)
    con.execute("INSERT INTO ai_budget(feature, monthly_limit_usd, set_by, set_at) VALUES(?,?,?,?)", (feature, usd, by, db.now()))
    con.commit()


def spent(con, feature: str, ym: str | None = None) -> float:
    store.ensure(con)
    r = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ai_call WHERE feature=? AND ym=?", (feature, ym or this_ym())).fetchone()
    return float(r[0])


def usage(con, ym: str | None = None) -> list:
    """화면용. 기능마다 이번 달 호출 수, 토큰, 쓴 비용 추정, 한도, 막힌 수."""
    store.ensure(con)
    ym = ym or this_ym()
    out = []
    for f, ko in FEATURES.items():
        r = con.execute("""SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) tin, COALESCE(SUM(output_tokens),0) tout,
                                  COALESCE(SUM(cost_usd),0) usd, COALESCE(SUM(status='budget'),0) blocked, COALESCE(SUM(status='error'),0) errors
                           FROM ai_call WHERE feature=? AND ym=?""", (f, ym)).fetchone()
        lim = limit_for(con, f)
        out.append({"feature": f, "name": ko, "ym": ym, "calls": r["n"], "input_tokens": r["tin"], "output_tokens": r["tout"],
                    "cost_usd": round(r["usd"], 4), "limit_usd": lim, "blocked": r["blocked"], "errors": r["errors"],
                    "reached": r["usd"] >= lim})
    return out


def _log(con, *, feature, ym, login_id, model, guide_version, input_hash, status, tin=0, tout=0, usd=0.0, attempts=0, error=None) -> int:
    cid = con.execute(
        """INSERT INTO ai_call(feature, ym, called_at, login_id, model, guide_version, input_hash, status, input_tokens, output_tokens,
                               cost_usd, attempts, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (feature, ym, db.now(), login_id, model, guide_version, input_hash, status, tin, tout, usd, attempts, error)).lastrowid
    con.commit()
    return cid


# ------------------------------------------------------------------ 부르는 쪽
@dataclass
class AIClient:
    """기능 코드가 쓰는 입구. transport 를 주지 않으면 키가 있을 때만 HttpTransport 를 만든다."""

    con: object
    transport: object = None
    model: str = field(default_factory=lambda: env_str("AI_MODEL", DEFAULT_MODEL))

    def __post_init__(self):
        store.ensure(self.con)
        if self.transport is None:
            key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
            if key:
                self.transport = HttpTransport(key)

    @property
    def enabled(self) -> bool:
        return self.transport is not None

    def call(self, feature: str, *, system: str, user: str, login_id: str | None = None, guide_version: str | None = None,
             input_hash: str | None = None, max_tokens: int = 1500, ym: str | None = None) -> Reply:
        if feature not in FEATURES:
            raise ValueError("없는 기능입니다")
        if not self.enabled:
            return Reply("off", message=OFF_MESSAGE)
        ym = ym or this_ym()
        est = cost(self.model, estimate_tokens(system) + estimate_tokens(user), max_tokens)
        if spent(self.con, feature, ym) + est > limit_for(self.con, feature):
            cid = _log(self.con, feature=feature, ym=ym, login_id=login_id, model=self.model, guide_version=guide_version,
                       input_hash=input_hash, status="budget")
            return Reply("budget", message=BUDGET_MESSAGE, model=self.model, call_id=cid)
        try:
            res = self.transport.send(model=self.model, system=system, user=user, max_tokens=max_tokens)
        except TransportError as e:
            cid = _log(self.con, feature=feature, ym=ym, login_id=login_id, model=self.model, guide_version=guide_version,
                       input_hash=input_hash, status="error", attempts=e.attempts, error=e.kind)
            return Reply("error", message=ERROR_MESSAGE, model=self.model, call_id=cid)
        model = res.get("model") or self.model
        usd = cost(model, res["input_tokens"], res["output_tokens"])
        cid = _log(self.con, feature=feature, ym=ym, login_id=login_id, model=model, guide_version=guide_version, input_hash=input_hash,
                   status="ok", tin=res["input_tokens"], tout=res["output_tokens"], usd=usd, attempts=res.get("attempts", 1))
        return Reply("ok", text=res["text"], input_tokens=res["input_tokens"], output_tokens=res["output_tokens"], cost_usd=usd,
                     model=model, call_id=cid)


def get_client(con, transport=None) -> AIClient:
    return AIClient(con, transport)
