"""작업 워커. 작업 표 job 의 대기 작업을 하나씩 꺼내 처리한다. 처리기와 규칙은 app/ops/jobs.py 다.

    python -m app.worker                               # 멈출 때까지 처리한다. WORKER_INTERVAL 초마다 대기열을 본다
    python -m app.worker --once                        # 대기열이 빌 때까지 처리하고 끝낸다
    python -m app.worker enqueue perf_run --ym 202610  # 작업을 넣는다. rank_run, notify_sweep, connect_watch, gdrive
    python -m app.worker enqueue ingest --area raw --key contract/202610/<해시>_<이름> --kind contract --ym 202610
    python -m app.worker status [--limit 20]           # 최근 작업의 번호, 종류, 상태, 건수

DB 는 --db 또는 APP_DB, 없으면 data/demo.db 다. SIGTERM 을 받으면 하던 작업을 마치고 끝낸다.
돌릴 때마다 알림 정리를 하루 한 번, CONNECT_WATCH_DIR 이 있으면 폴더 가져오기를 WORKER_WATCH_MINUTES 분마다 넣는다.
출력에는 사람 이름과 금액을 넣지 않는다. 끝난 뒤 돌려주는 값: 0 성공, 1 실패한 작업이 있음.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys
import time

from app import db
from app.ops import jobs, observe


def _con(path):
    p = pathlib.Path(path)
    if not p.exists():
        raise SystemExit(f"DB 가 없습니다: {p}. --db 나 APP_DB 로 정해 주세요")
    return db.connect(p)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _brief(j: dict) -> dict:
    return {k: j.get(k) for k in ("job_id", "kind", "status", "attempts", "params", "result", "error", "created_at", "finished_at")}


def cmd_run(a) -> int:
    stop = {"now": False}

    def _stop(*_):
        stop["now"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    con = _con(a.db)
    failed = 0
    try:
        while not stop["now"]:
            jobs.schedule_due(con)
            done = jobs.drain(con, limit=a.batch) if not a.once else jobs.drain(con)
            failed += sum(1 for j in done if j["status"] == jobs.FAILED)
            if a.once:
                break
            if not done:
                for _ in range(int(a.interval * 10)):
                    if stop["now"]:
                        break
                    time.sleep(0.1)
    finally:
        con.close()
    return 1 if failed else 0


def cmd_enqueue(a) -> int:
    con = _con(a.db)
    try:
        params = {k: v for k, v in (("ym", a.ym), ("area", a.area), ("key", a.key), ("kind", a.source_kind), ("title", a.title)) if v}
        try:
            job_id = jobs.enqueue(con, a.kind, params, by=a.by)
        except jobs.JobError as e:
            print(str(e), file=sys.stderr)
            return 1
        _print(_brief(jobs.get(con, job_id)))
        return 0
    finally:
        con.close()


def cmd_status(a) -> int:
    con = _con(a.db)
    try:
        _print({"counts": jobs.counts(con), "recent": [_brief(j) for j in jobs.recent(con, a.limit)]})
        return 0
    finally:
        con.close()


def main(argv=None) -> int:
    observe.setup_logging()
    default_db = os.environ.get("APP_DB") or str(db.DEFAULT_DB)
    ap = argparse.ArgumentParser(prog="python -m app.worker", description="뒤에서 하는 작업의 워커")
    ap.add_argument("--db", default=default_db)
    ap.add_argument("--once", action="store_true", help="대기열이 빌 때까지 처리하고 끝낸다")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("WORKER_INTERVAL") or 5), help="대기열이 비었을 때 기다리는 초")
    ap.add_argument("--batch", type=int, default=20, help="한 번 깰 때 처리하는 작업 수")
    ap.set_defaults(fn=cmd_run)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("enqueue", help="작업을 넣는다")
    p.add_argument("kind", choices=sorted(jobs.HANDLERS))
    p.add_argument("--ym")
    p.add_argument("--area", choices=["raw", "uploads"])
    p.add_argument("--key")
    p.add_argument("--kind", dest="source_kind", help="ingest 의 원본 종류: contract, plan, employee, edu_roster")
    p.add_argument("--title")
    p.add_argument("--by", default="cli")
    p.add_argument("--db", default=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_enqueue)

    p = sub.add_parser("status", help="최근 작업")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--db", default=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_status)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
