"""실적과 랭킹: 웹 화면이 부르는 함수. 기간별 실적, 구분별 집계, 업권과 조직 범위 랭킹, 조직 비교.

규칙은 업적 집계 하나다. 값은 모두 performance.tally() 에서 온다. 청약은 예정 업적, 성립한 달에 더하고 철회한 달에 뺀다.
저장하는 업적 run(agent_performance_summary), 랭킹 run(ranking_snapshot), 조직 부문 순위와 같은 숫자가 나온다.
값은 계약 원장에서 그때그때 읽는다. 저장된 run 보다 뒤에 계약을 적재했으면 freshness() 의 stale 이 참이다.

조회 범위. 모든 함수는 범위 안 설계사 목록(agent_ids)이나 범위 안 조직 목록(org_ids)을 받아서만 읽는다.
랭킹만 예외로 그 조직 전체를 줄 세운다. 순위는 전체 안의 자리이기 때문이다. 대신 ranking_for() 가 고를 수 있는 조직을
ranking_orgs() 로 막고, 돌려주는 줄은 org_award.view_rows() 와 같이 메인 관리자는 전부, 그 밖에는 상위 10명과 자기 범위만이다.

퇴사자 (D-42). 그 달 중간에 퇴사한 설계사의 퇴사 전 실적은 합계(기간별 실적, 구분별 집계, 조직 비교)에 넣고 줄에 left 를 붙인다.
랭킹에서는 뺀다.

가정. 열린 결정은 지금 동작 그대로 둔다.
- D-34 조직장 개인 실적: 랭킹은 job_type 이 설계사(fc)인 사람만 줄 세운다. 합계와 조직 비교에는 job_type 과 상관없이
  소속 이력이 있는 사람의 계약을 모두 넣는다. 업적 run 과 v_org_performance_rollup 이 그렇게 하기 때문이다.
- D-36 조직 집계 기준: 계약에 적힌 조직 경로가 아니라 사원현황의 소속 이력(agent_org_history)이다. 분기, 반기, 연도
  기간 마지막 달(as_of_ym)의 소속으로 묶는다. 업적 run 이 기간 기준일 소속을 남기는 것과 같다.
- 가동은 그 기간 순건수가 0 보다 큰 것이다. 홈과 팀원 현황이 쓰는 기준과 같다. 공동모집 0.5건(D-32)은 지금 없다.
- D-30 일시납, D-32 공동모집은 바꾸지 않는다. 보험료와 건수는 원장 값 그대로 더한다.
"""
from app.engine import orgs, performance
from app.engine.org_award import view_rows
from app.org import levels
from app.periods import period_end, period_key, period_start, ym_range

PERIOD_KO = {"month": "월", "quarter": "분기", "half": "반기", "year": "연"}
SECTOR_KO = {None: "전체", "": "전체", "life": "생명보험", "nonlife": "손해보험"}
RANK_METRIC_KO = {"converted_premium": "인정보험료", "monthly_premium": "보험료", "net_count": "순건수"}
RANK_METRIC_UNIT = {"converted_premium": "원", "monthly_premium": "원", "net_count": "건"}
PLANNED = ("applied_count", "applied_premium", "applied_converted")                 # 청약: 예정 업적
CONFIRMED = ("new_count", "withdrawn_count", "net_count", "monthly_premium", "converted_premium")   # 성립과 철회: 확정 업적
KEYS = performance.ROW_KEYS
UNCLASSIFIED = "미분류"


# ---------------------------------------------------------------- 기간
def period(period_type: str, as_of_ym: str) -> dict:
    """as_of_ym 이 든 기간. 분기, 반기, 연은 기간 첫 달부터 as_of_ym 까지의 누적이다. 업적 run 과 같은 범위다.
    {period_type, period_key, start_ym, end_ym, months, label, partial}. partial 은 기간이 아직 다 차지 않았다는 뜻이다."""
    if period_type not in PERIOD_KO:
        raise ValueError(period_type)
    start, key, last = period_start(period_type, as_of_ym), period_key(period_type, as_of_ym), period_end(period_type, as_of_ym)
    y = int(as_of_ym[:4])
    label = {"month": f"{y}년 {int(as_of_ym[4:])}월", "quarter": f"{y}년 {key[-1]}분기",
             "half": f"{y}년 {'상' if key[-1] == '1' else '하'}반기", "year": f"{y}년"}[period_type]
    partial = as_of_ym < last
    if partial and period_type != "month":
        label += f" {int(as_of_ym[4:])}월까지"
    return {"period_type": period_type, "period_key": key, "start_ym": start, "end_ym": as_of_ym, "months": ym_range(start, as_of_ym),
            "label": label, "partial": partial, "type_ko": PERIOD_KO[period_type]}


