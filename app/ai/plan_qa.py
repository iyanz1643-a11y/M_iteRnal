"""시상안 묻고 답하기. docs/13 1.4 의 둘째 줄이다.

- D-27 제안대로 처음에는 메인 관리자만 쓴다. 설계사에게 여는 일은 메인 관리자가 한 달 써 본 뒤 정한다. FEATURE_ROLES 한 곳만 바꾸면 열린다.
- 원장 전체를 넣지 않는다. 질문에서 보험사, 상품군, 기간을 뽑아 맞는 승인 줄만 원문 그대로 번호를 붙여 넣는다.
- 넣지 않는 것: 승인 전의 줄, 다른 사람의 실적, 총괄 몫과 지사 몫. 그래서 수령 계층이 FC 와 비고인 줄만 쓴다.
- 근거 줄이 없으면 API 를 부르지 않고 모른다고 답한다. 멈추는 조건의 하나다.
- 답은 근거 번호가 있어야 하고, 숫자는 근거와 질문에 있는 것만, 상품을 권하는 말은 막는다. 막히면 답 대신 근거 줄만 보인다.
"""
from __future__ import annotations

import re
from datetime import date

from app.ai import checks, guides, store
from app.ai import context as aictx
from app.ai.client import AIClient

FEATURE = "plan_qa"
LEVELS = ("fc", "note")
NOTICE = "시상 원안과 다르면 원안이 우선합니다. 금액과 지급 여부는 원안과 정산을 따릅니다."
NO_EVIDENCE = "승인된 시상안에서 근거가 되는 줄을 찾지 못했습니다. 원안을 확인해 주세요."
BLOCKED = "답을 자동 점검에서 확인하지 못해 보여 드리지 않습니다. 아래 근거 줄을 직접 확인해 주세요."
CLOSED = "시상안 묻고 답하기는 지금 메인 관리자만 씁니다."
# 상품군 낱말. 질문과 줄의 분류, 달성기준에 함께 있으면 맞는 줄로 본다.
GROUPS = ["종신", "건강", "인보험", "재물", "운전자", "암", "치아", "간병", "연금", "저축", "실손", "자동차", "정기", "어린이", "태아",
          "보장성", "변액", "상해", "화재", "펫"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def parse_question(con, question: str, today: date) -> dict:
    """질문에서 보험사, 상품군, 귀속월을 뽑는다. 못 뽑은 것은 비워 둔다."""
    q = _norm(question)
    insurers = sorted({r[0] for r in con.execute("SELECT DISTINCT insurer_name FROM incentive_plan_line") if r[0] and _norm(r[0]) in q})
    if not insurers:                         # 보험사 이름의 앞 두 글자로도 찾는다. 예: 한빛
        insurers = sorted({r[0] for r in con.execute("SELECT DISTINCT insurer_name FROM incentive_plan_line")
                           if r[0] and len(_norm(r[0])) >= 2 and _norm(r[0])[:2] in q})
    groups = [g for g in GROUPS if g in q]
    yms = set()
    for y, m in re.findall(r"(?:(20[0-9]{2})년)?([0-9]{1,2})월", q):
        mm = int(m)
        if 1 <= mm <= 12:
            yy = int(y) if y else (today.year if mm <= today.month + 1 else today.year - 1)
            yms.add(f"{yy}{mm:02d}")
    return {"insurers": insurers, "groups": groups, "yms": sorted(yms)}


def find_lines(con, filters: dict, limit: int = 8) -> list:
    """맞는 승인 줄. 보험사를 말했으면 그 보험사만, 귀속월을 말했으면 그 달의 시상안이나 기간이 겹치는 줄만 쓴다."""
    rows = [dict(r) for r in con.execute(
        f"""SELECT l.*, d.period_ym FROM incentive_plan_line l JOIN incentive_plan_doc d ON d.plan_doc_id=l.plan_doc_id
            WHERE l.review_status='approved' AND d.status!='superseded' AND l.recipient_level IN ({','.join('?' * len(LEVELS))})
            ORDER BY d.period_ym DESC, l.plan_doc_id, l.sheet, l.row_no""", LEVELS)]
    if filters["insurers"]:
        rows = [r for r in rows if r["insurer_name"] in filters["insurers"]]
    if filters["yms"]:
        def hit(r):
            for ym in filters["yms"]:
                first = f"{ym[:4]}-{ym[4:]}-01"
                last = f"{ym[:4]}-{ym[4:]}-31"
                if r["period_ym"] == ym or (r["period_from"] and r["period_to"] and r["period_from"] <= last and r["period_to"] >= first):
                    return True
            return False
        rows = [r for r in rows if hit(r)]
    if filters["groups"]:
        def score(r):
            hay = _norm((r["category_raw"] or "") + (r["criteria_raw"] or ""))
            return sum(1 for g in filters["groups"] if g in hay)
        scored = [r for r in rows if score(r) > 0 or r["recipient_level"] == "note"]
        rows = sorted(scored, key=lambda r: -score(r)) if any(score(r) for r in scored) else []
    # 비고 줄은 같은 보험사의 본문 줄이 있을 때만 붙인다
    main = [r for r in rows if r["recipient_level"] != "note"]
    keep_ins = {r["insurer_name"] for r in main}
    notes = [r for r in rows if r["recipient_level"] == "note" and r["insurer_name"] in keep_ins]
    return (main[:limit] + notes)[: limit + 2]


def _line_text(r: dict) -> str:
    cols = [("귀속월", r.get("period_ym")), ("보험사", r.get("insurer_name")), ("시상대상", r.get("recipient_raw")), ("분류", r.get("category_raw")),
            ("기간", r.get("period_raw")), ("달성기준", r.get("criteria_raw")), ("시상내용", r.get("reward_raw"))]
    return " | ".join(f"{k} {str(v).strip()}" for k, v in cols if v not in (None, ""))


def ask(con, ctx: dict, question: str, *, today: date | None = None, client: AIClient | None = None, limit: int = 8) -> dict:
    """질문 하나에 답한다. 화면이 부른다.

    돌려주는 것: {"status": answered|blocked|no_evidence|closed|off|budget|error, "answer", "message", "notice",
                 "evidence": [{"no", "line_id", "text"}], "problems_ko", "output_id", "guide_version"}
    """
    store.ensure(con)
    if ctx.get("role") not in aictx.FEATURE_ROLES[FEATURE]:
        return {"status": "closed", "answer": None, "message": CLOSED, "notice": NOTICE, "evidence": [], "problems_ko": [], "output_id": None,
                "guide_version": guides.GUIDES[FEATURE]["version"]}
    question = (question or "").strip()
    if not question:
        raise ValueError("질문을 적어 주세요")
    today = today or date.today()
    filters = parse_question(con, question, today)
    lines = find_lines(con, filters, limit)
    org = (ctx.get("home_org") or {}).get("name") or "총괄"
    c = aictx.assemble(FEATURE, ctx, task=question, today=today,
                       evidence=[{"source": f"plan_line:{r['line_id']}", "text": _line_text(r)} for r in lines],
                       facts={"질문한 사람의 소속": org})
    ev = [{"no": e.no, "line_id": int(e.source.split(":")[1]), "text": e.text} for e in c.evidence]
    base = {"notice": NOTICE, "evidence": ev, "guide_version": c.guide_version, "filters": filters}
    if not lines:
        oid = store.save_output(con, feature=FEATURE, login_id=ctx.get("login_id"), context=c, status="no_evidence", output_text=NO_EVIDENCE)
        return {**base, "status": "no_evidence", "answer": NO_EVIDENCE, "message": NO_EVIDENCE, "problems_ko": [], "output_id": oid}
    client = client or AIClient(con)
    rep = client.call(FEATURE, system=c.system, user=c.user, login_id=ctx.get("login_id"), guide_version=c.guide_version,
                      input_hash=c.input_hash, max_tokens=800)
    if not rep.ok:
        oid = store.save_output(con, feature=FEATURE, login_id=ctx.get("login_id"), context=c, status=rep.status, call_id=rep.call_id)
        return {**base, "status": rep.status, "answer": None, "message": rep.message, "problems_ko": [], "output_id": oid}
    chk = checks.check(rep.text, c, forbidden=guides.forbidden("common", "recommend"), require_citation=True)
    status = "answered" if chk.ok else "blocked"
    oid = store.save_output(con, feature=FEATURE, login_id=ctx.get("login_id"), context=c, status=status, output_text=rep.text,
                            problems=chk.problems, call_id=rep.call_id)
    return {**base, "status": status, "answer": rep.text.strip() if chk.ok else None, "message": "" if chk.ok else BLOCKED,
            "problems_ko": checks.describe(chk.problems), "output_id": oid}
