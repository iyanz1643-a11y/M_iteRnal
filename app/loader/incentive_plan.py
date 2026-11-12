"""보험사별 시상안(엑셀) → 시상안 원장.

흐름: read_xlsx() 또는 read_drive_extract() 로 원문 행을 읽는다
      → normalize() 로 병합 셀을 채우고 줄마다 유형·기간·지급률을 해석한다
      → load() 로 incentive_plan_doc / incentive_plan_line / incentive_plan_component 에 저장한다.
원문은 한 글자도 바꾸지 않고 *_raw 열에 보존한다. 해석 결과는 초안(draft)이며,
담당자가 승인하기 전에는 계산 규칙으로 넘어가지 않는다.
"""
import calendar
import csv
import hashlib
import io
import json
import pathlib
import re

from app import db

COLUMNS = ["보험사", "시상대상", "분류", "기간", "달성기준", "시상내용"]
SECTOR_BY_SHEET = [("생보", "life"), ("손보", "nonlife")]
RECIPIENT = {"GA": "ga", "총괄": "hq", "지사": "branch", "FC": "fc", "비고": "note"}
BASIS = [("월초", "initial_premium"), ("월납환산", "converted_premium"), ("월납", "monthly_premium"), ("입금보험료", "paid_premium"), ("입금P", "paid_premium"),
         ("기본보험료", "base_premium"), ("보험료", "premium")]          # 앞에서부터 먼저 맞는 낱말을 쓴다
TIMING_WORD = r"(?:익월|\d+\s*(?:회차?|차월?))"
# 추출본에서 시트가 바뀌는 자리. 6열 표준 양식이 아닌 시트는 적재하지 않고 사유만 남긴다.
EXTRACT_SHEETS = [
    ("1.생보사", r"1\.생보사 1\. 생보사 ,", True, None),
    ("2.손보사", r"2\. 손보사 2\. 손보사 ,", True, None),
    ("3.리스크관리자금 운영 시상률", r"3\. 리스크관리자금 운영 시상률 ■", False, "7열 비교표(원안과 배포안). 표준 6열 양식이 아님"),
    ("4.손보사 FC주차시상(현금시상)", r"4\.손보사 FC주차시상\(현금시상\) ■", False, "열 구성이 다름(내용·적용사항·환수). 연도 표기 없음"),
    ("7월 재물", r"7월 재물 ■", False, "2018년 7월 자료"),
    ("4.손보사_주차시상비교", r"4\.손보사\\?_주차시상비교 ,■", False, "2018년 6월 기준 자료"),
]


