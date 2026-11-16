"""인별실적 파일(사람별 기간 실적 집계) 적재기.

순서: 머리글 검사 → 같은 파일 막기 → 사람 행 읽기 → agent_period_perf 와 보험사별 표에 넣기 → 건수와 합계 대조.
- 머리글은 두 줄이다. 1행 묶음, 2행 칸. 열 순서는 보지 않고 (묶음, 칸) 이름으로 찾는다. 매핑은 config/mapping/eternal_person_perf.yaml.
- 필요한 묶음이나 칸이 없거나, 모르는 묶음이 있거나, 묶음에 모르는 칸이 있으면 DB 에 아무것도 쓰지 않고 HeaderMismatch 로 막는다.
- 같은 파일(import_batch.file_hash)은 DuplicateFile 로 막는다. 같은 기간을 새 파일로 다시 넣으면 이전 배치는 superseded 가 된다.
- 사원명은 사원번호와 맞춰 보는 데만 쓰고 표에도 원본 행(raw_row)에도 남기지 않는다. 민감 열은 머리글만 보고 버린다.
- 사원번호와 사원명이 빈 마지막 합계 행은 사람으로 넣지 않고, 사람 행의 합과 견주어 다르면 차이 비율만 경고한다.
- 계약 단위 원장(contract)과 섞지 않는다. 업적 집계 규칙(성립한 달에 더하고 철회한 달에 뺀다)은 이 표를 읽지 않는다.
- 경고와 오류 메시지에는 사람 이름과 금액을 넣지 않는다. 사원번호, 보험사, 칸 이름, 건수, 비율만 넣는다.

읽기 도우미(period_rows, no_perf, compare_with_ledger)는 범위 안의 설계사 목록(agent_ids)을 받아서만 읽는다.
"""
from __future__ import annotations

import json
import pathlib
import re
from collections import Counter

import openpyxl

from app.loader import mapping
from app.loader.excel_loader import DuplicateFile, finish_batch, issue, register_batch, summary  # noqa: F401  (DuplicateFile 을 여기서도 꺼내 쓴다)
from app.loader.sheet import HeaderMismatch, is_sensitive
from app.loader.transforms import _blank, as_text, normalize_header, parse_money

DATA_TYPE = "person_perf"
BEGIN, END = "-- [person_perf:begin]", "-- [person_perf:end]"
PERSON_TEXT = ("position", "org_general", "org_hq", "org_branch", "org_direct_branch", "org_team")


