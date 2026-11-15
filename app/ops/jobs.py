"""뒤에서 하는 작업. 작업 표 job 과 처리기. 워커는 app/worker.py 이고 화면은 /data/jobs 다.

화면과 명령줄은 작업을 넣고 상태만 본다. 계산과 적재는 워커가 한다. 처리기는 업무 로직을 그대로 부르고 계산을 새로 하지 않는다.

| 종류 | 하는 일 | params | 뒤따르는 작업 |
| --- | --- | --- | --- |
| ingest | 큰 적재. 저장소의 파일 하나를 연결 흐름으로 적재한다 | area, key, kind, ym, title | 계약현황이나 사원현황이 들어가면 업적 run 과 랭킹 run |
| perf_run | 업적 run. performance.run | ym | 없음 |
| rank_run | 랭킹 run. ranking.run | ym | 없음 |
| notify_sweep | 알림 정리. 끝난 명함 승인 권한을 맞춘다 | 없음 | 없음 |
| connect_watch | 연결 가져오기. CONNECT_WATCH_DIR 폴더의 새 파일 | 없음 | ingest 와 같다 |
| gdrive | 연결 가져오기. 구글 드라이브. 설정 전에는 설정 안 됨으로 끝난다 | 없음 | ingest 와 같다 |

- 적재 뒤에는 그 귀속월과, 그 뒤에 이미 저장된 업적 run 이 있는 달을 모두 다시 돌린다. 분기, 반기, 연 누적이 앞 달의 적재를 담기 때문이다.
  홈과 팀원 현황은 저장된 업적 run 을 읽고 실적 화면은 계약에서 새로 센다. 적재 뒤 run 을 다시 돌려 두 숫자가 어긋나지 않게 한다.
- 같은 종류와 같은 params 의 작업이 이미 대기 중이면 새로 넣지 않고 그 번호를 돌려준다.
- 실패하면 30초, 60초, 120초 뒤로 미뤄 다시 하고 max_attempts 번 실패하면 failed 로 둔다.
- 워커가 죽어 lease_until 이 지난 처리 중 작업은 다시 대기로 돌린다.
- params 와 result 에는 사람 이름, 연락처, 금액을 넣지 않는다. 파일 이름도 result 에 넣지 않는다. error 에는 예외 종류만 둔다.
- 알림 발송은 D-10 제안대로 앱 안 알림만이라 보낼 것이 없다. 알림은 열 때 원본에서 만들고 읽음 표시는 지우지 않는다(가정).
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import sqlite3
from datetime import datetime, timedelta

from app import db
from app.ops import observe, schema
from app.ops.storage import StorageError, get_storage

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
STATUS_KO = {QUEUED: "대기", RUNNING: "처리 중", DONE: "끝남", FAILED: "실패"}
KIND_KO = {"ingest": "큰 적재", "perf_run": "업적 run", "rank_run": "랭킹 run", "notify_sweep": "알림 정리",
           "connect_watch": "폴더 가져오기", "gdrive": "구글 드라이브 가져오기"}
YM = re.compile(r"^20\d\d(0[1-9]|1[0-2])$")
LEASE_SECONDS = 30 * 60
BACKOFF_SECONDS = 30
REFRESH_KINDS = ("contract", "employee")       # 들어가면 업적 run 을 다시 돌리는 원본 종류
FMT = "%Y-%m-%d %H:%M:%S"


class JobError(ValueError):
    """넣을 수 없는 작업. 화면과 명령줄에 그대로 보여도 되는 글만 담는다."""


def _now(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime(FMT)


def _later(seconds: float, now: datetime | None = None) -> str:
    return ((now or datetime.now()) + timedelta(seconds=seconds)).strftime(FMT)


def worker_name() -> str:
    return f"{socket.gethostname()[:40]}:{os.getpid()}"


# ---------------------------------------------------------------- 넣기
def _check(kind: str, params: dict) -> dict:
    if kind not in HANDLERS:
        raise JobError(f"없는 작업 종류입니다: {kind}")
    p = dict(params or {})
    if kind in ("perf_run", "rank_run"):
        if not YM.match(str(p.get("ym") or "")):
            raise JobError("귀속월은 YYYYMM 꼴이어야 합니다.")
        p = {"ym": str(p["ym"])}
    elif kind == "ingest":
        from app.connect.base import KINDS
        if p.get("area") not in ("raw", "uploads"):
            raise JobError("적재할 파일의 구역은 raw 나 uploads 여야 합니다.")
        if p.get("kind") not in KINDS:
            raise JobError("적재할 원본 종류가 없습니다.")
        if p.get("ym") and not YM.match(str(p["ym"])):
            raise JobError("귀속월은 YYYYMM 꼴이어야 합니다.")
        try:
            if not get_storage().exists(p["area"], p.get("key") or ""):
                raise JobError("저장소에 그 파일이 없습니다.")
        except StorageError as e:
            raise JobError(str(e)) from None
        p = {k: p[k] for k in ("area", "key", "kind", "ym", "title", "by") if p.get(k)}
    else:
        p = {}
    return p


def dedupe_key(kind: str, params: dict) -> str:
    return f"{kind}:{json.dumps(params, sort_keys=True, ensure_ascii=False)}"[:300]


def enqueue(con, kind: str, params: dict | None = None, *, by: str = "system", parent_id: int | None = None,
            max_attempts: int = 3, delay: float = 0, commit: bool = True, now: datetime | None = None) -> int:
    """작업을 넣고 번호를 돌려준다. 같은 작업이 대기 중이면 그 번호를 돌려준다."""
    schema.ensure(con)
    p = _check(kind, params or {})
    key = dedupe_key(kind, p)
    old = con.execute("SELECT job_id FROM job WHERE dedupe_key=? AND status='queued'", (key,)).fetchone()
    if old:
        return old[0]
    try:
        cur = con.execute(
            """INSERT INTO job(kind, params, dedupe_key, status, max_attempts, run_after, requested_by, parent_id, created_at)
               VALUES(?,?,?,'queued',?,?,?,?,?)""",
            (kind, json.dumps(p, ensure_ascii=False), key, max(1, int(max_attempts)), _later(delay, now), by, parent_id, _now(now)))
    except sqlite3.IntegrityError:                       # 다른 프로세스가 그 사이에 같은 작업을 넣었다
        con.rollback()
        return con.execute("SELECT job_id FROM job WHERE dedupe_key=? AND status='queued'", (key,)).fetchone()[0]
    if commit:
        con.commit()
    return cur.lastrowid


def refresh_months(con, ym: str | None) -> list:
    """적재 뒤에 다시 돌릴 달. 그 달과, 그 뒤에 저장된 업적 run 이 있는 달. ym 이 없으면 가장 최근 업적 run 의 달."""
    done = [r[0] for r in con.execute("SELECT DISTINCT period_ym FROM calc_run WHERE run_type='performance' AND status='done' ORDER BY 1")]
    if not ym or not YM.match(str(ym)):
        return done[-1:]
    return sorted({str(ym), *[m for m in done if m > str(ym)]})


def enqueue_refresh(con, ym: str | None, *, by: str = "system", parent_id: int | None = None) -> list:
    """업적 run 과 랭킹 run 을 다시 돌리는 작업을 넣는다. 넣은 번호 목록."""
    out = []
    for m in refresh_months(con, ym):
        out.append(enqueue(con, "perf_run", {"ym": m}, by=by, parent_id=parent_id, commit=False))
        out.append(enqueue(con, "rank_run", {"ym": m}, by=by, parent_id=parent_id, commit=False))
    con.commit()
    return out


# ---------------------------------------------------------------- 보기
def _row(r) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.get("params") or "{}")
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    d["status_ko"] = STATUS_KO.get(d["status"], d["status"])
    d["kind_ko"] = KIND_KO.get(d["kind"], d["kind"])
    return d


def get(con, job_id: int) -> dict | None:
    schema.ensure(con)
    r = con.execute("SELECT * FROM job WHERE job_id=?", (job_id,)).fetchone()
    return _row(r) if r else None


def recent(con, limit: int = 50) -> list:
    schema.ensure(con)
    return [_row(r) for r in con.execute("SELECT * FROM job ORDER BY job_id DESC LIMIT ?", (int(limit),))]


def counts(con) -> dict:
    schema.ensure(con)
    return {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM job GROUP BY status")}


# ---------------------------------------------------------------- 꺼내기와 처리
def recover(con, now: datetime | None = None) -> int:
    """잡은 시각이 지난 처리 중 작업을 대기로 돌린다. 횟수를 다 쓴 것은 실패로 둔다."""
    n = 0
    stale = con.execute("SELECT job_id, attempts, max_attempts FROM job WHERE status='running' AND lease_until<?", (_now(now),)).fetchall()
    for r in stale:
        try:
            if r["attempts"] >= r["max_attempts"]:
                con.execute("UPDATE job SET status='failed', error='LeaseExpired', finished_at=?, lease_until=NULL WHERE job_id=?", (_now(now), r["job_id"]))
            else:
                con.execute("UPDATE job SET status='queued', lease_until=NULL, worker=NULL, error='LeaseExpired' WHERE job_id=?", (r["job_id"],))
        except sqlite3.IntegrityError:                   # 같은 작업이 이미 대기 중이다
            con.execute("UPDATE job SET status='failed', error='Superseded', finished_at=?, lease_until=NULL WHERE job_id=?", (_now(now), r["job_id"]))
        n += 1
    con.commit()
    return n


def claim(con, worker: str | None = None, now: datetime | None = None) -> dict | None:
    """대기 중인 작업 하나를 잡는다. 다른 워커가 먼저 잡았으면 다음 것을 본다."""
    schema.ensure(con)
    recover(con, now)
    worker = worker or worker_name()
    for _ in range(20):
        r = con.execute("SELECT job_id FROM job WHERE status='queued' AND run_after<=? ORDER BY job_id LIMIT 1", (_now(now),)).fetchone()
        if not r:
            return None
        cur = con.execute(
            """UPDATE job SET status='running', attempts=attempts+1, started_at=?, lease_until=?, worker=?
               WHERE job_id=? AND status='queued'""",
            (_now(now), _later(LEASE_SECONDS, now), worker, r[0]))
        con.commit()
        if cur.rowcount == 1:
            return get(con, r[0])
    return None


def run_one(con, worker: str | None = None, now: datetime | None = None) -> dict | None:
    """작업 하나를 처리하고 끝난 줄을 돌려준다. 대기열이 비었으면 None."""
    job = claim(con, worker, now)
    if not job:
        return None
    try:
        result = HANDLERS[job["kind"]](con, job["params"], job) or {}
    except Exception as exc:
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        name = type(exc).__name__
        if job["attempts"] < job["max_attempts"]:
            wait = BACKOFF_SECONDS * 2 ** (job["attempts"] - 1)
            con.execute("UPDATE job SET status='queued', run_after=?, lease_until=NULL, worker=NULL, error=? WHERE job_id=?",
                        (_later(wait, now), name, job["job_id"]))
        else:
            con.execute("UPDATE job SET status='failed', finished_at=?, lease_until=NULL, error=? WHERE job_id=?", (_now(now), name, job["job_id"]))
        con.commit()
        observe.emit(logging.ERROR, "job_failed", job=job["job_id"], kind=job["kind"], attempt=job["attempts"], error=name, where=observe.where(exc))
        return get(con, job["job_id"])
    con.execute("UPDATE job SET status='done', finished_at=?, lease_until=NULL, result=?, error=NULL WHERE job_id=?",
                (_now(now), json.dumps(result, ensure_ascii=False, default=str), job["job_id"]))
    con.commit()
    observe.emit(logging.INFO, "job_done", job=job["job_id"], kind=job["kind"])
    return get(con, job["job_id"])


def drain(con, worker: str | None = None, limit: int = 1000) -> list:
    """대기열이 빌 때까지 처리한다. 뒤로 미룬 작업은 기다리지 않는다."""
    out = []
    for _ in range(limit):
        j = run_one(con, worker)
        if j is None:
            break
        out.append(j)
    return out


# ---------------------------------------------------------------- 처리기
def _perf_run(con, p, job):
    from app.engine import performance
    run_id, n = performance.run(con, p["ym"], run_by="worker")
    return {"run_id": run_id, "rows": n}


def _rank_run(con, p, job):
    from app.engine import ranking
    run_id, n = ranking.run(con, p["ym"], run_by="worker")
    return {"run_id": run_id, "rows": n}


def _source_result(con, run, job, by) -> dict:
    """연결 흐름의 결과에서 건수만 남기고, 업적에 닿는 원본이 들어갔으면 run 을 다시 돌리는 작업을 넣는다."""
    from app.connect.base import LOADED
    yms = set()
    for r in run.results:
        if r.status == LOADED and r.kind in REFRESH_KINDS:
            yms.add(r.period_ym)
    follow = []
    for ym in sorted(yms, key=lambda x: x or ""):
        follow += enqueue_refresh(con, ym, by=by, parent_id=job["job_id"])
    return {"status": run.status, "counts": run.counts(), "follow": sorted(set(follow))}


def _ingest(con, p, job):
    from app.connect.base import VIA_UPLOAD, RawFile
    from app.connect.pipeline import Pipeline
    from app.connect.sources import UploadSource
    path = get_storage().local_path(p["area"], p["key"])
    meta = {k: p[k] for k in ("title", "by") if p.get(k)}
    raw = RawFile.from_path(path, via=VIA_UPLOAD, kind=p["kind"], period_ym=p.get("ym"), **meta)
    run = Pipeline(con).run(UploadSource([raw]))
    return _source_result(con, run, job, p.get("by") or "worker")


def _connect_watch(con, p, job):
    from app.connect.pipeline import Pipeline
    from app.connect.sources import FolderWatchSource
    from app.connect.state import StateStore
    folder = os.environ.get("CONNECT_WATCH_DIR")
    if not folder:
        return {"status": "not_configured"}
    state = StateStore(con=con)
    run = Pipeline(con, state=state).run(FolderWatchSource(folder, state=state))
    return _source_result(con, run, job, "worker")


def _gdrive(con, p, job):
    from app.connect.pipeline import Pipeline
    from app.connect.sources import GoogleDriveSource
    run = Pipeline(con).run(GoogleDriveSource(), attempts=1)
    return _source_result(con, run, job, "worker")


def _notify_sweep(con, p, job):
    from app.org import card_grant
    try:
        out = card_grant.expire_stale(con)
    except sqlite3.OperationalError:                    # 명함 권한 표가 없는 오래된 DB
        con.rollback()
        return {"skipped": "no_table"}
    con.commit()
    return {k: (len(v) if isinstance(v, (list, tuple, set, dict)) else v) for k, v in (out or {}).items()}


HANDLERS = {"ingest": _ingest, "perf_run": _perf_run, "rank_run": _rank_run, "notify_sweep": _notify_sweep,
            "connect_watch": _connect_watch, "gdrive": _gdrive}


# ---------------------------------------------------------------- 정해진 때 넣기
def schedule_due(con, now: datetime | None = None) -> list:
    """워커가 돌 때마다 부른다. 알림 정리는 하루 한 번, 폴더 가져오기는 CONNECT_WATCH_DIR 이 있으면 WORKER_WATCH_MINUTES 분마다."""
    schema.ensure(con)
    now = now or datetime.now()
    out = []
    wants = [("notify_sweep", f"notify_sweep:day:{now:%Y-%m-%d}")]
    if os.environ.get("CONNECT_WATCH_DIR"):
        try:
            every = max(1, int(os.environ.get("WORKER_WATCH_MINUTES") or 5))
        except ValueError:
            every = 5
        slot = (now.hour * 60 + now.minute) // every
        wants.append(("connect_watch", f"connect_watch:slot:{now:%Y-%m-%d}:{slot}"))
    for kind, key in wants:
        if con.execute("SELECT 1 FROM job WHERE dedupe_key=?", (key,)).fetchone():
            continue
        cur = con.execute("INSERT INTO job(kind, params, dedupe_key, status, max_attempts, run_after, requested_by, created_at) VALUES(?,?,?,'queued',1,?,?,?)",
                          (kind, "{}", key, _now(now), "schedule", _now(now)))
        out.append(cur.lastrowid)
    con.commit()
    return out


def db_path() -> str:
    return str(os.environ.get("APP_DB") or db.DEFAULT_DB)
