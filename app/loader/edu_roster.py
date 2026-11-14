"""교육 수료자 명부 적재기. 엑셀(xlsx)과 CSV 를 읽어 한 줄씩 수료로 넣는다.

실제 명부 양식은 아직 없다(D-09 제안: 드라이브의 수료자 명부를 원본으로 삼는다). 머리글은 샘플 양식의 가정이고
config/mapping/edu_roster.yaml 의 aliases 로 다른 이름도 받는다.

순서: 머리글 검사 → 같은 파일 막기 → 줄마다 사번, 과정, 차수, 날짜 맞추기 → 겹친 건 세기 → 수료 넣기.
- 머리글이 맞지 않으면 DB 에 아무것도 쓰지 않고 HeaderMismatch 로 사유를 돌려준다.
- 같은 파일(import_batch.file_hash)은 DuplicateFile 로 막는다. 배치는 import_batch.data_type='edu_roster' 다.
- 줄마다 education.record_completion(source='roster', batch_id, row_no, agent_ids=None) 을 부른다.
  돌려받은 created 가 거짓이면 겹친 건이다. 파일 안에서 겹친 것과 이미 들어간 수료와 겹친 것을 나눠 센다.
- 고친 명부를 다시 올릴 때는 replaces 에 앞 배치 번호를 준다. 앞 배치를 void_batch 로 빼고 superseded 로 표시한 뒤 새로 넣는다.
  지우지 않으므로 앞 배치의 줄과 원본 파일은 남는다. 새 파일에 넣을 줄이 하나도 없으면 앞 배치를 빼지 않고 멈춘다.
- 이름은 사번과 맞춰 보는 데만 쓰고 저장하지 않는다. 원본 행(raw_row)도 남기지 않는다.
  연락처, 주민번호, 생년월일 같은 민감 열은 머리글만 보고 버리며 그 칸의 값은 꺼내지 않는다.
- 품질 문제 메시지와 돌려주는 값에는 사람 이름을 넣지 않는다. 사번, 과정 코드, 줄 번호, 칸 이름만 넣는다.

막는 것과 경고의 기준은 계약현황 적재기와 같다. 그 줄을 넣을 수 없거나 넣으면 틀린 기록이 되면 block 이고 그 줄만 뺀다.
넣을 수 있으면 warn 이다. 줄이 모두 막히면 배치를 failed 로 둔다.

화면이 부르는 것: preview(con, path), load_roster(con, path, ...), batch_issues(con, batch_id), roster_batches(con).
"""
from __future__ import annotations

import csv
import io
import pathlib
import re
import unicodedata
from collections import Counter
from datetime import date

import openpyxl

from app.engine import education as edu
from app.loader import mapping
from app.loader.excel_loader import DuplicateFile, file_hash, finish_batch, issue, register_batch
from app.loader.sheet import HeaderMismatch
from app.loader.transforms import _blank, apply, normalize_header

DATA_TYPE = "edu_roster"
KIND = "edu_roster"
SOURCE, MAP_TYPE = "edu", "roster"
HEADER_SCAN = 10
CSV_SUFFIX = (".csv", ".txt")

FIELD_KO = {"employee_no": "사번", "name": "이름", "course_code": "과정 코드", "course_name": "과정명",
            "session_no": "차수", "session_date": "교육일", "completed_on": "수료일"}
MASK_CHARS = "*○●□■xX"


# ------------------------------------------------------------------ 매핑과 머리글
def load_mapping(period_ym=None) -> dict:
    return mapping.load(SOURCE, MAP_TYPE, period_ym or date.today().strftime("%Y%m"))


def _sensitive(header: str, keywords) -> bool:
    h = header.lower()
    return any(k.lower() in h for k in keywords or [])


def _alias_index(m) -> dict:
    """정규화한 머리글 → 표준 칸 이름."""
    return {normalize_header(a): f for f, names in m["aliases"].items() for a in names}


def _norm_row(row) -> list:
    headers = [normalize_header(v) if not _blank(v) else "" for v in row]
    while headers and headers[-1] == "":
        headers.pop()
    return headers


def _looks_like_header(headers, m) -> bool:
    idx = _alias_index(m)
    got = {idx[h] for h in headers if h in idx}
    return all(f in got for f in m.get("required", [])) and any(f in got for f in m["one_of"][0])


