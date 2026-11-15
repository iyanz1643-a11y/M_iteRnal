"""정답을 아는 예시 묶음과 회귀 평가. docs/13 1.4 원칙 9 와 2.4 의 7단계다.

지침이나 점검 규칙을 바꿀 때마다 돌린다. tests/test_ai.py 가 오프라인 묶음을 자동 시험에 넣는다.

| 묶음 | 보는 것 | 모델 |
| --- | --- | --- |
| CHECK_CASES | 자동 점검이 막을 것을 막고 둘 것을 두는지 | 부르지 않는다 |
| MASK_CASES | 가림이 주민번호, 연락처, 이름을 남기지 않는지 | 부르지 않는다 |
| PLAN_CASES | 해석 보조가 정답 해석을 내는지, 원문 확인을 지키는지 | 오프라인은 정답을 답하는 가짜, --live 는 실제 API |

    python -m app.ai.evals            # 오프라인 묶음
    python -m app.ai.evals --live     # ANTHROPIC_API_KEY 가 있을 때만 해석 보조 묶음을 실제 모델로. 비용이 든다

예시의 원문은 docs/08 과 샘플 시상안의 꼴이다. 실제 시상안, 실제 사람이 아니다.
연락처와 주민번호 꼴은 이 파일에 그대로 적지 않고 조각을 이어 만든다. 저장소의 개인정보 꼴 점검에 걸리지 않게 하려는 것이다.
"""
from __future__ import annotations

import json
import sys

from app.ai import checks, guides, plan_assist
from app.ai import context as aictx

_FAKE_PHONE = "010" + "-" + "1234" + "-" + "5678"
_FAKE_RRN = "900101" + "-" + "1234567"

CHECK_CASES = [
    # (이름, 입력 글, 출력 글, 막아야 하나, 막힐 종류)
    ("입력의 지급률을 그대로", "시상내용 월초P * 50%(13회차 지급)", "월초 보험료의 50% 를 13회차에 지급합니다 [1]", False, set()),
    ("천 단위 쉼표는 같은 숫자", "시상 150000원", "150,000원을 드립니다 [1]", False, set()),
    ("지어낸 지급률", "시상내용 월초P * 50%(13회차 지급)", "월초 보험료의 70% 를 드립니다 [1]", True, {"number"}),
    ("계산한 금액", "보험료 100000원, 지급률 200%", "200000원을 받습니다 [1]", True, {"number"}),
    ("없는 근거 번호", "시상내용 200% (익월)", "익월에 200% 입니다 [7]", True, {"citation"}),
    ("상품 권유", "건강보험 200%", "건강보험이 더 유리합니다. 가입하세요 [1]", True, {"forbidden"}),
    ("단정 표현", "건강보험 200%", "무조건 200% 를 받습니다 [1]", True, {"forbidden"}),
]

MASK_CASES = [
    ("주민번호", f"고객 주민번호 {_FAKE_RRN} 입니다", ["900101", "1234567"]),
    ("휴대폰", f"연락처 {_FAKE_PHONE} 로 주세요", ["1234", "5678"]),
    ("이름", "홍가상 고객의 증권 요약", ["홍가상"]),
]

PLAN_CASES = [
    {"name": "정률 13회차", "line": {"line_id": 0, "insurer_name": "샘플생명", "recipient_raw": "FC", "category_raw": "판매활성화 시책",
                                   "period_raw": "2026년 10월", "criteria_raw": "종신보험 전 상품", "reward_raw": "월초P * 50%(13회차 지급)",
                                   "recipient_level": "fc"},
     "gold": {"line_kind": "rate_promo", "recipient_level": "fc", "period_from": None, "period_to": None, "pay_timing": "inst13",
              "components": [{"basis": "initial_premium", "rate": 50, "pay_timing": "inst13", "condition": None}],
              "amount_status": "원문", "quote": "월초P * 50%(13회차 지급)", "note": None}},
    {"name": "금액이 그림에만", "line": {"line_id": 0, "insurer_name": "대한생명", "recipient_raw": "FC", "category_raw": "업적 구간 시책",
                                      "period_raw": "2026년 10월", "criteria_raw": "※ 구간별 중복지급 없음", "reward_raw": "", "recipient_level": "fc"},
     "gold": {"line_kind": "tier", "recipient_level": "fc", "period_from": None, "period_to": None, "pay_timing": "unspecified",
              "components": [], "amount_status": plan_assist.SOURCE_CHECK, "quote": "업적 구간 시책", "note": None},
     # 모델이 지급률을 지어내도 원문 확인으로 덮어써야 한다
     "model_says": {"line_kind": "tier", "components": [{"basis": "premium", "rate": 300}], "amount_status": "원문", "quote": "업적 구간 시책"}},
    {"name": "지급 시점만", "line": {"line_id": 0, "insurer_name": "한빛손해", "recipient_raw": "FC", "category_raw": "인보험 주차 시상",
                                  "period_raw": "2026년 10월 01일 ~ 11일", "criteria_raw": "", "reward_raw": "13회차 유지", "recipient_level": "fc"},
     "gold": {"line_kind": "unknown", "recipient_level": "fc", "period_from": None, "period_to": None, "pay_timing": "inst13",
              "components": [], "amount_status": plan_assist.SOURCE_CHECK, "quote": "13회차 유지", "note": None}},
]

