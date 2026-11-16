"""종류별 처리기 등록. 보관한 원본을 기존 적재기로 넘긴다.

처리기 함수의 꼴: load(con, path, raw, rel_path) -> dict
- path 는 data/raw 에 보관한 원본의 절대 경로, rel_path 는 data 아래 상대 경로다.
- 돌려주는 dict 는 화면과 명령줄에 그대로 보이므로 사람 이름과 금액을 넣지 않는다.
- 적재가 막혔으면 {"failed": True, "reason": ...} 를 넣어 돌려준다.

already_loaded(con, file_hash) -> bool 은 같은 파일을 두 번 넣지 않으려고 먼저 묻는다.
duplicate_errors 는 적재기가 같은 파일이라며 던지는 예외다. 둘 다 '이미 넣은 파일' 로 돌려준다.

사원현황은 app/loader/employee_handler.py, 수료자 명부는 app/loader/edu_roster.py 가 아래처럼 기본 등록부에 넣는다. 이 파일 끝에서 부른다.
    register_handler("employee", load_fn, already_loaded=check_fn)
    register_handler("edu_roster", load_fn, already_loaded=check_fn, needs_period=False)
계약현황, 시상안, 인별실적(person_perf)은 이 파일의 default_registry() 가 넣는다. 인별실적은 기간을 meta 의 start_ym, end_ym 이나 파일 이름에서 읽는다.
default_registry() 로 새로 만든 등록부에는 사원현황과 수료자 명부가 없다. 등록이 없으면 '처리기 없음' 으로 돌려주고 원본은 보관해 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.connect.base import KINDS


@dataclass
class Handler:
    kind: str
    load: Callable
    already_loaded: Callable | None = None
    duplicate_errors: tuple = field(default_factory=tuple)
    needs_period: bool = True


class Registry:
    def __init__(self):
        self._h: dict[str, Handler] = {}

    def register(self, kind, load, already_loaded=None, duplicate_errors=(), needs_period=True) -> Handler:
        if kind not in KINDS:
            raise ValueError(f"모르는 종류입니다: {kind}. 쓸 수 있는 것: {', '.join(KINDS)}")
        h = Handler(kind, load, already_loaded, tuple(duplicate_errors), needs_period)
        self._h[kind] = h
        return h

    def unregister(self, kind) -> None:
        self._h.pop(kind, None)

    def get(self, kind) -> Handler | None:
        return self._h.get(kind)

    def kinds(self) -> list:
        return sorted(self._h)


# ------------------------------------------------------------------ 계약현황
CONTRACT_KEEP = ("batch_id", "block", "warn", "file_rows", "loaded", "dropped_columns",
                 "inserted", "updated", "refreshed", "stale", "skipped", "history")


def _contract_loaded(con, file_hash) -> bool:
    return con.execute("SELECT 1 FROM import_batch WHERE file_hash=?", (file_hash,)).fetchone() is not None


def _contract_load(con, path, raw, rel_path) -> dict:
    from app.loader import eternal_contract as ec

    out = ec.load_contract(con, path, raw.period_ym, by=raw.meta.get("by", "connect"))
    # 적재기는 들어온 길을 manual 로, 경로를 절대 경로로 적는다. 연결 쪽 값으로 바꿔 둔다
    con.execute("UPDATE import_batch SET received_via=?, file_path=? WHERE batch_id=?", (raw.via, rel_path, out["batch_id"]))
    con.commit()
    status = con.execute("SELECT status FROM import_batch WHERE batch_id=?", (out["batch_id"],)).fetchone()[0]
    keep = {k: out[k] for k in CONTRACT_KEEP if k in out}   # 합계 같은 금액은 넘기지 않는다
    keep["batch_status"] = status
    if status == "failed":
        keep["failed"] = True
        keep["reason"] = "적재 검사에서 막힌 문제가 있습니다. 품질 문제 목록을 확인해 주세요"
    return keep


# ------------------------------------------------------------------ 시상안
def _plan_loaded(con, file_hash) -> bool:
    return con.execute("SELECT 1 FROM incentive_plan_doc WHERE file_hash=?", (file_hash,)).fetchone() is not None


def _plan_load(con, path, raw, rel_path) -> dict:
    from app.loader import incentive_plan as ip

    out = ip.load(con, path, raw.period_ym, title=raw.meta.get("title"), loaded_by=raw.meta.get("by", "connect"))
    return {"plan_doc_id": out["plan_doc_id"], "lines": out["lines"], "skipped_sheets": len(out.get("skipped") or [])}


# ------------------------------------------------------------------ 인별실적
PERSON_PERF_KEEP = ("batch_id", "block", "warn", "file_rows", "loaded", "skipped", "summary_rows", "sums_match", "matched_agents",
                    "unmapped_agents", "name_mismatch", "insurer_sum_mismatch", "insurers", "unmapped_insurers", "tiers",
                    "dropped_columns", "superseded", "start_ym", "end_ym")


def _person_perf_load(con, path, raw, rel_path) -> dict:
    """인별실적 한 파일. 기간은 meta 의 start_ym, end_ym 이고 없으면 파일 이름의 '202603-05' 꼴에서 읽는다.
    돌려주는 dict 에는 건수와 비율만 담는다. 금액 대조 값과 사람 이름은 넘기지 않는다."""
    from app.loader import person_perf as pp
    from app.loader.sheet import HeaderMismatch

    start, end = raw.meta.get("start_ym"), raw.meta.get("end_ym")
    if not (start and end):
        start, end = pp.guess_period(raw.name) or (None, None)
    if not (start and end):
        return {"failed": True, "reason": "기간을 알 수 없습니다. 파일 이름에 202603-05 꼴로 기간을 넣거나 시작월과 끝월을 정해 주세요"}
    try:
        out = pp.load_person_perf(con, path, start, end, by=raw.meta.get("by", "connect"), via=raw.via)
    except HeaderMismatch as e:
        con.rollback()
        return {"failed": True, "reason": "인별실적 양식이 아닙니다. " + " / ".join(e.reasons[:3])}
    con.execute("UPDATE import_batch SET file_path=? WHERE batch_id=?", (rel_path, out["batch_id"]))
    con.commit()
    status = con.execute("SELECT status FROM import_batch WHERE batch_id=?", (out["batch_id"],)).fetchone()[0]
    keep = {k: out[k] for k in PERSON_PERF_KEEP if k in out}
    keep["batch_status"] = status
    if status == "failed":
        keep["failed"] = True
        keep["reason"] = "적재 검사에서 막힌 문제가 있습니다. 품질 문제 목록을 확인해 주세요"
    return keep


def default_registry() -> Registry:
    from app.loader.excel_loader import DuplicateFile
    from app.loader.incentive_plan import DuplicatePlan

    r = Registry()
    r.register("contract", _contract_load, already_loaded=_contract_loaded, duplicate_errors=(DuplicateFile,))
    r.register("plan", _plan_load, already_loaded=_plan_loaded, duplicate_errors=(DuplicatePlan,))
    r.register("person_perf", _person_perf_load, already_loaded=_contract_loaded, duplicate_errors=(DuplicateFile,), needs_period=False)
    # employee, edu_roster: 아래 _register_loaders 가 기본 등록부에만 넣는다
    return r


DEFAULT_REGISTRY = default_registry()


def register_handler(kind, load, already_loaded=None, duplicate_errors=(), needs_period=True) -> Handler:
    """기본 등록부에 처리기를 넣는다. 같은 종류가 있으면 바꾼다."""
    return DEFAULT_REGISTRY.register(kind, load, already_loaded, duplicate_errors, needs_period)


def _register_loaders() -> None:
    """적재기 쪽에서 register_handler 로 넣는 처리기. register_handler 가 정의된 뒤에 부른다."""
    from app.loader import edu_roster, employee_handler

    employee_handler.register()
    edu_roster.register()


_register_loaders()
