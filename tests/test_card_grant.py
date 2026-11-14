"""명함 승인 권한을 켜고 끄는 시험. D-44 를 고친 11월 14일 결정.

권한은 메인 관리자가 지금 조직장이거나 대리인 관리자에게 골라서 켠다. 조직장에서 빠지거나 대리 기간이 끝나면 효력이 없다.
"""
import json
import pathlib
import sqlite3
import tempfile
from datetime import date, timedelta

import pytest

from app import db, masters
from app.org import card_grant, heads
from app.web import auth


@pytest.fixture()
def con():
    path = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    c = db.init_db(path)
    masters.seed(c)
    auth.seed_accounts(c, "grant-pass-1234")
    yield c
    c.close()


def _uid(con, login_id):
    return con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0]


def _heads(con, n=2):
    """지금 조직장인 계정 n 개. 샘플 자료의 조직장 지정을 쓴다."""
    ym = auth.current_ym(con)
    rows = con.execute(
        """SELECT DISTINCT u.login_id FROM org_manager m JOIN user_account u ON u.agent_id=m.agent_id
           WHERE m.effective_from<=? AND (m.effective_to IS NULL OR m.effective_to>=?) ORDER BY u.login_id LIMIT ?""", (ym, ym, n)).fetchall()
    assert len(rows) == n
    return [r[0] for r in rows]


def _plain(con, n=1):
    """조직장도 대리도 아닌 설계사 계정."""
    rows = con.execute(
        """SELECT u.login_id, u.agent_id FROM user_account u WHERE u.agent_id IS NOT NULL
           AND u.agent_id NOT IN (SELECT agent_id FROM org_manager) AND u.agent_id NOT IN (SELECT agent_id FROM org_deputy)
           ORDER BY u.login_id LIMIT ?""", (n,)).fetchall()
    assert len(rows) == n
    return [(r[0], r[1]) for r in rows]


def _team_org(con):
    return con.execute("SELECT org_id FROM org_unit WHERE level='team' LIMIT 1").fetchone()[0]


def _audit(con, action):
    return con.execute("SELECT actor, target, after_json FROM audit_log WHERE action=? ORDER BY id", (action,)).fetchall()


def test_only_main_admin_can_grant_and_revoke(con):
    h1, h2 = _heads(con)
    (agent, _), = _plain(con)
    for by in (h2, agent, "nobody"):
        with pytest.raises(card_grant.GrantError):
            card_grant.grant(con, [h1], by=by)
        with pytest.raises(card_grant.GrantError):
            card_grant.revoke(con, [h1], by=by)
    assert not card_grant.is_granted(con, h1)
    assert card_grant.granted(con) == []
    denied = _audit(con, card_grant.ACTION_DENIED)
    assert len(denied) == 6 and {r["actor"] for r in denied} == {h2, agent, "nobody"}

    # 중지된 메인 관리자도 켜지 못한다
    con.execute("UPDATE user_account SET status='suspended' WHERE login_id='admin'")
    with pytest.raises(card_grant.GrantError):
        card_grant.grant(con, [h1], by="admin")
    con.execute("UPDATE user_account SET status='active' WHERE login_id='admin'")
    assert card_grant.grant(con, [h1], by="admin")["changed"] == [h1]
    assert card_grant.is_granted(con, h1)


def test_grant_many_at_once_and_targets_must_be_heads(con):
    h1, h2 = _heads(con)
    (agent, _), = _plain(con)
    out = card_grant.grant(con, [h1, h2, h1, " "], by="admin")
    assert out == {"changed": [h1, h2], "unchanged": []}
    assert card_grant.is_granted(con, h1) and card_grant.is_granted(con, h2)
    assert {g["login_id"] for g in card_grant.granted(con)} == {h1, h2}
    assert all(g["effective"] and g["granted_by"] == "admin" for g in card_grant.granted(con))
    assert len(_audit(con, card_grant.ACTION_GRANT)) == 2

    # 다시 켜면 그대로 둔다
    assert card_grant.grant(con, [h1], by="admin") == {"changed": [], "unchanged": [h1]}
    assert len(_audit(con, card_grant.ACTION_GRANT)) == 2

    # 조직장이 아닌 설계사나 메인 관리자가 섞이면 아무것도 켜지 않는다
    card_grant.revoke(con, [h2], by="admin")
    for bad in ([h2, agent], [h2, "admin"], [h2, "nobody"]):
        with pytest.raises(card_grant.GrantError):
            card_grant.grant(con, bad, by="admin")
        assert not card_grant.is_granted(con, h2)
    assert not card_grant.is_granted(con, agent)

    cands = {c["login_id"]: c for c in card_grant.candidates(con)}
    assert h1 in cands and h2 in cands and agent not in cands and "admin" not in cands
    assert cands[h1]["granted"] and not cands[h2]["granted"]
    assert all(c["heads"] for c in cands.values())


