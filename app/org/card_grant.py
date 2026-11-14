"""명함 승인과 반려 권한을 켜고 끄는 일. D-44 를 고친 11월 14일 결정.

- 권한은 조직장이 되었다고 저절로 생기지 않는다. 메인 관리자가 관리자 가운데 골라서 켠다.
- 켤 수 있는 대상은 지금 조직장이거나 대리인 활성 계정이다. 메인 관리자 계정은 대상이 아니다.
- 효력은 켜져 있고, 지금도 조직장이거나 대리일 때만 있다. 조직장에서 빠지거나 대리 기간이 끝나면 줄이 남아 있어도 효력이 없다.
- 끌 때는 줄을 지우지 않고 끈 사람과 끈 시각을 적는다. 다시 켜면 새 줄이 생긴다.
- 켜고 끈 일과 메인 관리자가 아닌 사람의 시도는 audit_log 에 남긴다. grant 와 revoke 는 끝에 커밋한다.
- 조직장은 자료 기준 달로, 대리는 오늘 날짜로 따진다. app/web/auth.py 의 context 와 같은 규칙이다.

범위는 여기서 보지 않는다. 자기 범위 안 설계사의 요청인지는 화면의 guard 가 준 설계사 목록으로 판단한다.
관리자 본인의 요청은 본인 범위에 본인이 들어 있어도 처리하지 못한다. 그 검사는 승인 판단 쪽에서 한다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from typing import Iterable

from app import db

TABLE = "card_approver"
BEGIN, END = "-- [card_grant:begin]", "-- [card_grant:end]"
ACTION_GRANT, ACTION_REVOKE, ACTION_DENIED = "card_grant", "card_revoke", "card_grant_denied"


class GrantError(ValueError):
    pass


# ------------------------------------------------------------------ 표 준비
def _statements() -> list:
    text = db.SCHEMA.read_text(encoding="utf-8")
    i, j = text.find(BEGIN), text.find(END)
    if i < 0 or j < i:
        raise RuntimeError("schema.sql 에 명함 승인 권한 묶음이 없습니다")
    body = "\n".join(re.sub(r"--.*$", "", line) for line in text[i + len(BEGIN):j].splitlines())
    out, buf = [], ""
    for part in body.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip(" \n;"):
                out.append(buf.strip())
            buf = ""
    return out


def _has_table(con) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def ensure(con) -> None:
    """표가 없는 오래된 DB 에서 표를 만든다. 묶음은 모두 IF NOT EXISTS 다."""
    if not _has_table(con):
        for sql in _statements():
            con.execute(sql)


# ------------------------------------------------------------------ 지금 조직장이거나 대리인가
def _current_ym(con) -> str:
    """app/web/auth.py 의 current_ym 과 같은 규칙. auth 가 이 모듈을 부르므로 거꾸로 부르지 않게 여기 둔다."""
    r = con.execute("SELECT MAX(period_ym) FROM calc_run WHERE status='done'").fetchone()
    return (r[0] if r else None) or datetime.now().strftime("%Y%m")


def _heads_now(con, agent_id, today: date | None = None) -> list:
    """agent_id 가 지금 맡은 조직장 자리와 대리 자리. 없으면 빈 목록."""
    if agent_id is None:
        return []
    ym = _current_ym(con)
    d = (today or date.today()).isoformat()
    rows = con.execute(
        """SELECT m.org_id, m.role, o.name org_name, o.level FROM org_manager m JOIN org_unit o ON o.org_id=m.org_id
           WHERE m.agent_id=? AND m.effective_from<=? AND (m.effective_to IS NULL OR m.effective_to>=?)""",
        (agent_id, ym, ym)).fetchall()
    rows += con.execute(
        """SELECT d.org_id, 'deputy' role, o.name org_name, o.level FROM org_deputy d JOIN org_unit o ON o.org_id=d.org_id
           WHERE d.agent_id=? AND d.ended_at IS NULL AND d.date_from<=? AND d.date_to>=?""",
        (agent_id, d, d)).fetchall()
    return [dict(r) for r in rows]


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


# ------------------------------------------------------------------ 조회
def is_granted(con, login_id: str, today: date | None = None) -> bool:
    """이 계정이 지금 명함 승인과 반려를 할 수 있는가.

    활성 메인 관리자는 늘 참이다. 관리자는 켜진 줄이 있고 지금도 조직장이거나 대리일 때만 참이다.
    중지된 계정, 설계사, 없는 계정은 거짓이다."""
    u = _account(con, login_id)
    if not u or u["status"] != "active":
        return False
    if u["is_main_admin"]:
        return True
    if u["agent_id"] is None or _active_row(con, u["login_id"]) is None:
        return False
    return bool(_heads_now(con, u["agent_id"], today))


def granted(con, today: date | None = None) -> list:
    """켜져 있는 줄 전부. effective 는 지금 효력이 있는지다. 조직장에서 빠졌거나 계정이 중지되면 거짓이다."""
    if not _has_table(con):
        return []
    rows = con.execute(
        f"""SELECT g.id, g.login_id, g.agent_id, g.granted_by, g.granted_at, u.display_name, u.status
            FROM {TABLE} g LEFT JOIN user_account u ON u.login_id=g.login_id
            WHERE g.revoked_at IS NULL ORDER BY g.login_id""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["effective"] = d.pop("status") == "active" and bool(_heads_now(con, d["agent_id"], today))
        out.append(d)
    return out


