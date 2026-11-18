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


def _scope_agents(con, ctx, with_left=False):
    """범위 안 설계사. 재직자만이다. with_left 가 참이면 범위 안 퇴사자도 싣고 줄마다 left 를 붙인다 (D-42).
    퇴사자의 범위는 자료 기준 달의 소속 조직, 곧 퇴사 전 소속으로 본다. 그 달에 소속 이력이 없는 퇴사자는 싣지 않는다.
    엔진의 조직별 진척이 사람을 조직에 놓는 기준(pm._homes)과 같다. 관리자의 범위(ctx agent_ids)도 같은 이력에서 나온다."""
    if ctx["role"] != "main":
        ids = ctx["agent_ids"]
    else:
        ids = {r["agent_id"] for r in con.execute("SELECT agent_id FROM agent" + ("" if with_left else " WHERE status='active'"))}
    q, lst = _in(ids)
    ym = ctx["ym"]
    keep = "(a.status='active' OR h.agent_id IS NOT NULL)" if with_left else "a.status='active'"
    return _q(con, f"""SELECT a.agent_id, a.internal_code code, a.name, o.name org, a.status!='active' AS "left" FROM agent a
                       LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
                       LEFT JOIN org_unit o ON o.org_id=h.org_id WHERE a.agent_id IN ({q}) AND {keep} ORDER BY a.internal_code""", (ym, ym, *lst))


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


CONFIRMED_VIEW_NOTE = "메인 관리자가 확정한 결과입니다. 확정한 뒤 계약이 바뀌어도 다시 계산하지 않습니다. 지난 프로모션 화면과 같은 값입니다."


def _confirmed_team_rows(con, ctx, sel, head, only=None):
    """확정한 프로모션의 사람별 줄. 다시 계산하지 않고 엔진이 남긴 확정 값(pm.final_results)을 옮긴다. 지난 프로모션 화면과 같은 숫자다.
    확정 값에는 창별 실적과 다음 구간이 없어 그 칸은 비운다. 대상은 확정할 때의 재직자 가운데 범위 안 사람이다.
    확정한 뒤에 퇴사한 사람도 줄이 있고 gone 이 참이다. 개인 화면은 재직자만 열리므로 조직별 보기의 사람 표가 그 줄에 주소를 붙이지 않는다."""
    scope = {a["agent_id"]: a for a in _scope_agents(con, ctx, with_left=True)}
    keep = None if only is None else set(only)
    ids = [a for a in scope if keep is None or a in keep]
    fin = pm.final_results(con, sel, ids)
    tiers = {t["tier_no"]: t for t in sel["tiers"]}
    rows = []
    for x in fin["rows"]:
        tier = (tiers.get(x["tier_no"]) or {"tier_no": x["tier_no"]}) if x["tier_no"] else None
        r = {"value": x["value"], "tier": tier, "next": None, "reward": x["reward"], "amount": x["amount"], "broken": False, "capped": False,
             "status": "achieved" if x["outcome"] == "achieved" else "missed", "windows": [], "basis": fin["basis"], "confirmed": True}
        rows.append({**scope[x["agent_id"]], "left": False, "gone": bool(scope[x["agent_id"]]["left"]), "r": r})   # gone: 확정한 뒤에 퇴사했다
    rows.sort(key=lambda x: -x["r"]["value"])
    summary = {"near_text": org_values.pct_text([]), "n": len(rows), "achieved": fin["summary"]["achieved"], "near": 0, "missed": 0, "n_left": 0, "value_left": 0,
               "top": max((x["r"]["value"] for x in rows), default=0),
               "confirmed": {"by": _who_name(con, head["confirmed_by"]) or head["confirmed_by"], "at": head["confirmed_at"], "note": CONFIRMED_VIEW_NOTE}}
    return rows, summary


def _team_rows(con, ctx, sel, ref, only=None):
    """팀원 달성 현황의 줄과 요약. only 를 주면 범위 안 설계사 가운데 그 사람만 남긴다. 조직별 보기의 사람 목록도 이 함수를 쓴다.
    확정한 프로모션이면 확정 값을 보인다(_confirmed_team_rows). 퇴사자는 프로모션 달성 대상이 아니라 줄로 싣지 않는다 (D-42, 11월 14일 결정).
    범위 안 퇴사자의 퇴사 전 실적은 조직별 진척의 실적 합계에만 든다. 그 인원과 합계를 요약(n_left, value_left)에 실어 표 아래에 안내한다.
    only 를 주면 퇴사자는 보지 않는다."""
    head = pm.confirmation(con, sel["promo_id"])
    if head:
        return _confirmed_team_rows(con, ctx, sel, head, only)
    agents = _scope_agents(con, ctx, with_left=only is None)
    if only is not None:
        keep = set(only)
        agents = [a for a in agents if a["agent_id"] in keep]
    res = pm.progress(con, sel, [a["agent_id"] for a in agents], ref)
    pcts = near_pcts(con, ctx, [a["agent_id"] for a in agents])
    rows = [{**a, "r": res[a["agent_id"]]} for a in agents if not a["left"]]
    gone = [res[a["agent_id"]] for a in agents if a["left"] and res[a["agent_id"]].get("target", True)]
    rows.sort(key=lambda x: -x["r"]["value"])
    near_rows = [x for x in rows if near(x["r"], org_values.ratio(pcts[x["agent_id"]]))]
    summary = {"near_text": org_values.pct_text(pcts[x["agent_id"]] for x in rows), "n": len(rows),
               "achieved": sum(1 for x in rows if x["r"]["tier"] or (sel["kind"] == "rate" and x["r"]["value"] > 0)),
               "near": len(near_rows), "missed": sum(1 for x in rows if x["r"]["broken"]), "top": max((x["r"]["value"] for x in rows), default=0),
               "n_left": len(gone), "value_left": sum(r["value"] for r in gone), "confirmed": None}
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
                   ym: str | None = None, who: str | None = None, row: str | None = None, sort: str | None = None,
                   person: int | None = None, f: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    cards, head = progress_cards(con, ctx, ym)
    if view == "org":
        if ctx["role"] not in ("main", "manager"):                 # 가정: 설계사에게는 조직별 보기가 없다
            auth.log(con, ctx["login_id"], "access_denied", "/promo/progress org")
            raise HTTPException(403, "조직별 보기는 메인 관리자와 관리자만 봅니다.")
        return _org_view(request, con, ctx, cards, head, p, org, bool(direct), who, row, sort, person, f)
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
            c["either_text"] = either_text(c)
            c["either_next"] = either_next(c["r"], c["phase_rows"], c["sum_row"]) if c["either_text"] else None
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
    void = bool(r.get("broken")) or r.get("status") == "missed"     # 무효가 됐거나 놓쳤으면 남은 금액을 보이지 않는다
    chain_void = void or bool(((r.get("paths") or {}).get("each") or {}).get("broken"))     # 연계에서 연속이 끊겼으면 창별 남은 금액이 없다. 합산은 남는다
    rows = []
    for w in r["windows"]:
        floor = window_floor(p, w)
        state = _STATE_PHASE[w["state"]]
        left = None
        if state == "open" and not w["tier"] and floor is not None and not chain_void:
            left = nxt["gap"] if nxt.get("path", "each") == "each" and nxt.get("min_value") == floor else max(floor - w["value"], 0)
        rows.append({"seq": w["seq"], "label": w["label"], "state": state, "state_ko": PHASE_KO[state], "value": w["value"], "plain": w.get("plain", w["value"]),
                     "floor": floor, "met": bool(w["tier"]), "left": left, "date_from": w["date_from"], "date_to": w["date_to"]})
    target = sum_target(p)
    sum_row = None
    if target:
        total = (r.get("paths") or {}).get("sum", {}).get("value", r["value"])
        met = bool((r.get("paths") or {}).get("sum", {}).get("tier_no"))
        sum_row = {"target": target, "value": total, "met": met, "left": None if met or void else max(target - total, 0)}
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
    ids = [a["agent_id"] for a in _scope_agents(con, ctx, with_left=True)]     # 범위 안 퇴사자도 넘긴다. 엔진이 실적 합계에만 넣는다 (D-42)
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