def test_revoke_keeps_history(con):
    h1, h2 = _heads(con)
    card_grant.grant(con, [h1, h2], by="admin")
    out = card_grant.revoke(con, [h1, h2, "nobody"], by="admin")
    assert out == {"changed": [h1, h2], "unchanged": ["nobody"]}
    assert not card_grant.is_granted(con, h1) and not card_grant.is_granted(con, h2)
    assert card_grant.granted(con) == []

    rows = con.execute("SELECT * FROM card_approver WHERE login_id=?", (h1,)).fetchall()
    assert len(rows) == 1 and rows[0]["revoked_by"] == "admin" and rows[0]["revoked_at"] and rows[0]["granted_at"]

    # 다시 켜면 새 줄이 생기고 앞 줄은 남는다
    card_grant.grant(con, [h1], by="admin")
    rows = con.execute("SELECT revoked_at FROM card_approver WHERE login_id=? ORDER BY id", (h1,)).fetchall()
    assert len(rows) == 2 and rows[0]["revoked_at"] and rows[1]["revoked_at"] is None
    assert card_grant.is_granted(con, h1)

    revs = _audit(con, card_grant.ACTION_REVOKE)
    assert [r["target"] for r in revs] == [f"user:{h1}", f"user:{h2}"]
    assert json.loads(revs[0]["after_json"]) == {"card_approver": False}


def test_no_effect_after_head_ends(con):
    (login, agent_id), = _plain(con)
    ym = auth.current_ym(con)
    mid = heads.assign_head(con, _team_org(con), agent_id, ym, by="admin")
    con.commit()
    card_grant.grant(con, [login], by="admin")
    assert card_grant.is_granted(con, login)

    heads.end_head(con, mid, ym)
    con.commit()
    assert not card_grant.is_granted(con, login)
    assert login not in {g["login_id"] for g in card_grant.granted(con)}      # 빠지면 저절로 꺼진다
    row, = con.execute("SELECT revoked_by, revoked_at FROM card_approver WHERE login_id=?", (login,)).fetchall()
    assert row["revoked_by"] == card_grant.SYSTEM_LEFT and row["revoked_at"]
    assert login not in {c["login_id"] for c in card_grant.candidates(con)}
    assert card_grant.revoke(con, [login], by="admin") == {"changed": [], "unchanged": [login]}


def test_no_effect_after_deputy_period(con):
    (login, agent_id), = _plain(con)
    d0 = date(2026, 11, 14)
    heads.assign_deputy(con, _team_org(con), agent_id, "2026-11-10", "2026-11-20", "휴가", by="admin", today=d0)
    con.commit()
    with pytest.raises(card_grant.GrantError):
        card_grant.grant(con, [login], by="admin", today=date(2026, 11, 21))     # 기간 밖에는 켜지 못한다
    card_grant.grant(con, [login], by="admin", today=d0)
    assert card_grant.is_granted(con, login, today=d0)
    assert card_grant.is_granted(con, login, today=date(2026, 11, 20))
    assert not card_grant.is_granted(con, login, today=date(2026, 11, 21))
    assert not card_grant.is_granted(con, login, today=date(2026, 11, 9))


def test_no_effect_after_deputy_ended_early(con):
    (login, agent_id), = _plain(con)
    today = date.today()
    did = heads.assign_deputy(con, _team_org(con), agent_id, today.isoformat(), (today + timedelta(days=5)).isoformat(), "", by="admin")
    con.commit()
    card_grant.grant(con, [login], by="admin")
    assert card_grant.is_granted(con, login)
    heads.end_deputy(con, did, by="admin")
    con.commit()
    assert not card_grant.is_granted(con, login)


def test_context_flag(con):
    h1, h2 = _heads(con)
    (agent, _), = _plain(con)
    card_grant.grant(con, [h1], by="admin")
    ctx = {lid: auth.context(con, _uid(con, lid)) for lid in ("admin", h1, h2, agent)}
    assert ctx["admin"]["role"] == "main" and ctx["admin"]["card_approver"] is True
    assert ctx[h1]["role"] == "manager" and ctx[h1]["card_approver"] is True
    assert ctx[h2]["role"] == "manager" and ctx[h2]["card_approver"] is False
    assert ctx[agent]["role"] == "agent" and ctx[agent]["card_approver"] is False

    card_grant.revoke(con, [h1], by="admin")
    assert auth.context(con, _uid(con, h1))["card_approver"] is False

    # 중지된 계정은 켜진 줄이 있어도 거짓이다
    card_grant.grant(con, [h2], by="admin")
    con.execute("UPDATE user_account SET status='suspended' WHERE login_id=?", (h2,))
    assert not card_grant.is_granted(con, h2)
    assert [g["effective"] for g in card_grant.granted(con) if g["login_id"] == h2] == [False]


