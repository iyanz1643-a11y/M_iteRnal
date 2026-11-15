"""수수료·점검(파트 G)의 웹용 함수. 화면 /settle/commission, /settle/clawback, /settle/recon 이 부른다.

계산은 기존 엔진이 한다. commission.py 가 요율표로 자체 계산을 commission_calc 에 남기고, reconcile.py 가 정산서와 자체 계산을
맞춰 reconciliation_diff 를 만든다. 이 파일은 그 결과를 범위 안 설계사 목록으로만 읽고, 대사 차이의 처리 상태를 남긴다.

D-11 은 제안대로 둔 가정이다. 정산서 기준으로 점검하고, 요율표가 있는 보험사만 자체 계산이 있다.
- 금액의 기준은 정산서다. 그 달 그 보험사의 정산서가 들어와 있으면 정산서 줄을 쓴다.
- 정산서가 아직 없고 요율표로 계산한 줄만 있으면 그 줄을 쓰고 estimated=True 로 넘긴다. 화면은 '예상'을 붙인다.
- 둘 다 없는 보험사는 나오지 않는다. 금액을 지어내지 않는다.
- 지급은 환수가 아닌 줄의 합, 환수는 환수 줄의 합(음수), 순액은 둘의 합이다. v_agent_monthly_commission 과 같은 나눔이다.
- 정산서는 적재 상태가 active 또는 loaded 인 배치만 읽는다. 대사(reconcile.run)와 같다.
- 조정(adjustment)은 순액에 더하지 않는다. 마감을 엔진으로 옮긴 뒤에도 그 달 지급에 넣지 않는다. 넣을지는 돈 기준이라 정하지 않았다.

D-23 초년도 지급 한도 점검은 규정 확인 전이라 넣지 않았다.
D-17. 관리자용 함수는 없다. 수수료는 메인 관리자가 전체를, 설계사와 관리자는 본인만 본다. 화면은 본인 한 사람의 목록을 넘긴다.

범위. 모든 조회 함수는 범위 안 설계사 목록(agent_ids)을 받는다. 정산서의 설계사 코드가 매핑되지 않은 줄과 담당자가 없는 계약은
agent_id 가 비어 있다. 그 줄은 unmapped=True 일 때만 든다. 메인 관리자(범위 all)만 True 로 부른다.

대사 차이의 처리 상태. 화면의 세 상태는 checked(확인함), asked(보험사 문의), resolved(해결)이고 처리 전은 open(미처리)이다.
- 처리는 recon_diff_action 에 한 줄씩 쌓고 지우지 않는다. 지금 상태는 마지막 줄이다.
- 대사를 다시 돌리면 diff_id 가 새로 생긴다. 처리 기록은 (월, 보험사, 증권번호, 수수료 유형, 회차)로 묶어 새 차이로 이어진다.
- 처리한 뒤 차이 금액이 바뀌면 changed=True 다. 상태는 그대로 두고 화면이 알린다.
- 처리는 메인 관리자만 하고 메모가 있어야 한다(docs/04 의 처리 흐름). 마감한 달은 처리할 수 없다.
- 기존 도구의 reconciliation_diff.status(open, confirmed, adjusted, ignored)는 건드리지 않는다. legacy_status 로 함께 넘긴다.
  마감 조건은 recon_diff_action 한 가지로 본다. legacy_status 는 참고 건수로만 넘긴다.

월 마감. close_check, close_month, reopen_month 가 웹과 기존 도구의 마감 규칙 한 곳이다. 마감 조건은 가정이다.
- 막는 것: 차단 등급 품질 문제, 검토 대기 시상 공문, 정산서가 들어왔는데 대사를 돌리지 않음, 대사 뒤에 들어온 정산서,
  처리하지 않은(open) 대사 차이.
- 알리는 것: 보험사 문의(asked) 중인 차이, 처리한 뒤 금액이 바뀐 차이, 정산서가 없어 예상으로 남는 보험사, 정산서도 자체 계산도 없는 달.
- 마감과 해제는 메인 관리자만 한다. 계정 표에서 다시 확인한다. 해제는 사유가 있어야 한다.
- close_period 에 상태만 쓰고 audit_log 에 close, close_reopen 을 남긴다. 금액 묶음(snapshot)은 만들지 않는다.

계약을 가리킬 때는 증권번호 끝 네 자리(policy_tail)만 넘긴다. 계약자 이름은 넘기지 않는다 (D-05).
"""
import json
import re
import sqlite3

