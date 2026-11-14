"""명함 승인과 반려 권한을 켜고 끄는 일과 그 승인 요청. D-44 를 고친 11월 14일 결정 두 가지.

권한
- 권한은 조직장이 되었다고 저절로 생기지 않는다. 메인 관리자가 승인 요청을 승인하거나 직접 켠다.
- 켤 수 있는 대상은 지금 조직장이거나 대리인 활성 계정이다. 메인 관리자 계정은 대상이 아니다.
- 효력은 켜진 줄이 있고, 지금 조직장이거나 대리이며, 켠 시각이 지금 맡은 자리의 시작보다 앞서지 않을 때만 있다.
  자리의 시작은 지정의 시작(조직장은 시작 달의 1일, 대리는 시작일)과 지정 줄을 만든 시각 가운데 늦은 쪽이다.
  자리가 여럿이면 가장 이른 시작을 쓴다. 예전 자리에서 켠 줄은 다시 맡아도 되살아나지 않는다.
- 끌 때는 줄을 지우지 않고 끈 사람과 끈 시각을 적는다. 다시 켜면 새 줄이 생긴다.
- 조직장이나 대리에서 빠지면 켜진 줄을 revoked_by='system:left' 로 끈다. 자리를 옮겨 새 자리의 시작이 켠 시각보다 늦어져도 같다.

승인 요청 (11월 14일 대표님 결정 "승인요청 만들어서 하는게 좋겠네")
- 조직장이나 대리가 되었는데 효력 있는 권한이 없으면 pending 요청이 저절로 생긴다. 까닭은 예전에 켜진 줄이 있었으면 reappointed, 아니면 new_head.
  지금 자리를 맡은 뒤에 요청이나 켜진 줄이 이미 있으면 다시 만들지 않는다. 반려하거나 메인 관리자가 끈 뒤에 저절로 다시 생기지 않게 하려는 것이다.
- 관리자 본인도 요청한다. 까닭은 self. 같은 사람의 pending 은 하나다.
- 메인 관리자가 여러 건을 한 번에 승인하거나 반려한다. 승인하면 켜진 줄이 생긴다.
- 조직장이나 대리에서 빠지면 pending 요청은 decided_by='system:left' 로 cancelled 가 된다.

언제 맞추나
- 지정 화면의 함수(app/org/heads.py)와 사원현황 동기화(app/org/sync.py)가 settle_agent 나 expire_stale 을 부른다. 커밋은 부른 쪽이 한다.
- 끝 달이 지나는 일처럼 따로 일이 없는 경우는 expire_stale 이 맞춘다. 로그인, 승인 요청 목록, 동기화 때 부른다.
  로그인한 사람 본인 몫은 app/web/auth.py 의 context 가 화면을 열 때마다 settle_agent 로 맞춘다.
- 조직장은 자료 기준 달로, 대리는 오늘 날짜로 따진다. app/web/auth.py 의 context 와 같은 규칙이다.

감사 기록 audit_log
- card_grant: 켬. after_json 의 how 가 "직접 켬" 이면 메인 관리자가 요청 없이 켠 것, "요청 승인" 이면 요청을 승인한 것이다.
- card_revoke: 끔. actor 가 system:left 면 조직장이나 대리에서 빠져 저절로 끈 것이다.
- card_request: 요청이 생김. card_request_reject: 반려. card_request_cancel: 빠져서 거둠.
- card_grant_denied: 메인 관리자가 아닌 사람의 켜기, 끄기, 승인, 반려 시도와 남의 이름으로 한 요청.

범위는 여기서 보지 않는다. 자기 범위 안 설계사의 명함 요청인지는 화면의 guard 가 준 설계사 목록으로 판단한다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from typing import Iterable

from app import db

TABLE = "card_approver"
REQ_TABLE = "card_approver_request"
BLOCKS = (("-- [card_grant:begin]", "-- [card_grant:end]", TABLE), ("-- [card_request:begin]", "-- [card_request:end]", REQ_TABLE))
BEGIN, END = BLOCKS[0][0], BLOCKS[0][1]
ACTION_GRANT, ACTION_REVOKE, ACTION_DENIED = "card_grant", "card_revoke", "card_grant_denied"
ACTION_REQUEST, ACTION_REJECT, ACTION_CANCEL = "card_request", "card_request_reject", "card_request_cancel"
HOW_DIRECT, HOW_REQUEST, HOW_LEFT = "직접 켬", "요청 승인", "조직장·대리에서 빠짐"
SYSTEM_LEFT = "system:left"               # 조직장이나 대리에서 빠져 저절로 끄거나 거둔 표시
SYSTEM_INACTIVE = "system:inactive"       # 계정이 중지되어 요청을 거둔 표시
SYSTEM_AUTO = "system:appointed"          # 조직장이나 대리가 되어 저절로 생긴 요청의 요청한 사람
REASONS = ("new_head", "reappointed", "self")
REASON_KO = {"new_head": "새로 조직장·대리가 됨", "reappointed": "다시 맡음", "self": "본인 요청"}
STATUSES = ("pending", "approved", "rejected", "cancelled")
STATUS_KO = {"pending": "승인 대기", "approved": "승인", "rejected": "반려", "cancelled": "거둠"}


class GrantError(ValueError):
    pass


# ------------------------------------------------------------------ 표 준비
def _statements(begin: str = BEGIN, end: str = END) -> list:
    text = db.SCHEMA.read_text(encoding="utf-8")
    i, j = text.find(begin), text.find(end)
    if i < 0 or j < i:
        raise RuntimeError(f"schema.sql 에 {begin} 묶음이 없습니다")
    body = "\n".join(re.sub(r"--.*$", "", line) for line in text[i + len(begin):j].splitlines())
    out, buf = [], ""
    for part in body.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip(" \n;"):
                out.append(buf.strip())
            buf = ""
    return out


def _has(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _has_table(con) -> bool:
    return _has(con, TABLE)


def ensure(con) -> None:
    """표가 없는 오래된 DB 에서 두 표를 만든다. 묶음은 모두 IF NOT EXISTS 다. 커밋하지 않는다."""
    for begin, end, name in BLOCKS:
        if not _has(con, name):
            for sql in _statements(begin, end):
                con.execute(sql)


# ------------------------------------------------------------------ 지금 조직장이거나 대리인가
def _current_ym(con) -> str:
    """app/web/auth.py 의 current_ym 과 같은 규칙. auth 가 이 모듈을 부르므로 거꾸로 부르지 않게 여기 둔다."""
    r = con.execute("SELECT MAX(period_ym) FROM calc_run WHERE status='done'").fetchone()
    return (r[0] if r else None) or datetime.now().strftime("%Y%m")


def _heads_now(con, agent_id, today: date | None = None) -> list:
    """agent_id 가 지금 맡은 조직장 자리와 대리 자리. 없으면 빈 목록.

    since 는 그 자리의 시작이다. 지정의 시작과 지정 줄을 만든 시각 가운데 늦은 쪽이고 'YYYY-MM-DD' 나 'YYYY-MM-DD HH:MM:SS' 다."""
    if agent_id is None:
        return []
    ym = _current_ym(con)
    d = (today or date.today()).isoformat()
    rows = con.execute(
        """SELECT m.org_id, m.role, o.name org_name, o.level,
                  MAX(substr(m.effective_from, 1, 4) || '-' || substr(m.effective_from, 5, 2) || '-01', COALESCE(m.created_at, '')) since
           FROM org_manager m JOIN org_unit o ON o.org_id=m.org_id
           WHERE m.agent_id=? AND m.effective_from<=? AND (m.effective_to IS NULL OR m.effective_to>=?)""",
        (agent_id, ym, ym)).fetchall()
    rows += con.execute(
        """SELECT d.org_id, 'deputy' role, o.name org_name, o.level, MAX(d.date_from, COALESCE(d.created_at, '')) since
           FROM org_deputy d JOIN org_unit o ON o.org_id=d.org_id
           WHERE d.agent_id=? AND d.ended_at IS NULL AND d.date_from<=? AND d.date_to>=?""",
        (agent_id, d, d)).fetchall()
    return [dict(r) for r in rows]


def _since(seats: list) -> str | None:
    """지금 맡은 자리들의 가장 이른 시작. 자리가 없으면 None."""
    return min((s["since"] for s in seats), default=None)


def _account(con, login_id):
    return con.execute("SELECT user_id, login_id, agent_id, display_name, is_main_admin, status FROM user_account WHERE login_id=?",
                       (str(login_id or "").strip(),)).fetchone()


def _is_main(con, login_id) -> bool:
    u = _account(con, login_id)
    return bool(u and u["status"] == "active" and u["is_main_admin"])


def _active_row(con, login_id):
    if not _has_table(con):
        return None
    return con.execute(f"SELECT * FROM {TABLE} WHERE login_id=? AND revoked_at IS NULL", (login_id,)).fetchone()


def _row_effective(row, seats: list) -> bool:
    """켜진 줄이 지금 맡은 자리에서 켠 것인가. 켠 시각이 자리의 시작보다 앞이면 예전 자리의 줄이라 효력이 없다."""
    return row is not None and bool(seats) and row["granted_at"] >= _since(seats)


def _pending_row(con, login_id):
    if not _has(con, REQ_TABLE):
        return None
    return con.execute(f"SELECT * FROM {REQ_TABLE} WHERE login_id=? AND status='pending'", (login_id,)).fetchone()


# ------------------------------------------------------------------ 조회
def is_granted(con, login_id: str, today: date | None = None) -> bool:
    """이 계정이 지금 명함 승인과 반려를 할 수 있는가.

    활성 메인 관리자는 늘 참이다. 관리자는 켜진 줄이 있고, 지금도 조직장이거나 대리이며,
    그 줄을 지금 맡은 자리에서 켰을 때만 참이다. 중지된 계정, 설계사, 없는 계정은 거짓이다."""
    u = _account(con, login_id)
    if not u or u["status"] != "active":
        return False
    if u["is_main_admin"]:
        return True
    if u["agent_id"] is None:
        return False
    row = _active_row(con, u["login_id"])
    if row is None:
        return False
    return _row_effective(row, _heads_now(con, u["agent_id"], today))


def granted(con, today: date | None = None) -> list:
    """켜져 있는 줄 전부. effective 는 지금 효력이 있는지다. 조직장에서 빠졌거나 예전 자리의 줄이거나 계정이 중지되면 거짓이다."""
    if not _has_table(con):
        return []
    rows = con.execute(
        f"""SELECT g.id, g.login_id, g.agent_id, g.granted_by, g.granted_at, u.display_name, u.status
            FROM {TABLE} g LEFT JOIN user_account u ON u.login_id=g.login_id
            WHERE g.revoked_at IS NULL ORDER BY g.login_id""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["effective"] = d.pop("status") == "active" and _row_effective(r, _heads_now(con, d["agent_id"], today))
        out.append(d)
    return out


