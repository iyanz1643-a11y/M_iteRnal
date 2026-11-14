"""엑셀 적재기: 원본 보관 정보 등록 → 매핑 → 표준 적재 → 품질검증."""
import hashlib
import json
import pathlib

import openpyxl

from app import db
from app.loader import mapping
from app.loader.transforms import apply
from app.periods import date_ym


class DuplicateFile(Exception):
    pass


def file_hash(path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------------ 공통
def register_batch(con, path, data_type, period_ym, insurer_id=None, upload_org_id=None, via="manual", by="system", mapping_version=None):
    h = file_hash(path)
    dup = con.execute("SELECT batch_id, file_name FROM import_batch WHERE file_hash=?", (h,)).fetchone()
    if dup:
        raise DuplicateFile(f"이미 적재된 파일입니다 (batch {dup['batch_id']}: {dup['file_name']})")
    prev = con.execute(
        "SELECT COALESCE(MAX(version),0) v FROM import_batch WHERE data_type=? AND period_ym=? AND insurer_id IS ? AND upload_org_id IS ?",
        (data_type, period_ym, insurer_id, upload_org_id)).fetchone()["v"]
    if prev and data_type == "statement":  # 정산서 재수신은 이전 배치를 대체
        con.execute("UPDATE import_batch SET status='superseded' WHERE data_type=? AND period_ym=? AND insurer_id IS ? AND status='active'",
                    (data_type, period_ym, insurer_id))
    cur = con.execute(
        """INSERT INTO import_batch(insurer_id, data_type, upload_org_id, period_ym, version, file_name, file_path, file_hash,
           received_via, received_at, loaded_by, mapping_version, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'received')""",
        (insurer_id, data_type, upload_org_id, period_ym, prev + 1, pathlib.Path(path).name, str(path), h, via, db.now(), by, mapping_version))
    return cur.lastrowid


def issue(con, batch_id, severity, code, message, row_no=None):
    con.execute("INSERT INTO quality_issue(batch_id, row_no, severity, check_code, message) VALUES(?,?,?,?,?)",
                (batch_id, row_no, severity, code, message))


SIGN_ALIGN_MESSAGE = "철회 계약의 음수 금액을 양수로 맞췄습니다 (D-31). 원본 행은 그대로 두었습니다"


def align_withdrawn_sign(std, status, rule) -> bool:
    """D-31. 철회 계약의 금액 칸에 음수가 오면 절댓값으로 바꾼다. 바꾼 칸이 하나라도 있으면 True.

    rule 은 매핑의 sign_align 이다: {when_status: [withdrawn], fields: [...]}. 없으면 아무것도 바꾸지 않는다.
    계산 엔진은 철회 계약의 금액이 양수라고 보고 성립한 달에 더하고 철회한 달에 뺀다. 철회가 아닌 계약의 음수는 건드리지 않는다.
    std 만 고친다. 원본 행(raw_row)은 원래 값 그대로 남는다.
    """
    if not rule or status not in rule.get("when_status", ()):
        return False
    changed = False
    for f in rule.get("fields", ()):
        v = std.get(f)
        if isinstance(v, (int, float)) and v < 0:
            std[f] = -v
            changed = True
    return changed


def finish_batch(con, batch_id, row_count, failed=False):
    con.execute("UPDATE import_batch SET status=?, loaded_at=?, row_count=? WHERE batch_id=?",
                ("failed" if failed else "active", db.now(), row_count, batch_id))
    con.commit()


def read_rows(con, path, m, batch_id):
    """매핑에 따라 시트를 읽어 표준 행 목록을 돌려준다. 원본 행은 raw_row 에 그대로 보존한다."""
    wb = openpyxl.load_workbook(path, data_only=True)
    sheet = m["file"].get("sheet")
    ws = wb[sheet] if sheet in wb.sheetnames else wb.worksheets[0]
    hrow = m["file"].get("header_row", 1)
    headers = [str(c.value).strip() if c.value is not None else "" for c in ws[hrow]]
    missing = [src for src in m["columns"] if src not in headers]
    if missing:
        issue(con, batch_id, "block", "missing_column", f"필수 열 없음: {', '.join(missing)}")
        return None, None
    skip_rules = m["file"].get("skip_if", [])
    tc = m.get("totals_check")
    rows, file_total, raw_buf = [], None, []
    for r_idx, row in enumerate(ws.iter_rows(min_row=hrow + 1, values_only=True), start=hrow + 1):
        raw = {h: v for h, v in zip(headers, row) if h}
        if all(v is None or str(v).strip() == "" for v in raw.values()):
            continue
        raw_buf.append((batch_id, ws.title, r_idx, json.dumps(raw, ensure_ascii=False, default=str)))
        if tc and str(raw.get(tc["label_column"], "")).strip() == tc["label"]:
            from app.loader.transforms import parse_money
            file_total = parse_money(raw.get(tc["value_column"]))
            continue
        if any(str(raw.get(rule["column"]) if raw.get(rule["column"]) is not None else "").strip() in rule["in"] for rule in skip_rules):
            continue
        std, bad = {"_row": r_idx}, False
        for src, dst in m["columns"].items():
            try:
                std[dst] = apply(raw.get(src), m.get("transforms", {}).get(dst, []))
            except (ValueError, TypeError) as e:
                issue(con, batch_id, "block", "bad_value", f"{src}: {e}", r_idx)
                bad = True
        for p in m.get("post", []):
            if "negate_if" in p:
                n = p["negate_if"]
                if std.get(n["when_field"]) == n["equals"] and std.get(n["field"]) is not None:
                    std[n["field"]] = -abs(std[n["field"]])
        blanks = [f for f in m.get("required", []) if std.get(f) in (None, "")]
        if blanks and not bad:
            issue(con, batch_id, "block", "required_blank", f"필수 값 없음: {', '.join(blanks)}", r_idx)
            bad = True
        if not bad:
            rows.append(std)
    con.executemany("INSERT INTO raw_row(batch_id, sheet_name, row_no, raw_json) VALUES(?,?,?,?)", raw_buf)
    return rows, file_total


def check_total(con, batch_id, m, rows, file_total):
    tc = m.get("totals_check")
    if not tc or file_total is None:
        return
    vals = [r.get(tc["compare_to"]) or 0 for r in rows]
    total = sum(abs(v) for v in vals) if tc.get("absolute") else sum(vals)
    if abs(total - file_total) > 1:
        issue(con, batch_id, "block", "total_mismatch", f"파일 합계 {file_total:,} ≠ 적재 대상 합계 {total:,} (제외된 행이 있거나 매핑 확인 필요)")


def summary(con, batch_id, **extra):
    q = con.execute("SELECT severity, COUNT(*) n FROM quality_issue WHERE batch_id=? GROUP BY severity", (batch_id,)).fetchall()
    out = dict(batch_id=batch_id, block=0, warn=0, **extra)
    for r in q:
        out[r["severity"]] = r["n"]
    return out


def org_members(con, org_id, period_ym) -> set:
    """그 달에 org_id 조직과 그 아래 모든 하위 조직(지점, 센터, 팀)에 소속된 설계사 agent_id.
    조직 계층의 깊이는 config/org_levels.yaml 을 따르므로 한 단계만 보지 않고 재귀로 내려간다."""
    if org_id is None:
        return set()
    return {r[0] for r in con.execute(
        """WITH RECURSIVE sub(org_id) AS (
               SELECT org_id FROM org_unit WHERE org_id=?
               UNION
               SELECT o.org_id FROM org_unit o JOIN sub s ON o.parent_id=s.org_id)
           SELECT DISTINCT h.agent_id FROM agent_org_history h JOIN sub s ON s.org_id=h.org_id
           WHERE h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)""",
        (org_id, period_ym, period_ym))}


# ------------------------------------------------------------------ 본부 계약 현황
def load_org_contract(con, path, period_ym, upload_org_id, by="system"):
    """본부 계약 엑셀 적재. 사번·성명·원수사·상품·구분·보험료·인정보험료·건수를 뽑아 계약 원장에 반영한다.
    청약번호를 키로 삼아, 다시 올라온 건은 상태 변화만 이력에 추가한다."""
    m = mapping.load("internal", "org_contract", period_ym)
    batch_id = register_batch(con, path, "org_contract", period_ym, upload_org_id=upload_org_id, by=by, mapping_version=m["mapping_version"])
    rows, file_total = read_rows(con, path, m, batch_id)
    if rows is None:
        finish_batch(con, batch_id, 0, failed=True)
        return summary(con, batch_id, inserted=0, updated=0, skipped=0)
    check_total(con, batch_id, m, rows, file_total)

    agents = {r["internal_code"]: r for r in con.execute("SELECT * FROM agent")}
    insurers = {r["name"]: r for r in con.execute("SELECT * FROM insurer")}
    products = {(r["insurer_id"], r["product_name"]): r for r in con.execute("SELECT * FROM product")}
    categories = {r["name"]: r["code"] for r in con.execute("SELECT * FROM product_category")}
    hq_members = org_members(con, upload_org_id, period_ym)
    seen, inserted, updated, skipped, sign_fixed = {}, 0, 0, 0, 0
    first_fixed = None
    for r in rows:
        n = r["_row"]
        # 파일 합계 대조(check_total)는 원본 부호로 끝냈다. 원장에 넣을 값만 부호를 맞춘다
        if align_withdrawn_sign(r, r.get("status"), m.get("sign_align")):
            sign_fixed += 1
            first_fixed = first_fixed or n
        elif any(isinstance(r.get(f), (int, float)) and r[f] < 0 for f in ("premium", "recognized_premium")):
            issue(con, batch_id, "warn", "negative_amount", "철회가 아닌 계약의 보험료나 인정보험료가 음수입니다. 값은 그대로 두었습니다", n)
        if r["application_no"] in seen:
            issue(con, batch_id, "warn", "duplicate_key", f"청약번호 {r['application_no']} 가 {seen[r['application_no']]}행과 중복 (뒤 행으로 반영)", n)
        seen[r["application_no"]] = n
        ag = agents.get(r["internal_code"])
        if not ag:
            issue(con, batch_id, "block", "unmapped_agent", f"사번 {r['internal_code']} ({r.get('agent_name')}) 이 설계사 마스터에 없음 → 행 제외", n)
            skipped += 1
            continue
        if r.get("agent_name") and r["agent_name"] != ag["name"]:
            issue(con, batch_id, "warn", "name_mismatch", f"사번 {r['internal_code']} 성명 불일치: 파일 '{r['agent_name']}' / 마스터 '{ag['name']}'", n)
        if ag["agent_id"] not in hq_members:
            issue(con, batch_id, "warn", "org_mismatch", f"사번 {r['internal_code']} 은 업로드 본부 소속이 아님", n)
        ins = insurers.get(r["insurer_name"])
        if not ins:
            issue(con, batch_id, "block", "unmapped_insurer", f"원수사 '{r['insurer_name']}' 가 마스터에 없음 → 행 제외", n)
            skipped += 1
            continue
        if r["status"] == "unknown":
            issue(con, batch_id, "block", "bad_status", "상태 값을 인식할 수 없음 → 행 제외", n)
            skipped += 1
            continue
        cat = categories.get(r["category_name"])
        if not cat:
            issue(con, batch_id, "warn", "unknown_category", f"구분 '{r['category_name']}' 이 구분 마스터에 없음", n)
        prod = products.get((ins["insurer_id"], r["product_name"]))
        if not prod:
            issue(con, batch_id, "warn", "unmapped_product", f"상품 '{r['product_name']}' ({r['insurer_name']}) 이 상품 마스터에 없음 → 수수료 계산 제외", n)
        else:
            if cat and prod["product_category"] != cat:
                issue(con, batch_id, "warn", "category_mismatch", f"구분 불일치: 파일 '{r['category_name']}' / 상품 마스터 '{prod['product_category']}'", n)
            expect = round(r["premium"] * prod["conversion_rate"])
            if abs(expect - r["recognized_premium"]) > 1:
                issue(con, batch_id, "warn", "recognized_mismatch",
                      f"인정보험료 {r['recognized_premium']:,} ≠ 보험료 {r['premium']:,} × 인정률 {prod['conversion_rate']} = {expect:,}", n)
        status, cdate = r["status"], r.get("contract_date")
        if status not in ("applied", "declined") and not cdate:
            issue(con, batch_id, "block", "missing_contract_date", f"상태 '{status}' 인데 계약일이 없음 → 행 제외", n)
            skipped += 1
            continue
        sdate = r.get("status_date") or cdate or r.get("application_date")
        pm = r["premium"]
        values = dict(
            policy_no=r.get("policy_no") or None, product_id=prod["product_id"] if prod else None, product_code_raw=r["product_name"],
            agent_id=ag["agent_id"], policyholder_name=r.get("policyholder_name"), application_date=r.get("application_date"),
            contract_date=cdate, perf_date=cdate, perf_ym=date_ym(cdate) if cdate else None, payment_mode=r.get("payment_mode"),
            payment_term=r.get("payment_term"), premium=pm * 12 if r.get("payment_mode") == "annual" else pm, premium_monthly=pm,
            converted_premium=r["recognized_premium"], policy_count=r["policy_count"], status=status, status_date=sdate,
            withdrawal_date=sdate if status == "withdrawn" else None)
        old = con.execute("SELECT * FROM contract WHERE insurer_id=? AND application_no=?", (ins["insurer_id"], r["application_no"])).fetchone()
        if old is None:
            cols = ["insurer_id", "application_no", "source_batch_id", "created_at"] + list(values)
            cur = con.execute(f"INSERT INTO contract({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              [ins["insurer_id"], r["application_no"], batch_id, db.now()] + list(values.values()))
            cid = cur.lastrowid
            hist = [("applied", r.get("application_date") or sdate)]
            if status not in ("applied", "declined"):
                hist.append(("inforce", cdate))
            if status not in ("applied", "inforce"):
                hist.append((status, sdate))
            for st, d in hist:
                con.execute("INSERT INTO contract_status_history(contract_id, status, status_date, batch_id, created_at) VALUES(?,?,?,?,?)",
                            (cid, st, d, batch_id, db.now()))
            inserted += 1
        elif old["status"] != status:
            sets = ", ".join(f"{k}=?" for k in values)
            con.execute(f"UPDATE contract SET {sets} WHERE contract_id=?", list(values.values()) + [old["contract_id"]])
            if old["status"] == "applied" and status not in ("inforce", "declined"):
                con.execute("INSERT INTO contract_status_history(contract_id, status, status_date, batch_id, created_at) VALUES(?,?,?,?,?)",
                            (old["contract_id"], "inforce", cdate, batch_id, db.now()))
            con.execute("INSERT INTO contract_status_history(contract_id, status, status_date, batch_id, created_at) VALUES(?,?,?,?,?)",
                        (old["contract_id"], status, sdate, batch_id, db.now()))
            updated += 1
    if sign_fixed:
        issue(con, batch_id, "warn", "sign_aligned", f"{SIGN_ALIGN_MESSAGE} ({sign_fixed}행, 첫 행 {first_fixed})", first_fixed)
    finish_batch(con, batch_id, len(rows))
    return summary(con, batch_id, rows=len(rows), inserted=inserted, updated=updated, skipped=skipped, sign_fixed=sign_fixed)


# ------------------------------------------------------------------ 보험사 정산서
def load_statement(con, path, insurer_code, period_ym, by="system"):
    ins = con.execute("SELECT * FROM insurer WHERE code=?", (insurer_code,)).fetchone()
    m = mapping.load(insurer_code, "statement", period_ym)
    batch_id = register_batch(con, path, "statement", period_ym, insurer_id=ins["insurer_id"], via="portal", by=by, mapping_version=m["mapping_version"])
    rows, file_total = read_rows(con, path, m, batch_id)
    if rows is None:
        finish_batch(con, batch_id, 0, failed=True)
        return summary(con, batch_id, rows=0)
    check_total(con, batch_id, m, rows, file_total)
    codes = {r["insurer_agent_code"]: r["agent_id"] for r in con.execute(
        "SELECT insurer_agent_code, agent_id FROM agent_insurer_code WHERE insurer_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)",
        (ins["insurer_id"], period_ym, period_ym))}
    known_products = {r["product_code"] for r in con.execute("SELECT product_code FROM product WHERE insurer_id=?", (ins["insurer_id"],))}
    seen = set()
    for r in rows:
        n = r["_row"]
        agent_id = codes.get(r.get("insurer_agent_code"))
        if r.get("insurer_agent_code") and agent_id is None:
            issue(con, batch_id, "warn", "unmapped_agent", f"설계사 코드 {r['insurer_agent_code']} 매핑 없음", n)
        if r.get("product_code") and r["product_code"] not in known_products:
            issue(con, batch_id, "warn", "unmapped_product", f"상품 코드 {r['product_code']} 가 마스터에 없음", n)
        if r["amount"] < 0 and r["commission_type"] != "clawback":
            issue(con, batch_id, "warn", "negative_amount", f"환수가 아닌데 음수 금액 {r['amount']:,}", n)
        key = (r["policy_no"], r["commission_type"], r.get("installment_no"))
        if key in seen:
            issue(con, batch_id, "warn", "duplicate_key", f"(증권번호, 유형, 회차) 중복: {key} → 합산 대사", n)
        seen.add(key)
        con.execute(
            """INSERT INTO commission_statement_line(batch_id, insurer_id, period_ym, policy_no, insurer_agent_code, agent_id, product_code,
               commission_type, installment_no, basis_amount, rate, amount, pay_date, raw_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, ins["insurer_id"], period_ym, r["policy_no"], r.get("insurer_agent_code"), agent_id, r.get("product_code"),
             r["commission_type"], r.get("installment_no"), r.get("basis_amount"), r.get("rate"), r["amount"], r.get("pay_date"), f"{m['file'].get('sheet')}!R{n}"))
    failed = con.execute("SELECT 1 FROM quality_issue WHERE batch_id=? AND severity='block' AND row_no IS NULL", (batch_id,)).fetchone() is not None
    finish_batch(con, batch_id, len(rows), failed=failed)
    return summary(con, batch_id, rows=len(rows))


# ------------------------------------------------------------------ 유지율 현황
def load_persistency(con, path, period_ym, by="system"):
    m = mapping.load("internal", "persistency", period_ym)
    batch_id = register_batch(con, path, "persistency", period_ym, by=by, mapping_version=m["mapping_version"])
    rows, _ = read_rows(con, path, m, batch_id)
    if rows is None:
        finish_batch(con, batch_id, 0, failed=True)
        return summary(con, batch_id, rows=0)
    agents = {r["internal_code"]: r["agent_id"] for r in con.execute("SELECT internal_code, agent_id FROM agent")}
    insurers = {r["name"]: r["insurer_id"] for r in con.execute("SELECT name, insurer_id FROM insurer")}
    for r in rows:
        aid, iid = agents.get(r["internal_code"]), insurers.get(r["insurer_name"])
        if aid is None or iid is None:
            issue(con, batch_id, "warn", "unmapped_agent" if aid is None else "unmapped_insurer", f"{r['internal_code']} / {r['insurer_name']} 매핑 없음 → 행 제외", r["_row"])
            continue
        for kind, num, den in (("p13", r.get("p13_num"), r.get("p13_den")), ("p25", r.get("p25_num"), r.get("p25_den"))):
            if not den:
                continue
            if num > den:
                issue(con, batch_id, "warn", "bad_ratio", f"{kind} 유지 {num} > 대상 {den}", r["_row"])
            con.execute("INSERT OR REPLACE INTO persistency(agent_id, insurer_id, period_ym, kind, numerator, denominator, rate, batch_id) VALUES(?,?,?,?,?,?,?,?)",
                        (aid, iid, period_ym, kind, num, den, round(num / den, 4), batch_id))
    finish_batch(con, batch_id, len(rows))
    return summary(con, batch_id, rows=len(rows))