def freshness(con, as_of_ym: str) -> dict:
    """기준 시각. 마지막 계약 적재 시각과 그 달 업적 run 의 완료 시각. stale 이면 저장된 집계(홈, 조직 집계 뷰)가 원장보다 늦다."""
    load = con.execute("SELECT MAX(loaded_at) t FROM import_batch WHERE data_type='org_contract'").fetchone()["t"]
    run = con.execute("""SELECT MAX(finished_at) t FROM calc_run WHERE run_type='performance' AND period_ym=? AND status='done'""",
                      (as_of_ym,)).fetchone()["t"]
    return {"last_load": load, "run_at": run, "stale": bool(load and (not run or load > run))}


def _split(row: dict) -> dict:
    """한 줄에 예정(planned)과 확정(confirmed)을 나눠 붙인다. 원래 칸도 그대로 둔다."""
    row["planned"] = {k: row[k] for k in PLANNED}
    row["confirmed"] = {k: row[k] for k in CONFIRMED}
    return row


def _add(into: dict, a: dict) -> dict:
    for k in KEYS:
        into[k] += a.get(k, 0) or 0
    return into


def _people(con, agent_ids, ym) -> dict:
    """범위 안 설계사 가운데 합계에 넣는 사람. 재직자와, 퇴사했어도 그 달에 소속 이력이 있는 사람이다.
    팀원 현황(app/web/deps.py 의 _perf)과 조직 집계 뷰가 쓰는 기준과 같다. left 는 퇴사자다."""
    ids = sorted(set(agent_ids or ()))
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    rows = con.execute(
        f"""SELECT a.agent_id, a.internal_code code, a.name, a.job_type, a.status, a.termination_date, a.status='terminated' AS "left",
                   h.org_id, o.name org, o.level org_level
            FROM agent a
            LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
            LEFT JOIN org_unit o ON o.org_id=h.org_id
            WHERE a.agent_id IN ({q}) AND (a.status='active' OR h.org_id IS NOT NULL)
            ORDER BY a.agent_id, h.effective_from DESC""", (ym, ym, *ids)).fetchall()
    out = {}
    for r in rows:                                               # 한 달에 이력이 둘이면 org_at() 처럼 나중 것을 쓴다
        out.setdefault(r["agent_id"], {**dict(r), "left": bool(r["left"])})
    return out


# ---------------------------------------------------------------- 기간별 실적과 구분별 집계
def period_summary(con, agent_ids, period_type: str, as_of_ym: str, *, sector=None, categories=None) -> dict:
    """기간별 실적. 범위 안 설계사의 사람별 줄, 합계, 달마다의 합계.

    돌려주는 것: {period, sector, sector_ko, rows, total, months, people, active, left}
    - rows: [{agent_id, code, name, org, org_id, job_type, left, termination_date, 업적 칸, planned, confirmed}] 인정보험료 큰 순
    - 업적 칸: applied_count, applied_premium, applied_converted 는 청약(예정). new_count, withdrawn_count, net_count,
      monthly_premium, converted_premium 은 성립과 철회(확정)
    - total: 합계. 퇴사자의 퇴사 전 실적도 든다 (D-42)
    - months: [{ym, 업적 칸, planned, confirmed}] 기간의 달마다. 달의 합은 total 과 같다
    - active: 그 기간 순건수가 0 보다 큰 사람 수. left: 퇴사자 수
    sector 는 life / nonlife / None(전체), categories 는 구분 코드 목록이다.
    """
    p = period(period_type, as_of_ym)
    people = _people(con, agent_ids, as_of_ym)
    vals = performance.tally(con, p["start_ym"], p["end_ym"], agent_ids=list(people), sector=sector or None, categories=categories)
    rows = [_split({**pe, **(vals.get(a) or performance.blank())}) for a, pe in people.items()]
    rows.sort(key=lambda r: (-(r["converted_premium"] or 0), r["code"]))
    total = performance.blank()
    for r in rows:
        _add(total, r)
    by_month = performance.tally(con, p["start_ym"], p["end_ym"], agent_ids=list(people), sector=sector or None, categories=categories, by="month")
    months = {ym: dict(performance.blank(), ym=ym) for ym in p["months"]}
    for per in by_month.values():
        for ym, a in per.items():
            _add(months[ym], a)
    return {"period": p, "sector": sector or None, "sector_ko": SECTOR_KO[sector or None], "rows": rows, "total": _split(total),
            "months": [_split(m) for m in months.values()], "people": len(rows),
            "active": sum(1 for r in rows if (r["net_count"] or 0) > 0), "left": sum(1 for r in rows if r["left"])}


