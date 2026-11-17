"""프로모션 묶음: 진행 중 프로모션, 달성 현황, 프로모션 비교, 지난 프로모션, 게시 관리, 구간과 조건 입력, 조직 부문 시상.

- 비교(/promo/compare)는 pm.compare 를 그대로 그린다. D-19 제안대로 설계사에게도 열고 내부 참고용 안내를 붙인다.
  D-21 제안대로 원안은 메인 관리자에게만 넘긴다. 설계사와 관리자에게는 줄에서 원안 칸을 떼고 넘긴다.
- 지난 프로모션(/promo/archive)은 pm.past 로 낸 마감 전 예상이다. 설계사는 본인 줄만, 관리자는 범위 안 사람의 달성 여부만 본다.
  D-17 에 따라 관리자 화면으로는 금액과 보상 글을 넘기지 않는다. 메인 관리자는 전부 본다.
  메인 관리자는 결과 확정, 확정 무르기(D-50 가정), 13회차 판정 확정을 한다. 모두 경고와 확인 단계를 거친다.
  확정 이력과 무른 사유는 메인 관리자에게만 넘긴다. 순위는 엔진이 재직자 전체에서 매긴 값이다.
  여행과 물품 시상은 pm.travel_roster 로 달성자 명단을 낸다. D-20 가정대로 관리자는 본인과 직속 팀원, 설계사는 본인만 본다.
  13회차 조건이 붙은 프로모션은 pm.inst13_results 로 지급, 환수, 판정 전을 보인다. 관리자에게는 금액 칸을 떼고 넘긴다 (D-17).
- 조직 부문 시상(/promo/org-awards)의 순위별 시상 내용은 D-45 제안대로(가정) 모두에게 공개하는 상품표다.
  규칙은 app/web/awards.py 한 곳이고 명예의전당과 같다. 합계는 만들지 않는다.
"""
import calendar
import itertools
import sqlite3
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.engine import org_award as oa
from app.engine import promotion as pm
from app.web import auth, awards, org_labels, org_values, promo_extra
from app.web.deps import _cards, _in, _period_text, _q, get_con, guard, page, promo_cards

router = APIRouter()
# 구간 근접: 다음 구간까지 남은 양이 다음 구간 문턱의 이 비율 이내. 달성 현황과 알림함이 함께 쓴다.
# 값은 조직별 설정 promo.near_pct 이고 사람마다 소속 조직의 값을 쓴다. 이 상수는 설정이 없을 때의 기본값이다
NEAR_RATIO = org_values.ratio(org_values.default(org_values.NEAR_KEY))


def near(r, ratio: float = NEAR_RATIO) -> bool:
    """progress() 한 사람의 결과가 구간 근접인가. 실적이 있고 다음 구간까지 ratio 안이다. ratio 는 그 사람 조직의 값을 넘긴다."""
    return bool(r["next"] and r["value"] > 0 and r["next"]["gap"] <= r["next"]["min_value"] * ratio)


def near_pcts(con, ctx, agent_ids) -> dict:
    """{agent_id: 구간 근접 퍼센트}. 사람마다 자료 기준 달의 소속 조직 값이다."""
    return org_values.for_agents(con, agent_ids, org_values.NEAR_KEY, ctx["ym"])


def _scope_agents(con, ctx):
    ids = ctx["agent_ids"] if ctx["role"] != "main" else {r["agent_id"] for r in con.execute("SELECT agent_id FROM agent WHERE status='active'")}
    q, lst = _in(ids)
    ym = ctx["ym"]
    return _q(con, f"""SELECT a.agent_id, a.internal_code code, a.name, o.name org FROM agent a
                       LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
                       LEFT JOIN org_unit o ON o.org_id=h.org_id WHERE a.agent_id IN ({q}) AND a.status='active' ORDER BY a.internal_code""", (ym, ym, *lst))


