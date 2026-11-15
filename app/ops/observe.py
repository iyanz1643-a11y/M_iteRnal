"""관측: 요청 번호, 오류 기록, 느린 요청 기록. 개인정보는 남기지 않는다.

| 기록 | 남기는 칸 | 남기지 않는 것 |
| --- | --- | --- |
| 오류 error | 요청 번호, 방식, 경로 틀, 사용자 번호, 예외 종류, 코드 위치(파일:줄 함수) | 예외 메시지, 주소 뒤 꼬리(?...), 폼, 쿠키, 이름, 금액 |
| 느린 요청 slow_request | 요청 번호, 방식, 경로 틀, 사용자 번호, 응답 코드, 걸린 밀리초 | 위와 같다 |
| 서버 접근 기록 uvicorn.access | uvicorn 이 정한 칸. 주소는 경로까지만. 접속 주소는 앞부분만 | 주소 뒤 꼬리(?...). 초대 토큰 같은 값이 꼬리에 실린다. 공개 화면의 접속 주소, 접속 포트 |

- 경로 틀은 /perf/contract/{contract_id} 처럼 라우터의 틀이다. 틀이 없으면 실제 경로의 숫자를 {n} 으로 바꾼다.
- 사용자 번호는 user_account.user_id 다. 사번과 이름은 남기지 않는다.
- 예외 메시지에는 화면 값이 섞일 수 있어 남기지 않는다. 다시 일으킬 때는 요청 번호와 코드 위치로 찾는다.
- 요청 번호는 응답 머리글 X-Request-ID 로 돌려주고 500 화면에도 보인다. 들어온 X-Request-ID 가 안전한 꼴이면 그대로 쓴다.
- 느린 요청의 기준은 APP_SLOW_MS 밀리초다. 기본 1000.
- 서버 접근 기록의 꼬리는 QueryTailFilter 가 지운다. setup_logging 이 uvicorn.access 와 uvicorn.error 에 달고, app.web.main 이 불러올 때와 켤 때 부른다. 그래서 uvicorn 을 어떤 인자로 띄워도 거른다.
- 접속 주소도 같은 거르개가 가린다. 공개 화면 /consult, /invite, /welcome 과 그 아래 경로는 접속 주소를 - 로 지운다. 상담 신청 화면이 접속 주소를 저장하지 않는다고 안내하기 때문이다.
- 그 밖의 요청은 APP_LOG_IP 를 따른다. 기본 prefix 는 앞부분만 남긴다. IPv4 는 /24, IPv6 는 /48 이고 포트는 지운다. none 은 모두 지운다. full 은 장애를 좇을 때만 잠깐 켜고, 그때도 공개 화면은 지운다.
- 기록은 logging 의 app.ops 이름으로 한 줄 JSON 을 낸다. 컨테이너에서는 표준 오류로 나가 운영 환경의 기록 수집기가 모은다.
"""
from __future__ import annotations

import ipaddress
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
PUBLIC_PATHS = ("/consult", "/invite", "/welcome")             # 로그인 전 공개 화면. 접속 주소를 남기지 않는다
PUBLIC_RE = re.compile(r"(?<![\w/.-])(?:" + "|".join(re.escape(p) for p in PUBLIC_PATHS) + r")(?=[/?\s\"']|$)")
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?::\d{1,5})?(?![\d.])")
IP_MODES = ("prefix", "none", "full")


def slow_ms() -> float:
    try:
        return float(os.environ.get("APP_SLOW_MS") or 1000)
    except ValueError:
        return 1000.0


def strip_query(text: str) -> str:
    """주소 뒤 꼬리(?...)를 지운다. 문자열 안 어디에 있든 공백이나 따옴표 앞까지 지운다."""
    return QUERY_RE.sub("", text) if "?" in text else text


def ip_mode() -> str:
    mode = (os.environ.get("APP_LOG_IP") or "prefix").strip().lower()
    return mode if mode in IP_MODES else "prefix"


def is_public_path(path: str) -> bool:
    path = strip_query(path)
    return any(path == p or path.startswith(p + "/") for p in PUBLIC_PATHS)


