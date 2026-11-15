"""프로모션: 게시, 구간 입력, 달성 현황 계산, 비교, 지난 프로모션.

프로모션 하나 = 기간 창(하나 이상) + 구간표 + 결합 규칙 + 보상 + 조건.
  - rate      정률 시책. 대상 계약의 보험료에 지급률을 곱한 예상액
  - weekly    주차 시상. 창 하나의 실적을 구간표에 대입
  - linked    연계 시상. 창마다 달성해야 하고 낮은 쪽 구간을 적용 (combine_rule = all)
  - monthly / membership / travel / activity  누적 실적을 구간표에 대입
달성 현황은 저장하지 않고 계약 원장에서 그때그때 계산한다. 모두 예상이며 확정은 보험사 정산을 따른다.
비교(compare)는 진행 중 프로모션을 실적 10만 원당 예상 보상, 구간, 조건으로 나란히 놓는다. 지난 프로모션(past)은 끝난
프로모션의 사람별 최종 결과를 progress() 로 낸다. 메인 관리자가 confirm_results() 로 확정하면 그 뒤에는 확정한 값을 낸다.

실적 기준 (D-08, 열린 결정이라 제안대로 둔 가정).
- 프로모션은 보험사 규칙대로 기간 안 계약 가운데 성립한 것을 세고 철회, 청약, 반송은 세지 않는다. 지금 상태로 센다.
  업적 집계(성립한 달에 더하고 철회한 달에 뺀다)와 다를 수 있다. 화면은 progress() 의 basis 글을 그대로 보여 준다.
- 기준일은 프로모션마다 고른다. 새로 만들 때의 기본값은 영수일자(DEFAULT_DATE_BASIS)다. 영수일자가 빈 계약은 계약일자로 센다.
  실제 계약현황의 '영수일자' 열은 contract.receipt_date 에 실린다 (config/mapping/eternal_contract.yaml). 샘플 계약에는 영수일자가 없다.

원안과 배포안 (D-21, 열린 결정이라 제안대로 둔 가정). 지금 금액(reward_value, rate)은 배포안이다. 원안은 promotion_tier.original_value 와
promotion_rate.original_rate 에 메인 관리자가 넣은 것만 있고, 없으면 원문 확인이다. 원안을 보여 줄지는 화면이 역할로 정한다.
"""
import calendar
import json
import sqlite3
from datetime import date

from app import db
from app.engine import classify

KIND_KO = {"rate": "정률 시책", "weekly": "주차 시상", "linked": "연계 시상", "monthly": "월 구간 시상",
           "travel": "여행 시상", "activity": "가동 시상", "membership": "멤버십"}
METRIC_KO = {"premium_monthly": "보험료", "converted_premium": "인정보험료", "policy_count": "건수"}
METRIC_UNIT = {"premium_monthly": "원", "converted_premium": "원", "policy_count": "건"}
BASIS_KO = {"receipt_date": "영수일자", "contract_date": "계약일자", "application_date": "청약일자"}
DEFAULT_DATE_BASIS = "receipt_date"                          # D-08 제안. 새 프로모션의 기본값
BASIS_FALLBACK = {"receipt_date": "contract_date"}           # 그 날짜가 빈 계약은 이 날짜로 센다
COMBINE_KO = {"single": "창 하나", "all": "창마다 달성", "sum": "창을 합산"}
REWARD_KO = {"fixed": "정액", "rate": "실적의 비율", "travel": "여행", "goods": "물품"}
TIMING_KO = {"next_month": "익월", "inst13": "13회차", "next_month+inst13": "익월과 13회차", "year1": "1차년", "year2": "2차년",
             "mixed": "혼합", "unspecified": "표기 없음"}
NO_TIMING = "지급 시점 표기 없음"
COUNTED = ("inforce", "lapsed", "cancelled", "matured")       # 성립한 계약. 청약, 반송, 철회는 세지 않는다
FROM_LINE_KIND = {"rate_promo": "rate", "activity": "activity", "consecutive": "linked", "tier": "monthly"}


def ref_date(ym: str) -> date:
    """기준일. 기준 월이 이번 달이면 오늘, 지난 달이면 그 달의 말일이다."""
    today = date.today()
    if today.strftime("%Y%m") == ym:
        return today
    y, m = int(ym[:4]), int(ym[4:])
    return date(y, m, calendar.monthrange(y, m)[1])


def timing_ko(code) -> str | None:
    """지급 시점 코드의 한국어. 비었거나 표기 없음(unspecified)이면 None 이다. 모르는 코드는 원문 그대로 둔다."""
    if not code or code == "unspecified":
        return None
    return TIMING_KO.get(code, code)


def timing_text(code) -> str:
    """'익월 지급' 처럼 붙여 쓰는 글. 표기가 없으면 '지급 시점 표기 없음' 이다."""
    ko = timing_ko(code)
    return f"{ko} 지급" if ko else NO_TIMING


def basis_note(p) -> str:
    """달성 현황의 실적 기준 설명 (D-08 가정). 화면이 그대로 보여 준다."""
    b = p["date_basis"]
    head = f"기준: {BASIS_KO[b]}"
    if b in BASIS_FALLBACK:
        head += f", {BASIS_KO[b]}가 없는 계약은 {BASIS_KO[BASIS_FALLBACK[b]]}"
    return f"{head}. 기간 안 성립 계약만 세고 철회는 뺍니다. 보험사 규칙대로 세어 업적 집계와 다를 수 있습니다."


def _date_expr(basis: str) -> str:
    if basis not in BASIS_KO:
        raise ValueError(basis)
    if basis in BASIS_FALLBACK:
        return f"COALESCE(c.{basis}, c.{BASIS_FALLBACK[basis]})"
    return f"c.{basis}"


