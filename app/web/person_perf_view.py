"""인별실적 파일(사람별 기간 실적 집계)을 화면에 싣는 도우미. 실적 요약 /my/summary 와 팀원 현황 /team/members 가 쓴다.

- 읽기는 적재기의 읽기 도우미(app/loader/person_perf.py 의 periods, period_rows, no_perf)만 부른다. 셈은 하지 않고 모양만 만든다.
- 범위는 guard 가 준 ctx 에서 정한 설계사 목록(ids)으로만 읽는다. 범위 밖 사람과 설계사 마스터에 없는 사번은 나오지 않는다.
- 계약 원장의 업적 집계와 원천이 다르다. 두 숫자를 섞어 더하지 않고 화면에 원천과 기간, 파일 적재 시각을 함께 적는다.
- 보이는 기간은 가장 최근에 들어온 기간 하나다. 같은 기간을 다시 넣으면 적재기가 이전 배치를 대체한다.
- 무실적 명단은 금액 없이 사람과 건수만 싣는다. '건수 0' 은 파일에 있고 Total 건수가 0 인 사람,
  '파일에 없음' 은 범위 안 재직 설계사 가운데 그 기간 파일에 줄이 없는 사람이다. 퇴사자는 파일에 있을 때만 센다(가정).
- 금액은 월납보험료 실적이다. 수수료나 시상 금액이 아니어서 관리자에게도 팀원 현황의 인정보험료처럼 보인다(D-17 판단).
- 표가 없는 오래된 DB 에서는 GET 이 DB 에 쓰지 않도록 표를 만들지 않고 자료 없음으로 둔다.
"""
from app.loader import person_perf as pp
from app.periods import ym_diff

DATA_TYPE = pp.DATA_TYPE
SOURCE_KO = "인별실적 파일"
# 유실적 구분. 적재기가 파일의 묶음 값으로 정한다. 순서는 높은 구분부터다
TIERS = ("elite_50", "active_10", "some", "none")
TIER_KO = {"elite_50": "정예", "active_10": "10만 이상", "some": "일부", "none": "무실적"}
TIER_SUB = {"elite_50": "정예(50만이상) 건수가 있는 사람", "active_10": "유실적(10만이상) 건수가 있는 사람",
            "some": "Total 건수가 있는 그 밖의 사람", "none": "Total 건수가 0 인 사람"}
TIER_CHIP = {"elite_50": "good", "active_10": "blue", "some": "", "none": "warn"}


def ready(con) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_period_perf'").fetchone() is not None


def period_label(start, end) -> str:
    """'2026년 3월 ~ 5월'. 해가 다르면 '2025년 12월 ~ 2026년 2월'."""
    a = f"{start[:4]}년 {int(start[4:])}월"
    if start == end:
        return a
    b = f"{int(end[4:])}월" if start[:4] == end[:4] else f"{end[:4]}년 {int(end[4:])}월"
    return f"{a} ~ {b}"


def latest(con) -> dict | None:
    """가장 최근 기간과 그 기간 파일의 적재 정보. 없으면 None. 파일 이름은 싣지 않는다."""
    if not ready(con):
        return None
    ps = pp.periods(con)
    if not ps:
        return None
    start, end = ps[0]
    b = con.execute("""SELECT b.batch_id, b.loaded_at FROM import_batch b WHERE b.data_type=? AND b.status='active'
                        AND b.batch_id IN (SELECT DISTINCT batch_id FROM agent_period_perf WHERE start_ym=? AND end_ym=?)
                        ORDER BY b.batch_id DESC LIMIT 1""", (DATA_TYPE, start, end)).fetchone()
    return {"start": start, "end": end, "label": period_label(start, end), "months": ym_diff(end, start) + 1,
            "batch_id": b["batch_id"] if b else None, "loaded_at": b["loaded_at"] if b else None}


def _people(con, ids, ym) -> dict:
    """{agent_id: {name, code, active, org}}. 소속은 앱 조직 트리의 그 달 소속이다."""
    ids = sorted(set(ids or ()))
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r["agent_id"]: dict(r) for r in con.execute(
        f"""SELECT a.agent_id, a.name, a.internal_code code, a.status='active' AS active, o.name org FROM agent a
            LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
            LEFT JOIN org_unit o ON o.org_id=h.org_id WHERE a.agent_id IN ({q}) ORDER BY a.internal_code""", (ym, ym, *ids))}


def team(con, ids, ym) -> dict | None:
    """팀원 현황의 묶음. ids 는 guard 가 준 범위다. 자료가 없으면 None.

    돌려주는 것: {period, rows: [사람 줄], dist: {구분: 명}, none: [사람], absent: [사람], in_file, in_scope}."""
    period = latest(con)
    if period is None:
        return None
    people = _people(con, ids, ym)
    rows = pp.period_rows(con, period["start"], period["end"], list(people))
    scope = [a for a, p in people.items() if p["active"] or a in rows]       # 재직자와, 퇴사했어도 파일에 있는 사람
    np = pp.no_perf(con, period["start"], period["end"], scope)
    out_rows = []
    for aid, r in rows.items():
        p = people[aid]
        out_rows.append({"agent_id": aid, "name": p["name"], "code": p["code"], "org": p["org"], "left": not p["active"],
                         "tier": r["perf_tier"], "count": r["total_count"], "premium": r["total_monthly_premium_amt"]})
    order = {t: i for i, t in enumerate(TIERS)}
    out_rows.sort(key=lambda x: (order.get(x["tier"], 9), -(x["premium"] or 0), x["code"]))
    dist = {t: 0 for t in TIERS}
    for x in out_rows:
        dist[x["tier"]] = dist.get(x["tier"], 0) + 1

    def lst(agent_ids, count):
        return [{"name": people[a]["name"], "code": people[a]["code"], "org": people[a]["org"], "count": count} for a in agent_ids]
    return {"period": period, "rows": out_rows, "dist": dist, "none": lst(np["none"], 0), "absent": lst(np["absent"], None),
            "in_file": len(out_rows), "in_scope": len(scope)}


def unmapped(con, period) -> int:
    """그 기간 파일에서 설계사 마스터에 없는 사번 수. 메인 관리자에게만 건수로 보인다."""
    if not period:
        return 0
    return con.execute("SELECT COUNT(*) FROM v_agent_period_perf WHERE start_ym=? AND end_ym=? AND agent_id IS NULL",
                       (period["start"], period["end"])).fetchone()[0]


def mine(con, agent_id) -> dict | None:
    """실적 요약의 본인 묶음. 본인 한 사람으로만 읽는다. 자료가 없으면 None, 파일에 본인이 없으면 row 가 None."""
    period = latest(con)
    if period is None or not agent_id:
        return None
    r = pp.period_rows(con, period["start"], period["end"], [agent_id]).get(agent_id)
    if r is None:
        return {"period": period, "row": None}
    ins = [{"name": k, "count": v[0], "premium": v[1]} for k, v in r["insurers"].items() if v[0] or v[1]]
    ins.sort(key=lambda x: (-(x["count"] or 0), -(x["premium"] or 0), x["name"]))
    row = {"tier": r["perf_tier"], "count": r["total_count"], "premium": r["total_monthly_premium_amt"],
           "per_policy": r["total_premium_per_policy_amt"], "single": r["single_count"], "auto": r["auto_count"],
           "general": r["general_count"], "insurers": ins}
    return {"period": period, "row": row}
