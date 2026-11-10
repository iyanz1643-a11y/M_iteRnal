"""시상 공문 PDF → 시상 규칙 초안.

흐름: parse(pdf) → save_draft() 로 incentive_source_doc(extracted) + incentive_rule(draft) 저장
      → 담당자가 화면에서 원문과 대조 → approve() 로 active 전환.
자동 추출은 초안일 뿐이며, 승인 전에는 계산에 쓰이지 않는다.
"""
import hashlib
import json
import pathlib
import re

import pdfplumber

from app import db

PERIODS = {"월": "month", "분기": "quarter", "반기": "half", "연": "year", "년": "year"}
METRICS = [("인정보험료", "converted_premium"), ("환산", "converted_premium"), ("건수", "count"), ("보험료", "monthly_premium")]


def _amount(text):
    """'50만원' → 500000, '1,000,000원' → 1000000, '3건' → 3, 빈 값/'-' → None"""
    t = (text or "").replace(",", "").replace(" ", "").strip()
    if t in ("", "-", "~"):
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)(억|천만|백만|만)?(원|건)?$", t)
    if not m:
        raise ValueError(f"금액 해석 불가: {text!r}")
    mult = {"억": 100_000_000, "천만": 10_000_000, "백만": 1_000_000, "만": 10_000, None: 1}[m.group(2)]
    v = float(m.group(1)) * mult
    return int(v) if v.is_integer() else v


def _reward(text):
    t = (text or "").replace(" ", "").strip()
    if t.endswith("%"):
        return "rate", round(float(t[:-1]) / 100, 6)
    return "fixed", _amount(t)


def _ym(text):
    return [f"{y}{int(m):02d}" for y, m in re.findall(r"(\d{4})년\s*(\d{1,2})월", text)]


def _elements(path):
    out = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.find_tables()
            boxes = [t.bbox for t in tables]
            for line in page.extract_text_lines():
                mid = (line["top"] + line["bottom"]) / 2
                if any(b[1] <= mid <= b[3] for b in boxes):
                    continue
                out.append((page.page_number, line["top"], "text", line["text"].strip()))
            for t in tables:
                out.append((page.page_number, t.bbox[1], "table", t.extract()))
    return [(kind, val) for _, _, kind, val in sorted(out, key=lambda e: (e[0], e[1]))]


def parse(path, category_names) -> dict:
    """PDF 한 건을 해석해 규칙 초안(딕셔너리)을 돌려준다. DB에는 쓰지 않는다."""
    doc = dict(title=None, sender=None, effective_from=None, effective_to=None, sections=[], warnings=[])
    cur = None
    for kind, val in _elements(path):
        if kind == "text":
            line = val
            if doc["title"] is None and line:
                doc["title"] = line
                continue
            if line.startswith("발신"):
                doc["sender"] = line.split(":", 1)[-1].strip()
            elif line.startswith("적용 기간") or line.startswith("대상 월"):
                yms = _ym(line)
                if yms:
                    doc["effective_from"], doc["effective_to"] = yms[0], yms[-1]
                else:
                    doc["warnings"].append(f"기간 해석 불가: {line}")
            m = re.search(r"\[(기본시상|추가시상)\]\s*(.*)", line)
            if m:
                cur = dict(kind="base" if m.group(1) == "기본시상" else "extra", name=m.group(2).strip() or m.group(1),
                           categories=[], metric=None, period_type="month", tiers=[], conditions=[], clawback_months=0, warnings=[])
                doc["sections"].append(cur)
                continue
            if cur is None:
                continue
            if line.startswith("대상 구분"):
                names = [x.strip() for x in re.split(r"[,·/]", line.split(":", 1)[-1]) if x.strip()]
                for nm in names:
                    if nm in category_names:
                        cur["categories"].append(nm)
                    else:
                        cur["warnings"].append(f"알 수 없는 구분: {nm}")
            elif line.startswith("평가 지표"):
                body = line.split(":", 1)[-1]
                cur["metric"] = next((code for word, code in METRICS if word in body), None)
                pm = re.search(r"\((월|분기|반기|연|년)\)", body)
                if pm:
                    cur["period_type"] = PERIODS[pm.group(1)]
                if cur["metric"] is None:
                    cur["warnings"].append(f"지표 해석 불가: {body.strip()}")
            elif line.startswith("지급 조건"):
                body = line.split(":", 1)[-1]
                for k, typ in (("13", "persistency_13"), ("25", "persistency_25")):
                    pm = re.search(rf"{k}회차\s*유지율\s*(\d+(?:\.\d+)?)%\s*이상", body)
                    if pm:
                        cur["conditions"].append((typ, ">=", str(float(pm.group(1)) / 100)))
                pm = re.search(r"(\d+)건\s*이상", body)
                if pm:
                    cur["conditions"].append(("min_count", ">=", pm.group(1)))
                pm = re.search(r"위촉\s*(\d+)개월\s*이상", body)
                if pm:
                    cur["conditions"].append(("tenure_months", ">=", pm.group(1)))
            elif line.startswith("환수"):
                pm = re.search(r"(\d+)개월\s*이내", line)
                if pm:
                    cur["clawback_months"] = int(pm.group(1))
        else:  # table
            if cur is None:
                doc["warnings"].append("시상 제목([기본시상]/[추가시상]) 없이 표가 나옴 → 무시")
                continue
            rows = [[(c or "").strip() for c in r] for r in val if r]
            body = [r for r in rows if not any("구간" in c for c in r)]
            for r in body:
                try:
                    mn, mx = _amount(r[0]), _amount(r[1]) if len(r) > 2 else None
                    rtype, rval = _reward(r[-1])
                    cur["tiers"].append(dict(min=mn, max=mx, reward_type=rtype, reward_value=rval))
                except (ValueError, IndexError) as e:
                    cur["warnings"].append(f"표 행 해석 실패 {r}: {e}")
    for s in doc["sections"]:
        if not s["tiers"]:
            s["warnings"].append("구간 표를 찾지 못함")
        if not s["categories"]:
            s["warnings"].append("대상 구분 미기재 → 전 구분 대상으로 처리됨")
        s["warnings"].append("구간 경계는 '이상 ~ 미만' 으로 해석함. 원문 표기 확인 필요")
    if not doc["effective_from"]:
        doc["warnings"].append("적용 기간을 찾지 못함")
    if not doc["sections"]:
        doc["warnings"].append("시상 구간 섹션을 찾지 못함 → 수기 입력 필요")
    return doc