# ---------------------------------------------------------------- 등록과 게시
def _normalize(kind, windows, tiers, combine_rule, metric, date_basis, categories):
    """등록과 고치기가 함께 쓰는 검사."""
    if kind not in KIND_KO or metric not in METRIC_KO or date_basis not in BASIS_KO:
        raise ValueError("유형, 지표, 기준일 값이 맞지 않습니다.")
    windows = [(l, f, t) for l, f, t in windows if f and t]
    if not windows:
        raise ValueError("기간 창이 하나 이상 있어야 합니다.")
    for l, f, t in windows:
        if date.fromisoformat(f) > date.fromisoformat(t):
            raise ValueError(f"기간 창 '{l}' 의 시작일이 종료일보다 늦습니다.")
    combine_rule = combine_rule or ("all" if kind == "linked" else ("sum" if len(windows) > 1 else "single"))
    if combine_rule not in COMBINE_KO or (combine_rule == "single" and len(windows) > 1):
        raise ValueError("창이 둘 이상이면 결합 규칙을 정해야 합니다.")
    if len(windows) == 1:
        combine_rule = "single"
    tiers = sorted((float(m), k, (float(v) if v not in (None, "") else None), (lab or None)) for m, k, v, lab in tiers)
    if kind != "rate" and not tiers:
        raise ValueError("구간표가 비어 있습니다.")
    if any(k not in REWARD_KO for _, k, _, _ in tiers) or len({m for m, *_ in tiers}) != len(tiers):
        raise ValueError("구간의 보상 종류가 맞지 않거나 기준값이 겹칩니다.")
    if isinstance(categories, (list, tuple, set)):
        categories = ",".join(sorted(categories)) or None
    return windows, tiers, combine_rule, (categories or None)


def _write_children(con, pid, windows, tiers):
    for i, (l, f, t) in enumerate(windows, start=1):
        con.execute("INSERT INTO promotion_window(promo_id, seq, label, date_from, date_to) VALUES(?,?,?,?,?)", (pid, i, l or f"{i}구간", f, t))
    for i, (m, k, v, lab) in enumerate(tiers, start=1):
        con.execute("INSERT INTO promotion_tier(promo_id, tier_no, min_value, reward_kind, reward_value, reward_label) VALUES(?,?,?,?,?,?)", (pid, i, m, k, v, lab))


def create(con, *, period_ym, name, kind, windows, tiers=(), rates=(), by, recipient_level="fc", insurer_id=None, insurer_name=None,
           sector=None, categories=None, target_note=None, metric="premium_monthly", date_basis=DEFAULT_DATE_BASIS, per_contract_cap=None,
           combine_rule=None, pay_timing=None, caution=None, source_line_id=None, publish=False) -> int:
    """windows: [(label, from, to)], tiers: [(min_value, reward_kind, reward_value, reward_label)], rates: [(option, basis, rate, timing)]"""
    windows, tiers, combine_rule, categories = _normalize(kind, windows, tiers, combine_rule, metric, date_basis, categories)
    cur = con.execute(
        """INSERT INTO promotion(period_ym, name, kind, recipient_level, insurer_id, insurer_name, sector, categories, target_note, metric, date_basis,
                                 per_contract_cap, combine_rule, pay_timing, caution, source_line_id, status, created_by, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)""",
        (period_ym, name.strip(), kind, recipient_level, insurer_id, insurer_name, sector or None, categories, target_note, metric, date_basis,
         per_contract_cap or None, combine_rule, pay_timing, caution, source_line_id, by, db.now()))
    pid = cur.lastrowid
    _write_children(con, pid, windows, tiers)
    for i, (opt, basis, rate, timing) in enumerate(rates, start=1):
        con.execute("INSERT INTO promotion_rate(promo_id, seq, option_label, basis, rate, pay_timing) VALUES(?,?,?,?,?,?)", (pid, i, opt, basis, rate, timing))
    con.commit()
    if publish:
        set_status(con, pid, "published", by)
    return pid


def _snapshot(p):
    return {"name": p["name"], "kind": p["kind"], "insurer_name": p["insurer_name"], "sector": p["sector"], "categories": p["categories"],
            "metric": p["metric"], "date_basis": p["date_basis"], "per_contract_cap": p["per_contract_cap"], "combine_rule": p["combine_rule"],
            "pay_timing": p["pay_timing"], "target_note": p["target_note"], "caution": p["caution"],
            "windows": [(w["label"], w["date_from"], w["date_to"]) for w in p["windows"]],
            "tiers": [(t["min_value"], t["reward_kind"], t["reward_value"], t["reward_label"]) for t in p["tiers"]]}


def update(con, promo_id, *, by, name, windows, tiers=(), kind=None, insurer_id=None, insurer_name=None, sector=None, categories=None, target_note=None,
           metric="premium_monthly", date_basis=DEFAULT_DATE_BASIS, per_contract_cap=None, combine_rule=None, pay_timing=None, caution=None) -> dict:
    """프로모션을 고친다. 게시 중이어도 고칠 수 있고, 고치기 전과 뒤를 변경 기록에 남긴다.
    정률 시책은 유형과 지급률을 바꾸지 않는다. 지급률은 시상안 원장에서 온 것이기 때문이다."""
    before = _load(con, promo_id)
    if not before:
        raise ValueError("없는 프로모션입니다.")
    if not (name or "").strip():
        raise ValueError("이름을 넣어야 합니다.")
    kind = "rate" if before["kind"] == "rate" else (kind or before["kind"])
    if kind == "rate":
        tiers = ()
    windows, tiers, combine_rule, categories = _normalize(kind, windows, tiers, combine_rule, metric, date_basis, categories)
    con.execute("""UPDATE promotion SET name=?, kind=?, insurer_id=?, insurer_name=?, sector=?, categories=?, target_note=?, metric=?, date_basis=?,
                   per_contract_cap=?, combine_rule=?, pay_timing=?, caution=? WHERE promo_id=?""",
                (name.strip(), kind, insurer_id, insurer_name, sector or None, categories, target_note, metric, date_basis, per_contract_cap or None,
                 combine_rule, pay_timing, caution, promo_id))
    kept = {float(t["min_value"]): t["original_value"] for t in before["tiers"] if t.get("original_value") is not None}
    con.execute("DELETE FROM promotion_window WHERE promo_id=?", (promo_id,))
    con.execute("DELETE FROM promotion_tier WHERE promo_id=?", (promo_id,))
    _write_children(con, promo_id, windows, tiers)
    for m, v in kept.items():                                    # 원안(D-21)은 문턱이 같은 구간에 그대로 둔다. 문턱이 바뀐 구간은 원문 확인이 된다
        con.execute("UPDATE promotion_tier SET original_value=? WHERE promo_id=? AND min_value=?", (v, promo_id, m))
    after = _load(con, promo_id)
    b, a = _snapshot(before), _snapshot(after)
    changed = sorted(k for k in a if json.dumps(a[k], ensure_ascii=False, default=str) != json.dumps(b[k], ensure_ascii=False, default=str))
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), by, "promotion_update", f"{promo_id}:{','.join(changed) or '바뀐 것 없음'}",
                 json.dumps({k: b[k] for k in changed}, ensure_ascii=False, default=str), json.dumps({k: a[k] for k in changed}, ensure_ascii=False, default=str)))
    con.commit()
    return {"promo_id": promo_id, "changed": changed}