COMPARE_KEYS = ("line_kind", "pay_timing", "amount_status")


def run_checks() -> list:
    out = []
    for name, src, output, should_block, kinds in CHECK_CASES:
        c = checks.check(output, allowed_numbers=aictx.numbers(src) | {"1"}, forbidden=guides.forbidden("common", "recommend"))
        if "[7]" in output:            # 근거 번호 점검은 근거 줄 하나짜리 가짜 컨텍스트로 본다
            fake = aictx.Context("plan_qa", "plan_qa", "", None, "", src, evidence=[aictx.Evidence(1, "x", src)], numbers=aictx.numbers(src))
            c = checks.check(output, fake)
        ok = (not c.ok) == should_block and kinds <= c.kinds()
        out.append({"suite": "check", "name": name, "ok": ok})
    return out


def run_masks() -> list:
    out = []
    for name, text, must_not in MASK_CASES:
        masked = aictx.mask(text, {"customer": ["홍가상"]})
        out.append({"suite": "mask", "name": name, "ok": not any(s in masked for s in must_not)})
    return out


def _compare(draft: dict | None, gold: dict) -> bool:
    if draft is None:
        return False
    if any(draft.get(k) != gold.get(k) for k in COMPARE_KEYS):
        return False
    got = [(c["basis"], float(c["rate"])) for c in draft.get("components") or []]
    want = [(c["basis"], float(c["rate"])) for c in gold.get("components") or []]
    return got == want


def run_plan(reply_fn=None) -> list:
    """해석 보조 묶음. reply_fn(case, system, user) 가 모델 답 글을 돌려준다. 없으면 정답을 답하는 가짜다."""
    out = []
    for case in PLAN_CASES:
        line = {**{k: None for k in ("period_from", "period_to", "parse_note")}, "line_kind": "unknown", "pay_timing": "unspecified", **case["line"]}
        ctx = {"role": "main", "login_id": "eval", "agent_ids": set()}
        c = aictx.assemble(plan_assist.FEATURE, ctx, task="[1] 의 해석할 줄을 해석해 JSON 으로 답합니다.",
                           evidence=[{"source": "eval", "text": "해석할 줄 " + plan_assist.raw_text(line)}])
        if reply_fn is None:
            text = json.dumps(case.get("model_says") or case["gold"], ensure_ascii=False)
        else:
            text = reply_fn(case, c.system, c.user)
        parsed = checks.parse_json(text)
        draft, problems = plan_assist.normalize(parsed or {}, line)
        chk = checks.check(plan_assist._free_text(draft), c)
        ok = parsed is not None and chk.ok and not problems and _compare(draft, case["gold"])
        out.append({"suite": "plan", "name": case["name"], "ok": ok})
    return out


def run_all(reply_fn=None) -> dict:
    rows = run_checks() + run_masks() + run_plan(reply_fn)
    return {"total": len(rows), "passed": sum(r["ok"] for r in rows), "rows": rows,
            "versions": {k: v["version"] for k, v in guides.versions().items()}}


def _live_reply_fn():
    import os

    from app import db
    from app.ai.client import AIClient

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY 가 없어 실제 평가를 하지 않습니다")
    con = db.connect(os.environ.get("APP_DB", db.DEFAULT_DB))
    cli = AIClient(con)

    def fn(case, system, user):
        rep = cli.call(plan_assist.FEATURE, system=system, user=user, login_id="eval", max_tokens=1200)
        return rep.text if rep.ok else ""
    return fn


if __name__ == "__main__":
    res = run_all(_live_reply_fn() if "--live" in sys.argv else None)
    for r in res["rows"]:
        print(f"[{'통과' if r['ok'] else '실패'}] {r['suite']} {r['name']}")
    print(f"{res['passed']}/{res['total']} 통과")
    sys.exit(0 if res["passed"] == res["total"] else 1)