from app import db

BEGIN, END = "-- [settle:begin]", "-- [settle:end]"
TABLES = ("recon_diff_action",)

CTYPE_KO = {"initial": "초회", "renewal": "계속", "persistency": "유지", "promo": "시책", "clawback": "환수", "other": "기타"}
DIFF_KO = {"rate": "요율 차이", "conversion": "기준보험료 차이", "status_mismatch": "계약 상태 불일치", "missing_contract": "원장에 없는 계약",
           "insurer_only": "보험사만 지급", "internal_only": "자체 계산만 있음", "clawback": "환수 차이", "rounding": "절사 차이"}
STATUS_KO = {"open": "미처리", "checked": "확인함", "asked": "보험사 문의", "resolved": "해결"}
ACTIONS = ("checked", "asked", "resolved")
SOURCE_KO = {"statement": "정산서", "calc": "자체 계산"}
LIVE_BATCH = ("active", "loaded")
REASON_MAX = 500                    # 해제 사유의 글자 수 상한. 웹 화면의 메모 상한과 같다
BASIS_NOTE = ("정산서가 들어온 보험사는 정산서 금액입니다. 정산서가 아직 없고 요율표가 있는 보험사는 자체 계산이라 예상 금액입니다. "
              "환수는 음수이고 순액은 지급과 환수의 합입니다.")
_STATEMENTS: list | None = None


# ---------------------------------------------------------------- 표 준비
def statements() -> list:
    """schema.sql 의 [settle] 묶음을 문 하나씩 나눠 돌려준다."""
    global _STATEMENTS
    if _STATEMENTS is None:
        text = db.SCHEMA.read_text(encoding="utf-8")
        i, j = text.find(BEGIN), text.find(END)
        if i < 0 or j < i:
            raise RuntimeError("schema.sql 에 수수료·점검 묶음이 없습니다")
        body = "\n".join(re.sub(r"--.*$", "", line) for line in text[i + len(BEGIN):j].splitlines())
        out, buf = [], ""
        for part in body.split(";"):
            buf += part + ";"
            if sqlite3.complete_statement(buf):
                if buf.strip(" \n;"):
                    out.append(buf.strip())
                buf = ""
        _STATEMENTS = out
    return list(_STATEMENTS)


