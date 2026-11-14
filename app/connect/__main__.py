"""연결 명령줄.

    python -m app.connect ingest <파일> --kind contract --ym 202610
    python -m app.connect watch <폴더> [--kind plan] [--ym 202610] [--loop --interval 60]
    python -m app.connect status [--db data/demo.db]
    python -m app.connect migrate-state [--json data/state/connections.json]
    python -m app.connect gdrive
    python -m app.connect card-status <명함 주소>
    python -m app.connect card-list

끝난 뒤 돌려주는 값: 0 성공, 1 실패가 하나라도 있음, 2 설정 안 됨.
출력에는 사람 이름과 금액을 넣지 않는다. card-list 는 상태별 건수만 보인다.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from app import db
from app.connect.base import KINDS, NOT_CONFIGURED, OK
from app.connect.pipeline import Pipeline
from app.connect.sources import FolderWatchSource, GoogleDriveSource, UploadSource
from app.connect.state import StateStore, default_state_db


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _open_db(path) -> object:
    p = pathlib.Path(path)
    if not p.exists():
        raise SystemExit(f"DB 가 없습니다: {p}. --db 로 경로를 정해 주세요")
    return db.connect(p)


def _code(run) -> int:
    if run.status == NOT_CONFIGURED:
        return 2
    return 0 if run.ok else 1


def cmd_ingest(a) -> int:
    con = _open_db(a.db)
    try:
        meta = {"title": a.title} if a.title else {}
        run = Pipeline(con).run(UploadSource.from_path(a.file, kind=a.kind, period_ym=a.ym, **meta))
        _print(run.to_dict())
        return _code(run)
    finally:
        con.close()


def cmd_watch(a) -> int:
    con = _open_db(a.db)
    try:
        state = StateStore(con=con)
        src = FolderWatchSource(a.folder, kind=a.kind, period_ym=a.ym, state=state, min_age=a.settle, rescan=a.rescan)
        pipe = Pipeline(con, state=state)
        while True:
            run = pipe.run(src)
            _print(run.to_dict())
            if not a.loop:
                return _code(run)
            src.rescan = False
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        con.close()


def _state(a) -> StateStore:
    """명령줄의 상태 기록. DB 파일이 없으면 새로 만들지 않는다."""
    return StateStore(db_path=a.db, create=False)


def cmd_status(a) -> int:
    if not pathlib.Path(a.db).exists():
        _print({})
        return 0
    _print(_state(a).all())
    return 0


def cmd_migrate_state(a) -> int:
    if not pathlib.Path(a.db).exists():
        raise SystemExit(f"DB 가 없습니다: {a.db}. --db 로 경로를 정해 주세요")
    out = _state(a).migrate_json(a.json)
    _print(out)
    return 1 if out.get("error") else 0


def cmd_gdrive(a) -> int:
    run = Pipeline(None).run(GoogleDriveSource(), attempts=1)   # 지금은 가져오기 전에 끝나서 DB 가 필요 없다
    _print(run.to_dict())
    return _code(run)


def cmd_card_status(a) -> int:
    from app.connect.card_site import CardSiteClient

    out = CardSiteClient(state=_state(a)).status(a.slug)
    _print({"status": out.status, "reason": out.reason, "card": out.value.to_dict() if out.value else None})
    return 0 if out.status == OK else (2 if out.status == NOT_CONFIGURED else 1)


def cmd_card_list(a) -> int:
    from app.connect.card_site import CardSiteClient

    out = CardSiteClient(state=_state(a)).admin_list()
    counts: dict = {}
    for e in out.value or []:
        counts[e.request_status] = counts.get(e.request_status, 0) + 1
    _print({"status": out.status, "reason": out.reason, "live": sum(1 for e in out.value or [] if e.live), "by_request_status": counts})
    return 0 if out.status == OK else (2 if out.status == NOT_CONFIGURED else 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.connect", description="운영 연결: 파일 가져오기와 연결 상태")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="파일 하나를 직접 올린다")
    p.add_argument("file")
    p.add_argument("--kind", required=True, choices=list(KINDS))
    p.add_argument("--ym", help="귀속월 YYYYMM")
    p.add_argument("--title", help="시상안 제목")
    p.add_argument("--db", default=str(db.DEFAULT_DB))
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("watch", help="폴더의 새 파일을 가져온다")
    p.add_argument("folder")
    p.add_argument("--kind", choices=list(KINDS), help="없으면 폴더와 파일 이름으로 짐작한다")
    p.add_argument("--ym", help="없으면 파일 이름에서 짐작한다")
    p.add_argument("--db", default=str(db.DEFAULT_DB))
    p.add_argument("--settle", type=float, default=2.0, help="바뀐 지 이 초가 지난 파일만 읽는다")
    p.add_argument("--rescan", action="store_true", help="감시 목록을 무시하고 모두 다시 본다")
    p.add_argument("--loop", action="store_true", help="멈출 때까지 되풀이한다")
    p.add_argument("--interval", type=float, default=60.0)
    p.set_defaults(fn=cmd_watch)

    state_db = str(default_state_db())

    p = sub.add_parser("status", help="연결마다 마지막 성공 시각과 실패 사유")
    p.add_argument("--db", default=state_db)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("migrate-state", help="예전 상태 파일 connections.json 을 표로 옮긴다. 파일은 그대로 둔다")
    p.add_argument("--json", help="없으면 CONNECT_STATE_DIR 아래 connections.json")
    p.add_argument("--db", default=state_db)
    p.set_defaults(fn=cmd_migrate_state)

    p = sub.add_parser("gdrive", help="구글 드라이브 가져오기. 지금은 설정 확인만 한다")
    p.set_defaults(fn=cmd_gdrive)

    p = sub.add_parser("card-status", help="명함 하나의 공개 상태")
    p.add_argument("slug")
    p.add_argument("--db", default=state_db)
    p.set_defaults(fn=cmd_card_status)

    p = sub.add_parser("card-list", help="명함 관리자 목록의 상태별 건수")
    p.add_argument("--db", default=state_db)
    p.set_defaults(fn=cmd_card_list)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
