"""실적 묶음: 기간별 업적, 구분별 집계, 랭킹, 조직 비교, 명예의전당.

계산은 app/engine/results.py 가 한다. 이 파일은 고른 값을 확인하고 범위 안 설계사 목록이나 조직 목록을 넘길 뿐이다.
범위는 guard 가 준 메뉴 범위(all, org, self)를 따른다. 범위 밖 조직을 고르면 403 이다.

- 기간별 업적과 구분별 집계: 범위 안 설계사 목록(scope_ids)으로만 읽는다. 조직을 고르면 그 조직 아래 사람과 범위의 겹침만 읽는다.
- 랭킹: results.ranking_for() 가 고를 수 있는 조직(ranking_orgs)을 막고 보이는 줄을 거른다(상위 10명과 자기 범위).
- 조직 비교: 범위 안 조직 목록(ctx['org_ids'])만 넘긴다.
- 명예의전당: 저장된 전월 랭킹 run(ranking_snapshot)과 확정한 조직 부문 수상(org_award_result), 수상 이력(agent_certification_award).

D-17. 이 묶음에는 수수료와 시상 금액을 싣지 않는다. 명예의전당의 조직 부문 시상 내용은 D-45 제안대로(가정) 모두에게
공개하는 순위별 상품표로 보고 모든 역할에게 적는다. 규칙은 app/web/awards.py 한 곳이고 조직 부문 시상 화면과 같다.
계약 원장은 app/web/routes/contracts.py 에 따로 있다.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.engine import org_award as oa
from app.engine import orgs
from app.engine import results as rs
from app.org import levels
from app.periods import ym_add
from app.web import awards
from app.web.deps import _in, _q, get_con, guard, page

router = APIRouter()

SECTORS = {"": "전체", "life": "생보", "nonlife": "손보"}
METRICS = {"converted_premium": "인정보험료", "monthly_premium": "월납보험료", "net_count": "순건수"}
HALL_METRICS = {"recognized_premium": "인정보험료", "net_count": "순건수"}        # 랭킹 run 이 남기는 두 지표


# ---------------------------------------------------------------- 범위와 고른 값
def scope_ids(con, ctx) -> list:
    """메뉴 범위 안의 설계사. all 은 전체, org 는 auth.context 가 준 범위, self 는 본인이다."""
    if ctx["scope"] == "all":
        return [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent")]
    if ctx["scope"] == "org":
        return sorted(ctx["agent_ids"])
    return [ctx["agent_id"]] if ctx.get("agent_id") else []


def scope_orgs(con, ctx) -> list:
    """고를 수 있는 조직. 메인 관리자는 전부, 관리자는 맡은 조직과 그 아래, 설계사는 없다. 위 계층부터."""
    if ctx["scope"] == "self":
        return []
    sql, params = "SELECT org_id, name, level, path_key FROM org_unit WHERE active=1", []
    if ctx["scope"] == "org":
        q, params = _in(ctx["org_ids"])
        sql += f" AND org_id IN ({q})"
    ko = levels.level_ko()
    rows = [{**r, "level_ko": ko.get(r["level"], r["level"]), "depth": levels.depth(r["level"])} for r in _q(con, sql, params)]
    return sorted(rows, key=lambda r: (r["depth"], r["path_key"] or r["name"], r["org_id"]))


def pick_org(con, ctx, org_id, ids) -> tuple:
    """고른 조직으로 사람을 좁힌다. (조직 또는 None, 좁힌 사람). 범위 밖 조직이면 403 이다."""
    if org_id is None:
        return None, ids
    allowed = {o["org_id"]: o for o in scope_orgs(con, ctx)}
    if org_id not in allowed:
        raise HTTPException(403, "이 조직의 실적을 볼 수 없습니다.")
    ym = ctx["ym"]
    under = sorted(orgs.descendants(con, org_id))
    q, params = _in(under)
    inside = {r["agent_id"] for r in con.execute(
        f"""SELECT DISTINCT agent_id FROM agent_org_history WHERE org_id IN ({q}) AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)""",
        (*params, ym, ym))}
    return allowed[org_id], [a for a in ids if a in inside]


def months(con, ctx) -> list:
    """고를 수 있는 기준 월. 업적 run 이 끝난 달이고 최근 달부터다. 로그인 문맥의 기준 월보다 뒤는 없다."""
    got = [r["period_ym"] for r in con.execute(
        "SELECT DISTINCT period_ym FROM calc_run WHERE run_type='performance' AND status='done' AND period_ym<=? ORDER BY period_ym DESC", (ctx["ym"],))]
    return got or [ctx["ym"]]


def pick_ym(con, ctx, ym) -> tuple:
    opts = months(con, ctx)
    return (ym if ym in opts else ctx["ym"]), opts


def pick(value, options, default):
    return value if value in options else default


def fresh(con, ym) -> dict:
    """기준 시각. 계약 적재 시각과 그 달 업적 run 의 완료 시각."""
    return rs.freshness(con, ym)


def label_months(rows) -> list:
    """달마다의 줄에 막대 이름표를 붙인다."""
    for m in rows:
        m["label"], m["sub"] = f"{int(m['ym'][4:])}월", f"순건수 {m['net_count']:g}건"
    return rows


def filters(pt, sector, ym, org=None, **extra) -> dict:
    """화면의 거르기 단추가 주소에 실을 값. 기본값이면 싣지 않는다."""
    out = {"pt": pt if pt != "month" else "", "sector": sector, "ym": ym, "org": org or ""}
    out.update(extra)
    return out


# ---------------------------------------------------------------- 기간별 업적
@router.get("/perf/period", response_class=HTMLResponse)
def perf_period(request: Request, pt: str = "month", sector: str = "", ym: str | None = None, org: int | None = None,
                ctx: dict = Depends(guard), con=Depends(get_con)):
    pt, sector = pick(pt, rs.PERIOD_KO, "month"), pick(sector, SECTORS, "")
    ym, yms = pick_ym(con, ctx, ym)
    sel, ids = pick_org(con, ctx, org, scope_ids(con, ctx))
    s = rs.period_summary(con, ids, pt, ym, sector=sector or None)
    label_months(s["months"])
    return page(request, ctx, "perf_period.html", s=s, sel=sel, orgs=scope_orgs(con, ctx), yms=yms, ym=ym, pt=pt, sector=sector,
                SECTORS=SECTORS, PERIODS=rs.PERIOD_KO, fresh=fresh(con, ym), f=filters(pt, sector, ym, org))


# ---------------------------------------------------------------- 구분별 집계
@router.get("/perf/category", response_class=HTMLResponse)
def perf_category(request: Request, pt: str = "month", sector: str = "", by: str = "category", ym: str | None = None, org: int | None = None,
                  ctx: dict = Depends(guard), con=Depends(get_con)):
    pt, sector, by = pick(pt, rs.PERIOD_KO, "month"), pick(sector, SECTORS, ""), pick(by, ("category", "sector"), "category")
    if by == "sector":
        sector = ""                                       # 업권별로 나눌 때는 업권 거르기를 쓰지 않는다
    ym, yms = pick_ym(con, ctx, ym)
    sel, ids = pick_org(con, ctx, org, scope_ids(con, ctx))
    b = rs.breakdown(con, ids, pt, ym, by=by, sector=sector or None)
    return page(request, ctx, "perf_category.html", b=b, sel=sel, orgs=scope_orgs(con, ctx), yms=yms, ym=ym, pt=pt, sector=sector, by=by,
                SECTORS=SECTORS, PERIODS=rs.PERIOD_KO, fresh=fresh(con, ym), f=filters(pt, sector, ym, org, by=by if by != "category" else ""))


# ---------------------------------------------------------------- 랭킹
@router.get("/perf/ranking", response_class=HTMLResponse)
def perf_ranking(request: Request, pt: str = "month", sector: str = "", metric: str = "converted_premium", ym: str | None = None,
                 org: int | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    pt, sector, metric = pick(pt, rs.PERIOD_KO, "month"), pick(sector, SECTORS, ""), pick(metric, METRICS, "converted_premium")
    ym, yms = pick_ym(con, ctx, ym)
    choices = rs.ranking_orgs(con, ctx)
    if not choices:
        return page(request, ctx, "perf_ranking.html", r=None, choices=[], yms=yms, ym=ym, pt=pt, sector=sector, metric=metric,
                    SECTORS=SECTORS, PERIODS=rs.PERIOD_KO, METRICS=METRICS, fresh=fresh(con, ym), f=filters(pt, sector, ym))
    org_id = org if org is not None else choices[0]["org_id"]
    try:
        r = rs.ranking_for(con, ctx, org_id, ym, period_type=pt, metric=metric, sector=sector or None)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    return page(request, ctx, "perf_ranking.html", r=r, choices=choices, yms=yms, ym=ym, pt=pt, sector=sector, metric=metric,
                SECTORS=SECTORS, PERIODS=rs.PERIOD_KO, METRICS=METRICS, fresh=fresh(con, ym),
                f=filters(pt, sector, ym, org if org is not None else "", metric=metric if metric != "converted_premium" else ""))


# ---------------------------------------------------------------- 조직 비교
@router.get("/perf/org-compare", response_class=HTMLResponse)
def perf_org_compare(request: Request, pt: str = "month", sector: str = "", level: str | None = None, parent: int | None = None,
                     ym: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    pt, sector = pick(pt, rs.PERIOD_KO, "month"), pick(sector, SECTORS, "")
    ym, yms = pick_ym(con, ctx, ym)
    units = scope_orgs(con, ctx)
    org_ids = {u["org_id"] for u in units}
    if parent is not None and parent not in org_ids:
        raise HTTPException(403, "이 조직의 실적을 볼 수 없습니다.")
    root = levels.root_code()
    under = orgs.descendants(con, parent) & org_ids if parent is not None else org_ids
    count = {}
    for u in units:
        if u["level"] != root and u["org_id"] in under:
            count[u["level"]] = count.get(u["level"], 0) + 1
    lv_opts = [lv for lv in levels.codes() if lv in count]
    if level not in lv_opts:
        level = next((lv for lv in lv_opts if count[lv] >= 2), lv_opts[0] if lv_opts else None)
    c = rs.org_compare(con, ym, org_ids=org_ids, level=level, parent_id=parent, period_type=pt, sector=sector or None) if level else None
    for r in (c or {}).get("rows", []):
        r["rate_label"] = f"가동 {r['active']} / {r['headcount']}명"
    parents = [u for u in units if level and u["depth"] < levels.depth(level)]
    return page(request, ctx, "perf_org_compare.html", c=c, level=level, lv_opts=lv_opts, LEVEL_NAMES=levels.level_ko(), parents=parents,
                parent=parent, yms=yms, ym=ym, pt=pt, sector=sector, SECTORS=SECTORS, PERIODS=rs.PERIOD_KO, fresh=fresh(con, ym),
                f=filters(pt, sector, ym, None, level=level or "", parent=parent or ""))


# ---------------------------------------------------------------- 명예의전당
def hall_ranking(con, ctx, prev, metric) -> dict | None:
    """전월 랭킹 run 의 전체 설계사 순위. 맨 위 조직의 순위를 볼 수 있는 사람만 본다. 보이는 줄은 랭킹 화면과 같다."""
    root = next((o for o in rs.ranking_orgs(con, ctx) if o["level"] == levels.root_code()), None)
    if root is None:
        return None
    run = con.execute("SELECT MAX(run_id) r FROM calc_run WHERE run_type='ranking' AND period_ym=? AND status='done'", (prev,)).fetchone()["r"]
    if not run:
        return {"root": root, "rows": [], "total": 0, "run_at": None, "me": None}
    rows = _q(con, """SELECT s.rank_no rank, s.agent_id, a.internal_code code, a.name, o.name org, s.value
                      FROM ranking_snapshot s JOIN agent a ON a.agent_id=s.agent_id LEFT JOIN org_unit o ON o.org_id=s.org_id
                      WHERE s.run_id=? AND s.period_type='month' AND s.period_key=? AND s.metric=? AND s.scope='all'
                      ORDER BY s.rank_no, a.internal_code""", (run, prev, metric))
    run_at = con.execute("SELECT finished_at FROM calc_run WHERE run_id=?", (run,)).fetchone()["finished_at"]
    me = next((r for r in rows if r["agent_id"] == ctx.get("agent_id")), None)
    return {"root": root, "rows": oa.view_rows(rows, ctx), "total": len(rows), "run_at": run_at, "me": me}


def hall_divisions(con, ctx, prev) -> list:
    """전월에 확정한 조직 부문 수상자. 볼 수 있는 부문(org_award.visible)만, 줄은 상위 10명과 자기 범위다.
    순위별 시상 내용은 모두에게 공개하는 상품표라 역할과 상관없이 싣는다. 합계는 만들지 않는다 (awards.py, D-45 가정, D-17)."""
    out = []
    for d in oa.visible(con, ctx, prev, include_inactive=True):
        done = oa.confirmed(con, d["division_id"], prev)
        rows = awards.label_rows(oa.view_rows(done, ctx))
        if not rows:
            continue
        out.append({**d, "winners": rows, "prizes": awards.confirmed_prizes(done)})
    return out


def hall_history(con, ctx) -> list:
    """수상 이력. 조직 부문 확정 수상과 영업우수·명예의전당 같은 수상 프로그램. 범위 안 사람만이다."""
    ids = scope_ids(con, ctx)
    if not ids:
        return []
    q, params = _in(ids)
    awards = _q(con, f"""SELECT w.agent_id, a.name, a.internal_code code, p.name title, CAST(w.year AS TEXT) at, 'program' kind
                         FROM agent_certification_award w JOIN agent a ON a.agent_id=w.agent_id JOIN certification_program p ON p.program_id=w.program_id
                         WHERE w.agent_id IN ({q}) AND p.category='award'""", params)
    org = _q(con, f"""SELECT r.agent_id, a.name, a.internal_code code, d.name || ' ' || r.rank_no || '위' title, r.period_ym at, 'division' kind
                      FROM org_award_result r JOIN agent a ON a.agent_id=r.agent_id JOIN org_award_division d ON d.division_id=r.division_id
                      WHERE r.agent_id IN ({q})""", params)
    rows = awards + org
    rows.sort(key=lambda r: (r["at"] or "", r["code"]), reverse=True)
    return rows


@router.get("/perf/hall", response_class=HTMLResponse)
def perf_hall(request: Request, metric: str = "recognized_premium", ctx: dict = Depends(guard), con=Depends(get_con)):
    metric = pick(metric, HALL_METRICS, "recognized_premium")
    prev = ym_add(ctx["ym"], -1)
    closed = bool(con.execute("SELECT 1 FROM close_period WHERE period_ym=? AND status='closed'", (prev,)).fetchone())
    return page(request, ctx, "perf_hall.html", prev=prev, closed=closed, metric=metric, METRICS=HALL_METRICS, fresh=fresh(con, prev),
                rank=hall_ranking(con, ctx, prev, metric), divisions=hall_divisions(con, ctx, prev), history=hall_history(con, ctx),
                prize_note=awards.PUBLIC_NOTE)