def set_status(con, promo_id, status, by):
    if status not in ("draft", "published", "withdrawn"):
        raise ValueError(status)
    con.execute("UPDATE promotion SET status=?, published_by=CASE WHEN ?='published' THEN ? ELSE published_by END, "
                "published_at=CASE WHEN ?='published' THEN ? ELSE published_at END WHERE promo_id=?", (status, status, by, status, db.now(), promo_id))
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), by, f"promotion_{status}", str(promo_id)))
    con.commit()


def delete_draft(con, promo_id):
    row = con.execute("SELECT status FROM promotion WHERE promo_id=?", (promo_id,)).fetchone()
    if not row or row["status"] == "published":
        raise ValueError("게시 중인 프로모션은 지울 수 없습니다. 먼저 회수하세요.")
    for t in ("promotion_window", "promotion_tier", "promotion_rate", "promotion"):
        con.execute(f"DELETE FROM {t} WHERE promo_id=?", (promo_id,))
    con.commit()


def publish_from_line(con, line_id, by, categories=None, sector="auto") -> int:
    """자동 해석된 정률 시책 줄을 승인하고 프로모션으로 게시한다. 기간이 날짜로 읽힌 줄만 된다."""
    ln = con.execute("""SELECT l.*, d.period_ym FROM incentive_plan_line l JOIN incentive_plan_doc d ON d.plan_doc_id=l.plan_doc_id WHERE l.line_id=?""", (line_id,)).fetchone()
    if not ln:
        raise ValueError("없는 줄입니다.")
    if ln["line_kind"] != "rate_promo" or ln["parse_status"] != "auto" or not ln["period_from"]:
        raise ValueError("자동 해석된 정률 시책만 바로 게시할 수 있습니다. 나머지는 구간과 조건을 입력하세요.")
    if con.execute("SELECT 1 FROM promotion WHERE source_line_id=? AND status!='withdrawn'", (line_id,)).fetchone():
        raise ValueError("이미 프로모션으로 만든 줄입니다.")
    comps = con.execute("SELECT option_label, basis, rate, pay_timing FROM incentive_plan_component WHERE line_id=? ORDER BY seq", (line_id,)).fetchall()
    con.execute("UPDATE incentive_plan_line SET review_status='approved', reviewed_by=?, reviewed_at=? WHERE line_id=?", (by, db.now(), line_id))
    return create(con, period_ym=ln["period_ym"], name=ln["category_raw"] or "시책", kind="rate", recipient_level=ln["recipient_level"],
                  windows=[("시책 기간", ln["period_from"], ln["period_to"])],
                  rates=[(c["option_label"], c["basis"], c["rate"], c["pay_timing"]) for c in comps],
                  insurer_id=ln["insurer_id"], insurer_name=ln["insurer_name"], sector=(ln["sector"] if sector == "auto" else sector),
                  categories=categories, target_note=ln["criteria_raw"], pay_timing=ln["pay_timing"],
                  caution="원안과 다르면 원안을 따릅니다.", source_line_id=line_id, by=by, publish=True)


# ---------------------------------------------------------------- 조회
def _load(con, promo_id) -> dict | None:
    p = con.execute("SELECT * FROM promotion WHERE promo_id=?", (promo_id,)).fetchone()
    if not p:
        return None
    p = dict(p)
    p["windows"] = [dict(r) for r in con.execute("SELECT * FROM promotion_window WHERE promo_id=? ORDER BY seq", (promo_id,))]
    p["tiers"] = [dict(r) for r in con.execute("SELECT * FROM promotion_tier WHERE promo_id=? ORDER BY tier_no", (promo_id,))]
    p["rates"] = [dict(r) for r in con.execute("SELECT * FROM promotion_rate WHERE promo_id=? ORDER BY seq", (promo_id,))]
    p["date_from"], p["date_to"] = p["windows"][0]["date_from"], p["windows"][-1]["date_to"]
    p["kind_ko"], p["metric_ko"], p["unit"] = KIND_KO[p["kind"]], METRIC_KO[p["metric"]], METRIC_UNIT[p["metric"]]
    p["timing_ko"] = timing_ko(p["pay_timing"])
    p["category_list"] = p["categories"].split(",") if p["categories"] else []
    return p


def listing(con, ym, role, include_unpublished=False) -> list:
    """그 달에 걸친 프로모션. 설계사와 관리자에게는 게시된 설계사 몫만 보인다."""
    y, m = int(ym[:4]), int(ym[4:])
    first, last = f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
    sql = """SELECT DISTINCT p.promo_id FROM promotion p JOIN promotion_window w ON w.promo_id=p.promo_id
             WHERE (p.period_ym=? OR (w.date_from<=? AND w.date_to>=?))"""
    params = [ym, last, first]
    if role != "main":
        sql += " AND p.status='published' AND p.recipient_level='fc'"
    elif not include_unpublished:
        sql += " AND p.status='published'"
    return [_load(con, r["promo_id"]) for r in con.execute(sql + " ORDER BY p.promo_id", params)]


def headline(p) -> dict:
    """카드에 크게 쓸 한 가지."""
    if p["kind"] == "rate":
        plain = [r for r in p["rates"] if not r["option_label"]]
        if len(p["rates"]) == 1 and plain:
            return {"big": f"{plain[0]['rate'] * 100:g}%", "small": "지급률"}
        if p["rates"] and len(plain) == len(p["rates"]):
            return {"big": " + ".join(f"{r['rate'] * 100:g}%" for r in plain), "small": " + ".join(timing_ko(r["pay_timing"]) or "지급" for r in plain)}
        return {"big": None, "small": "조건별 지급률. 원문 확인"}
    top = p["tiers"][-1] if p["tiers"] else None
    if not top:
        return {"big": None, "small": "구간표 없음"}
    return {"big": reward_text(top), "small": f"최고 구간 · {fmt_value(top['min_value'], p)} 이상"}