def plan_headers(headers, m) -> dict:
    """머리글을 검사한다. 돌려주는 것: {reasons(막는 사유), warnings, plan(표준 칸 → 열 위치), dropped, unknown}."""
    idx = _alias_index(m)
    ignore = {normalize_header(h) for h in m.get("ignore", [])}
    drop_kw = m.get("drop_keywords", [])
    dropped, unknown, reasons, warnings = [], [], [], []
    plan, seen_field, blanks = {}, {}, []
    kept_names = []
    for i, h in enumerate(headers):
        if h and _sensitive(h, drop_kw):
            dropped.append(h)
            continue
        if h == "":
            blanks.append(i + 1)
            continue
        kept_names.append(h)
        f = idx.get(h)
        if f is None:
            if h not in ignore:
                unknown.append(h)
            continue
        seen_field.setdefault(f, []).append(h)
        plan.setdefault(f, i)
    if blanks:
        reasons.append(f"이름 없는 열: {', '.join(map(str, blanks[:5]))}번째")
    dups = sorted({h for h in kept_names if kept_names.count(h) > 1})
    if dups:
        reasons.append(f"이름이 겹치는 열: {', '.join(dups)}")
    for f, hs in seen_field.items():
        if len(set(hs)) > 1:
            reasons.append(f"{FIELD_KO[f]} 칸으로 읽히는 열이 둘 이상입니다: {', '.join(hs)}")
    for f in m.get("required", []):
        if f not in plan:
            reasons.append(f"필수 열 없음: {FIELD_KO[f]} ({', '.join(m['aliases'][f])} 가운데 하나)")
    for group in m.get("one_of", []):
        if not any(f in plan for f in group):
            names = " 또는 ".join(FIELD_KO[f] for f in group)
            reasons.append(f"필수 열 없음: {names} 칸이 하나도 없습니다")
    if "name" not in plan:
        warnings.append("이름 칸이 없어 사번과 이름을 맞춰 보지 못했습니다")
    if unknown:
        warnings.append(f"매핑에 없는 열 {len(unknown)}개를 읽지 않았습니다: {', '.join(unknown)}")
    return {"reasons": reasons, "warnings": warnings, "plan": plan, "dropped": dropped, "unknown": unknown}


# ------------------------------------------------------------------ 파일 읽기
def _csv_rows(path, m):
    data = pathlib.Path(path).read_bytes()
    text = None
    for enc in m["file"].get("csv_encodings") or ["utf-8-sig", "cp949"]:
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise HeaderMismatch(["CSV 글자 인코딩을 읽지 못했습니다. UTF-8 또는 CP949 로 저장해 주세요"], pathlib.Path(path).name)
    first = text.split("\n", 1)[0]
    delim = "\t" if first.count("\t") > first.count(",") else ","
    return [("CSV", list(csv.reader(io.StringIO(text), delimiter=delim)))]


def _open(path, m):
    """(시트 이름, 머리글 행 번호, 머리글, 줄 반복자, 닫기 함수) 를 돌려준다. 머리글 행을 못 찾으면 HeaderMismatch."""
    name = pathlib.Path(path).name
    if pathlib.Path(path).suffix.lower() in CSV_SUFFIX:
        sheets, close = _csv_rows(path, m), (lambda: None)
        lazy = False
    else:
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception:
            raise HeaderMismatch(["엑셀(xlsx) 또는 CSV 파일만 읽습니다"], name) from None
        want = m["file"].get("sheet")
        sheets = [(ws.title, ws) for ws in ([wb[want]] if want and want in wb.sheetnames else wb.worksheets)]
        close, lazy = wb.close, True
    fixed = m["file"].get("header_row")
    for title, src in sheets:
        head = list(src.iter_rows(min_row=1, max_row=fixed or HEADER_SCAN, values_only=True)) if lazy else src[:fixed or HEADER_SCAN]
        for r in ([fixed] if fixed else range(1, len(head) + 1)):
            if r > len(head):
                continue
            headers = _norm_row(head[r - 1])
            if fixed or _looks_like_header(headers, m):
                if lazy:
                    body = ((i, row) for i, row in enumerate(src.iter_rows(min_row=r + 1, values_only=True), start=r + 1))
                else:
                    body = ((i, row) for i, row in enumerate(src[r:], start=r + 1))
                return title, r, headers, body, close
    close()
    names = " / ".join(f"{FIELD_KO[f]}({', '.join(m['aliases'][f])})" for f in ["employee_no", *m["one_of"][0]])
    raise HeaderMismatch([f"머리글 행을 찾지 못함: 앞 {HEADER_SCAN}행에 사번 칸과 과정 칸이 함께 있는 행이 없습니다. 받는 이름: {names}"], name)


