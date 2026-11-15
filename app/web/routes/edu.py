"""교육 화면: 내 교육, 교육 진척도, 과정, 차수와 일정, 수료 기록, 수료자 명부 안내, 명함 수료 코드 발급. S2 파트 E.

계산은 app/engine/education.py 를, 명부 적재는 app/loader/edu_roster.py 를, 수료 코드 발급은 app/connect/card_codes.py 를
부르기만 한다. 모든 주소는 guard 를 거치고, 사람 자료는 범위 안 설계사 목록(_scope_ids)으로만 읽는다.

| 주소 | 범위 | 쓰기 |
| --- | --- | --- |
| /my/education | 본인 | 없음 |
| /team/education | 메인 관리자 전체, 관리자 자기 조직 | 수료 확인. 확인 단계를 거친다. 관리자 본인의 수료는 확인하지 않는다(가정) |
| /edu/courses | 과정은 조직 범위로 거른다 | 과정 등록, 고치기, 닫기, 다시 열기. 메인 관리자만(가정) |
| /edu/sessions | 수료자 수는 범위 안 설계사만 센다 | 차수 등록과 고치기. 메인 관리자만(가정) |
| /edu/completions | 관리자는 범위 안 수료 기록만 본다 | 명부 올리기와 수료 코드 발급, 대조, 남은 요청 풀기. 메인 관리자만 |
| /data/courses | 메인 관리자 | 없음. /edu/completions 로 안내한다. 같은 일을 두 곳에 두지 않는다 |

명부 화면에는 이름을 보여 주지 않는다. 문제는 줄 번호와 사번, 과정 코드로만 보여 준다. 적재기의 메시지도 그렇게 만들어져 있다.
올린 명부 파일은 저장소 밖 업로드 폴더(staff.upload_dir)에 두고, 적재한 파일은 원본으로 남긴다. 미리보기만 하고 버린 파일은 지운다.

수료 코드 원문은 발급 POST 의 응답 화면에 한 번만 그린다. 세션, DB, 변경 기록, 다음 화면 어디에도 남기지 않고 응답은 no-store 다.
결과 줄은 dataclasses.asdict 로 바꾸지 않고 필요한 칸만 하나씩 꺼낸다. 원문이 섞이지 않게 하려는 것이다.
"""
from __future__ import annotations

import pathlib
import re
import secrets
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.connect import card_codes as cc
from app.connect.base import NOT_CONFIGURED, OK
from app.connect.card_site import WRITE_OFF
from app.engine import education as edu
from app.loader import edu_roster as er
from app.loader.excel_loader import DuplicateFile
from app.loader.sheet import HeaderMismatch
from app.org.sync import org_texts
from app.web import auth
from app.web.deps import _in, _q, edu_today, get_con, guard, page
from app.web.routes import card as card_route
from app.web.routes.staff import upload_dir

router = APIRouter()

FLASH = "edu_flash"
ROSTER_KEY = "edu_roster_upload"          # 세션에는 파일 표지, 고른 배치, 파일 이름만 둔다. 이름이나 자료는 두지 않는다
TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
SUFFIXES = (".xlsx", ".csv", ".txt")
MAX_BYTES = 20 * 1024 * 1024
LEVELS = ("hq", "branch", "center", "team")
ISSUE_SHOW = 200                         # 화면에 그리는 문제 줄의 상한
NOT_DONE_SHOW = 300

STATE_ORDER = {"overdue": 0, "soon": 1, "open": 2, "done": 3}
CODE_STATUS_KO = {"requested": "요청 중", "issued": "발급함", "failed": "실패", "revoked": "내림"}
BATCH_STATUS_KO = {"received": "받음", "active": "반영", "failed": "실패", "superseded": "바꿈"}
SESSION_STATUS_KO = {"planned": "예정", "held": "마침", "cancelled": "취소"}


# ---------------------------------------------------------------- 도우미
def _today() -> date:
    """교육 화면의 기준일. deps.edu_today 한 곳에서 정한다."""
    return edu_today()


def _day_label(d: date) -> str:
    return f"{d.month}월 {d.day}일"


