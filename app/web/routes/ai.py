"""앱 안 AI 기능의 화면. 모두 메인 관리자만 쓰고 모두 guard 를 거친다. 설계는 docs/13 1.4 와 2.4 다.

| 주소 | 하는 일 |
| --- | --- |
| GET /review/requests | 카드뉴스, 제안 리포트 서식, 안내문, 기준 문안, 랜딩 문구를 심사에 올리는 폼과 심사 작업의 상태 |
| POST /review/requests | 요청 파일을 저장소 uploads 의 review/ 에 두고 작업 review_submit 을 넣는다. 심사는 워커가 3회전까지 돈다 |
| GET /review/results | 심사 목록. ?id= 로 한 건의 회전별 형상화, 자리별 의견, 회전 수, 결재 |
| POST /review/results | 사람의 최종 결재. review.decide |
| GET, POST /data/plan-qa | 시상안 묻고 답하기. 답, 근거 줄 번호, 원안 우선 문구. D-27 제안대로 메인 관리자만 |
| GET, POST /org/ai | 기능별 이번 달 AI 사용량과 한도, 한도 고치기. client.usage, set_limit |

- 키가 없으면 모든 화면이 ai_view.OFF 안내를 보이고 AI 를 부르는 단추를 막는다. 지난 심사의 결재와 한도 고치기는 AI 없이 된다.
- 심사할 글은 작업 params 에 넣지 않는다. 작업 표에는 요청 파일의 키와 요청한 아이디만 남는다.
- 시상안 해석 보조는 /data/plan 의 줄 상세에 붙어 app/web/routes/data.py 가 ai_view 를 부른다.
- 금액과 선발은 AI 가 만들지 않는다. 사용량의 비용은 토큰으로 낸 추정이라 추정이라고 적는다.
"""
from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.ai import client as aiclient
from app.ai import context as aictx
from app.ai import guides, plan_qa, review, store
from app.ops import jobs
from app.ops import schema as ops_schema
from app.ops.storage import get_storage
from app.web import ai_view, auth
from app.web.deps import get_con, guard, page

router = APIRouter()
FLASH = "ai_flash"
REQ_PATH, RES_PATH, QA_PATH, USE_PATH = "/review/requests", "/review/results", "/data/plan-qa", "/org/ai"
REQ_KINDS = ("cardnews", "report_form", "notice", "standard_text", "landing")       # 화면에서 올리는 심사 종류. 리포트와 월간 보고서는 AI 초안이 올린다
TEXT_MAX, TITLE_MAX, QUESTION_MAX, REASON_MAX = 4000, 80, 300, 300
LIMIT_MAX = 10000.0


def _flash(request, kind, text):
    request.session[FLASH] = {"kind": kind, "text": text}


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ================================================================ 심사 요청
def _review_jobs(con, limit=20) -> list:
    ops_schema.ensure(con)
    rows = [jobs.get(con, r[0]) for r in con.execute("SELECT job_id FROM job WHERE kind='review_submit' ORDER BY job_id DESC LIMIT ?", (limit,))]
    ids = [j["result"]["review_id"] for j in rows if j and j.get("result") and j["result"].get("review_id")]
    titles = {}
    if ids:
        q = ",".join("?" * len(ids))
        store.ensure(con)
        for r in con.execute(f"SELECT review_id, kind, audience, title, status FROM ai_review WHERE review_id IN ({q})", ids):
            titles[r["review_id"]] = dict(r)
    for j in rows:
        rid = (j.get("result") or {}).get("review_id")
        j["review"] = titles.get(rid)
    return rows