def test_old_db_without_table(con):
    con.execute("DROP TABLE card_approver")
    h1, _ = _heads(con)
    assert not card_grant.is_granted(con, h1)
    assert card_grant.granted(con) == []
    assert auth.context(con, _uid(con, h1))["card_approver"] is False
    assert card_grant.grant(con, [h1], by="admin")["changed"] == [h1]
    assert card_grant.is_granted(con, h1)


# ------------------------------------------------------------------ 승인 요청. 11월 14일 대표님 결정 "승인요청 만들어서 하는게 좋겠네"
def _requests(con, login_id, status=None):
    sql = "SELECT * FROM card_approver_request WHERE login_id=?" + (" AND status=?" if status else "") + " ORDER BY id"
    return con.execute(sql, (login_id, status) if status else (login_id,)).fetchall()


def _assign(con, agent_id, ym=None, to_ym=None):
    mid = heads.assign_head(con, _team_org(con), agent_id, ym or auth.current_ym(con), to_ym, by="admin")
    con.commit()
    return mid


def _backdate_grants(con, login_id, at="2020-01-01 00:00:00"):
    """같은 초에 켜고 다시 맡는 일을 피하려고 켠 시각과 요청 시각을 예전으로 돌린다. 예전 자리의 줄과 요청을 흉내 낸다."""
    con.execute("UPDATE card_approver SET granted_at=? WHERE login_id=?", (at, login_id))
    con.execute("UPDATE card_approver_request SET requested_at=? WHERE login_id=?", (at, login_id))
    con.commit()


def test_request_created_on_head_and_deputy_assign(con):
    (a, a_id), (b, b_id) = _plain(con, 2)
    _assign(con, a_id)
    req, = _requests(con, a)
    assert req["status"] == "pending" and req["reason"] == "new_head" and req["requested_by"] == card_grant.SYSTEM_AUTO
    assert req["agent_id"] == a_id and req["requested_at"]
    assert not card_grant.is_granted(con, a)                           # 요청만 생기고 권한은 꺼져 있다
    assert [r["target"] for r in _audit(con, card_grant.ACTION_REQUEST)] == [f"user:{a}"]

    today = date.today()
    heads.assign_deputy(con, _team_org(con), b_id, today.isoformat(), (today + timedelta(days=3)).isoformat(), "", by="admin")
    con.commit()
    assert [r["reason"] for r in _requests(con, b, "pending")] == ["new_head"]

    # 앞으로 시작할 지정은 지금 관리자가 아니라 요청이 없다
    (c, c_id), = _plain(con, 1)                                        # a 와 b 는 이제 조직장과 대리라 빠진다
    from app.periods import ym_add
    _assign(con, c_id, ym_add(auth.current_ym(con), 2))
    assert _requests(con, c) == []

    # 다시 맞춰도 늘지 않는다. 샘플 조직장의 요청은 처음 맞출 때만 생긴다
    card_grant.expire_stale(con)
    assert card_grant.expire_stale(con) == {"revoked": 0, "cancelled": 0, "requested": 0}
    assert len(_requests(con, a)) == 1 and len(_requests(con, b)) == 1
    cands = {x["login_id"]: x for x in card_grant.candidates(con)}
    assert cands[a]["request_id"] == req["id"]


def test_reappointed_request_and_grant_stays_off(con):
    (a, a_id), = _plain(con)
    mid = _assign(con, a_id)
    rid = _requests(con, a, "pending")[0]["id"]
    card_grant.approve_requests(con, [rid], by="admin")
    assert card_grant.is_granted(con, a)

    heads.end_head(con, mid, auth.current_ym(con))
    con.commit()
    assert not card_grant.is_granted(con, a)
    _backdate_grants(con, a)

    _assign(con, a_id)                                                 # 다시 맡는다
    assert not card_grant.is_granted(con, a)
    pend, = _requests(con, a, "pending")
    assert pend["reason"] == "reappointed"
    assert a not in {g["login_id"] for g in card_grant.granted(con)}

    # 다시 승인해야 켜진다
    card_grant.approve_requests(con, [pend["id"]], by="admin")
    assert card_grant.is_granted(con, a)
    assert con.execute("SELECT COUNT(*) FROM card_approver WHERE login_id=?", (a,)).fetchone()[0] == 2


