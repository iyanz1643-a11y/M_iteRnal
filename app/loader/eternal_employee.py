"""이터널 사원현황 파서. 사람 행을 읽어 사번, 이름, 조직 경로, 직위, 입사일, 퇴사일, 자격만 돌려준다.

DB 에는 쓰지 않는다. 조직과 설계사 적재는 auth-org 가 이 결과로 한다.
주민번호, 생년월일, 음양구분, 4대보험, 보험업계고유번호, 계좌, 소득, 이행보증, 예치금 열은
머리글만 보고 버리며 그 칸의 값은 꺼내지 않는다.
"""
from datetime import date

from app.loader import mapping
from app.loader.sheet import HeaderMismatch, check_headers, iter_raw, open_sheet, to_standard
from app.loader.transforms import normalize_header

ORG_KO = {"org_general": "총괄", "org_hq": "본부", "org_branch": "지점", "org_direct_branch": "직할지점", "org_team": "팀"}


def parse_employee_file(path, period_ym=None, sheet=None) -> dict:
    """돌려주는 것: {rows, warnings, dropped_columns, unknown_columns, sheet, header_row}.
    rows 의 한 줄: {employee_no, name, org, org_path, position, hire_date, termination_date, life, nonlife, _row}.
    warnings 의 메시지에는 사람 이름이 없다. 필수 열이 없으면 HeaderMismatch."""
    m = mapping.load("eternal", "employee", period_ym or date.today().strftime("%Y%m"))
    drop_kw = m.get("drop_keywords", [])
    must = [src for src, dst in m["columns"].items() if dst in m["required"]]
    wb, ws, hrow, headers = open_sheet(path, sheet or m["file"].get("sheet"), m["file"].get("header_row"), must_have=must)
    try:
        reasons, dropped = check_headers(headers, required=must, drop_keywords=drop_kw)
        if reasons:
            raise HeaderMismatch(reasons)
        known = {normalize_header(h) for h in list(m["columns"]) + m.get("ignore", [])}
        unknown = [h for h in headers if h and h not in known and h not in dropped]
        rows, warnings, seen = [], [], {}
        for r_idx, raw in iter_raw(ws, hrow, headers, drop_kw):
            raw = {h: v for h, v in raw.items() if h in known}
            std, blocks, warns = to_standard(raw, m["columns"], m.get("transforms", {}), set(m["required"]))
            for w in warns:
                warnings.append({"row_no": r_idx, "severity": "warn", "message": w})
            if blocks:
                warnings.append({"row_no": r_idx, "severity": "block", "message": " / ".join(blocks) + ". 이 행은 돌려주지 않습니다"})
                continue
            no = std["employee_no"]
            if no in seen:
                warnings.append({"row_no": r_idx, "severity": "warn", "message": f"사원번호 {no} 가 {seen[no]}행과 겹칩니다"})
            seen[no] = r_idx
            org = {ORG_KO[k]: std.get(k) for k in m["org_levels"]}
            rows.append({
                "employee_no": no, "name": std["name"], "org": org,
                "org_path": tuple(v for v in org.values() if v),
                "position": std.get("position"), "hire_date": std.get("hire_date"), "termination_date": std.get("termination_date"),
                "life": {"status": std.get("life_status"), "insurer": std.get("life_insurer"),
                         "registered": std.get("life_registered"), "deregistered": std.get("life_deregistered")},
                "nonlife": {"status": std.get("nonlife_status"), "insurer": std.get("nonlife_insurer"),
                            "registered": std.get("nonlife_registered"), "deregistered": std.get("nonlife_deregistered")},
                "_row": r_idx,
            })
        return {"rows": rows, "warnings": warnings, "dropped_columns": dropped, "unknown_columns": unknown,
                "sheet": ws.title, "header_row": hrow}
    finally:
        wb.close()
