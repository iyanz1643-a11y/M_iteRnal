"""고객. 체결한 DB 에서 온다. 계약 잇기, 기념일, 다음 할 일, 해촉자 고객 넘기기와 준법 확인. 커밋은 부르는 쪽이 한다.

| 무엇 | 규칙 |
| --- | --- |
| 계약 잇기 | 담당 본인의 계약(contract.agent_id 가 같음)만 잇는다. 한 계약은 한 고객에게만 |
| 기념일 | 생일과 다른 기념일은 월일만 받는다. 이은 계약의 계약일도 기념일로 본다 |
| 해촉자 고객 | 해촉한 설계사의 고객을 마지막 소속 조직의 조직장에게 넘긴다. 없으면 위 조직의 조직장. 들고 있던 DB 는 회수한다 |
| 준법 확인 | 넘긴 고객은 pending 이다. 메인 관리자가 준법 확인을 남기기 전까지 받은 관리자에게도 연락처를 가린다 |

D-24 제안대로 둔 가정이다. 넘기기와 준법 확인은 메인 관리자만 한다.
"""
from __future__ import annotations

from datetime import date, timedelta

from app import db
from app.sales import core
from app.sales import leads as ld


def _in(ids):
    ids = list(ids) or [-1]
    return ",".join("?" * len(ids)), ids


def get(con, customer_id) -> dict | None:
    r = con.execute("SELECT * FROM sales_customer WHERE customer_id=?", (customer_id,)).fetchone()
    return dict(r) if r else None