def _current_head_agents(con, today: date | None = None) -> set:
    ym = _current_ym(con)
    d = (today or date.today()).isoformat()
    ids = {r[0] for r in con.execute(
        "SELECT agent_id FROM org_manager WHERE effective_from<=? AND (effective_to IS NULL OR effective_to>=?)", (ym, ym))}
    ids |= {r[0] for r in con.execute(
        "SELECT agent_id FROM org_deputy WHERE ended_at IS NULL AND date_from<=? AND date_to>=?", (d, d))}
    return ids


def candidates(con, today: date | None = None) -> list:
    """지금 조직장이거나 대리인 활성 관리자 계정과 권한 여부. 한 사람에 한 줄이고 맡은 자리는 heads 에 모은다.
    메인 관리자 계정과 계정이 없는 조직장은 넣지 않는다. request_id 는 승인 대기 중인 요청이 있으면 그 번호다."""
    agent_ids = _current_head_agents(con, today)
    if not agent_ids:
        return []
    q = ",".join("?" * len(agent_ids))
    users = con.execute(
        f"""SELECT login_id, agent_id, display_name FROM user_account
            WHERE agent_id IN ({q}) AND status='active' AND is_main_admin=0 ORDER BY login_id""", tuple(agent_ids)).fetchall()
    on = {r[0] for r in con.execute(f"SELECT login_id FROM {TABLE} WHERE revoked_at IS NULL")} if _has_table(con) else set()
    pend = {r[0]: r[1] for r in con.execute(f"SELECT login_id, id FROM {REQ_TABLE} WHERE status='pending'")} if _has(con, REQ_TABLE) else {}
    return [{"login_id": u["login_id"], "agent_id": u["agent_id"], "display_name": u["display_name"],
             "heads": _heads_now(con, u["agent_id"], today), "granted": u["login_id"] in on,
             "request_id": pend.get(u["login_id"])} for u in users]


