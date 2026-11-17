"""대조용 미리보기 DB 의 실적 적재기. 2026-11-17 드라이브의 두 양식, 인별업적순위와 보험사별을 읽는다.

대표님 지시(2026-11-17): 사원현황은 업적실적 데이터로 구분하면 된다. 그래서 사원현황조회 파일 없이 실적 파일의 조직 칸으로
조직과 사람을 만든다. 이 모듈의 결과는 scripts/build_trial_db.py 가 data/real/ 아래의 대조용 DB 에만 넣는다.

- 매핑은 config/mapping/eternal_rank_perf.yaml 과 eternal_insurer_perf.yaml 이다. 머리글 두 줄은 person_perf.read_layout 으로 읽는다.
  필요한 칸이 없거나 모르는 칸이 있으면 DB 에 아무것도 쓰지 않고 HeaderMismatch 로 막는다.
- 파일 칸 이름과 실제 계층이 한 단계씩 어긋난다. 지점 칸이 총괄본부, 센터 칸이 예하 본부, 팀 칸이 지점이다. 매핑의 표준 이름이 실제 뜻이다.
- 민감 열은 머리글만 보고 버린다. 사원명은 설계사를 만들 때만 쓰고 실적 표와 원본 행(raw_row)에는 남기지 않는다.
- 같은 파일은 두 번 넣지 않는다. 바이트가 같으면 import_batch.file_hash 로, 이름과 저장 정보만 다르고 본문이 같으면
  본문 해시(content_hash)로 가린다. 본문 해시는 import_batch.note 에 'content:' 를 붙여 남긴다.
- 파일 안에 기간이 없다. 월은 부르는 쪽이 정한다. 인별업적순위는 누적과 평균이 다르면 여러 달 파일로 보고 막는다.
- 경고와 오류 메시지에는 사람 이름과 금액을 넣지 않는다. 사원번호, 칸 이름, 건수만 넣는다.

month_sum_contracts 는 대조용 DB 에만 쓰는 연결이다. 보험사별 파일의 한 칸(사람, 시트, 보험사)을 '그 달 대표 계약 1건' 으로
계약 원장에 넣는다. 상품명과 상태 원문에 MONTH_SUM_LABEL 을 적어 만든 행임을 표시한다. 운영 적재에는 쓰지 않는다.
"""
from __future__ import annotations

import calendar
import fnmatch
import hashlib
import json
import pathlib
from collections import Counter

import openpyxl

from app import db
from app.loader import mapping, person_perf
from app.loader.excel_loader import DuplicateFile, finish_batch, issue, register_batch
from app.loader.sheet import HeaderMismatch
from app.loader.transforms import _blank, as_text, normalize_header
from app.org import levels, sync

RANK, INSURER = "rank_perf", "insurer_perf"
REF_YM = "202611"                       # 매핑 버전을 고르는 기준 달. 파일에 기간이 없어 고정한다
CONTENT_NOTE = "content:"
MAIN_TYPE, SIDE_TYPE = "person_perf", "person_perf_insurer"   # 그 달 실적 표를 가진 배치, 보험사별 묶음만 보탠 배치
MONTH_SUM_LABEL = "월 합계 대표 계약(대조용 추정)"
PERF_NUM_COLS = ("total_count", "total_monthly_premium_amt", "total_premium_per_policy_amt", "total_insurer_perf_amt", "total_global_perf_amt",
                 "active_10_count", "active_10_monthly_premium_amt", "elite_50_count", "elite_50_premium_amt", "single_count", "single_premium_amt",
                 "single_insurer_perf_amt", "auto_count", "auto_premium_amt", "auto_global_perf_amt", "general_count", "general_premium_amt",
                 "general_global_perf_amt")
RANK_TO_PERF = ("total_count", "total_monthly_premium_amt", "total_insurer_perf_amt")   # 인별업적순위 누적에서 실적 표로 옮기는 칸
# 계약 원장의 조직 칸은 파일 칸 이름을 따른다 (config/org_levels.yaml 의 contract 짝)
CONTRACT_ORG = {"org_company": "org_head_office", "org_division": "org_hq", "org_general": "org_branch", "org_hq": "org_center", "org_branch": "org_team"}