def mask_ip(ip: str, mode: str) -> str | None:
    """주소 하나를 가린다. 주소가 아니면 None."""
    try:
        addr = ipaddress.ip_address(ip.strip("[]"))
    except ValueError:
        return None
    if mode == "full":
        return str(addr)
    if mode == "none":
        return "-"
    bits = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{bits}", strict=False))


def mask_client(client: str, public: bool, mode: str | None = None) -> str:
    """uvicorn 의 접속 주소 칸(주소:포트)을 가린다. 공개 화면은 늘 지운다. 주소 꼴이 아니면 full 일 때만 남긴다."""
    mode = mode or ip_mode()
    if not client or client == "-":
        return client
    if public or mode == "none":
        return "-"
    if mode == "full":
        return client
    host, sep, port = client.rpartition(":")
    masked = mask_ip(host, mode) if sep and port.isdigit() else None
    if masked is None:
        masked = mask_ip(client, mode)
    return masked or "-"


def mask_ips_in_text(text: str, public: bool, mode: str | None = None) -> str:
    """이미 채워진 글 안의 IPv4 주소를 가린다. uvicorn 기록에 섞여 오는 주소를 위한 것이다."""
    mode = mode or ip_mode()
    if (mode == "full" and not public) or not IPV4_RE.search(text):
        return text
    return IPV4_RE.sub(lambda m: "-" if public else (mask_ip(m.group(1), mode) or "-"), text)


class QueryTailFilter(logging.Filter):
    """uvicorn 기록에서 주소 뒤 꼬리를 지우고 접속 주소를 가린다. 기록은 버리지 않고 값만 바꾼다.
    uvicorn 접근 기록은 args 꼴(접속 주소, 방식, 경로와 꼬리, 판, 코드)로 오므로 칸 수와 종류는 그대로 두고 문자열 칸만 고친다.
    첫 칸이 주소:포트 꼴이면 접속 주소 칸으로 보고 mask_client 로, 나머지 문자열 칸은 꼬리를 지운 뒤 IPv4 를 가린다."""

    def filter(self, record: logging.LogRecord) -> bool:
        mode = ip_mode()
        args = record.args
        if isinstance(args, tuple) and args:
            strs = [strip_query(a) if isinstance(a, str) else a for a in args]
            public = any(isinstance(a, str) and (is_public_path(a) or PUBLIC_RE.search(a)) for a in strs)
            out = []
            for i, a in enumerate(strs):
                if not isinstance(a, str):
                    out.append(a)
                elif i == 0 and _looks_like_client(a):
                    out.append(mask_client(a, public, mode))
                else:
                    out.append(mask_ips_in_text(a, public, mode))
            record.args = tuple(out)
        elif isinstance(args, dict) and args:
            strs = {k: strip_query(v) if isinstance(v, str) else v for k, v in args.items()}
            public = any(isinstance(v, str) and (is_public_path(v) or PUBLIC_RE.search(v)) for v in strs.values())
            record.args = {k: mask_ips_in_text(v, public, mode) if isinstance(v, str) else v for k, v in strs.items()}
        elif isinstance(record.msg, str):
            msg = strip_query(record.msg)                   # 이미 채워진 글. 꼴 문자열의 %s 는 건드리지 않는다
            record.msg = mask_ips_in_text(msg, bool(PUBLIC_RE.search(msg)), mode)
        return True


def _looks_like_client(text: str) -> bool:
    host, sep, port = text.rpartition(":")
    return bool(sep and port.isdigit() and host and " " not in host and "/" not in host)


def install_query_filter() -> None:
    """uvicorn 기록기에 꼬리와 접속 주소 거르개를 한 번만 단다."""
    for name in ACCESS_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, QueryTailFilter) for f in lg.filters):
            lg.addFilter(QueryTailFilter())


def setup_logging() -> None:
    """app 기록기에 처리기가 없으면 표준 오류로 한 줄씩 낸다. uvicorn 기록기는 처리기와 꼴을 그대로 두고 꼬리와 접속 주소 거르개만 단다."""
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