# ------------------------------------------------------------------ 이름 맞추기
def _name_key(v) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", str(v or "")))


def same_name(roster_name, master_name) -> bool:
    """명부의 이름이 마스터와 같은가. 공백은 무시한다. 가린 글자(*, ○ 등)는 그 자리만 건너뛰고 길이는 같아야 한다."""
    a, b = _name_key(roster_name), _name_key(master_name)
    if not a or not b:
        return True
    if not any(c in MASK_CHARS for c in a):
        return a == b
    return len(a) == len(b) and all(x in MASK_CHARS or x == y for x, y in zip(a, b))


# ------------------------------------------------------------------ 줄 읽기 (DB 에 쓰지 않는다)
def _course_lookup(con, m):
    courses = {r["course_id"]: edu.course(con, r["course_id"]) for r in con.execute("SELECT course_id FROM edu_course")}
    by_code = {c["code"].strip().upper(): c for c in courses.values()}
    by_name = {_name_key(c["name"]): c for c in courses.values()}
    alias = {_name_key(k): str(v).strip().upper() for k, v in (m.get("course_alias") or {}).items()}
    return by_code, by_name, alias


def _sessions(con) -> dict:
    out: dict = {}
    for r in con.execute("SELECT session_id, course_id, session_no, date_from, date_to, status FROM edu_session ORDER BY date_from, session_no"):
        out.setdefault(r["course_id"], []).append(dict(r))
    return out


def parse(con, path, period_ym=None) -> dict:
    """명부를 읽어 넣을 줄과 문제를 돌려준다. DB 에 쓰지 않는다.

    돌려주는 것: {rows, issues, file_rows, dropped_columns, unknown_columns, sheet, header_row, mapping_version}.
    rows 의 한 줄: {row_no, agent_id, employee_no, course_id, course_code, cycle_key, session_id, completed_on}. 이름은 없다.
    issues 의 한 줄: {row_no, severity, check_code, message}. 머리글이 맞지 않으면 HeaderMismatch.
    """
    edu.ensure(con)
    m = load_mapping(period_ym)
    title, hrow, headers, body, close = _open(path, m)
    try:
        hp = plan_headers(headers, m)
        if hp["reasons"]:
            raise HeaderMismatch(hp["reasons"], pathlib.Path(path).name)
        plan, tf = hp["plan"], m.get("transforms", {})
        issues = [{"row_no": None, "severity": "warn", "check_code": "dropped_column",
                   "message": f"민감 정보 열 '{h}' 을 읽지 않고 버렸습니다"} for h in hp["dropped"]]
        issues += [{"row_no": None, "severity": "warn", "check_code": "header", "message": w} for w in hp["warnings"]]
        agents = {r["internal_code"].strip().upper(): dict(r) for r in
                  con.execute("SELECT agent_id, internal_code, name, status, termination_date FROM agent")}
        by_code, by_name, alias = _course_lookup(con, m)
        sessions = _sessions(con)
        name_sev = "warn" if m.get("name_mismatch") == "warn" else "block"
        min_year = int(m.get("min_year") or 2000)
        today = date.today().isoformat()
        rows, file_rows = [], 0
        for r_idx, row in body:
            cells = {f: (row[i] if i < len(row) else None) for f, i in plan.items()}
            if all(_blank(v) for v in cells.values()):
                continue
            file_rows += 1
            out = _row(r_idx, cells, tf, agents, by_code, by_name, alias, sessions, name_sev, min_year, today)
            issues += out["issues"]
            if out["row"]:
                rows.append(out["row"])
        return {"rows": rows, "issues": issues, "file_rows": file_rows, "dropped_columns": hp["dropped"],
                "unknown_columns": hp["unknown"], "sheet": title, "header_row": hrow, "mapping_version": m["mapping_version"]}
    finally:
        close()


