"""시상안 해석 보조. docs/13 1.4 의 첫 줄과 docs/08 이다.

넣는 것: 사람 확인이 필요한 원문 한 줄, 같은 보험사의 앞뒤 줄, 해석 예시, 적재기의 기계 해석.
넣지 않는 것: 실적, 사람 정보.
나오는 것: 유형, 기간, 지급률의 구조화된 초안과 근거 문구. 메인 관리자가 원문과 대조해 승인한다.

- 금액이나 지급률이 원문 글에 없으면 amount_status 를 "원문 확인" 으로 두고 지급률 조각을 비운다. 모델이 무엇을 내도 덮어쓴다.
- 초안의 숫자는 원문과 기계 해석에 있는 것만 받는다. 아니면 blocked 로 남기고 사람에게 보이지 않는다.
- 승인은 ai_output 의 결재만 남긴다. incentive_plan_line 의 해석 칸과 승인 상태는 바꾸지 않는다.
  원장 줄의 승인은 지금처럼 incentive_plan.review 로 한다. 초안을 원장 칸에 옮기는 일은 돈 계산 기준에 닿아 열어 둔다.
"""
from __future__ import annotations

import json
import re

from app.ai import checks, guides, store
from app.ai import context as aictx
from app.ai.client import AIClient

FEATURE = "plan_interpret"
SOURCE_CHECK = "원문 확인"
KINDS = {"rate_promo", "activity", "consecutive", "tier", "note", "unknown"}
LEVELS = {"ga", "hq", "branch", "fc", "note", "unknown"}
TIMINGS = {"next_month", "inst13", "next_month+inst13", "year1", "year2", "mixed", "unspecified"}
BASES = {"initial_premium", "monthly_premium", "paid_premium", "base_premium", "premium", "unspecified"}
AMOUNT = re.compile(r"[0-9][0-9,.]*\s*(?:%|％|원|만)")
RAW_COLS = [("보험사", "insurer_name"), ("시상대상", "recipient_raw"), ("분류", "category_raw"), ("기간", "period_raw"),
            ("달성기준", "criteria_raw"), ("시상내용", "reward_raw")]

# 해석 예시. 원문은 docs/08 2장의 꼴과 샘플 시상안의 꼴이다. 실제 시상안이 아니다.
EXAMPLES = [
    {"원문": "분류 판매활성화 시책 | 기간 2026년 10월 | 시상내용 월초P * 50%(13회차 지급)",
     "해석": {"line_kind": "rate_promo", "pay_timing": "inst13", "components": [{"basis": "initial_premium", "rate": 50, "pay_timing": "inst13"}],
              "amount_status": "원문"}},
    {"원문": "분류 업적 구간 시책 | 기간 2026년 10월 | 달성기준 ※ 구간별 중복지급 없음 | 시상내용 (빈칸)",
     "해석": {"line_kind": "tier", "components": [], "amount_status": SOURCE_CHECK, "note": "금액이 원문 그림에만 있음"}},
]


def raw_text(line: dict) -> str:
    return " | ".join(f"{ko} {(line.get(col) or '').strip() or '(빈칸)'}" for ko, col in RAW_COLS)


def amount_in_text(line: dict) -> bool:
    """시상내용이나 달성기준의 글에 지급률이나 금액이 있는지. 없으면 원문 그림에만 있는 것으로 본다."""
    return bool(AMOUNT.search(line.get("reward_raw") or ""))