def _stamp(s: str | None) -> str:
    """db.now() 꼴의 시각을 '11월 14일 9시 05분' 으로."""
    if not s:
        return ""
    try:
        t = datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(s)[:16]
    return f"{t.month}월 {t.day}일 {t.hour}시 {t.minute:02d}분"


def _scope_ids(con, ctx) -> list:
    """이 화면에서 볼 수 있는 설계사. 메인 관리자는 전체(퇴사자 포함), 관리자는 guard 가 준 범위, 본인 범위는 본인 하나다."""
    if ctx["scope"] == "all":
        return [r[0] for r in con.execute("SELECT agent_id FROM agent ORDER BY agent_id")]
    if ctx["scope"] == "self":
        return [ctx["agent_id"]] if ctx.get("agent_id") is not None else []
    return sorted(ctx["agent_ids"])


def _course_orgs(ctx):
    """과정 목록을 거르는 조직. 메인 관리자는 None(전체), 관리자는 맡은 조직들, 설계사는 소속 조직."""
    if ctx["role"] == "main":
        return None
    if ctx["role"] == "manager":
        return sorted(ctx["org_ids"])
    return [ctx["home_org"]["org_id"]] if ctx.get("home_org") else []


def _deny(con, ctx, request, what: str, message: str):
    auth.log(con, ctx["login_id"], "access_denied", f"{request.url.path} {what}")
    raise HTTPException(403, message)


def _main_only(con, ctx, request, what: str):
    if ctx["role"] != "main":
        _deny(con, ctx, request, what, "메인 관리자만 할 수 있는 일입니다.")


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _flash(request, kind, text, detail=""):
    request.session[FLASH] = {"kind": kind, "text": text, "detail": detail}


def _who(con, ym, agent_ids) -> dict:
    """{agent_id: {code, name, org}}. 범위 안 사람만 넘겨 부른다."""
    if not agent_ids:
        return {}
    q, ids = _in(sorted(set(agent_ids)))
    texts = org_texts(con)
    rows = _q(con, f"""SELECT a.agent_id, a.internal_code code, a.name, a.status='terminated' AS "left",
                              (SELECT h.org_id FROM agent_org_history h WHERE h.agent_id=a.agent_id AND h.effective_from<=?
                                ORDER BY h.effective_from DESC, h.id DESC LIMIT 1) org_id
                       FROM agent a WHERE a.agent_id IN ({q})""", (ym, *ids))
    return {r["agent_id"]: {**r, "org": texts.get(r["org_id"], "")} for r in rows}