def fmt_value(v, p) -> str:
    return f"{v:g}건" if p["metric"] == "policy_count" else f"{int(v):,}원"


def reward_text(t, value=None) -> str:
    if t["reward_kind"] == "fixed":
        return f"{int(t['reward_value'] or 0):,}원"
    if t["reward_kind"] == "rate":
        return f"{(t['reward_value'] or 0) * 100:g}%"
    return t["reward_label"] or REWARD_KO[t["reward_kind"]]


def window_state(w, ref: date) -> dict:
    f, t = date.fromisoformat(w["date_from"]), date.fromisoformat(w["date_to"])
    if ref < f:
        return {"state": "upcoming", "days_left": (t - ref).days}
    if ref > t:
        return {"state": "ended", "days_left": 0}
    return {"state": "active", "days_left": (t - ref).days}


def due_label(p, ref: date) -> tuple:
    f, t = date.fromisoformat(p["date_from"]), date.fromisoformat(p["date_to"])
    if ref < f:
        return f"{f.month}월 {f.day}일 시작", ""
    if ref > t:
        return f"{t.month}월 {t.day}일 종료", ""
    left = (t - ref).days
    return ("오늘 마감" if left == 0 else f"D-{left}"), ("bad" if left <= 3 else "blue")


# ---------------------------------------------------------------- 달성 현황
def _values(con, p, agent_ids) -> dict:
    """{agent_id: [창마다의 실적]}"""
    out = {a: [0.0] * len(p["windows"]) for a in agent_ids}
    if not agent_ids:
        return out
    col = p["metric"]
    expr = f"MIN(c.{col}, ?)" if p["per_contract_cap"] and col != "policy_count" else f"c.{col}"
    q = ",".join("?" * len(agent_ids))
    for i, w in enumerate(p["windows"]):
        sql = f"""SELECT c.agent_id, SUM({expr}) v FROM contract c {classify.JOINS}
                  WHERE c.agent_id IN ({q}) AND c.status IN ({','.join('?' * len(COUNTED))}) AND {_date_expr(p['date_basis'])} BETWEEN ? AND ?"""
        params = ([p["per_contract_cap"]] if "MIN(" in expr else []) + list(agent_ids) + list(COUNTED) + [w["date_from"], w["date_to"]]
        if p["insurer_id"]:
            sql += " AND c.insurer_id=?"
            params.append(p["insurer_id"])
        if p["sector"]:
            sql += f" AND {classify.SECTOR}=?"
            params.append(p["sector"])
        if p["category_list"]:
            sql += f" AND {classify.CATEGORY} IN ({','.join('?' * len(p['category_list']))})"
            params += p["category_list"]
        for r in con.execute(sql + " GROUP BY c.agent_id", params):
            out[r["agent_id"]][i] = float(r["v"] or 0)
    return out


def _tier_of(tiers, value):
    hit = None
    for t in tiers:
        if value >= t["min_value"]:
            hit = t
    return hit


def progress(con, p, agent_ids, ref: date) -> dict:
    """{agent_id: 달성 현황}. 금액과 구간은 모두 예상이다. basis 는 실적 기준 설명 글이다 (D-08, basis_note())."""
    vals = _values(con, p, list(agent_ids))
    states = [window_state(w, ref) for w in p["windows"]]
    ended_all = all(s["state"] == "ended" for s in states)
    basis = basis_note(p)
    out = {}
    for a, per_window in vals.items():
        wins = [{**w, **s, "value": v, "tier": (_tier_of(p["tiers"], v) if p["combine_rule"] == "all" else None)}
                for w, s, v in zip(p["windows"], states, per_window)]
        total = sum(per_window)
        res = {"windows": wins, "value": total, "tier": None, "next": None, "reward": None, "amount": None, "status": "in_progress", "broken": False, "capped": False,
               "basis": basis, "date_basis": p["date_basis"]}
        if p["kind"] == "rate":
            plain = [r for r in p["rates"] if not r["option_label"]]
            if plain and len(plain) == len(p["rates"]):
                res["amount"] = sum(r["rate"] for r in plain) * total
                res["parts"] = [{"timing": timing_text(r["pay_timing"]),
                                 "amount": r["rate"] * total} for r in plain]
            res["status"] = "ended" if ended_all else ("achieved" if total > 0 else "in_progress")
        else:
            if p["combine_rule"] == "all":
                done = [w for w in wins if w["state"] == "ended"]
                res["broken"] = any(w["tier"] is None for w in done)          # 끝난 창을 놓치면 뒤 창은 무효다
                reached = [w["tier"]["tier_no"] if w["tier"] else 0 for w in wins]
                low = min(reached)
                tier = next((t for t in p["tiers"] if t["tier_no"] == low), None) if low else None
                focus = next((w for w in wins if w["state"] == "active"), wins[-1])
                base = focus["value"]
            else:
                tier, base = _tier_of(p["tiers"], total), total
            res["tier"] = tier
            nxt = next((t for t in p["tiers"] if t["min_value"] > base), None)
            if p["combine_rule"] == "all":
                closed = [w["tier"]["tier_no"] if w["tier"] else 0 for w in wins if w["state"] == "ended"]
                cap = min(closed) if closed else None                         # 끝난 창의 구간보다 위로는 올라갈 수 없다
                if cap is not None and (nxt is None or nxt["tier_no"] > cap):
                    nxt = None
                    res["capped"] = bool(p["tiers"]) and 0 < cap < p["tiers"][-1]["tier_no"]
            if nxt and not res["broken"]:
                res["next"] = {"tier_no": nxt["tier_no"], "min_value": nxt["min_value"], "gap": nxt["min_value"] - base, "reward": reward_text(nxt)}
            top = p["tiers"][-1]["min_value"] if p["tiers"] else 0
            res["pct"] = min(base / top * 100, 100) if top else 0
            if tier:
                res["reward"] = reward_text(tier)
                if tier["reward_kind"] == "fixed":
                    res["amount"] = tier["reward_value"]
                elif tier["reward_kind"] == "rate":
                    res["amount"] = (tier["reward_value"] or 0) * total
            if res["broken"]:
                res["status"] = "missed"
            elif ended_all:
                res["status"] = "achieved" if tier else "missed"
            else:
                res["status"] = "achieved" if tier and not nxt else "in_progress"
        out[a] = res
    return out