def for_agent(con, agent_id) -> list:
    rows = [dict(r) for r in con.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM sales_customer_contract x WHERE x.customer_id=c.customer_id) n_contracts
           FROM sales_customer c WHERE c.agent_id=? ORDER BY COALESCE(c.next_on, '9999'), c.customer_id""", (agent_id,))]
    for c in rows:
        c["handover_ko"] = core.HANDOVER_KO.get(c["handover_status"] or "", "")
    return rows


def access(con, ctx, cust: dict) -> str:
    """core.access 에 넘기기 규칙을 더한다. 준법 확인 전에 넘겨받은 고객은 담당과 직속 관리자에게도 가린다(pending)."""
    a = core.access(con, ctx, cust["agent_id"])
    if a in ("own", "head") and cust.get("handover_status") == "pending":
        return "pending"
    return a


# ---------------------------------------------------------------- 계약
STATUS_KO = {"applied": "청약", "inforce": "정상", "withdrawn": "철회", "declined": "반송", "lapsed": "실효", "cancelled": "해지", "matured": "만기"}


def contracts(con, customer_id) -> list:
    rows = [dict(r) for r in con.execute(
        """SELECT c.contract_id, c.policy_no, c.application_no, c.contract_date, c.application_date, c.status, i.name insurer,
                  COALESCE(p.product_name, c.product_code_raw, c.insurer_product_code) product
           FROM sales_customer_contract x JOIN contract c ON c.contract_id=x.contract_id
           LEFT JOIN insurer i ON i.insurer_id=c.insurer_id LEFT JOIN product p ON p.product_id=c.product_id
           WHERE x.customer_id=? ORDER BY COALESCE(c.contract_date, c.application_date) DESC""", (customer_id,))]
    for r in rows:
        r["status_ko"] = STATUS_KO.get(r["status"], r["status"])
        r["no"] = r["policy_no"] or r["application_no"] or "-"
    return rows


def linkable(con, agent_id, limit=200) -> list:
    """담당 본인의 계약 가운데 아직 어느 고객에도 잇지 않은 것. 최근 것부터. 이름 칸은 읽지 않는다."""
    rows = [dict(r) for r in con.execute(
        """SELECT c.contract_id, c.policy_no, c.application_no, c.contract_date, c.application_date, c.status, i.name insurer
           FROM contract c LEFT JOIN insurer i ON i.insurer_id=c.insurer_id
           WHERE c.agent_id=? AND c.contract_id NOT IN (SELECT contract_id FROM sales_customer_contract)
           ORDER BY COALESCE(c.contract_date, c.application_date) DESC, c.contract_id DESC LIMIT ?""", (agent_id, limit))]
    for r in rows:
        r["status_ko"] = STATUS_KO.get(r["status"], r["status"])
        r["no"] = r["policy_no"] or r["application_no"] or "-"
    return rows


def link(con, customer_id, agent_id, contract_id, by) -> None:
    cust = get(con, customer_id)
    if not cust or cust["agent_id"] != agent_id:
        raise PermissionError("내 고객이 아닙니다.")
    c = con.execute("SELECT agent_id FROM contract WHERE contract_id=?", (contract_id,)).fetchone()
    if not c or c["agent_id"] != agent_id:
        raise PermissionError("내 계약만 이을 수 있습니다.")
    if con.execute("SELECT 1 FROM sales_customer_contract WHERE contract_id=?", (contract_id,)).fetchone():
        raise ValueError("이미 다른 고객에 이은 계약입니다.")
    con.execute("INSERT INTO sales_customer_contract(customer_id, contract_id, linked_by, linked_at) VALUES(?,?,?,?)",
                (customer_id, contract_id, by, db.now()))
    core.audit(con, by, "sales_contract_link", f"customer:{customer_id}", {"contract_id": contract_id})


def set_plan(con, customer_id, agent_id, by, *, next_action=None, next_on=None, birth_md=None, anniv_md=None, anniv_label=None) -> None:
    cust = get(con, customer_id)
    if not cust or cust["agent_id"] != agent_id:
        raise PermissionError("내 고객이 아닙니다.")
    con.execute("""UPDATE sales_customer SET next_action=?, next_on=?, birth_md=?, anniv_md=?, anniv_label=? WHERE customer_id=?""",
                (core.clean_text(next_action, core.MEMO_MAX), core.clean_date(next_on), core.clean_md(birth_md), core.clean_md(anniv_md),
                 core.clean_text(anniv_label, 20), customer_id))


# ---------------------------------------------------------------- 기념일과 할 일
def _next_md(md: str, day: date) -> date | None:
    mm, dd = int(md[:2]), int(md[3:5])
    for y in (day.year, day.year + 1):
        try:
            d = date(y, mm, dd)
        except ValueError:                                   # 2월 29일은 평년에 2월 28일로 본다
            d = date(y, 2, 28)
        if d >= day:
            return d
    return None


def upcoming(con, agent_id, day: date, days: int = 30) -> list:
    """앞으로 days 일 안의 기념일과 다음 할 일. 이름은 담당 본인 화면에만 나간다. 날짜순."""
    end = day + timedelta(days=days)
    out = []
    for c in for_agent(con, agent_id):
        cid = c["customer_id"]
        for md, label in ((c["birth_md"], "생일"), (c["anniv_md"], c["anniv_label"] or "기념일")):
            d = _next_md(md, day) if md else None
            if d and d <= end:
                out.append({"customer_id": cid, "name": c["name"], "date": d.isoformat(), "kind": label, "tone": "blue"})
        if c["next_on"] and c["next_on"] <= end.isoformat():
            out.append({"customer_id": cid, "name": c["name"], "date": c["next_on"], "kind": c["next_action"] or "다음 할 일",
                        "tone": "bad" if c["next_on"] < day.isoformat() else "warn", "todo": True})
        for k in contracts(con, cid):
            cd = k["contract_date"]
            if not cd:
                continue
            d = _next_md(cd[5:10], day)
            if d and d <= end and d.year > int(cd[:4]):
                out.append({"customer_id": cid, "name": c["name"], "date": d.isoformat(), "kind": f"계약 {d.year - int(cd[:4])}주년", "tone": "good"})
    out.sort(key=lambda e: (e["date"], e["customer_id"]))
    return out


# ---------------------------------------------------------------- 해촉자 고객 넘기기
def receiver(con, agent_id, ym) -> int | None:
    """해촉자의 고객을 받을 관리자. 마지막 소속 조직의 조직장, 없으면 위로 올라가며 처음 만나는 조직장. 본인은 뺀다."""
    org = core.last_org(con, agent_id)
    seen = set()
    while org is not None and org not in seen:
        seen.add(org)
        heads = sorted(h for h in core.heads_of(con, org, ym) if h != agent_id)
        active = [h for h in heads if con.execute("SELECT 1 FROM agent WHERE agent_id=? AND status='active'", (h,)).fetchone()]
        if active:
            return active[0]
        r = con.execute("SELECT parent_id FROM org_unit WHERE org_id=?", (org,)).fetchone()
        org = r["parent_id"] if r else None
    return None


def handover_candidates(con, ym) -> list:
    """해촉한 설계사 가운데 고객이나 들고 있는 DB 가 남은 사람. 받을 관리자를 함께 준다."""
    rows = [dict(r) for r in con.execute(
        """SELECT a.agent_id, a.internal_code code, a.name, a.termination_date,
                  (SELECT COUNT(*) FROM sales_customer c WHERE c.agent_id=a.agent_id) n_customers,
                  (SELECT COUNT(*) FROM sales_lead l WHERE l.agent_id=a.agent_id AND l.status IN ('assigned','working')) n_leads
           FROM agent a WHERE a.status='terminated' ORDER BY a.termination_date, a.internal_code""")]
    out = []
    for r in rows:
        if not (r["n_customers"] or r["n_leads"]):
            continue
        to = receiver(con, r["agent_id"], ym)
        t = con.execute("SELECT internal_code, name FROM agent WHERE agent_id=?", (to,)).fetchone() if to else None
        out.append({**r, "to_agent_id": to, "to_name": t["name"] if t else None, "to_code": t["internal_code"] if t else None})
    return out


def handover(con, agent_id, by, ym) -> dict:
    """해촉자의 고객을 받을 관리자에게 넘기고 들고 있던 DB 를 회수한다. 넘긴 고객은 준법 확인 대기다."""
    a = con.execute("SELECT status FROM agent WHERE agent_id=?", (agent_id,)).fetchone()
    if not a or a["status"] != "terminated":
        raise ValueError("해촉한 설계사의 고객만 넘깁니다.")
    to = receiver(con, agent_id, ym)
    custs = [r["customer_id"] for r in con.execute("SELECT customer_id FROM sales_customer WHERE agent_id=?", (agent_id,))]
    if custs and not to:
        raise ValueError("받을 관리자를 찾지 못했습니다. 조직장 지정을 먼저 확인해 주세요.")
    now = db.now()
    for cid in custs:
        con.execute("""UPDATE sales_customer SET agent_id=?, handed_from_agent_id=?, handover_status='pending', handover_at=?, handover_by=?,
                       compliance_by=NULL, compliance_at=NULL WHERE customer_id=?""", (to, agent_id, now, by, cid))
    n_leads = 0
    for r in con.execute("SELECT lead_id FROM sales_lead WHERE agent_id=? AND status IN ('assigned','working')", (agent_id,)).fetchall():
        ld.reclaim(con, r["lead_id"], by, ym, reason="해촉")
        n_leads += 1
    core.audit(con, by, "sales_handover", f"agent:{agent_id}", {"to_agent_id": to, "customers": len(custs), "leads": n_leads})
    return {"customers": len(custs), "leads": n_leads, "to_agent_id": to}


def pending_handover(con) -> list:
    rows = [dict(r) for r in con.execute(
        """SELECT c.customer_id, c.name, c.handover_at, c.agent_id, a.name to_name, a.internal_code to_code, f.name from_name, f.internal_code from_code,
                  (SELECT COUNT(*) FROM sales_customer_contract x WHERE x.customer_id=c.customer_id) n_contracts
           FROM sales_customer c JOIN agent a ON a.agent_id=c.agent_id LEFT JOIN agent f ON f.agent_id=c.handed_from_agent_id
           WHERE c.handover_status='pending' ORDER BY c.handover_at, c.customer_id""")]
    for r in rows:
        r["name"] = core.mask_name(r["name"])               # 메인 관리자 목록에서도 이름은 가린다
    return rows


def confirm(con, customer_id, by) -> None:
    cust = get(con, customer_id)
    if not cust or cust["handover_status"] != "pending":
        raise ValueError("준법 확인 대기인 고객이 아닙니다.")
    con.execute("UPDATE sales_customer SET handover_status='confirmed', compliance_by=?, compliance_at=? WHERE customer_id=?",
                (by, db.now(), customer_id))
    core.audit(con, by, "sales_handover_confirm", f"customer:{customer_id}")