def breakdown(con, agent_ids, period_type: str, as_of_ym: str, *, by="category", sector=None) -> dict:
    """구분별 집계(by='category') 또는 업권별 집계(by='sector'). 범위 안 설계사의 합계를 칸마다 나눈다.

    돌려주는 것: {period, by, sector, sector_ko, rows, total}
    - rows: [{key, name, 업적 칸, planned, confirmed}]. 구분은 마스터 순서이고 실적이 없는 구분도 0 으로 한 줄 둔다.
      구분이 비어 있는 계약은 key None, name 미분류 줄로 모은다. 업권은 생명보험, 손해보험 순이다
    - total: 줄의 합. period_summary() 의 total 과 같다
    """
    if by not in ("category", "sector"):
        raise ValueError(by)
    p = period(period_type, as_of_ym)
    people = _people(con, agent_ids, as_of_ym)
    vals = performance.tally(con, p["start_ym"], p["end_ym"], agent_ids=list(people), sector=sector or None, by=by)
    if by == "category":
        names = [(r["code"], r["name"]) for r in con.execute("SELECT code, name FROM product_category ORDER BY sort_order, code")]
    else:
        names = [("life", SECTOR_KO["life"]), ("nonlife", SECTOR_KO["nonlife"])]
    cells = {k: dict(performance.blank(), key=k, name=n) for k, n in names}
    for per in vals.values():
        for k, a in per.items():
            if k not in cells:
                cells[k] = dict(performance.blank(), key=k, name=UNCLASSIFIED if k is None else str(k))
            _add(cells[k], a)
    total = performance.blank()
    for c in cells.values():
        _add(total, c)
    return {"period": p, "by": by, "sector": sector or None, "sector_ko": SECTOR_KO[sector or None],
            "rows": [_split(c) for c in cells.values()], "total": _split(total)}


# ---------------------------------------------------------------- 랭킹
def _rank(rows, key):
    """같은 값이면 같은 순위다. 다음 사람은 그만큼 밀린다. ranking.py 와 조직 부문 순위의 방식과 같다."""
    rows.sort(key=lambda r: (-r[key], r["code"]))
    rank, prev = 0, None
    for i, r in enumerate(rows, start=1):
        if r[key] != prev:
            rank, prev = i, r[key]
        r["rank"] = rank
    return rows


def ranking(con, as_of_ym: str, *, agent_ids, period_type="month", metric="converted_premium", sector=None, categories=None,
            include_zero=True) -> list:
    """agent_ids 를 줄 세운 순위. 재직 중인 설계사(job_type fc)만 넣는다. 퇴사자는 뺀다 (D-42). 조직장 개인 실적은 D-34 가정대로다.

    돌려주는 것: [{rank, agent_id, code, name, org_id, org, value}] 순위 순.
    metric 은 converted_premium(인정보험료), monthly_premium(보험료), net_count(순건수).
    sector 는 life(생보) / nonlife(손보) / None(전체). 값은 업적 집계와 같다. 전체, 월과 연의 인정보험료와 순건수 순위는
    랭킹 run(ranking_snapshot) 과 같은 순위가 나온다. include_zero 가 참이면 값이 0 인 사람도 넣는다. 랭킹 run 과 같다.
    """
    if metric not in RANK_METRIC_KO:
        raise ValueError(metric)
    p = period(period_type, as_of_ym)
    ids = sorted(set(agent_ids or ()))
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    pop = con.execute(f"SELECT agent_id FROM agent WHERE agent_id IN ({q}) AND job_type='fc' AND status='active'", ids).fetchall()
    people = _people(con, [r["agent_id"] for r in pop], as_of_ym)
    vals = performance.tally(con, p["start_ym"], p["end_ym"], agent_ids=list(people), sector=sector or None, categories=categories)
    rows = [{"agent_id": a, "code": pe["code"], "name": pe["name"], "org_id": pe["org_id"], "org": pe["org"],
             "value": (vals.get(a) or performance.blank())[metric] or 0} for a, pe in people.items()]
    if not include_zero:
        rows = [r for r in rows if r["value"] > 0]
    return _rank(rows, "value")