def candidates(con, today: date | None = None) -> list:
    """지금 조직장이거나 대리인 활성 관리자 계정과 권한 여부. 한 사람에 한 줄이고 맡은 자리는 heads 에 모은다.
    메인 관리자 계정과 계정이 없는 조직장은 넣지 않는다."""
    ym = _current_ym(con)
    d = (today or date.today()).isoformat()
    agent_ids = {r[0] for r in con.execute(
        "SELECT agent_id FROM org_manager WHERE effective_from<=? AND (effective_to IS NULL OR effective_to>=?)", (ym, ym))}
    agent_ids |= {r[0] for r in con.execute(
        "SELECT agent_id FROM org_deputy WHERE ended_at IS NULL AND date_from<=? AND date_to>=?", (d, d))}
    if not agent_ids:
        return []
    q = ",".join("?" * len(agent_ids))
    users = con.execute(
        f"""SELECT login_id, agent_id, display_name FROM user_account
            WHERE agent_id IN ({q}) AND status='active' AND is_main_admin=0 ORDER BY login_id""", tuple(agent_ids)).fetchall()
    on = {r[0] for r in con.execute(f"SELECT login_id FROM {TABLE} WHERE revoked_at IS NULL")} if _has_table(con) else set()
    return [{"login_id": u["login_id"], "agent_id": u["agent_id"], "display_name": u["display_name"],
             "heads": _heads_now(con, u["agent_id"], today), "granted": u["login_id"] in on} for u in users]


# ------------------------------------------------------------------ 켜고 끄기
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


def _audit(con, actor, action, target, after) -> None:
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), str(actor or "")[:64], action, target, None, json.dumps(after, ensure_ascii=False)))


def _require_main(con, by, what: str, ids: list) -> None:
    if not _is_main(con, by):
        _audit(con, by, ACTION_DENIED, f"{what}:{len(ids)}", {"op": what, "count": len(ids)})
        con.commit()
        raise GrantError("명함 승인 권한은 메인 관리자만 켜고 끌 수 있습니다.")


def grant(con, login_ids: Iterable[str], by: str, today: date | None = None) -> dict:
    """여러 계정에 권한을 한 번에 켠다. by 는 활성 메인 관리자여야 한다.

    대상 가운데 하나라도 켤 수 없는 계정이면 아무것도 켜지 않고 GrantError 를 올린다.
    이미 켜진 계정은 그대로 둔다. 돌려주는 값은 {"changed": [login_id], "unchanged": [login_id]}."""
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
        if _active_row(con, u["login_id"]) is not None:
            unchanged.append(u["login_id"])
            continue
        con.execute(f"INSERT INTO {TABLE}(login_id, agent_id, granted_by, granted_at) VALUES(?,?,?,?)",
                    (u["login_id"], u["agent_id"], by, at))
        _audit(con, by, ACTION_GRANT, f"user:{u['login_id']}", {"card_approver": True})
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
