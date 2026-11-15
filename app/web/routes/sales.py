"""영업·제안 묶음: 내 DB, DB 배정과 성과, 고객, 가이드 자료. 저장과 규칙은 app/sales 에 있고 여기는 부르고 그리기만 한다.

모든 주소가 guard 를 거친다. 자료는 guard 가 준 범위 안의 설계사 목록으로만 읽는다.

| 주소 | 하는 일 | 누가 |
| --- | --- | --- |
| GET /sales/leads | 내 DB 목록과 상세(?id=), 직접 등록 폼(?new=1) | 관리자와 설계사 본인 |
| POST /sales/leads | create 직접 등록, activity 활동 기록, close 종료, convert 고객으로 | 본인 DB 만 |
| GET /sales/assign | 미배정 풀, 방치 DB, 사람별 보유와 지역, 조직 단위 체결률, DB 상세(?lead=) | 메인 관리자 전체, 관리자 자기 조직 |
| POST /sales/assign | assign, auto, reclaim, reclaim_all, regions. 메인 관리자만 pool, add, handover, confirm, reveal | 같음 |
| GET /sales/customers | 내 고객 목록, 다가오는 기념일과 할 일, 상세(?id=) | 관리자와 설계사 본인 |
| POST /sales/customers | link 계약 잇기, plan 다음 할 일과 기념일 | 본인 고객만 |
| GET /sales/guide | 공지와 자료실의 가이드 분류 셋을 모아 보인다. 올리기는 공지와 자료실에서 메인 관리자가 한다 | 전원. 보이는 글만 |

이름과 연락처: 목록에서는 연락처를 늘 가린다. 이름은 원문을 볼 수 있는 사람(담당, 직속 관리자)에게만 원문이다.
상세에서 담당과 직속 관리자는 원문을 보고, 메인 관리자는 사유를 적은 뒤 본다. 원문을 보인 일은 모두 sales_contact_view 로 남는다.
범위 밖 DB 와 고객을 연 시도는 없는 것과 같은 404 로 답하고 access_denied 로 남긴다. D-12 제안대로 둔 가정이다.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.org.sync import org_texts
from app.sales import core, stats
from app.sales import customers as cs
from app.sales import leads as ld
from app.web import auth
from app.web import board as bd
from app.web.deps import _in, _q, get_con, guard, page

router = APIRouter()
FLASH = "sales_flash"
LEVELS = ("team", "branch", "hq")


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _flash(request, text, kind="ok"):
    request.session[FLASH] = {"kind": kind, "text": text}


def _deny(con, ctx, what: str, message="이 일을 할 권한이 없습니다."):
    auth.log(con, ctx["login_id"], "access_denied", what)
    raise HTTPException(403, message)


def _not_found(con, ctx, what: str | None = None, message="없는 자료입니다."):
    if what:                                              # 있는 자료를 범위 밖 사람이 열었다
        auth.log(con, ctx["login_id"], "access_denied", what)
    raise HTTPException(404, message)


def scope_ids(con, ctx) -> list:
    """메뉴 범위 안의 설계사. all 은 전체, org 는 auth.context 가 준 범위, self 는 본인."""
    if ctx["scope"] == "all":
        return [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent")]
    if ctx["scope"] == "org":
        return sorted(ctx["agent_ids"])
    return [ctx["agent_id"]] if ctx.get("agent_id") else []


def _agents(con, ids) -> dict:
    q, params = _in(ids)
    return {r["agent_id"]: r for r in _q(con, f"SELECT agent_id, internal_code code, name, status FROM agent WHERE agent_id IN ({q})", params)}


def _shown(lead: dict, plain: bool, listing: bool = False) -> dict:
    """화면에 내보낼 DB. 원문 칸은 지우고 보일 값만 남긴다. 목록(listing)이면 연락처는 원문을 볼 수 있는 사람에게도 가린다."""
    out = {k: v for k, v in lead.items() if k not in ("name", "phone")}
    out["name_v"] = lead["name"] if plain else core.mask_name(lead["name"])
    out["phone_v"] = lead["phone"] if plain and not listing else core.mask_phone(lead["phone"])
    out["plain"] = plain and not listing
    return out


def _guides(con, ctx, limit=3) -> list:
    bd.ensure(con)
    return [p for p in bd.visible_posts(con, ctx) if p["category"] in bd.GUIDE_CATS][:limit]


# ================================================================ 내 DB
def _lead_page(request, ctx, con, *, status="open", opened=None, new=False, form=None, error=None, status_code=200):
    me, day = ctx.get("agent_id"), core.today()
    status = status if status in ("open", "won", "lost", "all") else "open"
    rows = ld.for_agent(con, me, day, "" if status == "all" else status) if me else []
    every = ld.for_agent(con, me, day) if me else []
    counts = {k: sum(1 for r in every if r["status"] in (core.OPEN if k == "open" else (k,))) for k in ("open", "won", "lost")}
    counts["all"] = len(every)
    month = day.strftime("%Y-%m")
    acts = {r["kind"]: r["n"] for r in _q(con, "SELECT kind, COUNT(*) n FROM sales_activity WHERE agent_id=? AND substr(done_on,1,7)=? GROUP BY kind",
                                          (me or -1, month))}
    resp = page(request, ctx, "sales_leads.html", status_code=status_code, rows=[_shown(r, True, listing=True) for r in rows], counts=counts, status=status,
                stale=sum(1 for r in every if r["stale"]), warn=sum(1 for r in every if r["warn"]), acts=acts, opened=opened, new=new,
                form=form or {}, error=error, flash=request.session.pop(FLASH, None), now=db.now(), day=day.isoformat(),
                REGIONS=core.REGIONS, KIND_KO=core.KIND_KO, SOURCE_KO=core.SOURCE_KO, STALE=core.setting("stale"), HOLD=core.setting("hold"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _my_lead(con, ctx, lead_id) -> dict:
    lead = ld.get(con, lead_id) if lead_id else None
    if lead is None:
        _not_found(con, ctx)
    if lead["agent_id"] != ctx.get("agent_id"):
        _not_found(con, ctx, f"/sales/leads lead:{lead_id}")
    return lead


@router.get("/sales/leads", response_class=HTMLResponse)
def sales_leads(request: Request, status: str = "open", id: str = "", new: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    core.ensure(con)
    if id:
        lead = _my_lead(con, ctx, _int(id))
        day = core.today()
        opened = _shown(ld.decorate(lead, day), True)
        core.log_view(con, ctx, f"lead:{lead['lead_id']}", "own")
        opened.update(acts=ld.activities(con, lead["lead_id"]), history=ld.history(con, lead["lead_id"]), guides=_guides(con, ctx))
        return _lead_page(request, ctx, con, status=status, opened=opened)
    return _lead_page(request, ctx, con, status=status, new=new == "1")


@router.post("/sales/leads", response_class=HTMLResponse)
async def sales_leads_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    core.ensure(con)
    f = await request.form()
    action, me = f.get("action", ""), ctx.get("agent_id")
    if not me:
        _deny(con, ctx, "/sales/leads no-agent")
    if action == "create":
        keep = {k: str(f.get(k, ""))[:200] for k in ("name", "phone", "region", "memo", "source")}
        src = keep["source"] if keep["source"] in ("self", "referral", "event") else "self"
        try:
            lid = ld.create(con, name=keep["name"], phone=keep["phone"], region=keep["region"], source=src, by=ctx["login_id"],
                            memo=keep["memo"], consent=f.get("consent") == "1", agent_id=me)
        except ValueError as e:
            con.rollback()
            return _lead_page(request, ctx, con, new=True, form=keep, error=str(e), status_code=400)
        con.commit()
        _flash(request, "등록했습니다. 내 DB 로 바로 들어왔습니다.")
        return RedirectResponse(f"/sales/leads?id={lid}", status_code=303)
    lid = _int(f.get("lead_id"))
    _my_lead(con, ctx, lid)
    try:
        if action == "activity":
            ld.add_activity(con, lid, me, f.get("kind", ""), f.get("done_on", ""), f.get("result", ""), ctx["login_id"])
            msg = "활동을 남겼습니다."
        elif action == "close":
            ld.close(con, lid, me, ctx["login_id"], f.get("reason", ""))
            msg = "DB 를 종료했습니다. 기록은 남습니다."
        elif action == "convert":
            cid = ld.convert(con, lid, me, ctx["login_id"])
            con.commit()
            _flash(request, "고객으로 바꿨습니다. 계약을 잇고 다음 할 일을 적어 주세요.")
            return RedirectResponse(f"/sales/customers?id={cid}", status_code=303)
        else:
            raise HTTPException(400, "처리할 수 없는 요청입니다.")
    except ValueError as e:
        con.rollback()
        _flash(request, str(e), "bad")
        return RedirectResponse(f"/sales/leads?id={lid}#act", status_code=303)
    con.commit()
    _flash(request, msg)
    return RedirectResponse(f"/sales/leads?id={lid}", status_code=303)


# ================================================================ DB 배정과 성과
def _candidates(con, ctx) -> list:
    """배정받을 수 있는 사람. 범위 안 재직자. 지역과 보유 수를 붙인다."""
    ids = scope_ids(con, ctx)
    q, params = _in(ids)
    rows = _q(con, f"SELECT agent_id, internal_code code, name FROM agent WHERE agent_id IN ({q}) AND status='active' ORDER BY internal_code", params)
    regions = ld.regions_of(con, [r["agent_id"] for r in rows])
    held = ld.open_counts(con, [r["agent_id"] for r in rows])
    per = stats.by_agent(con, [r["agent_id"] for r in rows])
    for r in rows:
        r["regions"] = regions.get(r["agent_id"], [])
        r["held"] = held.get(r["agent_id"], 0)
        v = per.get(r["agent_id"]) or {"total": 0, "won": 0}
        r["total"], r["won"], r["rate"] = v["total"], v["won"], stats.rate(v["won"], v["total"])
    return rows


def _lead_view(con, ctx, lead_id, reason=None) -> dict:
    """배정 화면의 DB 상세. 범위 밖이면 404. 원문은 직속 관리자와, 사유를 적은 메인 관리자에게만."""
    lead = ld.get(con, lead_id) if lead_id else None
    if lead is None:
        _not_found(con, ctx)
    if not ld.in_scope(con, ctx, lead, scope_ids(con, ctx)):
        _not_found(con, ctx, f"/sales/assign lead:{lead_id}")
    acc = core.access(con, ctx, lead["agent_id"])
    plain = acc in ("own", "head") or (acc == "reason" and bool(reason))
    if plain:
        core.log_view(con, ctx, f"lead:{lead_id}", "main" if acc == "reason" else acc, reason)
    v = _shown(ld.decorate(lead, core.today()), plain)
    v.update(access=acc, acts=ld.activities(con, lead_id), history=ld.history(con, lead_id))
    a = _agents(con, [lead["agent_id"]]).get(lead["agent_id"]) if lead["agent_id"] else None
    v["agent_name"] = a["name"] if a else None
    return v


def _assign_page(request, ctx, con, *, level="", opened=None, ask=None, error=None, regions_for=None, status_code=200):
    day = core.today()
    is_main = ctx["role"] == "main"
    ids = scope_ids(con, ctx)
    conv_orgs = None if is_main else set(ctx.get("org_ids") or ())
    if level not in LEVELS:                               # 고르지 않았으면 셀 DB 가 있는 가장 아래 단위
        level = next((lv for lv in LEVELS if stats.conversion(con, ids, ctx["ym"], lv, conv_orgs)), "team")
    cands = _candidates(con, ctx)
    names = _agents(con, ids)
    texts = org_texts(con)
    pool = [_shown(r, False, listing=True) for r in ld.pool(con, ctx, day)]
    for r in pool:
        r["pool_name"] = texts.get(r["pool_org_id"], "본부 풀") if r["pool_org_id"] else "본부 풀"
    held = ld.held(con, ids, day)
    stale_rows = []
    for r in held:
        if r["stale"] or r["warn"]:
            v = _shown(r, core.access(con, ctx, r["agent_id"]) in ("own", "head"), listing=True)
            v["agent_name"] = names[r["agent_id"]]["name"] if r["agent_id"] in names else "-"
            stale_rows.append(v)
    stale_rows.sort(key=lambda r: -(r["idle"] or 0))
    conv = stats.conversion(con, ids, ctx["ym"], level, conv_orgs)
    tot = stats.by_agent(con, ids)
    total, won = sum(v["total"] for v in tot.values()), sum(v["won"] for v in tot.values())
    extra = {}
    if is_main:
        extra = {"handover": cs.handover_candidates(con, ctx["ym"]), "pending": cs.pending_handover(con),
                 "orgs": sorted(((o, t) for o, t in texts.items()), key=lambda x: x[1])}
    pick_agent = next((c for c in cands if c["agent_id"] == regions_for), None) if regions_for else None
    resp = page(request, ctx, "sales_assign.html", status_code=status_code, is_main=is_main, pool=pool, stale_rows=stale_rows, cands=cands,
                conv=conv, level=level, total=total, won=won, rate=stats.rate(won, total), held_n=len(held),
                n_stale=sum(1 for r in held if r["stale"]), opened=opened, ask=ask, error=error, pick_agent=pick_agent,
                flash=request.session.pop(FLASH, None), now=db.now(), REGIONS=core.REGIONS, SOURCE_KO=core.SOURCE_KO, LEVELS=LEVELS,
                STALE=core.setting("stale"), WARN=core.setting("warn"), HOLD=core.setting("hold"), **extra)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/sales/assign", response_class=HTMLResponse)
def sales_assign(request: Request, level: str = "", lead: str = "", ask: str = "", regions: str = "",
                 ctx: dict = Depends(guard), con=Depends(get_con)):
    core.ensure(con)
    opened = _lead_view(con, ctx, _int(lead)) if lead else None
    ask_row = None
    if ask:
        if ctx["role"] != "main":
            _deny(con, ctx, "/sales/assign handover")
        aid = _int(ask)
        ask_row = next((h for h in cs.handover_candidates(con, ctx["ym"]) if h["agent_id"] == aid), None)
        if ask_row is None:
            _not_found(con, ctx)
    reg = _int(regions) if regions else None
    if reg is not None and reg not in {c["agent_id"] for c in _candidates(con, ctx)}:
        _not_found(con, ctx, f"/sales/assign regions:{reg}")
    return _assign_page(request, ctx, con, level=level, opened=opened, ask=ask_row, regions_for=reg)


MAIN_ONLY = {"pool", "add", "handover", "confirm", "reveal"}


@router.post("/sales/assign", response_class=HTMLResponse)
async def sales_assign_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    core.ensure(con)
    f = await request.form()
    action, by = f.get("action", ""), ctx["login_id"]
    if action in MAIN_ONLY and ctx["role"] != "main":
        _deny(con, ctx, f"/sales/assign {action}", "메인 관리자만 할 수 있는 일입니다.")
    ids = scope_ids(con, ctx)
    cands = {c["agent_id"] for c in _candidates(con, ctx)}
    day = core.today()
    back = "/sales/assign"

    def lead_in_scope(lid):
        lead = ld.get(con, lid) if lid else None
        if lead is None:
            _not_found(con, ctx)
        if not ld.in_scope(con, ctx, lead, ids):
            _not_found(con, ctx, f"/sales/assign lead:{lid}")
        return lead

    try:
        if action == "assign":
            lid, aid = _int(f.get("lead_id")), _int(f.get("agent_id"))
            lead_in_scope(lid)
            if aid not in cands:
                _deny(con, ctx, f"/sales/assign agent:{aid}", "범위 밖의 사람에게는 배정하지 않습니다.")
            ld.assign(con, lid, aid, by)
            msg = "배정했습니다."
        elif action == "auto":
            pool = [r["lead_id"] for r in ld.pool(con, ctx, day)]
            n, left = ld.auto_assign(con, pool, sorted(cands), by)
            msg = f"지역 매칭으로 {n}건을 배정했습니다." + (f" {left}건은 맞는 사람이 없어 남겼습니다." if left else "")
        elif action == "reclaim":
            lid = _int(f.get("lead_id"))
            lead = lead_in_scope(lid)
            if not ld.decorate(dict(lead), day)["stale"]:
                raise ValueError(f"활동 없이 {core.setting('stale')}일이 지난 DB 만 회수합니다.")
            ld.reclaim(con, lid, by, ctx["ym"])
            msg = "회수했습니다. 소속 조직의 미배정 풀로 돌아갔습니다."
        elif action == "reclaim_all":
            rows = ld.stale(con, ids, day)
            for r in rows:
                ld.reclaim(con, r["lead_id"], by, ctx["ym"])
            msg = f"방치 DB {len(rows)}건을 회수했습니다."
        elif action == "regions":
            aid = _int(f.get("agent_id"))
            if aid not in cands:
                _deny(con, ctx, f"/sales/assign regions:{aid}", "범위 밖의 사람입니다.")
            keep = ld.set_regions(con, aid, f.getlist("region"), by)
            msg = f"맡을 지역을 {len(keep)}곳으로 정했습니다."
        elif action == "pool":
            ld.move_pool(con, _int(f.get("lead_id")), _int(f.get("org_id")), by)
            msg = "조직 풀로 넘겼습니다. 그 조직의 관리자가 배정합니다."
        elif action == "add":
            org = _int(f.get("org_id")) if f.get("org_id") else None
            src = f.get("source") if f.get("source") in ("company", "referral", "event") else "company"
            ld.create(con, name=f.get("name", ""), phone=f.get("phone", ""), region=f.get("region", ""), source=src, by=by,
                      preferred_time=f.get("time") or None, memo=f.get("memo", ""), consent=f.get("consent") == "1", pool_org_id=org)
            msg = "미배정 풀에 넣었습니다."
        elif action == "handover":
            aid = _int(f.get("agent_id"))
            if f.get("confirm") != "1":
                return RedirectResponse(f"{back}?ask={aid}#handover", status_code=303)
            out = cs.handover(con, aid, by, ctx["ym"])
            msg = f"고객 {out['customers']}명을 넘기고 DB {out['leads']}건을 회수했습니다. 넘긴 고객은 준법 확인 전까지 연락처가 가려집니다."
            back += "#handover"
        elif action == "confirm":
            cs.confirm(con, _int(f.get("customer_id")), by)
            msg = "준법 확인을 남겼습니다. 받은 관리자가 연락처를 봅니다."
            back += "#handover"
        elif action == "reveal":
            lid = _int(f.get("lead_id"))
            reason = core.clean_reason(f.get("reason"))
            if not reason:
                opened = _lead_view(con, ctx, lid)
                return _assign_page(request, ctx, con, opened=opened, status_code=400,
                                    error=f"조회 사유를 {core.REASON_MIN}자에서 {core.REASON_MAX}자 사이로 적어 주세요.")
            return _assign_page(request, ctx, con, opened=_lead_view(con, ctx, lid, reason=reason))
        else:
            raise HTTPException(400, "처리할 수 없는 요청입니다.")
    except ValueError as e:
        con.rollback()
        _flash(request, str(e), "bad")
        return RedirectResponse(back, status_code=303)
    con.commit()
    _flash(request, msg)
    return RedirectResponse(back, status_code=303)


# ================================================================ 고객
def _my_customer(con, ctx, cid) -> dict:
    cust = cs.get(con, cid) if cid else None
    if cust is None:
        _not_found(con, ctx)
    if cust["agent_id"] != ctx.get("agent_id"):
        _not_found(con, ctx, f"/sales/customers customer:{cid}")
    return cust


def _cust_shown(c: dict, plain: bool, listing: bool = False) -> dict:
    out = {k: v for k, v in c.items() if k not in ("name", "phone")}
    out["name_v"] = c["name"] if plain else core.mask_name(c["name"])
    out["phone_v"] = c["phone"] if plain and not listing else core.mask_phone(c["phone"])
    out["plain"] = plain and not listing
    out["handover_ko"] = core.HANDOVER_KO.get(c.get("handover_status") or "", "")
    return out


@router.get("/sales/customers", response_class=HTMLResponse)
def sales_customers(request: Request, id: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    core.ensure(con)
    me, day = ctx.get("agent_id"), core.today()
    opened = None
    if id:
        cust = _my_customer(con, ctx, _int(id))
        acc = cs.access(con, ctx, cust)
        plain = acc == "own"
        if plain:
            core.log_view(con, ctx, f"customer:{cust['customer_id']}", "own")
        opened = _cust_shown(cust, plain)
        opened.update(access=acc, contracts=cs.contracts(con, cust["customer_id"]),
                      acts=ld.activities(con, cust["lead_id"]) if cust["lead_id"] else [],
                      linkable=cs.linkable(con, me) if plain else [])
    rows = []
    for c in (cs.for_agent(con, me) if me else []):
        rows.append(_cust_shown(c, c["handover_status"] != "pending", listing=True))
    pending = {c["customer_id"] for c in rows if c["handover_status"] == "pending"}
    events = [e for e in (cs.upcoming(con, me, day) if me else []) if e["customer_id"] not in pending]
    resp = page(request, ctx, "sales_customers.html", rows=rows, events=events, opened=opened, day=day.isoformat(), now=db.now(),
                flash=request.session.pop(FLASH, None))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/sales/customers", response_class=HTMLResponse)
async def sales_customers_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    core.ensure(con)
    f = await request.form()
    action, me = f.get("action", ""), ctx.get("agent_id")
    cid = _int(f.get("customer_id"))
    cust = _my_customer(con, ctx, cid)
    if cs.access(con, ctx, cust) == "pending":
        _deny(con, ctx, f"/sales/customers pending:{cid}", "준법 확인 전인 고객입니다. 메인 관리자의 확인을 기다려 주세요.")
    try:
        if action == "link":
            cs.link(con, cid, me, _int(f.get("contract_id")), ctx["login_id"])
            msg = "계약을 이었습니다."
        elif action == "plan":
            cs.set_plan(con, cid, me, ctx["login_id"], next_action=f.get("next_action"), next_on=f.get("next_on"),
                        birth_md=f.get("birth_md"), anniv_md=f.get("anniv_md"), anniv_label=f.get("anniv_label"))
            msg = "저장했습니다."
        else:
            raise HTTPException(400, "처리할 수 없는 요청입니다.")
    except PermissionError:
        con.rollback()
        _deny(con, ctx, f"/sales/customers link:{cid}", "내 계약만 이을 수 있습니다.")
    except ValueError as e:
        con.rollback()
        _flash(request, str(e), "bad")
        return RedirectResponse(f"/sales/customers?id={cid}", status_code=303)
    con.commit()
    _flash(request, msg)
    return RedirectResponse(f"/sales/customers?id={cid}", status_code=303)


# ================================================================ 가이드 자료
@router.get("/sales/guide", response_class=HTMLResponse)
def sales_guide(request: Request, cat: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    bd.ensure(con)
    cat = cat if cat in bd.GUIDE_CATS else ""
    posts = [p for p in bd.visible_posts(con, ctx) if p["category"] in bd.GUIDE_CATS]
    counts = {k: sum(1 for p in posts if p["category"] == k) for k in bd.GUIDE_CATS}
    shown = [p for p in posts if not cat or p["category"] == cat]
    return page(request, ctx, "sales_guide.html", posts=shown, counts=counts, total=len(posts), cat=cat, is_main=ctx["role"] == "main",
                CATS={k: bd.CATEGORY_KO[k] for k in bd.GUIDE_CATS})
