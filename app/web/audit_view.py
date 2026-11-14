"""변경 기록 화면의 이름표. 영문 행위 코드와 대상의 내부 번호를 사람이 읽는 말로 바꾼다.

이름표는 이 파일 한 곳에 모은다. 새 행위 코드를 audit_log 에 남기면 ACTION_KO 에 한 줄을 더한다.
모르는 코드와 모르는 대상 꼴은 원문을 그대로 보여 준다. 지어내지 않는다.

사람은 사번이나 아이디로만 적고 이름은 붙이지 않는다. 화면 표시용이라 계산은 하지 않는다.
"""
import re

from app.web import auth, menu

ACTION_KO = {
    # 로그인과 계정
    "login": "로그인",
    "login_fail": "로그인 실패",
    "password_change": "비밀번호 변경",
    "access_denied": "권한 밖 접근 시도",
    "account_issue": "계정 발급",
    "account_reset": "비밀번호 재설정",
    "account_unlock": "잠금 해제",
    "account_suspend": "계정 중지",
    "account_activate": "계정 다시 사용",
    # 조직
    "org_head_assign": "조직장 지정",
    "org_head_end": "조직장 해제",
    "org_deputy_assign": "대리 지정",
    "org_deputy_end": "대리 끝내기",
    "staff_sync": "사원현황 반영",
    "org_from_contracts": "계약현황으로 조직 만들기",
    # 프로모션과 조직 부문 시상
    "promotion_published": "프로모션 게시",
    "promotion_withdrawn": "프로모션 회수",
    "promotion_draft": "프로모션 초안으로 돌림",
    "promotion_update": "프로모션 고침",
    "org_award_create": "조직 부문 시상 만듦",
    "org_award_update": "조직 부문 시상 고침",
    "org_award_active": "조직 부문 시상 다시 엶",
    "org_award_inactive": "조직 부문 시상 닫음",
    "org_award_confirm": "조직 부문 수상자 확정",
    "incentive_approve": "시상 공문 승인",
    # 명함
    "card_grant": "명함 승인 권한 켬",
    "card_revoke": "명함 승인 권한 끔",
    "card_grant_denied": "명함 승인 권한 처리 거부",
    "card_request": "명함 승인 권한 요청",
    "card_request_reject": "명함 승인 권한 요청 반려",
    "card_request_cancel": "명함 승인 권한 요청 거둠",
    "card_approve": "명함 승인",
    "card_reject": "명함 반려",
    "card_decide": "명함 처리",
    "card_preview": "명함 신청본 미리 봄",
    "card_link": "명함 주소 이음",
    "card_unlink": "명함 주소 끊음",
    "card_code_issue": "명함 수료 코드 발급",
    # 교육. 행위 코드는 app/engine/education.py 의 _audit 호출과 같다
    "edu_course_create": "교육 과정 등록",
    "edu_course_update": "교육 과정 고침",
    "edu_course_active": "교육 과정 다시 엶",
    "edu_course_closed": "교육 과정 닫음",
    "edu_session_create": "교육 차수 등록",
    "edu_session_update": "교육 차수 고침",
    "edu_completion_confirm": "교육 수료 확인",
    "edu_completion_void": "교육 수료 뺌",
    "edu_batch_void": "수료자 명부 배치 뺌",
    "edu_card_code_request": "명함 수료 코드 요청",
    "edu_card_code_issued": "명함 수료 코드 발급 기록",
    "edu_card_code_failed": "명함 수료 코드 발급 실패",
    "edu_card_code_revoke": "명함 수료 코드 내림",
    # 기존 운영 도구
    "quality_resolve": "품질검증 처리",
    "diff_update": "대사 차이 처리",
    "close": "월 마감",
}

# 누가 칸의 시스템 행위자
ACTOR_KO = {
    "system:appointed": "자동: 조직장·대리 지정",
    "system:left": "자동: 조직장·대리에서 빠짐",
    "system:inactive": "자동: 계정 중지",
    "ui": "기존 운영 도구",
}