def _row(r_idx, cells, tf, agents, by_code, by_name, alias, sessions, name_sev, min_year, today) -> dict:
    issues, blocked = [], False

    def add(sev, code, msg):
        nonlocal blocked
        issues.append({"row_no": r_idx, "severity": sev, "check_code": code,
                       "message": msg + (". 이 줄은 넣지 않았습니다" if sev == "block" else "")})
        blocked = blocked or sev == "block"

    std = {}
    for f, v in cells.items():
        try:
            std[f] = apply(v, tf.get(f, []))
        except (ValueError, TypeError):
            std[f] = None
            if f in ("completed_on", "session_date"):
                add("block", "bad_date", f"날짜 오류: {FIELD_KO[f]} 칸을 날짜로 읽지 못했습니다")
            else:
                add("warn", "bad_value", f"{FIELD_KO[f]} 칸을 읽지 못해 비워 둡니다")
    if not _blank(cells.get("session_no")) and std.get("session_no") is None:
        add("warn", "bad_value", "차수 칸을 숫자로 읽지 못해 비워 둡니다")
    for f in ("completed_on", "session_date"):
        if std.get(f) and int(std[f][:4]) < min_year:
            add("block", "bad_date", f"날짜 오류: {FIELD_KO[f]}가 {min_year}년보다 앞섭니다")
            std[f] = None

    # ---------- 사번과 이름 ----------
    no = std.get("employee_no")
    ag = None
    if not no:
        add("block", "blank_agent", "사번이 없습니다")
    else:
        ag = agents.get(no)
        if ag is None:
            add("block", "unmapped_agent", f"사번 {no} 이 설계사 마스터에 없습니다")
        elif std.get("name") and not same_name(std["name"], ag["name"]):
            add(name_sev, "name_mismatch", f"사번 {no} 의 이름이 설계사 마스터와 다릅니다")

    # ---------- 과정 ----------
    code, cname = std.get("course_code"), std.get("course_name")
    by_c = by_code.get(code) if code else None
    by_n = None
    if cname:
        key = _name_key(cname)
        by_n = by_name.get(key) or by_code.get(alias.get(key, "")) or by_code.get(key.upper())
    c = None
    if not code and not cname:
        add("block", "blank_course", "과정 칸이 비어 있습니다")
    elif code and by_c is None:
        add("block", "unknown_course", f"과정 코드 '{code}' 를 과정 목록에서 찾지 못했습니다")
    elif not code and by_n is None:
        add("block", "unknown_course", f"과정명 '{cname}' 을 과정 목록에서 찾지 못했습니다. 매핑의 course_alias 로 이을 수 있습니다")
    elif by_c and cname and by_n and by_n["course_id"] != by_c["course_id"]:
        add("block", "course_conflict", f"과정 코드 '{code}' 와 과정명이 서로 다른 과정입니다")
    else:
        c = by_c or by_n
        if by_c and cname and by_n is None:
            add("warn", "course_name_mismatch", f"과정명이 과정 '{code}' 의 이름과 다릅니다. 과정 코드로 넣습니다")
        if c["status"] != "active":
            add("warn", "closed_course", f"닫힌 과정 '{c['code']}' 의 수료입니다. 기록은 넣습니다")

    # ---------- 차수와 날짜 ----------
    s = None
    done = std.get("completed_on")
    sday = std.get("session_date")
    if c is not None:
        mine = sessions.get(c["course_id"], [])
        sno = std.get("session_no")
        if sno is not None:
            s = next((x for x in mine if x["session_no"] == sno), None)
            if s is None:
                add("warn" if (done or sday) else "block", "unknown_session",
                    f"과정 '{c['code']}' 에 {sno}차가 없습니다" + (". 차수 없이 넣습니다" if (done or sday) else ""))
            elif sday and not (s["date_from"] <= sday <= s["date_to"]):
                add("warn", "session_date_mismatch", f"교육일이 과정 '{c['code']}' {sno}차의 날짜와 다릅니다")
        elif sday:
            s = next((x for x in mine if x["date_from"] <= sday <= x["date_to"] and x["status"] != "cancelled"), None)
            if s is None:
                add("warn", "unknown_session", f"과정 '{c['code']}' 에 그 교육일의 차수가 없습니다. 차수 없이 넣습니다")
        if s is not None and s["status"] == "cancelled":
            add("block", "cancelled_session", f"과정 '{c['code']}' {s['session_no']}차는 취소된 차수입니다")
        if not blocked:
            done = done or (s["date_to"] if s else None) or sday
            if not done:
                add("block", "no_date", "수료일이 없습니다. 수료일, 차수, 교육일 가운데 하나가 있어야 합니다")
            else:
                if done > today:
                    add("warn", "future_date", "수료일이 오늘보다 뒤입니다. 그날이 되어야 진척도에 듭니다")
                if s and done < s["date_from"]:
                    add("warn", "date_before_session", f"수료일이 과정 '{c['code']}' {s['session_no']}차 시작일보다 앞섭니다")
    if ag and ag.get("status") == "terminated" and not blocked:
        add("warn", "left_agent", f"사번 {no} 은 퇴사한 설계사입니다. 기록은 넣습니다")
    if blocked:
        return {"row": None, "issues": issues}
    return {"row": {"row_no": r_idx, "agent_id": ag["agent_id"], "employee_no": no, "course_id": c["course_id"],
                    "course_code": c["code"], "cycle_key": edu.cycle_key(c, done),
                    "session_id": s["session_id"] if s else None, "completed_on": done},
            "issues": issues}


