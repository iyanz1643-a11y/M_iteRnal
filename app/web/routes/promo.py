"""프로모션 묶음: 진행 중 프로모션, 달성 현황, 프로모션 비교, 지난 프로모션, 게시 관리, 구간과 조건 입력, 조직 부문 시상.

- 비교(/promo/compare)는 pm.compare 를 그대로 그린다. D-19 제안대로 설계사에게도 열고 내부 참고용 안내를 붙인다.
  D-21 제안대로 원안은 메인 관리자에게만 넘긴다. 설계사와 관리자에게는 줄에서 원안 칸을 떼고 넘긴다.
- 지난 프로모션(/promo/archive)은 pm.past 로 낸 마감 전 예상이다. 설계사는 본인 줄만, 관리자는 범위 안 사람의 달성 여부만 본다.
  D-17 에 따라 관리자 화면으로는 금액과 보상 글을 넘기지 않는다. 메인 관리자는 전부 본다.
- 조직 부문 시상(/promo/org-awards)의 순위별 시상 내용은 D-45 제안대로(가정) 모두에게 공개하는 상품표다.
  규칙은 app/web/awards.py 한 곳이고 명예의전당과 같다. 합계는 만들지 않는다.
"""
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.engine import org_award as oa
from app.engine import promotion as pm
from app.web import auth, awards, org_values
from app.web.deps import _cards, _in, _period_text, _q, get_con, guard, page

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


