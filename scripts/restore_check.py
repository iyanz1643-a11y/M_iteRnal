"""되살리기 시험과 되살리기. 규칙은 app/ops/backup.py 의 머리말이다.

    python scripts/restore_check.py                                  # BACKUP_DIR 의 가장 최근 백업을 임시 폴더에 풀어 확인한다
    python scripts/restore_check.py data/backup/app-20261115-030000.db.enc
    python scripts/restore_check.py <백업> --restore-to data/restored.db [--force]

확인은 원래 DB 를 건드리지 않는다. 되살리기는 대상이 있으면 멈추고, --force 면 있던 파일을 옆으로 옮긴 뒤 바꾼다.
키는 환경 변수 BACKUP_KEY 로만 받는다. 끝난 뒤 돌려주는 값: 0 맞음, 1 다름이나 실패.
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.ops import backup  # noqa: E402


def latest(folder: pathlib.Path):
    files = sorted(p for p in folder.glob("app-*") if backup.NAME_RE.match(p.name)) if folder.is_dir() else []
    return files[-1] if files else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="되살리기 시험")
    ap.add_argument("file", nargs="?", help="백업 파일. 없으면 BACKUP_DIR 의 가장 최근 것")
    ap.add_argument("--restore-to", help="확인한 뒤 이 경로로 되살린다")
    ap.add_argument("--force", action="store_true", help="대상이 있어도 바꾼다. 있던 파일은 옆으로 옮긴다")
    a = ap.parse_args(argv)
    f = pathlib.Path(a.file) if a.file else latest(backup.default_dir())
    if not f or not f.exists():
        print("확인할 백업이 없습니다.", file=sys.stderr)
        return 1
    try:
        out = backup.restore(f, a.restore_to, force=a.force) if a.restore_to else backup.verify(f)
    except backup.BackupError as e:
        print(f"되살리기 실패: {f.name}: {e}", file=sys.stderr)
        return 1
    print(f"되살리기 {'완료' if a.restore_to else '시험 통과'}: {out['file']} 표 {out['tables']} 줄 {out['rows']} 스키마 버전 {out['schema_version']}"
          + (f" 옮겨 둔 파일 {out['moved']}" if out.get("moved") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