def _phase_cell(ph, key="n_met") -> str:
    """창 칸의 글. 끝난 달은 n명, 진행 중인 달은 n명 진행 중, 시작 전인 달은 숫자 없이 시작 전이다.
    key 는 셀 인원이다. 추가안은 n_met, 연속으로 달성하는 창 칸은 n_chain(이 창까지 연속으로 채운 사람)이다."""
    if ph.get("state") == "future":
        return "시작 전"
    if ph.get(key) is None:
        return ""
    return f"{ph[key]}명" + (" 진행 중" if ph.get("state") == "open" else "")


def _target_cell(phases, i, n_target) -> str | None:
    """연속형 표의 창 대상자 글. 첫 창의 대상자는 프로모션 대상 전체이고 둘째 창부터는 앞 창까지 연속이 살아 있는 사람(앞 창의 n_chain)이다.
    시작 전 창과 엔진이 인원을 내지 않은 칸은 None 이다."""
    if i >= len(phases) or phases[i].get("state") == "future":
        return None
    n = n_target if i == 0 else (phases[i - 1].get("n_chain") if i - 1 < len(phases) else None)
    return None if n is None else f"{n}명"


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


def _final(word) -> int:
    """글의 마지막 글자의 받침 번호(0 이면 받침 없음, 8 은 ㄹ). 숫자는 읽는 소리로 본다(영, 일, 삼, 육, 칠, 팔)."""
    for ch in reversed(str(word).strip()):
        if "가" <= ch <= "힣":
            return (ord(ch) - 0xAC00) % 28
        if ch.isdigit():
            return {"0": 21, "1": 8, "3": 16, "6": 1, "7": 8, "8": 8}.get(ch, 0)
    return 0


def _eul(word) -> str:
    return "을" if _final(word) else "를"


def _euro(word) -> str:
    return "으로" if _final(word) not in (0, 8) else "로"


def _win_label(p, i) -> str:
    w = p["windows"][i]
    return w["label"] or f"{w['seq']}번 창"


def either_text(p) -> str | None:
    """연계(연속 또는 합산)의 안내 글. '10월을 채우면 11월에도 이어 갑니다. 연속이 끊기면 10월부터 12월까지 합산 40만으로 달성할 수 있습니다.
    둘 중 하나만 채우면 됩니다.' 창 이름과 합산 기준은 프로모션의 설정에서 읽고 숫자를 글에 박지 않는다. 추가안(조건 1-2)이 있으면 마지막 문장이 셋을 말한다.
    결합이 either 가 아니거나 창이 하나거나 합산 기준이 없으면 None 이다."""
    target = sum_target(p)
    if p["combine_rule"] != "either" or len(p["windows"]) < 2 or not target:
        return None
    first, second, last = _win_label(p, 0), _win_label(p, 1), _win_label(p, len(p["windows"]) - 1)
    t = _short(target, p)
    tail = "조건 1-1, 조건 1-2, 조건 2 가운데 하나만 채우면 됩니다." if pm._r(p).get("alt_chains") else "둘 중 하나만 채우면 됩니다."
    return (f"{first}{_eul(first)} 채우면 {second}에도 이어 갑니다. 연속이 끊기면 {first}부터 {last}까지 합산 {t}{_euro(t)} 달성할 수 있습니다. {tail}")


def either_next(r, rows, sum_row) -> dict | None:
    """연계에서 다음에 채울 것 두 가지. 연속의 다음 기준(each)과 합산까지 남은 금액(sum). 값은 my_phases 의 줄과 합산 줄을 옮긴다.
    each 는 {state, label, floor, left}. state 는 broken(연속이 끊김), next(다음 창이 있음), done(연속을 모두 채움)이다.
    left 는 진행 중인 창에서만 있고 시작 전인 창은 기준만 보인다. sum 은 합산 줄 그대로다. 합산 줄이 없으면 None 이다."""
    if not sum_row or not rows:
        return None
    each = (r.get("paths") or {}).get("each") or {}
    if each.get("broken"):
        chain = {"state": "broken", "label": None, "floor": None, "left": None}
    else:
        nxt = next((x for x in rows if x["state"] != "done" and not x["met"]), None)
        chain = {"state": "next", "label": nxt["label"], "floor": nxt["floor"], "left": nxt["left"], "window_state": nxt["state"]} if nxt \
            else {"state": "done", "label": None, "floor": None, "left": None}
    return {"each": chain, "sum": sum_row}