def candidates(con, plan_doc_id=None, limit=200) -> list:
    """사람 확인이 필요한 원장 줄. 해석 보조 화면의 목록이다. 이미 결재한 초안이 있으면 그 상태를 붙인다."""
    store.ensure(con)
    cond, args = ("AND l.plan_doc_id=?", (plan_doc_id,)) if plan_doc_id else ("AND d.status!='superseded'", ())
    rows = [dict(r) for r in con.execute(
        f"""SELECT l.line_id, l.plan_doc_id, d.period_ym, l.sheet, l.row_no, l.insurer_name, l.recipient_level, l.line_kind, l.parse_status,
                   l.parse_note, l.review_status
            FROM incentive_plan_line l JOIN incentive_plan_doc d ON d.plan_doc_id=l.plan_doc_id
            WHERE l.parse_status='manual' {cond} ORDER BY l.plan_doc_id, l.sheet, l.row_no LIMIT ?""", (*args, limit))]
    for r in rows:
        last = con.execute("SELECT output_id, status, decision FROM ai_output WHERE feature=? AND ref_kind='plan_line' AND ref_id=? ORDER BY output_id DESC LIMIT 1",
                           (FEATURE, r["line_id"])).fetchone()
        r["draft"] = dict(last) if last else None
    return rows


def _line(con, line_id) -> dict:
    r = con.execute("SELECT * FROM incentive_plan_line WHERE line_id=?", (line_id,)).fetchone()
    if not r:
        raise ValueError("없는 원장 줄입니다")
    return dict(r)


def _machine(con, line: dict) -> dict:
    comps = [dict(c) for c in con.execute("SELECT option_label, basis, rate, pay_timing, pay_condition FROM incentive_plan_component WHERE line_id=? ORDER BY seq",
                                          (line["line_id"],))]
    return {"수령 계층": line["recipient_level"], "유형": line["line_kind"], "기간": f"{line['period_from'] or '없음'} ~ {line['period_to'] or '없음'}",
            "지급 시점": line["pay_timing"], "지급률 조각": json.dumps(comps, ensure_ascii=False) if comps else "없음",
            "기계 해석 메모": line["parse_note"] or "없음"}


def build_context(con, ctx: dict, line_id: int) -> tuple:
    line = _line(con, line_id)
    near = [dict(r) for r in con.execute(
        """SELECT * FROM incentive_plan_line WHERE plan_doc_id=? AND sheet=? AND insurer_name=? AND line_id!=? AND ABS(row_no-?)<=2
           ORDER BY row_no""", (line["plan_doc_id"], line["sheet"], line["insurer_name"], line_id, line["row_no"]))]
    evidence = [{"source": f"plan_line:{line_id}", "text": "해석할 줄 " + raw_text(line)}]
    evidence += [{"source": f"plan_line:{n['line_id']}", "text": "앞뒤 줄 " + raw_text(n)} for n in near]
    facts = {f"기계 해석 {k}": v for k, v in _machine(con, line).items()}
    task = ("[1] 의 해석할 줄을 해석해 JSON 으로 답합니다. 앞뒤 줄은 병합 셀과 비고를 이해하는 데만 씁니다. "
            "<예시> 는 꼴만 보여 줍니다. 예시의 숫자를 쓰지 않습니다.")
    c = aictx.assemble(FEATURE, ctx, task=task, evidence=evidence, facts=facts,
                       examples=[json.dumps(e, ensure_ascii=False) for e in EXAMPLES])
    return line, c


def normalize(parsed: dict, line: dict) -> tuple:
    """모델의 초안을 정해진 값 안으로 맞추고, 원문 확인을 덮어쓴다. (초안, 문제 목록)."""
    problems = []
    d = {
        "line_kind": parsed.get("line_kind") if parsed.get("line_kind") in KINDS else "unknown",
        "recipient_level": parsed.get("recipient_level") if parsed.get("recipient_level") in LEVELS else line["recipient_level"],
        "period_from": parsed.get("period_from") or None,
        "period_to": parsed.get("period_to") or None,
        "pay_timing": parsed.get("pay_timing") if parsed.get("pay_timing") in TIMINGS else "unspecified",
        "components": [],
        "amount_status": "원문",
        "quote": (parsed.get("quote") or "").strip() or None,
        "note": parsed.get("note") or None,
    }
    for cp in parsed.get("components") or []:
        if not isinstance(cp, dict):
            continue
        d["components"].append({"basis": cp.get("basis") if cp.get("basis") in BASES else "unspecified", "rate": cp.get("rate"),
                                "pay_timing": cp.get("pay_timing") if cp.get("pay_timing") in TIMINGS else "unspecified",
                                "condition": cp.get("condition") or None})
    if not amount_in_text(line) or parsed.get("amount_status") == SOURCE_CHECK:
        d["amount_status"] = SOURCE_CHECK
        d["components"] = []                    # 금액이 원문 그림에만 있으면 지급률을 두지 않는다
    if d["quote"]:
        hay = re.sub(r"\s+", "", raw_text(line))
        if re.sub(r"\s+", "", d["quote"]) not in hay:
            problems.append({"kind": "quote", "detail": []})
    return d, problems