# ---------------------------------------------------------------- 읽기
def read_xlsx(path) -> dict:
    """표준 6열 시트만 읽는다. 병합 셀은 왼쪽 위 값만 있고 나머지는 빈칸으로 온다."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    rows, skipped = [], []
    for ws in wb.worksheets:
        header_at = None
        for r in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 15)):
            vals = [str(c.value).strip() if c.value is not None else "" for c in r]
            if COLUMNS[0] in vals and vals[vals.index(COLUMNS[0]):][:6] == COLUMNS:
                header_at = (r[0].row, vals.index(COLUMNS[0]))
                break
        if header_at is None:
            skipped.append({"sheet": ws.title, "reason": "표준 6열 머리글(보험사·시상대상·분류·기간·달성기준·시상내용)이 없음"})
            continue
        hr, c0 = header_at
        for r in ws.iter_rows(min_row=hr + 1):
            cells = [("" if c.value is None else str(c.value)) for c in r[c0:c0 + 6]]
            cells += [""] * (6 - len(cells))
            if any(x.strip() for x in cells):
                rows.append({"sheet": ws.title, "row_no": r[0].row, "cells": cells})
    return {"rows": rows, "skipped": skipped}


def _split_tail(tok):
    """추출본은 행과 행이 공백으로만 이어진다: '<6번째 칸> <다음 행 1번째 칸>' 을 가른다."""
    if tok.endswith('"'):                      # 다음 행의 보험사 칸이 따옴표로 묶인 경우
        q = tok.rfind(' "')
        if q >= 0:
            return tok[:q], tok[q + 2:-1]
        if tok.startswith('"'):
            return "", tok[1:-1]
    if tok == "" or tok.endswith(" "):
        return tok.rstrip(" "), ""
    sp = tok.rfind(" ")
    return (tok[:sp], tok[sp + 1:]) if sp >= 0 else ("", tok)


def read_drive_extract(path) -> dict:
    """구글 드라이브 본문 추출본(JSON 또는 텍스트)에서 표준 6열 시트를 복원한다.

    추출본은 줄바꿈이 공백으로 바뀌어 있어 셀 안 줄바꿈과 행 번호가 사라진다.
    원본 엑셀을 직접 읽을 수 없을 때만 쓴다.
    """
    raw = pathlib.Path(path).read_text(encoding="utf-8")
    try:
        text = json.loads(raw)["fileContent"]
    except (ValueError, KeyError, TypeError):
        text = raw
    marks = []
    for name, pat, std, reason in EXTRACT_SHEETS:
        m = re.search(pat, text)
        if m:
            marks.append((m.start(), name, std, reason))
    marks.sort()
    rows, skipped = [], []
    for i, (start, name, std, reason) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        chunk = text[start:end]
        if not std:
            skipped.append({"sheet": name, "reason": reason, "chars": len(chunk)})
            continue
        chunk = re.sub(r"\\([*~\[\]_>-])", r"\1", chunk)      # 마크다운 이스케이프 제거
        toks = next(csv.reader(io.StringIO(chunk)))
        cur, sheet_rows = [toks[0]], []
        for tok in toks[1:]:
            if len(cur) < 5:
                cur.append(tok)
                continue
            tail, head = _split_tail(tok)
            sheet_rows.append(cur + [tail])
            cur = [head]
        if len(cur) > 1 or cur[0].strip():
            sheet_rows.append((cur + [""] * 6)[:6])
        head_at = next((i for i, c in enumerate(sheet_rows) if [x.strip() for x in c[1:]] == COLUMNS[1:]), -1)
        for n, cells in enumerate(sheet_rows[head_at + 1:], start=1):       # 제목·안내·머리글은 버린다
            rows.append({"sheet": name, "row_no": n, "cells": cells})
    return {"rows": rows, "skipped": skipped}


# ---------------------------------------------------------------- 해석
def _last_day(y, m):
    return calendar.monthrange(y, m)[1]


def _period(text):
    """기간 원문 → (시작일, 종료일, 메모). 확실히 읽히는 한 구간만 날짜로 바꾼다."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        return None, None, None
    note = None
    paren = " ".join(re.findall(r"\(([^)]*)\)", t))
    if re.search(r"\d+\s*일", paren):
        note = "괄호 안에 기간 예외가 있음"
    core = re.sub(r"\([^)]*\)", " ", t).strip()
    if "/" in core or "," in core or "연속" in core:
        return None, None, "기간 창이 둘 이상"
    Y, M, D = r"(\d{4})년\s*", r"(\d{1,2})\s*월\s*", r"(\d{1,2})\s*일"
    m = re.fullmatch(Y + M + D + r"\s*[-~]\s*(?:\d{4}년\s*)?" + M + D, core)
    if m:
        y, m1, d1, m2, d2 = (int(x) for x in m.groups())
        return f"{y}-{m1:02d}-{d1:02d}", f"{y}-{m2:02d}-{d2:02d}", note
    m = re.fullmatch(Y + M + D + r"\s*[-~]\s*" + D, core)
    if m:
        y, mo, d1, d2 = (int(x) for x in m.groups())
        return f"{y}-{mo:02d}-{d1:02d}", f"{y}-{mo:02d}-{d2:02d}", note
    m = re.fullmatch(Y + r"(\d{1,2})\s*월?\s*[-~]\s*" + M.rstrip(r"\s*"), core)
    if m:
        y, m1, m2 = (int(x) for x in m.groups())
        return f"{y}-{m1:02d}-01", f"{y}-{m2:02d}-{_last_day(y, m2):02d}", note or "여러 달에 걸친 기간"
    m = re.fullmatch(Y + M.rstrip(r"\s*"), core)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-{_last_day(y, mo):02d}", note
    if len(re.findall(r"월", core)) > 2 or len(re.findall(r"일", core)) > 2:
        return None, None, "기간 창이 둘 이상"
    return None, None, "기간 해석 불가"