def test_approve_many_at_once(con):
    people = _plain(con, 3)
    for _, aid in people:
        _assign(con, aid)
    pend = card_grant.pending_requests(con)
    mine = [p for p in pend if p["login_id"] in {x[0] for x in people}]
    assert len(mine) == 3
    assert all(p["code"] and p["orgs"] and p["orgs"][0]["text"] and p["reason"] == "new_head" and p["reason_ko"] for p in mine)
    assert all("name" in p and p["requested_at"] for p in mine)

    ids = [p["id"] for p in mine]
    out = card_grant.approve_requests(con, ids + [ids[0], 999999], by="admin")
    assert out["approved"] == ids and [s["id"] for s in out["skipped"]] == [999999]
    for lid, _ in people:
        assert card_grant.is_granted(con, lid)
        r, = _requests(con, lid)
        assert r["status"] == "approved" and r["decided_by"] == "admin" and r["decided_at"] and r["grant_id"]
    assert {p["login_id"] for p in card_grant.pending_requests(con)}.isdisjoint({x[0] for x in people})

    grants = [json.loads(r["after_json"]) for r in _audit(con, card_grant.ACTION_GRANT)]
    assert len(grants) == 3 and all(g["how"] == card_grant.HOW_REQUEST and g["request_id"] in ids for g in grants)

    # 이미 처리한 요청은 건너뛴다
    again = card_grant.approve_requests(con, ids[:1], by="admin")
    assert again["approved"] == [] and again["skipped"][0]["id"] == ids[0]


def test_reject(con):
    (a, a_id), (b, b_id) = _plain(con, 2)
    _assign(con, a_id)
    _assign(con, b_id)
    ra, rb = _requests(con, a)[0]["id"], _requests(con, b)[0]["id"]
    out = card_grant.reject_requests(con, [ra, rb], by="admin")
    assert out == {"rejected": [ra, rb], "skipped": []}
    assert not card_grant.is_granted(con, a) and not card_grant.is_granted(con, b)
    r, = _requests(con, a)
    assert r["status"] == "rejected" and r["decided_by"] == "admin" and r["grant_id"] is None
    assert len(_audit(con, card_grant.ACTION_REJECT)) == 2

    # 반려한 뒤에는 같은 자리에서 저절로 다시 생기지 않는다. 본인은 다시 요청할 수 있다
    card_grant.expire_stale(con)
    assert _requests(con, a, "pending") == [] and _requests(con, b, "pending") == []
    out = card_grant.request(con, a, by=a)
    assert out["created"] and out["reason"] == "self"
    assert card_grant.approve_requests(con, [ra], by="admin")["skipped"][0]["id"] == ra   # 반려한 요청은 승인하지 못한다


def test_self_request_one_pending(con):
    h1, h2 = _heads(con)                                               # 샘플 조직장. 아직 맞추기 전이라 요청이 없다
    (agent, _), = _plain(con)
    out = card_grant.request(con, h1, by=h1)
    assert out["created"] and out["reason"] == "self"
    again = card_grant.request(con, h1, by=h1)
    assert again == {"id": out["id"], "created": False, "reason": "self"}
    assert len(_requests(con, h1, "pending")) == 1
    r, = _requests(con, h1)
    assert r["requested_by"] == h1
    # 맞춰도 pending 은 하나다
    card_grant.expire_stale(con)
    assert len(_requests(con, h1, "pending")) == 1
    # 표가 같은 사람의 pending 둘을 막는다
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("""INSERT INTO card_approver_request(login_id, agent_id, reason, status, requested_at, requested_by)
                       VALUES(?, NULL, 'self', 'pending', '2026-01-01 00:00:00', ?)""", (h1, h1))
    con.rollback()

    # 남의 이름으로는 요청하지 못하고 기록에 남는다
    with pytest.raises(card_grant.GrantError):
        card_grant.request(con, h2, by=h1)
    assert any(r["target"] == f"request:{h2}" for r in _audit(con, card_grant.ACTION_DENIED))
    # 설계사, 메인 관리자는 요청하지 못한다
    for who in (agent, "admin"):
        with pytest.raises(card_grant.GrantError):
            card_grant.request(con, who, by=who)
    # 이미 켜져 있으면 요청하지 못한다
    card_grant.approve_requests(con, [out["id"]], by="admin")
    with pytest.raises(card_grant.GrantError):
        card_grant.request(con, h1, by=h1)