def load_map(kind: str) -> dict:
    return mapping.load("eternal", kind, REF_YM)


def kind_of(path) -> str | None:
    """파일 이름으로 양식을 고른다. 매핑의 name_glob 을 쓴다. 어느 것도 아니면 None."""
    name = pathlib.Path(path).name
    for kind in (RANK, INSURER):
        if fnmatch.fnmatch(name, load_map(kind)["file"]["name_glob"]):
            return kind
    return None


def content_hash(wb) -> str:
    """시트 이름과 모든 칸의 값으로 만든 해시. 저장 시각이나 작성자처럼 본문 밖의 차이는 보지 않는다."""
    h = hashlib.sha256()
    for ws in wb.worksheets:
        h.update(("\x00sheet:" + ws.title).encode())
        for row in ws.iter_rows(values_only=True):
            while row and row[-1] is None:
                row = row[:-1]
            if row:
                h.update(json.dumps(row, ensure_ascii=False, default=str).encode())
    return h.hexdigest()


def _count_field(field: str) -> bool:
    return field == "count" or field.endswith("_count")


def _cell(row, col):
    return row[col] if col is not None and col < len(row) else None


def _read_sheet(ws, m, file_name) -> dict:
    """사람 줄을 읽는다. 돌려주는 것: {rows: {사번: 줄}, summary_rows, summary_match, issues, dropped, warnings}.
    줄: {employee_no, name, org, values, insurers: {보험사 원문: (건수, 금액)}, row_no, raw}. raw 에는 사원명이 없다."""
    lay = person_perf.read_layout(ws, m)
    if lay["reasons"]:
        raise HeaderMismatch(lay["reasons"], f"{file_name} / {ws.title}")
    person, groups, insurers, labels = lay["person"], lay["groups"], lay["insurers"], lay["labels"]
    name_col = person.get("name")
    keep_cols = sorted(c for c in labels if c != name_col)
    hr = m["file"].get("header_rows", 2)
    rows, summary, issues = {}, [], []
    for r_idx, row in enumerate(ws.iter_rows(min_row=hr + 1, values_only=True), start=hr + 1):
        if all(_blank(v) for v in row):
            continue
        code = as_text(_cell(row, person["employee_no"])).strip().upper()
        nm = as_text(_cell(row, name_col)).strip()
        values, ins, bad = {}, {}, []
        for cols in groups.values():
            for field, col in cols.items():
                try:
                    values[field] = person_perf._num(_cell(row, col), _count_field(field))
                except (ValueError, TypeError):
                    values[field] = 0
                    bad.append(labels[col])
        for iname, icols in insurers.items():
            try:
                ins[iname] = (person_perf._num(_cell(row, icols["policy_count"]), True), person_perf._num(_cell(row, icols["monthly_premium_amt"]), False))
            except (ValueError, TypeError):
                ins[iname] = (0, 0)
                bad.append(iname)
        if not code and not nm:                                   # 합계 줄
            summary.append((values, ins, bad))
            continue
        if not code:
            issues.append(("block", "bad_row", f"{ws.title}: 사원번호가 비어 있습니다. 이 행은 넣지 않았습니다", r_idx))
            continue
        if code in rows:
            issues.append(("block", "duplicate_key", f"{ws.title}: 사원번호 {code} 가 {rows[code]['row_no']}행과 겹칩니다. 이 행은 넣지 않았습니다", r_idx))
            continue
        if bad:
            issues.append(("block", "bad_row", f"{ws.title}: 숫자로 읽을 수 없는 칸이 있습니다: {', '.join(bad[:5])}. 이 행은 넣지 않았습니다", r_idx))
            continue
        org = {k: (as_text(_cell(row, person.get(k))).strip() or None) if person.get(k) is not None else None for k in m["org_levels"]}
        rows[code] = {"employee_no": code, "name": nm, "org": org, "values": values, "insurers": ins, "row_no": r_idx,
                      "raw": {labels[c]: _cell(row, c) for c in keep_cols}}
    match = None
    if summary:
        match = True
        for values, ins, bad in summary:
            if bad:
                match = False
                continue
            for f, v in values.items():
                if abs(v - sum(r["values"][f] for r in rows.values())) > 0.5:
                    match = False
            for iname, (n, amt) in ins.items():
                if abs(n - sum(r["insurers"][iname][0] for r in rows.values())) > 0.5 or abs(amt - sum(r["insurers"][iname][1] for r in rows.values())) > 0.5:
                    match = False
        issues.append(("warn", "summary_row", f"{ws.title}: 합계 줄 {len(summary)}개를 사람으로 넣지 않았습니다. 사람 줄의 합과 {'같습니다' if match else '다릅니다'}", None))
    return {"rows": rows, "summary_rows": len(summary), "summary_match": match, "issues": issues, "dropped": lay["dropped"],
            "warnings": [f"{ws.title}: {w}" for w in lay["warnings"]]}