def _timing(ctx):
    c = (ctx or "").replace(" ", "")
    inst13, nxt = bool(re.search(r"13(회|차)", c)), "익월" in c
    if inst13 and nxt:
        return "next_month+inst13"
    if inst13:
        return "inst13"
    if nxt:
        return "next_month"
    if "2차년" in c:
        return "year2"
    if "1차년" in c:
        return "year1"
    return "unspecified"


def _components(reward):
    """시상내용에서 '기준 보험료 × 지급률(지급 시점)' 조각을 뽑는다.

    지급률로 인정하는 자리: 기준 보험료 낱말 뒤, 문장 맨 앞, '+' 뒤, 번호(1. ①) 뒤, 지급 시점 낱말(익월, 13회) 뒤.
    그 밖의 퍼센트(보장비중 15% 미만 등)는 조건 문구로 보고 버리고 stray 로 센다.
    """
    t = re.sub(r"\s+", " ", reward or "").strip()
    head = re.split(r"\*\s*시책비 제외|※|√|\*\s*\d+회차 보험료", t)[0]     # 뒤에 붙은 제외·유의 문구는 지급률이 아님
    out, basis, stray, prev_end, option = [], "unspecified", 0, 0, None
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*(\(([^)]*)\)|\[([^\]]*)\])?", head):
        seg = head[prev_end:m.start()]
        opts = re.findall(r"(?:^|\s)([1-9])\.\s|([①②③④])", seg)
        if opts:
            option = "".join(opts[-1])
            basis = "unspecified" if not out else basis
        found = next((code for word, code in BASIS if word in seg), None)
        lead = re.sub(r"[\s*×xX]+$", "", seg)
        ok = (found is not None or lead == "" or lead.endswith("+") or bool(opts and re.search(r"(\d\.|[①②③④])$", lead))
              or re.search(TIMING_WORD + r"$", lead))
        if not ok:
            stray += 1
            continue
        basis = found or basis
        paren = m.group(3) or m.group(4) or ""
        near = re.findall(TIMING_WORD, seg)
        timing = _timing(paren) if _timing(paren) != "unspecified" else _timing(near[-1] if near else "")
        out.append({"option": option, "basis": basis, "rate": round(float(m.group(1)) / 100, 6), "pay_timing": timing,
                    "pay_condition": paren.strip() or None})
        prev_end = m.end()
    return out, head, stray


def _classify(recipient, category, criteria, reward):
    """(line_kind, parse_status, 메모, components)"""
    if recipient == "note":
        return "note", "note", None, []
    cat, rew = category or "", (reward or "").strip()
    comps, head, stray = _components(rew)
    if re.search(r"연속|브릿지|연계", cat):
        kind = "consecutive"
    elif "가동" in cat:
        kind = "activity"
    elif "구간" in cat or (not comps and re.search(r"구간", rew + (criteria or ""))):
        kind = "tier"
    elif comps:
        kind = "rate_promo"
    else:
        kind = "unknown"
    if not rew:
        return kind, "manual", "시상내용 칸이 비어 있음. 금액은 원본의 표·그림 확인 필요", []
    if kind == "rate_promo":
        if any(cp["option"] for cp in comps):
            return kind, "manual", "조건별 지급률이 여러 개", comps
        if stray or re.search(r"한도|限|초과|미만|이상|부지급", head):
            return kind, "manual", "한도 또는 조건부 지급률", comps
        return kind, "auto", None, comps
    if not comps and _timing(rew) != "unspecified":
        return kind, "manual", "지급 시점만 적혀 있음. 금액은 원본의 표·그림 확인 필요", []
    if kind in ("activity", "consecutive"):
        return kind, "manual", "가동·연속가동 조건은 사람이 구간으로 옮겨야 함", comps
    if kind == "tier":
        return kind, "manual", "구간 금액 시상", comps
    return kind, "manual", "지급률을 찾지 못함", comps


