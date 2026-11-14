"""프로모션 묶음: 진행 중 프로모션, 달성 현황, 게시 관리, 구간과 조건 입력, 조직 부문 시상."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.engine import org_award as oa
from app.engine import promotion as pm
from app.web import auth
from app.web.deps import _cards, _in, _q, get_con, guard, page

router = APIRouter()


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
        rows = [{**a, "r": res[a["agent_id"]]} for a in agents]
        rows.sort(key=lambda x: -x["r"]["value"])
        near = [x for x in rows if x["r"]["next"] and x["r"]["value"] > 0 and x["r"]["next"]["gap"] <= x["r"]["next"]["min_value"] * 0.3]
        summary = {"n": len(rows), "achieved": sum(1 for x in rows if x["r"]["tier"] or (sel["kind"] == "rate" and x["r"]["value"] > 0)),
                   "near": len(near), "missed": sum(1 for x in rows if x["r"]["broken"]), "top": max((x["r"]["value"] for x in rows), default=0)}
    return page(request, ctx, "promo_progress_team.html", cards=cards, sel=sel, rows=rows, summary=summary, ref=ref.isoformat(), show_amount=ctx["role"] == "main")


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
         "metric": "premium_monthly", "date_basis": "contract_date", "cap": "", "categories": [], "combine_rule": "", "pay_timing": "",
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


@router.get("/promo/tiers", response_class=HTMLResponse)
def promo_tiers(request: Request, line: int | None = None, edit: int | None = None, err: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    return page(request, ctx, "promo_tiers.html", d=_tier_form_defaults(con, ctx, line, edit), err=err,
                cats=_q(con, "SELECT code, name FROM product_category ORDER BY sort_order"),
                insurers=_q(con, "SELECT insurer_id, name, sector FROM insurer WHERE active=1 ORDER BY name"))


@router.post("/promo/tiers")
async def promo_tiers_save(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    f = await request.form()
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
                      metric=f.get("metric", "premium_monthly"), date_basis=f.get("date_basis", "contract_date"), per_contract_cap=num(f.get("cap")),
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
        blocks.append({**d, "rows": oa.view_rows(rows, ctx), "total": len(rows), "podium": (done or [r for r in rows if r["winner"]])[:3], "confirmed": done,
                       "me": me, "top": rows[0]["value"] if rows else 0, "can_manage": mine})
    form = None
    if manage and (edit or new):
        d = oa.load(con, edit) if edit else None
        if edit and (not d or not oa.can_manage(ctx, d["org_id"])):
            raise HTTPException(403, "이 부문을 고칠 권한이 없습니다.")
        form = d or {"division_id": "", "org_id": None, "name": "", "metric": "converted_premium", "sector": None, "category_list": [], "top_n": 3,
                     "min_value": None, "effective_from": ym, "effective_to": None, "note": "", "rewards": {}}
    return page(request, ctx, "org_awards.html", blocks=blocks, form=form, manage=manage, msg=msg, err=err, oa=oa,
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