def test_manager_cannot_approve_or_reject(con):
    h1, h2 = _heads(con)
    (a, a_id), = _plain(con)
    _assign(con, a_id)
    rid = _requests(con, a)[0]["id"]
    card_grant.grant(con, [h1], by="admin")                            # 권한을 받은 관리자도 요청은 처리하지 못한다
    for by in (h1, h2, a, "nobody"):
        with pytest.raises(card_grant.GrantError):
            card_grant.approve_requests(con, [rid], by=by)
        with pytest.raises(card_grant.GrantError):
            card_grant.reject_requests(con, [rid], by=by)
    assert _requests(con, a)[0]["status"] == "pending" and not card_grant.is_granted(con, a)
    ops = [json.loads(r["after_json"])["op"] for r in _audit(con, card_grant.ACTION_DENIED)]
    assert ops.count("approve") == 4 and ops.count("reject") == 4
    with pytest.raises(card_grant.GrantError):
        card_grant.approve_requests(con, [], by="admin")


def test_direct_grant_is_marked_and_closes_pending(con):
    (a, a_id), = _plain(con)
    _assign(con, a_id)
    rid = _requests(con, a)[0]["id"]
    card_grant.grant(con, [a], by="admin")
    g, = _audit(con, card_grant.ACTION_GRANT)
    assert json.loads(g["after_json"]) == {"card_approver": True, "how": card_grant.HOW_DIRECT}
    r, = _requests(con, a)
    assert r["id"] == rid and r["status"] == "approved" and r["grant_id"]

    # 메인 관리자가 끈 뒤에는 같은 자리에서 요청이 저절로 생기지 않는다
    card_grant.revoke(con, [a], by="admin")
    card_grant.expire_stale(con)
    assert _requests(con, a, "pending") == []


def test_leaving_turns_off(con):
    # 대리 기간이 끝나면 따로 일이 없어도 expire_stale 이 끈다
    (a, a_id), (b, b_id), (c, c_id) = _plain(con, 3)
    d0 = date(2026, 11, 14)
    heads.assign_deputy(con, _team_org(con), a_id, "2026-11-10", "2026-11-20", "휴가", by="admin", today=d0)
    con.commit()
    card_grant.approve_requests(con, [_requests(con, a)[0]["id"]], by="admin", today=d0)
    assert card_grant.is_granted(con, a, today=d0)
    out = card_grant.expire_stale(con, today=date(2026, 11, 21))
    assert out["revoked"] == 1
    row, = con.execute("SELECT revoked_by FROM card_approver WHERE login_id=?", (a,)).fetchall()
    assert row["revoked_by"] == card_grant.SYSTEM_LEFT
    left = [r for r in _audit(con, card_grant.ACTION_REVOKE) if r["target"] == f"user:{a}"]
    assert left[0]["actor"] == card_grant.SYSTEM_LEFT and json.loads(left[0]["after_json"])["how"] == card_grant.HOW_LEFT

    # 끝 달이 지나면 로그인 때 끈다. 자료 기준 달이 넘어가는 것으로 흉내 낸다
    ym = auth.current_ym(con)
    _assign(con, b_id, ym, ym)
    card_grant.approve_requests(con, [_requests(con, b)[0]["id"]], by="admin")
    assert card_grant.is_granted(con, b)
    from app.periods import ym_add
    con.execute("INSERT INTO calc_run(run_type, period_ym, started_at, status) VALUES('commission', ?, ?, 'done')", (ym_add(ym, 1), db.now()))
    con.commit()
    assert not card_grant.is_granted(con, b)
    u, _ = auth.login(con, "admin", "grant-pass-1234")
    assert u is not None
    assert con.execute("SELECT revoked_by FROM card_approver WHERE login_id=?", (b,)).fetchone()[0] == card_grant.SYSTEM_LEFT

    # 지정을 끝내면 승인 대기 요청은 거둔다
    mid = _assign(con, c_id, ym_add(ym, 1))
    assert _requests(con, c, "pending")
    heads.end_head(con, mid, ym_add(ym, 1))
    con.commit()
    r, = _requests(con, c)
    assert r["status"] == "cancelled" and r["decided_by"] == card_grant.SYSTEM_LEFT
    assert _audit(con, card_grant.ACTION_CANCEL)


def test_context_settles_own_account(con):
    (a, a_id), = _plain(con)
    mid = _assign(con, a_id)
    card_grant.approve_requests(con, [_requests(con, a)[0]["id"]], by="admin")
    con.execute("UPDATE org_manager SET effective_to='200001' WHERE id=?", (mid,))   # 끝내는 일을 거치지 않고 빠진다
    con.commit()
    ctx = auth.context(con, _uid(con, a))
    assert ctx["role"] == "agent" and ctx["card_approver"] is False
    assert con.execute("SELECT revoked_by FROM card_approver WHERE login_id=?", (a,)).fetchone()[0] == card_grant.SYSTEM_LEFT


