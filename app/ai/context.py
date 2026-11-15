"""컨텍스트 조립기. docs/13 1.4 의 한 곳이다.

조립기가 하는 일은 네 가지다.
1. 권한 범위로 거른다. 기능마다 쓸 수 있는 역할이 정해져 있고, 사람에 딸린 줄은 ctx["agent_ids"] 안의 것만 넣는다.
2. 개인정보를 가린다. 주민번호, 휴대폰과 전화번호, 전자우편 꼴은 지우고, 넘겨받은 사람 이름은 고객 가, 설계사 가 로 바꾼다.
   이름, 연락처, 주민번호, 주소, 계좌 열쇠를 가진 값은 아예 넣지 않는다. D-25 제안대로 이름과 연락처를 뺀 요약만 넣는다.
3. 근거 줄에 번호를 붙인다. [1] 부터다. 근거 안의 명령문은 자료로만 넣고 몇 줄에 있었는지 남긴다(원칙 6).
4. 지침 버전을 찍는다. 확정된 지침만 쓴다(원칙 3).

숫자 원칙: 모델에게 주는 숫자는 근거 줄의 원문과 facts 에 넣은 계산 엔진의 값뿐이다.
조립한 글에서 뽑은 숫자 모음 numbers 를 checks.check 가 받아, 출력에 그 밖의 숫자가 있으면 막는다.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

from app.ai import guides

# 기능마다 쓸 수 있는 역할. 묻고 답하기는 D-27 제안대로 처음에는 메인 관리자만 쓴다.
FEATURE_ROLES = {
    "plan_interpret": {"main"},
    "plan_qa": {"main"},
    "review": {"main"},
    "report": {"main", "manager", "agent"},
    "cardnews": {"main"},
    "monthly_report": {"main"},
}

RRN = re.compile(r"(?<![0-9])[0-9]{6}\s?-\s?[1-4][0-9]{6}(?![0-9])")
PHONE = re.compile(r"(?<![0-9])0[0-9]{1,2}[-. ]?[0-9]{3,4}[-. ]?[0-9]{4}(?![0-9])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PII_KEYS = {"name", "customer_name", "agent_name", "insured_name", "contractor_name", "display_name", "phone", "mobile", "tel",
            "contact", "email", "rrn", "resident_no", "jumin", "address", "addr", "account", "account_no", "bank_account",
            "이름", "성명", "고객명", "연락처", "휴대폰", "전화", "주민번호", "주소", "계좌"}
LABELS = "가나다라마바사아자차카타파하"

NUM = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


class OutOfScope(PermissionError):
    """그 역할이 쓸 수 없는 기능이거나 범위 밖 자료를 넣으려 할 때."""


def numbers(text: str) -> set:
    """글에서 숫자를 뽑아 같은 꼴로 맞춘다. 1,000 과 1000 은 같고, 05 와 5 와 5.0 도 같다."""
    out = set()
    for m in NUM.finditer(text or ""):
        tok = m.group(0).rstrip(",").replace(",", "")
        if not tok:
            continue
        if "." in tok:
            tok = tok.rstrip("0").rstrip(".")
        tok = tok.lstrip("0") or "0"
        out.add(tok)
    return out


class Masker:
    """한 컨텍스트 안에서 같은 이름은 같은 표시로 바꾼다. 표시는 고객 가, 고객 나 처럼 붙는다."""

    def __init__(self, people: dict | None = None):
        # people = {"customer": [이름...], "agent": [이름...]}
        self.map: dict = {}
        self.count = 0
        for kind, names in (people or {}).items():
            prefix = {"customer": "고객", "agent": "설계사"}.get(kind, "사람")
            for nm in names or []:
                nm = (nm or "").strip()
                if len(nm) >= 2 and nm not in self.map:
                    used = sum(1 for v in self.map.values() if v.startswith(prefix))
                    self.map[nm] = f"{prefix} {LABELS[used % len(LABELS)]}{'' if used < len(LABELS) else used // len(LABELS) + 1}"

    def __call__(self, text) -> str:
        if text is None:
            return ""
        text = str(text)
        for rx, tag in ((RRN, "[주민번호 가림]"), (EMAIL, "[전자우편 가림]"), (PHONE, "[연락처 가림]")):
            text, n = rx.subn(tag, text)
            self.count += n
        for nm in sorted(self.map, key=len, reverse=True):         # 긴 이름부터 바꿔 겹치는 이름이 깨지지 않게 한다
            if nm in text:
                self.count += text.count(nm)
                text = text.replace(nm, self.map[nm])
        return text


def mask(text, people: dict | None = None) -> str:
    return Masker(people)(text)


def drop_pii_keys(d: dict) -> dict:
    """이름, 연락처, 주민번호, 주소, 계좌 열쇠를 뺀다. D-25."""
    return {k: v for k, v in d.items() if str(k).lower() not in PII_KEYS}


def in_scope(ctx: dict, rows, key: str = "agent_id") -> list:
    """사람에 딸린 줄 가운데 그 사용자의 범위 안 것만 남긴다. 사람 열쇠가 없는 줄은 사람 정보가 아니라 남긴다."""
    allowed = set(ctx.get("agent_ids") or ())
    out = []
    for r in rows or []:
        aid = r.get(key) if isinstance(r, dict) else None
        if aid is None or aid in allowed:
            out.append(r)
    return out


def require_role(ctx: dict, feature: str) -> None:
    if ctx.get("role") not in FEATURE_ROLES.get(feature, set()):
        raise OutOfScope("이 기능을 쓸 권한이 없습니다")


def _neutralize(text: str) -> str:
    """근거 글이 자료 구분 표시를 흉내 내지 못하게 꺾쇠를 바꾼다."""
    return text.replace("<", "‹").replace(">", "›")


@dataclass
class Evidence:
    no: int
    source: str             # 어디서 온 줄인지. 예: plan_line:12
    text: str               # 가린 뒤의 글
    suspicious: bool = False


@dataclass
class Context:
    feature: str
    guide_key: str
    guide_version: str
    login_id: str | None
    system: str
    user: str
    evidence: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    numbers: set = field(default_factory=set)
    masked: int = 0
    injection_lines: list = field(default_factory=list)
    input_hash: str = ""

    def record(self) -> dict:
        """기록 표에 남길 것. 가린 뒤의 글만 담는다."""
        return {"feature": self.feature, "guide_version": self.guide_version, "forbidden_version": guides.FORBIDDEN_VERSION,
                "evidence": [{"no": e.no, "source": e.source, "text": e.text} for e in self.evidence],
                "facts": self.facts, "masked": self.masked, "injection_lines": self.injection_lines, "user": self.user}

    def evidence_nos(self) -> set:
        return {e.no for e in self.evidence}


def assemble(feature: str, ctx: dict, *, task: str, evidence=None, facts: dict | None = None, people: dict | None = None,
             guide_key: str | None = None, extra_system: str = "", today: date | str | None = None, examples=None) -> Context:
    """컨텍스트 하나를 만든다.

    evidence: [(source, text) 또는 {"source", "text", "agent_id"}] 근거 줄. 사람에 딸린 줄은 범위 밖이면 버린다.
    facts:    {"이름표": 값} 계산 엔진이 낸 값. 이름과 연락처 열쇠는 버린다.
    people:   {"customer": [...], "agent": [...]} 가릴 이름. 이름은 기록에 남지 않는다.
    task:     질문이나 할 일. 이것도 가린다.
    examples: 해석 예시 글의 목록. <예시> 에 넣되 예시의 숫자는 numbers 에 넣지 않는다. 예시의 숫자를 베껴 쓰면 막힌다.
    """
    require_role(ctx, feature)
    gkey = guide_key or feature
    g = guides.get(gkey)
    m = Masker(people)
    rows = []
    for ev in evidence or []:
        rows.append(ev if isinstance(ev, dict) else {"source": ev[0], "text": ev[1]})
    rows = in_scope(ctx, rows)
    evs, inj = [], []
    for i, r in enumerate(rows, start=1):
        txt = _neutralize(m(r.get("text")))
        sus = any(h.lower() in txt.lower() for h in guides.INJECTION_HINTS)
        if sus:
            inj.append(i)
        evs.append(Evidence(i, str(r.get("source") or ""), txt, sus))
    clean_facts = {}
    for k, v in drop_pii_keys(facts or {}).items():
        clean_facts[_neutralize(m(k))] = _neutralize(m(v))
    if today is None:
        today = date.today()
    today_s = today.isoformat() if isinstance(today, date) else str(today)
    task_s = _neutralize(m(task))

    system = "\n\n".join(s for s in (guides.COMMON_RULES, g["text"], extra_system) if s)
    parts = [f"<오늘>{today_s}</오늘>"]
    if clean_facts:
        parts.append("<계산값>\n" + "\n".join(f"- {k}: {v}" for k, v in clean_facts.items()) + "\n</계산값>")
    parts.append("<자료>\n" + ("\n".join(f"[{e.no}] {e.text}" for e in evs) if evs else "(근거 줄 없음)") + "\n</자료>")
    parts.append(f"<할 일>\n{task_s}\n</할 일>")
    allowed = numbers("\n\n".join(parts))
    if examples:
        parts.append("<예시>\n" + "\n".join(_neutralize(str(x)) for x in examples) + "\n</예시>")
    user = "\n\n".join(parts)
    h = hashlib.sha256(f"{g['version']}\n{system}\n{user}".encode()).hexdigest()
    return Context(feature=feature, guide_key=gkey, guide_version=g["version"], login_id=ctx.get("login_id"), system=system, user=user,
                   evidence=evs, facts=clean_facts, numbers=allowed, masked=m.count, injection_lines=inj, input_hash=h)
