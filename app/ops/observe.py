"""관측: 요청 번호, 오류 기록, 느린 요청 기록. 개인정보는 남기지 않는다.

| 기록 | 남기는 칸 | 남기지 않는 것 |
| --- | --- | --- |
| 오류 error | 요청 번호, 방식, 경로 틀, 사용자 번호, 예외 종류, 코드 위치(파일:줄 함수) | 예외 메시지, 주소 뒤 꼬리(?...), 폼, 쿠키, 이름, 금액 |
| 느린 요청 slow_request | 요청 번호, 방식, 경로 틀, 사용자 번호, 응답 코드, 걸린 밀리초 | 위와 같다 |
| 서버 접근 기록 uvicorn.access | uvicorn 이 정한 칸. 주소는 경로까지만 | 주소 뒤 꼬리(?...). 초대 토큰 같은 값이 꼬리에 실린다 |

- 경로 틀은 /perf/contract/{contract_id} 처럼 라우터의 틀이다. 틀이 없으면 실제 경로의 숫자를 {n} 으로 바꾼다.
- 사용자 번호는 user_account.user_id 다. 사번과 이름은 남기지 않는다.
- 예외 메시지에는 화면 값이 섞일 수 있어 남기지 않는다. 다시 일으킬 때는 요청 번호와 코드 위치로 찾는다.
- 요청 번호는 응답 머리글 X-Request-ID 로 돌려주고 500 화면에도 보인다. 들어온 X-Request-ID 가 안전한 꼴이면 그대로 쓴다.
- 느린 요청의 기준은 APP_SLOW_MS 밀리초다. 기본 1000.
- 서버 접근 기록의 꼬리는 QueryTailFilter 가 지운다. setup_logging 이 uvicorn.access 와 uvicorn.error 에 달고, app.web.main 이 불러올 때와 켤 때 부른다. 그래서 uvicorn 을 어떤 인자로 띄워도 거른다.
- 기록은 logging 의 app.ops 이름으로 한 줄 JSON 을 낸다. 컨테이너에서는 표준 오류로 나가 운영 환경의 기록 수집기가 모은다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import traceback

log = logging.getLogger("app.ops")
RID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
NUM_RE = re.compile(r"/\d+(?=/|$)")
HEADER = "x-request-id"
QUERY_RE = re.compile(r"\?[^\s\"']*")
ACCESS_LOGGERS = ("uvicorn.access", "uvicorn.error")


def slow_ms() -> float:
    try:
        return float(os.environ.get("APP_SLOW_MS") or 1000)
    except ValueError:
        return 1000.0


def strip_query(text: str) -> str:
    """주소 뒤 꼬리(?...)를 지운다. 문자열 안 어디에 있든 공백이나 따옴표 앞까지 지운다."""
    return QUERY_RE.sub("", text) if "?" in text else text


class QueryTailFilter(logging.Filter):
    """uvicorn 기록에서 주소 뒤 꼬리를 지운다. 기록은 버리지 않고 값만 바꾼다.
    uvicorn 접근 기록은 args 꼴(주소, 방식, 경로와 꼬리, 판, 코드)로 오므로 칸 수와 종류는 그대로 두고 문자열 칸만 고친다."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(strip_query(a) if isinstance(a, str) else a for a in args)
        elif isinstance(args, dict) and args:
            record.args = {k: strip_query(v) if isinstance(v, str) else v for k, v in args.items()}
        elif isinstance(record.msg, str):
            record.msg = strip_query(record.msg)            # 이미 채워진 글. 꼴 문자열의 %s 는 건드리지 않는다
        return True


def install_query_filter() -> None:
    """uvicorn 기록기에 꼬리 거르개를 한 번만 단다."""
    for name in ACCESS_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, QueryTailFilter) for f in lg.filters):
            lg.addFilter(QueryTailFilter())


def setup_logging() -> None:
    """app 기록기에 처리기가 없으면 표준 오류로 한 줄씩 낸다. uvicorn 기록기는 처리기와 꼴을 그대로 두고 꼬리 거르개만 단다."""
    install_query_filter()
    root = logging.getLogger("app")
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(h)
        root.setLevel(logging.INFO)
        root.propagate = False


def emit(level: int, event: str, **fields) -> dict:
    rec = {"event": event, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **fields}
    log.log(level, json.dumps(rec, ensure_ascii=False, default=str))
    return rec


def route_path(scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path and path != "/{rest:path}":
        return path
    return NUM_RE.sub("/{n}", scope.get("path") or "")


def user_no(scope):
    sess = scope.get("session")
    uid = sess.get("uid") if isinstance(sess, dict) else None
    return uid if isinstance(uid, int) and not isinstance(uid, bool) else None


def where(exc: BaseException, limit: int = 6) -> list:
    """예외가 난 코드 위치. 앱 안의 줄만, 안쪽부터 limit 개. 값과 메시지는 넣지 않는다."""
    out = []
    for fr in traceback.extract_tb(exc.__traceback__):
        f = fr.filename.replace("\\", "/")
        if "/app/" in f and not f.endswith("/app/ops/observe.py"):
            out.append(f"{f[f.rindex('/app/') + 1:]}:{fr.lineno} {fr.name}")
    return out[-limit:]


def request_id(scope) -> str:
    state = scope.setdefault("state", {})
    rid = state.get("request_id")
    if rid:
        return rid
    sent = ""
    for k, v in scope.get("headers") or []:
        if k == HEADER.encode():
            sent = v.decode("latin-1")
            break
    rid = sent if RID_RE.match(sent) else secrets.token_hex(8)
    state["request_id"] = rid
    return rid


class ObserveMiddleware:
    """순수 ASGI 미들웨어. 세션 미들웨어 안쪽에 둬서 사용자 번호를 읽는다. 오류는 기록한 뒤 다시 던져 500 화면 처리기에 맡긴다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = request_id(scope)
        start = time.perf_counter()
        status = {"code": 500}

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers") or [])
                headers.append((HEADER.encode(), rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            emit(logging.ERROR, "error", rid=rid, method=scope.get("method"), path=route_path(scope), uid=user_no(scope),
                 error=type(exc).__name__, where=where(exc))
            raise
        finally:
            ms = (time.perf_counter() - start) * 1000
            if ms >= slow_ms():
                emit(logging.WARNING, "slow_request", rid=rid, method=scope.get("method"), path=route_path(scope), uid=user_no(scope),
                     status=status["code"], ms=round(ms))
