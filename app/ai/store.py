"""AI 표 묶음과 넣은 것과 나온 것의 기록.

표의 정의는 schema/schema.sql 의 [ai:begin] 묶음이다. 새 DB 는 init_db 가 만들고, 오래된 DB 는 ensure 가 묶음만 돌린다.
기록은 지우지 않는다. 사람의 결재는 같은 줄의 decided_* 칸과 edited_text 를 채운다.
기록에는 가린 뒤의 컨텍스트만 남는다. 가리기 전의 글은 어디에도 남기지 않는다.
"""
from __future__ import annotations

import json
import sqlite3

from app import db
from app.ops import schema as ops_schema

BEGIN, END = "-- [ai:begin]", "-- [ai:end]"
TABLES = ("ai_budget", "ai_call", "ai_output", "ai_review", "ai_review_opinion")


def ensure(con) -> None:
    """AI 표가 하나라도 없으면 묶음을 돌린다."""
    try:
        have = {r[0] for r in con.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({','.join('?' * len(TABLES))})", TABLES)}
    except sqlite3.DatabaseError:
        return
    if len(have) < len(TABLES):
        ops_schema.run_block(con, BEGIN, END)


def _j(v):
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


def save_output(con, *, feature, login_id, context, status, output_text=None, parsed=None, problems=None,
                call_id=None, ref_kind=None, ref_id=None) -> int:
    """기능 하나의 한 번 실행을 남긴다. context 는 context.Context 다."""
    ensure(con)
    oid = con.execute(
        """INSERT INTO ai_output(feature, login_id, created_at, input_hash, guide_version, context_json, output_text, parsed_json,
                                 status, problems_json, call_id, ref_kind, ref_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (feature, login_id, db.now(), context.input_hash if context else None, context.guide_version if context else None,
         _j(context.record()) if context else None, output_text, _j(parsed), status, _j(problems or []), call_id, ref_kind, ref_id),
    ).lastrowid
    con.commit()
    return oid


def decide_output(con, output_id, *, by, approve: bool, edited_text=None, reason=None) -> dict:
    """사람의 승인이나 반려를 같은 줄에 남긴다. 이미 결재한 줄은 다시 결재하지 않는다."""
    ensure(con)
    row = con.execute("SELECT * FROM ai_output WHERE output_id=?", (output_id,)).fetchone()
    if not row:
        raise ValueError("없는 기록입니다")
    if row["decision"]:
        raise ValueError("이미 결재한 기록입니다")
    if row["status"] != "draft":
        raise ValueError("자동 점검을 통과한 초안만 결재합니다")
    if not approve and not (reason or "").strip():
        raise ValueError("반려 사유를 적어 주세요")
    con.execute("UPDATE ai_output SET decision=?, decided_by=?, decided_at=?, edited_text=?, decision_reason=? WHERE output_id=?",
                ("approved" if approve else "rejected", by, db.now(), edited_text, reason, output_id))
    con.commit()
    return get_output(con, output_id)


def get_output(con, output_id) -> dict | None:
    ensure(con)
    r = con.execute("SELECT * FROM ai_output WHERE output_id=?", (output_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("context_json", "parsed_json", "problems_json"):
        d[k[:-5]] = json.loads(d[k]) if d[k] else None
    return d


def list_outputs(con, feature=None, ref_kind=None, ref_id=None, limit=50) -> list:
    ensure(con)
    where, args = [], []
    for col, v in (("feature", feature), ("ref_kind", ref_kind), ("ref_id", ref_id)):
        if v is not None:
            where.append(f"{col}=?")
            args.append(v)
    sql = "SELECT output_id FROM ai_output" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY output_id DESC LIMIT ?"
    return [get_output(con, r[0]) for r in con.execute(sql, (*args, limit))]


def measures(con, feature=None) -> dict:
    """docs/13 2.4 의 재는 것: 승인율, 사람이 고친 비율, 반려 사유별 건수."""
    ensure(con)
    cond, args = ("WHERE feature=?", (feature,)) if feature else ("", ())
    rows = [dict(r) for r in con.execute(f"SELECT decision, edited_text, decision_reason FROM ai_output {cond}", args) if r["decision"]]
    n = len(rows)
    approved = [r for r in rows if r["decision"] == "approved"]
    reasons: dict = {}
    for r in rows:
        if r["decision"] == "rejected":
            reasons[r["decision_reason"]] = reasons.get(r["decision_reason"], 0) + 1
    return {"decided": n, "approval_rate": (len(approved) / n) if n else None,
            "edited_rate": (sum(1 for r in approved if r["edited_text"]) / len(approved)) if approved else None,
            "reject_reasons": reasons}