def _base(path, kind, m, wb) -> dict:
    path = pathlib.Path(path)
    return {"path": str(path), "file": path.name, "kind": kind, "mapping_version": m["mapping_version"], "content_hash": content_hash(wb),
            "people": {}, "issues": [], "dropped": [], "warnings": [], "blocked": False, "summary_match": {}}


def read_rank(path) -> dict:
    """인별업적순위 한 파일을 읽는다. DB 에 쓰지 않는다. 머리글이 맞지 않으면 HeaderMismatch.
    people 의 한 사람: {employee_no, name, org, values, row_no, raw}. 누적과 평균이 다른 사람이 있으면 blocked 다."""
    m = load_map(RANK)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        out = _base(path, RANK, m, wb)
        sheet = m["file"]["sheet"]
        if sheet not in wb.sheetnames:
            raise HeaderMismatch([f"시트 '{sheet}' 가 없습니다. 있는 시트: {', '.join(wb.sheetnames)}"], out["file"])
        res = _read_sheet(wb[sheet], m, out["file"])
    finally:
        wb.close()
    out.update(issues=res["issues"], dropped=res["dropped"], warnings=res["warnings"], summary_match={sheet: res["summary_match"]})
    total, avg = m["groups"]["total"]["subs"], m["groups"]["avg"]["subs"]
    pairs = [(total[k], avg[k]) for k in total if k in avg]
    multi = 0
    for code, r in res["rows"].items():
        if any(abs(r["values"][t] - r["values"][a]) > 0.5 for t, a in pairs):
            multi += 1
        out["people"][code] = {k: r[k] for k in ("employee_no", "name", "org", "values", "row_no", "raw")}
    if multi:
        out["blocked"] = True
        out["issues"].append(("block", "multi_month", f"누적과 평균이 다른 사람이 {multi}명입니다. 여러 달 파일로 보여 넣지 않습니다", None))
    return out


def read_insurer(path) -> dict:
    """보험사별 한 파일의 네 시트를 읽어 사람마다 합친다. DB 에 쓰지 않는다. 머리글이 맞지 않으면 HeaderMismatch.
    people 의 한 사람: {employee_no, name, org, row_no, sheets: {시트 key: {total: (건수, 금액), insurers: {원문: (건수, 금액)}}}, raw: {시트: 원본}}."""
    m = load_map(INSURER)
    sheets = m["file"]["sheets"]
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        out = _base(path, INSURER, m, wb)
        missing = [s for s in sheets if s not in wb.sheetnames]
        extra = [s for s in wb.sheetnames if s not in sheets]
        if missing or extra:
            reasons = ([f"시트가 없습니다: {', '.join(missing)}"] if missing else []) + ([f"매핑에 없는 시트가 있습니다: {', '.join(extra)}"] if extra else [])
            raise HeaderMismatch(reasons, out["file"])
        per = {}
        for sname, cfg in sheets.items():
            sm = dict(m, known_insurers=cfg.get("known_insurers", []))
            per[sname] = _read_sheet(wb[sname], sm, out["file"])
    finally:
        wb.close()
    org_diff, absent = 0, 0
    for sname, res in per.items():
        key = sheets[sname]["key"]
        out["issues"] += res["issues"]
        out["dropped"] += [f"{sname}: {d}" for d in res["dropped"]]
        out["warnings"] += res["warnings"]
        out["summary_match"][sname] = res["summary_match"]
        for code, r in res["rows"].items():
            p = out["people"].get(code)
            if p is None:
                p = out["people"][code] = {"employee_no": code, "name": r["name"], "org": r["org"], "row_no": r["row_no"], "sheets": {}, "raw": {}}
            elif p["org"] != r["org"] or p["name"] != r["name"]:
                org_diff += 1
            total = m["groups"]["total"]["subs"]
            p["sheets"][key] = {"total": (r["values"][total["건수"]], r["values"][total["보험료(월납)"]]), "insurers": r["insurers"]}
            p["raw"][sname] = r["raw"]
    keys = {cfg["key"] for cfg in sheets.values()}
    for p in out["people"].values():
        if set(p["sheets"]) != keys:
            absent += 1
    if org_diff:
        out["warnings"].append(f"시트마다 이름이나 조직 칸이 다른 사람 {org_diff}건. 첫 시트의 값을 썼습니다")
    if absent:
        out["warnings"].append(f"네 시트 가운데 일부에만 있는 사람 {absent}명")
    return out


