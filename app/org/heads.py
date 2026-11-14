"""조직장 지정과 기간제 대리 지정. 메인 관리자 화면(/org/heads)이 부른다.

- 조직장은 월 단위다. org_manager 에 시작 달과 끝 달로 남고, 끝 달이 지나면 따로 손대지 않아도 설계사로 돌아간다.
- 대리는 날짜 단위다. org_deputy 에 시작일과 끝일로 남고 오늘 날짜로 따진다. 대리는 그 조직장과 같은 범위를 갖는다.
- 맨 위 뿌리(GA)에는 조직장이 없다. 어떤 계층에 어떤 조직장 역할이 있는지는 config/org_levels.yaml 을 따른다.
여기 함수는 커밋하지 않는다. 화면이 변경 기록을 남기면서 함께 커밋한다.
지정하거나 끝낸 뒤에는 card_grant.settle_agent 로 명함 승인 권한을 맞춘다. 새로 맡으면 승인 요청이 생기고, 빠지면 켜진 권한이 꺼진다.
"""
import re
from datetime import date, timedelta

from app import db
from app.org import card_grant, levels
from app.periods import ym_add

YM = re.compile(r"^20\d\d(0[1-9]|1[0-2])$")
MAX_DEPUTY_DAYS = 93


class HeadError(ValueError):
    pass


def _org(con, org_id):
    o = con.execute("SELECT * FROM org_unit WHERE org_id=? AND active=1", (org_id,)).fetchone()
    if not o:
        raise HeadError("없는 조직입니다.")
    role = levels.head_role(o["level"])
    if not role:
        raise HeadError("이 계층에는 조직장이 없습니다.")
    return o, role


def _agent(con, agent_id):
    a = con.execute("SELECT * FROM agent WHERE agent_id=? AND status='active'", (agent_id,)).fetchone()
    if not a:
        raise HeadError("재직 중인 설계사가 아닙니다.")
    return a


def assign_head(con, org_id, agent_id, from_ym, to_ym=None, by="system") -> int:
    o, role = _org(con, org_id)
    _agent(con, agent_id)
    if not YM.match(from_ym or "") or (to_ym and (not YM.match(to_ym) or to_ym < from_ym)):
        raise HeadError("기간을 YYYYMM 으로 바르게 넣으세요.")
    clash = con.execute("""SELECT 1 FROM org_manager WHERE org_id=? AND agent_id=? AND role=? AND effective_from<=COALESCE(?, '999999')
                           AND (effective_to IS NULL OR effective_to>=?) AND (effective_to IS NULL OR effective_to>=effective_from)""",
                        (org_id, agent_id, role, to_ym, from_ym)).fetchone()
    if clash:
        raise HeadError("같은 기간에 이미 이 조직의 조직장으로 지정돼 있습니다.")
    mid = con.execute("""INSERT INTO org_manager(org_id, agent_id, role, effective_from, effective_to, source, created_by, created_at)
                         VALUES(?,?,?,?,?,'manual',?,?)""", (org_id, agent_id, role, from_ym, to_ym, by, db.now())).lastrowid
    card_grant.settle_agent(con, agent_id)
    return mid


def end_head(con, manager_id, ym) -> dict:
    """지정을 지금 끝낸다. 자료 기준 달 ym 부터는 조직장이 아니다. 앞으로 시작할 지정이면 시작하지 않은 채로 닫는다."""
    m = con.execute("SELECT * FROM org_manager WHERE id=?", (manager_id,)).fetchone()
    if not m:
        raise HeadError("없는 지정입니다.")
    if m["effective_to"] is not None and m["effective_to"] < ym:
        raise HeadError("이미 끝난 지정입니다.")
    con.execute("UPDATE org_manager SET effective_to=? WHERE id=?", (ym_add(ym, -1), manager_id))
    card_grant.settle_agent(con, m["agent_id"])
    return dict(m)


def assign_deputy(con, org_id, agent_id, date_from, date_to, reason, by, today=None) -> int:
    _org(con, org_id)
    _agent(con, agent_id)
    today = today or date.today()
    try:
        f, t = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except (TypeError, ValueError):
        raise HeadError("시작일과 끝일을 바르게 넣으세요.") from None
    if t < f or t < today:
        raise HeadError("끝일은 시작일과 오늘보다 앞일 수 없습니다.")
    if (t - f).days + 1 > MAX_DEPUTY_DAYS:
        raise HeadError(f"대리는 {MAX_DEPUTY_DAYS}일까지 지정할 수 있습니다. 더 길면 조직장으로 지정하세요.")
    did = con.execute("""INSERT INTO org_deputy(org_id, agent_id, date_from, date_to, reason, created_by, created_at)
                         VALUES(?,?,?,?,?,?,?)""", (org_id, agent_id, f.isoformat(), t.isoformat(), (reason or "").strip()[:200] or None, by, db.now())).lastrowid
    card_grant.settle_agent(con, agent_id, today=today)
    return did


def end_deputy(con, deputy_id, by, today=None) -> dict:
    d = con.execute("SELECT * FROM org_deputy WHERE id=? AND ended_at IS NULL", (deputy_id,)).fetchone()
    if not d:
        raise HeadError("없거나 이미 끝난 대리입니다.")
    today = today or date.today()
    end = min(date.fromisoformat(d["date_to"]), today - timedelta(days=1)).isoformat()
    con.execute("UPDATE org_deputy SET date_to=?, ended_at=?, ended_by=? WHERE id=?", (end, db.now(), by, deputy_id))
    card_grant.settle_agent(con, d["agent_id"], today=today)
    return dict(d)


def active_deputies(con, agent_id=None, today=None):
    today = (today or date.today()).isoformat()
    who = "AND d.agent_id=?" if agent_id is not None else ""
    return con.execute(f"""SELECT d.*, o.name org_name, o.level FROM org_deputy d JOIN org_unit o ON o.org_id=d.org_id
                           WHERE d.ended_at IS NULL AND d.date_from<=? AND d.date_to>=? {who} ORDER BY d.date_from""",
                       (today, today, *(() if agent_id is None else (agent_id,)))).fetchall()
