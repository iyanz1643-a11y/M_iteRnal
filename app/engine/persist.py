"""유지율 조회: 원수사별 행을 합산해 설계사·조직·업권 단위 유지율을 구한다."""


def rate(con, agent_ids, kind, ym, insurer_id=None, sector=None):
    """ym 이하 가장 최근 월의 유지율. (rate, 유지, 대상) 을 돌려준다. 데이터가 없으면 (0.0, 0, 0)."""
    ids = list(agent_ids)
    if not ids:
        return 0.0, 0, 0
    marks = ",".join("?" * len(ids))
    latest = con.execute(f"SELECT MAX(period_ym) m FROM persistency WHERE period_ym<=? AND kind=? AND agent_id IN ({marks})", [ym, kind] + ids).fetchone()["m"]
    if not latest:
        return 0.0, 0, 0
    sql = f"""SELECT COALESCE(SUM(p.numerator),0) n, COALESCE(SUM(p.denominator),0) d FROM persistency p JOIN insurer i ON i.insurer_id=p.insurer_id
              WHERE p.period_ym=? AND p.kind=? AND p.agent_id IN ({marks})"""
    params = [latest, kind] + ids
    if insurer_id:
        sql += " AND p.insurer_id=?"
        params.append(insurer_id)
    if sector and sector != "all":
        sql += " AND i.sector=?"
        params.append(sector)
    r = con.execute(sql, params).fetchone()
    return (round(r["n"] / r["d"], 4) if r["d"] else 0.0), r["n"], r["d"]