def read_file(path) -> dict:
    kind = kind_of(path)
    if kind == RANK:
        return read_rank(path)
    if kind == INSURER:
        return read_insurer(path)
    raise ValueError(f"양식을 알 수 없는 파일입니다: {pathlib.Path(path).name}")


def dedupe(reads: list) -> tuple:
    """본문이 같은 파일을 가린다. 파일 이름 순으로 앞의 것을 남긴다. 돌려주는 것: (남긴 것, [(버린 파일, 남긴 파일)])."""
    keep, seen, dropped = [], {}, []
    for r in sorted(reads, key=lambda x: x["file"]):
        h = r["content_hash"]
        if h in seen:
            dropped.append((r["file"], seen[h]["file"]))
            continue
        seen[h] = r
        keep.append(r)
    return keep, dropped


# ------------------------------------------------------------------ 조직과 사람
def org_steps(org: dict, m: dict) -> tuple:
    """조직 칸 {표준 이름: 값} 을 ([(계층 코드, 이름)], 뿌리 이름) 으로. 빈 칸은 건너뛴다. 경로 규칙은 app/org/sync.py 다."""
    root = levels.root_code()
    steps, root_name = [], None
    for key, code in m["org_levels"].items():
        v = org.get(key)
        if not v:
            continue
        if code == root:
            root_name = v
        else:
            steps.append((code, v))
    return steps, root_name


def pick_people(reads: list, month_of: dict) -> dict:
    """사람마다 이름과 조직을 하나로 정한다. 달이 정해진 파일이 있으면 가장 늦은 달 파일의 칸을 쓰고,
    없으면 여러 파일에서 가장 많이 나온 조직을 쓴다(같으면 파일 이름 순으로 뒤의 것).
    돌려주는 것: {사번: {name, org, paths}}. paths 는 그 사람이 나온 서로 다른 조직 경로 수다."""
    seen = {}
    for r in sorted(reads, key=lambda x: (month_of.get(x["file"]) or "", x["file"])):
        ym = month_of.get(r["file"]) or ""
        for code, p in r["people"].items():
            seen.setdefault(code, []).append((ym, r["file"], p["name"], p["org"]))
    out = {}
    for code, items in seen.items():
        paths = Counter(tuple(sorted(i[3].items())) for i in items)
        dated = [i for i in items if i[0]]
        if dated:
            ym, _, name, org = dated[-1]
        else:
            best = max(paths.values())
            ym, _, name, org = [i for i in items if paths[tuple(sorted(i[3].items()))] == best][-1]
        out[code] = {"name": name, "org": org, "paths": len(paths)}
    return out