def ensure(con) -> None:
    """처리 기록 표가 없는 오래된 DB 에서 표를 만든다. 이미 있으면 아무것도 하지 않는다."""
    have = {r[0] for r in con.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({','.join('?' * len(TABLES))})", TABLES)}
    if have == set(TABLES):
        return
    for sql in statements():
        con.execute(sql)
    con.commit()


# ---------------------------------------------------------------- 도우미
def _tail(policy_no) -> str | None:
    return policy_no[-4:] if policy_no else None


def _scope(col: str, agent_ids, unmapped: bool) -> tuple[str, list]:
    """범위 조건. agent_ids 안의 사람, unmapped 면 agent_id 가 빈 줄도."""
    ids = sorted({a for a in (agent_ids or ()) if a is not None})
    parts, params = [], []
    if ids:
        parts.append(f"{col} IN ({','.join('?' * len(ids))})")
        params += ids
    if unmapped:
        parts.append(f"{col} IS NULL")
    return ("(" + " OR ".join(parts) + ")") if parts else "0", params


def _sums(rows) -> dict:
    paid = sum(r["amount"] for r in rows if r["commission_type"] != "clawback")
    claw = sum(r["amount"] for r in rows if r["commission_type"] == "clawback")
    return {"paid": paid, "clawback": claw, "net": paid + claw, "lines": len(rows)}


def rate_insurers(con, ym) -> set:
    """그 달에 적용되는 요율표가 있는 보험사. 이 보험사만 자체 계산이 있다 (D-11 가정)."""
    return {r[0] for r in con.execute(
        "SELECT DISTINCT insurer_id FROM commission_rate_rule WHERE effective_from<=? AND (effective_to IS NULL OR effective_to>=?)", (ym, ym))}


def statement_insurers(con, ym) -> dict:
    """그 달 정산서가 들어온 보험사와 마지막 적재 시각. {insurer_id: loaded_at}"""
    marks = ",".join("?" * len(LIVE_BATCH))
    return {r["insurer_id"]: r["t"] for r in con.execute(
        f"""SELECT insurer_id, MAX(COALESCE(loaded_at, received_at)) t FROM import_batch
            WHERE data_type='statement' AND period_ym=? AND status IN ({marks}) AND insurer_id IS NOT NULL GROUP BY insurer_id""",
        (ym, *LIVE_BATCH))}


def _statement_lines(con, agent_ids, ym, unmapped=False) -> list:
    cond, params = _scope("s.agent_id", agent_ids, unmapped)
    marks = ",".join("?" * len(LIVE_BATCH))
    return [dict(r) for r in con.execute(
        f"""SELECT s.insurer_id, s.agent_id, s.policy_no, s.commission_type, s.installment_no, s.amount
            FROM commission_statement_line s JOIN import_batch b ON b.batch_id=s.batch_id
            WHERE s.period_ym=? AND b.status IN ({marks}) AND {cond}""", (ym, *LIVE_BATCH, *params))]


def _calc_lines(con, agent_ids, ym, unmapped=False) -> list:
    run_id = db.latest_run(con, "commission", ym)
    if run_id is None:
        return []
    cond, params = _scope("c.agent_id", agent_ids, unmapped)
    return [dict(r) for r in con.execute(
        f"""SELECT k.insurer_id, c.agent_id, k.policy_no, c.commission_type, c.installment_no, c.amount
            FROM commission_calc c JOIN contract k ON k.contract_id=c.contract_id
            WHERE c.run_id=? AND {cond}""", (run_id, *params))]


def _insurers(con) -> dict:
    return {r["insurer_id"]: {"insurer": r["name"], "sector": r["sector"]} for r in con.execute("SELECT insurer_id, name, sector FROM insurer")}


# ---------------------------------------------------------------- 월별 정산서 요약
def statement_summary(con, agent_ids, ym, *, unmapped=False) -> dict:
    """그 달 보험사별 정산서의 지급, 환수, 순액. 자체 계산과 대사 차이 건수를 옆에 둔다.

    돌려주는 것: {ym, note, rows: [...], total: {paid, clawback, net, lines}, calc_total, open_count}
    rows 는 [{insurer_id, insurer, sector, has_statement, has_rates, loaded_at, statement: {paid, clawback, net, lines} 또는 None,
             calc: {...} 또는 None, diff_count, open_count}] 보험사 이름 순.
    statement 가 None 이면 그 달 정산서가 아직 없다. calc 가 None 이면 요율표가 없어 자체 계산이 없다.
    total 은 정산서 금액만 더한다. 자체 계산은 calc_total 에 따로 둔다.
    """
    out = {"ym": ym, "note": BASIS_NOTE, "rows": [], "total": _sums([]), "calc_total": _sums([]), "open_count": 0}
    if not agent_ids and not unmapped:
        return out
    loaded, rates, names = statement_insurers(con, ym), rate_insurers(con, ym), _insurers(con)
    stmt, calc = _statement_lines(con, agent_ids, ym, unmapped), _calc_lines(con, agent_ids, ym, unmapped)
    diffs = {}
    for d in diff_list(con, agent_ids, ym, unmapped=unmapped)["rows"]:
        c = diffs.setdefault(d["insurer_id"], [0, 0])
        c[0] += 1
        c[1] += d["status"] == "open"
    for iid in sorted(set(loaded) | {r["insurer_id"] for r in stmt} | {r["insurer_id"] for r in calc},
                      key=lambda i: (names.get(i, {}).get("insurer") or "", i)):
        s_rows = [r for r in stmt if r["insurer_id"] == iid]
        c_rows = [r for r in calc if r["insurer_id"] == iid]
        has_stmt = iid in loaded or bool(s_rows)
        n_diff, n_open = diffs.get(iid, (0, 0))
        out["rows"].append({"insurer_id": iid, **names.get(iid, {"insurer": None, "sector": None}), "has_statement": has_stmt,
                            "has_rates": iid in rates, "loaded_at": loaded.get(iid), "statement": _sums(s_rows) if has_stmt else None,
                            "calc": _sums(c_rows) if (c_rows or iid in rates) else None, "diff_count": n_diff, "open_count": n_open})
    out["total"] = _sums(stmt)
    out["calc_total"] = _sums(calc)
    out["open_count"] = sum(r["open_count"] for r in out["rows"])
    return out


# ---------------------------------------------------------------- 대사 차이
def _key(d) -> tuple:
    return (d["period_ym"], d["insurer_id"], d["policy_no"], d["commission_type"], d["installment_no"])


def _actions(con, key) -> list:
    ensure(con)
    ym, ins, pol, ctype, inst = key
    return [dict(r) for r in con.execute(
        """SELECT action_id, diff_id, run_id, status, diff_amount, note, acted_by, acted_at FROM recon_diff_action
           WHERE period_ym=? AND insurer_id=? AND policy_no=? AND commission_type IS ? AND installment_no IS ? ORDER BY action_id""",
        (ym, ins, pol, ctype, inst))]


def _last_actions(con, ym) -> dict:
    ensure(con)
    out = {}
    for r in con.execute("SELECT * FROM recon_diff_action WHERE period_ym=? ORDER BY action_id", (ym,)):
        out[_key(r)] = dict(r)
    return out


def diff_list(con, agent_ids, ym, *, unmapped=False, insurer_id=None, diff_type=None, status=None) -> dict:
    """그 달 마지막 대사의 차이 목록. 차이 금액의 절댓값이 큰 순.

    돌려주는 것: {ym, run_id, ran_at, counts: {open, checked, asked, resolved}, rows: [...]}
    rows 는 [{diff_id, insurer_id, insurer, diff_type, diff_type_ko, commission_type, commission_type_ko, installment_no, policy_tail,
             statement_amount, internal_amount, diff_amount, agent_id, status, status_ko, note, acted_by, acted_at, changed, legacy_status}].
    사람은 정산서 줄의 설계사, 없으면 자체 계산 줄의 설계사다. 둘 다 없으면 unmapped=True 일 때만 든다.
    counts 는 거르기(insurer_id, diff_type, status) 전의 범위 안 건수다.
    """
    run = con.execute("SELECT run_id, finished_at FROM calc_run WHERE run_type='reconciliation' AND period_ym=? AND status='done' ORDER BY run_id DESC LIMIT 1",
                      (ym,)).fetchone()
    out = {"ym": ym, "run_id": run["run_id"] if run else None, "ran_at": run["finished_at"] if run else None,
           "counts": {k: 0 for k in STATUS_KO}, "rows": []}
    if not run or (not agent_ids and not unmapped):
        return out
    cond, params = _scope("COALESCE(s.agent_id, c.agent_id)", agent_ids, unmapped)
    rows = con.execute(
        f"""SELECT d.*, i.name insurer, COALESCE(s.agent_id, c.agent_id) who FROM reconciliation_diff d
            JOIN insurer i ON i.insurer_id=d.insurer_id
            LEFT JOIN commission_statement_line s ON s.line_id=d.statement_line_id
            LEFT JOIN commission_calc c ON c.calc_id=d.calc_id
            WHERE d.run_id=? AND {cond}""", (run["run_id"], *params)).fetchall()
    last = _last_actions(con, ym)
    for d in rows:
        a = last.get(_key(d))
        st = a["status"] if a else "open"
        out["counts"][st] += 1
        if (insurer_id and d["insurer_id"] != insurer_id) or (diff_type and d["diff_type"] != diff_type) or (status and st != status):
            continue
        out["rows"].append({
            "diff_id": d["diff_id"], "insurer_id": d["insurer_id"], "insurer": d["insurer"], "diff_type": d["diff_type"],
            "diff_type_ko": DIFF_KO.get(d["diff_type"], d["diff_type"]), "commission_type": d["commission_type"],
            "commission_type_ko": CTYPE_KO.get(d["commission_type"], d["commission_type"]), "installment_no": d["installment_no"],
            "policy_tail": _tail(d["policy_no"]), "statement_amount": d["statement_amount"], "internal_amount": d["internal_amount"],
            "diff_amount": d["diff_amount"], "agent_id": d["who"], "status": st, "status_ko": STATUS_KO[st],
            "note": a["note"] if a else None, "acted_by": a["acted_by"] if a else None, "acted_at": a["acted_at"] if a else None,
            "changed": bool(a) and a["diff_amount"] != d["diff_amount"], "legacy_status": d["status"]})
    out["rows"].sort(key=lambda r: (-abs(r["diff_amount"] or 0), r["insurer"] or "", r["diff_id"]))
    return out


def _require_main(con, by) -> str:
    """메인 관리자만 통과한다. by 는 login_id 이거나 auth.context() 의 결과다. 계정 표에서 다시 확인한다."""
    if isinstance(by, dict):
        if by.get("role") != "main":
            raise PermissionError("메인 관리자만 할 수 있습니다.")
        by = by.get("login_id")
    row = con.execute("SELECT is_main_admin FROM user_account WHERE login_id=? AND status='active'", (by,)).fetchone() if by else None
    if not row or not row["is_main_admin"]:
        raise PermissionError("메인 관리자만 할 수 있습니다.")
    return by


def set_diff_status(con, diff_id, status, note, by) -> dict:
    """대사 차이 하나의 처리 상태를 남긴다. status 는 checked, asked, resolved 가운데 하나다.

    메인 관리자만 한다. 메모가 있어야 한다. 그 달 마지막 대사의 차이만 받는다. 마감한 달은 받지 않는다.
    한 줄을 쌓고 audit_log 에 diff_update 를 남긴다. 돌려주는 것: 새 처리 줄 {action_id, diff_id, status, status_ko, note, acted_by, acted_at}.
    """
    who = _require_main(con, by)
    if status not in ACTIONS:
        raise ValueError("처리 상태는 확인함, 보험사 문의, 해결 가운데 하나입니다.")
    note = (note or "").strip()
    if not note:
        raise ValueError("처리 메모를 적어 주세요.")
    d = con.execute("SELECT * FROM reconciliation_diff WHERE diff_id=?", (diff_id,)).fetchone()
    if not d:
        raise LookupError("대사 차이를 찾을 수 없습니다.")
    if d["run_id"] != db.latest_run(con, "reconciliation", d["period_ym"]):
        raise ValueError("대사를 다시 돌린 뒤의 목록에서 처리해 주세요.")
    closed = con.execute("SELECT status FROM close_period WHERE period_ym=?", (d["period_ym"],)).fetchone()
    if closed and closed["status"] == "closed":
        raise ValueError("마감한 달의 차이는 처리할 수 없습니다.")
    ensure(con)
    at = db.now()
    cur = con.execute(
        """INSERT INTO recon_diff_action(period_ym, insurer_id, policy_no, commission_type, installment_no, diff_id, run_id, status,
                                         diff_amount, note, acted_by, acted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d["period_ym"], d["insurer_id"], d["policy_no"], d["commission_type"], d["installment_no"], diff_id, d["run_id"], status,
         d["diff_amount"], note, who, at))
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (at, who, "diff_update", f"diff:{diff_id}", json.dumps({"status": status, "note": note}, ensure_ascii=False)))
    con.commit()
    return {"action_id": cur.lastrowid, "diff_id": diff_id, "status": status, "status_ko": STATUS_KO[status], "note": note, "acted_by": who, "acted_at": at}


def diff_history(con, diff_id) -> list:
    """대사 차이 하나의 처리 이력. 대사를 다시 돌리기 전의 같은 차이에 남긴 처리도 든다. 오래된 것부터.
    [{action_id, diff_id, run_id, status, status_ko, diff_amount, note, acted_by, acted_at}]"""
    d = con.execute("SELECT * FROM reconciliation_diff WHERE diff_id=?", (diff_id,)).fetchone()
    if not d:
        return []
    return [{**a, "status_ko": STATUS_KO[a["status"]]} for a in _actions(con, _key(d))]


# ---------------------------------------------------------------- 사람별 월 수수료
def _month_lines(con, agent_ids, ym) -> list:
    """범위 안 사람의 그 달 수수료 줄. 정산서가 들어온 보험사는 정산서 줄, 아니면 자체 계산 줄(예상)이다."""
    ids = sorted({a for a in (agent_ids or ()) if a is not None})
    if not ids:
        return []
    loaded = statement_insurers(con, ym)
    names = _insurers(con)
    out = [{**r, "source": "statement", "estimated": False} for r in _statement_lines(con, ids, ym)]
    out += [{**r, "source": "calc", "estimated": True} for r in _calc_lines(con, ids, ym) if r["insurer_id"] not in loaded]
    for r in out:
        r["insurer"] = names.get(r["insurer_id"], {}).get("insurer")
        r["commission_type_ko"] = CTYPE_KO.get(r["commission_type"], r["commission_type"])
        r["source_ko"] = SOURCE_KO[r["source"]]
        r["policy_tail"] = _tail(r.pop("policy_no"))
    out.sort(key=lambda r: (r["insurer"] or "", r["policy_tail"] or "", r["commission_type"], r["installment_no"] or 0))
    return out


def agent_month(con, agent_ids, ym) -> dict:
    """그 달 수수료. 본인 화면은 본인 한 사람의 목록을 넘긴다. 여럿을 넘기면 합이다.

    돌려주는 것: {ym, note, estimated, total, confirmed, expected, by_insurer: [...], by_type: [...], lines: [...]}
    total, confirmed(정산서), expected(자체 계산, 예상)는 {paid, clawback, net, lines}.
    by_insurer 는 [{insurer_id, insurer, source, source_ko, estimated, paid, clawback, net, lines}].
    by_type 은 [{commission_type, commission_type_ko, amount, lines, estimated}]. estimated 는 예상 줄이 하나라도 섞였는지다.
    lines 는 [{insurer_id, insurer, agent_id, policy_tail, commission_type, commission_type_ko, installment_no, amount, source, source_ko, estimated}].
    """
    lines = _month_lines(con, agent_ids, ym)
    by_ins = {}
    for r in lines:
        by_ins.setdefault(r["insurer_id"], []).append(r)
    by_type = {}
    for r in lines:
        t = by_type.setdefault(r["commission_type"], {"commission_type": r["commission_type"], "commission_type_ko": r["commission_type_ko"],
                                                      "amount": 0, "lines": 0, "estimated": False})
        t["amount"] += r["amount"]
        t["lines"] += 1
        t["estimated"] |= r["estimated"]
    order = list(CTYPE_KO)
    return {"ym": ym, "note": BASIS_NOTE, "estimated": any(r["estimated"] for r in lines), "total": _sums(lines),
            "confirmed": _sums([r for r in lines if not r["estimated"]]), "expected": _sums([r for r in lines if r["estimated"]]),
            "by_insurer": [{"insurer_id": i, "insurer": rs[0]["insurer"], "source": rs[0]["source"], "source_ko": rs[0]["source_ko"],
                            "estimated": rs[0]["estimated"], **_sums(rs)} for i, rs in sorted(by_ins.items(), key=lambda x: (x[1][0]["insurer"] or "", x[0]))],
            "by_type": sorted(by_type.values(), key=lambda t: order.index(t["commission_type"]) if t["commission_type"] in order else len(order)),
            "lines": lines}


def by_agent(con, agent_ids, ym) -> list:
    """메인 관리자의 수수료 내역 전체 보기. 사람별 그 달 합계. agent_month 와 같은 줄을 사람마다 나눠 더한다.
    [{agent_id, code, name, paid, clawback, net, lines, estimated}] 사번 순. 줄이 없는 사람은 넣지 않는다."""
    lines = _month_lines(con, agent_ids, ym)
    if not lines:
        return []
    mine = {}
    for r in lines:
        mine.setdefault(r["agent_id"], []).append(r)
    marks = ",".join("?" * len(mine))
    people = {r["agent_id"]: r for r in con.execute(f"SELECT agent_id, internal_code code, name FROM agent WHERE agent_id IN ({marks})", list(mine))}
    out = [{"agent_id": a, "code": people[a]["code"] if a in people else None, "name": people[a]["name"] if a in people else None,
            **_sums(rs), "estimated": any(r["estimated"] for r in rs)} for a, rs in mine.items()]
    return sorted(out, key=lambda x: (x["code"] or "", x["agent_id"]))


def clawbacks(con, agent_ids, ym) -> dict:
    """그 달 환수 대상과 금액. agent_month 의 환수 줄만이다. {ym, note, estimated, total, lines}"""
    lines = [r for r in _month_lines(con, agent_ids, ym) if r["commission_type"] == "clawback"]
    return {"ym": ym, "note": BASIS_NOTE, "estimated": any(r["estimated"] for r in lines), "total": sum(r["amount"] for r in lines), "lines": lines}


# ---------------------------------------------------------------- 월 마감
def is_closed(con, ym) -> bool:
    r = con.execute("SELECT status FROM close_period WHERE period_ym=?", (ym,)).fetchone()
    return bool(r and r["status"] == "closed")


def close_check(con, ym) -> dict:
    """마감 조건. 막는 것(block)과 알리는 것(warn), 참고 건수. 가정이다. 규칙은 이 파일 머리말의 월 마감 절에 있다.

    마감은 메인 관리자의 일이라 범위는 전체 설계사와 매핑되지 않은 줄이다.
    돌려주는 것: {block: [문장], warn: [문장], counts: {open, checked, asked, resolved}, legacy_open, diff_total, statements, ran_at}
    """
    block, warn = [], []
    n = con.execute("""SELECT COUNT(*) FROM quality_issue i JOIN import_batch b ON b.batch_id=i.batch_id
                       WHERE b.period_ym=? AND b.status<>'superseded' AND i.severity='block' AND i.resolved=0""", (ym,)).fetchone()[0]
    if n:
        block.append(f"차단 등급 품질 문제 {n}건이 풀리지 않았습니다. 데이터 묶음의 품질검증에서 처리합니다.")
    n = con.execute("SELECT COUNT(*) FROM incentive_source_doc WHERE period_ym=? AND extraction_status='extracted'", (ym,)).fetchone()[0]
    if n:
        block.append(f"검토를 기다리는 시상 공문 {n}건이 있습니다.")
    loaded, rates = statement_insurers(con, ym), rate_insurers(con, ym)
    everyone = [r[0] for r in con.execute("SELECT agent_id FROM agent")]
    diffs = diff_list(con, everyone, ym, unmapped=True)
    if loaded and diffs["run_id"] is None:
        block.append("정산서가 들어왔지만 대사를 돌리지 않았습니다. 대사 화면에서 다시 돌리세요.")
    elif diffs["run_id"] is not None:
        stale = con.execute(f"""SELECT COUNT(*) FROM import_batch WHERE data_type='statement' AND period_ym=?
                                AND status IN ({','.join('?' * len(LIVE_BATCH))}) AND COALESCE(loaded_at, received_at)>?""",
                            (ym, *LIVE_BATCH, diffs["ran_at"] or "")).fetchone()[0]
        if stale:
            block.append(f"대사를 돌린 뒤에 들어온 정산서가 {stale}개 있습니다. 대사를 다시 돌리세요.")
    c = diffs["counts"]
    if c["open"]:
        block.append(f"처리하지 않은 대사 차이 {c['open']}건이 있습니다. 조정 화면에서 확인함, 보험사 문의, 해결 가운데 하나로 처리합니다.")
    if c["asked"]:
        warn.append(f"보험사에 문의 중인 차이 {c['asked']}건이 있습니다. 마감해도 처리 기록은 남지만 마감한 달은 더 처리할 수 없습니다.")
    changed = sum(1 for r in diffs["rows"] if r["changed"])
    if changed:
        warn.append(f"처리한 뒤 차이 금액이 바뀐 차이 {changed}건이 있습니다.")
    missing = sorted(rates - set(loaded))
    if missing:
        warn.append(f"정산서가 아직 없는 보험사 {len(missing)}곳은 자체 계산이라 예상 금액으로 남습니다.")
    if not loaded and not rates:
        warn.append("이 달은 정산서도 자체 계산도 없습니다.")
    legacy_open = sum(1 for r in diffs["rows"] if r["legacy_status"] == "open")
    return {"block": block, "warn": warn, "counts": c, "legacy_open": legacy_open, "diff_total": sum(c.values()),
            "statements": len(loaded), "ran_at": diffs["ran_at"]}


def close_month(con, ym, by) -> dict:
    """월 잠금. 메인 관리자만 한다. 이미 마감했거나 막는 조건이 남았으면 ValueError 다. 경고는 막지 않는다.

    close_period 에 closed 를 쓰고 audit_log 에 close 를 남긴다. 금액 묶음(snapshot)은 만들지 않는다.
    돌려주는 것: {ym, status, closed_at, closed_by, warn}
    """
    who = _require_main(con, by)
    if is_closed(con, ym):
        raise ValueError("이미 마감한 달입니다.")
    check = close_check(con, ym)
    if check["block"]:
        raise ValueError("마감을 막는 일이 남아 있습니다. " + " ".join(check["block"]))
    at = db.now()
    con.execute("INSERT OR REPLACE INTO close_period(period_ym, status, closed_at, closed_by) VALUES(?,?,?,?)", (ym, "closed", at, who))
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (at, who, "close", ym, json.dumps({"via": "web"}, ensure_ascii=False)))
    con.commit()
    return {"ym": ym, "status": "closed", "closed_at": at, "closed_by": who, "warn": check["warn"]}


def reopen_month(con, ym, by, reason) -> dict:
    """월 잠금 해제. 메인 관리자만 하고 사유가 있어야 한다. 사유를 변경 기록에 남긴다. 마감한 시각과 사람은 지우지 않는다.
    마감하지 않은 달, 빈 사유, REASON_MAX 자를 넘는 사유는 ValueError 다. 돌려주는 것: {ym, status, reopened_at, reopened_by, reason}"""
    who = _require_main(con, by)
    reason = (reason or "").strip()
    if not is_closed(con, ym):
        raise ValueError("마감하지 않은 달입니다.")
    if not reason:
        raise ValueError("해제 사유를 적어 주세요.")
    if len(reason) > REASON_MAX:
        raise ValueError(f"해제 사유는 {REASON_MAX}자까지 적습니다.")
    at = db.now()
    con.execute("UPDATE close_period SET status='open' WHERE period_ym=?", (ym,))
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (at, who, "close_reopen", ym, json.dumps({"reason": reason}, ensure_ascii=False)))
    con.commit()
    return {"ym": ym, "status": "open", "reopened_at": at, "reopened_by": who, "reason": reason}