def test_old_row_does_not_revive(con):
    (a, a_id), = _plain(con)
    mid = _assign(con, a_id)
    card_grant.approve_requests(con, [_requests(con, a)[0]["id"]], by="admin")
    _backdate_grants(con, a)
    # 맞추는 일 없이 빠졌다가 다시 맡은 경우. 켜진 줄이 그대로 남아 있다
    con.execute("UPDATE org_manager SET effective_to='200001' WHERE id=?", (mid,))
    con.execute("""INSERT INTO org_manager(org_id, agent_id, role, effective_from, source, created_by, created_at)
                   VALUES(?,?,'team_head',?,'manual','admin',?)""", (_team_org(con), a_id, auth.current_ym(con), db.now()))
    con.commit()
    assert con.execute("SELECT 1 FROM card_approver WHERE login_id=? AND revoked_at IS NULL", (a,)).fetchone()
    assert not card_grant.is_granted(con, a)                           # 예전 자리에서 켠 줄이라 효력이 없다
    assert [g["effective"] for g in card_grant.granted(con) if g["login_id"] == a] == [False]
    assert auth.context(con, _uid(con, a))["card_approver"] is False

    # 맞추면 예전 줄을 끄고 다시 맡음 요청을 만든다
    card_grant.expire_stale(con)
    assert con.execute("SELECT 1 FROM card_approver WHERE login_id=? AND revoked_at IS NULL", (a,)).fetchone() is None
    assert [r["reason"] for r in _requests(con, a, "pending")] == ["reappointed"]

    # 직접 켤 때도 예전 줄을 그대로 쓰지 않고 새 줄을 만든다
    (b, b_id), = _plain(con, 1)
    mid = _assign(con, b_id)
    card_grant.grant(con, [b], by="admin")
    _backdate_grants(con, b)
    con.execute("UPDATE org_manager SET created_at=? WHERE id=?", (db.now(), mid))   # 자리를 그 뒤에 다시 만든 것으로 둔다
    con.commit()
    assert not card_grant.is_granted(con, b)
    assert card_grant.grant(con, [b], by="admin")["changed"] == [b]
    assert card_grant.is_granted(con, b)


def test_sync_creates_requests_and_turns_off(tmp_path):
    from app.loader import eternal_employee as ee
    from app.loader import fake_eternal as fe
    from app.org import sync
    c = db.init_db(tmp_path / "s.db")
    emp_path, _ = fe.make_employee_file(tmp_path / "emp.xlsx", fe.fake_roster())
    rows = ee.parse_employee_file(emp_path)["rows"]
    first = sync.sync_staff(c, rows, "202602")
    assert first["card"]["requested"] == 0                             # 계정이 없으면 요청도 없다
    auth.seed_accounts(c, "grant-pass-1234")
    dry = sync.sync_staff(c, rows, "202602", dry_run=True)
    assert dry["card"]["requested"] > 0
    assert c.execute("SELECT COUNT(*) FROM card_approver_request").fetchone()[0] == 0   # 미리보기는 되돌린다
    real = sync.sync_staff(c, rows, "202602")
    assert real["card"]["requested"] == dry["card"]["requested"]
    pend = c.execute("SELECT id, login_id FROM card_approver_request WHERE status='pending' ORDER BY id").fetchall()
    assert len(pend) == real["card"]["requested"] and all(r["login_id"] for r in pend)
    assert sync.sync_staff(c, rows, "202602")["card"] == {"revoked": 0, "cancelled": 0, "requested": 0}

    card_grant.approve_requests(c, [r["id"] for r in pend], by="admin")
    target = pend[0]["login_id"]
    assert card_grant.is_granted(c, target)
    later = [dict(r, position="FC") if r["employee_no"] == target else r for r in rows]
    out = sync.sync_staff(c, later, "202603")
    assert out["card"]["revoked"] >= 1
    assert not card_grant.is_granted(c, target)
    assert c.execute("SELECT revoked_by FROM card_approver WHERE login_id=?", (target,)).fetchone()[0] == card_grant.SYSTEM_LEFT
    c.close()


# ------------------------------------------------------------------ 옮기거나 더 맡으면 새로 요청. 11월 14일 대표님 결정 "옮겨도 권한은 새로 요청받게 해"
OLD_SEAT, OLD_GRANT = "2020-01-01 00:00:00", "2024-06-01 00:00:00"


def _teams(con, n):
    """샘플 팀과 같은 센터 아래의 시험용 팀까지 n 개."""
    from app.org import sync
    first = con.execute("SELECT org_id, path_key FROM org_unit WHERE level='team' ORDER BY org_id LIMIT 1").fetchone()
    steps = [tuple(s.split(":", 1)) for s in first["path_key"].split(" > ")]
    out = [first["org_id"]] + [sync.ensure_path(con, steps[:-1] + [("team", f"시험{i}팀")]) for i in range(2, n + 1)]
    con.commit()
    return out


