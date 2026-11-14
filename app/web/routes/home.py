"""홈. 역할마다 다른 카드를 보여 준다."""
from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.periods import period_key
from app.web.deps import _cards, _last_load, _perf, _q, get_con, guard, page
from app.web.routes import card

router = APIRouter()

TIMING_KO = {"next_month": "익월", "inst13": "13회차", "next_month+inst13": "익월과 13회차", "year1": "1차년", "year2": "2차년"}


def _pct(rate):
    return f"{rate * 100:g}%"


def _promos(con, ym, limit=6):
    """게시 기능을 만들기 전까지는 시상안 원장의 설계사 몫 줄을 미리보기로 보여 준다."""
    doc = con.execute("SELECT plan_doc_id FROM incentive_plan_doc WHERE status!='superseded' ORDER BY period_ym=? DESC, plan_doc_id DESC LIMIT 1", (ym,)).fetchone()
    if not doc:
        return [], 0
    rows = _q(con, """SELECT l.line_id, l.insurer_name, l.category_raw, l.period_raw, l.period_to, l.reward_raw, l.pay_timing, l.parse_status,
                             (SELECT GROUP_CONCAT(c.rate, '|') FROM incentive_plan_component c WHERE c.line_id=l.line_id) rates
                      FROM incentive_plan_line l WHERE l.plan_doc_id=? AND l.recipient_level='fc'
                      ORDER BY l.period_to IS NULL, l.period_to, l.line_id""", (doc["plan_doc_id"],))
    today = date.today()
    for r in rows[:limit]:
        rates = [float(x) for x in (r["rates"] or "").split("|") if x]
        if len(rates) == 1 and r["parse_status"] == "auto":
            r["rate_kind"], r["rate_label"] = "one", _pct(rates[0])
        elif r["reward_raw"] and rates:
            r["rate_kind"], r["rate_label"] = "text", r["reward_raw"]        # 조건이 여럿이면 숫자 하나로 줄이지 않는다
        else:
            r["rate_kind"], r["rate_label"] = "none", None
        r["timing"] = TIMING_KO.get(r["pay_timing"])
        if r["period_to"]:
            end = date.fromisoformat(r["period_to"])
            left = (end - today).days
            r["due"] = "오늘 마감" if left == 0 else (f"D-{left}" if left > 0 else f"{end.month}월 {end.day}일 종료")
            r["due_class"] = "bad" if 0 <= left <= 3 else ("blue" if left > 3 else "")
        else:
            r["due"], r["due_class"] = "기간 확인", "warn"
    return rows[:limit], len(rows)


def _goal(con, ym, agent_id):
    """분기 인정보험료 목표가 있으면 달성률을 낸다."""
    key = period_key("quarter", ym)
    g = con.execute("SELECT target_value FROM agent_goal WHERE agent_id=? AND period_type='quarter' AND period_key=? AND metric='converted_premium'", (agent_id, key)).fetchone()
    if not g or not g["target_value"]:
        return None
    v = con.execute("""SELECT s.converted_premium v FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                       WHERE s.period_type='quarter' AND s.period_key=? AND s.agent_id=?""", (ym, key, agent_id)).fetchone()
    value = (v["v"] if v else 0) or 0
    return {"label": f"{key[:4]}년 {key[-1]}분기", "target": g["target_value"], "value": value,
            "pct": value / g["target_value"] * 100, "left": max(g["target_value"] - value, 0)}


@router.get("/", response_class=HTMLResponse)
def home(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    ym, role = ctx["ym"], ctx["role"]
    data = {"last_load": _last_load(con)}
    if data["last_load"]:
        t = datetime.strptime(data["last_load"], "%Y-%m-%d %H:%M:%S")
        data["last_load_label"] = f"{t.month}월 {t.day}일 {t.hour}시 {t.minute:02d}분"
    cards, _ = _cards(con, ctx)
    data["cards"] = [c for c in cards if c["recipient_level"] == "fc" and c["phase"] != "ended"][:6] or [c for c in cards if c["recipient_level"] == "fc"][:6]
    data["promos"], data["promo_total"] = ([], 0) if data["cards"] else _promos(con, ym)
    if ctx["agent_id"]:
        mine = _perf(con, ym, [ctx["agent_id"]])
        data["mine"] = mine[0] if mine else None
        r = con.execute("""SELECT s.rank_no FROM ranking_snapshot s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                           WHERE s.period_type='month' AND s.period_key=? AND s.metric='recognized_premium' AND s.scope='all' AND s.agent_id=?""",
                        (ym, ym, ctx["agent_id"])).fetchone()
        data["my_rank"] = r["rank_no"] if r else None
        data["rank_total"] = con.execute("""SELECT COUNT(*) FROM ranking_snapshot s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                                            WHERE s.period_type='month' AND s.period_key=? AND s.metric='recognized_premium' AND s.scope='all'""", (ym, ym)).fetchone()[0]
        data["goal"] = _goal(con, ym, ctx["agent_id"])
    if role in ("manager", "main"):
        team = _perf(con, ym, ctx["agent_ids"] if role == "manager" else [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent")])
        # 합계와 가동에는 그 달 중간에 퇴사한 사람의 퇴사 전 실적도 넣는다. 상위 목록은 경쟁 표시라 퇴사자를 뺀다
        data["team"] = {"n": len(team), "active": sum(1 for t in team if (t["net_count"] or 0) > 0), "left": sum(1 for t in team if t["left"]),
                        "premium": sum(t["converted_premium"] or 0 for t in team), "count": sum(t["net_count"] or 0 for t in team),
                        "top": [t for t in team if not t["left"]][:5]}
    if role in ("manager", "main"):
        data["card_pending"] = card.pending_count(con, ctx)      # 키가 없으면 None. 짧은 시간 제한과 잠깐 담아 둔 목록을 쓴다
    if role == "main":
        one = lambda sql, p=(): con.execute(sql, p).fetchone()[0]
        data["ops"] = {
            "batches": one("SELECT COUNT(*) FROM import_batch WHERE period_ym=?", (ym,)),
            "block": one("SELECT COUNT(*) FROM quality_issue q JOIN import_batch b ON b.batch_id=q.batch_id WHERE b.period_ym=? AND q.severity='block'", (ym,)),
            "warn": one("SELECT COUNT(*) FROM quality_issue q JOIN import_batch b ON b.batch_id=q.batch_id WHERE b.period_ym=? AND q.severity='warn'", (ym,)),
            "plan_manual": one("SELECT COUNT(*) FROM incentive_plan_line l JOIN incentive_plan_doc d ON d.plan_doc_id=l.plan_doc_id WHERE d.status!='superseded' AND l.parse_status='manual' AND l.review_status='draft'"),
            "accounts": one("SELECT COUNT(*) FROM user_account WHERE status='active'"),
            "closed": bool(con.execute("SELECT 1 FROM close_period WHERE period_ym=? AND status='closed'", (ym,)).fetchone()),
        }
    return page(request, ctx, "home.html", d=data)