def build_people(con, people: dict, from_ym: str) -> dict:
    """조직 마디, 설계사, 소속을 만든다. 다시 돌려도 같다. 커밋한다.
    돌려주는 것: {agents: {사번: agent_id}, stats: {new_orgs, new_agents, no_org, multi_path, gap, short_path}}."""
    m = load_map(RANK)
    stats = Counter()
    agents = {}
    for code in sorted(people):
        p = people[code]
        steps, root_name = org_steps(p["org"], m)
        if not steps:
            stats["no_org"] += 1
        vals = [p["org"].get(k) for k in m["org_levels"]]
        while vals and not vals[-1]:
            vals.pop()
        if any(not v for v in vals):
            stats["gap"] += 1                       # 중간 계층이 빈 사람
        if len(vals) < len(m["org_levels"]):
            stats["short_path"] += 1                # 맨 아래 계층(지점)이 비어 위 조직에 바로 붙은 사람
        org_id = sync.ensure_path(con, steps, root_name, stats)
        a = con.execute("SELECT agent_id FROM agent WHERE internal_code=?", (code,)).fetchone()
        if a:
            aid = a[0]
        else:
            aid = con.execute("INSERT INTO agent(internal_code, name, status, job_type) VALUES(?,?,'active','fc')", (code, p["name"] or code)).lastrowid
            stats["new_agents"] += 1
        cur = con.execute("SELECT org_id FROM agent_org_history WHERE agent_id=? AND effective_to IS NULL", (aid,)).fetchone()
        if not cur:
            con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (aid, org_id, from_ym))
        if p.get("paths", 1) > 1:
            stats["multi_path"] += 1
        agents[code] = aid
    con.commit()
    return {"agents": agents, "stats": dict(stats)}


# ------------------------------------------------------------------ 한 달 적재
def _insurer_ids(con, m) -> dict:
    master = {normalize_header(r["name"]): r["insurer_id"] for r in con.execute("SELECT insurer_id, name FROM insurer")}
    alias = {normalize_header(k): normalize_header(v) for k, v in (m.get("insurer_alias") or {}).items()}
    return lambda name: master.get(alias.get(normalize_header(name), normalize_header(name)))


def _register(con, r, data_type, ym, by) -> int:
    dup = con.execute("SELECT batch_id, file_name FROM import_batch WHERE note=?", (CONTENT_NOTE + r["content_hash"],)).fetchone()
    if dup:
        raise DuplicateFile(f"본문이 같은 파일이 이미 적재됐습니다 (batch {dup['batch_id']}: {dup['file_name']})")
    bid = register_batch(con, r["path"], data_type, ym, via="manual", by=by, mapping_version=r["mapping_version"])
    con.execute("UPDATE import_batch SET note=? WHERE batch_id=?", (CONTENT_NOTE + r["content_hash"], bid))
    for sev, code, msg, row in r["issues"]:
        issue(con, bid, sev, code, msg, row)
    for h in r["dropped"]:
        issue(con, bid, "warn", "dropped_column", f"민감 정보 열 '{h}' 을 읽지 않고 버렸습니다")
    for w in r["warnings"]:
        issue(con, bid, "warn", "header_note", w)
    raw = []
    for p in r["people"].values():
        if r["kind"] == RANK:
            raw.append((bid, load_map(RANK)["file"]["sheet"], p["row_no"], json.dumps(p["raw"], ensure_ascii=False, default=str)))
        else:
            for sname, vals in p["raw"].items():
                raw.append((bid, sname, p["row_no"], json.dumps(vals, ensure_ascii=False, default=str)))
    con.executemany("INSERT INTO raw_row(batch_id, sheet_name, row_no, raw_json) VALUES(?,?,?,?)", raw)
    return bid


