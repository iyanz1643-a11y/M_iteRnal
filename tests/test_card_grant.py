"""명함 승인 권한을 켜고 끄는 시험. D-44 를 고친 11월 14일 결정.

권한은 메인 관리자가 지금 조직장이거나 대리인 관리자에게 골라서 켠다. 조직장에서 빠지거나 대리 기간이 끝나면 효력이 없다.
"""
import json
import pathlib
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
    g, = [g for g in card_grant.granted(con) if g["login_id"] == login]
    assert g["effective"] is False                       # 줄은 남아 있지만 효력이 없다
    assert login not in {c["login_id"] for c in card_grant.candidates(con)}

    # 조직장에서 빠진 뒤에도 끌 수는 있다
    assert card_grant.revoke(con, [login], by="admin")["changed"] == [login]


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
