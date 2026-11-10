"""대사 엔진: 보험사 정산서 라인 ↔ 내부 수수료 계산 매칭, 차이 분류."""
import json

from app import db

TOLERANCE = 10  # 라인당 허용 오차 (원)


def _key(insurer_id, policy_no, ctype, inst):
    return (insurer_id, policy_no, ctype, inst)


def run(con, period_ym, run_by="system", tolerance=TOLERANCE):
    calc_run_id = db.latest_run(con, "commission", period_ym)
    if calc_run_id is None:
        raise RuntimeError(f"{period_ym} 수수료 계산이 먼저 필요합니다")
    run_id = db.new_run(con, "reconciliation", period_ym, run_by, json.dumps({"commission_run": calc_run_id, "tolerance": tolerance}))

    stmt = {}
    for s in con.execute(
        """SELECT s.* FROM commission_statement_line s JOIN import_batch b ON b.batch_id=s.batch_id
           WHERE s.period_ym=? AND b.status IN ('active','loaded')""", (period_ym,)):
        stmt.setdefault(_key(s["insurer_id"], s["policy_no"], s["commission_type"], s["installment_no"]), []).append(s)
    calc = {}
    for c in con.execute(
        """SELECT c.*, k.insurer_id, k.policy_no, k.status AS contract_status FROM commission_calc c
           JOIN contract k ON k.contract_id=c.contract_id WHERE c.run_id=?""", (calc_run_id,)):
        calc.setdefault(_key(c["insurer_id"], c["policy_no"], c["commission_type"], c["installment_no"]), []).append(c)

    ledger = {(r["insurer_id"], r["policy_no"]): r["status"] for r in con.execute("SELECT insurer_id, policy_no, status FROM contract WHERE policy_no IS NOT NULL")}
    n = 0

    def add(ins, pol, ctype, inst, s_id, c_id, s_amt, c_amt, dtype, status="open"):
        nonlocal n
        con.execute(
            """INSERT INTO reconciliation_diff(run_id, period_ym, insurer_id, policy_no, commission_type, installment_no,
               statement_line_id, calc_id, statement_amount, internal_amount, diff_amount, diff_type, status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, period_ym, ins, pol, ctype, inst, s_id, c_id, s_amt, c_amt, (s_amt or 0) - (c_amt or 0), dtype, status))
        n += 1

    for key in set(stmt) | set(calc):
        ins, pol, ctype, inst = key
        s_rows, c_rows = stmt.get(key, []), calc.get(key, [])
        s_amt = sum(r["amount"] for r in s_rows) if s_rows else None
        c_amt = sum(r["amount"] for r in c_rows) if c_rows else None
        s_id = s_rows[0]["line_id"] if s_rows else None
        c_id = c_rows[0]["calc_id"] if c_rows else None
        if s_rows and c_rows:
            diff = s_amt - c_amt
            if abs(diff) <= tolerance:
                continue
            s0, c0 = s_rows[0], c_rows[0]
            if ctype == "clawback":
                dtype = "clawback"
            elif s0["basis_amount"] is not None and abs((s0["basis_amount"] or 0) - c0["basis_amount"]) > tolerance:
                dtype = "conversion"
            else:
                dtype = "rate"
            add(ins, pol, ctype, inst, s_id, c_id, s_amt, c_amt, dtype)
        elif s_rows:
            if (ins, pol) not in ledger:
                dtype = "missing_contract"
            elif ctype in ("promo", "other"):
                dtype = "insurer_only"
            elif ledger[(ins, pol)] in ("lapsed", "cancelled", "withdrawn") and ctype != "clawback":
                dtype = "status_mismatch"
            else:
                dtype = "insurer_only"
            add(ins, pol, ctype, inst, s_id, None, s_amt, None, dtype)
        else:
            add(ins, pol, ctype, inst, None, c_id, None, c_amt, "internal_only")
    db.finish_run(con, run_id, f"{n} diffs")
    return run_id, n