# ---------------------------------------------------------------- 내 교육
@router.get("/my/education", response_class=HTMLResponse)
def my_education(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    edu.ensure(con)
    today = _today()
    me, upcoming = None, {}
    if ctx.get("agent_id") is not None:
        me = edu.my_education(con, ctx["agent_id"], [ctx["agent_id"]], today)
        for x in me["remaining"]:
            nxt = [s for s in edu.sessions(con, x["course_id"], [ctx["agent_id"]])
                   if s["status"] == "planned" and s["date_from"] >= today.isoformat()]
            if nxt:
                upcoming[x["course_id"]] = nxt[0]
    return page(request, ctx, "edu_my.html", me=me, upcoming=upcoming, as_of=_day_label(today), SOURCE_KO=edu.SOURCE_KO,
                soon_days=edu.SOON_DAYS, year=str(today.year))


# ---------------------------------------------------------------- 교육 진척도
def _team_page(request, ctx, con, *, course=None, level=None, ask=None, error=None, status_code=200):
    today = _today()
    ids = _scope_ids(con, ctx)
    prog = edu.org_progress(con, ids, today)
    level = level if level in LEVELS else ("branch" if ctx["role"] == "main" else "team")
    by_org = edu.progress_by_org(con, ids, level, today)
    texts = org_texts(con)
    for r in by_org:
        r["org_text"] = texts.get(r["org_id"], r["org_name"] or "소속 없음")
    rows = edu.not_done(con, ids, today, course_id=course)
    for r in rows:
        r["org"] = texts.get(r["org_id"], "")
        r["mine"] = ctx["role"] != "main" and r["agent_id"] == ctx.get("agent_id")
    confirm = None
    if ask:
        aid, cid = ask
        row = next((r for r in rows if r["agent_id"] == aid and r["course_id"] == cid), None)
        if row is None and course is not None and course != cid:
            row = next((r for r in edu.not_done(con, ids, today, course_id=cid) if r["agent_id"] == aid), None)
            if row:
                row["org"] = texts.get(row["org_id"], "")
                row["mine"] = ctx["role"] != "main" and row["agent_id"] == ctx.get("agent_id")
        if row is None:
            error = error or "확인할 수 있는 미수료 줄이 아닙니다. 범위 밖이거나 이미 수료한 과정입니다."
        elif row["mine"]:
            error = error or "본인의 수료는 다른 관리자나 메인 관리자가 확인합니다."
        else:
            sess = [s for s in edu.sessions(con, cid, ids) if s["status"] != "cancelled" and s["date_from"] <= today.isoformat()]
            confirm = {**row, "sessions": sess, "today": today.isoformat()}
    courses = [c for c in prog["courses"]]
    resp = page(request, ctx, "edu_team.html", status_code=status_code, prog=prog, by_org=by_org, level=level, LEVELS=LEVELS,
                rows=rows[:NOT_DONE_SHOW], n_rows=len(rows), course=course, courses=courses, confirm=confirm, error=error,
                flash=request.session.pop(FLASH, None), as_of=_day_label(today), soon_days=edu.SOON_DAYS,
                year=str(today.year))
    return resp


@router.get("/team/education", response_class=HTMLResponse)
def team_education(request: Request, course: str = "", level: str = "", confirm: str = "", ctx: dict = Depends(guard),
                   con=Depends(get_con)):
    edu.ensure(con)
    ask = None
    m = re.fullmatch(r"(\d+)-(\d+)", confirm.strip())
    if m:
        ask = (int(m.group(1)), int(m.group(2)))
    return _team_page(request, ctx, con, course=_int(course) if course else None, level=level, ask=ask)


@router.post("/team/education", response_class=HTMLResponse)
async def team_education_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    edu.ensure(con)
    form = await request.form()
    action = form.get("action", "")
    if action != "confirm":
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    aid, cid = _int(form.get("agent_id")), _int(form.get("course_id"))
    if aid is None or cid is None:
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    if form.get("confirm") != "1":                       # 확인 단계를 한 번 거친다
        return RedirectResponse(f"/team/education?confirm={aid}-{cid}#confirm", status_code=303)
    is_main = ctx["role"] == "main"
    if not is_main and aid not in ctx["agent_ids"]:
        _deny(con, ctx, request, "confirm", "범위 밖 설계사의 수료는 확인할 수 없습니다.")
    if not is_main and aid == ctx.get("agent_id"):
        _deny(con, ctx, request, "confirm_self", "본인의 수료는 다른 관리자나 메인 관리자가 확인합니다.")
    c = edu.course(con, cid)
    if not c or c["status"] != "active":
        return _team_page(request, ctx, con, error="열려 있는 과정이 아닙니다.", status_code=400)
    done = (form.get("completed_on") or "").strip()
    sid = _int(form.get("session_id")) if form.get("session_id") else None
    if done and done > _today().isoformat():
        return _team_page(request, ctx, con, ask=(aid, cid), error="수료일은 오늘이나 그 전이어야 합니다.", status_code=400)
    try:
        res = edu.record_completion(con, agent_id=aid, course_id=cid, by=ctx["login_id"], agent_ids=None if is_main else ctx["agent_ids"],
                                    completed_on=done or None, session_id=sid, source="manual")
    except PermissionError:
        con.rollback()
        _deny(con, ctx, request, "confirm", "범위 밖 설계사의 수료는 확인할 수 없습니다.")
    except ValueError as e:
        con.rollback()
        return _team_page(request, ctx, con, ask=(aid, cid), error=str(e), status_code=400)
    if res["created"]:
        _flash(request, "ok", f"{c['name']} 수료를 확인 처리했습니다. 진척도에 바로 반영됩니다.")
    else:
        _flash(request, "warn", "이미 같은 주기의 수료 기록이 있어 새로 넣지 않았습니다.")
    return RedirectResponse("/team/education", status_code=303)


# ---------------------------------------------------------------- 과정
def _course_form_defaults(c=None) -> dict:
    base = {k: "" for k in edu.COURSE_FIELDS}
    base.update(kind="compliance", target_role="fc", deadline_rule="none", required=1)
    if c:
        base.update({k: ("" if c[k] is None else c[k]) for k in edu.COURSE_FIELDS})
        base["course_id"] = c["course_id"]
    return base


def _courses_page(request, ctx, con, *, edit=None, form=None, error=None, show_closed=False, status_code=200):
    is_main = ctx["role"] == "main"
    courses = edu.list_courses(con, _course_orgs(ctx), include_closed=is_main and show_closed)
    texts = org_texts(con)
    for c in courses:
        c["owner"] = texts.get(c["owner_org_id"], "") if c["owner_org_id"] else ""
    orgs = sorted(((oid, t) for oid, t in texts.items()), key=lambda x: x[1]) if is_main else []
    levels = [r["code"] for r in con.execute("SELECT code FROM career_level ORDER BY code")] if is_main else []
    if is_main and form is None:
        form = _course_form_defaults(edu.course(con, edit)) if edit else (_course_form_defaults() if edit is not None else None)
    return page(request, ctx, "edu_courses.html", status_code=status_code, courses=courses, is_main=is_main, form=form, error=error,
                orgs=orgs, levels=levels, show_closed=show_closed, flash=request.session.pop(FLASH, None),
                KIND_KO=edu.KIND_KO, TARGET_KO=edu.TARGET_KO, RULE_KO=edu.RULE_KO, CARD_COURSES=edu.CARD_COURSES)


@router.get("/edu/courses", response_class=HTMLResponse)
def edu_courses(request: Request, edit: str = "", new: str = "", closed: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    edu.ensure(con)
    target = _int(edit) if edit else (0 if new == "1" else None)
    if target and not edu.course(con, target):
        target = None
    return _courses_page(request, ctx, con, edit=target, show_closed=closed == "1")


@router.post("/edu/courses", response_class=HTMLResponse)
async def edu_courses_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    edu.ensure(con)
    form = await request.form()
    action = form.get("action", "")
    _main_only(con, ctx, request, action or "save")
    cid = _int(form.get("course_id")) if form.get("course_id") else None
    if action in ("close", "open"):
        if not cid or not edu.course(con, cid):
            raise HTTPException(400, "없는 과정입니다.")
        edu.set_course_status(con, cid, "closed" if action == "close" else "active", ctx["login_id"])
        _flash(request, "ok", "과정을 닫았습니다. 진척도에서 빠지고 수료 기록은 남습니다." if action == "close" else "과정을 다시 열었습니다.")
        return RedirectResponse("/edu/courses" + ("?closed=1" if action == "close" else ""), status_code=303)
    if action != "save":
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    fields = {k: form.get(k, "") for k in edu.COURSE_FIELDS}
    fields["required"] = form.get("required") == "1"
    fields["owner_org_id"] = _int(fields["owner_org_id"]) if fields["owner_org_id"] else None
    try:
        new_id = edu.save_course(con, by=ctx["login_id"], course_id=cid, **fields)
    except ValueError as e:
        con.rollback()
        return _courses_page(request, ctx, con, form={**fields, "course_id": cid or "", "required": 1 if fields["required"] else 0},
                             error=str(e), status_code=400)
    _flash(request, "ok", "과정을 고쳤습니다." if cid else "과정을 등록했습니다.")
    return RedirectResponse(f"/edu/courses#course-{new_id}", status_code=303)


# ---------------------------------------------------------------- 차수와 일정
def _sessions_page(request, ctx, con, *, course=None, edit=None, form=None, error=None, status_code=200):
    is_main = ctx["role"] == "main"
    today = _today().isoformat()
    ids = _scope_ids(con, ctx)
    courses = edu.list_courses(con, _course_orgs(ctx))
    shown = [c for c in courses if course is None or c["course_id"] == course]
    rows = []
    for c in shown:
        for s in edu.sessions(con, c["course_id"], ids):
            rows.append({**s, "course_code": c["code"], "course_name": c["name"], "required": c["required"]})
    upcoming = sorted([r for r in rows if r["date_to"] >= today and r["status"] != "cancelled"], key=lambda r: (r["date_from"], r["course_code"]))
    past = sorted([r for r in rows if r not in upcoming], key=lambda r: (r["date_from"], r["course_code"]), reverse=True)
    if is_main and form is None and edit is not None:
        s = con.execute("SELECT * FROM edu_session WHERE session_id=?", (edit,)).fetchone() if edit else None
        form = dict(s) if s else {"session_id": "", "course_id": course or "", "date_from": "", "date_to": "", "online": 0, "place": "",
                                  "capacity": "", "status": "planned", "note": ""}
    return page(request, ctx, "edu_sessions.html", status_code=status_code, courses=courses, course=course, upcoming=upcoming, past=past,
                is_main=is_main, form=form, error=error, flash=request.session.pop(FLASH, None), self_scope=ctx["scope"] == "self",
                STATUS_KO=SESSION_STATUS_KO, year=today[:4], as_of=_day_label(_today()))


@router.get("/edu/sessions", response_class=HTMLResponse)
def edu_sessions(request: Request, course: str = "", edit: str = "", new: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    edu.ensure(con)
    target = _int(edit) if edit else (0 if new == "1" else None)
    return _sessions_page(request, ctx, con, course=_int(course) if course else None, edit=target)


@router.post("/edu/sessions", response_class=HTMLResponse)
async def edu_sessions_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    edu.ensure(con)
    form = await request.form()
    _main_only(con, ctx, request, form.get("action", "") or "save")
    if form.get("action") != "save":
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    sid = _int(form.get("session_id")) if form.get("session_id") else None
    cid = _int(form.get("course_id"))
    vals = {"date_from": form.get("date_from", ""), "date_to": form.get("date_to", ""), "online": form.get("online") == "1",
            "place": form.get("place", ""), "capacity": form.get("capacity", ""), "status": form.get("status", "planned"),
            "note": (form.get("note", "") or "").strip() or None}
    try:
        edu.save_session(con, by=ctx["login_id"], course_id=cid, session_id=sid, **vals)
    except ValueError as e:
        con.rollback()
        return _sessions_page(request, ctx, con, course=cid, form={**vals, "session_id": sid or "", "course_id": cid or "",
                                                                     "online": 1 if vals["online"] else 0}, error=str(e), status_code=400)
    _flash(request, "ok", "차수를 고쳤습니다." if sid else "차수를 등록했습니다.")
    return RedirectResponse(f"/edu/sessions?course={cid}", status_code=303)


# ---------------------------------------------------------------- 수료 기록과 명부
def _upload_file(token: str | None) -> pathlib.Path | None:
    if not token or not TOKEN.match(token):
        return None
    d = upload_dir() / f"edu_{token}"
    if not d.is_dir():
        return None
    files = [p for p in d.iterdir() if p.is_file()]
    return files[0] if len(files) == 1 else None


def _drop_upload(token: str | None) -> None:
    p = _upload_file(token)
    if p is not None:
        p.unlink(missing_ok=True)
        try:
            p.parent.rmdir()
        except OSError:
            pass


def _safe_name(name: str) -> str:
    """올린 파일의 이름. 경로를 떼고 위험한 글자를 바꾼다. 확장자는 받는 것만."""
    base = pathlib.Path(str(name or "")).name.strip() or "roster.xlsx"
    stem, suffix = base.rsplit(".", 1) if "." in base else (base, "")
    suffix = "." + suffix.lower()
    stem = re.sub(r"[^0-9A-Za-z가-힣._ -]", "_", stem)[:80].strip(" .") or "roster"
    return stem + suffix


def _recent(con, ids, limit=40) -> list:
    """범위 안 최근 수료 기록. 과정마다 completers 를 불러 합친다."""
    out = []
    for c in edu.list_courses(con, include_closed=True):
        for r in edu.completers(con, c["course_id"], ids):
            out.append({**r, "course_code": c["code"], "course_name": c["name"]})
    out.sort(key=lambda r: (r["completed_on"], r["completion_id"]), reverse=True)
    return out[:limit]


def _card_view(con, ctx) -> dict | None:
    """메인 관리자의 수료 코드 묶음. 대상, 발급 기록, 결과를 모르는 요청. 코드 원문은 없다."""
    if ctx["role"] != "main":
        return None
    client = card_route.card_client(con)
    ids = _scope_ids(con, ctx)
    cands = edu.card_code_candidates(con, ids)
    codes = edu.card_codes(con, ids)
    for k in codes:
        k["status_ko"] = CODE_STATUS_KO.get(k["status"], k["status"])
    unknown = [k for k in codes if k["status"] == "requested" and not k["serial"]]
    return {"write_on": client.write_enabled, "configured": client.admin_configured, "cands": cands, "codes": codes, "unknown": unknown}


def _completions_page(request, ctx, con, *, preview=None, result=None, error=None, card_pick=None, issued=None, reconcile=None,
                      release=None, card_flash=None, status_code=200):
    is_main = ctx["role"] == "main"
    ids = _scope_ids(con, ctx)
    batches = er.roster_batches(con) if is_main else []
    for b in batches:
        b["status_ko"] = BATCH_STATUS_KO.get(b["status"], b["status"])
        b["loaded_label"] = _stamp(b["loaded_at"])
    saved = request.session.get(ROSTER_KEY) or {}
    resp = page(request, ctx, "edu_completions.html", status_code=status_code, is_main=is_main, batches=batches,
                replaceable=[b for b in batches if b["status"] in ("active", "failed")], recent=_recent(con, ids),
                preview=preview, result=result, error=error, saved=saved if preview else {}, card=_card_view(con, ctx),
                card_pick=card_pick, issued=issued, reconcile=reconcile, release=release, card_flash=card_flash,
                flash=request.session.pop(FLASH, None), SOURCE_KO=edu.SOURCE_KO, ISSUE_SHOW=ISSUE_SHOW)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/edu/completions", response_class=HTMLResponse)
def edu_completions(request: Request, release: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    edu.ensure(con)
    ask = None
    if release and ctx["role"] == "main":
        iid = _int(release)
        card = _card_view(con, ctx)
        ask = next((k for k in card["unknown"] if k["issue_id"] == iid), None)
        if ask is None:
            return _completions_page(request, ctx, con, card_flash={"kind": "warn", "text": "결과를 모르는 요청 상태의 발급이 아닙니다."})
    return _completions_page(request, ctx, con, release=ask)


async def _roster_preview(request, ctx, con, form):
    up = form.get("file")
    replaces = _int(form.get("replaces")) if form.get("replaces") else None
    if up is None or not getattr(up, "filename", ""):
        return _completions_page(request, ctx, con, error="수료자 명부 파일을 고르세요.", status_code=400)
    name = _safe_name(up.filename)
    if not name.lower().endswith(SUFFIXES):
        return _completions_page(request, ctx, con, error="엑셀(xlsx) 또는 CSV 파일만 받습니다.", status_code=400)
    data = await up.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return _completions_page(request, ctx, con, error="파일이 너무 큽니다.", status_code=400)
    _drop_upload((request.session.get(ROSTER_KEY) or {}).get("token"))
    token = secrets.token_urlsafe(24)
    d = upload_dir() / f"edu_{token}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_bytes(data)
    try:
        pv = er.preview(con, path, replaces=replaces)
    except Exception:                                     # 엑셀이 아니거나 깨진 파일
        con.rollback()
        _drop_upload(token)
        request.session.pop(ROSTER_KEY, None)
        return _completions_page(request, ctx, con, error="파일을 읽지 못했습니다. 수료자 명부 엑셀이나 CSV 인지 확인하세요.", status_code=400)
    con.rollback()                                        # 미리보기는 아무것도 쓰지 않는다
    if not pv["ok"]:
        _drop_upload(token)
        request.session.pop(ROSTER_KEY, None)
        return _completions_page(request, ctx, con, preview={**pv, "file": name, "replaces": replaces}, status_code=400)
    request.session[ROSTER_KEY] = {"token": token, "replaces": replaces, "file": name}
    return _completions_page(request, ctx, con, preview={**pv, "file": name, "replaces": replaces})


def _roster_load(request, ctx, con, form):
    saved = request.session.get(ROSTER_KEY) or {}
    path = _upload_file(saved.get("token"))
    if path is None:
        return _completions_page(request, ctx, con, error="미리보기한 파일이 없습니다. 파일을 다시 올려 미리보기부터 하세요.", status_code=400)
    try:
        res = er.load_roster(con, path, via="manual", file_path=str(path), by=ctx["login_id"], replaces=saved.get("replaces"))
    except HeaderMismatch as e:
        con.rollback()
        return _completions_page(request, ctx, con, error=f"수료자 명부 양식이 아닙니다. {e}", status_code=400)
    except DuplicateFile:
        con.rollback()
        request.session.pop(ROSTER_KEY, None)
        _drop_upload(saved.get("token"))
        return _completions_page(request, ctx, con, error="이미 넣은 파일입니다.", status_code=400)
    except ValueError as e:
        con.rollback()
        return _completions_page(request, ctx, con, error=str(e), status_code=400)
    request.session.pop(ROSTER_KEY, None)                 # 적재한 파일은 원본으로 남긴다. 세션의 표지만 지운다
    issues = er.batch_issues(con, res["batch_id"])
    return _completions_page(request, ctx, con, result={**res, "file": saved.get("file"), "issues": issues})


def _card_rows(con, ctx, completion_ids) -> list:
    """고른 수료 가운데 지금 발급 대상인 것. 대상 판단은 엔진의 card_code_candidates 다."""
    pick = {i for i in completion_ids if i is not None}
    return [c for c in edu.card_code_candidates(con, _scope_ids(con, ctx)) if c["completion_id"] in pick]


def _issued_view(con, ctx, out) -> dict:
    """발급 결과를 화면 줄로 바꾼다. 결과 줄에서 칸을 하나씩 꺼낸다. asdict 를 쓰지 않는다."""
    rows = list(getattr(out.value, "rows", ()) or ())
    who = _who(con, ctx["ym"], [r.agent_id for r in rows if r.agent_id is not None])
    view = []
    for r in rows:
        p = who.get(r.agent_id, {})
        course_name = edu.CARD_COURSES.get(r.course or "", r.course or "")
        secret = r.code or ""
        view.append({"code": p.get("code", ""), "name": p.get("name", ""), "course": r.course or "", "course_name": course_name,
                     "ym": r.ym or "", "serial": r.serial or "", "result": r.result, "result_ko": r.result_ko, "reason": r.reason,
                     "secret": secret,
                     "message": (f"{p.get('name', '')}님, {course_name} 수료 코드는 {secret} 입니다. 모바일 명함을 만들 때 교육 수료 칸에 넣어 주세요. "
                                 "이 코드는 다시 보여 드릴 수 없으니 잘 보관해 주세요.") if secret else ""})
    return {"status": out.status, "status_ko": cc.ISSUE_KO.get(out.status, out.status), "reason": out.reason, "rows": view,
            "n_secret": sum(1 for v in view if v["secret"]), "write_off": out.status == WRITE_OFF, "not_configured": out.status == NOT_CONFIGURED}


@router.post("/edu/completions", response_class=HTMLResponse)
async def edu_completions_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    edu.ensure(con)
    form = await request.form()
    action = form.get("action", "")
    _main_only(con, ctx, request, action or "post")
    if action == "preview":
        return await _roster_preview(request, ctx, con, form)
    if action == "load":
        return _roster_load(request, ctx, con, form)
    if action == "cancel":
        _drop_upload((request.session.pop(ROSTER_KEY, None) or {}).get("token"))
        _flash(request, "ok", "미리보기한 파일을 버렸습니다. 아무것도 넣지 않았습니다.")
        return RedirectResponse("/edu/completions", status_code=303)

    is_main = ctx["role"] == "main"
    if action == "card_pick":                             # 확인 단계. 아무것도 보내지 않는다
        rows = _card_rows(con, ctx, [_int(x) for x in form.getlist("cid")])
        if not rows:
            return _completions_page(request, ctx, con, card_flash={"kind": "warn", "text": "발급할 수료를 골라 주세요."}, status_code=400)
        return _completions_page(request, ctx, con, card_pick=rows)
    if action == "card_issue":
        ids = [_int(x) for x in form.getlist("cid")]
        if form.get("confirm") != "1":
            rows = _card_rows(con, ctx, ids)
            return _completions_page(request, ctx, con, card_pick=rows or None,
                                     card_flash=None if rows else {"kind": "warn", "text": "발급할 수료를 골라 주세요."})
        out = cc.issue_card_codes(con, card_route.card_client(con), completion_ids=[i for i in ids if i is not None],
                                  actor=ctx["login_id"], is_main=is_main)
        resp = _completions_page(request, ctx, con, issued=_issued_view(con, ctx, out))
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp
    if action == "card_reconcile":
        out = cc.reconcile_card_codes(con, card_route.card_client(con), actor=ctx["login_id"], is_main=is_main)
        rec = out.value if out.status == OK else None
        view = {"status": out.status, "reason": out.reason}
        if rec is not None:
            view.update(clean=rec.clean, matched=len(rec.matched), hash_mismatch=list(rec.hash_mismatch), app_only=list(rec.app_only),
                        site_only=list(rec.site_only), revoked_on_app=list(rec.revoked_on_app), unconfirmed=list(rec.unconfirmed))
        return _completions_page(request, ctx, con, reconcile=view)
    if action == "card_release":
        iid = _int(form.get("issue_id"))
        if iid is None:
            raise HTTPException(400, "처리할 수 없는 요청입니다.")
        if form.get("confirm") != "1":
            return RedirectResponse(f"/edu/completions?release={iid}#release", status_code=303)
        out = cc.release_card_code_request(con, issue_id=iid, actor=ctx["login_id"], is_main=is_main)
        _flash(request, "ok" if out.status == OK else "warn", out.reason or ("요청을 풀었습니다." if out.status == OK else "요청을 풀지 못했습니다."))
        return RedirectResponse("/edu/completions#card-codes", status_code=303)
    raise HTTPException(400, "처리할 수 없는 요청입니다.")


# ---------------------------------------------------------------- 데이터 묶음의 수료자 명부
@router.get("/data/courses", response_class=HTMLResponse)
def data_courses(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    """명부 올리기는 /edu/completions 한 곳에서 한다. 여기는 안내와 최근 배치만 보여 준다."""
    edu.ensure(con)
    batches = er.roster_batches(con)[:5]
    for b in batches:
        b["status_ko"] = BATCH_STATUS_KO.get(b["status"], b["status"])
        b["loaded_label"] = _stamp(b["loaded_at"])
    return page(request, ctx, "edu_data.html", batches=batches)


# ---------------------------------------------------------------- 홈의 할 일
def due_soon_count(con, ctx) -> dict | None:
    """홈 할 일의 교육 기한 임박 건수. 설계사는 본인, 관리자는 범위 안. 메인 관리자는 None.
    {n, href, as_of, days}. 엔진의 due_soon 을 그대로 센다."""
    if ctx["role"] not in ("agent", "manager"):
        return None
    try:
        edu.ensure(con)
        ids = [ctx["agent_id"]] if ctx["role"] == "agent" else sorted(ctx["agent_ids"])
        if not ids or (ctx["role"] == "agent" and ctx.get("agent_id") is None):
            return None
        n = len(edu.due_soon(con, ids, _today()))
    except Exception:                                     # 홈은 교육 때문에 멈추지 않는다
        con.rollback()
        return None
    return {"n": n, "href": "/my/education" if ctx["role"] == "agent" else "/team/education#not-done", "as_of": _day_label(_today()),
            "days": edu.SOON_DAYS}
