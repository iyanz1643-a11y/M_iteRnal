"""이터널 실제 계약현황(56열) 적재기.

순서: 머리글 검사 → 같은 파일 막기 → 원본 행 보관 → 표준 행 → 계약 원장 반영 → 건수와 합계 대조.
- 머리글이 매핑과 다르면 DB 에 아무것도 쓰지 않고 HeaderMismatch 로 사유를 돌려준다.
- 같은 파일(import_batch.file_hash)은 DuplicateFile 로 막는다.
- 키는 (보험사, 증권번호)다. 이미 있는 계약은 값을 새로 고치고, 계약상태나 최종상태변경일이 바뀌면 contract_status_history 에 쌓는다.
- 인정보험료는 파일 값을 그대로 쓴다. 1차년에서 4차년 성적은 보관만 한다 (D-03).
- 경고와 오류 메시지에는 사람 이름과 금액을 넣지 않는다. 사번, 보험사, 상품종류 같은 분류 값만 넣는다.
"""
import hashlib
import json
import pathlib

import yaml

from app import db
from app.loader import mapping, org_path
from app.loader.excel_loader import DuplicateFile, finish_batch, issue, register_batch, summary  # noqa: F401  (DuplicateFile 을 여기서도 꺼내 쓴다)
from app.loader.sheet import HeaderMismatch, check_headers, iter_raw, open_sheet, to_standard
from app.loader.transforms import normalize_header, normalize_policy_no
from app.periods import date_ym

CATEGORY_MAP = mapping.MAPPING_DIR / "product_category_map.yaml"
SEVEN = ("whole_life", "health", "savings", "annuity", "property", "auto", "general")
CONCLUDED = ("inforce", "lapsed", "cancelled", "matured", "withdrawn")
DATA_TYPE = "org_contract"


# ------------------------------------------------------------------ 분류표
def load_category_map(path=CATEGORY_MAP) -> dict:
    """상품종류 → {category, sector, confidence}. 표의 구분 코드가 일곱 구분 밖이면 오류."""
    doc = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    out = {}
    for line in doc["map"]:
        if line["category"] not in SEVEN:
            raise ValueError(f"분류표의 구분 코드 '{line['category']}' 가 일곱 구분에 없습니다")
        out[normalize_header(line["product_type"])] = {
            "category": line["category"], "sector": line.get("sector"), "confidence": line.get("confidence", "high")}
    out["_meta"] = {"status": doc.get("status"), "version": doc.get("version")}
    return out


# ------------------------------------------------------------------ 머리글만 미리 보기
def preview(path, period_ym):
    """DB 에 쓰지 않고 머리글만 검사한다. 돌려주는 것: {ok, reasons, dropped_columns, header_count}."""
    m = mapping.load("eternal", "contract", period_ym)
    wb, ws, hrow, headers = open_sheet(path, m["file"].get("sheet"), m["file"].get("header_row", 1))
    wb.close()
    reasons, dropped = check_headers(headers, expected=m["headers"], drop_keywords=m.get("drop_keywords"))
    return {"ok": not reasons, "reasons": reasons, "dropped_columns": dropped, "header_count": len(headers)}


# ------------------------------------------------------------------ 적재
def _insurer_code(name):
    return "AUTO_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8].upper()


class _Agg:
    """같은 값으로 반복되는 경고를 한 줄로 모은다. {(code, key): [message, first_row, count]}"""

    def __init__(self):
        self.items = {}

    def add(self, code, key, message, row_no):
        it = self.items.setdefault((code, key), [message, row_no, 0])
        it[2] += 1

    def flush(self, con, batch_id):
        for (code, _), (msg, row_no, n) in self.items.items():
            issue(con, batch_id, "warn", code, f"{msg} ({n}행, 첫 행 {row_no})", row_no)