# ---------------------------------------------------------------- 원안 (D-21)
# D-21 은 열려 있다. 제안대로 지금 금액(reward_value, rate)을 배포안으로 보고, 원안은 메인 관리자가 넣은 original_value, original_rate 만 쓴다.
# 원안 칸이 비면 원문 확인이다. 원안 칸은 schema.sql 의 [promotion_plan] 묶음이고 오래된 DB 에는 ensure() 가 붙인다.
PLAN_BEGIN, PLAN_END = "-- [promotion_plan:begin]", "-- [promotion_plan:end]"
MISSING_ORIGINAL = "원문 확인"


def ensure(con) -> None:
    """원안 칸과 확정 결과 표가 없는 오래된 DB 에서 schema.sql 의 [promotion_plan] 묶음을 돌린다. 이미 있는 칸과 표는 건너뛴다."""
    text = db.SCHEMA.read_text(encoding="utf-8")
    block = text[text.index(PLAN_BEGIN):text.index(PLAN_END)]
    body = "\n".join(line.split("--", 1)[0] for line in block.splitlines())
    for stmt in (x.strip() for x in body.split(";")):
        if not stmt:
            continue
        words = stmt.split()
        if words[:2] == ["ALTER", "TABLE"] and words[3:5] == ["ADD", "COLUMN"]:
            if words[5] in {r[1] for r in con.execute(f"PRAGMA table_info({words[2]})")}:
                continue
        con.execute(stmt)
    con.commit()


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


def has_original(p) -> bool:
    return any(t.get("original_value") is not None for t in p["tiers"]) or any(r.get("original_rate") is not None for r in p["rates"])


def _original_view(p) -> dict | None:
    """원안 칸으로 바꿔 끼운 프로모션. 원안이 하나도 없으면 None 이다. 원안이 빈 금액 구간과 지급률은 missing 이다."""
    if not has_original(p):
        return None
    tiers = []
    for t in p["tiers"]:
        money = t["reward_kind"] in ("fixed", "rate")
        miss = money and t.get("original_value") is None
        tiers.append({**t, "reward_value": None if miss else (t.get("original_value") if money else t["reward_value"]), "missing": miss})
    rates = [{**r, "rate": r.get("original_rate"), "missing": r.get("original_rate") is None} for r in p["rates"]]
    return {**p, "tiers": tiers, "rates": rates}


def set_original(con, promo_id, *, by, tiers=None, rates=None) -> dict:
    """원안 보상을 넣는다. 메인 관리자만 한다. tiers 는 {구간 번호: 원안 보상}, rates 는 {조각 순번: 원안 지급률}이다.
    값이 None 이면 원안을 비운다(원문 확인). 바꾸기 전과 뒤를 변경 기록에 남긴다."""
    by = _require_main(con, by)
    ensure(con)
    p = _load(con, promo_id)
    if not p:
        raise ValueError("없는 프로모션입니다.")
    known_t = {t["tier_no"]: t for t in p["tiers"]}
    known_r = {r["seq"]: r for r in p["rates"]}
    before, after = {}, {}
    for no, v in (tiers or {}).items():
        if int(no) not in known_t:
            raise ValueError(f"없는 구간입니다: {no}")
        if known_t[int(no)]["reward_kind"] not in ("fixed", "rate"):
            raise ValueError("여행과 물품 구간에는 원안 금액을 넣지 않습니다.")
        v = None if v in (None, "") else float(v)
        before[f"tier{no}"], after[f"tier{no}"] = known_t[int(no)].get("original_value"), v
        con.execute("UPDATE promotion_tier SET original_value=? WHERE promo_id=? AND tier_no=?", (v, promo_id, int(no)))
    for seq, v in (rates or {}).items():
        if int(seq) not in known_r:
            raise ValueError(f"없는 지급률 조각입니다: {seq}")
        v = None if v in (None, "") else float(v)
        before[f"rate{seq}"], after[f"rate{seq}"] = known_r[int(seq)].get("original_rate"), v
        con.execute("UPDATE promotion_rate SET original_rate=? WHERE promo_id=? AND seq=?", (v, promo_id, int(seq)))
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), by, "promotion_original", f"{promo_id}:{','.join(sorted(after)) or '바뀐 것 없음'}",
                 json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)))
    con.commit()
    return {"promo_id": promo_id, "changed": sorted(after)}


# ---------------------------------------------------------------- 비교
# 비교는 역할과 상관없이 같은 값을 낸다. 설계사에게 여는 일(D-19)과 원안을 보여 줄지(D-21)는 화면이 정한다.
COMPARE_UNIT = 100_000
PLAN_KO = {"distributed": "배포안", "original": "원안"}
PLAN_NOTE = "지금 금액을 적립금을 뺀 배포안으로 봅니다. 원안 금액은 메인 관리자가 넣은 것만 보이고, 없으면 원문 확인입니다."
ESTIMATE_NOTE = "금액과 구간은 모두 예상입니다. 확정은 보험사 정산을 따릅니다."
RATE_BASIS_KO = {"initial_premium": "월초보험료", "converted_premium": "월납환산보험료", "monthly_premium": "월납보험료", "paid_premium": "입금보험료",
                 "base_premium": "기본보험료", "premium": "보험료", "unspecified": "기준 표기 없음"}
SECTOR_KO = {None: "전 업권", "": "전 업권", "life": "생명보험", "nonlife": "손해보험"}


def _money(p) -> bool:
    return p["metric"] != "policy_count"


def _tier_amount(t):
    """그 구간의 문턱에 딱 닿았을 때의 예상 보상(원). 여행과 물품, 원안이 빈 구간은 금액이 없어 None 이다."""
    if t.get("missing"):
        return None
    if t["reward_kind"] == "fixed":
        return t["reward_value"]
    if t["reward_kind"] == "rate":
        return (t["reward_value"] or 0) * t["min_value"]
    return None


