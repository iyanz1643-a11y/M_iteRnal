"""DB 백업. 규칙은 app/ops/backup.py 의 머리말이다.

    python scripts/backup_db.py                         # APP_DB 를 BACKUP_DIR(없으면 data/backup)에 암호화해 백업. BACKUP_KEY 가 없으면 멈춘다
    python scripts/backup_db.py --db data/demo.db --out /backups --keep 30
    python scripts/backup_db.py --no-encrypt            # 평문 백업. 키 없이 만들려면 이것을 분명히 줘야 한다

키는 환경 변수 BACKUP_KEY 로만 받는다. 출력에는 파일 이름, 크기, 표 수, 줄 수만 낸다.
끝난 뒤 돌려주는 값: 0 성공, 1 실패.
"""
import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.ops import backup  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DB 백업. 기본으로 암호화하고, 환경 변수 BACKUP_KEY 가 없으면 --no-encrypt 를 주지 않는 한 멈춘다")
    ap.add_argument("--db", default=os.environ.get("APP_DB") or str(db.DEFAULT_DB))
    ap.add_argument("--out", help="백업 폴더. 없으면 BACKUP_DIR, 그것도 없으면 data/backup")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--encrypt", dest="encrypt", action="store_true", default=None, help="암호화한다. 기본값이다. BACKUP_KEY 가 있어야 한다")
    g.add_argument("--no-encrypt", dest="encrypt", action="store_false", help="암호화하지 않은 평문 백업을 만든다. BACKUP_KEY 가 없을 때 평문을 만드는 유일한 길이다")
    ap.add_argument("--keep", type=int, help="이 폴더의 백업을 최근 N 개만 남긴다. 주지 않으면 지우지 않는다")
    a = ap.parse_args(argv)
    try:
        m = backup.backup(a.db, a.out, encrypt=a.encrypt, keep=a.keep)
    except backup.BackupError as e:
        print(f"백업 실패: {e}", file=sys.stderr)
        return 1
    if not m["encrypted"]:
        print("주의: 암호화하지 않은 백업입니다. 보관 위치의 접근 권한을 확인해 주세요.", file=sys.stderr)
    print(f"백업: {m['file']} 크기 {m['size']} 표 {len(m['tables'])} 줄 {sum(m['tables'].values())} 암호화 {'예' if m['encrypted'] else '아니오'}"
          + (f" 지운 옛 백업 {len(m['removed'])}" if m.get("removed") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
