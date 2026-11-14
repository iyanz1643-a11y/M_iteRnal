"""프로모션: 게시, 구간 입력, 달성 현황 계산.

프로모션 하나 = 기간 창(하나 이상) + 구간표 + 결합 규칙 + 보상 + 조건.
  - rate      정률 시책. 대상 계약의 보험료에 지급률을 곱한 예상액
  - weekly    주차 시상. 창 하나의 실적을 구간표에 대입
  - linked    연계 시상. 창마다 달성해야 하고 낮은 쪽 구간을 적용 (combine_rule = all)
  - monthly / membership / travel / activity  누적 실적을 구간표에 대입
달성 현황은 저장하지 않고 계약 원장에서 그때그때 계산한다. 모두 예상이며 확정은 보험사 정산을 따른다.
"""
import calendar
from datetime import date

from app import db

KIND_KO = {"rate": "정률 시책", "weekly": "주차 시상", "linked": "연계 시상", "monthly": "월 구간 시상",
           "travel": "여행 시상", "activity": "가동 시상", "membership": "멤버십"}
METRIC_KO = {"premium_monthly": "보험료", "converted_premium": "인정보험료", "policy_count": "건수"}
METRIC_UNIT = {"premium_monthly": "원", "converted_premium": "원", "policy_count": "건"}
BASIS_KO = {"contract_date": "계약일자", "application_date": "청약일자"}
COMBINE_KO = {"single": "창 하나", "all": "창마다 달성", "sum": "창을 합산"}
REWARD_KO = {"fixed": "정액", "rate": "실적의 비율", "travel": "여행", "goods": "물품"}
TIMING_KO = {"next_month": "익월", "inst13": "13회차", "next_month+inst13": "익월과 13회차", "year1": "1차년", "year2": "2차년"}
COUNTED = ("inforce", "lapsed", "cancelled", "matured")       # 성립한 계약. 청약, 반송, 철회는 세지 않는다
FROM_LINE_KIND = {"rate_promo": "rate", "activity": "activity", "consecutive": "linked", "tier": "monthly"}


def ref_date(ym: str) -> date:
    """기준일. 기준 월이 이번 달이면 오늘, 지난 달이면 그 달의 말일이다."""
    today = date.today()
    if today.strftime("%Y%m") == ym:
        return today
    y, m = int(ym[:4]), int(ym[4:])
    return date(y, m, calendar.monthrange(y, m)[1])