@router.get("/promo/progress", response_class=HTMLResponse)
def promo_progress(request: Request, p: int | None = None, view: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    cards, ref = _cards(con, ctx)
    cards = [c for c in cards if c["recipient_level"] == "fc"]
    team_view = ctx["role"] == "main" or (ctx["role"] == "manager" and view != "me")
    if not team_view:
        mine = [{**c, "r": pm.progress(con, c, [ctx["agent_id"]], ref)[ctx["agent_id"]]} for c in cards] if ctx["agent_id"] else []
        return page(request, ctx, "promo_progress_me.html", items=mine, ref=ref.isoformat())
    sel = next((c for c in cards if c["promo_id"] == p), cards[0] if cards else None)
    rows, summary = [], None
    if sel:
        agents = _scope_agents(con, ctx)
        res = pm.progress(con, sel, [a["agent_id"] for a in agents], ref)
        pcts = near_pcts(con, ctx, [a["agent_id"] for a in agents])
        rows = [{**a, "r": res[a["agent_id"]]} for a in agents]
        rows.sort(key=lambda x: -x["r"]["value"])
        near_rows = [x for x in rows if near(x["r"], org_values.ratio(pcts[x["agent_id"]]))]
        summary = {"near_text": org_values.pct_text(pcts.values()), "n": len(rows), "achieved": sum(1 for x in rows if x["r"]["tier"] or (sel["kind"] == "rate" and x["r"]["value"] > 0)),
                   "near": len(near_rows), "missed": sum(1 for x in rows if x["r"]["broken"]), "top": max((x["r"]["value"] for x in rows), default=0)}
    return page(request, ctx, "promo_progress_team.html", cards=cards, sel=sel, rows=rows, summary=summary, ref=ref.isoformat(), show_amount=ctx["role"] == "main")


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
CONFIRM_WARN = "확정하면 되돌릴 수 없습니다. 지금의 사람별 결과가 그대로 남고, 그 뒤에 계약이 바뀌어도 이 프로모션의 결과는 다시 계산하지 않습니다."


def _who_name(con, login_id) -> str | None:
    if not login_id:
        return None
    r = con.execute("SELECT display_name FROM user_account WHERE login_id=?", (login_id,)).fetchone()
    return (r["display_name"] if r and r["display_name"] else login_id)


@router.get("/promo/archive", response_class=HTMLResponse)
def promo_archive(request: Request, ym: str | None = None, p: int | None = None, view: str | None = None, confirm: int | None = None,
                  msg: str | None = None, err: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    today = date.today()
    months = sorted({x["period_ym"] for x in pm.ended(con, ref=today)}, reverse=True)
    sel_ym = ym if ym in months else (months[0] if months else None)
    role = ctx["role"]
    mine = role == "agent" or (role == "manager" and view == "me")
    if mine:
        ids = [ctx["agent_id"]] if ctx["agent_id"] else []
    else:
        ids = [a["agent_id"] for a in _scope_agents(con, ctx)]
    results = pm.past(con, ids, ym=sel_ym, ref=today) if sel_ym and ids else []
    show_amount = role == "main" or mine
    for res in results:
        if mine:                                                # 본인 줄만 남긴다
            res["rows"] = [x for x in res["rows"] if x["agent_id"] == ctx["agent_id"]]
        if not show_amount:
            hidden, hidden_sum = HIDDEN_FOR_MANAGER, ("amount", "original_amount")
        else:
            hidden = hidden_sum = HIDDEN_ORIGINAL if role != "main" else ()
        res["rows"] = [{k: v for k, v in x.items() if k not in hidden} for x in res["rows"]]
        res["summary"] = {k: v for k, v in res["summary"].items() if k not in hidden_sum}
        res["period_text"] = _period_text(res["promo"])
        res["confirmed_name"] = _who_name(con, res["confirmed_by"])
    sel = None
    if not mine and results:
        sel = next((x for x in results if x["promo"]["promo_id"] == p), results[0])
    ask = sel if (role == "main" and sel and confirm == sel["promo"]["promo_id"] and sel["settlement"] == "estimate") else None
    n_done = sum(1 for x in results if x["settlement"] == "confirmed")
    return page(request, ctx, "promo_archive.html", months=months, sel_ym=sel_ym, results=results, sel=sel, mine=mine, ask=ask, msg=msg, err=err,
                show_amount=show_amount, can_confirm=role == "main", today=today.isoformat(), n_done=n_done, n_open=len(results) - n_done,
                SETTLE_KO=pm.SETTLE_KO, confirm_warn=CONFIRM_WARN)


@router.post("/promo/archive")
async def promo_archive_confirm(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    """결과 확정. 메인 관리자만 한다. 확인 칸(agree=yes)이 없으면 확정하지 않는다. pm.confirm_results 가 계정 표에서 다시 확인한다."""
    f = await request.form()
    if ctx["role"] != "main":
        auth.log(con, ctx["login_id"], "access_denied", "/promo/archive confirm")
        raise HTTPException(403, "메인 관리자만 결과를 확정합니다.")
    raw = str(f.get("promo_id") or "")
    if f.get("action") != "confirm" or not raw.isdigit():
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    pid = int(raw)
    ym = str(f.get("ym") or "")
    back = f"/promo/archive?{'ym=' + quote(ym) + '&' if ym.isdigit() else ''}p={pid}"
    if f.get("agree") != "yes":
        return RedirectResponse(f"{back}&confirm={pid}&err={quote('되돌릴 수 없다는 것을 확인하는 칸에 표시해야 확정합니다.')}#confirm", status_code=303)
    try:
        res = pm.confirm_results(con, pid, ctx, ref=date.today())
    except PermissionError as e:
        auth.log(con, ctx["login_id"], "access_denied", "/promo/archive confirm")
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        return RedirectResponse(f"{back}&err={quote(str(e))}", status_code=303)
    done = quote(f"결과 {res['summary']['n']}명을 확정했습니다.")
    return RedirectResponse(f"{back}&msg={done}", status_code=303)


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


def _tier_form_defaults(con, ctx, line_id=None, promo_id=None):
    d = {"promo_id": "", "name": "", "kind": "weekly", "insurer_id": None, "insurer_name": "", "sector": "", "caution": "", "target_note": "",
         "metric": "premium_monthly", "date_basis": pm.DEFAULT_DATE_BASIS, "cap": "", "categories": [], "combine_rule": "", "pay_timing": "",
         "windows": [("", "", "")] * 3, "tiers": [("", "fixed", "", "")] * 6, "rates": [], "source_line_id": "", "origin": None, "status": None}
    num = lambda v: "" if v is None else f"{v:g}"
    if promo_id:
        p = pm._load(con, promo_id)
        if not p:
            raise HTTPException(404, "없는 프로모션입니다.")
        tiers = [(num(t["min_value"]), t["reward_kind"], num(t["reward_value"] * 100 if t["reward_kind"] == "rate" and t["reward_value"] is not None else t["reward_value"]),
                  t["reward_label"] or "") for t in p["tiers"]]
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
                ctx: dict = Depends(guard), con=Depends(get_con)):
    d = _tier_form_defaults(con, ctx, line, edit)
    plan = original_rows(pm._load(con, edit)) if edit else None
    return page(request, ctx, "promo_tiers.html", d=d, err=err, msg=msg, plan=plan,
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


@router.post("/promo/tiers")
async def promo_tiers_save(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    f = await request.form()
    if f.get("action") == "original":
        return _save_original(con, ctx, f)
    num = lambda v: float(str(v).replace(",", "").strip()) if str(v or "").strip() else None
    promo_id = int(f["promo_id"]) if f.get("promo_id") else None
    try:
        windows = [(f.get(f"w_label_{i}", "").strip(), f.get(f"w_from_{i}", ""), f.get(f"w_to_{i}", "")) for i in range(1, 4)]
        tiers = [(num(f.get(f"t_min_{i}")), f.get(f"t_kind_{i}", "fixed"), num(f.get(f"t_value_{i}")), f.get(f"t_label_{i}", "").strip())
                 for i in range(1, 7) if num(f.get(f"t_min_{i}")) is not None]
        for _m, k, v, lab in tiers:
            if k in ("fixed", "rate") and v is None:
                raise ValueError("정액과 비율 보상에는 값을 넣어야 합니다.")
            if k in ("travel", "goods") and not lab:
                raise ValueError("여행과 물품 보상에는 이름을 넣어야 합니다.")
        tiers = [(m, k, (v / 100 if k == "rate" else v), lab) for m, k, v, lab in tiers]
        ins = con.execute("SELECT insurer_id, name FROM insurer WHERE insurer_id=?", (int(f["insurer_id"]),)).fetchone() if f.get("insurer_id") else None
        if not f.get("name", "").strip():
            raise ValueError("이름을 넣어야 합니다.")
        common = dict(name=f["name"], windows=windows, tiers=tiers, by=ctx["login_id"], kind=f.get("kind", "weekly"),
                      insurer_id=ins["insurer_id"] if ins else None, insurer_name=(ins["name"] if ins else f.get("insurer_name", "").strip() or None),
                      sector=f.get("sector") or None, categories=f.getlist("categories"), target_note=f.get("target_note", "").strip() or None,
                      metric=f.get("metric", "premium_monthly"), date_basis=f.get("date_basis") or pm.DEFAULT_DATE_BASIS, per_contract_cap=num(f.get("cap")),
                      combine_rule=f.get("combine_rule") or None, pay_timing=f.get("pay_timing") or None, caution=f.get("caution", "").strip() or None)
        if promo_id:
            res = pm.update(con, promo_id, **common)
            msg = "고쳤습니다. 바뀐 항목 " + str(len(res["changed"])) + "개를 변경 기록에 남겼습니다." if res["changed"] else "바뀐 것이 없습니다."
        else:
            pm.create(con, period_ym=ctx["ym"], source_line_id=int(f["source_line_id"]) if f.get("source_line_id") else None,
                      publish=f.get("action") == "publish", **common)
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