def conditions(p, cat_names=None) -> list:
    """비교 표에 나란히 놓을 조건 글. 원문을 바꾸지 않고 칸의 값을 말로 옮긴다. cat_names 는 {구분 코드: 이름}."""
    out = []
    if p["insurer_name"]:
        out.append(f"보험사 {p['insurer_name']}")
    out.append(f"업권 {SECTOR_KO.get(p['sector'], p['sector'])}")
    if p["category_list"]:
        out.append("구분 " + ", ".join((cat_names or {}).get(c, c) for c in p["category_list"]))
    out.append(f"실적 {METRIC_KO[p['metric']]}, 기준일 {BASIS_KO[p['date_basis']]}")
    if len(p["windows"]) > 1:
        out.append(f"기간 창 {len(p['windows'])}개, {COMBINE_KO[p['combine_rule']]}" + (". 낮은 쪽 구간 적용, 끝난 창을 놓치면 무효" if p["combine_rule"] == "all" else ""))
    if p["per_contract_cap"]:
        out.append(f"건당 {int(p['per_contract_cap']):,}원까지 인정")
    if p["pay_timing"]:
        out.append(f"지급 {timing_ko(p['pay_timing'])}" if timing_ko(p["pay_timing"]) else NO_TIMING)
    if p["pay_timing"] and "inst13" in p["pay_timing"]:
        out.append("13회차 유지 시 지급")
    return out


def _plan(p, unit) -> dict:
    """금액 한 벌(배포안 또는 원안)을 같은 기준에 놓는다. {per_unit, per_unit_entry, parts, tiers, comparable, note, per_unit_basis}"""
    plan = {"per_unit": None, "per_unit_entry": None, "parts": [], "tiers": [], "comparable": False, "note": None,
            "per_unit_basis": f"{METRIC_KO[p['metric']]} {unit:,}원당"}
    if p["kind"] == "rate":
        plain = [r for r in p["rates"] if not r["option_label"]]
        if p["rates"] and len(plain) == len(p["rates"]):
            plan["parts"] = [{"rate": r["rate"], "basis": r["basis"], "basis_ko": RATE_BASIS_KO.get(r["basis"], r["basis"]),
                              "timing_ko": timing_ko(r["pay_timing"]) or NO_TIMING,
                              "per_unit": None if r["rate"] is None else r["rate"] * unit, "missing": bool(r.get("missing"))} for r in plain]
            bases = {RATE_BASIS_KO.get(r["basis"], r["basis"]) for r in plain}
            plan["per_unit_basis"] = f"{' · '.join(sorted(bases))} {unit:,}원당"
            if all(r["rate"] is not None for r in plain):
                plan["per_unit"] = plan["per_unit_entry"] = sum(r["rate"] for r in plain) * unit
                plan["comparable"] = True
            else:
                plan["note"] = f"원안 지급률이 빈 조각이 있습니다. {MISSING_ORIGINAL}"
        else:
            plan["note"] = "조건별 지급률이라 숫자 하나로 견주지 않습니다. 원문 확인"
    else:
        for t in p["tiers"]:
            amt = _tier_amount(t)
            per = amt / t["min_value"] * unit if (amt is not None and _money(p) and t["min_value"] > 0) else None
            plan["tiers"].append({"tier_no": t["tier_no"], "min_value": t["min_value"], "min_text": fmt_value(t["min_value"], p),
                                  "reward_kind": t["reward_kind"], "reward": MISSING_ORIGINAL if t.get("missing") else reward_text(t),
                                  "amount": amt, "per_unit": per, "missing": bool(t.get("missing"))})
        pers = [t["per_unit"] for t in plan["tiers"] if t["per_unit"] is not None]
        if pers:
            plan["per_unit"] = max(pers)
            plan["per_unit_entry"] = plan["tiers"][0]["per_unit"] if plan["tiers"] else None
            plan["comparable"] = True
        elif not _money(p):
            plan["note"] = "건수 기준이라 보험료로 견주지 않습니다. 구간과 보상을 보세요"
        else:
            plan["note"] = "여행이나 물품 보상이라 금액으로 견주지 않습니다. 구간과 보상을 보세요"
    return plan


def compare_row(p, unit=COMPARE_UNIT, cat_names=None) -> dict:
    """프로모션 하나를 같은 기준에 놓는다. 기준은 실적 unit 원(기본 10만 원)당 예상 보상이다.

    - 정률 시책: 지급률의 합 × unit. 조건별 지급률이면 숫자 하나로 줄이지 않고 None 이다
    - 구간 시상: 구간마다 문턱 실적에 딱 닿았을 때의 보상 ÷ 문턱 × unit. per_unit 은 그 가운데 가장 높은 값,
      per_unit_entry 는 첫 구간의 값이다. 여행과 물품, 건수 기준(가동 시상)은 보험료로 견주지 않아 None 이다
    금액은 plan 아래 distributed(배포안)와 original(원안)로 나눈다. 견주는 값(per_unit)은 배포안이다.
    원안은 원안 칸이 하나라도 있을 때만 같은 꼴로 채우고, 원안이 빈 구간과 조각은 missing 이며 글은 '원문 확인'이다.
    원안 칸이 하나도 없으면 original 은 None 이다 (D-21 가정).
    """
    row = {k: p[k] for k in ("promo_id", "period_ym", "name", "kind", "kind_ko", "insurer_name", "sector", "category_list", "metric", "metric_ko",
                             "unit", "date_from", "date_to", "recipient_level", "pay_timing", "timing_ko", "per_contract_cap", "combine_rule", "caution")}
    row.update(sector_ko=SECTOR_KO.get(p["sector"], p["sector"]), windows=[{"label": w["label"], "date_from": w["date_from"], "date_to": w["date_to"]} for w in p["windows"]],
               conditions=conditions(p, cat_names), compare_unit=unit, estimate=True, estimate_note=ESTIMATE_NOTE, plan_assumed=True, plan_note=PLAN_NOTE,
               basis=basis_note(p))
    dist = _plan(p, unit)
    op = _original_view(p)
    orig = _plan(op, unit) if op else None
    row.update(comparable=dist.pop("comparable"), note=dist.pop("note"), per_unit_basis=dist.pop("per_unit_basis"), has_original=orig is not None)
    if orig:
        orig.pop("per_unit_basis")
    row["plan"] = {"distributed": dist, "original": orig}
    row["per_unit"], row["per_unit_entry"] = dist["per_unit"], dist["per_unit_entry"]
    return row