def _age(con, agent_ids, login_ids):
    """지금 자리와 켠 줄을 예전 일로 돌린다. 자리는 2024년 1월부터, 줄과 요청은 그 뒤인 2024년 6월로 둔다.
    같은 초에 켜고 옮기는 일을 피하고, 옮기기 전에는 효력이 있도록 하려는 것이다."""
    for aid in agent_ids:
        con.execute("UPDATE org_manager SET effective_from='202401', created_at=? WHERE agent_id=? AND effective_to IS NULL", (OLD_SEAT, aid))
    for lid in login_ids:
        _backdate_grants(con, lid, OLD_GRANT)
    con.commit()


def _granted_head(con, agent_id, login, org_id):
    """org_id 의 팀장으로 지정하고 요청을 승인한 뒤 예전 일로 돌린다. 지정 번호를 돌려준다."""
    mid = heads.assign_head(con, org_id, agent_id, auth.current_ym(con), by="admin")
    con.commit()
    card_grant.approve_requests(con, [_requests(con, login, "pending")[0]["id"]], by="admin")
    _age(con, [agent_id], [login])
    assert card_grant.is_granted(con, login)
    return mid


def _advance(con, ym):
    """자료 기준 달을 ym 으로 넘긴다."""
    con.execute("INSERT INTO calc_run(run_type, period_ym, started_at, status) VALUES('commission', ?, ?, 'done')", (ym, db.now()))
    con.commit()


def _off_and_reappointed(con, login):
    """권한이 꺼지고, 켜진 줄은 system:left 로 꺼지고, 다시 맡음 요청이 하나 생겼는가."""
    assert not card_grant.is_granted(con, login)
    assert not [g for g in card_grant.granted(con) if g["login_id"] == login]
    assert [r["revoked_by"] for r in con.execute("SELECT revoked_by FROM card_approver WHERE login_id=? ORDER BY id", (login,))] \
        == [card_grant.SYSTEM_LEFT]
    pend, = _requests(con, login, "pending")
    assert pend["reason"] == "reappointed" and pend["requested_by"] == card_grant.SYSTEM_AUTO
    return pend


def _still_on(con, login):
    assert card_grant.is_granted(con, login)
    assert _requests(con, login, "pending") == []
    assert con.execute("SELECT COUNT(*) FROM card_approver WHERE login_id=? AND revoked_at IS NULL", (login,)).fetchone()[0] == 1


@pytest.mark.parametrize("when", ["same_month", "next_month"])
def test_move_by_screen_needs_new_request(con, when):
    from app.periods import ym_add
    (a, a_id), (b, b_id) = _plain(con, 2)
    x, y, z = _teams(con, 3)
    ym = auth.current_ym(con)
    mid_x = _granted_head(con, a_id, a, x)
    _granted_head(con, b_id, b, z)                                     # 옮기지 않는 사람

    start = ym if when == "same_month" else ym_add(ym, 1)
    heads.end_head(con, mid_x, start)                                  # 같은 날 X 에서 해제하고 Y 의 팀장으로 지정한다
    heads.assign_head(con, y, a_id, start, by="admin")
    con.commit()
    if when == "next_month":
        _still_on(con, a)                                              # 옮기기 전 달에는 X 의 팀장이라 그대로 켜져 있다
        assert [h["org_id"] for h in card_grant._heads_now(con, a_id)] == [x]
        _advance(con, start)
        assert not card_grant.is_granted(con, a)                       # 달이 넘어가면 맞추기 전에도 효력이 없다
        card_grant.expire_stale(con)
    assert [h["org_id"] for h in card_grant._heads_now(con, a_id)] == [y]
    pend = _off_and_reappointed(con, a)
    _still_on(con, b)

    # 새 요청을 승인해야 다시 켜진다. 다음 달 경우는 자료 기준 달이 시계보다 앞서 있어 같은 달에서만 본다
    if when == "same_month":
        card_grant.approve_requests(con, [pend["id"]], by="admin")
        assert card_grant.is_granted(con, a)
        _still_on(con, b)