def save_draft(con, path, by="system"):
    """PDF 를 해석해 공문과 규칙 초안(draft)을 저장하고 doc_id 를 돌려준다."""
    path = pathlib.Path(path)
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    if con.execute("SELECT 1 FROM incentive_source_doc WHERE file_hash=?", (h,)).fetchone():
        raise ValueError("이미 등록된 공문 PDF 입니다")
    cats = {r["name"]: r["code"] for r in con.execute("SELECT name, code FROM product_category")}
    parsed = parse(path, set(cats))
    ins = con.execute("SELECT * FROM insurer WHERE name=?", (parsed["sender"],)).fetchone() if parsed["sender"] else None
    ef = parsed["effective_from"] or "000000"
    cur = con.execute(
        """INSERT INTO incentive_source_doc(insurer_id, period_ym, title, file_path, file_hash, received_at, extraction_method, extraction_status, note)
           VALUES(?,?,?,?,?,?,'text',?,?)""",
        (ins["insurer_id"] if ins else None, ef, parsed["title"], str(path), h, db.now(),
         "extracted" if parsed["sections"] else "pending", json.dumps(parsed, ensure_ascii=False)))
    doc_id = cur.lastrowid
    for s in parsed["sections"]:
        cur = con.execute(
            """INSERT INTO incentive_rule(name, insurer_id, source_doc_id, status, rule_kind, period_type, metric, scope, scope_value,
               clawback_months, effective_from, effective_to) VALUES(?,?,?,'draft',?,?,?,?,?,?,?,?)""",
            (s["name"], ins["insurer_id"] if ins else None, doc_id, s["kind"], s["period_type"], s["metric"] or "converted_premium",
             "insurer" if ins else "all", ins["code"] if ins else None, s["clawback_months"], ef, parsed["effective_to"]))
        rid = cur.lastrowid
        for nm in s["categories"]:
            con.execute("INSERT OR IGNORE INTO incentive_rule_category(rule_id, product_category) VALUES(?,?)", (rid, cats[nm]))
        for i, t in enumerate(s["tiers"], start=1):
            con.execute("INSERT INTO incentive_tier(rule_id, tier_no, min_value, max_value, reward_type, reward_value) VALUES(?,?,?,?,?,?)",
                        (rid, i, t["min"] or 0, t["max"], t["reward_type"], t["reward_value"]))
        for typ, op, v in s["conditions"]:
            con.execute("INSERT INTO incentive_condition(rule_id, condition_type, operator, value) VALUES(?,?,?,?)", (rid, typ, op, v))
    con.commit()
    return doc_id, parsed


def approve(con, doc_id, by):
    """담당자 승인: 해당 공문의 규칙 초안을 active 로 바꾼다."""
    con.execute("UPDATE incentive_rule SET status='active', approved_by=?, approved_at=? WHERE source_doc_id=? AND status='draft'", (by, db.now(), doc_id))
    con.execute("UPDATE incentive_source_doc SET extraction_status='reviewed', reviewed_by=?, reviewed_at=? WHERE doc_id=?", (by, db.now(), doc_id))
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), by, "incentive_approve", f"doc:{doc_id}"))
    con.commit()


def reject(con, doc_id, by):
    con.execute("UPDATE incentive_rule SET status='retired' WHERE source_doc_id=? AND status='draft'", (doc_id,))
    con.execute("UPDATE incentive_source_doc SET extraction_status='rejected', reviewed_by=?, reviewed_at=? WHERE doc_id=?", (by, db.now(), doc_id))
    con.commit()