def compare(con, ym, *, ref: date | None = None, unit=COMPARE_UNIT, recipient_level="fc", include_ended=False, promo_ids=None) -> dict:
    """같은 기간에 걸친 게시 중 프로모션을 같은 기준으로 견준다. 역할과 상관없이 같은 값을 낸다.

    recipient_level 은 수령 계층이다. 기본은 설계사 몫(fc)이다. 총괄과 지사 몫(D-18)을 넣을지는 부르는 쪽이 정한다.
    include_ended 가 거짓이면 기준일에 끝난 프로모션은 뺀다. promo_ids 를 주면 그것만 견준다.

    돌려주는 것: {ref, unit, rows, groups, estimate_note, plan_note}
    - rows: compare_row() 의 줄에 phase(active / upcoming / ended), days_left 를 붙인 것. 10만 원당 예상 보상이 큰 순이고
      견줄 수 없는 줄은 뒤에 둔다. plan.original 은 원안 칸이 있을 때만 채운다. 원안을 보여 줄지는 화면이 정한다
    - groups: 실적 기준(metric)이 같은 줄끼리. 보험료와 인정보험료와 건수는 서로 견주지 않는다
    """
    ref = ref or ref_date(ym)
    cat_names = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM product_category")}
    rows = []
    for p in listing(con, ym, "main"):                         # 게시된 것만. 수령 계층은 아래에서 고른다
        if recipient_level and p["recipient_level"] != recipient_level:
            continue
        if promo_ids is not None and p["promo_id"] not in set(promo_ids):
            continue
        f, t = date.fromisoformat(p["date_from"]), date.fromisoformat(p["date_to"])
        phase = "upcoming" if ref < f else ("ended" if ref > t else "active")
        if phase == "ended" and not include_ended:
            continue
        rows.append({**compare_row(p, unit, cat_names), "phase": phase, "days_left": max((t - ref).days, 0) if phase != "ended" else 0})
    rows.sort(key=lambda r: (r["per_unit"] is None, -(r["per_unit"] or 0), r["date_to"], r["promo_id"]))
    groups = []
    for metric in METRIC_KO:
        g = [r for r in rows if r["metric"] == metric]
        if g:
            groups.append({"metric": metric, "metric_ko": METRIC_KO[metric], "rows": g})
    return {"ref": ref.isoformat(), "unit": unit, "rows": rows, "groups": groups, "estimate_note": ESTIMATE_NOTE, "plan_note": PLAN_NOTE}


# ---------------------------------------------------------------- 지난 프로모션과 확정
# 확정 전에는 결과가 마감 전 예상(estimate)이다. 메인 관리자가 confirm_results() 로 확정하면 그때의 값을 promotion_result 에 남기고,
# 그 뒤에 final_results() 는 다시 계산하지 않고 남긴 값(confirmed)을 낸다.
SETTLE_KO = {"estimate": "마감 전 예상", "confirmed": "확정"}
OUTCOME_KO = {"achieved": "달성", "missed": "미달성"}
CONFIRMED_NOTE = "메인 관리자가 확정한 결과입니다. 지급은 보험사 정산을 따릅니다."


def _eligible(con, agent_ids) -> list:
    """달성 대상. 재직자만이다. 그 달 중간에 퇴사한 사람은 프로모션 달성 대상에서 뺀다 (D-42). 달성 현황 화면과 같은 기준이다."""
    ids = sorted(set(agent_ids or ()))
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return [r["agent_id"] for r in con.execute(f"SELECT agent_id FROM agent WHERE agent_id IN ({q}) AND status='active' ORDER BY agent_id", ids)]


def ended(con, *, ym=None, ref: date | None = None, recipient_level="fc") -> list:
    """기준일 앞에서 모든 창이 끝난 게시 프로모션. ym 을 주면 그 달에 걸친 것만(월별 보관), 없으면 전부다. 늦게 끝난 것부터."""
    ref = ref or date.today()
    if ym:
        items = listing(con, ym, "main")
    else:
        items = [_load(con, r["promo_id"]) for r in con.execute("SELECT promo_id FROM promotion WHERE status='published' ORDER BY promo_id")]
    items = [p for p in items if (not recipient_level or p["recipient_level"] == recipient_level) and date.fromisoformat(p["date_to"]) < ref]
    return sorted(items, key=lambda p: (p["date_to"], p["promo_id"]), reverse=True)


def original_amount(p, r) -> float | None:
    """progress() 의 결과 하나에 원안을 대입한 금액. 원안 칸이 없거나 그 구간의 원안이 비었으면 None 이다."""
    op = _original_view(p)
    if not op:
        return None
    total = r["value"]
    if p["kind"] == "rate":
        plain = [x for x in op["rates"] if not x["option_label"]]
        if not plain or len(plain) != len(op["rates"]) or any(x["rate"] is None for x in plain):
            return None
        return sum(x["rate"] for x in plain) * total
    if not r["tier"]:
        return None
    t = next((x for x in op["tiers"] if x["tier_no"] == r["tier"]["tier_no"]), None)
    if not t or t["missing"]:
        return None
    if t["reward_kind"] == "fixed":
        return t["reward_value"]
    if t["reward_kind"] == "rate":
        return (t["reward_value"] or 0) * total
    return None


