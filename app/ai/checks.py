"""자동 점검. docs/13 2.4 의 3단계다.

- 금지 표현이 있는지 본다.
- 출력의 숫자가 모두 입력에 있는지 본다. 근거 번호 [3] 은 숫자 점검에서 빼고 따로 본다.
- 근거 번호가 컨텍스트의 근거 줄 번호 안에 있는지 본다. 근거가 꼭 있어야 하는 기능은 번호가 하나도 없으면 막는다.

숫자가 맞지 않으면 그 출력은 사람에게도 보이지 않고 멈춘다. 멈추는 조건의 하나다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ai.context import CITE, Context, numbers, strip_dates

UNKNOWN_HINTS = ("모릅니다", "알 수 없습니다", "찾지 못했습니다", "확인되지 않습니다", "원안을 확인")


@dataclass
class Check:
    ok: bool
    problems: list = field(default_factory=list)       # [{"kind": number|forbidden|citation|no_citation, "detail": ...}]

    def kinds(self) -> set:
        return {p["kind"] for p in self.problems}


def forbidden_hits(text: str, words) -> list:
    flat = re.sub(r"\s+", "", text or "")
    return [w for w in words if re.sub(r"\s+", "", w) in flat]


def citations(text: str) -> list:
    return [int(n) for n in CITE.findall(text or "")]


def strip_citations(text: str) -> str:
    return CITE.sub(" ", text or "")


def new_numbers(output: str, allowed: set) -> list:
    """출력에서 입력에 없던 숫자. 정렬해 돌려준다."""
    return sorted(numbers(strip_citations(output)) - set(allowed), key=lambda s: (len(s), s))


def check(output: str, context: Context | None = None, *, allowed_numbers: set | None = None, forbidden=(), require_citation=False) -> Check:
    allowed = set(allowed_numbers or ()) | (context.numbers if context else set())
    problems = []
    # 컨텍스트의 날짜(오늘)는 날짜 꼴 그대로일 때만 받는다. 연, 월, 일을 따로 쓰면 숫자 점검에 걸린다.
    extra = new_numbers(strip_dates(output, context.dates) if context else output, allowed)
    if extra:
        problems.append({"kind": "number", "detail": extra})
    hits = forbidden_hits(output, forbidden)
    if hits:
        problems.append({"kind": "forbidden", "detail": hits})
    cited = citations(output)
    if context is not None:
        bad = sorted({n for n in cited if n not in context.evidence_nos()})
        if bad:
            problems.append({"kind": "citation", "detail": bad})
    if require_citation and not cited and not any(h in (output or "") for h in UNKNOWN_HINTS):
        problems.append({"kind": "no_citation", "detail": []})
    return Check(not problems, problems)


def parse_json(text: str) -> dict | None:
    """모델 답에서 JSON 객체 하나를 꺼낸다. 코드 울타리나 앞뒤 글이 있어도 첫 { 부터 짝이 맞는 } 까지 읽는다."""
    import json

    s = text or ""
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(s[start:i + 1])
                        return v if isinstance(v, dict) else None
                    except ValueError:
                        break
        start = s.find("{", start + 1)
    return None


PROBLEM_KO = {"number": "입력에 없는 숫자", "forbidden": "금지 표현", "citation": "없는 근거 번호", "no_citation": "근거 번호 없음",
              "quote": "원문에 없는 인용", "format": "형식 오류"}


def describe(problems: list) -> list:
    """화면에 보일 한국어 설명."""
    out = []
    for p in problems or []:
        d = p.get("detail")
        tail = f": {', '.join(map(str, d))}" if d else ""
        out.append(PROBLEM_KO.get(p["kind"], p["kind"]) + tail)
    return out