# ---------------------------------------------------------------- 등록과 게시
def create(con, *, period_ym, name, kind, windows, tiers=(), rates=(), by, recipient_level="fc", insurer_id=None, insurer_name=None,
           sector=None, categories=None, target_note=None, metric="premium_monthly", date_basis="contract_date", per_contract_cap=None,
           combine_rule=None, pay_timing=None, caution=None, source_line_id=None, publish=False) -> int:
    """windows: [(label, from, to)], tiers: [(min_value, reward_kind, reward_value, reward_label)], rates: [(option, basis, rate, timing)]"""
    if kind not in KIND_KO or metric not in METRIC_KO or date_basis not in BASIS_KO:
        raise ValueError("유형, 지표, 기준일 값이 맞지 않습니다.")
    windows = [(l, f, t) for l, f, t in windows if f and t]
    if not windows:
        raise ValueError("기간 창이 하나 이상 있어야 합니다.")
    for l, f, t in windows:
        if date.fromisoformat(f) > date.fromisoformat(t):
            raise ValueError(f"기간 창 '{l}' 의 시작일이 종료일보다 늦습니다.")
    combine_rule = combine_rule or ("all" if kind == "linked" else ("sum" if len(windows) > 1 else "single"))
    if combine_rule == "single" and len(windows) > 1:
        raise ValueError("창이 둘 이상이면 결합 규칙을 정해야 합니다.")
    tiers = sorted((float(m), k, (float(v) if v not in (None, "") else None), (lab or None)) for m, k, v, lab in tiers)
    if kind != "rate" and not tiers:
        raise ValueError("구간표가 비어 있습니다.")
    if any(k not in REWARD_KO for _, k, _, _ in tiers) or len({m for m, *_ in tiers}) != len(tiers):
        raise ValueError("구간의 보상 종류가 맞지 않거나 기준값이 겹칩니다.")
    if isinstance(categories, (list, tuple, set)):
        categories = ",".join(sorted(categories)) or None
    cur = con.execute(
        """INSERT INTO promotion(period_ym, name, kind, recipient_level, insurer_id, insurer_name, sector, categories, target_note, metric, date_basis,
                                 per_contract_cap, combine_rule, pay_timing, caution, source_line_id, status, created_by, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)""",
        (period_ym, name.strip(), kind, recipient_level, insurer_id, insurer_name, sector or None, categories or None, target_note, metric, date_basis,
         per_contract_cap or None, combine_rule, pay_timing, caution, source_line_id, by, db.now()))
    pid = cur.lastrowid
    for i, (l, f, t) in enumerate(windows, start=1):
        con.execute("INSERT INTO promotion_window(promo_id, seq, label, date_from, date_to) VALUES(?,?,?,?,?)", (pid, i, l or f"{i}구간", f, t))
    for i, (m, k, v, lab) in enumerate(tiers, start=1):
        con.execute("INSERT INTO promotion_tier(promo_id, tier_no, min_value, reward_kind, reward_value, reward_label) VALUES(?,?,?,?,?,?)", (pid, i, m, k, v, lab))
    for i, (opt, basis, rate, timing) in enumerate(rates, start=1):
        con.execute("INSERT INTO promotion_rate(promo_id, seq, option_label, basis, rate, pay_timing) VALUES(?,?,?,?,?,?)", (pid, i, opt, basis, rate, timing))
    con.commit()
    if publish:
        set_status(con, pid, "published", by)
    return pid


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
    p["timing_ko"] = TIMING_KO.get(p["pay_timing"])
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
            return {"big": " + ".join(f"{r['rate'] * 100:g}%" for r in plain), "small": " + ".join(TIMING_KO.get(r["pay_timing"], "지급") for r in plain)}
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
        sql = f"""SELECT c.agent_id, SUM({expr}) v FROM contract c LEFT JOIN product pr ON pr.product_id=c.product_id
                  JOIN insurer i ON i.insurer_id=c.insurer_id
                  WHERE c.agent_id IN ({q}) AND c.status IN ({','.join('?' * len(COUNTED))}) AND c.{p['date_basis']} BETWEEN ? AND ?"""
        params = ([p["per_contract_cap"]] if "MIN(" in expr else []) + list(agent_ids) + list(COUNTED) + [w["date_from"], w["date_to"]]
        if p["insurer_id"]:
            sql += " AND c.insurer_id=?"
            params.append(p["insurer_id"])
        if p["sector"]:
            sql += " AND i.sector=?"
            params.append(p["sector"])
        if p["category_list"]:
            sql += f" AND pr.product_category IN ({','.join('?' * len(p['category_list']))})"
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
    """{agent_id: 달성 현황}. 금액과 구간은 모두 예상이다."""
    vals = _values(con, p, list(agent_ids))
    states = [window_state(w, ref) for w in p["windows"]]
    ended_all = all(s["state"] == "ended" for s in states)
    out = {}
    for a, per_window in vals.items():
        wins = [{**w, **s, "value": v, "tier": (_tier_of(p["tiers"], v) if p["combine_rule"] == "all" else None)}
                for w, s, v in zip(p["windows"], states, per_window)]
        total = sum(per_window)
        res = {"windows": wins, "value": total, "tier": None, "next": None, "reward": None, "amount": None, "status": "in_progress", "broken": False}
        if p["kind"] == "rate":
            plain = [r for r in p["rates"] if not r["option_label"]]
            if plain and len(plain) == len(p["rates"]):
                res["amount"] = sum(r["rate"] for r in plain) * total
                res["parts"] = [{"timing": (TIMING_KO[r["pay_timing"]] + " 지급") if r["pay_timing"] in TIMING_KO else "지급 시점 표기 없음",
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
