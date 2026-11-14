"""명함 수료 코드 발급의 판단과 대조. S2 명함 연동 2차. D-44: 수료 코드 발급은 메인 관리자만 한다.

화면이 부르는 함수
- issue_card_codes: 고른 수료 기록으로 수료 코드를 발급한다.
- reconcile_card_codes: 사이트 발급 기록과 앱 기록을 일련번호와 해시로 견준다.
- release_card_code_request: 결과를 모르는 채 남은 요청을, 메인 관리자가 사이트에서 발급되지 않은 것을 확인한 뒤 실패로 돌린다.

발급의 차례
1. 두 번째 잠금. is_main 이 참이고 DB 에서도 활성 메인 관리자여야 한다. card_decide.actor_allowed 로 본다.
2. 대상은 education.card_code_candidates 가 주는 수료만이다. 빠진 수료, 명함 과정이 없는 과정, 해촉자, 이미 살아 있는 발급은 빠진다.
3. 쓰기 스위치와 키를 본다. 막히면 앱 기록도 바꾸지 않는다.
4. 과정과 수료월마다 묶어 200명씩 나눈다. 묶음마다 education.request_card_code 로 requested 를 만든 뒤 한 번 보낸다.
5. 결과에 따라 record_card_code_issued 나 record_card_code_failed 를 부른다. 결과를 모르면 requested 로 둔다.

같은 수료에 두 번 발급하지 않는 장치
- edu_card_code 의 살아 있는 발급 유일 색인(설계사와 명함 과정)이 requested 와 issued 를 하나로 막는다. 프로세스가 여럿이어도 같다.
- 결과를 모르면 requested 로 두어 다시 보내지 않는다. 메인 관리자가 사이트에서 확인한 뒤 release_card_code_request 로 푼다.
- 클라이언트는 POST 를 자동으로 다시 보내지 않는다.

밖으로 나가는 것
- 명단에는 이름만 담는다. 이름은 앱 DB 의 agent.name 을 education.card_code_candidates 로 읽는다. 휴대폰은 앱 DB 에 없고 보내지 않는다.
- 과정은 edu_course.card_course_code, 수료월은 edu_completion.completed_on 의 YYYY-MM 이다.

남기는 것
- edu_card_code 에는 일련번호와 해시만 남는다. 코드 원문은 결과의 CodeIssueRow.code 에만 있고 화면에 한 번 보여 주고 버린다.
- audit_log 에는 묶음마다 action card_code_issue, target card_code:과정:수료월, after_json 에 과정, 수료월, 건수, 결과만 둔다.
  education 엔진도 요청, 발급, 실패를 한 줄씩 남긴다. 거기에는 수료 번호, 과정, 일련번호, 정해진 실패 사유만 있다.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Iterable

from app import db
from app.connect import tables
from app.connect.base import FAILED, NOT_CONFIGURED, OK, Outcome
from app.connect.card_decide import ALREADY, DENIED, actor_allowed
from app.connect.card_site import CODES_MAX, UNKNOWN, WRITE_OFF, CardSiteClient, CodeBatch, SiteCode
from app.engine import education as edu

# 결과 상태. 전체 결과는 OK, PARTIAL, UNKNOWN, ALREADY, DENIED, WRITE_OFF, NOT_CONFIGURED, FAILED 가운데 하나다
PARTIAL = "partial"               # 일부만 발급했다
# 줄마다의 결과
ISSUED = "issued"
NOT_ELIGIBLE = "not_eligible"     # 발급할 수 있는 수료가 아니다

ISSUE_KO = {
    OK: "발급함", PARTIAL: "일부 발급", UNKNOWN: "확인 필요", ALREADY: "이미 발급함", DENIED: "권한 없음",
    WRITE_OFF: "쓰기 꺼짐", NOT_CONFIGURED: "설정 안 됨", FAILED: "실패", ISSUED: "발급함", NOT_ELIGIBLE: "발급 대상 아님",
}
AUDIT_ISSUE = "card_code_issue"
MAIN_ONLY = "수료 코드 발급은 메인 관리자만 합니다"
FAIL_NOT_ISSUED = "명함 사이트가 발급하지 않았습니다"
FAIL_RELEASED = "메인 관리자가 명함 사이트에서 발급되지 않은 것을 확인했습니다"

_LOCK = threading.Lock()
_INFLIGHT: set = set()            # 이 프로세스에서 보내는 중인 수료 번호


@dataclass(frozen=True)
class CodeIssueRow:
    """수료 하나의 발급 결과. 이름은 없다. code 는 화면에 한 번 보여 줄 원문이고 repr 과 to_dict 에 빠진다."""
    completion_id: int
    agent_id: int | None
    result: str                   # issued / failed / unknown / already_done / not_eligible / write_off / not_configured
    issue_id: int | None = None
    course: str | None = None
    ym: str | None = None
    serial: str | None = None
    reason: str = ""
    code: str = field(default="", repr=False, compare=False)

    @property
    def result_ko(self) -> str:
        return ISSUE_KO.get(self.result, self.result)

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "code"}
        d["result_ko"] = self.result_ko
        return d


@dataclass(frozen=True)
class CodeIssueReport:
    rows: tuple = ()              # CodeIssueRow

    def count(self, result: str) -> int:
        return sum(1 for r in self.rows if r.result == result)

    def to_dict(self) -> dict:
        return {"rows": [r.to_dict() for r in self.rows],
                "counts": {k: self.count(k) for k in sorted({r.result for r in self.rows})}}


@dataclass(frozen=True)
class CodeReconcile:
    """사이트 발급 기록과 앱 기록의 대조. 일련번호와 발급 번호만 담는다."""
    matched: tuple = ()           # 일련번호와 해시가 같은 일련번호
    hash_mismatch: tuple = ()     # 일련번호는 같은데 해시가 다른 일련번호
    app_only: tuple = ()          # 앱에는 발급으로 있는데 사이트에 없는 일련번호
    site_only: tuple = ()         # 사이트에만 있는 일련번호. 사이트 관리 화면에서 바로 발급한 것일 수 있다
    revoked_on_app: tuple = ()    # 앱에서 내렸는데 사이트에는 남은 일련번호. 사이트에는 코드를 지우는 기능이 없다
    unconfirmed: tuple = ()       # 앱에 requested 로 남아 결과를 모르는 발급 번호

    @property
    def clean(self) -> bool:
        return not (self.hash_mismatch or self.app_only or self.unconfirmed)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["clean"] = self.clean
        return d


# ------------------------------------------------------------------ 변경 기록
def _audit(con, actor, course, ym, result, counts: dict) -> None:
    after = {"course": course, "ym": ym, "result": result, **counts}
    with tables.write(con, "card_code_issue"):
        con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                    (db.now(), str(actor or "")[:64], AUDIT_ISSUE, f"card_code:{course or '-'}:{ym or '-'}", None,
                     json.dumps({k: v for k, v in after.items() if v is not None}, ensure_ascii=False)))


def _ids(completion_ids) -> list:
    out = []
    for x in completion_ids or ():
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v not in out:
            out.append(v)
    return out


def _main_lock(con, actor, is_main, today) -> tuple[bool, str]:
    if not is_main:
        return False, MAIN_ONLY
    ok, why = actor_allowed(con, actor, True, today=today)
    return (True, "") if ok else (False, f"{why}. {MAIN_ONLY}")


# ------------------------------------------------------------------ 발급
def issue_card_codes(con, client: CardSiteClient, *, completion_ids: Iterable, actor: str, is_main: bool,
                     today: date | None = None) -> Outcome:
    """고른 수료 기록으로 수료 코드를 발급한다. 예외를 올리지 않는다.

    돌려주는 값은 Outcome(status, value=CodeIssueReport, reason). reason 은 화면에 그대로 보여도 되는 말이다.
    status: ok / partial / unknown / already_done / denied / write_off / not_configured / failed
    줄마다 CodeIssueRow.result 가 있다. 발급한 줄의 code 는 화면에 한 번만 보여 주고 어디에도 남기지 않는다."""
    ids = _ids(completion_ids)
    if not ids:
        return Outcome(FAILED, value=CodeIssueReport(), reason="발급할 수료 기록을 골라 주세요")

    ok, why = _main_lock(con, actor, is_main, today)
    if not ok:
        try:
            _audit(con, actor, None, None, DENIED, {"count": len(ids)})
        except Exception:
            pass
        return Outcome(DENIED, value=CodeIssueReport(), reason=why)

    # 대상. DB 만 읽는다
    try:
        targets, rows = _targets(con, ids)
    except Exception:
        return Outcome(FAILED, value=CodeIssueReport(), reason="수료 기록을 읽지 못했습니다")
    if not targets:
        status = ALREADY if rows and all(r.result == ALREADY for r in rows) else FAILED
        return Outcome(status, value=CodeIssueReport(tuple(rows)),
                       reason="이미 발급했거나 발급 요청이 있습니다" if status == ALREADY else "발급할 수 있는 수료 기록이 없습니다")

    groups: dict = {}
    for t in targets:
        groups.setdefault((t["card_course_code"], t["completed_ym"]), []).append(t)

    # 쓰기 스위치와 설정. 막히면 앱 기록도 바꾸지 않는다
    gate = client.write_gate()
    if gate is not None:
        for (course, ym), ts in groups.items():
            _safe_audit(con, actor, course, ym, gate.status, {"count": len(ts)})
            rows += [CodeIssueRow(completion_id=t["completion_id"], agent_id=t["agent_id"], result=gate.status, course=course, ym=ym)
                     for t in ts]
        return Outcome(gate.status, value=CodeIssueReport(tuple(rows)), reason=gate.reason)

    # 이 프로세스 안에서 같은 수료를 겹쳐 보내지 않는다. 프로세스 밖은 DB 유일 색인이 막는다
    mine = [t["completion_id"] for t in targets]
    with _LOCK:
        busy = set(mine) & _INFLIGHT
        _INFLIGHT.update(set(mine) - busy)
    try:
        reasons = []
        for (course, ym), ts in groups.items():
            ts = [t for t in ts if t["completion_id"] not in busy]
            for i in range(0, len(ts), CODES_MAX):
                got, why = _issue_group(con, client, actor, course, ym, ts[i:i + CODES_MAX])
                rows += got
                if why:
                    reasons.append(why)
        rows += [CodeIssueRow(completion_id=t["completion_id"], agent_id=t["agent_id"], result=ALREADY,
                              course=t["card_course_code"], ym=t["completed_ym"], reason="같은 수료를 지금 발급하고 있습니다")
                 for t in targets if t["completion_id"] in busy]
    finally:
        with _LOCK:
            _INFLIGHT.difference_update(set(mine) - busy)

    rep = CodeIssueReport(tuple(sorted(rows, key=lambda r: ids.index(r.completion_id))))
    status = _overall(rep)
    head = {OK: f"{rep.count(ISSUED)}건 발급했습니다", PARTIAL: f"{rep.count(ISSUED)}건 발급하고 나머지는 발급하지 못했습니다",
            UNKNOWN: "결과를 확인하지 못한 발급이 있습니다. 명함 사이트 관리 화면의 발급 기록을 확인해 주세요",
            ALREADY: "이미 발급했거나 발급 요청이 있습니다", FAILED: "발급하지 못했습니다"}[status]
    tail = " ".join(dict.fromkeys(r for r in reasons if r))
    return Outcome(status, value=rep, reason=f"{head}. {tail}".strip(" .")[:300] if tail else head, attempts=1)


def _safe_audit(con, actor, course, ym, result, counts) -> None:
    try:
        _audit(con, actor, course, ym, result, counts)
    except Exception:
        pass


def _targets(con, ids: list) -> tuple[list, list]:
    """발급할 수 있는 수료와, 빠지는 수료의 결과 줄. 이름은 보내는 데만 쓴다."""
    q = ",".join("?" * len(ids))
    who = {r[0]: r[1] for r in con.execute(f"SELECT completion_id, agent_id FROM edu_completion WHERE completion_id IN ({q})", ids)}
    agent_ids = sorted({a for a in who.values() if a is not None})
    cands = {c["completion_id"]: c for c in edu.card_code_candidates(con, agent_ids)} if agent_ids else {}
    targets, rows, seen = [], [], set()
    for cid in ids:
        c = cands.get(cid)
        key = (c["agent_id"], c["card_course_code"]) if c else None
        if c and key not in seen:
            seen.add(key)
            targets.append(c)
            continue
        if c:      # 같은 사람의 같은 명함 과정을 한 번에 둘 골랐다
            rows.append(CodeIssueRow(completion_id=cid, agent_id=c["agent_id"], result=ALREADY, course=c["card_course_code"],
                                     ym=c["completed_ym"], reason="같은 사람의 같은 과정을 함께 골랐습니다"))
            continue
        live = con.execute(
            """SELECT 1 FROM edu_completion c JOIN edu_course e ON e.course_id=c.course_id
               JOIN edu_card_code k ON k.agent_id=c.agent_id AND k.card_course_code=e.card_course_code
               WHERE c.completion_id=? AND k.status IN ('requested','issued') LIMIT 1""", (cid,)).fetchone()
        if live:
            rows.append(CodeIssueRow(completion_id=cid, agent_id=who.get(cid), result=ALREADY, reason="이미 발급했거나 발급 요청이 있습니다"))
        else:
            rows.append(CodeIssueRow(completion_id=cid, agent_id=who.get(cid), result=NOT_ELIGIBLE,
                                     reason="살아 있는 수료가 아니거나, 명함 과정이 없는 과정이거나, 해촉자입니다"))
    return targets, rows


def _issue_group(con, client: CardSiteClient, actor, course: str, ym: str, ts: list) -> tuple[list, str]:
    """과정과 수료월이 같은 묶음 하나를 requested 로 만들고 한 번 보낸다."""
    rows, sent = [], []
    for t in ts:
        try:
            iid = edu.request_card_code(con, t["completion_id"], by=actor)
        except ValueError:
            rows.append(CodeIssueRow(completion_id=t["completion_id"], agent_id=t["agent_id"], result=ALREADY, course=course, ym=ym,
                                     reason="이미 발급했거나 발급 요청이 있습니다"))
            continue
        sent.append((t, iid))
    if not sent:
        return rows, ""

    try:
        out = client.issue_codes(course, ym, [t["name"] for t, _ in sent])
    except Exception:
        out = Outcome(UNKNOWN, value=CodeBatch(course=course, ym=ym, sent=len(sent), unknown=tuple(range(len(sent)))),
                      reason="보내는 중 오류가 났습니다. 명함 사이트 관리 화면의 발급 기록을 확인해 주세요")
    batch: CodeBatch = out.value if isinstance(out.value, CodeBatch) else CodeBatch(course=course, ym=ym, sent=0)
    by_index = {c.index: c for c in batch.issued}
    unknown = set(batch.unknown)
    if out.status not in (OK, UNKNOWN):
        unknown = set()                       # 보내지 않았거나 사이트가 받지 않았다
    for k, (t, iid) in enumerate(sent):
        base = {"completion_id": t["completion_id"], "agent_id": t["agent_id"], "issue_id": iid, "course": course, "ym": ym}
        got = by_index.get(k)
        if got is not None:
            try:
                edu.record_card_code_issued(con, iid, serial=got.serial, code=got.code, by=actor)
                rows.append(CodeIssueRow(**base, result=ISSUED, serial=got.serial, code=got.code))
            except Exception:
                _rollback(con)
                rows.append(CodeIssueRow(**base, result=UNKNOWN, serial=got.serial, code=got.code,
                                         reason="명함 사이트는 발급했지만 앱에 적지 못했습니다. 발급 기록 대조로 확인해 주세요"))
        elif k in unknown:
            rows.append(CodeIssueRow(**base, result=UNKNOWN, reason="결과를 확인하지 못해 요청 상태로 두었습니다"))
        else:
            why = FAIL_NOT_ISSUED if out.status == OK else (out.reason or FAIL_NOT_ISSUED)
            try:
                edu.record_card_code_failed(con, iid, reason=why, by=actor)
            except Exception:
                _rollback(con)
            rows.append(CodeIssueRow(**base, result=FAILED, reason=why))
    n = {r: sum(1 for x in rows if x.result == r) for r in (ISSUED, FAILED, UNKNOWN, ALREADY)}
    res = _overall(CodeIssueReport(tuple(rows)))
    _safe_audit(con, actor, course, ym, res, {"count": len(ts), "issued": n[ISSUED], "failed": n[FAILED],
                                              "unknown": n[UNKNOWN], "already": n[ALREADY]})
    return rows, out.reason if out.status != OK else ""


def _rollback(con) -> None:
    try:
        con.rollback()
    except Exception:
        pass


def _overall(rep: CodeIssueReport) -> str:
    results = [r.result for r in rep.rows]
    if not results:
        return FAILED
    if UNKNOWN in results:
        return UNKNOWN
    issued = results.count(ISSUED)
    if issued and issued == len(results):
        return OK
    if issued:
        return PARTIAL
    if all(r == ALREADY for r in results):
        return ALREADY
    return FAILED


# ------------------------------------------------------------------ 결과를 모르는 요청 풀기
def release_card_code_request(con, *, issue_id: int, actor: str, is_main: bool, today: date | None = None) -> Outcome:
    """requested 로 남은 발급을 실패로 돌린다. 메인 관리자가 사이트 발급 기록에 그 사람의 줄이 없는 것을 확인한 뒤에 부른다.
    실패로 돌리면 같은 수료로 다시 발급할 수 있다. 사유는 정해진 말만 남긴다."""
    ok, why = _main_lock(con, actor, is_main, today)
    if not ok:
        return Outcome(DENIED, reason=why)
    r = con.execute("SELECT status, serial FROM edu_card_code WHERE issue_id=?", (int(issue_id),)).fetchone()
    if r is None or r[0] != "requested" or r[1]:
        return Outcome(FAILED, reason="결과를 모르는 요청 상태의 발급이 아닙니다")
    try:
        edu.record_card_code_failed(con, int(issue_id), reason=FAIL_RELEASED, by=actor)
    except Exception:
        _rollback(con)
        return Outcome(FAILED, reason="앱 기록을 바꾸지 못했습니다")
    return Outcome(OK, reason="요청을 실패로 돌렸습니다. 다시 발급할 수 있습니다")


# ------------------------------------------------------------------ 발급 기록 대조
def reconcile_card_codes(con, client: CardSiteClient, *, actor: str, is_main: bool, today: date | None = None) -> Outcome:
    """사이트 발급 기록과 앱의 edu_card_code 를 일련번호와 해시로 견준다. 이름은 쓰지 않는다. 읽기만 한다.
    돌려주는 값은 Outcome(status, value=CodeReconcile, reason). status: ok / denied / not_configured / failed"""
    ok, why = _main_lock(con, actor, is_main, today)
    if not ok:
        return Outcome(DENIED, reason=why.replace(MAIN_ONLY, "수료 코드 발급 기록 대조는 메인 관리자만 합니다"))
    site = client.code_log()
    if site.status != OK:
        return Outcome(site.status if site.status == NOT_CONFIGURED else FAILED, value=CodeReconcile(),
                       reason=f"명함 사이트의 발급 기록을 읽지 못했습니다. {site.reason}".strip()[:300], attempts=site.attempts)
    on_site: dict = {}
    for c in site.value or []:
        if isinstance(c, SiteCode):
            on_site[c.serial] = c
    app = con.execute("SELECT issue_id, status, serial, code_sha256 FROM edu_card_code ORDER BY issue_id").fetchall()
    matched, mismatch, app_only, revoked, unconfirmed, known = [], [], [], [], [], set()
    for issue_id, status, serial, h in app:
        if not serial:
            if status == "requested":
                unconfirmed.append(issue_id)
            continue
        known.add(serial)
        s = on_site.get(serial)
        if status == "revoked":
            if s is not None:
                revoked.append(serial)
            continue
        if s is None:
            app_only.append(serial)
        elif h and s.code_sha256 and h == s.code_sha256:
            matched.append(serial)
        else:
            mismatch.append(serial)
    rec = CodeReconcile(matched=tuple(matched), hash_mismatch=tuple(mismatch), app_only=tuple(app_only),
                        site_only=tuple(sorted(set(on_site) - known)), revoked_on_app=tuple(revoked), unconfirmed=tuple(unconfirmed))
    msg = "사이트와 앱의 발급 기록이 맞습니다" if rec.clean else "사이트와 앱의 발급 기록에 다른 곳이 있습니다"
    return Outcome(OK, value=rec, reason=msg, attempts=site.attempts)
