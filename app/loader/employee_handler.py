"""사원현황 연결 처리기. 보관한 사원현황 파일을 파싱하고 조직과 설계사로 반영한다.

app.connect 의 처리기 꼴 load(con, path, raw, rel_path) -> dict 를 따른다.
register() 가 register_handler("employee", ...) 로 기본 등록부(DEFAULT_REGISTRY)에 넣는다.
app/connect/handlers.py 가 불릴 때 끝에서 register() 를 부르므로 app.connect 를 쓰면 늘 등록되어 있다.
default_registry() 로 새로 만든 등록부에는 들어가지 않는다. 시험이 처리기 없음을 따로 볼 수 있게 그렇게 둔다.

- 파싱은 app/loader/eternal_employee.parse_employee_file, 반영은 app/org/sync.sync_staff 를 그대로 쓴다.
- 같은 파일을 두 번 넣지 않으려고 import_batch 에 data_type='employee' 로 해시를 남긴다.
  원본 행(raw_row)은 남기지 않는다. 사원현황에는 버려야 할 민감 열이 많아서다.
- 머리글이 맞지 않으면 배치를 남기지 않고 실패로 돌려준다. 고친 파일은 다시 넣을 수 있다.
- 배치 기록과 동기화는 한 번에 커밋한다. 중간에 실패하면 Pipeline 이 둘 다 되돌린다.
- 돌려주는 dict 에는 건수만 담는다. sync_staff 의 changes 는 사람 이름이 있어 넘기지 않는다.
"""
from app import db
from app.loader import mapping
from app.loader.eternal_employee import parse_employee_file
from app.loader.excel_loader import DuplicateFile, issue, register_batch
from app.loader.sheet import HeaderMismatch

DATA_TYPE = "employee"
KIND = "employee"


def already_loaded(con, file_hash) -> bool:
    """같은 해시의 파일이 이미 들어왔는가. import_batch.file_hash 는 종류와 상관없이 하나뿐이라 종류를 가리지 않는다."""
    return con.execute("SELECT 1 FROM import_batch WHERE file_hash=?", (file_hash,)).fetchone() is not None


def load_employee_file(con, path, effective_ym, *, via="manual", file_path=None, by="connect") -> dict:
    """사원현황 한 파일을 파싱하고 반영한다. effective_ym 은 이동과 조직장 지정이 시작되는 달이다.
    머리글이 맞지 않으면 HeaderMismatch, 같은 파일이면 DuplicateFile 을 던진다."""
    parsed = parse_employee_file(path, effective_ym)
    rows = parsed["rows"]
    m = mapping.load("eternal", "employee", effective_ym)
    batch_id = register_batch(con, path, DATA_TYPE, effective_ym, via=via, by=by, mapping_version=m["mapping_version"])
    for h in parsed["dropped_columns"]:
        issue(con, batch_id, "warn", "dropped_column", f"민감 정보 열 '{h}' 을 읽지 않고 버렸습니다")
    for w in parsed["warnings"]:
        issue(con, batch_id, w["severity"], "bad_row" if w["severity"] == "block" else "bad_value", w["message"], w["row_no"])
    con.execute("UPDATE import_batch SET status='active', loaded_at=?, row_count=?, file_path=? WHERE batch_id=?",
                (db.now(), len(rows), str(file_path or path), batch_id))
    res = sync_rows(con, rows, effective_ym)               # 여기서 배치 기록과 함께 커밋한다
    block = sum(1 for w in parsed["warnings"] if w["severity"] == "block")
    return {"batch_id": batch_id, "file_rows": len(rows), "block": block, "warn": len(parsed["warnings"]) - block,
            "dropped_columns": list(parsed["dropped_columns"]), "unknown_columns": len(parsed["unknown_columns"]),
            "stats": dict(res["stats"])}


def sync_rows(con, rows, effective_ym) -> dict:
    from app.org import sync

    return sync.sync_staff(con, rows, effective_ym)


def _load(con, path, raw, rel_path) -> dict:
    try:
        return load_employee_file(con, path, raw.period_ym, via=raw.via, file_path=rel_path, by=raw.meta.get("by", "connect"))
    except HeaderMismatch as e:
        con.rollback()
        return {"failed": True, "reason": str(e)}


def register() -> None:
    from app.connect.handlers import register_handler

    register_handler(KIND, _load, already_loaded=already_loaded, duplicate_errors=(DuplicateFile,), needs_period=True)