# 프로모션 고침에서 바뀐 칸. app/engine/promotion.py 의 _snapshot 열쇠
PROMO_FIELD_KO = {
    "name": "이름", "kind": "유형", "insurer_name": "보험사", "sector": "업권", "categories": "구분", "metric": "기준 실적",
    "date_basis": "기준일", "per_contract_cap": "건당 한도", "combine_rule": "결합 규칙", "pay_timing": "지급 시점",
    "target_note": "대상 상품", "caution": "유의사항", "windows": "기간 창", "tiers": "구간",
}
OP_KO = {"approve": "요청 승인", "reject": "요청 반려", "grant": "권한 켜기", "revoke": "권한 끄기", "request": "권한 요청"}
FORM_ACTION_KO = {"save": "저장", "active": "다시 열기", "inactive": "닫기", "confirm": "확정", "card_request": "명함 승인 권한 요청"}

_KV = re.compile(r"(\w+)=(\S+)")


def action_ko(code: str) -> str:
    return ACTION_KO.get(code, code)


def actor_ko(actor: str) -> str:
    return ACTOR_KO.get(actor, actor)


def _ym(s: str) -> str:
    s = (s or "").strip()
    return f"{s[:4]}년 {int(s[4:6])}월" if re.fullmatch(r"20\d{4}", s) else s


class _Names:
    """대상 번호를 이름이나 사번으로 바꾸는 조회. 한 화면에서 한 번씩만 읽는다."""

    def __init__(self, con):
        self.con = con
        self._cache = {}

    def _one(self, kind, sql, key):
        k = (kind, key)
        if k not in self._cache:
            try:
                r = self.con.execute(sql, (key,)).fetchone()
            except Exception:                     # 오래된 DB 에 표가 없으면 원문으로 둔다
                r = None
            self._cache[k] = r[0] if r and r[0] is not None else None
        return self._cache[k]

    def promo(self, pid):
        return self._one("promo", "SELECT name FROM promotion WHERE promo_id=?", pid)

    def division(self, did):
        return self._one("division", "SELECT name FROM org_award_division WHERE division_id=?", did)

    def doc(self, did):
        return self._one("doc", "SELECT COALESCE(title, file_path) FROM incentive_source_doc WHERE doc_id=?", did)

    def course(self, cid):
        return self._one("course", "SELECT name FROM edu_course WHERE course_id=?", cid)

    def session(self, sid):
        r = self._one("session", "SELECT c.name || ' ' || s.session_no || '차' FROM edu_session s JOIN edu_course c ON c.course_id=s.course_id "
                                 "WHERE s.session_id=?", sid)
        return r

    def completion(self, cid):
        return self._one("completion", "SELECT a.internal_code || ' · ' || c.name FROM edu_completion e JOIN agent a ON a.agent_id=e.agent_id "
                                       "JOIN edu_course c ON c.course_id=e.course_id WHERE e.completion_id=?", cid)

    def card_issue(self, iid):
        return self._one("card_issue", "SELECT a.internal_code || ' · ' || k.card_course_code || ' ' || k.completed_ym FROM edu_card_code k "
                                       "JOIN agent a ON a.agent_id=k.agent_id WHERE k.issue_id=?", iid)

    def agent_code(self, aid):
        return self._one("agent", "SELECT internal_code FROM agent WHERE agent_id=?", aid)

    def org(self, oid):
        if "orgs" not in self._cache:
            try:
                from app.org.sync import org_texts
                self._cache["orgs"] = org_texts(self.con)
            except Exception:
                self._cache["orgs"] = {}
        return self._cache["orgs"].get(oid)


def _num(s):
    return int(s) if s and s.isdigit() else None