def my_request(con, login_id: str) -> dict | None:
    """이 계정의 가장 최근 요청. 관리자 본인 화면에서 요청 상태를 보일 때 쓴다. 없으면 None."""
    if not _has(con, REQ_TABLE):
        return None
    r = con.execute(f"SELECT * FROM {REQ_TABLE} WHERE login_id=? ORDER BY requested_at DESC, id DESC LIMIT 1",
                    (str(login_id or "").strip(),)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["reason_ko"], d["status_ko"] = REASON_KO.get(d["reason"], d["reason"]), STATUS_KO.get(d["status"], d["status"])
    return d


def pending_requests(con, today: date | None = None) -> list:
    """승인 대기 요청. 메인 관리자 화면용이고 오래된 요청부터 돌려준다.

    먼저 expire_stale 로 빠진 사람의 요청을 거두고 새로 맡은 사람의 요청을 만든 뒤 커밋한다.
    한 줄: {id, login_id, agent_id, name, code(사번), orgs: [{org_id, text, role, level, since}], reason, reason_ko,
           requested_at, requested_by}. orgs 는 지금 맡은 자리이고 text 는 뿌리 아래부터 이은 조직 이름이다."""
    expire_stale(con, today)
    if not _has(con, REQ_TABLE):
        return []
    # sync 가 이 모듈을 부르므로 여기서 부른다
    from app.org.sync import org_texts
    texts = org_texts(con)
    rows = con.execute(
        f"""SELECT r.id, r.login_id, r.agent_id, r.reason, r.requested_at, r.requested_by, u.display_name name, a.internal_code code
            FROM {REQ_TABLE} r LEFT JOIN user_account u ON u.login_id=r.login_id LEFT JOIN agent a ON a.agent_id=r.agent_id
            WHERE r.status='pending' ORDER BY r.requested_at, r.id""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reason_ko"] = REASON_KO.get(d["reason"], d["reason"])
        d["orgs"] = [{"org_id": h["org_id"], "text": texts.get(h["org_id"], h["org_name"]), "role": h["role"], "level": h["level"],
                      "since": h["since"]} for h in _heads_now(con, d["agent_id"], today)]
        out.append(d)
    return out


# ------------------------------------------------------------------ 공통
def _ids(login_ids) -> list:
    if isinstance(login_ids, str):
        login_ids = [login_ids]
    seen, out = set(), []
    for x in login_ids or ():
        s = str(x or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _req_ids(ids) -> list:
    if isinstance(ids, (int, str)):
        ids = [ids]
    seen, out = set(), []
    for x in ids or ():
        try:
            n = int(str(x).strip())
        except (TypeError, ValueError):
            continue
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _audit(con, actor, action, target, after) -> None:
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), str(actor or "")[:64], action, target, None, json.dumps(after, ensure_ascii=False)))


def _require_main(con, by, what: str, ids: list) -> None:
    if not _is_main(con, by):
        _audit(con, by, ACTION_DENIED, f"{what}:{len(ids)}", {"op": what, "count": len(ids)})
        con.commit()
        raise GrantError("명함 승인 권한은 메인 관리자만 켜고 끄고, 승인 요청을 처리할 수 있습니다.")


def _insert_grant(con, u, by, at, how, request_id=None) -> int:
    gid = con.execute(f"INSERT INTO {TABLE}(login_id, agent_id, granted_by, granted_at) VALUES(?,?,?,?)",
                      (u["login_id"], u["agent_id"], by, at)).lastrowid
    after = {"card_approver": True, "how": how}
    if request_id is not None:
        after["request_id"] = request_id
    _audit(con, by, ACTION_GRANT, f"user:{u['login_id']}", after)
    return gid


def _revoke_left(con, row, at) -> None:
    con.execute(f"UPDATE {TABLE} SET revoked_by=?, revoked_at=? WHERE id=?", (SYSTEM_LEFT, at, row["id"]))
    _audit(con, SYSTEM_LEFT, ACTION_REVOKE, f"user:{row['login_id']}", {"card_approver": False, "how": HOW_LEFT})


def _close_request(con, req, status, by, at, grant_id=None) -> None:
    con.execute(f"UPDATE {REQ_TABLE} SET status=?, decided_by=?, decided_at=?, grant_id=? WHERE id=?",
                (status, by, at, grant_id, req["id"]))


def _new_request(con, u, reason, by, at) -> int:
    rid = con.execute(f"""INSERT INTO {REQ_TABLE}(login_id, agent_id, reason, status, requested_at, requested_by)
                          VALUES(?,?,?,'pending',?,?)""", (u["login_id"], u["agent_id"], reason, at, by)).lastrowid
    _audit(con, by, ACTION_REQUEST, f"user:{u['login_id']}", {"request_id": rid, "reason": reason})
    return rid


# ------------------------------------------------------------------ 맞추기
def _settle(con, u, today: date | None, counts: dict, create: bool = True) -> None:
    """한 계정의 권한 줄과 요청을 지금 자리에 맞춘다. 커밋하지 않는다.

    1. 켜진 줄이 있는데 지금 자리가 없거나 예전 자리에서 켠 줄이면 system:left 로 끈다.
    2. 지금 자리가 없으면 pending 요청을 system:left 로 거둔다. 계정이 중지됐으면 system:inactive 로 거둔다.
    3. create 가 참이고, 지금 자리가 있고 효력 있는 줄도 pending 요청도 없으며, 지금 자리를 맡은 뒤의 요청이나 켠 줄이 없으면 요청을 만든다."""
    if u is None or u["is_main_admin"] or u["agent_id"] is None:
        return
    ensure(con)
    at = db.now()
    seats = _heads_now(con, u["agent_id"], today)
    row = _active_row(con, u["login_id"])
    if row is not None and not _row_effective(row, seats):
        _revoke_left(con, row, at)
        counts["revoked"] += 1
        row = None
    pend = _pending_row(con, u["login_id"])
    if not seats or u["status"] != "active":
        if pend is not None:
            _close_request(con, pend, "cancelled", SYSTEM_LEFT if not seats else SYSTEM_INACTIVE, at)
            _audit(con, SYSTEM_LEFT if not seats else SYSTEM_INACTIVE, ACTION_CANCEL, f"user:{u['login_id']}",
                   {"request_id": pend["id"], "status": "cancelled"})
            counts["cancelled"] += 1
        return
    if not create or row is not None or pend is not None:
        return
    since = _since(seats)
    seen = con.execute(f"SELECT 1 FROM {REQ_TABLE} WHERE login_id=? AND requested_at>=? LIMIT 1", (u["login_id"], since)).fetchone() \
        or con.execute(f"SELECT 1 FROM {TABLE} WHERE login_id=? AND granted_at>=? LIMIT 1", (u["login_id"], since)).fetchone()
    if seen:
        return
    before = con.execute(f"SELECT 1 FROM {TABLE} WHERE login_id=? LIMIT 1", (u["login_id"],)).fetchone()
    _new_request(con, u, "reappointed" if before else "new_head", SYSTEM_AUTO, at)
    counts["requested"] += 1


def _counts() -> dict:
    return {"revoked": 0, "cancelled": 0, "requested": 0}


def settle_agent(con, agent_id, today: date | None = None) -> dict:
    """설계사 한 사람의 계정을 맞춘다. 조직장과 대리를 지정하거나 끝낸 직후에 부른다. 커밋하지 않는다.

    돌려주는 값은 {"revoked": 끈 줄 수, "cancelled": 거둔 요청 수, "requested": 만든 요청 수}."""
    counts = _counts()
    if agent_id is None:
        return counts
    for u in con.execute("SELECT user_id, login_id, agent_id, display_name, is_main_admin, status FROM user_account WHERE agent_id=?",
                         (agent_id,)).fetchall():
        _settle(con, u, today, counts)
    return counts


def settle_login(con, login_id: str, today: date | None = None) -> dict:
    """계정 하나를 맞춘다. auth.context 가 화면을 열 때마다 본인 몫으로 부른다. 커밋하지 않는다."""
    counts = _counts()
    _settle(con, _account(con, login_id), today, counts)
    return counts


def expire_stale(con, today: date | None = None, commit: bool = True) -> dict:
    """모든 계정을 맞춘다. 켜진 줄이 있거나, 승인 대기 요청이 있거나, 지금 조직장이거나 대리인 계정을 본다.

    끝 달이 지나거나 대리 기간이 끝나는 일처럼 따로 일이 없는 경우를 맞추려고 둔다. 로그인, 승인 요청 목록, 사원현황 동기화 때 부른다.
    commit=False 면 커밋하지 않는다. 동기화의 미리보기는 이것까지 되돌린다.
    돌려주는 값은 {"revoked": 끈 줄 수, "cancelled": 거둔 요청 수, "requested": 만든 요청 수}."""
    counts = _counts()
    logins = set()
    if _has_table(con):
        logins |= {r[0] for r in con.execute(f"SELECT login_id FROM {TABLE} WHERE revoked_at IS NULL")}
    if _has(con, REQ_TABLE):
        logins |= {r[0] for r in con.execute(f"SELECT login_id FROM {REQ_TABLE} WHERE status='pending'")}
    heads = _current_head_agents(con, today)
    if heads:
        q = ",".join("?" * len(heads))
        logins |= {r[0] for r in con.execute(
            f"SELECT login_id FROM user_account WHERE agent_id IN ({q}) AND status='active' AND is_main_admin=0", tuple(heads))}
    for lid in sorted(logins):
        _settle(con, _account(con, lid), today, counts)
    if commit:
        con.commit()
    return counts


# ------------------------------------------------------------------ 요청과 처리
def request(con, login_id: str, by: str, today: date | None = None) -> dict:
    """관리자 본인이 권한을 요청한다. by 는 login_id 와 같아야 한다. 끝에 커밋한다.

    지금 조직장이거나 대리인 활성 관리자 계정만 요청한다. 효력 있는 권한이 이미 있으면 GrantError 를 올린다.
    승인 대기 요청이 이미 있으면 새로 만들지 않고 그 요청을 돌려준다.
    돌려주는 값은 {"id": 요청 번호, "created": 새로 만들었는가, "reason": 까닭}."""
    lid, actor = str(login_id or "").strip(), str(by or "").strip()
    if not lid or lid != actor:
        _audit(con, actor, ACTION_DENIED, f"request:{lid}", {"op": "request", "count": 1})
        con.commit()
        raise GrantError("명함 승인 권한은 본인만 요청할 수 있습니다.")
    u = _account(con, lid)
    if not u or u["status"] != "active" or u["is_main_admin"] or u["agent_id"] is None:
        raise GrantError("요청할 수 있는 관리자 계정이 아닙니다.")
    counts = _counts()
    _settle(con, u, today, counts, create=False)            # 예전 자리의 줄을 끈다. 저절로 생기는 요청은 만들지 않는다
    if not _heads_now(con, u["agent_id"], today):
        con.commit()
        raise GrantError("지금 조직장이거나 대리일 때만 요청할 수 있습니다.")
    if _active_row(con, lid) is not None:
        con.commit()
        raise GrantError("명함 승인 권한이 이미 켜져 있습니다.")
    pend = _pending_row(con, lid)
    if pend is not None:
        con.commit()
        return {"id": pend["id"], "created": False, "reason": pend["reason"]}
    rid = _new_request(con, u, "self", lid, db.now())
    con.commit()
    return {"id": rid, "created": True, "reason": "self"}


def _decide(con, ids, by: str, approve: bool, today: date | None) -> dict:
    nums = _req_ids(ids)
    _require_main(con, by, "approve" if approve else "reject", nums)
    if not nums:
        raise GrantError("처리할 요청을 고르세요.")
    ensure(con)
    done, skipped = [], []
    for rid in nums:
        req = con.execute(f"SELECT * FROM {REQ_TABLE} WHERE id=?", (rid,)).fetchone()
        if req is None:
            skipped.append({"id": rid, "why": "없는 요청입니다."})
            continue
        if req["status"] != "pending":
            skipped.append({"id": rid, "why": f"이미 {STATUS_KO.get(req['status'], req['status'])} 처리된 요청입니다."})
            continue
        u = _account(con, req["login_id"])
        counts = _counts()
        _settle(con, u, today, counts)                      # 그사이 빠졌으면 여기서 거둔다
        req = con.execute(f"SELECT * FROM {REQ_TABLE} WHERE id=?", (rid,)).fetchone()
        if req["status"] != "pending":
            skipped.append({"id": rid, "why": "조직장이나 대리에서 빠졌거나 계정이 중지되어 요청을 거두었습니다."})
            continue
        at = db.now()
        if not approve:
            _close_request(con, req, "rejected", by, at)
            _audit(con, by, ACTION_REJECT, f"user:{req['login_id']}", {"request_id": rid, "status": "rejected"})
            done.append(rid)
            continue
        row = _active_row(con, req["login_id"])            # _settle 뒤에 남은 줄은 효력이 있다
        gid = row["id"] if row is not None else _insert_grant(con, u, by, at, HOW_REQUEST, rid)
        _close_request(con, req, "approved", by, at, gid)
        done.append(rid)
    con.commit()
    return {"approved" if approve else "rejected": done, "skipped": skipped}


def approve_requests(con, ids: Iterable, by: str, today: date | None = None) -> dict:
    """승인 대기 요청 여러 건을 한 번에 승인한다. by 는 활성 메인 관리자여야 한다. 끝에 커밋한다.

    승인하면 켜진 줄이 생긴다. 이미 처리됐거나, 없거나, 그사이 조직장이나 대리에서 빠진 요청은 건너뛰고 까닭을 돌려준다.
    돌려주는 값은 {"approved": [요청 번호], "skipped": [{"id", "why"}]}."""
    return _decide(con, ids, by, True, today)


def reject_requests(con, ids: Iterable, by: str, today: date | None = None) -> dict:
    """승인 대기 요청 여러 건을 한 번에 반려한다. by 는 활성 메인 관리자여야 한다. 끝에 커밋한다.

    반려한 뒤에는 같은 자리에서 요청이 저절로 다시 생기지 않는다. 본인은 다시 요청할 수 있다.
    돌려주는 값은 {"rejected": [요청 번호], "skipped": [{"id", "why"}]}."""
    return _decide(con, ids, by, False, today)


# ------------------------------------------------------------------ 메인 관리자가 직접 켜고 끄기
def grant(con, login_ids: Iterable[str], by: str, today: date | None = None) -> dict:
    """요청 없이 여러 계정에 권한을 한 번에 켠다. by 는 활성 메인 관리자여야 한다. 감사 기록에는 "직접 켬" 으로 남는다.

    대상 가운데 하나라도 켤 수 없는 계정이면 아무것도 켜지 않고 GrantError 를 올린다.
    효력 있는 줄이 이미 있으면 그대로 둔다. 예전 자리의 줄이 남아 있으면 system:left 로 끄고 새 줄을 만든다.
    승인 대기 요청이 있으면 승인으로 닫는다. 돌려주는 값은 {"changed": [login_id], "unchanged": [login_id]}."""
    ids = _ids(login_ids)
    _require_main(con, by, "grant", ids)
    if not ids:
        raise GrantError("켤 관리자를 고르세요.")
    bad = []
    targets = []
    for lid in ids:
        u = _account(con, lid)
        if not u or u["status"] != "active" or u["is_main_admin"] or u["agent_id"] is None or not _heads_now(con, u["agent_id"], today):
            bad.append(lid)
        else:
            targets.append(u)
    if bad:
        raise GrantError(f"지금 조직장이거나 대리인 활성 관리자 계정만 켤 수 있습니다. 켤 수 없는 계정 {len(bad)}개가 있어 아무것도 켜지 않았습니다.")
    ensure(con)
    changed, unchanged = [], []
    at = db.now()
    for u in targets:
        row = _active_row(con, u["login_id"])
        if row is not None and _row_effective(row, _heads_now(con, u["agent_id"], today)):
            unchanged.append(u["login_id"])
            continue
        if row is not None:
            _revoke_left(con, row, at)
        gid = _insert_grant(con, u, by, at, HOW_DIRECT)
        pend = _pending_row(con, u["login_id"])
        if pend is not None:
            _close_request(con, pend, "approved", by, at, gid)
        changed.append(u["login_id"])
    con.commit()
    return {"changed": changed, "unchanged": unchanged}


def revoke(con, login_ids: Iterable[str], by: str) -> dict:
    """여러 계정의 권한을 한 번에 끈다. by 는 활성 메인 관리자여야 한다.

    줄을 지우지 않고 끈 사람과 끈 시각을 적는다. 조직장에서 이미 빠진 계정도 끌 수 있다.
    켜진 줄이 없는 계정은 그대로 둔다. 돌려주는 값은 {"changed": [login_id], "unchanged": [login_id]}."""
    ids = _ids(login_ids)
    _require_main(con, by, "revoke", ids)
    if not ids:
        raise GrantError("끌 관리자를 고르세요.")
    ensure(con)
    changed, unchanged = [], []
    at = db.now()
    for lid in ids:
        row = _active_row(con, lid)
        if row is None:
            unchanged.append(lid)
            continue
        con.execute(f"UPDATE {TABLE} SET revoked_by=?, revoked_at=? WHERE id=?", (by, at, row["id"]))
        _audit(con, by, ACTION_REVOKE, f"user:{lid}", {"card_approver": False})
        changed.append(lid)
    con.commit()
    return {"changed": changed, "unchanged": unchanged}
