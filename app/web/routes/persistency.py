"""유지율 화면 두 개. 실적 묶음의 유지율 /perf/persistency 와 내 진척의 내 유지율 /my/persistency 다.

계산은 app/engine/persist.py 가 한다. 이 파일은 범위 안 설계사 목록을 넘기고 엔진이 준 값을 화면 꼴로 묶을 뿐이다.

- 유지율: 메인 관리자와 관리자. 범위는 guard 가 준 메뉴 범위(all, org)다. 조직을 고르면 ps.org_members 로 그 조직과 아래에
  화면에서 고른 달에 소속 이력이 있는 재직 설계사 가운데 범위 안 사람만 읽는다. 기존 운영 도구와 인증 판정과 같은 목록이다.
  그 달 중간 퇴사자는 빠진다. 실적 합계에 퇴사 전 실적을 넣는 D-42 와 다르다. 엔진이 정한 가정을 따른다.
  조직을 고르지 않으면 범위 전체다. 조직이 범위 밖인지는 perf.pick_org 가 본다.
  persist.rates 로 업권별과 보험사별, persist.by_agent 로 사람별, persist.risk 로 위험 계약을 낸다.
- 내 유지율: 관리자와 설계사의 본인 화면. persist.overview 를 본인 한 사람으로 부른다. 석 달 안 13회차 도래를 함께 낸다.
- 유지율이 None 이면 대상 건수가 0 이라는 뜻이다. 화면은 0% 가 아니라 '자료 없음' 으로 적는다.
- 위험 계약 목록에는 증권번호 끝 네 자리와 단계만 둔다 (D-05). 고객 이름과 증권번호 전체는 엔진도 주지 않고 화면도 싣지 않는다.
- 금액은 싣지 않는다 (D-17). 보험료도 수수료도 없다. 건수와 비율만이다.
- 기준 시각은 둘이다. 유지율은 유지율 현황 파일을 실은 시각, 위험 계약과 13회차 도래는 계약 적재 시각이다.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.engine import persist as ps
from app.web.deps import get_con, guard, page
from app.web.routes.perf import org_choices, pick_org, pick_ym, scope_ids

router = APIRouter()
RISK_SHOW = 60              # 위험 계약 목록에 한 번에 싣는 줄 수. 넘으면 건수만 더 적는다
LEVELS = ("lapse_risk", "unpaid", "lapsed")


def stamps(con) -> dict:
    """기준 시각 두 가지. 유지율 현황 적재 시각과 계약 적재 시각."""
    one = lambda t: con.execute("SELECT MAX(loaded_at) t FROM import_batch WHERE data_type=?", (t,)).fetchone()["t"]
    return {"persistency": one("persistency"), "contract": one("org_contract")}


def side_by_side(rates: dict, part: str, key: str, label: str) -> list:
    """rates() 의 13회차와 25회차 줄을 한 표에 나란히 놓는다. 한쪽에만 있는 줄은 다른 쪽이 None 이다. 값은 엔진 것 그대로다."""
    rows: dict = {}
    for k in rates["kinds"]:
        for r in k[part]:
            row = rows.setdefault(r[key], {"key": r[key], "label": r[label], "sector": r.get("sector"), "p13": None, "p25": None})
            row[k["kind"]] = r
    return list(rows.values())


def kinds(rates: dict) -> dict:
    return {k["kind"]: k for k in rates["kinds"]}


def risk_view(risk: dict) -> dict:
    """위험 계약을 화면에 싣는 꼴. 목록 줄에는 증권번호 끝 네 자리와 단계만 남긴다 (D-05)."""
    items = [{"policy_tail": x["policy_tail"], "level": x["level"], "level_ko": x["level_ko"]} for x in risk["items"]]
    return {"counts": risk["counts"], "total": sum(risk["counts"][lv] for lv in LEVELS), "rows": items[:RISK_SHOW],
            "more": max(len(items) - RISK_SHOW, 0), "note": risk["note"]}


@router.get("/perf/persistency", response_class=HTMLResponse)
def perf_persistency(request: Request, ym: str | None = None, org: int | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    ym, yms = pick_ym(con, ctx, ym)
    scope = scope_ids(con, ctx)
    sel, ids = pick_org(con, ctx, org, scope)                     # 범위 밖 조직이면 403
    if sel is not None:                                           # 조직 유지율은 엔진의 재직자 목록. 고른 달 기준이다
        ids = ps.org_members(con, sel["org_id"], ym, agent_ids=scope)
    r = ps.rates(con, ids, ym)
    people = ps.by_agent(con, ids, ym)
    return page(request, ctx, "perf_persistency.html", rates=r, k=kinds(r), sectors=side_by_side(r, "by_sector", "sector", "sector_ko"),
                insurers=side_by_side(r, "by_insurer", "insurer_id", "insurer"), people=people,
                with_data=sum(1 for p in people if p["p13"]["rate"] is not None or p["p25"]["rate"] is not None),
                risk=risk_view(ps.risk(con, ids, ym)), sel=sel, orgs=org_choices(con, ctx, sel), yms=yms, ym=ym, stamps=stamps(con),
                f={"ym": ym, "org": org or ""})


@router.get("/my/persistency", response_class=HTMLResponse)
def my_persistency(request: Request, ym: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    ym, yms = pick_ym(con, ctx, ym)
    me = [ctx["agent_id"]] if ctx.get("agent_id") else []
    o = ps.overview(con, me, ym)
    return page(request, ctx, "my_persistency.html", o=o, k=kinds(o["rates"]), sectors=side_by_side(o["rates"], "by_sector", "sector", "sector_ko"),
                insurers=side_by_side(o["rates"], "by_insurer", "insurer_id", "insurer"), risk=risk_view(o["risk"]), due=o["due"],
                has_agent=bool(me), yms=yms, ym=ym, stamps=stamps(con), f={"ym": ym})
