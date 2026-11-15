"""앱 안 AI 기능의 화면이 함께 쓰는 것. 꺼짐 안내, 클라이언트, 이름표, 시상안 해석 보조의 줄 상세 묶음.

AI 를 부르는 일과 점검, 기록은 app/ai 가 한다. 여기서는 그 결과를 화면에 맞게 고르기만 하고 계산하지 않는다.

- 키가 없으면 AI 화면마다 OFF 안내를 보이고 AI 를 부르는 단추를 막는다. 다른 기능은 그대로 쓴다.
- 클라이언트는 app/ops/jobs.ai_client 하나에서 얻는다. 워커의 심사 작업과 화면이 같은 입구를 쓰고, 시험은 그 함수를 바꿔 가짜를 넣는다.
- 제작지침이 아직 없는 화면(제안 리포트, 카드뉴스)은 planned.html 이 guide_wait 로 제작지침 대기 안내를 보인다.
- 시상안 해석 보조는 /data/plan 의 줄 상세에 붙는다. 승인해도 원장 값은 바뀌지 않는다. 초안을 원장 칸에 옮기는 일은 열린 결정이다.
"""
from __future__ import annotations

from app.ai import checks, guides, plan_assist, review, store
from app.ai import client as aiclient
from app.loader import incentive_plan as ip
from app.ops import jobs
from app.web.deps import templates

OFF = "AI 기능이 꺼져 있습니다. 키와 월 한도(D-26)가 정해지면 켜집니다."
LEDGER_SAFE = "승인해도 원장 값은 바뀌지 않습니다. 원장 줄의 승인은 아래 표에서 따로 합니다. 초안을 원장 칸에 옮기는 일은 돈 계산 기준에 닿아 열린 결정으로 두었습니다."
GATE_NOTE = review.GATE + " D-15 제안대로 고객에게 나가는 글만 필수 관문입니다."
SEAT_WAIT = "명세 대기"

REVIEW_STATUS_KO = {"running": "심사 중", "passed": "통과", "stopped": "멈춤", "approved": "승인", "rejected": "반려"}
REVIEW_STATUS_CHIP = {"running": "warn", "passed": "good", "stopped": "warn", "approved": "good", "rejected": "bad"}
VERDICT_KO = {"pass": "통과", "revise": "고침 제안", "reject": "반려 의견", "error": "형식 오류"}
VERDICT_CHIP = {"pass": "good", "revise": "warn", "reject": "bad", "error": "bad"}
DRAFT_STATUS_KO = {"draft": "초안", "blocked": "자동 점검에서 막힘", "off": "AI 꺼짐", "budget": "한도 닿음", "error": "부르지 못함"}
DECISION_KO = {"approved": "승인", "rejected": "반려"}

# 제작지침을 기다리는 메뉴. 메뉴 항목 id 와 app/ai/guides 의 지침 열쇠
GUIDE_WAIT = {"sales-report": "report", "sales-cardnews": "cardnews"}


def client(con):
    return jobs.ai_client(con)


def enabled(con) -> bool:
    return bool(client(con).enabled)


def guide_wait(item) -> dict | None:
    """제작지침이 확정되지 않은 메뉴면 그 지침의 이름과 버전. planned.html 이 부른다."""
    key = GUIDE_WAIT.get((item or {}).get("id"))
    g = guides.GUIDES.get(key) if key else None
    if not g or g["status"] == "confirmed":
        return None
    return {"title": g["title"], "version": g["version"]}


templates.env.globals["guide_wait"] = guide_wait