def ensure(con) -> None:
    """오래된 DB 에 표가 없으면 schema.sql 의 [person_perf] 묶음만 돌린다."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_period_perf'").fetchone():
        from app.ops import schema as ops_schema

        ops_schema.run_block(con, BEGIN, END)


def guess_period(path):
    """파일 이름에서 기간을 읽는다. '202603-05_인별실적' 이나 '202603-202605' 꼴. 못 읽으면 None."""
    name = pathlib.Path(path).stem
    m = re.search(r"(20\d{2})(0[1-9]|1[0-2])\s*[-~]\s*(?:(20\d{2}))?(0[1-9]|1[0-2])", name)
    if not m:
        return None
    y1, m1, y2, m2 = m.group(1), m.group(2), m.group(3) or m.group(1), m.group(4)
    start, end = y1 + m1, y2 + m2
    return (start, end) if start <= end else None


def _squash(v) -> str:
    return re.sub(r"\s+", "", normalize_header(v))


# ------------------------------------------------------------------ 머리글
def read_layout(ws, m) -> dict:
    """머리글 두 줄을 읽어 칸 위치를 정한다. DB 에 쓰지 않는다.

    돌려주는 것: {person: {표준 이름: 열}, groups: {묶음 키: {표준 이름: 열}}, insurers: {보험사 원문: {표준 이름: 열}},
                 reasons: [막는 사유], warnings: [경고], dropped: [버린 머리글], labels: {열: '묶음/칸'}}"""
    rows = list(ws.iter_rows(min_row=1, max_row=m["file"].get("header_rows", 2), values_only=True))
    if len(rows) < 2:
        return {"reasons": ["머리글 두 줄을 찾지 못했습니다"], "warnings": [], "dropped": [], "person": {}, "groups": {}, "insurers": {}, "labels": {}}
    h1, h2 = rows[0], rows[1]
    width = max(len(h1), len(h2))
    drop_kw = m.get("drop_keywords", [])
    person_map = {normalize_header(k): v for k, v in m["person"].items()}
    cfg = m["groups"]
    ins_subs = {normalize_header(k): v for k, v in m["insurer_subs"].items()}
    ignore = {normalize_header(x) for x in m.get("ignore", [])}
    person, raw_groups, dropped, labels, dup_person, unknown = {}, {}, [], {}, [], []
    group = None
    for n in range(width):
        a = normalize_header(h1[n]) if n < len(h1) else ""
        b = normalize_header(h2[n]) if n < len(h2) else ""
        if a:
            group = a
        if (a and is_sensitive(a, drop_kw)) or (b and is_sensitive(b, drop_kw)) or (not a and not b and group and is_sensitive(group, drop_kw)):
            dropped.append(f"{group}/{b}" if b else (a or group))
            continue
        if a and not b:                    # 1행에만 있는 칸은 사람 칸이다
            if a in person_map:
                if person_map[a] in person:
                    dup_person.append(a)
                else:
                    person[person_map[a]] = n
                    labels[n] = a
            elif a in ignore:
                labels[n] = a
            else:
                unknown.append(a)
            group = None                   # 사람 칸 다음의 빈 칸은 묶음이 아니다
            continue
        if b and group:
            raw_groups.setdefault(group, {})
            if b in raw_groups[group]:
                raw_groups[group][b + "#dup"] = n
            else:
                raw_groups[group][b] = n
            labels[n] = f"{group}/{b}"
    reasons, warnings = [], []
    missing_person = [k for k, v in person_map.items() if v in m["person_required"] and v not in person]
    if missing_person:
        reasons.append(f"필수 열 없음: {', '.join(missing_person)}")
    if unknown:
        reasons.append(f"매핑에 없는 열 {len(unknown)}개: {', '.join(unknown)}")
    if dup_person:
        warnings.append(f"같은 이름의 사람 칸이 둘 있어 앞의 것을 썼습니다: {', '.join(sorted(set(dup_person)))}")
    groups, insurers, used = {}, {}, set()
    for key, g in cfg.items():
        hits = [name for name in raw_groups if re.search(g["match"], _squash(name))]
        if not hits:
            if g.get("required"):
                reasons.append(f"묶음 '{key}' 이 없습니다 ({g['match']})")
            continue
        if len(hits) > 1:
            reasons.append(f"묶음 '{key}' 에 맞는 머리글이 {len(hits)}개입니다: {', '.join(hits)}")
            continue
        name = hits[0]
        used.add(name)
        subs = {normalize_header(k): v for k, v in g["subs"].items()}
        have = raw_groups[name]
        miss = [s for s in subs if s not in have]
        extra = [s for s in have if s not in subs]
        if miss and g.get("required"):
            reasons.append(f"묶음 '{name}' 에 칸이 없습니다: {', '.join(miss)}")
        if extra:
            reasons.append(f"묶음 '{name}' 에 매핑에 없는 칸이 있습니다: {', '.join(extra)}")
        groups[key] = {subs[s]: have[s] for s in subs if s in have}
    for name, have in raw_groups.items():
        if name in used:
            continue
        if set(have) == set(ins_subs):
            insurers[name] = {ins_subs[s]: have[s] for s in have}
        else:
            reasons.append(f"모르는 묶음 '{name}' 이 있습니다. 칸: {', '.join(have)}")
    known = [normalize_header(x) for x in m.get("known_insurers", [])]
    new = [x for x in insurers if x not in known]
    gone = [x for x in known if x not in insurers]
    if new:
        warnings.append(f"매핑에 없던 보험사 묶음 {len(new)}개를 보험사로 읽었습니다: {', '.join(new)}")
    if gone:
        warnings.append(f"매핑에 있던 보험사 묶음 {len(gone)}개가 파일에 없습니다: {', '.join(gone)}")
    return {"person": person, "groups": groups, "insurers": insurers, "reasons": reasons, "warnings": warnings,
            "dropped": dropped, "labels": labels}


def _open(path, m):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = m["file"].get("sheet")
    if sheet not in wb.sheetnames:
        names = ", ".join(f"'{n}'" for n in wb.sheetnames)
        wb.close()
        raise HeaderMismatch([f"시트 '{sheet}' 가 없습니다. 있는 시트: {names}. 다른 시트는 읽지 않았습니다"], pathlib.Path(path).name)
    return wb, wb[sheet]


def preview(path, start_ym, end_ym) -> dict:
    """DB 에 쓰지 않고 머리글만 검사한다. 돌려주는 것: {ok, reasons, warnings, dropped_columns, insurers}."""
    m = mapping.load("eternal", DATA_TYPE, end_ym)
    try:
        wb, ws = _open(path, m)
    except HeaderMismatch as e:
        return {"ok": False, "reasons": e.reasons, "warnings": [], "dropped_columns": [], "insurers": 0}
    try:
        lay = read_layout(ws, m)
    finally:
        wb.close()
    return {"ok": not lay["reasons"], "reasons": lay["reasons"], "warnings": lay["warnings"], "dropped_columns": lay["dropped"],
            "insurers": len(lay["insurers"])}


# ------------------------------------------------------------------ 적재
def _num(v, is_count):
    """빈 칸과 '-' 는 0. 숫자가 아니면 ValueError. 건수가 정수가 아니면 ValueError."""
    if _blank(v):
        return 0
    n = parse_money(v)
    if is_count:
        if float(n) != int(n):
            raise ValueError("건수가 정수가 아닙니다")
        return int(n)
    return n


def _tier(rec) -> str:
    if rec["elite_50_count"] > 0:
        return "elite_50"
    if rec["active_10_count"] > 0:
        return "active_10"
    if rec["total_count"] > 0:
        return "some"
    return "none"


def _diff_pct(a, b):
    if b == 0:
        return 0.0 if a == 0 else float("inf")
    return (a - b) / abs(b) * 100


def load_person_perf(con, path, start_ym, end_ym, *, by="system", via="manual") -> dict:
    """인별실적 한 파일을 넣는다. start_ym 과 end_ym 은 파일이 다루는 기간이다(YYYYMM).
    머리글이 맞지 않으면 HeaderMismatch, 같은 파일이면 DuplicateFile. 돌려주는 것은 건수와 대조가 든 요약이다. 금액은 대조 결과만 담는다."""
    if not (re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", start_ym or "") and re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", end_ym or "") and start_ym <= end_ym):
        raise ValueError("기간은 YYYYMM 꼴이고 시작월이 끝월보다 늦을 수 없습니다")
    ensure(con)
    m = mapping.load("eternal", DATA_TYPE, end_ym)
    wb, ws = _open(path, m)
    try:
        lay = read_layout(ws, m)
        if lay["reasons"]:
            raise HeaderMismatch(lay["reasons"], pathlib.Path(path).name)
        batch_id = register_batch(con, path, DATA_TYPE, end_ym, via=via, by=by, mapping_version=m["mapping_version"])
        try:
            out = _load_rows(con, m, ws, lay, batch_id, start_ym, end_ym)
        except Exception:
            con.rollback()
            raise
    finally:
        wb.close()
    return out


def _load_rows(con, m, ws, lay, batch_id, start_ym, end_ym):
    for h in lay["dropped"]:
        issue(con, batch_id, "warn", "dropped_column", f"민감 정보 열 '{h}' 을 읽지 않고 버렸습니다")
    for w in lay["warnings"]:
        issue(con, batch_id, "warn", "header_note", w)
    person, groups, insurers, labels = lay["person"], lay["groups"], lay["insurers"], lay["labels"]
    count_fields = {f for g in m["groups"].values() for f in g["subs"].values() if f.endswith("_count")}
    agents = {r["internal_code"]: (r["agent_id"], r["name"]) for r in con.execute("SELECT agent_id, internal_code, name FROM agent")}
    master = {normalize_header(r["name"]): r["insurer_id"] for r in con.execute("SELECT insurer_id, name FROM insurer")}
    alias = {normalize_header(k): normalize_header(v) for k, v in (m.get("insurer_alias") or {}).items()}
    ins_ids = {name: master.get(alias.get(name, name)) for name in insurers}
    for name, iid in ins_ids.items():
        if iid is None:
            issue(con, batch_id, "warn", "unmapped_insurer", f"보험사 '{name}' 이 원수사 마스터에 없습니다. 이름만 남겼습니다")
    name_col = person.get("name")
    keep_cols = sorted(c for c in labels if c != name_col)

    def cell(row, col):
        return row[col] if col is not None and col < len(row) else None

    raw_buf, seen = [], {}
    stats = Counter()
    file_sum = {"total_count": 0, "total_monthly_premium_amt": 0}
    summary_sum = None
    unmapped, name_mismatch, ins_mismatch = [], 0, 0
    for r_idx, row in enumerate(ws.iter_rows(min_row=m["file"].get("header_rows", 2) + 1, values_only=True),
                                start=m["file"].get("header_rows", 2) + 1):
        if all(_blank(v) for v in row):
            continue
        raw_buf.append((batch_id, ws.title, r_idx, json.dumps({labels[c]: cell(row, c) for c in keep_cols}, ensure_ascii=False, default=str)))
        code = as_text(cell(row, person["employee_no"])).strip().upper()
        nm = as_text(cell(row, name_col)).strip()
        values, bad = {}, []
        for key, cols in groups.items():
            for field, col in cols.items():
                v = cell(row, col)
                if key == "active_small" and _blank(v):
                    values[field] = None
                    continue
                try:
                    values[field] = _num(v, field in count_fields)
                except (ValueError, TypeError):
                    values[field] = 0
                    bad.append(labels[col])
        if not code and not nm:                                   # 합계 행
            stats["summary_rows"] += 1
            if not bad:
                summary_sum = summary_sum or {k: 0 for k in file_sum}
                for k in file_sum:
                    summary_sum[k] += values.get(k) or 0
            continue
        stats["file_rows"] += 1
        if not code:
            issue(con, batch_id, "block", "bad_row", "사원번호가 비어 있습니다. 이 행은 넣지 않았습니다", r_idx)
            stats["skipped"] += 1
            continue
        if code in seen:
            issue(con, batch_id, "block", "duplicate_key", f"사원번호 {code} 가 {seen[code]}행과 겹칩니다. 이 행은 넣지 않았습니다", r_idx)
            stats["skipped"] += 1
            continue
        if bad:
            issue(con, batch_id, "block", "bad_row", f"숫자로 읽을 수 없는 칸이 있습니다: {', '.join(bad[:5])}. 이 행은 넣지 않았습니다", r_idx)
            stats["skipped"] += 1
            continue
        seen[code] = r_idx
        ag = agents.get(code)
        if ag is None:
            unmapped.append(code)
        elif nm and normalize_header(nm) != normalize_header(ag[1]):
            name_mismatch += 1
            issue(con, batch_id, "warn", "name_mismatch", f"사원번호 {code} 의 이름이 설계사 마스터와 다릅니다", r_idx)
        rec = {k: (as_text(cell(row, person.get(k))).strip() or None) if person.get(k) is not None else None for k in PERSON_TEXT}
        rec.update(values)
        for k in ("total_count", "total_monthly_premium_amt", "total_premium_per_policy_amt", "total_insurer_perf_amt", "total_global_perf_amt",
                  "active_10_count", "active_10_monthly_premium_amt", "elite_50_count", "elite_50_premium_amt", "single_count", "single_premium_amt",
                  "single_insurer_perf_amt", "auto_count", "auto_premium_amt", "auto_global_perf_amt", "general_count", "general_premium_amt",
                  "general_global_perf_amt"):
            rec.setdefault(k, 0)
        rec.setdefault("active_small_count", None)
        rec.setdefault("active_small_monthly_premium_amt", None)
        rec["perf_tier"] = _tier(rec)
        cols = ["batch_id", "employee_no", "agent_id", "start_ym", "end_ym", "row_no"] + list(rec)
        pid = con.execute(f"INSERT INTO agent_period_perf({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                          [batch_id, code, ag[0] if ag else None, start_ym, end_ym, r_idx] + list(rec.values())).lastrowid
        ins_n = 0
        ins_rows = []
        for name, icols in insurers.items():
            try:
                n = _num(cell(row, icols["policy_count"]), True)
                amt = _num(cell(row, icols["monthly_premium_amt"]), False)
            except (ValueError, TypeError):
                issue(con, batch_id, "warn", "bad_value", f"보험사 '{name}' 칸을 숫자로 읽을 수 없어 0 으로 두었습니다", r_idx)
                n, amt = 0, 0
            ins_n += n
            if n or amt:
                ins_rows.append((pid, name, ins_ids[name], n, amt))
        con.executemany("INSERT INTO agent_period_perf_insurer(perf_id, insurer_name, insurer_id, policy_count, monthly_premium_amt) VALUES(?,?,?,?,?)",
                        ins_rows)
        if ins_n != rec["total_count"]:
            ins_mismatch += 1
        for k in file_sum:
            file_sum[k] += rec[k]
        stats["loaded"] += 1
        stats["tier_" + rec["perf_tier"]] += 1

    con.executemany("INSERT INTO raw_row(batch_id, sheet_name, row_no, raw_json) VALUES(?,?,?,?)", raw_buf)
    if unmapped:
        issue(con, batch_id, "warn", "unmapped_agent",
              f"설계사 마스터에 없는 사원번호 {len(unmapped)}명. 실적은 넣고 설계사는 비워 두었습니다: {', '.join(unmapped[:20])}{' 외' if len(unmapped) > 20 else ''}")
    if ins_mismatch:
        issue(con, batch_id, "warn", "insurer_sum_mismatch", f"보험사별 건수의 합이 Total 건수와 다른 사람 {ins_mismatch}명")
    summary_match = None
    if stats["summary_rows"]:
        issue(con, batch_id, "warn", "summary_row", f"원본의 합계 행 {stats['summary_rows']}개를 사람으로 넣지 않았습니다. 원본 행에는 남겼습니다")
        if summary_sum is not None:
            summary_match = {k: abs(summary_sum[k] - file_sum[k]) <= 0.5 for k in file_sum}
            for k, ok in summary_match.items():
                if not ok:
                    issue(con, batch_id, "warn", "summary_mismatch",
                          f"원본 합계 행의 {k} 가 사람 행의 합과 {_diff_pct(summary_sum[k], file_sum[k]):+.2f}% 다릅니다")

    # ---------- 건수와 합계 대조 ----------
    n_db, c_db, a_db = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_count),0), COALESCE(SUM(total_monthly_premium_amt),0) FROM agent_period_perf WHERE batch_id=?",
        (batch_id,)).fetchone()
    failed = n_db != stats["loaded"] or c_db != file_sum["total_count"] or abs(a_db - file_sum["total_monthly_premium_amt"]) > 0.5
    if failed:
        issue(con, batch_id, "block", "total_mismatch", f"표 대조가 맞지 않습니다: 넣을 사람 {stats['loaded']}명, 표 {n_db}명")
    superseded = 0
    if not failed:
        old = [r[0] for r in con.execute(
            """SELECT DISTINCT p.batch_id FROM agent_period_perf p JOIN import_batch b ON b.batch_id=p.batch_id
               WHERE p.start_ym=? AND p.end_ym=? AND b.status='active' AND p.batch_id<>?""", (start_ym, end_ym, batch_id))]
        for b in old:
            con.execute("UPDATE import_batch SET status='superseded' WHERE batch_id=?", (b,))
        superseded = len(old)
    finish_batch(con, batch_id, stats["file_rows"], failed=failed)
    return summary(con, batch_id, start_ym=start_ym, end_ym=end_ym, file_rows=stats["file_rows"], loaded=stats["loaded"],
                   skipped=stats["skipped"], summary_rows=stats["summary_rows"], summary_matches=summary_match,
                   sums_match=not failed, matched_agents=stats["loaded"] - len(unmapped), unmapped_agents=len(unmapped),
                   name_mismatch=name_mismatch, insurer_sum_mismatch=ins_mismatch, insurers=len(insurers),
                   unmapped_insurers=sum(1 for v in ins_ids.values() if v is None),
                   tiers={k[5:]: v for k, v in stats.items() if k.startswith("tier_")}, dropped_columns=lay["dropped"], superseded=superseded)


# ------------------------------------------------------------------ 읽기 (범위 안의 설계사만)
def periods(con) -> list:
    """들어와 있는 기간. [(start_ym, end_ym)] 을 최근 순으로."""
    ensure(con)
    return [tuple(r) for r in con.execute("SELECT DISTINCT start_ym, end_ym FROM v_agent_period_perf ORDER BY end_ym DESC, start_ym DESC")]


def _ids(agent_ids):
    ids = sorted(set(agent_ids or ()))
    return ids, ",".join("?" * len(ids))


def period_rows(con, start_ym, end_ym, agent_ids) -> dict:
    """{agent_id: 줄}. 범위 안의 설계사 가운데 그 기간 파일에 있는 사람만. 보험사별 값은 줄의 insurers 에 {보험사 원문: (건수, 금액)}."""
    ensure(con)
    ids, q = _ids(agent_ids)
    if not ids:
        return {}
    out = {}
    for r in con.execute(f"SELECT * FROM v_agent_period_perf WHERE start_ym=? AND end_ym=? AND agent_id IN ({q})", [start_ym, end_ym] + ids):
        d = dict(r)
        d["insurers"] = {x["insurer_name"]: (x["policy_count"], x["monthly_premium_amt"]) for x in con.execute(
            "SELECT insurer_name, policy_count, monthly_premium_amt FROM agent_period_perf_insurer WHERE perf_id=? ORDER BY insurer_name", (d["perf_id"],))}
        out[d["agent_id"]] = d
    return out


def no_perf(con, start_ym, end_ym, agent_ids) -> dict:
    """무실적 명단. none 은 파일에서 Total 건수가 0 인 사람, absent 는 범위 안에 있는데 그 기간 파일에 없는 사람이다."""
    rows = period_rows(con, start_ym, end_ym, agent_ids)
    ids = set(agent_ids or ())
    return {"none": sorted(a for a, r in rows.items() if r["perf_tier"] == "none"), "absent": sorted(ids - set(rows))}


def compare_with_ledger(con, start_ym, end_ym, agent_ids) -> dict:
    """인별실적 파일 값과 계약 원장의 업적 집계를 사람마다 견준다. 계약 원장에 그 기간 계약이 없으면 ledger 가 False 다.

    짝은 가정이다. 파일의 Total 건수는 원장의 순건수(net_count), 월납보험료는 원장의 monthly_premium 과 견준다.
    원수사성적과 글로벌성적이 원장의 인정보험료와 같은 뜻인지는 확인 전이라 견주지 않고 나란히 보인다."""
    from app.engine import performance
    from app.periods import month_bounds

    rows = period_rows(con, start_ym, end_ym, agent_ids)
    d_from, _ = month_bounds(start_ym)
    _, d_to = month_bounds(end_ym)
    has_ledger = con.execute("SELECT 1 FROM contract WHERE contract_date>=? AND contract_date<? LIMIT 1", (d_from, d_to)).fetchone() is not None
    led = performance.tally(con, start_ym, end_ym, agent_ids=agent_ids) if has_ledger else {}
    out = []
    for aid in sorted(set(rows) | set(led)):
        f, lg = rows.get(aid), led.get(aid) or performance.blank()
        line = {"agent_id": aid, "employee_no": f["employee_no"] if f else None, "in_file": f is not None,
                "file_count": f["total_count"] if f else None, "ledger_net_count": lg["net_count"],
                "file_monthly_premium_amt": f["total_monthly_premium_amt"] if f else None, "ledger_monthly_premium": lg["monthly_premium"],
                "file_insurer_perf_amt": f["total_insurer_perf_amt"] if f else None, "file_global_perf_amt": f["total_global_perf_amt"] if f else None,
                "ledger_converted_premium": lg["converted_premium"]}
        line["count_same"] = f is not None and line["file_count"] == line["ledger_net_count"]
        line["premium_same"] = f is not None and abs((line["file_monthly_premium_amt"] or 0) - line["ledger_monthly_premium"]) <= 0.5
        out.append(line)
    return {"ledger": has_ledger, "rows": out}