def rank_population(con, org_id: int, as_of_ym: str) -> list:
    """그 조직과 아래 전부에 그 달 소속인 재직 설계사. 조직 범위 랭킹(GA, 본부, 지점, 센터, 팀 안에서)의 대상이다."""
    return orgs.members(con, org_id, as_of_ym)


def ranking_orgs(con, ctx) -> list:
    """이 사람이 순위를 볼 수 있는 조직. 메인 관리자는 모든 조직이다. 관리자와 설계사는 자기 소속 조직과 그 위 전부,
    맡은 조직과 그 위 전부, 자기 범위 안의 조직이다. 조직 부문 시상의 보이는 범위(org_award.visible)와 같은 방식이다.
    [{org_id, name, level, level_ko, depth}] 위 계층부터."""
    if ctx["role"] == "main":
        ids = None
    else:
        ids = set(ctx.get("org_ids") or ())
        if ctx.get("home_org"):
            ids |= orgs.ancestors(con, ctx["home_org"]["org_id"])
        for h in ctx.get("heads") or ():
            ids |= orgs.ancestors(con, h["org_id"])
        if not ids:
            return []
    sql = "SELECT org_id, name, level, path_key FROM org_unit WHERE active=1"
    params = []
    if ids is not None:
        sql += f" AND org_id IN ({','.join('?' * len(ids))})"
        params = sorted(ids)
    ko = levels.level_ko()
    rows = [{**dict(r), "level_ko": ko.get(r["level"], r["level"]), "depth": levels.depth(r["level"])} for r in con.execute(sql, params)]
    return sorted(rows, key=lambda r: (r["depth"], r["path_key"] or r["name"], r["org_id"]))


def ranking_for(con, ctx, org_id: int, as_of_ym: str, *, period_type="month", metric="converted_premium", sector=None, categories=None) -> dict:
    """조직 범위 랭킹을 화면에 내는 한 번의 호출. 고를 수 없는 조직이면 PermissionError 다.

    돌려주는 것: {org, period, metric, metric_ko, unit, sector, sector_ko, rows, me, total}
    - rows: 보이는 줄만. 메인 관리자는 전부, 그 밖에는 상위 10명과 자기 범위의 사람이다 (설계사는 상위 10명과 본인)
    - me: 본인 줄. 설계사가 아니거나 대상이 아니면 None. total: 순위에 든 전체 사람 수
    """
    allowed = {o["org_id"]: o for o in ranking_orgs(con, ctx)}
    if org_id not in allowed:
        raise PermissionError("이 조직의 순위를 볼 수 없습니다.")
    rows = ranking(con, as_of_ym, agent_ids=rank_population(con, org_id, as_of_ym), period_type=period_type, metric=metric,
                   sector=sector, categories=categories)
    me = next((r for r in rows if r["agent_id"] == ctx.get("agent_id")), None) if ctx.get("agent_id") else None
    return {"org": allowed[org_id], "period": period(period_type, as_of_ym), "metric": metric, "metric_ko": RANK_METRIC_KO[metric],
            "unit": RANK_METRIC_UNIT[metric], "sector": sector or None, "sector_ko": SECTOR_KO[sector or None],
            "rows": view_rows(rows, ctx), "me": me, "total": len(rows)}