def _free_text(draft: dict) -> str:
    """숫자 점검에 넣을 칸. 정해진 값 목록의 칸(inst13, year1 같은 것)은 숫자가 아니라 뺀다."""
    vals = [draft.get("period_from"), draft.get("period_to"), draft.get("quote"), draft.get("note")]
    for cp in draft.get("components") or []:
        vals += [cp.get("rate"), cp.get("condition")]
    return "\n".join(str(v) for v in vals if v not in (None, ""))


def interpret_line(con, ctx: dict, line_id: int, client: AIClient | None = None) -> dict:
    """원장 한 줄의 해석 초안을 만든다. 화면이 부른다.

    돌려주는 것: {"status": draft|blocked|off|budget|error, "message", "draft", "problems", "problems_ko", "output_id",
                 "evidence": [{"no","source","text"}], "guide_version"}
    """
    line, c = build_context(con, ctx, line_id)
    client = client or AIClient(con)
    rep = client.call(FEATURE, system=c.system, user=c.user, login_id=ctx.get("login_id"), guide_version=c.guide_version,
                      input_hash=c.input_hash, max_tokens=1200)
    base = {"line_id": line_id, "guide_version": c.guide_version,
            "evidence": [{"no": e.no, "source": e.source, "text": e.text} for e in c.evidence]}
    if not rep.ok:
        oid = store.save_output(con, feature=FEATURE, login_id=ctx.get("login_id"), context=c, status=rep.status, call_id=rep.call_id,
                                ref_kind="plan_line", ref_id=line_id)
        return {**base, "status": rep.status, "message": rep.message, "draft": None, "problems": [], "problems_ko": [], "output_id": oid}
    parsed = checks.parse_json(rep.text)
    if parsed is None:
        problems = [{"kind": "format", "detail": []}]
        draft = None
    else:
        draft, problems = normalize(parsed, line)
        chk = checks.check(_free_text(draft), c, forbidden=guides.forbidden("common"))
        problems = chk.problems + problems
    status = "draft" if not problems else "blocked"
    oid = store.save_output(con, feature=FEATURE, login_id=ctx.get("login_id"), context=c, status=status, output_text=rep.text,
                            parsed=draft, problems=problems, call_id=rep.call_id, ref_kind="plan_line", ref_id=line_id)
    msg = "" if status == "draft" else "초안이 자동 점검을 통과하지 못해 보여 드리지 않습니다. 원문을 직접 해석해 주세요."
    return {**base, "status": status, "message": msg, "draft": draft if status == "draft" else None, "problems": problems,
            "problems_ko": checks.describe(problems), "output_id": oid}


def decide(con, ctx: dict, output_id: int, approve: bool, edited: dict | None = None, reason: str | None = None) -> dict:
    """메인 관리자가 원문과 대조해 초안을 승인하거나 반려한다. 고친 초안은 edited 로 받는다."""
    aictx.require_role(ctx, FEATURE)
    row = store.get_output(con, output_id)
    if not row or row["feature"] != FEATURE:
        raise ValueError("없는 해석 초안입니다")
    return store.decide_output(con, output_id, by=ctx["login_id"], approve=approve,
                               edited_text=json.dumps(edited, ensure_ascii=False) if edited else None, reason=reason)


def history(con, line_id: int) -> list:
    return store.list_outputs(con, feature=FEATURE, ref_kind="plan_line", ref_id=line_id)