def target_ko(action: str, target, names: _Names) -> str:
    """대상 칸의 글. 아는 꼴만 바꾸고 나머지는 원문 그대로 돌려준다."""
    if target is None or target == "":
        return ""
    t = str(target)
    try:
        if action.startswith("promotion_"):
            pid, _, fields = t.partition(":")
            name = names.promo(_num(pid))
            if name is None:
                return t
            out = f"프로모션: {name}"
            if fields:
                out += " · 바뀐 칸 " + ", ".join(PROMO_FIELD_KO.get(f, f) for f in fields.split(","))
            return out
        if action == "org_award_confirm":
            did, ym, n = (t.split(":") + ["", ""])[:3]
            name = names.division(_num(did))
            return f"조직 부문 시상: {name} · {_ym(ym)} · {n}" if name else t
        if action.startswith("org_award_"):
            name = names.division(_num(t))
            return f"조직 부문 시상: {name}" if name else t
        if action.startswith("org_head_") or action.startswith("org_deputy_"):
            return _org_target(t, names)
        if action.startswith("account_"):
            return f"계정: {t}"
        if action.startswith("edu_"):
            return _edu_target(action, t, names)
        if action == "card_code_issue":
            _, course, ym = (t.split(":") + ["", "", ""])[:3]
            out = "명함 수료 코드"
            if course and course != "-":
                out += f": {course} 과정"
            if ym and ym != "-":
                out += f" · {ym[:4]}년 {int(ym[5:7])}월" if re.fullmatch(r"20\d\d-\d\d", ym) else f" · {ym}"
            return out
        if action == "access_denied":
            path, _, rest = t.partition(" ")
            item = menu.find(path)
            out = f"화면: {item['name']}" if item else f"주소: {path}"
            return out + (f" · {FORM_ACTION_KO.get(rest, rest)}" if rest else "")
        if action == "login_fail":
            return f"까닭: {t}"
        if action == "close":
            return _ym(t)
        if action == "card_grant_denied":
            op, _, n = t.partition(":")
            if op == "request":
                return f"계정: {n}"
            return f"{OP_KO.get(op, op)} {n}건" if n.isdigit() else t
        kind, sep, key = t.partition(":")
        if sep:
            if kind in ("user", "request"):
                return f"계정: {key}"
            if kind == "agent":
                if key == "none":
                    return "설계사: 이어지지 않은 명함"
                code = names.agent_code(_num(key))
                return f"설계사: {code}" if code else t
            if kind == "doc":
                title = names.doc(_num(key))
                return f"시상 공문: {title}" if title else t
            if kind == "diff":
                return f"대사 차이 {key}번"
    except Exception:                             # 이름표가 깨져도 기록은 보여야 한다
        return t
    return t


def _edu_target(action: str, t: str, names: _Names) -> str:
    """교육 행위의 대상은 번호 하나다. 과정, 차수, 수료, 명부 배치, 수료 코드 발급 번호 가운데 무엇인지는 행위 코드로 안다."""
    n = _num(t)
    if n is None:
        return t
    if action.startswith("edu_course_"):
        name = names.course(n)
        return f"과정: {name}" if name else t
    if action.startswith("edu_session_"):
        name = names.session(n)
        return f"차수: {name}" if name else t
    if action.startswith("edu_completion_"):
        name = names.completion(n)
        return f"수료: {name}" if name else t
    if action == "edu_batch_void":
        return f"명부 배치 {n}"
    if action.startswith("edu_card_code_"):
        name = names.card_issue(n)
        return f"수료 코드: {name}" if name else t
    return t


def _org_target(t: str, names: _Names) -> str:
    """'org=3 agent=A0001 202611~' 이나 'org=3 agent_id=5 role=team_head' 꼴."""
    kv = dict(_KV.findall(t))
    if "org" not in kv:
        return t
    parts = [f"조직: {names.org(_num(kv['org'])) or kv['org']}"]
    if "agent" in kv:
        parts.append(f"설계사: {kv['agent']}")
    elif "agent_id" in kv:
        parts.append(f"설계사: {names.agent_code(_num(kv['agent_id'])) or kv['agent_id']}")
    if "role" in kv:
        parts.append(auth.HEAD_KO.get(kv["role"], kv["role"]))
    period = re.search(r"(\S*~\S*)\s*$", t)
    if period and period.group(1) != "~":
        a, _, b = period.group(1).partition("~")
        if len(a) == 6:
            parts.append(f"{_ym(a)} ~ {_ym(b)}" if b else f"{_ym(a)}부터")
        else:
            parts.append(f"{a} ~ {b}" if b else f"{a}부터")
    return " · ".join(parts)


def rows_ko(con, rows: list) -> list:
    """audit_log 의 줄들에 action_ko, actor_ko, target_ko 를 붙인다."""
    names = _Names(con)
    return [{**r, "action_ko": action_ko(r["action"]), "actor_ko": actor_ko(r["actor"]),
             "target_ko": target_ko(r["action"], r.get("target"), names)} for r in rows]