def amount_text(v, p) -> str | None:
    """조직별 표의 실적 글. 1만원부터는 만원 단위로 줄인다(125만원, 39.9만원). 천원 아래는 버리고 1,000만원부터는 만원 아래도 버린다.
    기준에 못 미친 값이 기준과 같은 글로 보이지 않게 올리지 않는다. 건수 지표면 n건이고 값이 없으면 None 이다."""
    if v is None:
        return None
    if p["metric"] == "policy_count":
        return f"{v:g}건"
    sign, v = ("-" if v < 0 else ""), abs(int(v))
    if v < 10_000:
        return f"{sign}{v:,}원"
    man, frac = divmod(v // 1_000, 10)
    return f"{sign}{man:,}.{frac}만원" if frac and man < 1_000 else f"{sign}{man:,}만원"


def metric_note(shape, ref, confirmed=False) -> str:
    """표 위에 두는 지표와 기준 설명. '실적은 인정보험료 기준입니다' 와 기준일. shape 는 창을 읽는 프로모션이다(딸린 시책이면 부모).
    confirmed 가 참이면 다시 세지 않는 확정 값이라 그렇게 적는다. 이때 ref 는 확정의 계산 기준일이다."""
    basis = pm.BASIS_KO.get(shape["date_basis"], shape["date_basis"])
    count = pm.COUNT_BASIS_KO.get(pm._r(shape)["count_basis"], "")
    if confirmed:
        return f"실적은 {shape['metric_ko']} 기준입니다. 기준일은 {basis}이고 {count}으로 {ref.month}월 {ref.day}일에 확정한 값입니다."
    return f"실적은 {shape['metric_ko']} 기준입니다. 기준일은 {basis}이고 {count}으로 {ref.month}월 {ref.day}일까지 셉니다."


def confirm_ref(head, ref) -> date:
    """확정의 계산 기준일. 확정 줄(pm.confirmation 또는 org_progress 의 confirm)의 ref_date 이고 읽지 못하면 화면의 기준일이다."""
    try:
        return date.fromisoformat(str((head or {}).get("ref_date"))[:10])
    except ValueError:
        return ref


def org_phases(data, sel, ref, shape=None) -> list:
    """조직별 표의 칸. [{key, label, sub, state, group}] 순서는 달(창)마다 대상자, 추가안 달마다 대상자, 합산 달성자, 최종 달성이다.
    대표님이 정한 표 모양이다(11월 17일). 줄마다 cells 에 같은 순서의 글을 붙인다. 숫자는 엔진 값(phases, alt, n_sum, n_achieved)을
    옮기기만 하고 화면에서 세지 않는다. 엔진 값이 없으면 칸을 비우고, 추가안(alt)이 없는 프로모션은 추가안 칸을 그리지 않는다.
    창이 하나뿐이면 달 칸을 그리지 않는다. 대상, 가동, 근접, 미가동, 달성률, 순위는 줄의 두 번째 줄과 '자세히' 에 둔다.

    칸마다 인원과 함께 실적 합계를 보인다(대표님 지시, 11월 17일). 줄마다 sums 와 plains 에 cells 와 같은 순서로 붙인다.
    sums 는 달 칸이 그 달 실적 합계(phases[].value_sum), 추가안 칸이 추가안 대상자의 합계(alt.phases[].value_sum),
    합산 칸이 합산 실적 합계(sum_value, 없으면 value)다. 합산 칸이 없는 프로모션은 최종 달성 칸에 둔다.
    plains 는 가중 인정이 있어 합산용 값이 다를 때의 달 칸 값(plain_sum)이다. 모두 엔진 값을 옮기기만 하고 없으면 None 이다.
    shape 는 창과 지표를 읽는 프로모션이다. 딸린 시책이면 부모이고 비면 sel 이다. 칸의 모양(달 칸, 추가안, 합산 칸, 묶음 머리)은
    shape 를 따른다. 그래서 딸린 시책의 표가 부모와 같은 모양이고 추가안 대상 칸의 이름도 부모의 추가안에서 읽는다.

    창마다 연속으로 달성하는 프로모션(결합 all, both, either)은 연계 모양이다(대표님 지시, 11월 18일). 달 칸 하나에 대상자와 달성을 나눠 보인다.
    첫 창의 대상자는 프로모션 대상 전체, 달성은 그 창 기준을 채운 사람이다. 둘째 창부터 대상자는 앞 창까지 연속이 살아 있는 사람(앞 창의 n_chain),
    달성은 이 창까지 연속으로 채운 사람(이 창의 n_chain)이다. 줄의 cells 는 달성, targets 는 대상자의 글이고 칸 열쇠는 w(달성)와 t(대상자)이다.
    합산 칸에는 결합이 either 이면 '연속이 끊기면 합산으로 갑니다', both 이면 '연속과 합산을 모두 채워야 합니다' 라는 note 가 붙는다.
    엔진은 합산 달성자를 연속에서 빠진 사람과 이어 가는 사람으로 나눠 내지 않아 합산 칸은 하나로 둔다."""
    shape = shape or sel
    tot = data["total"]
    phases = _phase_list(tot, data, ref) if len(shape["windows"]) > 1 else []
    chain = bool(phases) and shape["combine_rule"] in pm.EACH_RULES and has_floors(data)
    alt = tot.get("alt") or data.get("alt")
    alt_phases = (alt or {}).get("phases") or []
    paired = shape["combine_rule"] in pm.PAIRED
    g_main = ("조건 1-1" if alt else "조건 1") if paired else ""       # 칸 묶음 이름은 원문대로 조건 1-1, 조건 1-2, 조건 2
    cols = []
    for ph, w in zip(phases, shape["windows"]):
        floor = ph.get("target") if ph.get("target") is not None else window_floor(shape, w)
        cols.append({"key": f"w{ph['seq']}", "label": ph["label"] if chain else f"{ph['label']} 대상자",
                     "sub": ((f"기준 {_short(floor, shape)}" if chain else _short(floor, shape)) if floor is not None else ""),
                     "state": ph["state"], "group": "main", "group_label": g_main, "chain": chain, "tkey": f"t{ph['seq']}"})
    if alt:
        conf = next((c for c in (pm._r(shape).get("alt_chains") or []) if c.get("key") == alt.get("key")), {})
        wl = {w["seq"]: w["label"] for w in shape["windows"]}
        missed = [wl.get(int(x)) for x in conf.get("requires_missed") or [] if wl.get(int(x))]
        cols.append({"key": "a0", "label": (", ".join(missed) + " 미달성자") if missed else "앞 창 미달성자", "sub": "조건 1-2 대상", "state": None, "group": "alt",
                     "group_label": "조건 1-2", "short": "대상"})
    for ph in alt_phases:
        floor = ph.get("min_value")
        cols.append({"key": f"a{ph['seq']}", "label": f"{ph['label']} 대상자", "sub": (_short(floor, shape) if floor is not None else ""),
                     "state": ph.get("state"), "group": "alt", "group_label": "조건 1-2", "short": ph["label"]})
    target = sum_target(shape, data)
    if target:
        note = None
        if chain and shape["combine_rule"] == "either":
            note = f"연속이 끊기면 합산 {_short(target, shape)}{_euro(_short(target, shape))} 갑니다"
        elif chain and shape["combine_rule"] == "both":
            note = "연속과 합산을 모두 채워야 합니다"
        cols.append({"key": "sum", "label": f"합산 {shape['metric_ko']}", "sub": f"기준 {_short(target, shape)}", "state": None, "group": "sum",
                     "group_label": "조건 2" if paired else "", "pre": f"{_short(target, shape)} 달성", "note": note})
    cols.append({"key": "final", "label": "최종 달성", "sub": "", "state": None, "group": "final", "group_label": ""})
    for i, c in enumerate(cols):                                     # 묶음 머리의 칸 수. 묶음의 첫 칸에만 둔다
        first = i == 0 or cols[i - 1]["group_label"] != c["group_label"]
        c["gspan"] = len(list(itertools.takewhile(lambda d, g=c["group_label"]: d["group_label"] == g, cols[i:]))) if first else 0

    def cells(x):
        out = [_phase_cell(ph, "n_chain" if chain else "n_met") for ph in (_phase_list(x, data, ref) if phases else [])]
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

    def sums(x):
        """(칸마다 실적 합계, 칸마다 합산용 값). 엔진이 낸 값만 옮긴다. 시작 전 달과 엔진 값이 없는 칸은 None 이다."""
        xp = _phase_list(x, data, ref) if phases else []
        val = [ph.get("value_sum") if ph.get("state") != "future" else None for ph in xp]
        plain = [ph.get("plain_sum") if ph.get("state") != "future" else None for ph in xp]
        val += [None] * (len(phases) - len(val))
        plain += [None] * (len(phases) - len(plain))
        plain = [pv if pv is not None and pv != v else None for v, pv in zip(val, plain)]
        if alt:
            val.append(None)
        xa = (x.get("alt") or {}).get("phases") or []
        for i, _ph in enumerate(alt_phases):
            val.append(xa[i].get("value_sum") if i < len(xa) and xa[i].get("state") != "future" else None)
        whole = x.get("sum_value", x.get("value"))
        if target:
            val.append(whole)
        val.append(None if target else whole)
        return val, plain + [None] * (len(val) - len(plain))
    def targets(x):
        """칸마다 대상자 글. 연속형 표의 달 칸에만 있고 그 밖의 칸은 None 이다."""
        if not chain:
            return [None] * len(cols)
        xp = _phase_list(x, data, ref)
        return [_target_cell(xp, i, x["n_target"]) if i < len(phases) else None for i in range(len(cols))]
    alt_ix = [i for i, c in enumerate(cols) if c["group"] == "alt"]
    for r in [*data["rows"], tot]:
        r["cells"] = cells(r)
        r["targets"] = targets(r)
        r["sums"], r["plains"] = sums(r)
        r["alt_line"] = " · ".join(                                  # 휴대폰에서 조건 1-2 를 둘째 줄로. 금액은 인원 뒤에 붙인다
            f"{cols[i]['short']} {r['cells'][i] or '-'}" + (f" {amount_text(r['sums'][i], shape)}" if r["sums"][i] is not None else "")
            for i in alt_ix)
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
# 확정한 프로모션이면 조직 표가 확정 값이므로(org_progress 의 confirmed) 명단과 개인 화면도 다시 세지 않고 pm.confirmed_progress 를 옮긴다.
# 확정 값의 자세함(detail)이 full 이 아니면 엔진이 창별 인원을 내지 않는다. 그 칸의 명단과 사람별 달 칸은 비우고 안내한다.
NO_WINDOW_NOTE = "확정 때의 창별 값이 남아 있지 않습니다."
NO_WINDOW_JUDGE_NOTE = "확정 때의 창별 판정이 남아 있지 않습니다."
NO_COND_NOTE = "확정 때의 조건별 판정이 남아 있지 않습니다."
NOT_IN_CONFIRM = "확정할 때 대상이 아니었습니다."


def window_note(detail, n_windows=2) -> str | None:
    """확정 값에서 창별 값을 낼 수 없을 때의 안내. 확정한 프로모션에서만 부른다. 낼 수 있으면(full 이거나 창이 하나) None 이다.
    detail 은 엔진의 확정 값 자세함이다. org_progress 는 full, values, none 을, confirmed_progress 는 full, values, None 을 낸다.
    values 는 센 계약의 창별 합계는 남아 있고 창별 판정만 없다. 조직 표의 달 칸에 합계가 보이고 개인 화면에 센 계약이 보이므로 말을 달리한다."""
    if detail == "full" or n_windows < 2:
        return None
    return NO_WINDOW_JUDGE_NOTE if detail == "values" else NO_WINDOW_NOTE
WHO_KO = {"target": "대상", "active": "가동", "near": "구간 근접", "inactive": "미가동", "final": "최종 달성", "missed": "미달성",
          "sum": "합산 달성", "chain": "연속 유지 중", "alt_el": "조건 1-2 대상"}
STATUS_LIST_KO = {"met": "달성", "open": "진행 중", "miss": "미달성", "future": "시작 전", "not_target": "대상 아님"}


def final_status(sel, r) -> str:
    """한 사람의 최종 상태 열쇠. 달성(met)은 엔진의 is_achieved, 미달성(miss)은 엔진이 놓쳤다고 본 것, 그 밖은 진행 중(open)이다."""
    if r.get("status") == "not_target":
        return "not_target"
    return "met" if pm.is_achieved(sel, r) else ("miss" if r["status"] == "missed" else "open")


def has_floors(data) -> bool:
    """달마다 기준이 있는 프로모션인가. 엔진이 낸 창별 단계의 기준(phases[].target)으로 본다."""
    return any(ph.get("target") is not None for ph in (data["total"].get("phases") or []))


def person_months(r, multi=True, floors=True) -> list:
    """한 사람의 달(창)별 실적. 엔진의 사람별 결과(windows[].value, plain, tier)를 옮긴다. value 는 그 달 기준에 대는 값이고
    plain 은 합산에 쓰는 값이 다를 때만 싣는다. 시작 전 달은 값을 싣지 않는다. met 은 그 달 기준을 채웠는가이고 달 기준이 없으면 None 이다."""
    out = []
    for w in (r.get("windows") or []) if multi else []:
        state = _STATE_PHASE[w["state"]]
        plain = w.get("plain", w["value"])
        out.append({"seq": w["seq"], "label": w["label"], "state": state, "met": bool(w["tier"]) if floors else None,
                    "value": None if state == "future" else w["value"],
                    "plain": plain if state != "future" and plain != w["value"] else None})
    return out


def cell_who(col_key: str) -> str | None:
    """표 칸 열쇠를 명단 열쇠로. w2(달성)와 t2(대상자)는 그대로, a0 는 alt_el, a2 는 alt_w2, sum, final 도 그대로다."""
    if col_key == "a0":
        return "alt_el"
    if col_key.startswith("a") and col_key[1:].isdigit():
        return f"alt_w{col_key[1:]}"
    return col_key


def _who_rule(sel, data, who, pcts):
    """(제목, 조건 함수(agent_id, 결과), 볼 값 함수). 엔진이 org_progress 에서 세는 조건과 같다. 모르는 열쇠면 None.
    확정 값이라 엔진이 그 칸의 인원을 내지 않았으면(None) 조건 함수와 볼 값 함수가 None 이다. 화면에서 대신 세지 않는다."""
    seqs = [w["seq"] for w in data["windows"]]
    tot = data["total"]
    phases = {ph["seq"]: ph for ph in (tot.get("phases") or [])}
    if who[:1] in ("w", "t") and who[1:].isdigit() and int(who[1:]) in seqs:
        seq = int(who[1:])
        i = seqs.index(seq)
        ph = phases.get(seq) or {}
        if ph.get("state") == "future" or ph.get("target") is None:
            return None
        floor = ph["target"]
        label = data["windows"][i]["label"]
        view = lambda r: (r["windows"][i]["value"], r["windows"][i].get("plain", r["windows"][i]["value"]), floor,
                          "met" if r["windows"][i]["tier"] else ("open" if ph.get("state") == "open" else "miss"))
        if who[0] == "t":                                               # 창 대상자. 첫 창은 대상 전체, 그 뒤는 앞 창까지 연속이 살아 있는 사람(앞 창의 n_chain)
            title = f"{label} 대상자" if i == 0 else f"{label} 대상자({data['windows'][i - 1]['label']}까지 연속으로 채운 사람)"
            if i == 0:
                return title, (lambda a, r: True), view
            if (tot["phases"][i - 1].get("n_chain")) is None:
                return title, None, None
            return title, (lambda a, r: all(r["windows"][j]["tier"] for j in range(i))), view
        title = f"{label} 기준({_short(floor, sel)}) 채운 사람" if i == 0 else f"{label} 기준({_short(floor, sel)})까지 연속으로 채운 사람"
        if ph.get("n_chain") is None:                                   # 달성은 이 창까지 연속으로 채운 사람(이 창의 n_chain)
            return title, None, None
        return title, (lambda a, r: all(r["windows"][j]["tier"] for j in range(i + 1))), view
    if who.startswith("alt_w") and who[5:].isdigit():
        alt = tot.get("alt")
        if not alt:
            return None
        seq = int(who[5:])
        j = next((k for k, ph in enumerate(alt["phases"]) if ph["seq"] == seq), None)
        if j is None or alt["phases"][j].get("state") == "future":
            return None
        ph = alt["phases"][j]
        if ph.get("n_met") is None:
            return f"조건 1-2 {ph['label']} 기준({_short(ph['min_value'], sel)}) 채운 사람", None, None

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
        if alt.get("n_eligible") is None:
            return WHO_KO[who], None, None
        pick = lambda a, r: any(x["key"] == alt["key"] and x["eligible"] for x in ((r.get("paths") or {}).get("alts") or []))
        return WHO_KO[who], pick, final_view
    if who == "sum":
        target = tot.get("sum_target")
        if not target:
            return None
        if tot.get("n_sum") is None:
            return f"합산 {_short(target, sel)} 채운 사람", None, None
        return (f"합산 {_short(target, sel)} 채운 사람", lambda a, r: r["value"] >= target,
                lambda r: (r["value"], None, target, "met" if r["value"] >= target else "open"))
    rules = {"target": lambda a, r: True, "active": lambda a, r: r["value"] > 0, "inactive": lambda a, r: not r["value"] > 0,
             "near": lambda a, r: pm.near(r, float(pcts.get(a, 30)) / 100.0), "final": lambda a, r: pm.is_achieved(sel, r),
             "missed": lambda a, r: not pm.is_achieved(sel, r)}
    if who in rules:
        return WHO_KO[who], rules[who], final_view
    return None


SORT_KO = {"cell": "누른 칸 실적 순", "sum": "합산 실적 큰 순"}


def who_roster(con, ctx, sel, ref, data, phases, who, row_key, sort=None):
    """명단 하나. row_key 가 표의 줄(path_key)이면 그 줄만, 비거나 all 이면 모든 줄이다. 없는 열쇠나 줄이면 None.
    사람마다 달(창)별 실적 months 와 합산 실적 total, 합산 기준까지 남은 값 sum_left 를 함께 싣는다. 모두 엔진의 사람별 결과
    (pm.progress 의 windows[].value, plain, value)를 옮긴 것이고 남은 값만 기준에서 합산 실적을 뺀다. 시작 전 달은 값을 싣지 않는다.
    sort 가 sum 이면 합산 실적이 큰 순, 아니면 누른 칸의 실적이 큰 순이다. 시상 금액은 싣지 않는다.
    확정한 프로모션이면(data 의 confirmed) 다시 세지 않고 pm.confirmed_progress 를 옮긴다. 조직 표와 같은 확정 값이다. 대상은 엔진이 낸
    확정 때의 재직자라 그 뒤에 퇴사한 범위 안 사람도 싣고 gone 으로 표시한다. 팀원 달성 현황의 확정 줄과 같은 사람이다.
    확정 값의 자세함이 full 이 아니면 달 칸을 싣지 않고 window_note 로 안내한다. 엔진이 인원을 내지 않은 칸의 명단은 unavailable 에
    안내만 싣고 사람은 싣지 않는다."""
    sort = sort if sort in SORT_KO else "cell"
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
    frozen = bool(data.get("confirmed"))                                # 확정한 프로모션. 조직 표가 확정 값이다
    note = window_note(data.get("detail"), len(data["windows"])) if frozen else None
    if pick is None:                                                    # 엔진이 인원을 내지 않은 칸. 화면에서 대신 세지 않는다
        return {"who": who, "title": title, "row": row_key or "all", "n": 0, "groups": [], "summary": [], "confirmed": frozen,
                "unavailable": (note or NO_WINDOW_NOTE) if (who[:1] in ("w", "t") and who[1:].isdigit()) or who.startswith("alt_w") else NO_COND_NOTE, "window_note": None, "estimate_note": data["estimate_note"], "windows": [], "on_seq": None,
                "sum_target": None, "sum_target_text": None, "sort": sort, "sorts": SORT_KO, "sortable": False, "marks": False, "has_plain": False}
    res = (pm.confirmed_progress(con, sel, ids) or {}) if frozen else pm.progress(con, sel, ids, ref)
    scope = {a["agent_id"]: a for a in _scope_agents(con, ctx, with_left=frozen)}   # 범위 안 사람만. org_progress 도 같은 목록으로 셌다
    multi = len(data["windows"]) > 1 and not note                       # 창별 값이 없는 확정 값이면 달 칸을 그리지 않는다
    shape = pm._parent(con, sel) or sel                                 # 합산 기준은 창을 읽는 프로모션의 것이다
    target, floors = sum_target(shape, data), has_floors(data)
    on_seq = int(who[1:]) if who[:1] in ("w", "t") and who[1:].isdigit() else (int(who[5:]) if who.startswith("alt_w") and who[5:].isdigit() else None)
    groups, n = [], 0
    for r in rows:
        people = []
        for a in r["agent_ids"]:
            if a not in scope or a not in res or not pick(a, res[a]):
                continue
            value, plain, floor, st = view(res[a])
            left = max(floor - value, 0) if floor is not None and st != "met" else None
            total = res[a]["value"]
            people.append({"agent_id": a, "code": scope[a]["code"], "name": scope[a]["name"], "branch": scope[a]["org"] or "-", "gone": bool(scope[a]["left"]),
                           "value": value, "plain": plain if plain is not None and plain != value else None, "floor": floor, "left": left,
                           "status": st, "status_ko": STATUS_LIST_KO[st], "months": person_months(res[a], multi, floors), "total": total,
                           "sum_left": sum_left(target, res[a])})
        people.sort(key=lambda x: (-(x["total"] if sort == "sum" else x["value"]), x["code"]))
        n += len(people)
        groups.append({"title": "바로 소속" if r["direct"] else org_labels.title(r), "path_key": r["path_key"], "people": people})
    return {"who": who, "title": title, "row": row_key or "all", "n": n, "groups": [g for g in groups if g["people"]],
            "summary": [(g["title"], len(g["people"])) for g in groups if g["people"]], "estimate_note": data["estimate_note"],
            "confirmed": frozen, "unavailable": None, "window_note": note,
            "windows": [{"seq": w["seq"], "label": w["label"]} for w in data["windows"]] if multi else [], "on_seq": on_seq,
            "sum_target": target, "sum_target_text": _short(target, shape) if target else None, "sort": sort, "sorts": SORT_KO,
            "sortable": on_seq is not None, "marks": multi and floors,    # 누른 칸이 달 칸일 때만 두 순서가 다르다
            "has_plain": any(m["plain"] is not None for g in groups for x in g["people"] for m in x["months"])}


PEOPLE_FILTER_KO = {"all": "전체", "met": "달성", "miss": "미달성"}


def sum_left(target, r):
    """합산 기준까지 남은 값. 기준에서 합산 실적을 뺀다. 합산 조건이 없거나 무효가 됐거나 놓친 사람은 None 이다."""
    if target is None or r.get("broken") or r.get("status") == "missed":
        return None
    return max(target - r["value"], 0)


def people_view(sel, data, people, f, href, shape=None) -> dict:
    """본부 화면의 사람 표. 줄마다 달별 실적, 합산 실적, 합산 기준까지 남은 값, 최종 상태, 개인 화면 주소를 붙인다.
    값은 팀원 달성 현황과 같은 사람별 결과(pm.progress)를 옮긴 것이고 남은 값만 기준에서 합산 실적을 뺀다.
    f 는 거르기다. met 은 최종 달성, miss 는 아직 달성하지 않은 사람 모두(진행 중 포함)다. 명단 열쇠 final, missed 와 같은 조건이다.
    실적이 없는 사람은 zero 에 따로 모아 화면에서 접어 둔다. 순서는 합산 실적이 큰 순이다(_team_rows)."""
    f = f if f in PEOPLE_FILTER_KO else "all"
    multi, floors = len(data["windows"]) > 1, has_floors(data)
    if any(x["r"].get("confirmed") for x in people):                # 확정 값에는 달별 실적이 없다. 달 칸을 그리지 않는다
        multi = False
    shape = shape or sel
    target = sum_target(shape, data)
    for x in people:
        r = x["r"]
        st = final_status(sel, r)
        x.update(months=person_months(r, multi, floors), total=r["value"], sum_left=sum_left(target, r),
                 status=st, status_ko=STATUS_LIST_KO[st], met=st == "met", href=None if x.get("gone") else f"{href}&person={x['agent_id']}")
    counts = {"all": len(people), "met": sum(1 for x in people if x["met"]), "miss": sum(1 for x in people if not x["met"])}
    shown = [x for x in people if f == "all" or (f == "met") == x["met"]]
    return {"f": f, "filters": PEOPLE_FILTER_KO, "counts": counts, "rows": [x for x in shown if x["total"] or x["met"]],
            "zero": [x for x in shown if not x["total"] and not x["met"]], "sum_target": target, "marks": multi and floors,
            "sum_target_text": _short(target, shape) if target else None, "windows": [{"seq": w["seq"], "label": w["label"]} for w in data["windows"]] if multi else [],
            "has_plain": any(m["plain"] is not None for x in people for m in x["months"])}


def contract_rows(cc) -> list:
    """개인 화면의 센 계약 줄. 엔진의 pm.counted_contracts() 가 낸 줄을 화면 글로 옮기기만 하고 다시 거르거나 세지 않는다.
    상태 코드는 CONTRACT_STATUS_KO 로 읽는다. value_each 는 달 기준에 대는 값이 센 값과 다를 때만 남긴다.
    after_ref 는 기준일 뒤 날짜의 계약이다. 고객 이름과 증권번호는 엔진이 싣지 않는다."""
    out = []
    for x in cc["rows"]:
        out.append({"date": x["date"], "window": x["window_label"] or "", "insurer": x["insurer"] or "-", "product": x["product"] or "상품명 없음",
                    "premium": x["premium"], "converted": x["converted"], "count": x["count"], "value": x["value"],
                    "value_each": x["value_each"] if x["value_each"] != x["value"] else None, "boosted": bool(x["boosted"]),
                    "status": x["status"], "status_ko": CONTRACT_STATUS_KO.get(x["status"], x["status"] or "-"), "after_ref": bool(x["after_ref"])})
    return out


CONTRACT_STATUS_KO = {"applied": "청약", "inforce": "정상", "withdrawn": "철회", "declined": "반송", "lapsed": "실효", "cancelled": "해지", "matured": "만기"}
NEXT_PATH_KO = {"each": "이번 달 기준까지", "alt": "조건 1-2의 이번 달 기준까지", "sum": "합산 기준까지"}


def _person_view(request, con, ctx, cards, head, sel, ref, org, starts, person):
    """개인 화면(person=agent_id). 조직별 보기 안의 셋째 단계다. 범위 안 설계사만 열리고 범위 밖이면 403 이다.
    달별 실적, 합산 실적, 조건별 상태, 다음에 채울 것, 엔진이 센 계약 목록을 보인다. 값은 pm.progress 의 그 사람 결과다.
    확정한 프로모션이면 다시 세지 않고 pm.confirmed_progress 의 그 사람 결과를 보인다. 조직 표, 명단과 같은 확정 값이다.
    확정 결과가 없는 사람(확정 뒤에 들어왔거나 딸린 시책의 대상 직급이 아니다)은 값 없이 대상이 아니었다고 안내한다.
    센 계약은 pm.counted_contracts 가 낸다. 확정 뒤에는 확정 때 센 계약이다.
    예상 시상(금액과 보상 글)은 메인 관리자에게만 넘긴다. 관리자에게는 실적 금액까지만 넘긴다 (D-17, D-74 가정)."""
    is_main = ctx["role"] == "main"
    scope = {a["agent_id"]: a for a in _scope_agents(con, ctx)}
    if person not in scope:
        auth.log(con, ctx["login_id"], "access_denied", "/promo/progress person")
        raise HTTPException(403, "볼 수 없는 사람입니다.")
    if not sel:
        raise HTTPException(404, "게시된 프로모션이 없습니다.")
    who = scope[person]
    shape = pm._parent(con, sel) or sel
    head_c = pm.confirmation(con, sel["promo_id"])                    # 살아 있는 확정. 있으면 다시 세지 않는다
    absent = False
    if head_c:
        r = (pm.confirmed_progress(con, sel, [person], head_c) or {}).get(person)
        absent = r is None
        if absent:
            r = {"value": 0, "tier": None, "next": None, "reward": None, "amount": None, "status": "not_target", "broken": None, "capped": None,
                 "windows": [], "basis": head_c.get("basis_note"), "confirmed": True, "detail": None, "unnamed": None}
    else:
        r = pm.progress(con, sel, [person], ref)[person]
    confirmed = {"by": _who_name(con, head_c["confirmed_by"]) or head_c["confirmed_by"], "at": head_c["confirmed_at"],
                 "note": CONFIRMED_VIEW_NOTE} if head_c else None
    no_windows = window_note(r.get("detail"), len(shape["windows"])) if head_c and not absent else None
    count_ref = confirm_ref(head_c, ref) if head_c else ref
    # 위로 가는 경로. 누른 조직이 그 사람의 소속 위에 있으면 그 조직까지, 아니면 소속 조직까지다. 범위 밖 조직은 싣지 않는다
    home = (pm._homes(con, [person], ctx["ym"]).get(person) or {}).get("org_id")
    home_row = con.execute("SELECT org_id, parent_id, name, level, path_key FROM org_unit WHERE org_id=?", (home,)).fetchone() if home else None
    at = org if org and home in pm.orgs.descendants(con, org["org_id"]) else (dict(home_row) if home_row else None)
    crumbs = [c for c in (_crumbs(con, at, starts) if at else []) if c["org_id"] in ctx["org_ids"]]
    rows, sum_row = my_phases(shape, r)
    floors = {x["seq"]: x for x in rows}
    months = [{**m, "floor": (floors.get(m["seq"]) or {}).get("floor"), "left": (floors.get(m["seq"]) or {}).get("left"),
               "date_from": w["date_from"], "date_to": w["date_to"]} for m, w in zip(person_months(r), r.get("windows") or [])]
    for m in months:
        if m["floor"] is None:
            m["met"] = None                                         # 달 기준이 없는 결합은 채움 여부를 보이지 않는다
    paths = r.get("paths") or {}
    paired = shape["combine_rule"] in pm.PAIRED
    each = None
    if paired and paths.get("each"):
        each = "miss" if paths["each"]["broken"] else ("met" if paths["each"]["tier_no"] else "open")
    elif shape["combine_rule"] == "all" and months:
        each = "miss" if r["broken"] else ("met" if r["tier"] else "open")
    st = final_status(sel, r)
    alt = my_alt(shape, r)
    # 다음에 채울 것. 조건마다 남은 값을 모은다. 값은 내 달성 현황과 같은 것(my_phases, my_alt)이고 여기서 새로 재지 않는다.
    # 조건이 깨졌으면 그 길은 싣지 않는다. 이미 달성했거나 조건에 남은 값이 없으면 엔진이 낸 다음 구간(next)만 쓴다
    todo = []
    if st == "open":
        if each != "miss":
            for m in months:
                if m["left"]:
                    todo.append({"label": f"{m['label']} 기준까지", "value": m["left"], "note": None})
                elif m["state"] == "future" and m["floor"] is not None:
                    todo.append({"label": f"{m['label']} 기준", "value": m["floor"], "note": "시작 전"})
        if alt and not alt["achieved"] and not alt["broken"]:
            for w in alt["rows"]:
                if w["left"]:
                    todo.append({"label": f"{alt['label']} {w['label']} 기준까지", "value": w["left"], "note": None})
                elif w["state"] == "future" and w["min_value"] is not None:
                    todo.append({"label": f"{alt['label']} {w['label']} 기준", "value": w["min_value"], "note": "시작 전"})
        if sum_row and sum_row["left"]:
            todo.append({"label": f"합산 {_short(sum_row['target'], shape)}까지", "value": sum_row["left"], "note": None})
    nxt = r.get("next")
    if not todo and nxt and st not in ("miss", "not_target"):
        path = nxt.get("path") or ("each" if shape["combine_rule"] == "all" else "sum")
        todo.append({"label": NEXT_PATH_KO.get(path, NEXT_PATH_KO["sum"]), "value": nxt["gap"], "note": None})
    next_reward = nxt.get("reward") if nxt and is_main else None        # 보상 글에는 금액이 들 수 있어 메인 관리자에게만
    amount = {"amount": r.get("amount"), "reward": r.get("reward")} if is_main else None
    cc = pm.counted_contracts(con, sel, person, ref)                    # 딸린 시책이면 엔진이 부모의 계약을 낸다
    here = f"/promo/progress?view=org&p={sel['promo_id']}" + (f"&ym={head['ym']}" if not head["now"] else "")
    return page(request, ctx, "promo_progress_person.html", cards=cards, sel=sel, shape=shape, head=head, ref=ref.isoformat(), who=who, r=r,
                crumbs=crumbs, months=months, sum_row=sum_row, alt=alt, each=each, each_ko=STATUS_LIST_KO.get(each),
                has_alt=bool(pm._r(shape).get("alt_chains")), paired_text=PAIRED_TEXT.get(shape["combine_rule"]) if paired else None,
                todo=todo, next_reward=next_reward, status=st, status_ko=STATUS_LIST_KO[st], amount=amount, show_amount=is_main,
                contracts=contract_rows(cc), contracts_n=cc["n"], contract_source=cc["source"], contract_note=cc["note"],
                unnamed=r.get("unnamed") or 0, here=here, confirmed=confirmed, absent=absent, absent_note=NOT_IN_CONFIRM, window_note=no_windows,
                org_q=("&org=" + quote(at["path_key"])) if at and at["org_id"] in ctx["org_ids"] else "",
                metric_note=metric_note(shape, count_ref, bool(head_c)), basis=r.get("basis"), attached=shape is not sel,
                boost_text=boost_text(shape), assumption=PERSON_ASSUMPTION, org_title=org_labels.title)


PERSON_ASSUMPTION = ("개인 화면을 보는 범위는 가정입니다. 메인 관리자는 시상 금액까지, 관리자는 범위 안 설계사의 실적과 센 계약의 보험료까지 봅니다. "
                     "시상 금액과 수수료는 관리자에게 보이지 않습니다.")


def _org_view(request, con, ctx, cards, head, p, org_key, direct, who=None, row_key=None, sort=None, person=None, f=None):
    is_main = ctx["role"] == "main"
    starts = _org_starts(con, ctx)
    sel = next((c for c in cards if c["promo_id"] == p), cards[0] if cards else None)
    ref = sel["ref"] if sel else date.fromisoformat(head["ref"])
    gates = _gates(con, cards)
    org = _org_row(con, org_key) if org_key else (starts[0] if starts else None)
    if org_key and (not org or org["org_id"] not in ctx["org_ids"]):     # 범위 밖이나 없는 조직
        auth.log(con, ctx["login_id"], "access_denied", "/promo/progress org")
        raise HTTPException(403, "볼 수 없는 조직입니다.")
    if person is not None:
        return _person_view(request, con, ctx, cards, head, sel, ref, org if org_key else None, starts, person)
    data, crumbs, people, people_summary, top = None, [], None, None, None
    mq = f"&ym={head['ym']}" if not head["now"] else ""              # 지난 달을 보고 있으면 아래로 내려가는 주소에도 그 달을 붙인다
    if sel and org:
        data = org_numbers(con, ctx, sel, ref, org["path_key"], keep_amounts=is_main)
        crumbs = _crumbs(con, org, starts)
        kids = [r for r in data["rows"] if not r["direct"]]
        has_children = bool(con.execute("SELECT 1 FROM org_unit WHERE parent_id=? LIMIT 1", (org["org_id"],)).fetchone())
        # 본부 화면: 아래 조직이 모두 맨 아래 조직이면 지점별 표 아래에 그 조직 사람 전원을 바로 보인다 (대표님 지시, 11월 17일)
        has_grand = bool(con.execute("SELECT 1 FROM org_unit c JOIN org_unit g ON g.parent_id=c.org_id WHERE c.parent_id=? LIMIT 1",
                                     (org["org_id"],)).fetchone())
        if direct or not has_children or not has_grand:            # 맨 아래 조직, 바로 소속인 사람 줄, 본부 화면이면 사람 목록
            members = [a for r in data["rows"] if (r["direct"] or not direct) for a in r["agent_ids"]]
            people, people_summary = _team_rows(con, ctx, sel, ref, only=members)
        for r in data["rows"]:
            r["level_ko"] = org_labels.level_name(r)                    # 화면 계층 이름. config/org_labels.yaml
            r["title"] = org_labels.title(r)
            r["href"] = f"/promo/progress?view=org&p={sel['promo_id']}{mq}&org={quote(r['path_key'])}" + ("&direct=1" if r["direct"] else "")
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
    shape = (pm._parent(con, sel) or sel) if sel else None          # 창과 지표를 읽는 프로모션. 딸린 시책이면 부모다
    phases = org_phases(data, sel, ref, shape) if data else []
    roster = None
    if data and who:
        roster = who_roster(con, ctx, sel, ref, data, phases, who, row_key, sort)
        if roster is None:
            raise HTTPException(404, "없는 명단입니다.")
    if sel:
        sel["versions"] = promo_extra.versions(con, sel)
        sel["chain_text"] = chain_text(shape)
    here = (f"/promo/progress?view=org&p={sel['promo_id']}{mq}&org={quote(org['path_key'])}" + ("&direct=1" if direct else "")) if sel and org else ""
    pv = people_view(sel, data, people, f, here, shape) if people is not None else None
    # 확정한 프로모션. 조직 줄의 숫자도 엔진이 확정 값으로 낸다(org_progress 의 confirmed, confirm, detail). 명단과 사람 표도 같은 확정 값이다
    head_c = data["confirm"] if data and data.get("confirmed") else None
    confirmed = {"by": _who_name(con, head_c["confirmed_by"]) or head_c["confirmed_by"], "at": head_c["confirmed_at"], "note": CONFIRMED_VIEW_NOTE,
                 "window_note": window_note(data.get("detail"), len(data["windows"]))} if head_c else None
    count_ref = confirm_ref(head_c, ref) if head_c else ref
    if roster:
        for g in roster["groups"]:
            for x in g["people"]:                                    # 확정 뒤에 퇴사한 사람은 개인 화면이 열리지 않아 주소를 붙이지 않는다
                x["href"] = None if x["gone"] else f"{here}&person={x['agent_id']}"
    return page(request, ctx, "promo_progress_org.html", pv=pv, confirmed=confirmed, cards=cards, sel=sel, org=org, data=data, crumbs=crumbs, starts=starts, people=people,
                summary=people_summary, direct=direct, top=top, gates=gates, ref=ref.isoformat(), show_amount=is_main, head=head,
                phases=phases, roster=roster, who=who, row_key=row_key, cell_who=cell_who, shape=shape,
                man=(lambda v: amount_text(v, shape)), metric_note=metric_note(shape, count_ref, bool(head_c)) if shape else "",
                word="확정" if head_c else "예상",
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
            "ret_basis": r.get("retention_basis") or "count", "alts": _alt_views(r)}


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
    """추가안 칸을 엔진의 alt_chains 꼴 [{key, label, windows: {창 순번: 기준}, requires_missed: [창 순번], boost, tier_no}] 로 옮긴다.
    폼은 추가안마다 한 묶음(r_alt_*_{n})이고 묶음 수는 r_alt_n 이다. 열쇠, 가중 인정 여부, 구간 번호를 폼에 실어 그대로 돌려준다.
    그래야 아무것도 바꾸지 않고 저장했을 때 조건이 그대로다. 기준을 모두 비운 묶음은 뺀다. 모두 빼면 추가안을 지운다.
    폼에 추가안 칸이 없으면(r_alt_n 이 없으면) 지금 값을 그대로 둔다. 검사는 엔진(pm._clean_alts)이 한다."""
    n = str(f.get("r_alt_n") or "")
    if not ALT_SUPPORTED or not n.isdigit():
        return {}
    chains = []
    for i in range(1, int(n) + 1):
        wins = {seq: num(f.get(f"r_alt_min_{i}_{seq}")) for seq in WINDOW_SEQS}
        wins = {seq: v for seq, v in wins.items() if v is not None}
        if not wins:
            continue
        tier = str(f.get(f"r_alt_tier_{i}") or "").strip()
        if tier and not tier.isdigit():
            raise ValueError("추가안의 구간 번호는 숫자로 넣습니다.")
        chains.append({"key": str(f.get(f"r_alt_key_{i}") or "").strip() or None, "label": str(f.get(f"r_alt_label_{i}") or "").strip() or None,
                       "windows": wins, "requires_missed": [int(x) for x in f.getlist(f"r_alt_missed_{i}") if str(x).isdigit()],
                       "boost": f.get(f"r_alt_boost_{i}", "1") != "0", "tier_no": int(tier) if tier else 1})
    return {"alt_chains": chains}


def _alt_views(r) -> list:
    """추가안마다 폼 한 묶음. 맨 끝에 새로 넣을 빈 묶음 하나를 둔다. 열쇠, 가중 인정 여부, 구간 번호를 함께 싣는다."""
    out = []
    for c in r.get("alt_chains") or []:
        wins = {int(k): v for k, v in (c.get("windows") or {}).items()}
        out.append({"key": c.get("key") or "", "label": c.get("label") or "", "missed": [int(x) for x in c.get("requires_missed") or []],
                    "floors": {seq: num_text(wins.get(seq)) for seq in WINDOW_SEQS}, "boost": c.get("boost", True) is not False,
                    "tier_no": int(c.get("tier_no") or 1), "new": False})
    out.append({"key": "", "label": "", "missed": [], "floors": {seq: "" for seq in WINDOW_SEQS}, "boost": True, "tier_no": 1, "new": True})
    return out


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


def ko_error(e) -> str:
    """화면에 보일 오류 글. 엔진의 한국어 안내는 그대로 쓰고, 한글이 없는 글(파이썬의 영어 오류)은 한국어 안내로 바꾼다."""
    text = str(e)
    return text if any("가" <= ch <= "힣" for ch in text) else "입력한 값을 읽지 못했습니다. 날짜는 2026-12-16 처럼, 숫자는 숫자만 넣습니다."


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
        for _lab, a, b in wins:                                   # 날짜 꼴을 먼저 본다. 엔진의 영어 오류가 화면에 나오지 않게 한다
            try:
                date.fromisoformat(str(a)), date.fromisoformat(str(b))
            except ValueError as e:
                raise ValueError("창의 날짜를 읽지 못했습니다. 2026-12-16 처럼 연, 월, 일을 넣습니다.") from e
        res = pm.copy_bridge(con, src, period_ym=ym, windows=wins, by=ctx["login_id"], name=str(f.get("name") or "").strip() or None)
    except ValueError as e:
        return RedirectResponse(f"{back}&err={quote(ko_error(e))}#copy", status_code=303)
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
    """폼의 구간표 한 장. 기준이 빈 줄은 뺀다. 비율은 퍼센트로 받아 100 으로 나눈다. prefix 는 공통 표 't_', 창별 표 'wt2_' 꼴이다.
    정액의 금액은 비울 수 있다. 시상안에 금액이 그림으로만 있을 때 '원문 확인' 으로 두는 구간이다(브릿지 샘플)."""
    rows = [(num(f.get(f"{prefix}min_{i}")), f.get(f"{prefix}kind_{i}", "fixed"), num(f.get(f"{prefix}value_{i}")), f.get(f"{prefix}label_{i}", "").strip())
            for i in TIER_ROWS if num(f.get(f"{prefix}min_{i}")) is not None]
    for _m, k, v, lab in rows:
        if k == "rate" and v is None:                           # 정액은 금액을 비워 둘 수 있다. 화면에 '원문 확인' 으로 보인다
            raise ValueError("비율 보상에는 값을 넣어야 합니다.")
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
            if not res["changed"] and str(f.get("version_note") or "").strip():     # 메모는 조건을 고친 판에만 붙는다
                msg += " 바뀐 내용이 없어 메모를 남기지 않았습니다."
        else:
            new_id = pm.create(con, period_ym=ctx["ym"], source_line_id=int(f["source_line_id"]) if f.get("source_line_id") else None,
                               publish=f.get("action") == "publish", **common)
            promo_extra.set_screen(con, new_id, by=ctx["login_id"], **screen)
            if f.get("source_line_id"):
                con.execute("UPDATE incentive_plan_line SET review_status='approved', reviewed_by=?, reviewed_at=? WHERE line_id=?", (ctx["login_id"], db.now(), int(f["source_line_id"])))
                con.commit()
            msg = "프로모션을 " + ("게시했습니다." if f.get("action") == "publish" else "초안으로 저장했습니다.")
    except (ValueError, KeyError) as e:
        back = f"/promo/tiers?err={quote(ko_error(e))}" + (f"&edit={promo_id}" if promo_id else (f"&line={f.get('source_line_id')}" if f.get("source_line_id") else ""))
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