def _period(rows, period_ym) -> str:
    if period_ym:
        return str(period_ym)
    months = Counter(r["completed_on"][:7].replace("-", "") for r in rows)
    return months.most_common(1)[0][0] if months else date.today().strftime("%Y%m")


def _dup_issue(row_no, first_row, in_file, code, no) -> dict:
    if in_file:
        msg = f"사번 {no} 과정 '{code}' 가 {first_row}행과 같은 주기의 수료입니다. 앞 줄만 넣었습니다"
    else:
        msg = f"사번 {no} 과정 '{code}' 는 이미 들어간 수료와 같은 주기입니다. 넣지 않았습니다"
    return {"row_no": row_no, "severity": "warn", "check_code": "duplicate_in_file" if in_file else "duplicate_existing", "message": msg}


def _counts(issues) -> dict:
    block = sum(1 for i in issues if i["severity"] == "block")
    return {"block": block, "warn": len(issues) - block, "by_code": dict(Counter(i["check_code"] for i in issues))}


def _check_replaces(con, replaces):
    if replaces in (None, ""):
        return None
    try:
        replaces = int(replaces)
    except (TypeError, ValueError):
        raise ValueError("바꿀 명부 배치 번호가 숫자가 아닙니다.") from None
    r = con.execute("SELECT batch_id, data_type, status FROM import_batch WHERE batch_id=?", (replaces,)).fetchone()
    if not r or r["data_type"] != DATA_TYPE:
        raise ValueError("바꿀 명부 배치가 없습니다.")
    if r["status"] == "superseded":
        raise ValueError("이미 다른 명부로 바꾼 배치입니다.")
    return int(r["batch_id"])


