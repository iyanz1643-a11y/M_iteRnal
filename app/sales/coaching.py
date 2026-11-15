"""면담 기록. 관리자가 범위 안 팀원과의 면담 날짜, 약속 사항, 다음 면담 날짜를 남긴다. 커밋은 부르는 쪽이 한다.

| 무엇 | 규칙 |
| --- | --- |
| 남기는 사람 | 관리자. 범위 안 팀원만이고 본인은 뺀다 |
| 보는 사람 | 그 팀원이 범위 안인 관리자. 위 조직의 관리자도 본다. 설계사 본인과 메인 관리자의 메뉴에는 없다 |
| 다음 면담 | 가장 최근 면담의 next_on. 지났으면 지남, DUE_DAYS 일 안이면 임박 |
| 지우기 | 하지 않는다. 고칠 일이 있으면 새 기록을 남긴다 |

보는 사람의 범위는 면담 기록 메뉴의 범위와 같다. 사람에 관한 기록이라 docs/09 5.3 의 검토 사람에 닿는다. 가정이다.
"""
from __future__ import annotations

from datetime import date

from app import db
from app.sales import core

DUE_DAYS = 3
PROMISE_MAX = 300


def _in(ids):
    ids = list(ids) or [-1]
    return ",".join("?" * len(ids)), ids


def add(con, manager_agent_id, agent_id, met_on, promises, next_on, by, day: date | None = None) -> int:
    d = core.clean_date(met_on, required=True)
    today = (day or core.today()).isoformat()
    if d > today:
        raise ValueError("면담 날짜는 오늘이나 지난 날이어야 합니다. 다음 면담은 다음 면담 칸에 적어 주세요.")
    nx = core.clean_date(next_on)
    if nx and nx <= d:
        raise ValueError("다음 면담은 이번 면담보다 뒤여야 합니다.")
    if agent_id == manager_agent_id:
        raise ValueError("본인과의 면담은 남기지 않습니다.")
    p = (str(promises or "").replace("\r\n", "\n").strip())[:PROMISE_MAX] or None
    cur = con.execute("""INSERT INTO team_coaching(agent_id, manager_agent_id, met_on, promises, next_on, created_by, created_at)
                         VALUES(?,?,?,?,?,?,?)""", (agent_id, manager_agent_id, d, p, nx, by, db.now()))
    core.audit(con, by, "team_coaching_add", f"agent:{agent_id}", {"coaching_id": cur.lastrowid})
    return cur.lastrowid


def latest(con, agent_ids) -> dict:
    """{agent_id: 가장 최근 면담}."""
    q, ids = _in(agent_ids)
    out = {}
    for r in con.execute(f"""SELECT t.*, m.name manager_name FROM team_coaching t LEFT JOIN agent m ON m.agent_id=t.manager_agent_id
                             WHERE t.agent_id IN ({q}) ORDER BY t.met_on, t.coaching_id""", ids):
        out[r["agent_id"]] = dict(r)                    # 뒤의 것이 덮는다
    return out


def history(con, agent_id, limit=20) -> list:
    return [dict(r) for r in con.execute(
        """SELECT t.*, m.name manager_name FROM team_coaching t LEFT JOIN agent m ON m.agent_id=t.manager_agent_id
           WHERE t.agent_id=? ORDER BY t.met_on DESC, t.coaching_id DESC LIMIT ?""", (agent_id, limit))]


def state(next_on: str | None, day: date) -> tuple:
    """(상태, 남은 날). over 지남, soon 임박, later 나중, none 없음."""
    if not next_on:
        return "none", None
    left = (date.fromisoformat(next_on) - day).days
    if left < 0:
        return "over", left
    if left <= DUE_DAYS:
        return "soon", left
    return "later", left


def due(con, agent_ids, day: date) -> list:
    """다음 면담이 지났거나 DUE_DAYS 일 안인 팀원. 알림함이 쓴다."""
    out = []
    for aid, r in latest(con, agent_ids).items():
        st, left = state(r["next_on"], day)
        if st in ("over", "soon"):
            out.append({"agent_id": aid, "next_on": r["next_on"], "state": st, "left": left})
    out.sort(key=lambda r: (r["next_on"], r["agent_id"]))
    return out
