"""시간 제한과 재시도 도우미. 실패해도 예외를 위로 올리지 않고 Outcome 으로 돌려준다.

- run_with_timeout: 함수를 따로 돌리고 정한 초 안에 끝나지 않으면 CallTimeout.
  파이썬 스레드는 강제로 멈출 수 없어서 늦게 끝난 호출은 결과만 버린다.
  그래서 DB 에 쓰는 적재에는 쓰지 않고 바깥 호출에만 쓴다.
- call_with_retry: 일시적인 실패만 다시 시도하고, 기다리는 시간을 두 배씩 늘린다.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable

import httpx

from app.connect.base import FAILED, NOT_CONFIGURED, OK, NotConfigured, Outcome, describe


class CallTimeout(Exception):
    pass


class Retryable(Exception):
    """다시 해 볼 만한 실패. 서버 쪽 5xx 같은 것."""


def run_with_timeout(fn: Callable, timeout: float | None):
    if not timeout:
        return fn()
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="connect")
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except FutureTimeout:
        fut.cancel()
        raise CallTimeout(f"{timeout:g}초 안에 끝나지 않았습니다") from None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def default_retryable(exc: BaseException) -> bool:
    if isinstance(exc, NotConfigured):
        return False
    return isinstance(exc, (CallTimeout, Retryable, TimeoutError, ConnectionError, httpx.TransportError))


def call_with_retry(fn: Callable, *, attempts: int = 3, timeout: float | None = 30.0, backoff: float = 1.0,
                    max_backoff: float = 10.0, retry_if: Callable[[BaseException], bool] = default_retryable,
                    sleep: Callable[[float], None] = time.sleep) -> Outcome:
    """fn 을 부른다. 돌려주는 것은 Outcome(ok | not_configured | failed). 예외는 새지 않는다."""
    attempts = max(1, int(attempts))
    last: BaseException | None = None
    n = 0
    for n in range(1, attempts + 1):
        try:
            return Outcome(OK, value=run_with_timeout(fn, timeout), attempts=n)
        except NotConfigured as e:
            return Outcome(NOT_CONFIGURED, reason=str(e) or "설정 안 됨", attempts=n)
        except Exception as e:  # 바깥 실패는 모두 결과로 바꾼다
            last = e
            if n >= attempts or not retry_if(e):
                break
            sleep(min(backoff * (2 ** (n - 1)), max_backoff))
    return Outcome(FAILED, reason=describe(last), attempts=n)
