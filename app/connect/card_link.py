"""설계사와 명함 주소를 잇는 표 card_link. CardLinkStore 의 DB 구현이다.

- 잇기와 끊기는 앱 안의 기록이고 메인 관리자만 한다 (D-28). 명함 사이트에는 아무것도 보내지 않는다.
- 이름 대조 제안(CardSiteClient.suggest_links)은 이 표에 넣지 않는다. open_suggestions 로 확인 화면에 보이고,
  메인 관리자가 고른 것만 confirm_suggestion 으로 들어온다. 제안은 화면을 열 때마다 다시 읽는다.
- 이름과 연락처는 이 표에 없다. 명함 사이트의 관리자 목록에 있어도 옮기지 않는다.
- 끊을 때는 줄을 지우지 않고 unlinked_at 을 채운다. 이어진 줄은 주소마다 하나, 설계사마다 하나다. DB 의 색인도 같은 것을 막는다.
- 잇기와 끊기는 audit_log 에 남긴다. 대상은 agent_id 와 주소뿐이다.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from app import db
from app.connect import tables
from app.connect.base import OK
from app.connect.card_site import SLUG_RE, STATES, CardEntry, CardStatus, LinkSuggestion

VIA_MANUAL = "manual"
VIA_SUGGEST = "suggest"

_COLS = ("id", "agent_id", "slug", "linked_via", "linked_by", "linked_at", "last_status", "last_live", "last_live_at",
         "last_checked_at", "unlinked_by", "unlinked_at", "unlink_reason")


class LinkError(ValueError):
    """잇거나 끊을 수 없다. 사유는 화면에 그대로 보여도 되는 말이다."""


def norm_slug(slug) -> str:
    return str(slug or "").strip().lower()


def _row(r) -> dict:
    d = {k: r[k] for k in _COLS}
    d["last_live"] = None if d["last_live"] is None else bool(d["last_live"])
    return d


class CardLinkTable:
    """card_link 표. con 은 앱 DB 연결이다.

    require_main_admin 이 참이면 잇기와 끊기를 한 사람(by, login_id)이 메인 관리자인지 user_account 에서 한 번 더 본다.
    화면의 권한 검사는 app/web 의 guard 가 하고, 이 검사는 그 뒤의 두 번째 잠금이다."""

    def __init__(self, con, *, require_main_admin: bool = True):
        self.con = con
        self.require_main_admin = require_main_admin
        tables.ensure(con)

    # ---------------------------------------------------------- 읽기
    def slug_for(self, agent_id) -> str | None:
        r = self.con.execute("SELECT slug FROM card_link WHERE agent_id=? AND unlinked_at IS NULL", (agent_id,)).fetchone()
        return r[0] if r else None

    def agent_for(self, slug: str):
        r = self.con.execute("SELECT agent_id FROM card_link WHERE slug=? AND unlinked_at IS NULL", (norm_slug(slug),)).fetchone()
        return r[0] if r else None

    def get(self, agent_id) -> dict | None:
        r = self.con.execute("SELECT * FROM card_link WHERE agent_id=? AND unlinked_at IS NULL", (agent_id,)).fetchone()
        return _row(r) if r else None

    def links(self, agent_ids: Iterable | None = None) -> list:
        """이어진 줄. agent_ids 를 주면 그 설계사만 본다. 화면은 범위 안의 설계사 목록을 넘긴다."""
        rows = self.con.execute("SELECT * FROM card_link WHERE unlinked_at IS NULL ORDER BY agent_id").fetchall()
        if agent_ids is not None:
            keep = set(agent_ids)
            rows = [r for r in rows if r["agent_id"] in keep]
        return [_row(r) for r in rows]

    def history(self, agent_id) -> list:
        return [_row(r) for r in self.con.execute("SELECT * FROM card_link WHERE agent_id=? ORDER BY id", (agent_id,))]

    # ---------------------------------------------------------- 제안과 확정
    def open_suggestions(self, suggestions: Iterable[LinkSuggestion]) -> list:
        """확인 화면에 보일 제안. 이미 이어진 주소는 빼고, 이미 다른 주소와 이어진 설계사도 후보에서 뺀다."""
        linked_slugs = {r[0] for r in self.con.execute("SELECT slug FROM card_link WHERE unlinked_at IS NULL")}
        linked_agents = {r[0] for r in self.con.execute("SELECT agent_id FROM card_link WHERE unlinked_at IS NULL")}
        out = []
        for s in suggestions:
            if s.slug in linked_slugs:
                continue
            ids = tuple(a for a in s.agent_ids if a not in linked_agents)
            if ids:
                out.append(LinkSuggestion(slug=s.slug, agent_ids=ids, live=s.live, request_status=s.request_status))
        return out

    def confirm_suggestion(self, suggestion: LinkSuggestion, agent_id, by: str) -> dict:
        """메인 관리자가 제안 하나를 확인해 잇는다. 고른 설계사는 제안의 후보 안에 있어야 한다."""
        if agent_id not in suggestion.agent_ids:
            raise LinkError("제안의 후보가 아닌 설계사입니다")
        return self.link(agent_id, suggestion.slug, by, via=VIA_SUGGEST)

    # ---------------------------------------------------------- 잇기와 끊기
    def _check_actor(self, by: str) -> None:
        if not str(by or "").strip():
            raise LinkError("이은 사람이 없습니다")
        if not self.require_main_admin:
            return
        r = self.con.execute("SELECT 1 FROM user_account WHERE login_id=? AND is_main_admin=1 AND status='active'", (by,)).fetchone()
        if r is None:
            raise LinkError("명함 주소는 메인 관리자만 잇고 끊을 수 있습니다")

    def _audit(self, by, action, agent_id, before, after) -> None:
        self.con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                         (db.now(), by, action, f"agent:{agent_id}",
                          json.dumps(before, ensure_ascii=False) if before else None,
                          json.dumps(after, ensure_ascii=False) if after else None))

    def link(self, agent_id, slug: str, by: str, *, via: str = VIA_MANUAL) -> dict:
        """설계사 하나에 주소 하나를 잇는다. 같은 짝이면 그대로 둔다. 그 설계사에게 다른 주소가 있으면 끊고 새로 잇는다.
        다른 설계사에게 이어진 주소면 LinkError."""
        slug = norm_slug(slug)
        if not SLUG_RE.match(slug):
            raise LinkError("명함 주소 꼴이 아닙니다. 영문 소문자, 숫자, 하이픈 2자에서 30자입니다")
        if via not in (VIA_MANUAL, VIA_SUGGEST):
            raise LinkError("잇는 길은 manual 이나 suggest 입니다")
        self._check_actor(by)
        if self.con.execute("SELECT 1 FROM agent WHERE agent_id=?", (agent_id,)).fetchone() is None:
            raise LinkError("없는 설계사입니다")
        other = self.agent_for(slug)
        if other is not None and other != agent_id:
            raise LinkError("이미 다른 설계사와 이어진 주소입니다")
        cur = self.get(agent_id)
        if cur and cur["slug"] == slug:
            return cur
        now = db.now()
        try:
            with tables.write(self.con, "card_link"):
                if cur:
                    self.con.execute("UPDATE card_link SET unlinked_at=?, unlinked_by=?, unlink_reason=? WHERE id=?",
                                     (now, by, "다른 주소로 바꿈", cur["id"]))
                self.con.execute("INSERT INTO card_link(agent_id, slug, linked_via, linked_by, linked_at) VALUES(?,?,?,?,?)",
                                 (agent_id, slug, via, by, now))
                self._audit(by, "card_link", agent_id, {"slug": cur["slug"]} if cur else None, {"slug": slug, "via": via})
        except sqlite3.IntegrityError:
            raise LinkError("이미 다른 설계사와 이어진 주소입니다") from None
        return self.get(agent_id)

    def unlink(self, agent_id, by: str, reason: str = "") -> None:
        self._check_actor(by)
        cur = self.get(agent_id)
        if not cur:
            return
        with tables.write(self.con, "card_link"):
            self.con.execute("UPDATE card_link SET unlinked_at=?, unlinked_by=?, unlink_reason=? WHERE id=?",
                             (db.now(), by, str(reason or "")[:200], cur["id"]))
            self._audit(by, "card_unlink", agent_id, {"slug": cur["slug"]}, None)

    # ---------------------------------------------------------- 마지막으로 확인한 상태
    def record_check(self, slug: str, card, at: str | None = None) -> bool:
        """명함 사이트에서 읽은 상태를 이어진 줄에 적는다. card 는 CardStatus 나 CardEntry. 이어진 줄이 없으면 False."""
        if isinstance(card, CardStatus):
            status, live, live_at = card.status, card.live, card.live_at
        elif isinstance(card, CardEntry):
            status, live, live_at = card.request_status, card.live, card.live_at
        else:
            raise TypeError("CardStatus 나 CardEntry 를 넘겨 주세요")
        status = status if status in STATES else "none"
        with tables.write(self.con, "card_link"):
            n = self.con.execute(
                "UPDATE card_link SET last_status=?, last_live=?, last_live_at=?, last_checked_at=? WHERE slug=? AND unlinked_at IS NULL",
                (status, int(bool(live)), live_at, at or db.now(), norm_slug(slug))).rowcount
        return n > 0

    def record_entries(self, entries: Iterable[CardEntry]) -> int:
        """관리자 목록으로 이어진 줄의 상태를 한꺼번에 적는다. 돌려주는 값은 적은 줄 수."""
        at = db.now()
        return sum(1 for e in entries if self.record_check(e.slug, e, at=at))

    def refresh(self, client, agent_ids: Iterable | None = None) -> dict:
        """이어진 주소의 공개 상태를 하나씩 읽어 적는다. 실패한 주소는 지난 값을 그대로 둔다. GET 만 보낸다."""
        counts = {"checked": 0, "failed": 0}
        for link in self.links(agent_ids):
            out = client.status(link["slug"])
            if out.status == OK and out.value is not None:
                self.record_check(link["slug"], out.value)
                counts["checked"] += 1
            else:
                counts["failed"] += 1
        return counts

