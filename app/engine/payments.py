"""납입 회차 반영. 보험사 납입 현황 파일이 없을 때 계약 상태로 해당 월 납입 회차를 추정한다.
(실운영에서는 보험사 납입 현황 파일 적재로 대체)"""
from app.periods import date_ym, ym_diff


def accrue(con, period_ym):
    con.execute("DELETE FROM premium_payment WHERE period_ym=?", (period_ym,))
    n = 0
    for c in con.execute(
        "SELECT * FROM contract WHERE contract_date IS NOT NULL AND status IN ('inforce','withdrawn','lapsed','cancelled','matured')").fetchall():
        c_ym = date_ym(c["contract_date"])
        if c_ym > period_ym:
            continue
        k = ym_diff(period_ym, c_ym) + 1
        if c["payment_mode"] in ("annual", "single") and k != 1:
            continue
        s_ym = date_ym(c["status_date"]) if c["status_date"] else None
        st = c["status"]
        paid = (st in ("inforce", "matured")
                or (st == "withdrawn" and period_ym == c_ym)
                or (st == "lapsed" and (period_ym < s_ym or period_ym == c_ym))
                or (st == "cancelled" and period_ym <= s_ym))
        if not paid:
            continue
        con.execute("INSERT INTO premium_payment(contract_id, period_ym, installment_no, amount, paid_date) VALUES(?,?,?,?,?)",
                    (c["contract_id"], period_ym, k, c["premium"], f"{period_ym[:4]}-{period_ym[4:]}-01"))
        n += 1
    con.commit()
    return n
