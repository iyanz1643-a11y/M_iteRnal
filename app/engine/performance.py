"""업적 집계: 월/분기/반기/연 설계사별 실적.

규칙: 청약은 예정 업적, 성립한 달에 더하고 철회한 달에 뺀다. 다른 엔진(랭킹, 조직 부문 순위)도 이 규칙을 따른다.
설계사가 비어 있는 계약(모집사원번호가 마스터에 없는 실제 계약)은 사람에게 붙일 수 없어 설계사별 집계에서 빠진다.
그 몫은 unassigned() 로 따로 본다. 설계사별 합과 unassigned() 를 더하면 원장 전체와 맞는다.
조직 집계는 org_rollup() 이다. 조직과 그 아래 전부(센터, 팀 포함)의 설계사를 더한다.
"""
import json
from collections import defaultdict

from app import db
from app.engine import classify, orgs
from app.periods import month_bounds, period_key, period_start

ACTIVE_STATUSES = ("inforce", "lapsed", "cancelled", "matured", "withdrawn")  # 한 번 성립한 계약


def org_at(con, agent_id, ym):
    row = con.execute(
        """SELECT org_id FROM agent_org_history WHERE agent_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)
           ORDER BY effective_from DESC LIMIT 1""",
        (agent_id, ym, ym),
    ).fetchone()
    return row["org_id"] if row else None


ROW_KEYS = ("applied_count", "applied_premium", "applied_converted",          # 청약: 예정 업적
            "new_count", "withdrawn_count", "net_count", "monthly_premium", "converted_premium")   # 성립과 철회: 확정 업적
BY_KEYS = ("category", "sector", "month")


def blank() -> dict:
    return dict.fromkeys(ROW_KEYS, 0)


def tally(con, start_ym, end_ym, *, agent_ids=None, sector=None, categories=None, by=None, unassigned_only=False) -> dict:
    """업적 집계 규칙의 한 곳. start_ym~end_ym 의 설계사별 집계.

    청약은 아직 applied 인 건을 청약일 달에 예정 업적으로 센다. 성립은 성립일 달에 더하고, 철회는 철회일 달에 뺀다.
    성립 뒤 철회된 건도 성립한 달에는 들어간다. 저장하는 업적 run(compute_range)과 웹 화면의 함수가 모두 이 함수를 쓴다.

    agent_ids 를 주면 그 사람들만 읽는다. sector(life, nonlife) 와 categories(구분 코드 목록)는 계약의 업권과 구분으로 좁힌다.
    by 가 None 이면 {agent_id: 줄}, 'category' / 'sector' / 'month' 면 {agent_id: {구분 코드 / 업권 / YYYYMM: 줄}} 이다.
    month 는 청약은 청약일, 성립은 성립일, 철회는 철회일의 달이다. 실적이 없는 사람은 키가 없다.
    """
    if by not in (None,) + BY_KEYS:
        raise ValueError(by)
    if agent_ids is not None:
        agent_ids = sorted(set(agent_ids))
        if not agent_ids:
            return {}
    d_from, _ = month_bounds(start_ym)
    _, d_to = month_bounds(end_ym)
    cond = ["c.agent_id IS NULL" if unassigned_only else "c.agent_id IS NOT NULL"]
    params = []
    if agent_ids is not None:
        cond.append(f"c.agent_id IN ({','.join('?' * len(agent_ids))})")
        params += agent_ids
    if sector:
        cond.append(f"{classify.SECTOR}=?")
        params.append(sector)
    if categories:
        categories = sorted(set(categories))
        cond.append(f"{classify.CATEGORY} IN ({','.join('?' * len(categories))})")
        params += categories
    where = " AND ".join(cond)
    out = {}

    def row(agent_id, key):
        a = out.setdefault(agent_id, {} if by else blank())
        return a.setdefault(key, blank()) if by else a

    def key_expr(date_col):
        if by == "month":
            return f"substr(c.{date_col},1,4)||substr(c.{date_col},6,2)"
        return {"category": classify.CATEGORY, "sector": classify.SECTOR, None: "NULL"}[by]

    def each(status_sql, date_col, extra=()):
        sql = f"""SELECT c.agent_id, {key_expr(date_col)} k, SUM(c.policy_count) n, SUM(c.premium_monthly) mp, SUM(c.converted_premium) cp
                  FROM contract c {classify.JOINS}
                  WHERE {where} AND {status_sql} AND c.{date_col}>=? AND c.{date_col}<? GROUP BY c.agent_id, k"""
        return con.execute(sql, params + list(extra) + [d_from, d_to]).fetchall()

    for r in each("c.status='applied'", "application_date"):                          # 청약 (예정 업적)
        a = row(r["agent_id"], r["k"])
        a["applied_count"] += r["n"] or 0
        a["applied_premium"] += r["mp"] or 0
        a["applied_converted"] += r["cp"] or 0
    for r in each(f"c.status IN ({','.join('?' * len(ACTIVE_STATUSES))})", "contract_date", ACTIVE_STATUSES):   # 성립
        a = row(r["agent_id"], r["k"])
        a["new_count"] += r["n"] or 0
        a["monthly_premium"] += r["mp"] or 0
        a["converted_premium"] += r["cp"] or 0
    for r in each("c.status='withdrawn'", "withdrawal_date"):                         # 철회: 철회 월에 차감
        a = row(r["agent_id"], r["k"])
        a["withdrawn_count"] += r["n"] or 0
        a["monthly_premium"] -= r["mp"] or 0
        a["converted_premium"] -= r["cp"] or 0
    for a in out.values():
        for x in (a.values() if by else [a]):
            x["net_count"] = x["new_count"] - x["withdrawn_count"]
    return out