def normalize(rows) -> list:
    out, last = [], {}
    for r in rows:
        sheet = r["sheet"]
        c = [x if x is not None else "" for x in r["cells"]]
        s = [x.strip() for x in c]
        if s[:2] == COLUMNS[:2] or "원안을 준수" in "".join(s):
            continue                                                    # 안내·머리글
        if not any(s):
            continue
        if last.get("sheet") != sheet:
            last = {"sheet": sheet, "insurer": "", "recipient": "", "category": "", "period": ""}
        new_group = bool(s[0]) or bool(s[1])
        insurer = s[0] or last["insurer"]
        recipient_raw = s[1] or last["recipient"]
        if new_group and not s[2]:
            last["category"], last["period"] = "", ""
        key = re.split(r"[\s*(]", recipient_raw.strip(), maxsplit=1)[0] if recipient_raw.strip() else ""
        recipient = RECIPIENT.get(key.upper() if key.isascii() else key, "unknown")
        if recipient == "note":
            category, period_raw = "", ""
            criteria, reward = " / ".join(x for x in s[2:5] if x), s[5]
            if reward:
                criteria = " / ".join(x for x in (criteria, reward) if x)
                reward = ""
        else:
            category = s[2] or last["category"]
            period_raw = s[3] or (last["period"] if not s[2] else "")
            criteria, reward = c[4].strip(), c[5].strip()
            last["category"], last["period"] = category, period_raw
        last["insurer"], last["recipient"] = insurer, recipient_raw
        sector = next((code for key, code in SECTOR_BY_SHEET if key in sheet), None)
        p_from, p_to, p_note = _period(period_raw)
        kind, status, note, comps = _classify(recipient, category, criteria, reward)
        notes = [n for n in (note, p_note if recipient != "note" else None) if n]
        if "첨부" in recipient_raw and recipient != "note":
            notes.append("첨부파일 참조 표기")
        timing = "unspecified" if recipient == "note" else _timing(" ".join((category, reward)))
        if timing == "unspecified" and comps:
            kinds = {cp["pay_timing"] for cp in comps} - {"unspecified"}
            timing = kinds.pop() if len(kinds) == 1 else ("mixed" if kinds else "unspecified")
        if recipient == "unknown":
            status, notes = "manual", notes + ["시상대상을 알 수 없음"]
        if status == "auto" and p_note:
            status = "manual"
        out.append({
            "sheet": sheet, "row_no": r["row_no"], "sector": sector,
            "insurer_raw": c[0], "insurer_name": re.sub(r"\s+", "", insurer),
            "recipient_raw": recipient_raw, "recipient_level": recipient,
            "category_raw": category, "period_raw": period_raw, "period_from": p_from, "period_to": p_to,
            "criteria_raw": criteria, "reward_raw": reward,
            "pay_timing": timing,
            "line_kind": kind, "parse_status": status, "parse_note": "; ".join(notes) or None,
            "components": comps,
        })
    return out


# ---------------------------------------------------------------- 저장
class DuplicatePlan(Exception):
    pass