# ------------------------------------------------------------------ 미리보기
def preview(con, path, period_ym=None, replaces=None) -> dict:
    """DB 에 쓰지 않고 넣으면 어떻게 되는지 돌려준다. 건수와 문제만 있고 이름은 없다.

    돌려주는 것: {ok, reasons, already_loaded, file_rows, loadable, new, duplicates, dup_in_file, dup_existing,
                  block, warn, by_code, issues, courses, period_ym, dropped_columns, unknown_columns, sheet, header_row}.
    replaces 를 주면 그 배치의 수료는 빠진다고 보고 겹친 건을 센다.
    """
    base = {"ok": False, "reasons": [], "already_loaded": False, "file_rows": 0, "loadable": 0, "new": 0, "duplicates": 0,
            "dup_in_file": 0, "dup_existing": 0, "block": 0, "warn": 0, "by_code": {}, "issues": [], "courses": {},
            "period_ym": period_ym, "dropped_columns": [], "unknown_columns": [], "sheet": None, "header_row": None}
    if already_loaded(con, file_hash(path)):
        base["already_loaded"] = True
        base["reasons"] = ["이미 넣은 파일입니다"]
        return base
    try:
        replaces = _check_replaces(con, replaces)
        p = parse(con, path, period_ym)
    except HeaderMismatch as e:
        base["reasons"] = list(e.reasons)
        return base
    except ValueError as e:
        base["reasons"] = [str(e)]
        return base
    issues = list(p["issues"])
    seen: dict = {}
    new = dup_in = dup_ex = 0
    for r in p["rows"]:
        key = (r["agent_id"], r["course_id"], r["cycle_key"])
        if key in seen:
            dup_in += 1
            issues.append(_dup_issue(r["row_no"], seen[key], True, r["course_code"], r["employee_no"]))
            continue
        seen[key] = r["row_no"]
        live = con.execute("""SELECT 1 FROM edu_completion WHERE agent_id=? AND course_id=? AND cycle_key=? AND voided_at IS NULL
                              AND batch_id IS NOT ?""", (*key, replaces)).fetchone()
        if live:
            dup_ex += 1
            issues.append(_dup_issue(r["row_no"], None, False, r["course_code"], r["employee_no"]))
        else:
            new += 1
    issues.sort(key=lambda i: (i["row_no"] or 0, i["severity"]))
    base.update(_counts(issues))
    reasons = [] if p["rows"] else ["넣을 수 있는 줄이 없습니다. 문제 목록을 확인해 주세요"]
    base.update(ok=not reasons, reasons=reasons, file_rows=p["file_rows"], loadable=len(p["rows"]), new=new,
                duplicates=dup_in + dup_ex, dup_in_file=dup_in, dup_existing=dup_ex, issues=issues,
                courses=dict(Counter(r["course_code"] for r in p["rows"])), period_ym=_period(p["rows"], period_ym),
                dropped_columns=p["dropped_columns"], unknown_columns=p["unknown_columns"], sheet=p["sheet"], header_row=p["header_row"])
    return base


# ------------------------------------------------------------------ 적재
def load_roster(con, path, period_ym=None, *, via="manual", file_path=None, by="system", replaces=None) -> dict:
    """명부 한 파일을 넣는다. 머리글이 맞지 않으면 HeaderMismatch, 같은 파일이면 DuplicateFile 을 던진다.

    period_ym 이 없으면 수료일이 가장 많은 달을 귀속월로 둔다. replaces 를 주면 그 배치를 빼고 바꾼다.
    돌려주는 것: {batch_id, batch_status, file_rows, loaded, duplicates, dup_in_file, dup_existing, block, warn, by_code,
                  courses, period_ym, replaced_batch, voided, dropped_columns, unknown_columns}. 이름은 없다.
    """
    replaces = _check_replaces(con, replaces)
    p = parse(con, path, period_ym)                      # 머리글이 틀리면 여기서 막혀 아무것도 쓰지 않는다
    if replaces is not None and not p["rows"]:
        raise ValueError("새 명부에 넣을 수 있는 줄이 없어 앞 배치를 빼지 않았습니다. 문제 목록을 확인해 주세요.")
    ym = _period(p["rows"], period_ym)
    batch_id = register_batch(con, path, DATA_TYPE, ym, via=via, by=by, mapping_version=p["mapping_version"])
    try:
        voided = 0
        if replaces is not None:
            # void_batch 는 스스로 커밋한다. 새 배치의 등록과 앞 배치의 superseded 표시도 함께 커밋된다
            con.execute("UPDATE import_batch SET status='superseded', note=? WHERE batch_id=?", (f"배치 {batch_id} 로 바꿈", replaces))
            voided = edu.void_batch(con, replaces, by=by, reason=f"명부를 고쳐 다시 올림. 배치 {batch_id} 로 바꿈")
        issues = list(p["issues"])
        loaded = dup_in = dup_ex = 0
        for r in p["rows"]:
            try:
                res = edu.record_completion(con, agent_id=r["agent_id"], course_id=r["course_id"], by=by, agent_ids=None,
                                            completed_on=r["completed_on"], session_id=r["session_id"], source="roster",
                                            batch_id=batch_id, row_no=r["row_no"], commit=False)
            except ValueError as e:
                issues.append({"row_no": r["row_no"], "severity": "block", "check_code": "bad_row", "message": f"{e} 이 줄은 넣지 않았습니다"})
                continue
            if res["created"]:
                loaded += 1
                continue
            prev = con.execute("SELECT batch_id, row_no FROM edu_completion WHERE completion_id=?", (res["duplicate_of"],)).fetchone()
            in_file = prev is not None and prev["batch_id"] == batch_id
            dup_in += in_file
            dup_ex += not in_file
            issues.append(_dup_issue(r["row_no"], prev["row_no"] if in_file else None, in_file, r["course_code"], r["employee_no"]))
        issues.sort(key=lambda i: (i["row_no"] or 0, i["severity"]))
        for i in issues:
            issue(con, batch_id, i["severity"], i["check_code"], i["message"], i["row_no"])
        failed = loaded + dup_in + dup_ex == 0             # 넣은 줄도 겹친 줄도 없으면 실패다
        con.execute("UPDATE import_batch SET file_path=? WHERE batch_id=?", (str(file_path or path), batch_id))
        finish_batch(con, batch_id, p["file_rows"], failed=failed)
    except Exception:
        con.rollback()
        if replaces is not None:
            con.execute("UPDATE import_batch SET status='failed', note='적재 중 오류. 앞 배치는 빠졌습니다. 고친 파일을 다시 올려 주세요' WHERE batch_id=?",
                        (batch_id,))
            con.commit()
        raise
    out = {"batch_id": batch_id, "batch_status": "failed" if failed else "active", "file_rows": p["file_rows"], "loaded": loaded,
           "duplicates": dup_in + dup_ex, "dup_in_file": dup_in, "dup_existing": dup_ex,
           "courses": dict(Counter(r["course_code"] for r in p["rows"])), "period_ym": ym,
           "replaced_batch": replaces, "voided": voided, "dropped_columns": list(p["dropped_columns"]),
           "unknown_columns": len(p["unknown_columns"])}
    out.update(_counts(issues))
    return out


