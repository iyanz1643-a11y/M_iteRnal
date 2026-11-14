"""가져오기 흐름. 출처에서 가져오기, data/raw 에 해시와 함께 보관, 이미 넣은 해시면 건너뛰기, 종류에 맞는 적재기로 넘기기.

- 출처의 fetch 는 시간 제한과 재시도 안에서 부른다. 실패해도 예외는 위로 새지 않고 SourceRun 결과로 돌아온다.
- 적재는 DB 에 쓰므로 시간 제한을 걸지 않는다. 실패하면 되돌리고 실패 결과로 돌려준다.
- 연결 상태는 출처 이름으로 StateStore 에 남긴다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.connect.base import DUPLICATE, FAILED, KINDS, LOADED, NO_HANDLER, OK, SKIPPED, IngestResult, RawFile, Skipped, describe, valid_ym
from app.connect.handlers import DEFAULT_REGISTRY, Registry
from app.connect.raw_store import default_data_dir, store_raw
from app.connect.retry import call_with_retry
from app.connect.state import StateStore


@dataclass
class SourceRun:
    source: str
    status: str                  # ok / failed / not_configured
    reason: str = ""
    attempts: int = 0
    results: list = field(default_factory=list)

    def counts(self) -> dict:
        out: dict = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    @property
    def ok(self) -> bool:
        return self.status == OK and not any(r.status == FAILED for r in self.results)

    def to_dict(self) -> dict:
        return {"source": self.source, "status": self.status, "reason": self.reason, "attempts": self.attempts,
                "counts": self.counts(), "results": [r.to_dict() for r in self.results]}


class Pipeline:
    def __init__(self, con, registry: Registry | None = None, data_dir=None, state: StateStore | None = None):
        self.con = con
        self.registry = registry or DEFAULT_REGISTRY
        self.data_dir = data_dir or default_data_dir()
        self.state = state or StateStore()

    # ---------------------------------------------------------- 파일 하나
    def ingest(self, raw: RawFile) -> IngestResult:
        res = IngestResult(status=FAILED, file_name=raw.name, kind=raw.kind, period_ym=raw.period_ym, via=raw.via, file_hash=raw.sha256)
        try:
            return self._ingest(raw, res)
        except Exception as e:  # 어떤 경우에도 위로 새지 않는다
            _rollback(self.con)
            res.status, res.reason = FAILED, describe(e)
            return res

    def _ingest(self, raw: RawFile, res: IngestResult) -> IngestResult:
        if raw.kind is None:
            res.status, res.reason = SKIPPED, f"종류를 알 수 없어 건너뜁니다. 종류를 정해 주세요: {', '.join(KINDS)}"
            return res
        if raw.kind not in KINDS:
            res.reason = f"모르는 종류입니다: {raw.kind}"
            return res
        if raw.period_ym is not None and not valid_ym(raw.period_ym):
            res.reason = f"귀속월은 YYYYMM 꼴이어야 합니다: {raw.period_ym}"
            return res
        if raw.size == 0:
            res.reason = "빈 파일입니다"
            return res

        # 원본 보관은 처리기가 없어도 한다. 나중에 처리기가 생기면 그대로 다시 넣을 수 있다
        path, rel = store_raw(raw, self.data_dir)
        res.raw_path = rel

        h = self.registry.get(raw.kind)
        if h is None:
            res.status, res.reason = NO_HANDLER, f"처리기 없음: {KINDS[raw.kind]} 를 DB 에 반영할 함수가 아직 없습니다. 원본은 보관했습니다"
            return res
        if h.needs_period and not raw.period_ym:
            res.reason = "귀속월이 없습니다. YYYYMM 으로 정해 주세요"
            return res
        if h.already_loaded and h.already_loaded(self.con, raw.sha256):
            res.status, res.reason = DUPLICATE, "이미 넣은 파일입니다. 건너뜁니다"
            return res
        try:
            detail = h.load(self.con, path, raw, rel) or {}
        except h.duplicate_errors:
            _rollback(self.con)
            res.status, res.reason = DUPLICATE, "이미 넣은 파일입니다. 건너뜁니다"
            return res
        except Exception as e:
            _rollback(self.con)
            res.reason = describe(e)
            return res
        res.detail = {k: v for k, v in detail.items() if k not in ("failed", "reason")}
        if detail.get("failed"):
            res.status, res.reason = FAILED, str(detail.get("reason") or "적재기가 실패를 알렸습니다")
        else:
            res.status = LOADED
        return res

    # ---------------------------------------------------------- 출처 하나
    def run(self, source, *, attempts: int = 3, timeout: float | None = 60.0, backoff: float = 1.0, sleep=time.sleep) -> SourceRun:
        name = getattr(source, "name", type(source).__name__)
        got = call_with_retry(source.fetch, attempts=attempts, timeout=timeout, backoff=backoff, sleep=sleep)
        run = SourceRun(source=name, status=got.status, reason=got.reason, attempts=got.attempts)
        if got.status != OK:
            self._record(name, got.status, got.reason)
            return run
        for item in got.value or []:
            if isinstance(item, Skipped):
                run.results.append(IngestResult(status=SKIPPED, file_name=item.name, reason=item.reason))
                continue
            r = self.ingest(item)
            run.results.append(r)
            done = getattr(source, "done", None)
            if done:
                try:
                    done(item, r)
                except Exception:  # 감시 목록을 못 써도 적재 결과는 그대로 둔다
                    pass
        self._record(name, OK, "", run.counts())
        return run

    def _record(self, name, status, reason, detail=None):
        try:
            self.state.record(name, status, reason=reason, detail=detail)
        except Exception:  # 상태 기록 실패로 월 마감이 멈추면 안 된다
            pass


def _rollback(con):
    try:
        con.rollback()
    except Exception:
        pass