@router.get("/promo/list", response_class=HTMLResponse)
def promo_list(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    cards, ref = _cards(con, ctx)
    return page(request, ctx, "promo_list.html", cards=cards, ref=ref.isoformat())


def _gates(con, cards, ref=None) -> list:
    """딸린 시책의 조직 달성 인원 문턱 카드. 숫자만 낸다. 사람은 내지 않는다 (pm.org_gate, D-88 가정).
    카드에 기준일(ref)이 있으면 그 날로 센다. 여러 달에 걸친 프로모션은 오늘 기준이다 (progress_cards)."""
    out = []
    for c in cards:
        if c["phase"] == "ended" or not pm._r(c)["gate_min_achievers"]:
            continue
        g = pm.org_gate(con, c, c.get("ref") or ref)
        if g:
            out.append({**g, "promo_id": c["promo_id"], "name": c["name"], "org_title": org_labels.title({"path_key": g["org_path"], "name": g["org_name"]})})
    return out


def _team_rows(con, ctx, sel, ref, only=None):
    """팀원 달성 현황의 줄과 요약. only 를 주면 범위 안 설계사 가운데 그 사람만 남긴다. 조직별 보기의 사람 목록도 이 함수를 쓴다."""
    agents = _scope_agents(con, ctx)
    if only is not None:
        keep = set(only)
        agents = [a for a in agents if a["agent_id"] in keep]
    res = pm.progress(con, sel, [a["agent_id"] for a in agents], ref)
    pcts = near_pcts(con, ctx, [a["agent_id"] for a in agents])
    rows = [{**a, "r": res[a["agent_id"]]} for a in agents]
    rows.sort(key=lambda x: -x["r"]["value"])
    near_rows = [x for x in rows if near(x["r"], org_values.ratio(pcts[x["agent_id"]]))]
    summary = {"near_text": org_values.pct_text(pcts.values()), "n": len(rows), "achieved": sum(1 for x in rows if x["r"]["tier"] or (sel["kind"] == "rate" and x["r"]["value"] > 0)),
               "near": len(near_rows), "missed": sum(1 for x in rows if x["r"]["broken"]), "top": max((x["r"]["value"] for x in rows), default=0)}
    return rows, summary


# ---------------------------------------------------------------- 달성 현황의 달과 기준일
# 대표님 지시(11월 17일): 여러 달에 걸친 프로모션은 오늘 기준으로 보고, 지난 달은 월을 바꿔서 본다.
# 달을 고르지 않으면 '지금' 이다. 자료 기준 달의 프로모션과 이번 달에 걸친 프로모션을 함께 놓고, 여러 달에 걸친 것은 오늘,
# 한 달짜리는 자료 기준 달의 기준일(pm.ref_date)로 센다. 달을 고르면 그 달의 프로모션을 그 달 기준일로 센다(지난 달은 말일).
# 소속과 구간 근접 값은 어느 경우든 자료 기준 달(ctx ym)의 것이다. 팀원 현황과 조직별 보기와 내 현황이 같은 기준일을 쓴다.
MONTHS_BACK = 3                                                    # 달 고르기에 보이는 지난 달 수


def _multi_month(p) -> bool:
    return p["date_from"][:7] != p["date_to"][:7]


def view_month(ym: str | None) -> tuple:
    """(보는 달, 지금 보기인가). ym 이 YYYYMM 이고 이번 달 이전이면 그 달, 아니면 이번 달이다."""
    cur = date.today().strftime("%Y%m")
    if ym and len(ym) == 6 and ym.isdigit() and 1 <= int(ym[4:]) <= 12 and ym < cur:
        return ym, False
    return cur, True


def progress_cards(con, ctx, ym: str | None = None) -> tuple:
    """달성 현황 화면의 설계사 몫 카드와 머리 정보. 카드마다 ref 가 그 카드의 기준일이다."""
    vym, now = view_month(ym)
    today = date.today()
    if now:
        seen, promos = set(), []
        for x in pm.listing(con, ctx["ym"], ctx["role"]) + pm.listing(con, vym, ctx["role"]):
            if x["promo_id"] not in seen:
                seen.add(x["promo_id"])
                promos.append(x)
        data_ref = pm.ref_date(ctx["ym"])
        ref_of = lambda x: today if (_multi_month(x) or x["date_from"][:7].replace("-", "") >= vym) else data_ref
    else:
        promos = pm.listing(con, vym, ctx["role"])
        month_ref = pm.ref_date(vym)
        ref_of = lambda x: month_ref
    cards = [c for c in promo_cards(con, promos, ref_of) if c["recipient_level"] == "fc"]
    cur = date.today().replace(day=1)
    back = []
    y, m = cur.year, cur.month
    for _ in range(MONTHS_BACK):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        back.append(f"{y}{m:02d}")
    head = {"ym": vym, "now": now, "today": today.isoformat(), "months": back, "ref": (today if now else pm.ref_date(vym)).isoformat(),
            "month_refs": sorted({c["ref"].isoformat() for c in cards if c["ref"] != today})}
    return cards, head


@router.get("/promo/progress", response_class=HTMLResponse)
def promo_progress(request: Request, p: int | None = None, view: str | None = None, org: str | None = None, direct: int | None = None,
                   ym: str | None = None, who: str | None = None, row: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    cards, head = progress_cards(con, ctx, ym)
    if view == "org":
        if ctx["role"] not in ("main", "manager"):                 # 가정: 설계사에게는 조직별 보기가 없다
            auth.log(con, ctx["login_id"], "access_denied", "/promo/progress org")
            raise HTTPException(403, "조직별 보기는 메인 관리자와 관리자만 봅니다.")
        return _org_view(request, con, ctx, cards, head, p, org, bool(direct), who, row)
    gates = _gates(con, cards)
    team_view = ctx["role"] == "main" or (ctx["role"] == "manager" and view != "me")
    if not team_view:
        aid = ctx["agent_id"]
        mine = [{**c, "r": pm.progress(con, c, [aid], c["ref"])[aid]} for c in cards] if aid else []
        bridge_ids = {b["promo_id"] for b in pm.bridges(con, head["ym"], ctx["role"])}   # 브릿지 시상 대상 목록은 엔진이 낸다
        if head["now"]:                                           # 지금 보기에는 자료 기준 달의 브릿지도 있다
            bridge_ids |= {b["promo_id"] for b in pm.bridges(con, ctx["ym"], ctx["role"])}
        for c in mine:
            c["is_bridge"] = c["promo_id"] in bridge_ids and bool(c["r"].get("bridge"))
            c["rule_lines"] = pm.rule_conditions(c)
            c["boost_text"] = boost_text(c)
            c["versions"] = promo_extra.versions(con, c)
            c["chain_text"] = chain_text(c)
            c["phase_rows"], c["sum_row"] = my_phases(c, c["r"])
            c["alt"] = my_alt(c, c["r"])
            c["has_alt"] = bool(pm._r(c).get("alt_chains"))
            if aid and pm._r(c)["retention_min"]:
                c["retention"] = pm.retention(con, c, [aid], c["ref"])
                c["retention_me"] = c["retention"]["people"][aid]
        return page(request, ctx, "promo_progress_me.html", items=mine, head=head, ref=head["ref"], gates=gates, combine_ko=pm.COMBINE_KO,
                    paired_text=PAIRED_TEXT, bridge_state_class=BRIDGE_STATE_CLASS)
    sel = next((c for c in cards if c["promo_id"] == p), cards[0] if cards else None)
    rows, summary = [], None
    if sel:
        rows, summary = _team_rows(con, ctx, sel, sel["ref"])
        sel["versions"] = promo_extra.versions(con, sel)
    return page(request, ctx, "promo_progress_team.html", cards=cards, sel=sel, rows=rows, summary=summary, head=head,
                ref=(sel["ref"].isoformat() if sel else head["ref"]), show_amount=ctx["role"] == "main", gates=gates)


def my_phases(p, r) -> tuple:
    """내 달성 현황의 달(창)별 줄과 합산 줄. 대표님이 정한 틀: 끝난 달은 달성 여부, 진행 중인 달은 남은 금액, 시작 전인 달은 기준만.
    남은 금액은 엔진이 다음 구간까지 남은 값(next.gap)을 그 창에 대해 내면 그 값을 쓰고, 아니면 창 기준에서 창 실적을 뺀 차이만 보인다.
    창이 하나거나 창마다 달성하는 결합이 아니면 빈 값이다."""
    if p["combine_rule"] not in pm.EACH_RULES or len(p["windows"]) < 2 or not r.get("windows"):
        return [], None
    nxt = r.get("next") or {}
    rows = []
    for w in r["windows"]:
        floor = window_floor(p, w)
        state = _STATE_PHASE[w["state"]]
        left = None
        if state == "open" and not w["tier"] and floor is not None:
            left = nxt["gap"] if nxt.get("path", "each") == "each" and nxt.get("min_value") == floor else max(floor - w["value"], 0)
        rows.append({"seq": w["seq"], "label": w["label"], "state": state, "state_ko": PHASE_KO[state], "value": w["value"], "plain": w.get("plain", w["value"]),
                     "floor": floor, "met": bool(w["tier"]), "left": left, "date_from": w["date_from"], "date_to": w["date_to"]})
    target = sum_target(p)
    sum_row = None
    if target:
        total = (r.get("paths") or {}).get("sum", {}).get("value", r["value"])
        met = bool((r.get("paths") or {}).get("sum", {}).get("tier_no"))
        sum_row = {"target": target, "value": total, "met": met, "left": None if met else max(target - total, 0)}
    return rows, sum_row


def my_alt(p, r) -> dict | None:
    """추가안(엔진의 paths.alt). 대상(eligible)인 사람에게만 낸다. 앞 창을 채운 사람에게는 보이지 않는다. 엔진 값이 없으면 None 이다.
    진행 중인 창의 남은 금액은 그 창 기준에서 창 실적을 뺀 차이만 보인다."""
    alt = (r.get("paths") or {}).get("alt")
    if not alt or not alt.get("eligible"):
        return None
    labels = {w["seq"]: w["label"] for w in p["windows"]}
    rows = []
    for w in alt.get("windows") or []:
        state = w.get("state")
        state = _STATE_PHASE.get(state, state)
        left = max(w["min_value"] - w["value"], 0) if state == "open" and not w.get("met") and w.get("min_value") is not None else None
        rows.append({"label": labels.get(w["seq"], f"{w['seq']}번 창"), "state": state, "state_ko": PHASE_KO.get(state, ""), "min_value": w.get("min_value"),
                     "value": w.get("value", 0), "met": bool(w.get("met")), "left": left})
    need = ", ".join(f"{x['label']} {_short(x['min_value'], p)}" for x in rows if x["min_value"] is not None)
    return {"label": "조건 1-2", "name": alt.get("label"), "need": need, "rows": rows, "broken": bool(alt.get("broken")), "achieved": bool(alt.get("achieved"))}


def boost_text(p) -> str | None:
    """가중 인정을 설계사에게 보이는 글. 규칙 값을 말로 옮기기만 한다. 예시는 5만원에 배수를 곱한 값이다.
    날짜 목록(ranges)이면 든 달을 적고, 프로모션 기간 가운데 빠진 달은 '2배 인정이 없습니다' 로 적는다."""
    b = pm._r(p)["boost"]
    if not b:
        return None
    f = b["factor"]
    cap = f"(원보험료 {_short(b['cap'], p)}원까지)" if b.get("cap") is not None else ""
    if b.get("month_days"):
        d0, d1 = b["month_days"]
        when = "1~2주차" if (d0, d1) == (1, 14) else f"매월 {d0}일에서 {d1}일"
        text = f"{when} 가동은 {f:g}배로 인정합니다{cap}."
    else:
        ranges = b.get("ranges") or []
        months = sorted({(r[0][:4], int(r[0][5:7])) for r in ranges})
        weeks = all(r[0][8:] == "01" and r[1][8:] == "14" for r in ranges)
        when = "과 ".join(f"{m}월" for _y, m in months) + ("의 1~2주차" if weeks else "의 정한 날짜")
        text = f"{when} 가동은 {f:g}배로 인정합니다{cap}."
        span = {(w["date_from"][:4], int(w["date_from"][5:7])) for w in p["windows"]} | {(w["date_to"][:4], int(w["date_to"][5:7])) for w in p["windows"]}
        missing = sorted(span - set(months))
        if missing:
            text += " " + ", ".join(f"{m}월" for _y, m in missing) + "은 2배 인정이 없습니다." if f == 2 else f" {', '.join(f'{m}월' for _y, m in missing)}은 배수 인정이 없습니다."
    text += f" 예: 5만원 가동 시 {50_000 * f / 10_000:g}만원 인정."
    if b.get("applies_to") == "each":
        text += " 연속으로 채우는 조건에만 쓰고 합산은 원래 실적으로 셉니다."
    return text


# ---------------------------------------------------------------- 조직별 진척
# 보는 범위는 가정이다(대표님 확인 전). 메인 관리자는 전체 조직, 본부 순위, 금액을 본다. 관리자는 자기 조직과 그 아래만 보고,
# 순위도 자기 범위 안의 조직끼리만 매긴다. 설계사에게는 조직별 보기가 없고 Convention 같은 문턱의 숫자만 보인다.
# 숫자는 pm.org_progress 만 쓴다. 팀원 달성 현황과 같은 범위 목록(_scope_agents), 자료 기준 달(ctx ym), 구간 근접 값(near_pcts)을 넘긴다.
ORG_ASSUMPTION = "조직별 진척을 보는 범위는 가정입니다. 메인 관리자는 전체 조직과 금액을, 관리자는 자기 조직과 그 아래만 봅니다."
PAIRED_TEXT = {"both": "조건 1과 조건 2를 모두 채워야 합니다. 두 조건 가운데 낮은 쪽 구간이 적용됩니다.",
               "either": "조건 1과 조건 2 가운데 하나만 채우면 됩니다. 두 조건 가운데 높은 쪽 구간이 적용됩니다."}
BRIDGE_STATE_CLASS = {"achieved": "good", "void": "bad", "missed": "bad", "open": "blue", "waiting": "", "before": ""}   # 상태 칩 색


def _org_row(con, path_key):
    r = con.execute("SELECT org_id, parent_id, name, level, path_key FROM org_unit WHERE path_key=?", (path_key,)).fetchone()
    return dict(r) if r else None


def _org_starts(con, ctx) -> list:
    """조직별 보기를 시작하는 조직. 메인 관리자는 맨 위 조직, 관리자는 맡은 조직 가운데 다른 맡은 조직 아래에 있지 않은 것."""
    if ctx["role"] == "main":                                      # config/org_labels.yaml 의 시작 조직(이터널총괄본부). 없으면 맨 위 조직
        start = org_labels.start_path(con)
        if start:
            return _q(con, "SELECT org_id, parent_id, name, level, path_key FROM org_unit WHERE path_key=?", (start,))
        return _q(con, "SELECT org_id, parent_id, name, level, path_key FROM org_unit WHERE parent_id IS NULL ORDER BY org_id LIMIT 1")
    heads = sorted({h["org_id"] for h in ctx["heads"]})
    out = []
    for oid in heads:
        r = con.execute("SELECT org_id, parent_id, name, level, path_key FROM org_unit WHERE org_id=?", (oid,)).fetchone()
        up, top = r["parent_id"] if r else None, True
        while up is not None:
            if up in heads:
                top = False
                break
            up = con.execute("SELECT parent_id FROM org_unit WHERE org_id=?", (up,)).fetchone()["parent_id"]
        if r and top:
            out.append(dict(r))
    return out


def _crumbs(con, org, starts) -> list:
    """시작 조직에서 지금 조직까지의 경로. 시작 조직 위로는 올라가지 않는다."""
    stop = {s["org_id"] for s in starts}
    out, cur = [], org
    while cur:
        out.append(cur)
        if cur["org_id"] in stop or cur["parent_id"] is None:
            break
        r = con.execute("SELECT org_id, parent_id, name, level, path_key FROM org_unit WHERE org_id=?", (cur["parent_id"],)).fetchone()
        cur = dict(r) if r else None
    return list(reversed(out))


def org_numbers(con, ctx, sel, ref, org_key, *, keep_amounts: bool):
    """조직별 진척 하나. 팀원 달성 현황과 같은 범위, 달, 구간 근접 값으로 pm.org_progress 를 부른다.
    keep_amounts 가 거짓이면 금액(amounts)을 떼고 돌려준다 (D-17)."""
    ids = [a["agent_id"] for a in _scope_agents(con, ctx)]
    res = pm.org_progress(con, sel, org_key, ids, ref, ym=ctx["ym"], near_pct=near_pcts(con, ctx, ids))
    if not keep_amounts:
        res = {k: v for k, v in res.items() if k != "amounts"}
    return res


PHASE_KO = {"done": "끝남", "open": "진행 중", "future": "시작 전"}
_STATE_PHASE = {"ended": "done", "active": "open", "upcoming": "future"}


def _phase_list(x, data, ref) -> list:
    """줄 하나의 창별 단계. 엔진이 phases 를 내면 그대로 쓴다. 없으면 창 상태는 pm.window_state 로, 인원은 줄의 창별 달성 인원으로 둔다."""
    if x.get("phases") is not None:
        return x["phases"]
    wins = x.get("windows")
    if wins is None:
        return []
    out = []
    for w, n in zip(data["windows"], wins):
        out.append({"seq": w["seq"], "label": w["label"], "state": _STATE_PHASE[pm.window_state(w, ref)["state"]], "n_met": n["n_achieved"], "n_chain": None})
    return out


def _phase_cell(ph) -> str:
    """창 칸의 글. 끝난 달은 n명, 진행 중인 달은 n명 진행 중, 시작 전인 달은 숫자 없이 시작 전이다."""
    if ph.get("state") == "future":
        return "시작 전"
    if ph.get("n_met") is None:
        return ""
    return f"{ph['n_met']}명" + (" 진행 중" if ph.get("state") == "open" else "")


def window_floor(p, w):
    """그 창의 기준 금액. 창의 구간표(없으면 공통 구간표) 첫 구간의 문턱이다. 설정 값을 읽기만 한다."""
    t = pm.tiers_for(p, w)
    return t[0]["min_value"] if t else None


def sum_target(p, data=None):
    """합산 조건의 기준. 엔진이 sum_target 을 내면 그 값, 아니면 공통 구간표 첫 구간의 문턱이다. 합산 조건이 없으면 None."""
    if p["combine_rule"] not in pm.PAIRED:
        return None
    eng = (data or {}).get("sum_target") or ((data or {}).get("total") or {}).get("sum_target")
    return eng or (p["tiers"][0]["min_value"] if p["tiers"] else None)


def chain_text(p) -> str | None:
    """'연속으로 채우면 3개월 합계 80만원(10만, 30만, 40만), 합산으로는 100만원' 꼴의 설명. 창 기준을 더해 만든다."""
    if p["combine_rule"] not in pm.EACH_RULES or len(p["windows"]) < 2:
        return None
    floors = [window_floor(p, w) for w in p["windows"]]
    if any(f is None for f in floors):
        return None
    months = len(p["windows"])
    whole = all(w["date_from"][8:] == "01" and w["date_from"][:7] == w["date_to"][:7] and
                int(w["date_to"][8:]) == calendar.monthrange(int(w["date_to"][:4]), int(w["date_to"][5:7]))[1] for w in p["windows"])
    text = f"연속으로 채우면 {months}{'개월' if whole else '개 창'} 합계 {_short(sum(floors), p)}원({', '.join(_short(f, p) for f in floors)})"
    target = sum_target(p)
    if target:
        text += f", 합산으로는 {_short(target, p)}원"
    return text.replace("만원원", "만원").replace("건원", "건")


def org_phases(data, sel, ref) -> list:
    """조직별 표의 칸. [{key, label, sub, state, group}] 순서는 달(창)마다 대상자, 추가안 달마다 대상자, 합산 달성자, 최종 달성이다.
    대표님이 정한 표 모양이다(11월 17일). 줄마다 cells 에 같은 순서의 글을 붙인다. 숫자는 엔진 값(phases, alt, n_sum, n_achieved)을
    옮기기만 하고 화면에서 세지 않는다. 엔진 값이 없으면 칸을 비우고, 추가안(alt)이 없는 프로모션은 추가안 칸을 그리지 않는다.
    창이 하나뿐이면 달 칸을 그리지 않는다. 대상, 가동, 근접, 미가동, 달성률, 순위는 줄의 두 번째 줄과 '자세히' 에 둔다."""
    tot = data["total"]
    phases = _phase_list(tot, data, ref) if len(sel["windows"]) > 1 else []
    alt = tot.get("alt") or data.get("alt")
    alt_phases = (alt or {}).get("phases") or []
    paired = sel["combine_rule"] in pm.PAIRED
    g_main = ("조건 1-1" if alt else "조건 1") if paired else ""       # 칸 묶음 이름은 원문대로 조건 1-1, 조건 1-2, 조건 2
    cols = []
    for ph, w in zip(phases, sel["windows"]):
        floor = ph.get("target") if ph.get("target") is not None else window_floor(sel, w)
        cols.append({"key": f"w{ph['seq']}", "label": f"{ph['label']} 대상자", "sub": (_short(floor, sel) if floor is not None else ""),
                     "state": ph["state"], "group": "main", "group_label": g_main})
    if alt:
        conf = next((c for c in (pm._r(sel).get("alt_chains") or []) if c.get("key") == alt.get("key")), {})
        wl = {w["seq"]: w["label"] for w in sel["windows"]}
        missed = [wl.get(int(x)) for x in conf.get("requires_missed") or [] if wl.get(int(x))]
        cols.append({"key": "a0", "label": (", ".join(missed) + " 미달성자") if missed else "앞 창 미달성자", "sub": "조건 1-2 대상", "state": None, "group": "alt",
                     "group_label": "조건 1-2", "short": "대상"})
    for ph in alt_phases:
        floor = ph.get("min_value")
        cols.append({"key": f"a{ph['seq']}", "label": f"{ph['label']} 대상자", "sub": (_short(floor, sel) if floor is not None else ""),
                     "state": ph.get("state"), "group": "alt", "group_label": "조건 1-2", "short": ph["label"]})
    target = sum_target(sel, data)
    if target:
        cols.append({"key": "sum", "label": f"합산 {_short(target, sel)} 달성자", "sub": "", "state": None, "group": "sum",
                     "group_label": "조건 2" if paired else ""})
    cols.append({"key": "final", "label": "최종 달성", "sub": "", "state": None, "group": "final", "group_label": ""})
    for i, c in enumerate(cols):                                     # 묶음 머리의 칸 수. 묶음의 첫 칸에만 둔다
        first = i == 0 or cols[i - 1]["group_label"] != c["group_label"]
        c["gspan"] = len(list(itertools.takewhile(lambda d, g=c["group_label"]: d["group_label"] == g, cols[i:]))) if first else 0

    def cells(x):
        out = [_phase_cell(ph) for ph in (_phase_list(x, data, ref) if phases else [])]
        out += [""] * (len(phases) - len(out))
        if alt:
            n_el = (x.get("alt") or {}).get("n_eligible")
            out.append(f"{n_el}명" if n_el is not None else "")
        xa = (x.get("alt") or {}).get("phases") or []
        for i, _ph in enumerate(alt_phases):
            out.append(_phase_cell(xa[i]) if i < len(xa) else "")
        if target:
            out.append(f"{x['n_sum']}명" if x.get("n_sum") is not None else "")
        out.append(f"{x['n_achieved']}명")
        return out
    alt_ix = [i for i, c in enumerate(cols) if c["group"] == "alt"]
    for r in [*data["rows"], tot]:
        r["cells"] = cells(r)
        r["alt_line"] = " · ".join(f"{cols[i]['short']} {r['cells'][i] or '-'}" for i in alt_ix)    # 휴대폰에서 조건 1-2 를 둘째 줄로
    return cols


def _short(v, p) -> str:
    """40만, 100만 처럼 짧은 금액 글. 건수 지표면 n건이다."""
    if p["metric"] == "policy_count":
        return f"{v:g}건"
    return f"{v / 10_000:g}만" if v % 10_000 == 0 else f"{int(v):,}"


# ---------------------------------------------------------------- 숫자를 누르면 나오는 명단 (who)
# 조직별 표의 인원 숫자마다 명단 열쇠가 있다. 명단은 그 줄(row, 비면 합계)의 대상자(엔진 org_progress 의 agent_ids) 가운데
# 엔진이 사람마다 낸 결과(pm.progress)에서 엔진이 인원을 셀 때와 같은 조건을 채운 사람이다. 숫자와 명단 인원 수가 같다(시험).
# 범위 안 설계사만 싣고, 금액은 싣지 않는다. 실적과 남은 금액은 팀원 달성 현황과 같은 규칙이다.
WHO_KO = {"target": "대상", "active": "가동", "near": "구간 근접", "inactive": "미가동", "final": "최종 달성", "missed": "미달성",
          "sum": "합산 달성", "chain": "연속 유지 중", "alt_el": "조건 1-2 대상"}
STATUS_LIST_KO = {"met": "달성", "open": "진행 중", "miss": "미달성", "future": "시작 전"}


def cell_who(col_key: str) -> str | None:
    """표 칸 열쇠를 명단 열쇠로. w2 는 w2, a0 는 alt_el, a2 는 alt_w2, sum, final 은 그대로다."""
    if col_key == "a0":
        return "alt_el"
    if col_key.startswith("a") and col_key[1:].isdigit():
        return f"alt_w{col_key[1:]}"
    return col_key


def _who_rule(sel, data, who, pcts):
    """(제목, 조건 함수(agent_id, 결과), 볼 값 함수). 엔진이 org_progress 에서 세는 조건과 같다. 모르는 열쇠면 None."""
    seqs = [w["seq"] for w in data["windows"]]
    tot = data["total"]
    phases = {ph["seq"]: ph for ph in (tot.get("phases") or [])}
    if who.startswith("w") and who[1:].isdigit() and int(who[1:]) in seqs:
        seq = int(who[1:])
        i = seqs.index(seq)
        ph = phases.get(seq) or {}
        if ph.get("state") == "future" or ph.get("target") is None:
            return None
        floor = ph["target"]
        title = f"{data['windows'][i]['label']} 기준({_short(floor, sel)}) 채운 사람"
        pick = lambda a, r: bool(r["windows"][i]["tier"])
        view = lambda r: (r["windows"][i]["value"], r["windows"][i].get("plain", r["windows"][i]["value"]), floor,
                          "met" if r["windows"][i]["tier"] else ("open" if ph.get("state") == "open" else "miss"))
        return title, pick, view
    if who.startswith("alt_w") and who[5:].isdigit():
        alt = tot.get("alt")
        if not alt:
            return None
        seq = int(who[5:])
        j = next((k for k, ph in enumerate(alt["phases"]) if ph["seq"] == seq), None)
        if j is None or alt["phases"][j].get("state") == "future":
            return None
        ph = alt["phases"][j]

        def alt_of(r):
            alts = (r.get("paths") or {}).get("alts") or []
            return next((x for x in alts if x["key"] == alt["key"]), None)
        pick = lambda a, r: bool(alt_of(r) and alt_of(r)["eligible"] and alt_of(r)["windows"][j]["met"])
        view = lambda r: (alt_of(r)["windows"][j]["value"], None, ph["min_value"],
                          "met" if alt_of(r)["windows"][j]["met"] else ("open" if ph.get("state") == "open" else "miss"))
        return f"조건 1-2 {ph['label']} 기준({_short(ph['min_value'], sel)}) 채운 사람", pick, view
    final_view = lambda r: (r["value"], None, None, "met" if pm.is_achieved(sel, r) else ("miss" if r["status"] == "missed" else "open"))
    if who == "alt_el":
        alt = tot.get("alt")
        if not alt:
            return None
        pick = lambda a, r: any(x["key"] == alt["key"] and x["eligible"] for x in ((r.get("paths") or {}).get("alts") or []))
        return WHO_KO[who], pick, final_view
    if who == "sum":
        target = tot.get("sum_target")
        if not target:
            return None
        return (f"합산 {_short(target, sel)} 채운 사람", lambda a, r: r["value"] >= target,
                lambda r: (r["value"], None, target, "met" if r["value"] >= target else "open"))
    rules = {"target": lambda a, r: True, "active": lambda a, r: r["value"] > 0, "inactive": lambda a, r: not r["value"] > 0,
             "near": lambda a, r: pm.near(r, float(pcts.get(a, 30)) / 100.0), "final": lambda a, r: pm.is_achieved(sel, r),
             "missed": lambda a, r: not pm.is_achieved(sel, r)}
    if who in rules:
        return WHO_KO[who], rules[who], final_view
    return None


def who_roster(con, ctx, sel, ref, data, phases, who, row_key):
    """명단 하나. row_key 가 표의 줄(path_key)이면 그 줄만, 비거나 all 이면 모든 줄이다. 없는 열쇠나 줄이면 None."""
    rows = data["rows"] if row_key in (None, "", "all") else [r for r in data["rows"] if r["path_key"] == row_key and not r["direct"]] or \
        [r for r in data["rows"] if r["path_key"] == row_key]
    if not rows:
        return None
    ids = [a for r in rows for a in r["agent_ids"]]
    pcts = near_pcts(con, ctx, ids)
    rule = _who_rule(sel, data, who, pcts)
    if rule is None:
        return None
    title, pick, view = rule
    res = pm.progress(con, sel, ids, ref)
    scope = {a["agent_id"]: a for a in _scope_agents(con, ctx)}         # 범위 안 사람만. org_progress 도 같은 목록으로 셌다
    groups, n = [], 0
    for r in rows:
        people = []
        for a in r["agent_ids"]:
            if a not in scope or not pick(a, res[a]):
                continue
            value, plain, floor, st = view(res[a])
            left = max(floor - value, 0) if floor is not None and st != "met" else None
            people.append({"agent_id": a, "code": scope[a]["code"], "name": scope[a]["name"], "branch": scope[a]["org"] or "-",
                           "value": value, "plain": plain if plain is not None and plain != value else None, "floor": floor, "left": left,
                           "status": st, "status_ko": STATUS_LIST_KO[st]})
        people.sort(key=lambda x: (-x["value"], x["code"]))
        n += len(people)
        groups.append({"title": "바로 소속" if r["direct"] else org_labels.title(r), "path_key": r["path_key"], "people": people})
    return {"who": who, "title": title, "row": row_key or "all", "n": n, "groups": [g for g in groups if g["people"]],
            "summary": [(g["title"], len(g["people"])) for g in groups if g["people"]], "estimate_note": pm.ESTIMATE_NOTE}


def _org_view(request, con, ctx, cards, head, p, org_key, direct, who=None, row_key=None):
    is_main = ctx["role"] == "main"
    starts = _org_starts(con, ctx)
    sel = next((c for c in cards if c["promo_id"] == p), cards[0] if cards else None)
    ref = sel["ref"] if sel else date.fromisoformat(head["ref"])
    gates = _gates(con, cards)
    org = _org_row(con, org_key) if org_key else (starts[0] if starts else None)
    if org_key and (not org or org["org_id"] not in ctx["org_ids"]):     # 범위 밖이나 없는 조직
        auth.log(con, ctx["login_id"], "access_denied", "/promo/progress org")
        raise HTTPException(403, "볼 수 없는 조직입니다.")
    data, crumbs, people, people_summary, top = None, [], None, None, None
    if sel and org:
        data = org_numbers(con, ctx, sel, ref, org["path_key"], keep_amounts=is_main)
        crumbs = _crumbs(con, org, starts)
        kids = [r for r in data["rows"] if not r["direct"]]
        has_children = bool(con.execute("SELECT 1 FROM org_unit WHERE parent_id=? LIMIT 1", (org["org_id"],)).fetchone())
        if direct or not has_children:                             # 맨 아래 조직이거나 바로 소속인 사람 줄을 누르면 사람 목록
            members = [a for r in data["rows"] if (r["direct"] or not direct) for a in r["agent_ids"]]
            people, people_summary = _team_rows(con, ctx, sel, ref, only=members)
        for r in data["rows"]:
            r["level_ko"] = org_labels.level_name(r)                    # 화면 계층 이름. config/org_labels.yaml
            r["title"] = org_labels.title(r)
            r["href"] = f"/promo/progress?view=org&p={sel['promo_id']}&org={quote(r['path_key'])}" + ("&direct=1" if r["direct"] else "")
        data["kids"] = kids
        screen = promo_extra.screen(sel)
        if is_main and screen["top_org_reward"]:                  # 최다 달성 조직은 메인 관리자에게만. 맨 위 조직의 바로 아래끼리 견준다
            root = starts[0] if starts else org
            parent = pm._parent(con, sel)                         # 딸린 시책은 부모 프로모션의 달성 인원으로 견준다 (가정)
            base = (data if root["org_id"] == org["org_id"] and not parent
                    else org_numbers(con, ctx, parent or sel, ref, root["path_key"], keep_amounts=False))
            firsts = [r for r in base["rows"] if not r["direct"] and r["rank"] == 1]
            for r in firsts:
                r["title"] = org_labels.title(r)
            top = {"reward": screen["top_org_reward"], "rows": firsts, "level_ko": org_labels.level_name(firsts[0]) if firsts else "",
                   "by_parent": parent["name"] if parent else None}
    phases = org_phases(data, sel, ref) if data else []
    roster = None
    if data and who:
        roster = who_roster(con, ctx, sel, ref, data, phases, who, row_key)
        if roster is None:
            raise HTTPException(404, "없는 명단입니다.")
    if sel:
        sel["versions"] = promo_extra.versions(con, sel)
        sel["chain_text"] = chain_text(sel)
    return page(request, ctx, "promo_progress_org.html", cards=cards, sel=sel, org=org, data=data, crumbs=crumbs, starts=starts, people=people,
                summary=people_summary, direct=direct, top=top, gates=gates, ref=ref.isoformat(), show_amount=is_main, head=head,
                phases=phases, roster=roster, who=who, row_key=row_key, cell_who=cell_who,
                root_path=_q(con, "SELECT path_key FROM org_unit WHERE parent_id IS NULL ORDER BY org_id LIMIT 1")[0]["path_key"] if is_main else None,
                assumption=ORG_ASSUMPTION, org_level=org_labels.level_name, org_title=org_labels.title)


# ---------------------------------------------------------------- 비교
def _cat_names(con) -> dict:
    return {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM product_category ORDER BY sort_order")}


@router.get("/promo/compare", response_class=HTMLResponse)
def promo_compare(request: Request, cat: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    res = pm.compare(con, ctx["ym"])
    show_original = ctx["role"] == "main"                       # D-21: 원안은 메인 관리자만
    rows = res["rows"] if show_original else [{**r, "plan": {"distributed": r["plan"]["distributed"]}} for r in res["rows"]]
    names = _cat_names(con)
    present = [c for c in names if any(c in r["category_list"] for r in rows)]
    sel_cat = cat if cat in present else None
    if sel_cat:                                                 # 구분을 정하지 않은 줄은 모든 구분에 걸리므로 남긴다
        rows = [r for r in rows if not r["category_list"] or sel_cat in r["category_list"]]
    groups = [{"metric": m, "metric_ko": ko, "rows": [r for r in rows if r["metric"] == m]} for m, ko in pm.METRIC_KO.items()]
    groups = [g for g in groups if g["rows"]]
    for g in groups:
        g["top"] = max((r["per_unit"] or 0 for r in g["rows"]), default=0)
    return page(request, ctx, "promo_compare.html", cmp_groups=groups, n=len(rows), ref=res["ref"], unit=res["unit"], cats=[(c, names[c]) for c in present],
                sel_cat=sel_cat, show_original=show_original, estimate_note=res["estimate_note"], plan_note=res["plan_note"], cat_names=names)


# ---------------------------------------------------------------- 지난 프로모션과 결과 확정
HIDDEN_FOR_MANAGER = ("amount", "reward", "r", "original_amount")   # D-17: 관리자에게 팀원의 시상 금액과 보상 글을 넘기지 않는다
HIDDEN_ORIGINAL = ("original_amount",)                               # D-21: 원안 금액은 메인 관리자에게만
CONFIRM_WARN = ("확정하면 지금의 사람별 결과와 순위가 그대로 남고, 그 뒤에 계약이 바뀌어도 이 프로모션의 결과는 다시 계산하지 않습니다. "
                "무르기는 메인 관리자가 사유를 적어야만 할 수 있고 무른 기록도 남습니다.")
REVOKE_WARN = ("무르면 이 확정은 지우지 않고 무른 기록으로 남습니다. 화면은 다시 계산한 마감 전 예상으로 돌아가고 다시 확정할 수 있습니다. "
               "이 확정에 달린 13회차 판정도 함께 보이지 않습니다.")
REVOKE_ASSUMPTION = "확정 무르기는 D-50 제안대로 둔 가정입니다."
ROSTER_ASSUMPTION = "명단을 보는 범위는 D-20 제안대로 둔 가정입니다."
INST13_ASSUMPTION = "13회차 미유지 처리는 D-59 제안대로 둔 가정입니다."
# 13회차 판정에서 관리자에게 넘기지 않는 칸 (D-17). 메인 관리자가 아니면 원안 금액도 넘기지 않는다 (D-21)
I13_MONEY = ("amount", "amount_kept", "original_amount", "original_amount_kept", "clawback_amount", "reward_kept")
I13_ORIGINAL = ("original_amount", "original_amount_kept")
I13_CONTRACT_KEEP = ("policy_tail", "due_date", "judgement", "judgement_ko")    # 계약 목록은 증권번호 끝 네 자리, 도래일, 판정만


def _who_name(con, login_id) -> str | None:
    if not login_id:
        return None
    r = con.execute("SELECT display_name FROM user_account WHERE login_id=?", (login_id,)).fetchone()
    return (r["display_name"] if r and r["display_name"] else login_id)


def _end_ym(p) -> str:
    return p["date_to"][:4] + p["date_to"][5:7]


def _inst13_view(con, p, ids, today, *, money: bool, original: bool) -> dict:
    """13회차 판정을 화면에 넘길 꼴로. 금액을 볼 수 없는 사람에게는 금액 칸을 떼고 넘긴다. 계약은 세 칸만 남긴다."""
    res = pm.inst13_results(con, p, ids, today)
    drop = set() if money else set(I13_MONEY)
    if not original:
        drop |= set(I13_ORIGINAL)
    rows = []
    for x in res["rows"]:
        y = {k: v for k, v in x.items() if k not in drop and k != "contracts"}
        y["contracts"] = [{k: c.get(k) for k in I13_CONTRACT_KEEP} for c in x["contracts"]]
        rows.append(y)
    summary = {k: v for k, v in res["summary"].items() if money or k not in ("amount", "amount_kept", "clawback_amount")}
    ready = sum(1 for x in res["rows"] if x["settlement"] == "estimate" and x["decision"] != "pending")
    return {"available": res["available"], "note": res["note"], "judge_note": res["judge_note"], "rows": rows, "summary": summary, "ready": ready,
            "money": money}


@router.get("/promo/archive", response_class=HTMLResponse)
def promo_archive(request: Request, ym: str | None = None, p: int | None = None, view: str | None = None, confirm: int | None = None,
                  revoke: int | None = None, inst13: int | None = None, msg: str | None = None, err: str | None = None,
                  ctx: dict = Depends(guard), con=Depends(get_con)):
    today = date.today()
    months = sorted({x["period_ym"] for x in pm.ended(con, ref=today)}, reverse=True)
    sel_ym = ym if ym in months else (months[0] if months else None)
    role = ctx["role"]
    is_main = role == "main"
    mine = role == "agent" or (role == "manager" and view == "me")
    if mine:
        ids = [ctx["agent_id"]] if ctx["agent_id"] else []
    else:
        ids = [a["agent_id"] for a in _scope_agents(con, ctx)]
    results = pm.past(con, ids, ym=sel_ym, ref=today) if sel_ym and ids else []
    show_amount = is_main or mine
    for i, res in enumerate(results):
        promo = res["promo"]
        kind_reward = pm.in_kind(promo)
        if kind_reward and not is_main and not mine:            # D-20 가정: 여행과 물품의 달성 여부는 본인, 직속 관리자, 메인 관리자만
            res = results[i] = pm.final_results(con, promo, pm.roster_viewable(con, ctx, ids, _end_ym(promo)), today)
        res["in_kind"] = kind_reward
        if mine:                                                # 본인 줄만 남긴다
            res["rows"] = [x for x in res["rows"] if x["agent_id"] == ctx["agent_id"]]
        if not show_amount:
            hidden, hidden_sum = HIDDEN_FOR_MANAGER, ("amount", "original_amount")
        else:
            hidden = hidden_sum = HIDDEN_ORIGINAL if not is_main else ()
        res["rows"] = [{k: v for k, v in x.items() if k not in hidden} for x in res["rows"]]
        res["summary"] = {k: v for k, v in res["summary"].items() if k not in hidden_sum}
        if res["revoked"] and not is_main:                      # 무른 사유와 무른 사람은 메인 관리자에게만
            res["revoked"] = {k: v for k, v in res["revoked"].items() if k not in ("reason", "revoked_by")}
        res["period_text"] = _period_text(promo)
        res["confirmed_name"] = _who_name(con, res["confirmed_by"])
        if mine and promo["inst13"]:
            res["i13"] = _inst13_view(con, promo, ids, today, money=True, original=False)
    sel = None
    if not mine and results:
        sel = next((x for x in results if x["promo"]["promo_id"] == p), results[0])
    history, roster = [], None
    if sel:
        promo = sel["promo"]
        if is_main:
            sel["revoked_name"] = _who_name(con, (sel["revoked"] or {}).get("revoked_by"))
            history = [{**h, "confirmed_name": _who_name(con, h["confirmed_by"]), "revoked_name": _who_name(con, h["revoked_by"])}
                       for h in pm.confirm_history(con, promo["promo_id"])]
        if sel["in_kind"]:
            roster = pm.travel_roster(con, promo, ctx, ids, today)
        if promo["inst13"]:
            seen = [x["agent_id"] for x in sel["rows"]]          # 범위 안이고 이 화면이 보여 주는 사람만. 여행과 물품이면 D-20 범위다
            sel["i13"] = _inst13_view(con, promo, seen, today, money=is_main, original=is_main)
    choice = None
    if is_main and sel and roster is not None:                   # 보상 선택과 참석 기록. 메인 관리자만, 확정한 달성자만
        sc = promo_extra.screen(sel["promo"])
        if sc["reward_choices"] or promo_extra.is_travel(sel["promo"]):
            got = promo_extra.choices(con, sel["promo"]["promo_id"])
            choice = {"options": [(k, promo_extra.CHOICE_KO[k]) for k in sc["reward_choices"]], "travel": promo_extra.is_travel(sel["promo"]),
                      "ready": sel["settlement"] == "confirmed", "rows": [{**x, **{k: (got.get(x["agent_id"]) or {}).get(k) for k in ("choice", "attend")}}
                                                                       for x in roster["rows"]],
                      "n_done": sum(1 for x in roster["rows"] if (got.get(x["agent_id"]) or {}).get("choice") or (got.get(x["agent_id"]) or {}).get("attend")),
                      "cash_note": promo_extra.CASH_NOTE if sc["reward_choices"] else None, "CHOICE_KO": promo_extra.CHOICE_KO,
                      "ATTEND_KO": promo_extra.ATTEND_KO}
    sid = sel["promo"]["promo_id"] if sel else None
    ask = sel if (is_main and sel and confirm == sid and sel["settlement"] == "estimate") else None
    ask_revoke = sel if (is_main and sel and revoke == sid and sel["settlement"] == "confirmed") else None
    ask13 = sel if (is_main and sel and inst13 == sid and sel.get("i13") and sel["i13"]["available"] and sel["i13"]["ready"]) else None
    n_done = sum(1 for x in results if x["settlement"] == "confirmed")
    return page(request, ctx, "promo_archive.html", months=months, sel_ym=sel_ym, results=results, sel=sel, mine=mine, ask=ask, msg=msg, err=err,
                ask_revoke=ask_revoke, ask13=ask13, history=history, roster=roster, is_main=is_main, choice=choice,
                show_amount=show_amount, can_confirm=is_main, today=today.isoformat(), n_done=n_done, n_open=len(results) - n_done,
                SETTLE_KO=pm.SETTLE_KO, confirm_warn=CONFIRM_WARN, revoke_warn=REVOKE_WARN, revoke_min=pm.REVOKE_MIN,
                revoke_assumption=REVOKE_ASSUMPTION, roster_assumption=ROSTER_ASSUMPTION, inst13_assumption=INST13_ASSUMPTION)


ARCHIVE_ACTIONS = ("confirm", "revoke", "inst13", "reward_choice")


@router.post("/promo/archive")
async def promo_archive_confirm(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    """결과 확정, 확정 무르기, 13회차 판정 확정. 모두 메인 관리자만 한다. 확인 칸(agree=yes)이 없으면 하지 않는다.
    무르기는 사유를 pm.REVOKE_MIN 자 이상 적어야 한다. 엔진(pm.confirm_results, revoke_confirmation, confirm_inst13)이 계정 표에서 다시 확인한다."""
    f = await request.form()
    action = f.get("action")
    if ctx["role"] != "main":
        auth.log(con, ctx["login_id"], "access_denied", f"/promo/archive {action if action in ARCHIVE_ACTIONS else 'post'}")
        raise HTTPException(403, "메인 관리자만 결과를 확정하거나 무릅니다.")
    raw = str(f.get("promo_id") or "")
    if action not in ARCHIVE_ACTIONS or not raw.isdigit():
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    pid = int(raw)
    ym = str(f.get("ym") or "")
    back = f"/promo/archive?{'ym=' + quote(ym) + '&' if ym.isdigit() else ''}p={pid}"
    if action == "reward_choice":
        return _save_choices(con, ctx, f, pid, back)
    step = {"confirm": ("confirm", "confirm"), "revoke": ("revoke", "revoke"), "inst13": ("inst13", "inst13-confirm")}[action]
    again = f"{back}&{step[0]}={pid}"
    if f.get("agree") != "yes":
        word = {"confirm": "확정합니다", "revoke": "무릅니다", "inst13": "남깁니다"}[action]
        return RedirectResponse(f"{again}&err={quote('확인하는 칸에 표시해야 ' + word + '.')}#{step[1]}", status_code=303)
    try:
        if action == "confirm":
            res = pm.confirm_results(con, pid, ctx, ref=date.today())
            done = f"결과 {res['summary']['n']}명을 확정했습니다."
        elif action == "revoke":
            pm.revoke_confirmation(con, pid, ctx, str(f.get("reason") or ""))
            done = "확정을 물렀습니다. 지금은 다시 계산한 마감 전 예상이 보입니다."
        else:
            res = pm.confirm_inst13(con, pid, ctx, ref=date.today())
            done = f"13회차 판정 {res['n_new']}명을 남겼습니다." + (f" 판정 전 {res['summary']['pending']}명은 다음에 남깁니다." if res["summary"]["pending"] else "")
    except PermissionError as e:
        auth.log(con, ctx["login_id"], "access_denied", f"/promo/archive {action}")
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        target = f"{again}&err={quote(str(e))}#{step[1]}" if action == "revoke" else f"{back}&err={quote(str(e))}"
        return RedirectResponse(target, status_code=303)
    return RedirectResponse(f"{back}&msg={quote(done)}" + ("#inst13" if action == "inst13" else ""), status_code=303)


def _save_choices(con, ctx, f, pid, back) -> RedirectResponse:
    """보상 선택과 참석 기록. 메인 관리자인지는 부르는 쪽이 이미 봤다. 확정한 결과의 달성자만 받는다.
    달성자는 pm.travel_roster 가 확정 결과에서 낸 명단이다. 사람은 사번으로 변경 기록에 남는다."""
    p = pm._load(con, pid)
    today = date.today()
    if not p or p["status"] != "published" or p["kind"] == "rate" or date.fromisoformat(p["date_to"]) >= today:
        return RedirectResponse(f"{back}&err={quote('끝난 프로모션에만 기록합니다.')}#reward-choice", status_code=303)
    if not pm.confirmation(con, pid):
        return RedirectResponse(f"{back}&err={quote('결과를 확정한 뒤에 기록합니다.')}#reward-choice", status_code=303)
    roster = pm.travel_roster(con, p, ctx, [a["agent_id"] for a in _scope_agents(con, ctx)], today)
    allowed = {x["agent_id"]: x["code"] for x in roster["rows"]}
    picks = {}
    for key in f.keys():
        for prefix in ("choice_", "attend_"):
            if key.startswith(prefix) and key[len(prefix):].isdigit():
                aid = int(key[len(prefix):])
                picks.setdefault(aid, (str(f.get(f"choice_{aid}") or ""), str(f.get(f"attend_{aid}") or "")))
    try:
        n = promo_extra.record(con, p, by=ctx["login_id"], allowed=allowed, picks=picks)
    except ValueError as e:
        return RedirectResponse(f"{back}&err={quote(str(e))}#reward-choice", status_code=303)
    done = f"{n}명의 보상 선택과 참석 여부를 남겼습니다." if n else "바뀐 것이 없습니다."
    return RedirectResponse(f"{back}&msg={quote(done)}#reward-choice", status_code=303)


@router.get("/promo/publish", response_class=HTMLResponse)
def promo_publish(request: Request, msg: str | None = None, err: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    doc = con.execute("SELECT plan_doc_id, file_name, period_ym FROM incentive_plan_doc WHERE status!='superseded' ORDER BY period_ym=? DESC, plan_doc_id DESC LIMIT 1", (ctx["ym"],)).fetchone()
    lines = _q(con, """SELECT l.line_id, l.insurer_name, l.category_raw, l.period_raw, l.period_from, l.criteria_raw, l.reward_raw, l.line_kind, l.parse_status, l.parse_note,
                              (SELECT p.promo_id FROM promotion p WHERE p.source_line_id=l.line_id AND p.status!='withdrawn' LIMIT 1) promo_id
                       FROM incentive_plan_line l WHERE l.plan_doc_id=? AND l.recipient_level='fc' ORDER BY l.line_id""", (doc["plan_doc_id"],)) if doc else []
    for ln in lines:
        ln["direct"] = ln["line_kind"] == "rate_promo" and ln["parse_status"] == "auto" and bool(ln["period_from"])
    cards, _ = _cards(con, ctx, include_unpublished=True)
    cats = _q(con, "SELECT code, name FROM product_category ORDER BY sort_order")
    return page(request, ctx, "promo_publish.html", doc=dict(doc) if doc else None, lines=lines, cards=cards, cats=cats, msg=msg, err=err,
                waiting=sum(1 for ln in lines if not ln["promo_id"]))


@router.post("/promo/publish")
async def promo_publish_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    f = await request.form()
    action, by = f.get("action"), ctx["login_id"]
    try:
        if action == "publish_line":
            pm.publish_from_line(con, int(f["line_id"]), by, categories=[f["category"]] if f.get("category") else None)
            msg = "승인하고 게시했습니다."
        elif action in ("published", "withdrawn"):
            pm.set_status(con, int(f["promo_id"]), action, by)
            msg = "게시했습니다." if action == "published" else "회수했습니다."
        elif action == "delete":
            pm.delete_draft(con, int(f["promo_id"]))
            msg = "지웠습니다."
        else:
            raise ValueError("처리할 수 없는 요청입니다.")
    except (ValueError, KeyError) as e:
        return RedirectResponse(f"/promo/publish?err={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/promo/publish?msg={quote(msg)}", status_code=303)


WINDOW_SEQS = (1, 2, 3)                        # 입력 폼의 기간 창 수
TIER_ROWS = range(1, 7)                         # 구간표 한 장의 줄 수
WINDOW_TIERS_NOTE = ("창별 구간표는 창이 둘 이상이고 결합 규칙이 창마다 달성일 때만 고를 수 있습니다. 조건 1과 조건 2를 묶는 결합(모두, 하나)에서도 "
                     "창별 구간표가 조건 1입니다. 창마다 닿은 구간 가운데 낮은 쪽을 적용하고, "
                     "보상은 위의 공통 구간표에서 읽습니다. 공통 구간표를 비우면 마지막 창의 구간표에서 읽습니다. 모든 표의 구간 수가 같아야 합니다.")
WINDOW_TIERS_ASSUMPTION = "보상을 읽는 표는 D-60 제안대로 둔 가정입니다."
INST13_FORM_NOTE = "켜면 13회차까지 유지한 계약만 인정합니다. 지급 시점에 13회차가 들면 이 칸과 상관없이 13회차 조건이 붙습니다."


RULES_NOTE = ("조건 확장 칸은 비워 두면 예전처럼 셉니다. 청약 기준으로 세려면 기준일도 청약일자로 둡니다. 조건 1과 조건 2를 묶는 결합(모두, 하나)은 "
              "창별 구간표가 조건 1, 위의 공통 구간표가 조건 2입니다. 딸린 시책은 부모 프로모션의 실적과 구간을 그대로 씁니다.")
RULES_ASSUMPTION = "가중 인정 한도의 범위, 상품 이름이 빈 계약, 본인·가족 계약, 품보를 해지로 보는 것, 대상 직급, 조직 문턱, 유지율 표시는 제안대로 둔 가정입니다 (D-84 에서 D-92)."
STATUS_KO = {"applied": "청약", "inforce": "정상", "lapsed": "실효", "cancelled": "해지(품보 포함)", "matured": "만기"}


def _rules_view(r) -> dict:
    """조건 확장 값을 폼 칸 꼴로. 유지율은 퍼센트로, 상품 이름은 줄바꿈 글로, 가중 날짜 목록은 '시작~끝' 줄로 보인다."""
    b = r.get("boost") or {}
    return {"count_basis": r.get("count_basis") or "issued", "boost_factor": num_text(b.get("factor")),
            "boost_day_from": (b.get("month_days") or ["", ""])[0], "boost_day_to": (b.get("month_days") or ["", ""])[1],
            "boost_ranges": "\n".join(f"{f}~{t}" for f, t in (b.get("ranges") or [])), "boost_cap": num_text(b.get("cap")),
            "boost_cap_scope": b.get("cap_scope") or "contract", "boost_applies": b.get("applies_to") or "all",
            "include": "\n".join(r.get("product_include") or []), "exclude": "\n".join(r.get("product_exclude") or []),
            "exclude_self": bool(r.get("exclude_self")), "drop": list(r.get("drop_statuses") or []), "parent": r.get("parent_promo_id"),
            "levels": ", ".join(r.get("target_levels") or []), "gate_min": r.get("gate_min_achievers") or "", "gate_org": r.get("gate_org_path") or "",
            "ret_min": num_text(r["retention_min"] * 100) if r.get("retention_min") else "", "ret_ref": r.get("retention_ref") or "",
            "ret_basis": r.get("retention_basis") or "count", "alt": _alt_view(r)}


def _rules_from_form(f, num) -> dict:
    """폼의 조건 확장 칸을 pm.create, pm.update 의 rules 꼴로. 검사는 엔진(pm._clean_rules)이 한다."""
    factor = num(f.get("r_boost_factor"))
    boost = None
    if factor:
        boost = {"factor": factor, "cap": num(f.get("r_boost_cap")), "cap_scope": f.get("r_boost_cap_scope") or "contract",
                 "applies_to": f.get("r_boost_applies") or "all"}
        lines = [x.strip() for x in str(f.get("r_boost_ranges") or "").splitlines() if x.strip()]
        if lines:
            try:
                boost["ranges"] = [[a.strip(), b.strip()] for a, b in (x.split("~", 1) for x in lines)]
            except ValueError as e:
                raise ValueError("가중 기간의 날짜 목록은 줄마다 '시작일~종료일' 로 넣습니다.") from e
        elif f.get("r_boost_day_from") and f.get("r_boost_day_to"):
            boost["month_days"] = [int(f.get("r_boost_day_from")), int(f.get("r_boost_day_to"))]
    parent = str(f.get("r_parent") or "")
    return {"count_basis": f.get("r_count_basis") or "issued", "boost": boost,
            "product_include": str(f.get("r_include") or ""), "product_exclude": str(f.get("r_exclude") or ""),
            "exclude_self": f.get("r_exclude_self") == "1", "drop_statuses": f.getlist("r_drop"),
            "parent_promo_id": int(parent) if parent.isdigit() else None,
            "target_levels": [x for x in str(f.get("r_levels") or "").replace(",", " ").split() if x],
            "gate_min_achievers": str(f.get("r_gate_min") or "").strip() or None, "gate_org_path": f.get("r_gate_org") or None,
            "retention_min": str(f.get("r_ret_min") or "").strip() or None, "retention_ref": f.get("r_ret_ref") or None,
            "retention_basis": f.get("r_ret_basis") or "count", **_alt_from_form(f, num)}


ALT_SUPPORTED = "alt_chains" in pm.RULE_DEFAULTS                  # 추가안(alt_chains)은 엔진이 받을 때만 폼에 둔다


def _alt_from_form(f, num) -> dict:
    """추가안 칸 하나를 엔진의 alt_chains 꼴 [{key, label, windows: {창 순번: 기준}, requires_missed: [창 순번]}] 로 옮긴다.
    기준을 모두 비우면 추가안을 지운다. 검사는 엔진(pm._clean_alts)이 한다."""
    if not ALT_SUPPORTED:
        return {}
    wins = {seq: num(f.get(f"r_alt_min_{seq}")) for seq in WINDOW_SEQS}
    wins = {seq: v for seq, v in wins.items() if v is not None}
    missed = [int(x) for x in f.getlist("r_alt_missed") if str(x).isdigit()]
    if not wins:
        return {"alt_chains": []}
    return {"alt_chains": [{"key": "alt1", "label": str(f.get("r_alt_label") or "").strip() or None, "windows": wins, "requires_missed": missed}]}


def _alt_view(r) -> dict:
    chains = r.get("alt_chains") or []
    c = chains[0] if chains else {}
    wins = {int(k): v for k, v in (c.get("windows") or {}).items()}
    return {"label": c.get("label") or "", "missed": [int(x) for x in c.get("requires_missed") or []],
            "floors": {seq: num_text(wins.get(seq)) for seq in WINDOW_SEQS}}


def _tier_form_defaults(con, ctx, line_id=None, promo_id=None):
    d = {"promo_id": "", "name": "", "kind": "weekly", "insurer_id": None, "insurer_name": "", "sector": "", "caution": "", "target_note": "",
         "metric": "premium_monthly", "date_basis": pm.DEFAULT_DATE_BASIS, "cap": "", "categories": [], "combine_rule": "", "pay_timing": "",
         "windows": [("", "", "")] * 3, "tiers": [("", "fixed", "", "")] * 6, "rates": [], "source_line_id": "", "origin": None, "status": None,
         "window_tiers": {seq: [("", "fixed", "", "")] * 6 for seq in WINDOW_SEQS}, "window_mode": False, "inst13_required": False, "inst13_from_timing": False,
         "rules": _rules_view(pm.RULE_DEFAULTS), "screen": {"top_org_reward": None, "reward_choices": []}}
    num = lambda v: "" if v is None else f"{v:g}"
    row = lambda t: (num(t["min_value"]), t["reward_kind"], num(t["reward_value"] * 100 if t["reward_kind"] == "rate" and t["reward_value"] is not None else t["reward_value"]),
                     t["reward_label"] or "")
    if promo_id:
        p = pm._load(con, promo_id)
        if not p:
            raise HTTPException(404, "없는 프로모션입니다.")
        tiers = [row(t) for t in p["tiers"] if t.get("window_seq") is None]        # 공통 구간표만. 창별 구간표는 아래 칸에 따로 채운다
        wt = p.get("window_tiers") or {}
        d.update(window_mode=bool(wt), inst13_required=bool(p.get("inst13_required")), inst13_from_timing=p["inst13"] and not p.get("inst13_required"),
                 window_tiers={seq: ([row(t) for t in wt.get(seq, [])] + [("", "fixed", "", "")] * 6)[:6] for seq in WINDOW_SEQS})
        d.update(rules=_rules_view(p["rules"]), screen=promo_extra.screen(p))
        d.update(promo_id=str(promo_id), name=p["name"], kind=p["kind"], insurer_id=p["insurer_id"], insurer_name=p["insurer_name"] or "", sector=p["sector"] or "",
                 caution=p["caution"] or "", target_note=p["target_note"] or "", metric=p["metric"], date_basis=p["date_basis"], cap=num(p["per_contract_cap"]),
                 categories=p["category_list"], combine_rule=p["combine_rule"] if len(p["windows"]) > 1 else "", pay_timing=p["pay_timing"] or "",
                 windows=([(w["label"], w["date_from"], w["date_to"]) for w in p["windows"]] + [("", "", "")] * 3)[:3],
                 tiers=(tiers + [("", "fixed", "", "")] * 6)[:6], rates=p["rates"], status=p["status"])
    elif line_id:
        ln = con.execute("SELECT * FROM incentive_plan_line WHERE line_id=? AND recipient_level='fc'", (line_id,)).fetchone()
        if ln:
            origin = " / ".join(x for x in (ln["period_raw"], ln["criteria_raw"], ln["reward_raw"]) if x)
            d.update(name=ln["category_raw"] or "", kind=pm.FROM_LINE_KIND.get(ln["line_kind"], "weekly") if ln["line_kind"] != "rate_promo" else "weekly",
                     insurer_name=ln["insurer_name"], sector=ln["sector"] or "", caution=ln["reward_raw"] or "", source_line_id=str(line_id), origin=origin,
                     windows=[("", ln["period_from"] or "", ln["period_to"] or ""), ("", "", ""), ("", "", "")])
    return d


# ---------------------------------------------------------------- 원안 (D-21)
# D-21 제안대로 지금 금액을 배포안으로 보고, 원안은 메인 관리자가 이 칸에 넣은 것만 쓴다. 비우면 원문 확인이다.
# 구간과 조건 입력은 메뉴에서 메인 관리자에게만 열려 있고, pm.set_original 이 계정 표에서 다시 메인 관리자인지 본다.
MONEY_KINDS = ("fixed", "rate")
num_text = lambda v: "" if v is None else f"{v:g}"


def original_rows(p) -> dict:
    """원안 입력 칸. 금액 구간(정액과 비율)과 지급률 조각마다 한 줄. 비율과 지급률은 퍼센트로 보이고 받는다."""
    tiers = [{"no": t["tier_no"], "min_value": t["min_value"], "kind": t["reward_kind"], "kind_ko": pm.REWARD_KO[t["reward_kind"]],
              "dist": num_text(t["reward_value"] * 100 if t["reward_kind"] == "rate" and t["reward_value"] is not None else t["reward_value"]),
              "orig": num_text(t["original_value"] * 100 if t["reward_kind"] == "rate" and t.get("original_value") is not None else t.get("original_value")),
              "unit": "%" if t["reward_kind"] == "rate" else "원"}
             for t in p["tiers"] if t["reward_kind"] in MONEY_KINDS]
    rates = [{"seq": r["seq"], "label": (f"{r['option_label']}번 조건 · " if r["option_label"] else "") + pm.timing_text(r["pay_timing"]),
              "dist": num_text(r["rate"] * 100 if r["rate"] is not None else None),
              "orig": num_text(r["original_rate"] * 100 if r.get("original_rate") is not None else None)} for r in p["rates"]]
    return {"tiers": tiers, "rates": rates, "has": pm.has_original(p), "missing": pm.MISSING_ORIGINAL}


@router.get("/promo/tiers", response_class=HTMLResponse)
def promo_tiers(request: Request, line: int | None = None, edit: int | None = None, err: str | None = None, msg: str | None = None,
                cleared: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    pm.ensure(con)                                              # 조건 확장 칸과 화면 칸이 없는 오래된 DB. 이미 있으면 건너뛴다
    promo_extra.ensure(con)
    d = _tier_form_defaults(con, ctx, line, edit)
    plan = original_rows(pm._load(con, edit)) if edit else None
    parents = _q(con, "SELECT promo_id, name, period_ym FROM promotion WHERE kind!='rate' AND parent_promo_id IS NULL AND status!='withdrawn' "
                      "AND promo_id!=? ORDER BY promo_id DESC", (edit or 0,))
    gate_orgs = [{"path_key": o["path_key"], "label": f"{org_labels.title(o)} · {org_labels.level_name(o)}"}
                 for o in _q(con, "SELECT org_id, name, level, path_key FROM org_unit WHERE active=1 ORDER BY path_key")]
    return page(request, ctx, "promo_tiers.html", d=d, err=err, msg=msg, plan=plan, window_seqs=WINDOW_SEQS, wt_note=WINDOW_TIERS_NOTE,
                wt_assumption=WINDOW_TIERS_ASSUMPTION, inst13_note=INST13_FORM_NOTE, parents=parents, gate_orgs=gate_orgs, rules_note=RULES_NOTE,
                rules_assumption=RULES_ASSUMPTION, STATUS_KO=STATUS_KO, DROPPABLE=pm.DROPPABLE, COUNT_BASIS_KO=pm.COUNT_BASIS_KO,
                CHOICE_KO=promo_extra.CHOICE_KO, copy_note=COPY_FORM_NOTE, cleared=[CLEARED_KO.get(c, c) for c in (cleared or "").split(",") if c],
                copied_from=_copied_from(con, edit), is_main=ctx["role"] == "main", alt_supported=ALT_SUPPORTED,
                cats=_q(con, "SELECT code, name FROM product_category ORDER BY sort_order"),
                insurers=_q(con, "SELECT insurer_id, name, sector FROM insurer WHERE active=1 ORDER BY name"))


def _save_original(con, ctx, f) -> RedirectResponse:
    """원안 칸 저장. 퍼센트로 받은 비율과 지급률은 100 으로 나눠 넘긴다. 메인 관리자가 아니면 403 이다."""
    promo_id = int(f.get("promo_id") or 0) if str(f.get("promo_id") or "").isdigit() else 0
    pct = lambda v: None if v is None else v / 100
    num = lambda v: float(str(v).replace(",", "").strip()) if str(v or "").strip() else None
    p = pm._load(con, promo_id)
    if not p:
        raise HTTPException(404, "없는 프로모션입니다.")
    try:
        tiers = {t["tier_no"]: (pct(num(f.get(f"o_tier_{t['tier_no']}"))) if t["reward_kind"] == "rate" else num(f.get(f"o_tier_{t['tier_no']}")))
                 for t in p["tiers"] if t["reward_kind"] in MONEY_KINDS}
        rates = {r["seq"]: pct(num(f.get(f"o_rate_{r['seq']}"))) for r in p["rates"]}
        res = pm.set_original(con, promo_id, by=ctx, tiers=tiers, rates=rates)
    except PermissionError as e:
        auth.log(con, ctx["login_id"], "access_denied", f"/promo/tiers original {promo_id}")
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        return RedirectResponse(f"/promo/tiers?edit={promo_id}&err={quote(str(e))}#original", status_code=303)
    msg = f"원안을 저장했습니다. {len(res['changed'])}칸을 변경 기록에 남겼습니다."
    return RedirectResponse(f"/promo/tiers?edit={promo_id}&msg={quote(msg)}#original", status_code=303)


CLEARED_KO = {"retention_ref": "13회차 유지율 판정 기준일", "boost": "가중 인정 날짜 목록", "original_value": "원안 금액"}
COPY_FORM_NOTE = ("다음 달 창 날짜를 넣으면 구간, 금액, 조건을 그대로 옮긴 초안을 만듭니다. 게시는 포스터와 맞춰 본 뒤 게시 관리에서 합니다. "
                  "그 달에 묶인 값(원안 금액, 13회차 유지율 기준일, 가중 인정 날짜 목록)은 옮기지 않습니다 (D-95 가정).")


def _copy_bridge(con, ctx, f) -> RedirectResponse:
    """브릿지 다음 달 복사. 메인 관리자만 쓴다(구간과 조건 입력은 메뉴에서 메인 관리자 화면이고, 여기서도 한 번 더 본다).
    창 날짜는 화면에서 받고 pm.copy_bridge 가 초안을 만든다. 옮기지 않은 값(cleared)은 새 초안 화면에 안내로 보인다."""
    src = int(f.get("promo_id") or 0) if str(f.get("promo_id") or "").isdigit() else 0
    if ctx["role"] != "main":
        auth.log(con, ctx["login_id"], "access_denied", f"/promo/tiers copy_bridge {src}")
        raise HTTPException(403, "브릿지 복사는 메인 관리자만 합니다.")
    back = f"/promo/tiers?edit={src}"
    try:
        ym = str(f.get("period_ym") or "").replace("-", "")
        n = len((pm._load(con, src) or {}).get("windows") or [])
        wins = [(str(f.get(f"c_label_{i}") or "").strip() or None, f.get(f"c_from_{i}") or "", f.get(f"c_to_{i}") or "") for i in range(1, n + 1)]
        if any(not a or not b for _lab, a, b in wins):
            raise ValueError("새 창의 시작일과 종료일을 모두 넣습니다.")
        res = pm.copy_bridge(con, src, period_ym=ym, windows=wins, by=ctx["login_id"], name=str(f.get("name") or "").strip() or None)
    except ValueError as e:
        return RedirectResponse(f"{back}&err={quote(str(e))}#copy", status_code=303)
    cleared = ",".join(res["cleared"])
    msg = "초안을 만들었습니다. " + res["note"]
    return RedirectResponse(f"/promo/tiers?edit={res['promo_id']}&msg={quote(msg)}" + (f"&cleared={quote(cleared)}" if cleared else ""), status_code=303)


def _copied_from(con, promo_id):
    """복사로 만든 초안이면 원본 {promo_id, name}. 칸이 없는 오래된 DB 나 새로 만든 것이면 None."""
    if not promo_id:
        return None
    try:
        r = con.execute("SELECT s.promo_id, s.name FROM promotion p JOIN promotion s ON s.promo_id=p.copied_from WHERE p.promo_id=?", (promo_id,)).fetchone()
    except sqlite3.OperationalError:                                         # copied_from 칸이 없는 DB
        return None
    return dict(r) if r else None


def _tier_table(f, prefix, num) -> list:
    """폼의 구간표 한 장. 기준이 빈 줄은 뺀다. 비율은 퍼센트로 받아 100 으로 나눈다. prefix 는 공통 표 't_', 창별 표 'wt2_' 꼴이다."""
    rows = [(num(f.get(f"{prefix}min_{i}")), f.get(f"{prefix}kind_{i}", "fixed"), num(f.get(f"{prefix}value_{i}")), f.get(f"{prefix}label_{i}", "").strip())
            for i in TIER_ROWS if num(f.get(f"{prefix}min_{i}")) is not None]
    for _m, k, v, lab in rows:
        if k in ("fixed", "rate") and v is None:
            raise ValueError("정액과 비율 보상에는 값을 넣어야 합니다.")
        if k in ("travel", "goods") and not lab:
            raise ValueError("여행과 물품 보상에는 이름을 넣어야 합니다.")
    return [(m, k, (v / 100 if k == "rate" else v), lab) for m, k, v, lab in rows]


@router.post("/promo/tiers")
async def promo_tiers_save(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    f = await request.form()
    if f.get("action") == "original":
        return _save_original(con, ctx, f)
    if f.get("action") == "copy_bridge":
        return _copy_bridge(con, ctx, f)
    num = lambda v: float(str(v).replace(",", "").strip()) if str(v or "").strip() else None
    promo_id = int(f["promo_id"]) if f.get("promo_id") else None
    try:
        windows = [(f.get(f"w_label_{i}", "").strip(), f.get(f"w_from_{i}", ""), f.get(f"w_to_{i}", "")) for i in range(1, 4)]
        tiers = _tier_table(f, "t_", num)
        window_mode = f.get("window_mode") == "1"
        window_tiers = {seq: _tier_table(f, f"wt{seq}_", num) for seq in WINDOW_SEQS} if window_mode else {}
        window_tiers = {seq: rows for seq, rows in window_tiers.items() if rows}
        if window_mode and not window_tiers:
            raise ValueError("창별 구간표를 쓰려면 창마다 구간을 넣어야 합니다.")
        if window_mode and (f.get("combine_rule") or ("all" if f.get("kind") in ("linked", pm.BRIDGE) else "")) not in pm.EACH_RULES:
            raise ValueError("창별 구간표는 결합 규칙이 창마다 달성이거나 조건을 묶을 때만 고를 수 있습니다.")
        ins = con.execute("SELECT insurer_id, name FROM insurer WHERE insurer_id=?", (int(f["insurer_id"]),)).fetchone() if f.get("insurer_id") else None
        if not f.get("name", "").strip():
            raise ValueError("이름을 넣어야 합니다.")
        common = dict(name=f["name"], windows=windows, tiers=tiers, by=ctx["login_id"], kind=f.get("kind", "weekly"),
                      insurer_id=ins["insurer_id"] if ins else None, insurer_name=(ins["name"] if ins else f.get("insurer_name", "").strip() or None),
                      sector=f.get("sector") or None, categories=f.getlist("categories"), target_note=f.get("target_note", "").strip() or None,
                      metric=f.get("metric", "premium_monthly"), date_basis=f.get("date_basis") or pm.DEFAULT_DATE_BASIS, per_contract_cap=num(f.get("cap")),
                      combine_rule=f.get("combine_rule") or None, pay_timing=f.get("pay_timing") or None, caution=f.get("caution", "").strip() or None,
                      window_tiers=window_tiers, inst13_required=f.get("inst13_required") == "1", rules=_rules_from_form(f, num))
        screen = {"top_org_reward": f.get("top_org_reward", ""), "reward_choices": [c for c in f.getlist("reward_choices") if c]}
        if promo_id:
            res = pm.update(con, promo_id, **common)
            changed = res["changed"] + promo_extra.set_screen(con, promo_id, by=ctx["login_id"], **screen)
            if res["changed"]:                                     # 판 이력의 '바뀐 점' 메모. 이번 고치기 줄에 붙는다
                promo_extra.add_version_note(con, promo_id, f.get("version_note"), by=ctx["login_id"])
            msg = "고쳤습니다. 바뀐 항목 " + str(len(changed)) + "개를 변경 기록에 남겼습니다." if changed else "바뀐 것이 없습니다."
        else:
            new_id = pm.create(con, period_ym=ctx["ym"], source_line_id=int(f["source_line_id"]) if f.get("source_line_id") else None,
                               publish=f.get("action") == "publish", **common)
            promo_extra.set_screen(con, new_id, by=ctx["login_id"], **screen)
            if f.get("source_line_id"):
                con.execute("UPDATE incentive_plan_line SET review_status='approved', reviewed_by=?, reviewed_at=? WHERE line_id=?", (ctx["login_id"], db.now(), int(f["source_line_id"])))
                con.commit()
            msg = "프로모션을 " + ("게시했습니다." if f.get("action") == "publish" else "초안으로 저장했습니다.")
    except (ValueError, KeyError) as e:
        back = f"/promo/tiers?err={quote(str(e))}" + (f"&edit={promo_id}" if promo_id else (f"&line={f.get('source_line_id')}" if f.get("source_line_id") else ""))
        return RedirectResponse(back, status_code=303)
    return RedirectResponse(f"/promo/publish?msg={quote(msg)}", status_code=303)


# ---------------------------------------------------------------- 조직 부문 시상
def _manageable_orgs(con, ctx):
    q, ids = _in(ctx["org_ids"])
    return _q(con, f"SELECT org_id, name, level FROM org_unit WHERE org_id IN ({q}) AND active=1 ORDER BY org_id", tuple(ids)) if ctx["role"] in ("main", "manager") else []


@router.get("/promo/org-awards", response_class=HTMLResponse)
def org_awards(request: Request, edit: int | None = None, new: int | None = None, msg: str | None = None, err: str | None = None,
               ctx: dict = Depends(guard), con=Depends(get_con)):
    ym = ctx["ym"]
    manage = ctx["role"] in ("main", "manager")
    blocks = []
    for d in oa.visible(con, ctx, ym, include_inactive=manage):
        mine = oa.can_manage(ctx, d["org_id"])
        if d["status"] != "active" and not mine:
            continue
        rows = oa.standing(con, d, ym)
        done = oa.confirmed(con, d["division_id"], ym)
        me = next((r for r in rows if r["agent_id"] == ctx["agent_id"]), None) if ctx["agent_id"] else None
        blocks.append({**d, "rows": awards.label_rows(oa.view_rows(rows, ctx)), "total": len(rows), "podium": (done or [r for r in rows if r["winner"]])[:3],
                       "confirmed": done, "me": me, "top": rows[0]["value"] if rows else 0, "can_manage": mine,
                       "prizes": awards.prizes(d["rewards"], d["top_n"])})
    form = None
    if manage and (edit or new):
        d = oa.load(con, edit) if edit else None
        if edit and (not d or not oa.can_manage(ctx, d["org_id"])):
            raise HTTPException(403, "이 부문을 고칠 권한이 없습니다.")
        top_n = org_values.for_ctx(con, ctx, org_values.TOP_N_KEY)        # 조직별 설정의 기본 선발 인원. 만드는 사람의 소속 조직 값이다
        form = d or {"division_id": "", "org_id": None, "name": "", "metric": "converted_premium", "sector": None, "category_list": [], "top_n": top_n,
                     "min_value": None, "effective_from": ym, "effective_to": None, "note": "", "rewards": {}}
    return page(request, ctx, "org_awards.html", blocks=blocks, form=form, manage=manage, msg=msg, err=err, oa=oa, prize_note=awards.PUBLIC_NOTE,
                orgs=_manageable_orgs(con, ctx), cats=_q(con, "SELECT code, name FROM product_category ORDER BY sort_order"))


@router.post("/promo/org-awards")
async def org_awards_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    f = await request.form()
    action, by = f.get("action"), ctx["login_id"]

    def deny():
        auth.log(con, by, "access_denied", f"/promo/org-awards {action}")
        raise HTTPException(403, "이 부문을 다룰 권한이 없습니다.")
    try:
        if action == "save":
            org_id = int(f["org_id"])
            division_id = int(f["division_id"]) if f.get("division_id") else None
            old = oa.load(con, division_id) if division_id else None
            if not oa.can_manage(ctx, org_id) or (old and not oa.can_manage(ctx, old["org_id"])):
                deny()
            oa.save(con, by=by, org_id=org_id, name=f.get("name", ""), metric=f.get("metric", ""), sector=f.get("sector") or None, categories=f.getlist("categories"),
                    top_n=f.get("top_n") or 0, min_value=f.get("min_value") or None, effective_from=f.get("effective_from", ""), effective_to=f.get("effective_to") or None,
                    note=f.get("note", "").strip() or None, rewards=[(i, f.get(f"reward_{i}", "")) for i in range(1, 6)], division_id=division_id)
            msg = "부문을 고쳤습니다." if division_id else "부문을 만들었습니다."
        elif action in ("active", "inactive", "confirm"):
            d = oa.load(con, int(f["division_id"]))
            if not d or (action == "confirm" and ctx["role"] != "main") or not oa.can_manage(ctx, d["org_id"]):
                deny()
            if action == "confirm":
                msg = f"{d['name']}의 수상자 {oa.confirm(con, d['division_id'], ctx['ym'], by)}명을 확정했습니다."
            else:
                oa.set_status(con, d["division_id"], action, by)
                msg = "부문을 다시 열었습니다." if action == "active" else "부문을 닫았습니다."
        else:
            raise ValueError("처리할 수 없는 요청입니다.")
    except (ValueError, KeyError) as e:
        back = "&edit=" + f["division_id"] if f.get("division_id") and action == "save" else ("&new=1" if action == "save" else "")
        return RedirectResponse(f"/promo/org-awards?err={quote(str(e))}{back}", status_code=303)
    return RedirectResponse(f"/promo/org-awards?msg={quote(msg)}", status_code=303)