@router.get("/review/requests", response_class=HTMLResponse)
def review_requests(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    store.ensure(con)
    pre = None
    src = _int(request.query_params.get("from"))
    if src:
        d = review.get_review(con, src)
        if d:
            pre = {"kind": d["kind"] if d["kind"] in REQ_KINDS else "notice", "audience": d["audience"], "title": d["title"] or "",
                   "text": d["final_text"] or d["current_text"] or "", "parent_id": d["review_id"]}
    return page(request, ctx, "review_requests.html", on=ai_view.enabled(con), OFF=ai_view.OFF, KINDS=review.KINDS, REQ_KINDS=REQ_KINDS,
                AUDIENCES=review.AUDIENCES, rows=_review_jobs(con), STATUS_KO=jobs.STATUS_KO, R_STATUS_KO=ai_view.REVIEW_STATUS_KO,
                R_CHIP=ai_view.REVIEW_STATUS_CHIP, pre=pre, TEXT_MAX=TEXT_MAX, TITLE_MAX=TITLE_MAX, GATE=ai_view.GATE_NOTE,
                MAX_ROUNDS=review.MAX_ROUNDS, flash=request.session.pop(FLASH, None))


@router.post("/review/requests", response_class=HTMLResponse)
async def review_requests_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    kind, audience = str(form.get("kind") or ""), str(form.get("audience") or "")
    title = str(form.get("title") or "").strip()[:TITLE_MAX] or None
    text = str(form.get("text") or "").strip()
    parent = _int(form.get("parent_id"))
    if kind not in REQ_KINDS or audience not in review.AUDIENCES:
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    err = None
    if not ai_view.enabled(con):
        err = ai_view.OFF
    elif not text:
        err = "심사할 글을 적어 주세요."
    elif len(text) > TEXT_MAX:
        err = f"심사할 글은 {TEXT_MAX}자까지 받습니다."
    else:
        if aictx.has_pii(text, title):
            err = review.PII_FOUND
    if parent and not review.get_review(con, parent):
        parent = None
    if err:
        _flash(request, "bad", err)
        return RedirectResponse(REQ_PATH + (f"?from={parent}" if parent else ""), status_code=303)
    key = f"review/{secrets.token_urlsafe(24)}.json"
    body = {"kind": kind, "audience": audience, "title": title, "text": text, "parent_id": parent}
    get_storage().put_at("uploads", key, json.dumps(body, ensure_ascii=False).encode("utf-8"))
    try:
        jid = jobs.enqueue(con, "review_submit", {"key": key, "by": ctx["login_id"]}, by=ctx["login_id"], max_attempts=1)
    except jobs.JobError as e:
        get_storage().delete("uploads", key)
        raise HTTPException(400, str(e)) from None
    auth.log(con, ctx["login_id"], "ai_review_request", f"job:{jid} kind={kind} audience={audience}")
    _flash(request, "ok", f"심사 작업 {jid}번을 넣었습니다. 워커가 {review.MAX_ROUNDS}회전까지 돌고 나면 심사 결과에 보입니다.")
    return RedirectResponse(REQ_PATH, status_code=303)


# ================================================================ 심사 결과와 결재
def _seat_view(d: dict) -> list:
    """회전마다 정의된 자리의 의견과 명세가 없는 자리. 명세가 없는 자리는 부르지 않았고 명세 대기로 보인다."""
    panel = review.load_panel()
    out = []
    for rd in d["rounds_detail"]:
        by_key = {o["seat_key"]: o for o in rd["opinions"]}
        seats = []
        for s in panel["seats"]:
            o = by_key.get(s.key)
            seats.append({"name": s.name, "instinct": s.instinct, "defined": s.defined, "op": o,
                          "verdict_ko": ai_view.VERDICT_KO.get(o["verdict"]) if o else None,
                          "chip": ai_view.VERDICT_CHIP.get(o["verdict"], "") if o else ""})
        out.append({"round": rd["round"], "shape": rd["shape"], "seats": seats})
    return out


@router.get("/review/results", response_class=HTMLResponse)
def review_results(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    store.ensure(con)
    st = request.query_params.get("status") or ""
    if st and st not in ai_view.REVIEW_STATUS_KO:
        st = ""
    d = None
    rid = _int(request.query_params.get("id"))
    if rid:
        d = review.get_review(con, rid)
        if not d:
            raise HTTPException(404, "없는 심사입니다.")
        d["rounds_view"] = _seat_view(d)
    m = review.measures(con)
    return page(request, ctx, "review_results.html", rows=review.list_reviews(con, st or None, 100), d=d, f_status=st, m=m,
                on=ai_view.enabled(con), OFF=ai_view.OFF, R_STATUS_KO=ai_view.REVIEW_STATUS_KO, R_CHIP=ai_view.REVIEW_STATUS_CHIP,
                GATE=ai_view.GATE_NOTE, REEDIT=review.REEDIT, SEAT_WAIT=ai_view.SEAT_WAIT, MAX_ROUNDS=review.MAX_ROUNDS,
                panel_version=review.load_panel()["version"], REASON_MAX=REASON_MAX, flash=request.session.pop(FLASH, None))


@router.post("/review/results", response_class=HTMLResponse)
async def review_results_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    rid = _int(form.get("review_id"))
    action = form.get("action")
    if not rid or action not in ("approve", "reject"):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    final = str(form.get("final_text") or "").strip() or None
    reason = str(form.get("reason") or "").strip()[:REASON_MAX] or None
    try:
        out = review.decide(con, ctx, rid, action == "approve", final_text=final if action == "approve" else None, reason=reason)
    except ValueError as e:
        _flash(request, "bad", str(e))
    else:
        auth.log(con, ctx["login_id"], f"ai_review_{action}", f"review:{rid}" + (" edited" if out.get("human_edited") else ""))
        _flash(request, "ok", "승인했습니다. 승인한 글만 밖으로 내보낼 수 있습니다." if action == "approve" else "반려했습니다.")
    return RedirectResponse(f"{RES_PATH}?id={rid}", status_code=303)


# ================================================================ 시상안 묻고 답하기
def _qa_page(request, ctx, con, result=None, question="", status_code=200):
    return page(request, ctx, "data_plan_qa.html", status_code=status_code, on=ai_view.enabled(con), OFF=ai_view.OFF, NOTICE=plan_qa.NOTICE,
                result=result, question=question, QUESTION_MAX=QUESTION_MAX, guide_version=guides.GUIDES[plan_qa.FEATURE]["version"])


@router.get("/data/plan-qa", response_class=HTMLResponse)
def plan_qa_page(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return _qa_page(request, ctx, con)


@router.post("/data/plan-qa", response_class=HTMLResponse)
async def plan_qa_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    q = str(form.get("question") or "").strip()
    if not q:
        return _qa_page(request, ctx, con, result={"status": "empty", "message": "질문을 적어 주세요."}, status_code=400)
    if len(q) > QUESTION_MAX:
        return _qa_page(request, ctx, con, result={"status": "empty", "message": f"질문은 {QUESTION_MAX}자까지 받습니다."}, question=q[:QUESTION_MAX],
                        status_code=400)
    out = plan_qa.ask(con, ctx, q, client=ai_view.client(con))
    if out["status"] == "off":
        out["message"] = ai_view.OFF + " 답 대신 근거가 될 줄만 보여 드립니다."
    return _qa_page(request, ctx, con, result=out, question=q)


# ================================================================ AI 사용량과 한도
def _usage_page(request, ctx, con, status_code=200):
    rows = aiclient.usage(con)
    vers = guides.versions()
    for r in rows:
        g = vers.get(r["feature"]) or {}
        r["guide_version"], r["guide_status"] = g.get("version"), g.get("status")
        r["pct"] = min(100, round(r["cost_usd"] / r["limit_usd"] * 100)) if r["limit_usd"] else (100 if r["cost_usd"] else 0)
        mm = store.measures(con, r["feature"]) if r["feature"] != "review" else review.measures(con)
        r["decided"], r["approval_rate"] = mm["decided"], mm["approval_rate"]
    c = ai_view.client(con)
    return page(request, ctx, "org_ai.html", status_code=status_code, rows=rows, on=c.enabled, OFF=ai_view.OFF, model=c.model,
                ym=aiclient.this_ym(), forbidden_version=guides.FORBIDDEN_VERSION, default_limit=aiclient.DEFAULT_LIMIT_USD,
                LIMIT_MAX=LIMIT_MAX, flash=request.session.pop(FLASH, None))


@router.get("/org/ai", response_class=HTMLResponse)
def ai_usage(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return _usage_page(request, ctx, con)


@router.post("/org/ai", response_class=HTMLResponse)
async def ai_usage_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    feature = str(form.get("feature") or "")
    if feature not in aiclient.FEATURES:
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    try:
        usd = float(str(form.get("limit_usd") or "").strip())
    except ValueError:
        usd = -1.0
    if not (0 <= usd <= LIMIT_MAX):
        _flash(request, "bad", f"한도는 0 에서 {LIMIT_MAX:g} 달러 사이의 숫자로 적어 주세요.")
        return RedirectResponse(USE_PATH, status_code=303)
    usd = round(usd, 2)
    aiclient.set_limit(con, feature, usd, ctx["login_id"])
    auth.log(con, ctx["login_id"], "ai_limit_set", f"{feature}={usd:g}")
    _flash(request, "ok", f"{aiclient.FEATURES[feature]}의 월 한도를 {usd:g} 달러로 바꿨습니다.")
    return RedirectResponse(USE_PATH, status_code=303)
