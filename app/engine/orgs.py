"""조직 범위. 엔진이 조직과 그 아래 전부를 따질 때 쓰는 한 곳.

조직은 여섯 단계다(config/org_levels.yaml). 본부와 지점의 집계는 바로 아래만이 아니라 센터와 팀까지 전부 내려간다.
계층의 수나 이름을 여기서 따지지 않는다. parent_id 를 끝까지 따라갈 뿐이다.
"""


def descendants(con, org_id) -> set:
    """그 조직과 아래 전부의 org_id."""
    out, frontier = {org_id}, {org_id}
    while frontier:
        q = ",".join("?" * len(frontier))
        frontier = {r["org_id"] for r in con.execute(f"SELECT org_id FROM org_unit WHERE parent_id IN ({q})", tuple(frontier))} - out
        out |= frontier
    return out


def ancestors(con, org_id) -> set:
    """그 조직과 위 전부의 org_id."""
    out = set()
    while org_id is not None and org_id not in out:
        out.add(org_id)
        r = con.execute("SELECT parent_id FROM org_unit WHERE org_id=?", (org_id,)).fetchone()
        org_id = r["parent_id"] if r else None
    return out


def members(con, org_id, ym, fc_only=True, active_only=True) -> list:
    """그 달에 그 조직과 아래 전부에 소속인 설계사의 agent_id. 소속은 agent_org_history 를 따른다."""
    ids = sorted(descendants(con, org_id))
    q = ",".join("?" * len(ids))
    cond = (" AND a.job_type='fc'" if fc_only else "") + (" AND a.status='active'" if active_only else "")
    return [r["agent_id"] for r in con.execute(
        f"""SELECT DISTINCT h.agent_id FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id
            WHERE h.org_id IN ({q}) AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?){cond} ORDER BY h.agent_id""",
        ids + [ym, ym])]