@pytest.mark.parametrize("when", ["same_month", "next_month"])
def test_move_by_staff_sync_needs_new_request(tmp_path, when):
    from app.loader import eternal_employee as ee
    from app.loader import fake_eternal as fe
    from app.org import sync
    from app.periods import ym_add
    c = db.init_db(tmp_path / "s.db")
    emp_path, _ = fe.make_employee_file(tmp_path / "emp.xlsx", fe.fake_roster())
    rows = ee.parse_employee_file(emp_path)["rows"]
    ym = auth.current_ym(c)
    sync.sync_staff(c, rows, ym)
    auth.seed_accounts(c, "grant-pass-1234")                           # 설계사가 생긴 뒤에 계정을 만든다
    sync.sync_staff(c, rows, ym)
    pend = c.execute("SELECT id FROM card_approver_request WHERE status='pending'").fetchall()
    card_grant.approve_requests(c, [r["id"] for r in pend], by="admin")

    # 센터 없이 지점에 바로 있는 지점장 a 를 다른 지점으로 옮긴다. 나머지 지점장은 그대로 둔다
    bosses = [r for r in rows if r["position"] != "FC"]
    mover = next(r for r in bosses if not r["org"]["직할지점"])
    stay = [r["employee_no"] for r in bosses if r is not mover]
    assert stay
    dest = next(r["org"] for r in rows if r["org"]["지점"] != mover["org"]["지점"] and not r["org"]["직할지점"])
    a = mover["employee_no"]
    a_id = c.execute("SELECT agent_id FROM user_account WHERE login_id=?", (a,)).fetchone()[0]
    stay_ids = [c.execute("SELECT agent_id FROM user_account WHERE login_id=?", (s,)).fetchone()[0] for s in stay]
    _age(c, [a_id, *stay_ids], [a, *stay])
    for lid in (a, *stay):
        _still_on(c, lid)
    old_org = card_grant._heads_now(c, a_id)[0]["org_id"]

    start = ym if when == "same_month" else ym_add(ym, 1)
    later = [dict(r, org=dict(dest)) if r is mover else r for r in rows]
    out = sync.sync_staff(c, later, start)
    assert out["stats"]["heads_end"] == 1 and out["stats"]["heads_new"] == 1
    if when == "next_month":
        assert out["card"] == {"revoked": 0, "cancelled": 0, "requested": 0}
        _still_on(c, a)
        _advance(c, start)
        assert not card_grant.is_granted(c, a)
        card_grant.expire_stale(c)
    else:
        assert out["card"]["revoked"] == 1 and out["card"]["requested"] == 1
    seat, = card_grant._heads_now(c, a_id)
    assert seat["org_id"] != old_org and seat["role"] == "branch_head"
    _off_and_reappointed(c, a)
    for lid in stay:
        _still_on(c, lid)
    c.close()


@pytest.mark.parametrize("extra", ["head", "deputy"])
def test_taking_one_more_seat_needs_new_request(con, extra):
    (a, a_id), (b, b_id) = _plain(con, 2)
    x, y, z = _teams(con, 3)
    _granted_head(con, a_id, a, x)
    _granted_head(con, b_id, b, z)

    # X 의 팀장을 그대로 맡은 채 Y 를 하나 더 맡는다. 범위가 넓어지므로 새 요청을 거친다
    today = date.today()
    if extra == "head":
        heads.assign_head(con, y, a_id, auth.current_ym(con), by="admin")
    else:
        heads.assign_deputy(con, y, a_id, today.isoformat(), (today + timedelta(days=5)).isoformat(), "휴가", by="admin")
    con.commit()
    assert sorted(h["org_id"] for h in card_grant._heads_now(con, a_id)) == sorted([x, y])
    pend = _off_and_reappointed(con, a)
    _still_on(con, b)
    card_grant.expire_stale(con)
    assert [r["id"] for r in _requests(con, a, "pending")] == [pend["id"]]   # 다시 맞춰도 요청은 하나다

    card_grant.approve_requests(con, [pend["id"]], by="admin")
    assert card_grant.is_granted(con, a)


def test_staying_in_same_seat_keeps_grant(con):
    from app.periods import ym_add
    (a, a_id), = _plain(con)
    x, = _teams(con, 1)
    _granted_head(con, a_id, a, x)
    h1, _ = _heads(con)                                                 # 샘플 조직장도 직접 켠 뒤 예전 일로 돌린다
    h1_id = con.execute("SELECT agent_id FROM user_account WHERE login_id=?", (h1,)).fetchone()[0]
    card_grant.grant(con, [h1], by="admin")
    _age(con, [h1_id], [h1])

    for lid in (a, h1):
        _still_on(con, lid)
    card_grant.expire_stale(con)                                       # 다른 샘플 조직장의 요청만 생긴다
    for lid in (a, h1):
        _still_on(con, lid)
    # 자료 기준 달이 넘어가도, 로그인해도, 화면을 열어도 그대로다
    _advance(con, ym_add(auth.current_ym(con), 1))
    auth.login(con, "admin", "grant-pass-1234")
    assert auth.context(con, _uid(con, a))["card_approver"] is True
    for lid in (a, h1):
        _still_on(con, lid)
    assert not [r for r in _audit(con, card_grant.ACTION_REVOKE) if r["target"] in (f"user:{a}", f"user:{h1}")]