def confirmation(con, promo_id) -> dict | None:
    """살아 있는 확정 한 줄. 없거나 확정 표가 없는 오래된 DB 면 None 이다."""
    try:
        row = con.execute("SELECT * FROM promotion_confirm WHERE promo_id=? AND voided_at IS NULL ORDER BY confirm_id DESC LIMIT 1", (promo_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def _who(con, ids, ym) -> dict:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r["agent_id"]: dict(r) for r in con.execute(
        f"""SELECT a.agent_id, a.internal_code code, a.name, o.name org FROM agent a
            LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
            LEFT JOIN org_unit o ON o.org_id=h.org_id WHERE a.agent_id IN ({q})""", (ym, ym, *ids))}


def _summary(rows) -> dict:
    origs = [x["original_amount"] for x in rows if x["original_amount"] is not None]
    return {"n": len(rows), "achieved": sum(1 for x in rows if x["outcome"] == "achieved"),
            "missed": sum(1 for x in rows if x["outcome"] == "missed"), "amount": sum(x["amount"] or 0 for x in rows),
            "original_amount": sum(origs) if origs else None}


def _estimate(con, p, agent_ids, ref: date) -> list:
    ids = _eligible(con, agent_ids)
    res = progress(con, p, ids, ref)
    who = _who(con, ids, date.fromisoformat(p["date_to"]).strftime("%Y%m"))
    rows = []
    for a in ids:
        r = res[a]
        ok = (r["value"] > 0) if p["kind"] == "rate" else (r["status"] == "achieved")
        rows.append({**who.get(a, {"agent_id": a, "code": None, "name": None, "org": None}), "value": r["value"],
                     "tier_no": r["tier"]["tier_no"] if r["tier"] else None, "reward": r["reward"], "amount": r["amount"],
                     "original_amount": original_amount(p, r),
                     "outcome": "achieved" if ok else "missed", "outcome_ko": OUTCOME_KO["achieved" if ok else "missed"],
                     "settlement": "estimate", "settlement_ko": SETTLE_KO["estimate"], "r": r})
    rows.sort(key=lambda x: (-x["value"], x["code"] or ""))
    return rows


def _confirmed_rows(con, p, head, agent_ids) -> list:
    ids = sorted(set(agent_ids or ()))
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    who = _who(con, ids, date.fromisoformat(p["date_to"]).strftime("%Y%m"))
    rows = []
    for x in con.execute(f"SELECT * FROM promotion_result WHERE confirm_id=? AND agent_id IN ({q})", (head["confirm_id"], *ids)):
        a = x["agent_id"]
        rows.append({**who.get(a, {"agent_id": a, "code": None, "name": None, "org": None}), "value": x["value"], "tier_no": x["tier_no"],
                     "reward": x["reward"], "amount": x["amount"], "original_amount": x["original_amount"],
                     "outcome": x["outcome"], "outcome_ko": OUTCOME_KO[x["outcome"]],
                     "settlement": "confirmed", "settlement_ko": SETTLE_KO["confirmed"], "r": None})
    rows.sort(key=lambda x: (-x["value"], x["code"] or ""))
    return rows


def final_results(con, p, agent_ids, ref: date | None = None) -> dict:
    """끝난 프로모션 하나의 사람별 최종 달성 결과. agent_ids 는 범위 안 설계사 목록이다.

    돌려주는 것: {promo, settlement, settlement_ko, estimate_note, basis, confirmed_by, confirmed_at, rows, summary}
    - 확정 전(settlement=estimate): 값은 달성 현황 계산(progress)을 그대로 쓴다. 재직자만 대상이다 (D-42)
    - 확정 뒤(settlement=confirmed): 다시 계산하지 않고 confirm_results() 가 남긴 값을 낸다. 대상은 확정할 때의 재직자다
    - rows: [{agent_id, code, name, org, value, tier_no, reward, amount, original_amount, outcome, outcome_ko, settlement, settlement_ko, r}]
      값 큰 순. r 은 확정 전에는 progress() 의 결과, 확정 뒤에는 None 이다.
      amount 는 배포안 금액이고 관리자 화면에서는 가린다 (D-17). original_amount 는 원안 금액이고 메인 관리자에게만 보인다 (D-21)
    - outcome: 구간 시상은 끝난 뒤 구간이 있으면 달성. 정률 시책은 대상 실적이 있으면 달성이다
    - summary: {n, achieved, missed, amount, original_amount}
    """
    ref = ref or date.today()
    end = date.fromisoformat(p["date_to"])
    if ref <= end:
        raise ValueError("아직 끝나지 않은 프로모션입니다.")
    head = confirmation(con, p["promo_id"])
    if head:
        rows = _confirmed_rows(con, p, head, agent_ids)
        return {"promo": p, "settlement": "confirmed", "settlement_ko": SETTLE_KO["confirmed"], "estimate_note": CONFIRMED_NOTE,
                "basis": head["basis_note"] or basis_note(p), "confirmed_by": head["confirmed_by"], "confirmed_at": head["confirmed_at"],
                "rows": rows, "summary": _summary(rows)}
    rows = _estimate(con, p, agent_ids, ref)
    return {"promo": p, "settlement": "estimate", "settlement_ko": SETTLE_KO["estimate"], "estimate_note": ESTIMATE_NOTE,
            "basis": basis_note(p), "confirmed_by": None, "confirmed_at": None, "rows": rows, "summary": _summary(rows)}


def confirm_results(con, promotion_id, by, ref: date | None = None) -> dict:
    """끝난 프로모션의 사람별 결과를 확정해 남긴다. 메인 관리자만 부른다. by 는 login_id 이거나 auth.context() 의 결과다.

    대상은 확정하는 때의 재직자 모두다 (D-42). 값은 그때의 final_results() 예상과 같다. 이미 확정했으면 막는다.
    돌려주는 것: 확정 뒤의 final_results() 다. 메인 관리자 범위라 모든 대상이 들어 있다.
    """
    by = _require_main(con, by)
    p = _load(con, promotion_id)
    if not p:
        raise ValueError("없는 프로모션입니다.")
    if p["status"] != "published":
        raise ValueError("게시한 프로모션만 확정합니다.")
    ref = ref or date.today()
    if ref <= date.fromisoformat(p["date_to"]):
        raise ValueError("아직 끝나지 않은 프로모션입니다.")
    ensure(con)
    if confirmation(con, promotion_id):
        raise ValueError("이미 확정한 프로모션입니다.")
    everyone = [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent WHERE status='active'")]
    rows = _estimate(con, p, everyone, ref)
    at = db.now()
    cid = con.execute("""INSERT INTO promotion_confirm(promo_id, ref_date, date_basis, basis_note, n_rows, confirmed_by, confirmed_at)
                         VALUES(?,?,?,?,?,?,?)""", (promotion_id, ref.isoformat(), p["date_basis"], basis_note(p), len(rows), by, at)).lastrowid
    con.executemany("""INSERT INTO promotion_result(confirm_id, promo_id, agent_id, value, tier_no, reward, amount, original_amount, outcome)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    [(cid, promotion_id, x["agent_id"], x["value"], x["tier_no"], x["reward"], x["amount"], x["original_amount"], x["outcome"]) for x in rows])
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)",
                (at, by, "promotion_confirm", f"{promotion_id}:{len(rows)}명"))
    con.commit()
    return final_results(con, p, everyone, ref)


def past(con, agent_ids, *, ym=None, ref: date | None = None, recipient_level="fc") -> list:
    """지난 프로모션 목록과 사람별 최종 결과. agent_ids 는 범위 안 설계사 목록이다. [final_results(), ...] 늦게 끝난 것부터."""
    ref = ref or date.today()
    return [final_results(con, p, agent_ids, ref) for p in ended(con, ym=ym, ref=ref, recipient_level=recipient_level)]