# ---------------------------------------------------------------- 시상안 해석 보조
def _draft_rows(draft: dict | None) -> list:
    """초안을 이름표와 값의 줄로. 값은 초안 그대로 옮기고 계산하지 않는다."""
    if not draft:
        return []
    from app.web.routes.data import BASIS_KO
    period = " ~ ".join(x for x in (draft.get("period_from"), draft.get("period_to")) if x) or "원문에 없음"
    rows = [("유형", ip.KIND_KO.get(draft.get("line_kind"), draft.get("line_kind") or "-")),
            ("수령 계층", ip.LEVEL_KO.get(draft.get("recipient_level"), draft.get("recipient_level") or "-")),
            ("기간", period),
            ("지급 시점", ip.TIMING_KO.get(draft.get("pay_timing"), draft.get("pay_timing") or "-")),
            ("금액 상태", draft.get("amount_status") or "-")]
    comps = []
    for c in draft.get("components") or []:
        t = f"{BASIS_KO.get(c.get('basis'), c.get('basis'))} × {c.get('rate')}%" if c.get("rate") not in (None, "") else BASIS_KO.get(c.get("basis"), "-")
        if c.get("pay_timing") and c["pay_timing"] != "unspecified":
            t += f" · {ip.TIMING_KO.get(c['pay_timing'], c['pay_timing'])}"
        if c.get("condition"):
            t += f" · {c['condition']}"
        comps.append(t)
    rows.append(("지급률 조각", " / ".join(comps) if comps else ("원문 확인" if draft.get("amount_status") == plan_assist.SOURCE_CHECK else "없음")))
    return rows


def _output_view(o: dict) -> dict:
    """ai_output 한 줄을 화면용으로. 자동 점검에서 막힌 초안은 내용을 보이지 않고 문제만 보인다."""
    ev = ((o.get("context") or {}).get("evidence")) or []
    shown = o["status"] == "draft"
    return {"output_id": o["output_id"], "status": o["status"], "status_ko": DRAFT_STATUS_KO.get(o["status"], o["status"]),
            "created_at": o["created_at"], "login_id": o["login_id"], "guide_version": o["guide_version"],
            "rows": _draft_rows(o.get("parsed")) if shown else [], "quote": (o.get("parsed") or {}).get("quote") if shown else None,
            "note": (o.get("parsed") or {}).get("note") if shown else None,
            "problems_ko": checks.describe(o.get("problems") or []), "evidence": ev,
            "decision": o.get("decision"), "decision_ko": DECISION_KO.get(o.get("decision")), "decided_by": o.get("decided_by"),
            "decided_at": o.get("decided_at"), "decision_reason": o.get("decision_reason"),
            "can_decide": o["status"] == "draft" and not o.get("decision")}


def plan_panel(con, doc_id: int, line_id: int) -> dict | None:
    """시상안 줄 상세의 AI 해석 묶음. 그 시상안의 줄이 아니면 None."""
    r = con.execute("SELECT * FROM incentive_plan_line WHERE line_id=? AND plan_doc_id=?", (line_id, doc_id)).fetchone()
    if not r:
        return None
    line = dict(r)
    hist = [_output_view(o) for o in plan_assist.history(con, line_id)]
    return {"line": line, "raw": [(ko, (line.get(col) or "").strip() or "(빈칸)") for ko, col in plan_assist.RAW_COLS],
            "latest": hist[0] if hist else None, "older": hist[1:6], "on": enabled(con), "off": OFF, "ledger_safe": LEDGER_SAFE}


def plan_interpret(con, ctx, line_id: int) -> tuple:
    """AI 해석 초안을 만들고 화면에 띄울 (ok 또는 bad, 한 줄) 을 돌려준다. 결과는 ai_output 에 남아 줄 상세가 읽는다."""
    if not enabled(con):
        return "bad", OFF
    out = plan_assist.interpret_line(con, ctx, line_id, client=client(con))
    if out["status"] == "draft":
        return "ok", "AI 해석 초안을 만들었습니다. 원문과 대조한 뒤 승인하거나 반려해 주세요."
    return "bad", out.get("message") or aiclient.ERROR_MESSAGE


def plan_decide(con, ctx, output_id: int, approve: bool, reason: str | None) -> str:
    plan_assist.decide(con, ctx, output_id, approve, reason=reason)
    return "해석 초안을 승인했습니다. 원장 값은 바뀌지 않았습니다." if approve else "해석 초안을 반려했습니다."


def output_line(con, output_id: int) -> int | None:
    """해석 초안이 가리키는 원장 줄 번호."""
    o = store.get_output(con, output_id)
    if not o or o["feature"] != plan_assist.FEATURE or o["ref_kind"] != "plan_line":
        return None
    return o["ref_id"]