def load_month(con, ym: str, *, rank=None, ins=None, agents: dict, by="대조용 DB") -> dict:
    """한 달의 인별업적순위(rank)와 보험사별(ins) 읽은 결과를 실적 표에 넣는다. 둘 가운데 하나만 있어도 된다.
    한 사람의 한 달은 실적 표 한 줄이다. Total 은 인별업적순위의 누적, 없으면 보험사별 네 시트 총합계의 합이다.
    같은 본문은 DuplicateFile, blocked 인 파일은 ValueError 로 막는다. 돌려주는 것은 건수와 대조 결과다. 금액은 담지 않는다."""
    if not (rank or ins):
        raise ValueError("그 달의 파일이 없습니다")
    for r in (rank, ins):
        if r and r["blocked"]:
            raise ValueError(f"{r['file']}: 막힌 파일이라 넣지 않습니다")
    person_perf.ensure(con)
    m_ins = load_map(INSURER)
    sheets = {cfg["key"]: cfg for cfg in m_ins["file"]["sheets"].values()}
    ins_id = _insurer_ids(con, m_ins)
    try:
        main = _register(con, rank or ins, MAIN_TYPE, ym, by)
        side = _register(con, ins, SIDE_TYPE, ym, by) if (rank and ins) else None
        ins_batch = side or (main if ins else None)
        stats = Counter()
        perf_ids, want = {}, {"rows": 0, "count": 0, "amt": 0}
        unmapped = set()
        codes = sorted(set(rank["people"] if rank else ()) | set(ins["people"] if ins else ()))
        for code in codes:
            rp = rank["people"].get(code) if rank else None
            ip = ins["people"].get(code) if ins else None
            src = rp or ip
            rec = {"position": None, "org_general": src["org"].get("org_general"), "org_hq": src["org"].get("org_hq"),
                   "org_branch": src["org"].get("org_branch"), "org_direct_branch": None, "org_team": None}
            rec.update({k: 0 for k in PERF_NUM_COLS})
            rec["active_small_count"] = rec["active_small_monthly_premium_amt"] = None
            ins_count = sum(s["total"][0] for s in ip["sheets"].values()) if ip else 0
            ins_amt = sum(s["total"][1] for s in ip["sheets"].values()) if ip else 0
            if rp:
                for k in RANK_TO_PERF:
                    rec[k] = rp["values"][k]
                stats["from_rank"] += 1
                if ip and abs(ins_count - rec["total_count"]) > 0.5:
                    stats["rank_vs_insurer_count_diff"] += 1
            else:
                rec["total_count"], rec["total_monthly_premium_amt"] = int(ins_count), ins_amt
                stats["from_insurer_only"] += 1
            if ip:
                for key, s in ip["sheets"].items():
                    g = sheets[key].get("perf_group")
                    if g:
                        rec[f"{g}_count"], rec[f"{g}_premium_amt"] = int(s["total"][0]), s["total"][1]
            rec["perf_tier"] = person_perf._tier(rec)
            cols = ["batch_id", "employee_no", "agent_id", "start_ym", "end_ym", "row_no"] + list(rec)
            pid = con.execute(f"INSERT INTO agent_period_perf({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              [main, code, agents.get(code), ym, ym, src["row_no"]] + list(rec.values())).lastrowid
            perf_ids[code] = pid
            want["rows"] += 1
            want["count"] += rec["total_count"]
            want["amt"] += rec["total_monthly_premium_amt"]
            if agents.get(code) is None:
                stats["unmapped_agents"] += 1
            if ip:
                rows = []
                for key, s in ip["sheets"].items():
                    for name, (n, amt) in s["insurers"].items():
                        iid = ins_id(name)
                        if iid is None:
                            unmapped.add(name)
                        if n or amt:
                            rows.append((pid, name + (sheets[key].get("name_suffix") or ""), iid, n, amt))
                con.executemany("INSERT INTO agent_period_perf_insurer(perf_id, insurer_name, insurer_id, policy_count, monthly_premium_amt) VALUES(?,?,?,?,?)", rows)
                stats["insurer_rows"] += len(rows)
            stats["tier_" + rec["perf_tier"]] += 1
        if unmapped and ins_batch:
            issue(con, ins_batch, "warn", "unmapped_insurer", f"원수사 마스터에 없는 보험사 {len(unmapped)}곳: {', '.join(sorted(unmapped))}. 이름만 남겼습니다")
        if stats["rank_vs_insurer_count_diff"]:
            issue(con, main, "warn", "insurer_sum_mismatch",
                  f"인별업적순위 누적 건수와 보험사별 네 시트 총합계 건수가 다른 사람 {stats['rank_vs_insurer_count_diff']}명. 누적을 썼습니다")
        n_db, c_db, a_db = con.execute("SELECT COUNT(*), COALESCE(SUM(total_count),0), COALESCE(SUM(total_monthly_premium_amt),0) FROM agent_period_perf WHERE batch_id=?",
                                       (main,)).fetchone()
        failed = n_db != want["rows"] or c_db != want["count"] or abs(a_db - want["amt"]) > 0.5
        if failed:
            issue(con, main, "block", "total_mismatch", f"표 대조가 맞지 않습니다: 넣을 사람 {want['rows']}명, 표 {n_db}명")
        if side:
            finish_batch(con, side, sum(len(p["sheets"]) for p in ins["people"].values()), failed=failed)
        finish_batch(con, main, len(codes), failed=failed)
    except Exception:
        con.rollback()
        raise
    return {"ym": ym, "main_batch": main, "insurer_batch": ins_batch, "people": len(codes), "sums_match": not failed,
            "unmapped_insurers": sorted(unmapped), "perf_ids": perf_ids,
            **{k: v for k, v in stats.items()}}


# ------------------------------------------------------------------ 대조용 월 합계 계약 (연결 방식 가)
def month_sum_contracts(con, ym: str, ins: dict, agents: dict, batch_id: int) -> dict:
    """대조용 DB 에만 쓴다. 보험사별 파일의 한 칸(사람, 시트, 보험사)을 그 달 말일의 대표 계약 1건으로 계약 원장에 넣는다.
    건수는 policy_count, 월납보험료는 premium 과 premium_monthly 에 넣는다. 인정보험료는 파일에 없어 비운다.
    상품명(product_code_raw)과 상태 원문(status_raw)에 MONTH_SUM_LABEL 을 적고 증권번호는 'TRIALSUM-' 으로 시작한다.
    구분(product_category)과 업권은 매핑 시트 설정의 est_category, sector 다. 추정이다. month_sum 이 false 인 시트(자동차, 일반)는 넣지 않는다.
    마스터에 없는 보험사의 칸과 설계사가 없는 사람은 넣지 않고 센다. 돌려주는 것: {contracts, skipped_insurer, skipped_agent}."""
    m = load_map(INSURER)
    sheets = {cfg["key"]: (sname, cfg) for sname, cfg in m["file"]["sheets"].items()}
    ins_id = _insurer_ids(con, m)
    y, mo = int(ym[:4]), int(ym[4:])
    day = f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    now = db.now()
    out = Counter()
    for code in sorted(ins["people"]):
        p = ins["people"][code]
        aid = agents.get(code)
        k = 0
        for key, s in p["sheets"].items():
            sname, cfg = sheets[key]
            if cfg.get("month_sum") is False:
                continue
            for name, (n, amt) in s["insurers"].items():
                if not n and not amt:
                    continue
                if aid is None:
                    out["skipped_agent"] += 1
                    continue
                iid = ins_id(name)
                if iid is None:
                    out["skipped_insurer"] += 1
                    continue
                k += 1
                no = f"TRIALSUM-{ym}-{aid}-{k:03d}"
                org = {CONTRACT_ORG[f]: v for f, v in p["org"].items() if f in CONTRACT_ORG}
                cols = {"insurer_id": iid, "policy_no": no, "application_no": "A" + no, "product_code_raw": MONTH_SUM_LABEL,
                        "product_type_raw": sname, "product_category": cfg.get("est_category"), "sector": cfg.get("sector"),
                        "agent_id": aid, "application_date": day, "contract_date": day, "receipt_date": day, "perf_date": day, "perf_ym": ym,
                        "payment_mode": "monthly", "premium": amt, "premium_monthly": amt, "converted_premium": None, "policy_count": n,
                        "status": "inforce", "status_raw": MONTH_SUM_LABEL, "status_date": day, "source_batch_id": batch_id,
                        "last_batch_id": batch_id, "source_row_no": p["row_no"], "created_at": now, **org}
                con.execute(f"INSERT INTO contract({','.join(cols)}) VALUES({','.join('?' * len(cols))})", list(cols.values()))
                out["contracts"] += 1
    con.commit()
    return {"contracts": out["contracts"], "skipped_insurer": out["skipped_insurer"], "skipped_agent": out["skipped_agent"], "date": day}