# ------------------------------------------------------------------ 화면용 조회
def batch_issues(con, batch_id) -> list:
    """배치의 품질 문제 목록. 메시지에는 사람 이름이 없다."""
    return [dict(r) for r in con.execute(
        "SELECT severity, check_code, row_no, message FROM quality_issue WHERE batch_id=? ORDER BY severity, row_no", (batch_id,))]


def roster_batches(con) -> list:
    """명부 배치 목록과 배치마다 살아 있는 수료 수. 다시 올리기에서 바꿀 배치를 고를 때 쓴다."""
    edu.ensure(con)
    return [dict(r) for r in con.execute(
        """SELECT b.batch_id, b.file_name, b.period_ym, b.version, b.status, b.row_count, b.received_via, b.loaded_at, b.loaded_by, b.note,
                  (SELECT COUNT(*) FROM edu_completion c WHERE c.batch_id=b.batch_id AND c.voided_at IS NULL) live,
                  (SELECT COUNT(*) FROM quality_issue q WHERE q.batch_id=b.batch_id AND q.severity='block') block
           FROM import_batch b WHERE b.data_type=? ORDER BY b.batch_id DESC""", (DATA_TYPE,))]


# ------------------------------------------------------------------ 연결 처리기
def already_loaded(con, file_hash_) -> bool:
    """같은 해시의 파일이 이미 들어왔는가. import_batch.file_hash 는 종류와 상관없이 하나뿐이다."""
    return con.execute("SELECT 1 FROM import_batch WHERE file_hash=?", (file_hash_,)).fetchone() is not None


def _load(con, path, raw, rel_path) -> dict:
    try:
        out = load_roster(con, path, raw.period_ym, via=raw.via, file_path=rel_path, by=raw.meta.get("by", "connect"),
                          replaces=raw.meta.get("replaces"))
    except HeaderMismatch as e:
        con.rollback()
        return {"failed": True, "reason": str(e)}
    if out["batch_status"] == "failed":
        out["failed"] = True
        out["reason"] = "명부에 넣을 수 있는 줄이 없습니다. 품질 문제 목록을 확인해 주세요"
    return out


def register() -> None:
    from app.connect.handlers import register_handler

    # 귀속월이 없어도 받는다. 없으면 수료일이 가장 많은 달로 둔다
    register_handler(KIND, _load, already_loaded=already_loaded, duplicate_errors=(DuplicateFile,), needs_period=False)


__all__ = ["preview", "load_roster", "batch_issues", "roster_batches", "parse", "same_name", "plan_headers",
           "already_loaded", "register", "DuplicateFile", "HeaderMismatch"]