def load_contract(con, path, period_ym, upload_org_id=None, by="system"):
    """실제 계약현황 한 파일을 적재한다. 돌려주는 것은 건수와 합계 대조가 든 요약이다."""
    m = mapping.load("eternal", "contract", period_ym)
    drop_kw = m.get("drop_keywords", [])
    wb, ws, hrow, headers = open_sheet(path, m["file"].get("sheet"), m["file"].get("header_row", 1))
    try:
        reasons, dropped = check_headers(headers, expected=m["headers"], drop_keywords=drop_kw)
        if reasons:
            raise HeaderMismatch(reasons, pathlib.Path(path).name)
        batch_id = register_batch(con, path, DATA_TYPE, period_ym, upload_org_id=upload_org_id, by=by, mapping_version=m["mapping_version"])
        try:
            out = _load_rows(con, m, ws, hrow, headers, batch_id, dropped)
        except Exception:
            con.rollback()
            raise
    finally:
        wb.close()
    return out


def _load_rows(con, m, ws, hrow, headers, batch_id, dropped):
    drop_kw = m.get("drop_keywords", [])
    for h in dropped:
        issue(con, batch_id, "warn", "dropped_column", f"민감 정보 열 '{h}' 을 읽지 않고 버렸습니다")
    cmap = load_category_map()
    status_map, sector_map, mode_map = m["status_map"], m["sector_map"], m["payment_mode_map"]
    required = set(m.get("required", []))
    agents = {r["internal_code"]: r for r in con.execute("SELECT agent_id, internal_code, name FROM agent")}
    insurers = {normalize_header(r["name"]): dict(r) for r in con.execute("SELECT insurer_id, code, name, sector, origin FROM insurer")}
    agg = _Agg()

    raw_buf, file_rows, file_sum = [], 0, 0
    stats = dict(inserted=0, updated=0, refreshed=0, stale=0, skipped=0, history=0)
    written = {}  # (insurer_id, policy_no) → 인정보험료. 파일 안에서 같은 키가 다시 오면 뒤 행이 남는다
    seen_rows = {}
    for r_idx, raw in iter_raw(ws, hrow, headers, drop_kw):
        file_rows += 1
        raw_buf.append((batch_id, ws.title, r_idx, json.dumps(raw, ensure_ascii=False, default=str)))
        std, blocks, warns = to_standard(raw, m["columns"], m.get("transforms", {}), required)
        if isinstance(std.get("recognized_premium"), (int, float)):
            file_sum += std["recognized_premium"]
        for w in warns:
            issue(con, batch_id, "warn", "bad_value", w, r_idx)
        status = status_map.get(std.get("status_raw")) if std.get("status_raw") else None
        if std.get("status_raw") and status is None:
            blocks.append(f"계약상태 '{std['status_raw']}' 을 매핑에서 찾지 못했습니다. 매핑의 status_map 에 더해 주세요")
        if blocks:
            for b in blocks:
                issue(con, batch_id, "block", "bad_row", f"{b}. 이 행은 넣지 않았습니다", r_idx)
            stats["skipped"] += 1
            continue

        # ---------- 품질 검사 ----------
        path = org_path.path_of(std)
        if not (std.get("org_hq") and std.get("org_branch")):
            issue(con, batch_id, "warn", "org_blank", "본부나 지점이 비어 있습니다", r_idx)
        if org_path.has_gap(path):
            issue(con, batch_id, "warn", "org_gap", "센터가 비어 있는데 팀이 있습니다", r_idx)
        sector = sector_map.get(std.get("sector_raw"))
        if sector is None:
            agg.add("unknown_sector", std.get("sector_raw"), f"계약종류 '{std.get('sector_raw') or ''}' 을 생보나 손보로 읽지 못했습니다", r_idx)
        pt = std.get("product_type_raw")
        cat_line = cmap.get(normalize_header(pt)) if pt else None
        if cat_line is None:
            agg.add("unknown_category", pt, f"상품종류 '{pt or ''}' 이 분류표에 없어 구분을 비워 두었습니다", r_idx)
        elif cat_line["sector"] and std.get("sector_raw") and cat_line["sector"] != std["sector_raw"]:
            agg.add("category_sector_mismatch", (pt, std["sector_raw"]),
                    f"상품종류 '{pt}' 은 분류표에서 {cat_line['sector']} 인데 파일의 계약종류는 {std['sector_raw']} 입니다", r_idx)
        mode = mode_map.get(std.get("payment_mode_raw"))
        if std.get("payment_mode_raw") and mode is None:
            agg.add("unknown_payment_mode", std["payment_mode_raw"], f"납입주기 '{std['payment_mode_raw']}' 을 매핑에서 찾지 못했습니다", r_idx)
        if std["recognized_premium"] < 0:
            issue(con, batch_id, "warn", "negative_amount", "인정보험료가 음수입니다. 철회나 환수를 음수로 보내는지 확인이 필요합니다", r_idx)

        ag = agents.get(std.get("agent_code")) if std.get("agent_code") else None
        if std.get("agent_code") is None:
            issue(con, batch_id, "warn", "blank_agent", "모집사원번호가 비어 있습니다", r_idx)
        elif ag is None:
            agg.add("unmapped_agent", std["agent_code"], f"모집사원번호 {std['agent_code']} 이 설계사 마스터에 없습니다. 계약은 넣고 설계사는 비워 두었습니다", r_idx)
        elif std.get("agent_name") and normalize_header(std["agent_name"]) != normalize_header(ag["name"]):
            agg.add("name_mismatch", std["agent_code"], f"모집사원번호 {std['agent_code']} 의 이름이 마스터와 다릅니다", r_idx)
        col = agents.get(std.get("collector_code")) if std.get("collector_code") else None
        if std.get("collector_code") and col is None:
            agg.add("unmapped_collector", std["collector_code"], f"수금사원번호 {std['collector_code']} 이 설계사 마스터에 없습니다", r_idx)

        ins_key = normalize_header(std["insurer_name"])
        ins = insurers.get(ins_key)
        if ins is None:
            iid = con.execute("INSERT INTO insurer(code, name, sector, origin) VALUES(?,?,?,'loader')",
                              (_insurer_code(ins_key), ins_key, sector or "life")).lastrowid
            ins = insurers[ins_key] = {"insurer_id": iid, "code": _insurer_code(ins_key), "name": ins_key, "sector": sector or "life", "origin": "loader"}
        if ins["origin"] == "loader":
            agg.add("unmapped_insurer", ins_key, f"보험사 '{ins_key}' 이 원수사 마스터에 없어 임시로 등록했습니다. 마스터 확인이 필요합니다", r_idx)

        # ---------- 원장 반영 ----------
        policy_no = normalize_policy_no(std["policy_no"])
        key = (ins["insurer_id"], policy_no)
        if key in seen_rows:
            issue(con, batch_id, "warn", "duplicate_key", f"증권번호가 {seen_rows[key]}행과 겹칩니다. 뒤 행으로 반영했습니다", r_idx)
        seen_rows[key] = r_idx
        cdate = std["contract_date"]
        scd = std.get("status_changed_date")
        sdate = scd or cdate
        values = dict(
            product_code_raw=std.get("product_name"), agent_id=ag["agent_id"] if ag else None,
            policyholder_name=std.get("policyholder_name"), insured_name=std.get("insured_name"),
            contract_date=cdate, perf_date=cdate, perf_ym=date_ym(cdate), payment_mode=mode, payment_term=std.get("payment_term"),
            premium=std.get("first_premium"), premium_monthly=std.get("first_premium"), converted_premium=std["recognized_premium"],
            status=status, status_date=sdate, withdrawal_date=sdate if status == "withdrawn" else None,
            org_head_office=path[0], org_hq=path[1], org_branch=path[2], org_center=path[3], org_team=path[4], org_path=org_path.path_text(path),
            sector=sector, product_type_raw=pt, product_category=cat_line["category"] if cat_line else None,
            insurer_product_code=std.get("insurer_product_code"), ca_code=std.get("ca_code"), payment_term_name=std.get("payment_term_name"),
            coverage_start=std.get("coverage_start"), coverage_end=std.get("coverage_end"),
            status_raw=std["status_raw"], status_changed_date=scd, paid_installments=std.get("paid_installments"),
            last_paid_ym=std.get("last_paid_ym"), last_collected_date=std.get("last_collected_date"), receipt_date=std.get("receipt_date"),
            auto_debit_day=std.get("auto_debit_day"), collected_premium=std.get("collected_premium"),
            collector_code=std.get("collector_code"), collector_agent_id=col["agent_id"] if col else None,
            perf_y1=std.get("perf_y1"), perf_y2=std.get("perf_y2"), perf_y3=std.get("perf_y3"), perf_y4=std.get("perf_y4"),
            manual_perf=std.get("manual_perf"), self_family=std.get("self_family"), external_transfer=std.get("external_transfer"),
            excel_registered=std.get("excel_registered"), confirm_personal=std.get("confirm_personal"), confirm_compare=std.get("confirm_compare"),
            confirm_fcpa=std.get("confirm_fcpa"), confirm_whole_life=std.get("confirm_whole_life"),
            mobile_application=std.get("mobile_application"), last_batch_id=batch_id, source_row_no=r_idx)
        reason = f"계약현황 계약상태 '{std['status_raw']}'"
        old = con.execute("SELECT contract_id, status, status_date, status_changed_date FROM contract WHERE insurer_id=? AND policy_no=?", key).fetchone()
        if old is None:
            cols = ["insurer_id", "policy_no", "source_batch_id", "created_at"] + list(values)
            cid = con.execute(f"INSERT INTO contract({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              [ins["insurer_id"], policy_no, batch_id, db.now()] + list(values.values())).lastrowid
            hist = []
            if status in CONCLUDED:
                hist.append(("inforce", cdate, "계약현황 첫 적재. 계약일자에 성립"))
            if status != "inforce":
                hist.append((status, sdate, reason))
            for st, d, why in hist:
                _history(con, cid, st, d, why, batch_id)
            stats["history"] += len(hist)
            stats["inserted"] += 1
        else:
            if scd and old["status_changed_date"] and scd < old["status_changed_date"]:
                issue(con, batch_id, "warn", "stale_row", "원장의 최종상태변경일보다 이전 값입니다. 이 행은 반영하지 않았습니다", r_idx)
                stats["stale"] += 1
                continue
            changed = old["status"] != status or (old["status_changed_date"] or None) != (scd or None)
            con.execute(f"UPDATE contract SET {', '.join(f'{k}=?' for k in values)} WHERE contract_id=?",
                        list(values.values()) + [old["contract_id"]])
            if changed:
                if old["status"] in ("applied", "declined") and status in CONCLUDED and status != "inforce":
                    _history(con, old["contract_id"], "inforce", cdate, "계약현황 재적재. 계약일자에 성립", batch_id)
                    stats["history"] += 1
                _history(con, old["contract_id"], status, sdate, reason, batch_id)
                stats["history"] += 1
                stats["updated"] += 1
            else:
                stats["refreshed"] += 1
        written[key] = std["recognized_premium"]

    con.executemany("INSERT INTO raw_row(batch_id, sheet_name, row_no, raw_json) VALUES(?,?,?,?)", raw_buf)
    agg.flush(con, batch_id)

    # ---------- 건수와 합계 대조 ----------
    db_n, db_sum = con.execute("SELECT COUNT(*), COALESCE(SUM(converted_premium),0) FROM contract WHERE last_batch_id=?", (batch_id,)).fetchone()
    exp_sum = sum(written.values())
    failed = False
    if db_n != len(written) or abs(db_sum - exp_sum) > 0.5:
        issue(con, batch_id, "block", "total_mismatch",
              f"원장 대조가 맞지 않습니다: 반영할 계약 {len(written)}건, 원장 {db_n}건. 합계 차이 {db_sum - exp_sum:,.0f}")
        failed = True
    finish_batch(con, batch_id, file_rows, failed=failed)
    return summary(con, batch_id, file_rows=file_rows, file_recognized_sum=file_sum, loaded=len(written), loaded_recognized_sum=db_sum,
                   dropped_columns=dropped, category_map=cmap["_meta"], **stats)


def _history(con, contract_id, status, status_date, reason, batch_id):
    con.execute("INSERT INTO contract_status_history(contract_id, status, status_date, reason, batch_id, created_at) VALUES(?,?,?,?,?,?)",
                (contract_id, status, status_date, reason, batch_id, db.now()))


def batch_issues(con, batch_id) -> list:
    """화면에 보일 품질 문제 목록. 메시지에는 사람 이름과 금액이 없다."""
    return [dict(r) for r in con.execute(
        "SELECT severity, check_code, row_no, message FROM quality_issue WHERE batch_id=? ORDER BY severity, row_no", (batch_id,))]
