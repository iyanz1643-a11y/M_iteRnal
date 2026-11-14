"""수수료 계산 엔진: 회차별 초회/계속 수수료와 환수."""
import json

from app import db
from app.engine import classify


def _rule_sql():
    return """
        SELECT r.* FROM commission_rate_rule r
        WHERE r.insurer_id = :insurer_id
          AND r.commission_type = :ctype
          AND r.installment_from <= :inst
          AND (r.installment_to IS NULL OR r.installment_to >= :inst)
          AND r.effective_from <= :ym
          AND (r.effective_to IS NULL OR r.effective_to >= :ym)
          AND ((r.product_id = :product_id) OR (r.product_id IS NULL AND r.product_category = :category))
        ORDER BY r.priority ASC, r.rule_id DESC
        LIMIT 1
    """


def find_rate_rule(con, insurer_id, product_id, category, ctype, installment_no, ym):
    return con.execute(
        _rule_sql(),
        dict(insurer_id=insurer_id, product_id=product_id, category=category, ctype=ctype, inst=installment_no, ym=ym),
    ).fetchone()


def basis_amount(contract, basis):
    if basis == "monthly_premium":
        return contract["premium_monthly"] or 0
    if basis == "converted_premium":
        return contract["converted_premium"] or 0
    if basis == "annual_premium":
        return (contract["premium_monthly"] or 0) * 12
    raise ValueError(basis)


def _category(row):
    """행의 구분 코드. 아래 조회는 classify.CATEGORY 를 category 로 싣는다.
    contract.* 를 함께 읽으면 계약의 product_category 칸과 이름이 겹치므로 category 를 먼저 본다."""
    keys = row.keys() if hasattr(row, "keys") else row
    return row["category"] if "category" in keys else row["product_category"]


def initial_commission(con, contract):
    """계약의 초회 수수료 (환수 기준액)."""
    rule = find_rate_rule(
        con, contract["insurer_id"], contract["product_id"], _category(contract), "initial", 1, contract["perf_ym"]
    )
    if not rule:
        return 0, None
    return round(basis_amount(contract, rule["basis"]) * rule["rate"]), rule


def compute(con, period_ym):
    """period_ym에 대한 내부 수수료 계산 결과를 리스트로 돌려준다 (DB에 쓰지 않음)."""
    rows = []
    # 1) 해당 월 납입 회차 → 초회/계속 수수료
    payments = con.execute(
        f"""
        SELECT p.contract_id, p.installment_no, c.insurer_id, c.policy_no, c.product_id, c.agent_id,
               c.premium_monthly, c.converted_premium, c.perf_ym, {classify.CATEGORY} AS category, pr.product_code
        FROM premium_payment p
        JOIN contract c ON c.contract_id = p.contract_id
        LEFT JOIN product pr ON pr.product_id = c.product_id
        WHERE p.period_ym = ?
        """,
        (period_ym,),
    ).fetchall()
    for p in payments:
        ctype = "initial" if p["installment_no"] == 1 else "renewal"
        rule = find_rate_rule(con, p["insurer_id"], p["product_id"], p["category"], ctype, p["installment_no"], period_ym)
        if not rule:
            continue
        basis = basis_amount(p, rule["basis"])
        rows.append(
            dict(
                period_ym=period_ym, contract_id=p["contract_id"], agent_id=p["agent_id"], insurer_id=p["insurer_id"],
                policy_no=p["policy_no"], product_code=p["product_code"], commission_type=ctype,
                installment_no=p["installment_no"], basis_amount=basis, rate=rule["rate"],
                amount=round(basis * rule["rate"]), rule_id=rule["rule_id"], clawback_rule_id=None,
            )
        )
    # 2) 해당 월에 실효/해지/철회된 계약 → 환수
    ended = con.execute(
        f"""
        SELECT c.*, {classify.CATEGORY} AS category, pr.product_code,
               (SELECT COUNT(*) FROM premium_payment pp WHERE pp.contract_id = c.contract_id) AS paid_cnt
        FROM contract c
        LEFT JOIN product pr ON pr.product_id = c.product_id
        WHERE c.status IN ('lapsed','cancelled','withdrawn')
          AND substr(replace(c.status_date,'-',''),1,6) = ?
        """,
        (period_ym,),
    ).fetchall()
    for c in ended:
        init_amt, init_rule = initial_commission(con, c)
        if init_amt <= 0:
            continue
        elapsed = max(c["paid_cnt"], 1)
        cb = con.execute(
            """
            SELECT * FROM clawback_rule
            WHERE insurer_id = ? AND (product_category IS NULL OR product_category = ?)
              AND elapsed_from <= ? AND (elapsed_to IS NULL OR elapsed_to >= ?)
              AND effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?)
            ORDER BY product_category IS NULL, rule_id DESC LIMIT 1
            """,
            (c["insurer_id"], c["category"], elapsed, elapsed, period_ym, period_ym),
        ).fetchone()
        if not cb or cb["clawback_rate"] <= 0:
            continue
        rows.append(
            dict(
                period_ym=period_ym, contract_id=c["contract_id"], agent_id=c["agent_id"], insurer_id=c["insurer_id"],
                policy_no=c["policy_no"], product_code=c["product_code"], commission_type="clawback",
                installment_no=None, basis_amount=init_amt, rate=cb["clawback_rate"],
                amount=-round(init_amt * cb["clawback_rate"]), rule_id=init_rule["rule_id"] if init_rule else None,
                clawback_rule_id=cb["rule_id"],
            )
        )
    return rows


def run(con, period_ym, run_by="system"):
    rows = compute(con, period_ym)
    rule_ids = sorted({r["rule_id"] for r in rows if r["rule_id"]})
    run_id = db.new_run(con, "commission", period_ym, run_by, json.dumps({"commission_rate_rule": rule_ids}))
    con.executemany(
        """
        INSERT INTO commission_calc(run_id, period_ym, contract_id, agent_id, commission_type, installment_no,
                                    basis_amount, rate, amount, rule_id, clawback_rule_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (run_id, r["period_ym"], r["contract_id"], r["agent_id"], r["commission_type"], r["installment_no"],
             r["basis_amount"], r["rate"], r["amount"], r["rule_id"], r["clawback_rule_id"])
            for r in rows
        ],
    )
    db.finish_run(con, run_id, f"{len(rows)} lines")
    return run_id, len(rows)
