"""7나이츠 심사. 사람이 읽는 생산물을 심사하는 일곱 본능 심사단이다. 금액과 선발에는 손대지 않는다.

루프는 docs/13 2.4 를 따른다.
1. 올리기: 심사할 글과 받는 사람(audience)을 받는다. 연락처나 주민번호 꼴이 있으면 받지 않는다.
2. 자동 점검: AI 가 만든 초안이면 숫자가 입력에 있는지 본다. 맞지 않으면 심사하지 않고 멈춘다.
3. 심사: 회전마다 다빈치의 형상화가 먼저 나오고, 일곱 자리를 따로 부른다. 첫 회전에는 다른 자리의 의견을 보여 주지 않는다.
   둘째 회전부터는 앞 회전의 의견을 함께 준다. 테미스에게는 금지 표현과 준수 항목을 빠짐없이 넣는다. 회전은 세 번까지다.
   고친 문안은 새 숫자와 금지 표현이 없을 때만 받고, 받은 문안으로 다음 회전을 돈다.
4. 결재: 메인 관리자가 승인하거나 반려한다. D-15 제안대로 고객에게 나가는 글만 심사 통과가 필수 관문이다.
   고객에게 나가는 글을 사람이 고쳤으면 고친 글로 다시 심사에 올린다.
5. 기록: ai_review 에 입력 해시, 지침 버전, 심사단 버전, 처음 글, 마지막 글, 사람이 확정한 글. ai_review_opinion 에 자리별 의견.

심사단 명세: docs 에는 다빈치의 형상화와 테미스의 금지 표현과 준수 항목만 적혀 있다. 나머지 여섯 자리의 이름과 역할은
대표님의 노션에 있고 아직 받지 않았다. 지어내지 않고 자리만 둔다. 역할이 없는 자리는 부르지 않고 결과에 명세 대기로 보인다.
명세를 받으면 AI_KNIGHTS_FILE 에 JSON 이나 YAML 로 두면 그것을 쓴다. 꼴은 load_panel 의 설명이다.
다빈치가 일곱 자리 밖의 형상화 단계인지 일곱 가운데 하나인지는 문서로 알 수 없어, 따로 먼저 부르는 단계로 둔 가정이다.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from dataclasses import dataclass

from app import db
from app.ai import checks, guides, store
from app.ai import context as aictx
from app.ai.client import AIClient

FEATURE = "review"
MAX_ROUNDS = 3
AUDIENCES = {"customer": "고객", "agent": "설계사", "internal": "내부"}
KINDS = {"cardnews": "카드뉴스", "report_form": "제안 리포트 서식", "notice": "안내문", "standard_text": "기준 문안", "landing": "랜딩 문구",
         "report": "보장분석 리포트", "monthly_report": "월간 보고서"}
GATE = "고객에게 나가는 글은 7나이츠 심사를 통과해야 승인할 수 있습니다."
REEDIT = "고객에게 나가는 글을 고쳤으면 고친 글로 다시 심사에 올려 주세요."
PII_FOUND = "심사할 글에 연락처나 주민번호 꼴이 있습니다. 빼고 올려 주세요."
STOP_KO = {"rounds": "3회전 안에 모든 자리가 통과하지 않았습니다", "reject": "반려 의견이 있어 사람이 보아야 합니다",
           "number": "입력에 없는 숫자가 있습니다", "off": "AI 기능이 꺼져 있습니다", "budget": "이번 달 AI 사용 한도에 닿았습니다",
           "error": "AI 를 부르지 못했습니다"}


@dataclass
class Seat:
    key: str
    name: str
    instinct: str | None
    role: str | None

    @property
    def defined(self) -> bool:
        return bool(self.role)


# docs/13 1.4 와 docs/09 4.11 에 적힌 것만 채웠다.
DEFAULT_PANEL = {
    "version": "panel@2026-11-15.docs",
    "shape": Seat("davinci", "다빈치", "형상화", "심사할 글이 읽는 사람에게 어떤 모습으로 그려지는지 형상화한다"),
    "seats": [Seat("themis", "테미스", None, "금지 표현과 준수 항목을 빠짐없이 대조한다")]
    + [Seat(f"seat{i}", f"자리 {i}", None, None) for i in range(2, 8)],
}


def load_panel(path: str | None = None) -> dict:
    """심사단 명세. AI_KNIGHTS_FILE 이 있으면 읽는다.

    꼴: {"version": "...", "shape": {"name": "다빈치", "role": "..."},
         "seats": [{"key": "themis", "name": "테미스", "instinct": "...", "role": "..."}, ... 일곱 개]}
    """
    path = path or os.environ.get("AI_KNIGHTS_FILE")
    if not path:
        return DEFAULT_PANEL
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    sh = data.get("shape") or {}
    shape = Seat("davinci", sh.get("name") or "다빈치", sh.get("instinct") or "형상화", sh.get("role") or DEFAULT_PANEL["shape"].role)
    seats = [Seat(s.get("key") or f"seat{i}", s.get("name") or f"자리 {i}", s.get("instinct"), s.get("role"))
             for i, s in enumerate(data.get("seats") or [], start=1)]
    if len(seats) != 7:
        raise ValueError("심사단은 일곱 자리여야 합니다")
    return {"version": data.get("version") or f"panel@{hashlib.sha256(text.encode()).hexdigest()[:12]}", "shape": shape, "seats": seats}


def needs_review(audience: str) -> bool:
    """D-15 제안대로 고객에게 나가는 것만 필수 관문이다."""
    return audience == "customer"


def _themis_extra(forbidden: list) -> str:
    lines = ["<금지 표현>"] + [f"- {w}" for w in forbidden] + ["</금지 표현>", "<준수 항목>"]
    lines += [f"- {t} ({src})" for t, src in guides.COMPLIANCE] + ["</준수 항목>"]
    return "\n".join(lines)


def _role_block(seat: Seat) -> str:
    title = seat.name + (f" ({seat.instinct})" if seat.instinct else "")
    return f"<역할>\n{title}: {seat.role}\n</역할>"


def _quote_ok(quote, text) -> bool:
    if not quote:
        return True
    return re.sub(r"\s+", "", str(quote)) in re.sub(r"\s+", "", text)


def _save_opinion(con, review_id, rnd, seat: Seat, verdict, quote=None, quote_ok=True, reason=None, revised=None, call_id=None) -> int:
    oid = con.execute(
        """INSERT INTO ai_review_opinion(review_id, round_no, seat_key, seat_name, verdict, quote, quote_ok, reason, revised_text, adopted,
                                         call_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,0,?,?)""",
        (review_id, rnd, seat.key, seat.name, verdict, quote, int(bool(quote_ok)), reason, revised, call_id, db.now())).lastrowid
    con.commit()
    return oid


def _stop(con, review_id, reason, rounds, text) -> None:
    con.execute("UPDATE ai_review SET status='stopped', stop_reason=?, rounds=?, current_text=?, updated_at=? WHERE review_id=?",
                (reason, rounds, text, db.now(), review_id))
    con.commit()


def submit(con, ctx: dict, *, kind: str, audience: str, text: str, title: str | None = None, source_numbers: set | None = None,
           client: AIClient | None = None, panel: dict | None = None, parent_id: int | None = None) -> dict:
    """심사에 올리고 회전을 끝까지 돈다. 화면의 심사 요청이 부른다.

    source_numbers: AI 가 만든 초안이면 그 초안을 만든 컨텍스트의 numbers. 주면 초안의 숫자를 먼저 대조한다.
    parent_id: 고친 글로 다시 올릴 때 앞 심사의 번호.
    돌려주는 것은 get_review 의 결과다.
    """
    aictx.require_role(ctx, FEATURE)
    if kind not in KINDS:
        raise ValueError("없는 심사 종류입니다")
    if audience not in AUDIENCES:
        raise ValueError("받는 사람은 customer, agent, internal 가운데 하나입니다")
    text = (text or "").strip()
    if not text:
        raise ValueError("심사할 글이 비어 있습니다")
    probe = aictx.Masker()
    probe(text)
    if probe.count:
        raise ValueError(PII_FOUND)
    store.ensure(con)
    panel = panel or load_panel()
    forbidden = guides.forbidden("common")
    g = guides.get("review")
    h = hashlib.sha256(f"{g['version']}\n{panel['version']}\n{guides.FORBIDDEN_VERSION}\n{audience}\n{text}".encode()).hexdigest()
    rid = con.execute(
        """INSERT INTO ai_review(kind, audience, title, requested_by, created_at, updated_at, input_hash, guide_version, panel_version,
                                 forbidden_version, required, draft_text, current_text, status, rounds, parent_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'running',0,?)""",
        (kind, audience, title, ctx.get("login_id"), db.now(), db.now(), h, g["version"], panel["version"], guides.FORBIDDEN_VERSION,
         int(needs_review(audience)), text, text, parent_id)).lastrowid
    con.commit()

    allowed = aictx.numbers(text) | set(source_numbers or ())
    if source_numbers is not None:
        extra = checks.new_numbers(text, set(source_numbers))
        if extra:
            _stop(con, rid, "number", 0, text)
            return get_review(con, rid)

    client = client or AIClient(con)
    seats = [s for s in panel["seats"] if s.defined]
    current = text
    prev_opinions: list = []
    for rnd in range(1, MAX_ROUNDS + 1):
        # 다빈치의 형상화가 먼저 나온다
        c = aictx.assemble(FEATURE, ctx, guide_key="review_shape", task="형상화해 주세요.", evidence=[("review_text", current)],
                           extra_system=_role_block(panel["shape"]))
        rep = client.call(FEATURE, system=c.system, user=c.user, login_id=ctx.get("login_id"), guide_version=c.guide_version,
                          input_hash=c.input_hash, max_tokens=600)
        if not rep.ok:
            _stop(con, rid, rep.status, rnd, current)
            return get_review(con, rid)
        _save_opinion(con, rid, rnd, panel["shape"], "shape", reason=rep.text.strip(), call_id=rep.call_id)

        opinions = []
        for seat in seats:
            extra = _role_block(seat) + ("\n\n" + _themis_extra(forbidden) if seat.key == "themis" else "")
            task = "심사해 JSON 으로 답해 주세요."
            if rnd > 1 and prev_opinions:          # 첫 회전에는 다른 자리의 의견을 보여 주지 않는다
                task += "\n지난 회전의 의견입니다. 참고만 하고 당신의 본능으로 판정합니다.\n" + "\n".join(
                    f"- {o['seat_name']}: {o['verdict']}. {o['reason'] or ''}" for o in prev_opinions)
            c = aictx.assemble(FEATURE, ctx, guide_key="review", task=task, evidence=[("review_text", current)], extra_system=extra)
            rep = client.call(FEATURE, system=c.system, user=c.user, login_id=ctx.get("login_id"), guide_version=c.guide_version,
                              input_hash=c.input_hash, max_tokens=1500)
            if not rep.ok:
                _stop(con, rid, rep.status, rnd, current)
                return get_review(con, rid)
            p = checks.parse_json(rep.text) or {}
            verdict = p.get("verdict") if p.get("verdict") in ("pass", "revise", "reject") else "error"
            revised = ((p.get("revised") or "").strip() or None) if verdict == "revise" else None
            oid = _save_opinion(con, rid, rnd, seat, verdict, quote=p.get("quote"), quote_ok=_quote_ok(p.get("quote"), current),
                                reason=p.get("reason") if verdict != "error" else "형식에 맞지 않는 답", revised=revised, call_id=rep.call_id)
            opinions.append({"opinion_id": oid, "seat_name": seat.name, "verdict": verdict, "reason": p.get("reason"), "revised": revised})

        auto = checks.check(current, allowed_numbers=allowed, forbidden=forbidden)
        if all(o["verdict"] == "pass" for o in opinions) and auto.ok:
            con.execute("UPDATE ai_review SET status='passed', rounds=?, current_text=?, updated_at=? WHERE review_id=?",
                        (rnd, current, db.now(), rid))
            con.commit()
            return get_review(con, rid)
        if any(o["verdict"] == "reject" for o in opinions):
            _stop(con, rid, "reject", rnd, current)
            return get_review(con, rid)
        # 고친 문안은 자리 순서대로 보고, 새 숫자와 금지 표현이 없는 첫 문안을 받는다
        for o in opinions:
            if o["revised"] and o["revised"] != current and checks.check(o["revised"], allowed_numbers=allowed, forbidden=forbidden).ok \
                    and not _has_pii(o["revised"]):
                current = o["revised"]
                con.execute("UPDATE ai_review_opinion SET adopted=1 WHERE opinion_id=?", (o["opinion_id"],))
                break
        con.execute("UPDATE ai_review SET rounds=?, current_text=?, updated_at=? WHERE review_id=?", (rnd, current, db.now(), rid))
        con.commit()
        prev_opinions = opinions
    _stop(con, rid, "rounds", MAX_ROUNDS, current)
    return get_review(con, rid)


def _has_pii(text) -> bool:
    m = aictx.Masker()
    m(text)
    return m.count > 0


def get_review(con, review_id: int) -> dict | None:
    """심사 한 건. 화면의 심사 결과가 부른다. rounds_detail 은 회전마다 형상화가 먼저, 그다음 자리별 의견이다."""
    store.ensure(con)
    r = con.execute("SELECT * FROM ai_review WHERE review_id=?", (review_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    ops = [dict(o) for o in con.execute("SELECT * FROM ai_review_opinion WHERE review_id=? ORDER BY round_no, opinion_id", (review_id,))]
    d["rounds_detail"] = []
    for n in sorted({o["round_no"] for o in ops}):
        rows = [o for o in ops if o["round_no"] == n]
        d["rounds_detail"].append({"round": n, "shape": next((o["reason"] for o in rows if o["verdict"] == "shape"), None),
                                   "opinions": [o for o in rows if o["verdict"] != "shape"]})
    panel = load_panel()
    d["pending_seats"] = [s.name for s in panel["seats"] if not s.defined]
    d["audience_ko"] = AUDIENCES.get(d["audience"], d["audience"])
    d["kind_ko"] = KINDS.get(d["kind"], d["kind"])
    d["stop_ko"] = STOP_KO.get(d["stop_reason"]) if d["stop_reason"] else None
    d["can_approve"] = d["status"] == "passed" or (d["status"] == "stopped" and not d["required"] and d["stop_reason"] != "number")
    return d


def list_reviews(con, status: str | None = None, limit: int = 100) -> list:
    store.ensure(con)
    cond, args = ("WHERE status=?", (status,)) if status else ("", ())
    rows = [dict(r) for r in con.execute(
        f"""SELECT review_id, kind, audience, title, requested_by, created_at, status, stop_reason, rounds, required, decided_by, decided_at
            FROM ai_review {cond} ORDER BY review_id DESC LIMIT ?""", (*args, limit))]
    for r in rows:
        r["kind_ko"], r["audience_ko"] = KINDS.get(r["kind"], r["kind"]), AUDIENCES.get(r["audience"], r["audience"])
    return rows


def decide(con, ctx: dict, review_id: int, approve: bool, *, final_text: str | None = None, reason: str | None = None) -> dict:
    """메인 관리자의 결재. 화면의 결재 단추가 부른다."""
    aictx.require_role(ctx, FEATURE)
    d = get_review(con, review_id)
    if not d:
        raise ValueError("없는 심사입니다")
    if d["status"] not in ("passed", "stopped"):
        raise ValueError("결재할 수 있는 상태가 아닙니다")
    if not approve:
        if not (reason or "").strip():
            raise ValueError("반려 사유를 적어 주세요")
        con.execute("UPDATE ai_review SET status='rejected', decided_by=?, decided_at=?, decision_reason=?, updated_at=? WHERE review_id=?",
                    (ctx["login_id"], db.now(), reason, db.now(), review_id))
        con.commit()
        return get_review(con, review_id)
    if d["required"] and d["status"] != "passed":
        raise ValueError(GATE)
    if d["stop_reason"] == "number":
        raise ValueError("입력에 없는 숫자가 있는 글은 승인하지 않습니다")
    final = (final_text or "").strip() or d["current_text"]
    edited = final != d["current_text"]
    if edited:
        if d["required"]:
            raise ValueError(REEDIT)
        if _has_pii(final):
            raise ValueError(PII_FOUND)
        hits = checks.forbidden_hits(final, guides.forbidden("common"))
        if hits:
            raise ValueError("금지 표현이 있습니다: " + ", ".join(hits))
    con.execute("""UPDATE ai_review SET status='approved', decided_by=?, decided_at=?, final_text=?, human_edited=?, updated_at=?
                   WHERE review_id=?""", (ctx["login_id"], db.now(), final, int(edited), db.now(), review_id))
    con.commit()
    return get_review(con, review_id)


def can_release(con, review_id: int, text: str | None = None) -> bool:
    """밖으로 내보내도 되는지. 사람이 승인했고, text 를 주면 승인한 글과 같아야 한다."""
    d = get_review(con, review_id)
    if not d or d["status"] != "approved":
        return False
    return text is None or text.strip() == (d["final_text"] or "").strip()


def measures(con) -> dict:
    """심사의 재는 것: 승인율, 사람이 고친 비율, 반려 사유별 건수, 멈춘 사유별 건수."""
    store.ensure(con)
    rows = [dict(r) for r in con.execute("SELECT status, stop_reason, human_edited, decision_reason FROM ai_review")]
    decided = [r for r in rows if r["status"] in ("approved", "rejected")]
    approved = [r for r in decided if r["status"] == "approved"]
    count = lambda key, rs: {k: sum(1 for r in rs if r[key] == k) for k in {r[key] for r in rs if r[key]}}
    return {"decided": len(decided), "approval_rate": len(approved) / len(decided) if decided else None,
            "edited_rate": sum(r["human_edited"] or 0 for r in approved) / len(approved) if approved else None,
            "reject_reasons": count("decision_reason", [r for r in decided if r["status"] == "rejected"]),
            "stop_reasons": count("stop_reason", rows)}