def compute_range(con, start_ym, end_ym, unassigned_only=False):
    """start_ym~end_ym 사이 설계사별 집계 (딕셔너리). unassigned_only 면 설계사가 빈 계약만 None 키 하나로 모은다.
    값은 tally() 와 같고, 완료된 수수료 run 의 수수료와 환수를 곁들인다."""
    agg = defaultdict(lambda: dict(blank(), commission_amount=0, clawback_amount=0))
    for agent_id, a in tally(con, start_ym, end_ym, unassigned_only=unassigned_only).items():
        agg[agent_id].update(a)
    # 수수료 (완료 run 기준)
    for r in [] if unassigned_only else con.execute(
        """SELECT c.agent_id,
                  SUM(CASE WHEN c.commission_type<>'clawback' THEN c.amount ELSE 0 END) paid,
                  SUM(CASE WHEN c.commission_type='clawback' THEN c.amount ELSE 0 END) cb
           FROM commission_calc c JOIN calc_run r ON r.run_id=c.run_id AND r.status='done'
           WHERE c.agent_id IS NOT NULL AND c.period_ym>=? AND c.period_ym<=? GROUP BY c.agent_id""",
        (start_ym, end_ym),
    ):
        agg[r["agent_id"]]["commission_amount"] += r["paid"] or 0
        agg[r["agent_id"]]["clawback_amount"] += r["cb"] or 0
    for a in agg.values():
        a["net_count"] = a["new_count"] - a["withdrawn_count"]
    return agg


def unassigned(con, start_ym, end_ym) -> dict:
    """설계사가 빈 계약의 집계. 설계사별 집계와 같은 규칙이다. 원장 대조에 쓴다."""
    agg = compute_range(con, start_ym, end_ym, unassigned_only=True)
    return agg.get(None) or blank()


SUM_COLS = ("applied_count", "new_count", "withdrawn_count", "net_count", "monthly_premium", "converted_premium", "commission_amount", "clawback_amount")


def org_rollup(con, org_id, period_ym, period_type="month") -> dict:
    """조직과 그 아래 전부의 집계. period_ym 의 완료된 업적 run 을 읽는다.
    소속은 그 run 이 남긴 기간 기준일 소속(agent_performance_summary.org_id)이다. 뷰 v_org_performance_rollup 과 같은 값이다."""
    ids = sorted(orgs.descendants(con, org_id))
    q = ",".join("?" * len(ids))
    r = con.execute(
        f"""SELECT COUNT(*) agents, {', '.join(f'COALESCE(SUM(s.{c}),0) {c}' for c in SUM_COLS)}
            FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.run_type='performance' AND r.period_ym=?
            WHERE s.period_type=? AND s.period_key=? AND s.org_id IN ({q})""",
        [period_ym, period_type, period_key(period_type, period_ym)] + ids).fetchone()
    return dict(r)


def run(con, period_ym, run_by="system"):
    """period_ym 기준 월·분기(누적)·반기(누적)·연(누적) 집계를 한 run으로 저장."""
    run_id = db.new_run(con, "performance", period_ym, run_by, json.dumps({"as_of": period_ym}))
    n = 0
    for ptype in ("month", "quarter", "half", "year"):
        start = period_start(ptype, period_ym)
        key = period_key(ptype, period_ym)
        agg = compute_range(con, start, period_ym)
        for fc in con.execute("SELECT agent_id FROM agent WHERE job_type='fc'").fetchall():
            agg[fc["agent_id"]]  # 실적이 없는 설계사도 0 으로 한 줄 남긴다
        for agent_id, a in agg.items():
            a.setdefault("net_count", a["new_count"] - a["withdrawn_count"])
            con.execute(
                """INSERT INTO agent_performance_summary(run_id, agent_id, org_id, period_type, period_key, applied_count, new_count,
                   withdrawn_count, net_count, monthly_premium, converted_premium, commission_amount, clawback_amount)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, agent_id, org_at(con, agent_id, period_ym), ptype, key, a["applied_count"], a["new_count"],
                 a["withdrawn_count"], a["net_count"], a["monthly_premium"], a["converted_premium"],
                 a["commission_amount"], a["clawback_amount"]),
            )
            n += 1
    db.finish_run(con, run_id, f"{n} rows")
    return run_id, n