# ---------------------------------------------------------------- 조직 비교
def org_path(con, org_id: int) -> dict:
    """{계층 코드: 조직 이름}. 그 조직과 위 전부다. 기존 운영 도구의 본부 이름처럼 위 조직 이름을 찾을 때 쓴다."""
    ids = orgs.ancestors(con, org_id)
    q = ",".join("?" * len(ids))
    return {r["level"]: r["name"] for r in con.execute(f"SELECT level, name FROM org_unit WHERE org_id IN ({q})", sorted(ids))}


def org_compare(con, as_of_ym: str, *, org_ids, level="branch", parent_id=None, period_type="month", sector=None, categories=None) -> dict:
    """같은 단계 조직끼리 견준다. org_ids 는 범위 안 조직 목록이고 그 밖의 조직은 내지 않는다.

    level 은 org_unit.level(hq, branch, center, team). parent_id 를 주면 그 조직의 아래만 본다.
    값은 조직과 그 아래 전부에 as_of_ym 에 소속 이력이 있는 사람의 합이다. 퇴사자의 퇴사 전 실적도 든다 (D-42).
    월 인정보험료와 순건수는 v_org_performance_rollup, performance.org_rollup() 과 같다 (D-36 가정).

    돌려주는 것: {period, level, level_ko, sector, sector_ko, rows, total}
    - rows: [{rank, org_id, name, level, path(계층 코드: 이름), headcount, fc_count, left, active, active_rate,
              업적 칸, planned, confirmed}] 인정보험료 큰 순. active_rate 는 가동 인원 / 인원이고 인원이 0 이면 None 이다
    - total: 줄의 합. 같은 단계 조직은 겹치지 않는다
    """
    p = period(period_type, as_of_ym)
    scope = set(org_ids or ())
    if not scope:
        return {"period": p, "level": level, "level_ko": levels.level_ko().get(level, level), "sector": sector or None,
                "sector_ko": SECTOR_KO[sector or None], "rows": [], "total": _split(performance.blank())}
    if parent_id is not None:
        scope &= orgs.descendants(con, parent_id)
    q = ",".join("?" * len(scope))
    units = [dict(r) for r in con.execute(f"SELECT org_id, name, level, path_key FROM org_unit WHERE level=? AND org_id IN ({q}) ORDER BY org_id",
                                          [level, *sorted(scope)])]
    under = {u["org_id"]: orgs.descendants(con, u["org_id"]) for u in units}
    all_orgs = set().union(*under.values()) if under else set()
    hist = []
    if all_orgs:
        q = ",".join("?" * len(all_orgs))
        hist = con.execute(f"""SELECT DISTINCT h.agent_id FROM agent_org_history h
                               WHERE h.org_id IN ({q}) AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)""",
                           [*sorted(all_orgs), as_of_ym, as_of_ym]).fetchall()
    people = {a: pe for a, pe in _people(con, [r["agent_id"] for r in hist], as_of_ym).items() if pe["org_id"] in all_orgs}
    vals = performance.tally(con, p["start_ym"], p["end_ym"], agent_ids=list(people), sector=sector or None, categories=categories)
    rows = []
    for u in units:
        mine = [a for a, pe in people.items() if pe["org_id"] in under[u["org_id"]]]
        row = dict(performance.blank(), org_id=u["org_id"], name=u["name"], level=u["level"], path_key=u["path_key"],
                   path=org_path(con, u["org_id"]), headcount=len(mine),
                   fc_count=sum(1 for a in mine if people[a]["job_type"] == "fc"), left=sum(1 for a in mine if people[a]["left"]))
        for a in mine:
            _add(row, vals.get(a) or {})
        row["active"] = sum(1 for a in mine if ((vals.get(a) or {}).get("net_count") or 0) > 0)
        row["active_rate"] = row["active"] / row["headcount"] if row["headcount"] else None
        rows.append(_split(row))
    rows.sort(key=lambda r: (-(r["converted_premium"] or 0), r["path_key"] or r["name"]))
    rank, prev = 0, None
    for i, r in enumerate(rows, start=1):
        if r["converted_premium"] != prev:
            rank, prev = i, r["converted_premium"]
        r["rank"] = rank
    total = performance.blank()
    for r in rows:
        _add(total, r)
    return {"period": p, "level": level, "level_ko": levels.level_ko().get(level, level), "sector": sector or None,
            "sector_ko": SECTOR_KO[sector or None], "rows": rows, "total": _split(total)}