def load(con, path, period_ym, source_kind=None, title=None, loaded_by="system") -> dict:
    path = pathlib.Path(path)
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    dup = con.execute("SELECT plan_doc_id FROM incentive_plan_doc WHERE file_hash=?", (file_hash,)).fetchone()
    if dup:
        raise DuplicatePlan(f"이미 적재한 파일입니다 (plan_doc_id={dup['plan_doc_id']})")
    source_kind = source_kind or ("xlsx" if path.suffix.lower() in (".xlsx", ".xlsm") else "drive_extract")
    data = read_xlsx(path) if source_kind == "xlsx" else read_drive_extract(path)
    lines = normalize(data["rows"])
    con.execute("UPDATE incentive_plan_doc SET status='superseded' WHERE period_ym=? AND status!='superseded'", (period_ym,))
    doc_id = con.execute(
        """INSERT INTO incentive_plan_doc(period_ym, title, file_name, file_hash, source_kind, received_at, loaded_at, loaded_by,
                                          row_count, skipped_sheets, status)
           VALUES(?,?,?,?,?,?,?,?,?,?,'loaded')""",
        (period_ym, title or path.stem, path.name, file_hash, source_kind, db.now(), db.now(), loaded_by, len(lines),
         json.dumps(data["skipped"], ensure_ascii=False)),
    ).lastrowid
    names = {re.sub(r"\s+", "", r["name"]): r["insurer_id"] for r in con.execute("SELECT insurer_id, name FROM insurer")}
    for ln in lines:
        line_id = con.execute(
            """INSERT INTO incentive_plan_line(plan_doc_id, sheet, row_no, sector, insurer_raw, insurer_name, insurer_id,
                   recipient_raw, recipient_level, category_raw, period_raw, period_from, period_to, criteria_raw, reward_raw,
                   pay_timing, line_kind, parse_status, parse_note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, ln["sheet"], ln["row_no"], ln["sector"], ln["insurer_raw"], ln["insurer_name"], names.get(ln["insurer_name"]),
             ln["recipient_raw"], ln["recipient_level"], ln["category_raw"], ln["period_raw"], ln["period_from"], ln["period_to"],
             ln["criteria_raw"], ln["reward_raw"], ln["pay_timing"], ln["line_kind"], ln["parse_status"], ln["parse_note"]),
        ).lastrowid
        for seq, cp in enumerate(ln["components"], start=1):
            con.execute(
                "INSERT INTO incentive_plan_component(line_id, seq, option_label, basis, rate, pay_timing, pay_condition) VALUES(?,?,?,?,?,?,?)",
                (line_id, seq, cp["option"], cp["basis"], cp["rate"], cp["pay_timing"], cp["pay_condition"]),
            )
    con.commit()
    return {"plan_doc_id": doc_id, "lines": len(lines), "skipped": data["skipped"]}


def review(con, line_id, approve=True, by="담당자"):
    """원장 한 줄을 승인하거나 반려한다. 승인해야 계산 규칙으로 넘길 수 있다."""
    con.execute("UPDATE incentive_plan_line SET review_status=?, reviewed_by=?, reviewed_at=? WHERE line_id=?",
                ("approved" if approve else "rejected", by, db.now(), line_id))
    con.commit()


KIND_KO = {"rate_promo": "정률 시책", "activity": "가동 시상", "consecutive": "연속가동 시상", "tier": "구간 시상",
           "note": "비고", "unknown": "유형 미정"}
STATUS_KO = {"auto": "자동 해석", "manual": "사람 확인 필요", "note": "비고"}
LEVEL_KO = {"ga": "GA", "hq": "총괄", "branch": "지사", "fc": "FC", "note": "비고", "unknown": "미상"}
TIMING_KO = {"next_month": "익월", "inst13": "13회차", "next_month+inst13": "익월+13회차", "year1": "1차년", "year2": "2차년",
             "mixed": "혼합", "unspecified": "미표기"}


def summary(con, plan_doc_id) -> dict:
    one = lambda sql: [dict(r) for r in con.execute(sql, (plan_doc_id,))]
    doc = dict(con.execute("SELECT * FROM incentive_plan_doc WHERE plan_doc_id=?", (plan_doc_id,)).fetchone())
    return {
        "doc": doc,
        "skipped": json.loads(doc["skipped_sheets"] or "[]"),
        "by_sheet": one("SELECT sheet, COUNT(*) n, COUNT(DISTINCT insurer_name) insurers FROM incentive_plan_line WHERE plan_doc_id=? GROUP BY 1"),
        "by_level": one("SELECT recipient_level, COUNT(*) n FROM incentive_plan_line WHERE plan_doc_id=? GROUP BY 1 ORDER BY 2 DESC"),
        "by_kind": one("SELECT line_kind, parse_status, COUNT(*) n FROM incentive_plan_line WHERE plan_doc_id=? GROUP BY 1,2 ORDER BY 3 DESC"),
        "by_timing": one("SELECT pay_timing, COUNT(*) n FROM incentive_plan_line WHERE plan_doc_id=? AND recipient_level!='note' GROUP BY 1 ORDER BY 2 DESC"),
        "by_note": one("SELECT COALESCE(parse_note,'-') parse_note, COUNT(*) n FROM incentive_plan_line WHERE plan_doc_id=? AND parse_status='manual' GROUP BY 1 ORDER BY 2 DESC"),
        "period": one("""SELECT SUM(period_from IS NOT NULL) dated, SUM(period_from IS NULL) undated,
                         SUM(period_from IS NOT NULL AND (substr(period_from,9,2)!='01' OR julianday(period_to)-julianday(period_from)<27)) day_level
                         FROM incentive_plan_line WHERE plan_doc_id=? AND recipient_level!='note'""")[0],
        "components": one("""SELECT COUNT(*) n, SUM(c.basis!='unspecified') with_basis, SUM(c.pay_timing!='unspecified') with_timing
                             FROM incentive_plan_component c JOIN incentive_plan_line l ON l.line_id=c.line_id WHERE l.plan_doc_id=?""")[0],
        "matched_insurer": one("SELECT COUNT(DISTINCT insurer_name) total, COUNT(DISTINCT CASE WHEN insurer_id IS NOT NULL THEN insurer_name END) matched FROM incentive_plan_line WHERE plan_doc_id=?")[0],
    }


def print_summary(sm):
    d = sm["doc"]
    print(f"[시상안] {d['file_name']} ({d['source_kind']}) → 원장 {d['row_count']}줄, plan_doc_id={d['plan_doc_id']}")
    for x in sm["by_sheet"]:
        print(f"   시트 {x['sheet']}: {x['n']}줄, 보험사 {x['insurers']}곳")
    for x in sm["skipped"]:
        print(f"   적재하지 않은 시트 {x['sheet']}: {x['reason']}")
    print("   수령 계층: " + ", ".join(f"{LEVEL_KO[x['recipient_level']]} {x['n']}" for x in sm["by_level"]))
    print("   유형·해석: " + ", ".join(f"{KIND_KO[x['line_kind']]}/{STATUS_KO[x['parse_status']]} {x['n']}" for x in sm["by_kind"]))
    print("   지급 시점: " + ", ".join(f"{TIMING_KO[x['pay_timing']]} {x['n']}" for x in sm["by_timing"]))
    p, c, m = sm["period"], sm["components"], sm["matched_insurer"]
    print(f"   기간: 날짜로 읽음 {p['dated']} (이 중 일 단위 {p['day_level']}), 못 읽음 {p['undated']}")
    print(f"   지급률 조각 {c['n']}개: 기준 보험료 명시 {c['with_basis']}, 지급 시점 명시 {c['with_timing']}")
    print(f"   보험사 마스터 일치: {m['matched']}/{m['total']}")
    for x in sm["by_note"]:
        print(f"   확인 필요 {x['n']:>3}줄: {x['parse_note']}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        sys.exit("사용법: python -m app.loader.incentive_plan <시상안 파일(.xlsx 또는 드라이브 추출본)> <YYYYMM>")
    _con = db.connect()
    _res = load(_con, sys.argv[1], sys.argv[2])
    print_summary(summary(_con, _res["plan_doc_id"]))
