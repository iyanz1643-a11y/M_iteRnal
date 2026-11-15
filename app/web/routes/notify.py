"""공통 묶음의 알림함 /notifications. D-10 제안대로 앱 안 알림만 한다. 카카오와 푸시는 보내지 않는다.

알림은 표에 남기지 않고 열 때마다 원본에서 만든다. 표에는 읽음 표시만 둔다(schema.sql 의 [notifications:begin] 묶음).
범위와 권한은 각 원본 화면과 같다. 이 파일은 범위를 새로 정하지 않고 guard 가 준 ctx 를 그대로 쓴다.

| 종류 | 받는 사람 | 원본 | 범위 |
| --- | --- | --- | --- |
| 새 프로모션 | 설계사, 관리자 | 진행 중 프로모션 | 게시된 설계사 몫. 끝나지 않은 것 |
| 마감 임박 | 전원 | 진행 중 프로모션 | 기준일에 진행 중이고 마감까지 3일 이내 |
| 구간 근접 | 설계사, 관리자 | 달성 현황 | 본인. 관리자는 범위 안 재직 팀원의 사람 수만. 금액은 넣지 않는다 |
| 교육 기한 | 설계사, 관리자 | education.due_soon | 본인. 관리자는 범위 안 팀원의 과정별 사람 수 |
| 명함 승인 대기 | card_approver 인 메인 관리자와 관리자 | card.pending_count | 명함 승인 화면과 같은 범위와 본인 빼기 |
| 명함 권한 요청 | 메인 관리자 | card_grant.pending_requests | 조직장과 대리 화면과 같다 |
| 새 공지 | 공지의 대상 안인 사람 | board.recent_notices | 공지와 자료실과 같은 보는 사람 규칙. 최근 board.NOTICE_DAYS 일. 올린 사람은 뺀다 |

- 프로모션 알림의 기준일은 프로모션 화면과 같은 pm.ref_date(자료 기준 달)이다.
- 교육 기한은 팀 교육 진척도와 같은 deps.edu_today(오늘)로 따진다. 교육은 실적 자료와 상관없는 날짜 일이다.
- 알림 글에는 고객 이름, 연락처, 시상 금액을 넣지 않는다. 구간 근접에는 본인의 남은 실적만 적는다.
- 종 아이콘의 숫자는 base.html 이 notif_badge(ctx) 로 부른다. 만든 알림 목록을 사람마다 BADGE_TTL 초 동안 이 프로세스 안에 담아 두고,
  읽음 표시는 부를 때마다 표에서 새로 읽는다. 종 아이콘은 쓰기가 생기지 않게 명함 권한 요청을 표에서 바로 센다(light).
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.engine import education as edu
from app.engine import promotion as pm
from app.org import card_grant as cg
from app.web import board as bd
from app.web import menu
from app.web.deps import _cards, edu_today, get_con, guard, page, templates
from app.web.routes import card
from app.web.routes.promo import NEAR_RATIO, _scope_agents, near

router = APIRouter()
PATH = "/notifications"
BEGIN, END = "-- [notifications:begin]", "-- [notifications:end]"
DUE_DAYS = 3                 # docs/09 6장: 프로모션 마감 3일 전과 하루 전
BADGE_TTL = 60               # 종 아이콘의 숫자에 쓰는 목록을 담아 두는 초
KEY_RE = re.compile(r"^[a-z_]+:[0-9A-Za-z:_\-]{1,80}$")
KIND_KO = {"promo_new": "새 프로모션", "promo_due": "마감 임박", "tier_near": "구간 근접", "edu_due": "교육 기한",
           "card_pending": "명함 승인 대기", "card_grant": "명함 권한 요청", "board_new": "새 공지"}
TONE = {"promo_new": "blue", "promo_due": "bad", "tier_near": "good", "edu_due": "warn", "card_pending": "warn", "card_grant": "warn",
        "board_new": "blue"}


# ---------------------------------------------------------------- 표
def ensure(con) -> None:
    """읽음 표가 없는 오래된 DB 에서 schema.sql 의 알림 묶음을 돌린다. 있으면 아무것도 하지 않는다."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_read'").fetchone():
        return
    text = db.SCHEMA.read_text(encoding="utf-8")
    con.executescript(text[text.index(BEGIN):text.index(END)])
    con.commit()


def read_keys(con, user_id) -> set:
    try:
        return {r[0] for r in con.execute("SELECT note_key FROM notification_read WHERE user_id=?", (user_id,))}
    except sqlite3.OperationalError:                         # 표가 없는 오래된 DB
        return set()


def mark_read(con, user_id, keys) -> int:
    ensure(con)
    n = 0
    for k in keys:
        n += con.execute("INSERT OR IGNORE INTO notification_read(user_id, note_key, read_at) VALUES(?,?,?)", (user_id, k, db.now())).rowcount
    con.commit()
    return n


# ---------------------------------------------------------------- 알림 만들기
def _link(ctx, path, query=""):
    """그 사람에게 열린 화면이면 주소를, 아니면 None 을 준다. 알림의 링크도 메뉴와 같은 규칙을 따른다."""
    it = menu.find(path)
    return (path + query) if it and menu.allowed(it, ctx) else None


def _note(kind, key, title, body, at, link=None):
    return {"key": f"{kind}:{key}", "kind": kind, "kind_ko": KIND_KO[kind], "tone": TONE[kind], "title": title, "body": body, "at": at, "link": link}


def _promo_notes(con, ctx, ref: date) -> list:
    out = []
    cards, _ = _cards(con, ctx)
    cards = [c for c in cards if c["recipient_level"] == "fc"]
    role = ctx["role"]
    for c in cards:
        if c["phase"] == "ended":
            continue
        if role != "main":
            at = (c.get("published_at") or c["date_from"])[:10]
            out.append(_note("promo_new", c["promo_id"], f"{c['insurer_name'] or '전 보험사'} {c['name']}",
                             f"{c['kind_ko']} · {c['period_text']}" + (f" · {c['timing_ko']} 지급" if c["timing_ko"] else ""), at,
                             _link(ctx, "/promo/list")))
        left = (date.fromisoformat(c["date_to"]) - ref).days
        if c["phase"] == "active" and 0 <= left <= DUE_DAYS:
            out.append(_note("promo_due", f"{c['promo_id']}:{c['date_to']}", f"{c['name']} {'오늘 마감' if left == 0 else f'마감 {left}일 전'}",
                             f"{c['insurer_name'] or '전 보험사'} · {c['date_to'][5:7].lstrip('0')}월 {c['date_to'][8:].lstrip('0')}일까지입니다.",
                             ref.isoformat(), _link(ctx, "/promo/list")))
    # 구간 근접. 진행 중인 구간 시상만 본다. 정률 시책에는 구간이 없다
    if role == "main":
        return out
    tiered = [c for c in cards if c["phase"] == "active" and c["kind"] != "rate" and c["tiers"]]
    team = [a["agent_id"] for a in _scope_agents(con, ctx)] if role == "manager" else []
    for c in tiered:
        ids = sorted(set(team) | ({ctx["agent_id"]} if ctx["agent_id"] else set()))
        if not ids:
            continue
        res = pm.progress(con, c, ids, ref)
        me = res.get(ctx["agent_id"]) if ctx["agent_id"] else None
        if me and near(me):
            nx = me["next"]
            gap = f"{nx['gap']:g}건" if c["metric"] == "policy_count" else f"{int(nx['gap']):,}원"
            out.append(_note("tier_near", f"{c['promo_id']}:{nx['tier_no']}", f"{c['name']} {nx['tier_no']}구간까지 {gap} 남았습니다",
                             f"{c['metric_ko']} 기준 예상입니다. 마감은 {c['due']}입니다.", ref.isoformat(),
                             _link(ctx, "/promo/progress", f"?p={c['promo_id']}&view=me" if role == "manager" else "")))
        if role == "manager":
            n = sum(1 for a in team if a != ctx["agent_id"] and near(res[a]))
            if n:
                out.append(_note("tier_near", f"team:{c['promo_id']}:{n}", f"{c['name']} 팀원 {n}명이 다음 구간에 가깝습니다",
                                 f"다음 구간까지 {round(NEAR_RATIO * 100)}% 이내입니다. 달성 현황에서 사람별로 봅니다.", ref.isoformat(),
                                 _link(ctx, "/promo/progress", f"?p={c['promo_id']}")))
    return out



def _edu_notes(con, ctx) -> list:
    """교육 기한 알림. 기준일은 프로모션과 따로 교육 화면과 같은 오늘이다."""
    ref = edu_today()
    if ctx["role"] == "main":
        return []
    ids = sorted(ctx["agent_ids"]) if ctx["role"] == "manager" else ([ctx["agent_id"]] if ctx["agent_id"] else [])
    if not ids:
        return []
    try:
        rows = edu.due_soon(con, ids, as_of=ref)
    except sqlite3.OperationalError:                         # 교육 표가 없는 오래된 DB
        con.rollback()
        return []
    out, team = [], {}
    for r in rows:
        when = f"{int(r['due'][5:7])}월 {int(r['due'][8:10])}일"
        if r["agent_id"] == ctx["agent_id"]:
            out.append(_note("edu_due", f"{r['course_id']}:{r['due']}", f"{r['course_name']} 기한이 {r['days_left']}일 남았습니다" if r["days_left"] else
                             f"{r['course_name']} 기한이 오늘입니다", f"{when}까지 수료해야 하는 필수 과정입니다.", ref.isoformat(),
                             _link(ctx, "/my/education")))
        else:
            team.setdefault((r["course_id"], r["due"], r["course_name"]), []).append(r["agent_id"])
    for (cid, due, name), who in sorted(team.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        out.append(_note("edu_due", f"team:{cid}:{due}:{len(who)}", f"{name} 기한 임박 팀원 {len(who)}명",
                         f"{int(due[5:7])}월 {int(due[8:10])}일까지 수료하지 않은 팀원입니다.", ref.isoformat(), _link(ctx, "/team/education")))
    return out


def _approval_notes(con, ctx, ref: date, light: bool) -> list:
    out = []
    if ctx["role"] in ("main", "manager") and ctx.get("card_approver"):
        pc = card.pending_count(con, ctx)                     # 명함 승인 화면과 같은 범위와 본인 빼기. 키가 없으면 None
        if pc and pc["n"]:
            out.append(_note("card_pending", f"{ref.isoformat()}:{pc['n']}", f"명함 승인 대기 {pc['n']}건",
                             f"명함 사이트 기준 {pc['at_label']}입니다. 신청본을 미리 보고 승인하거나 반려합니다.", ref.isoformat(),
                             _link(ctx, "/sales/card-approve")))
    if ctx["role"] == "main":
        try:
            if light:                                         # 종 아이콘은 쓰기 없이 센다. 알림함을 열면 pending_requests 로 맞춘다
                reqs = [dict(r) for r in con.execute(
                    """SELECT r.id, r.requested_at, u.display_name name FROM card_approver_request r
                       LEFT JOIN user_account u ON u.login_id=r.login_id WHERE r.status='pending' ORDER BY r.requested_at, r.id""")]
            else:
                reqs = cg.pending_requests(con)
        except sqlite3.OperationalError:
            con.rollback()
            reqs = []
        for r in reqs:
            out.append(_note("card_grant", r["id"], f"{r['name'] or '관리자'} 님의 명함 승인 권한 요청",
                             "조직장과 대리 화면에서 승인하거나 반려합니다.", (r["requested_at"] or "")[:10], _link(ctx, "/org/heads", "#card-requests")))
    return out


def _board_notes(con, ctx) -> list:
    """새 공지. 공지와 자료실의 보는 사람 규칙을 그대로 쓴다. 날짜는 올린 날이고, 최근 NOTICE_DAYS 일은 오늘부터 센다."""
    since = (date.today() - timedelta(days=bd.NOTICE_DAYS)).isoformat()
    out = []
    for p in bd.recent_notices(con, ctx, since):
        body = re.sub(r"\s+", " ", p["body"] or "").strip()
        body = (body[:60] + "…") if len(body) > 60 else body
        if p["n_files"]:
            body = (body + " · " if body else "") + f"첨부 {p['n_files']}개"
        out.append(_note("board_new", p["post_id"], p["title"], body or "공지와 자료실에서 봅니다.", p["posted_at"][:10],
                         _link(ctx, "/board", f"?post={p['post_id']}#post")))
    return out


def collect(con, ctx, light: bool = False) -> list:
    """그 사람의 알림 전부. 읽음 여부는 붙이지 않는다. 최근 것부터."""
    ref = pm.ref_date(ctx["ym"])
    out = _promo_notes(con, ctx, ref) + _edu_notes(con, ctx) + _approval_notes(con, ctx, ref, light) + _board_notes(con, ctx)
    order = {"card_grant": 0, "card_pending": 0, "promo_due": 1, "edu_due": 2, "board_new": 3, "tier_near": 4, "promo_new": 5}
    out.sort(key=lambda n: order[n["kind"]])
    out.sort(key=lambda n: n["at"], reverse=True)               # 날짜가 같으면 승인 대기, 마감 임박, 교육 기한 순
    seen, uniq = set(), []
    for n in out:
        if n["key"] not in seen:
            seen.add(n["key"])
            uniq.append(n)
    return uniq


# ---------------------------------------------------------------- 담아 두기와 종 아이콘
_LOCK = threading.Lock()
_CACHE: dict = {}            # {(DB 경로, user_id): (monotonic 시각, 알림 목록)}


def _db_key():
    return str(os.environ.get("APP_DB", db.DEFAULT_DB))


def _remember(user_id, items):
    with _LOCK:
        _CACHE[(_db_key(), user_id)] = (time.monotonic(), items)


def _recall(user_id):
    with _LOCK:
        hit = _CACHE.get((_db_key(), user_id))
    return hit[1] if hit and time.monotonic() - hit[0] < BADGE_TTL else None


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def unread_count(con, ctx) -> int:
    items = _recall(ctx["user_id"])
    if items is None:
        items = collect(con, ctx, light=True)
        _remember(ctx["user_id"], items)
    done = read_keys(con, ctx["user_id"])
    return sum(1 for n in items if n["key"] not in done)


def badge(ctx) -> int:
    """base.html 의 종 아이콘 숫자. 화면을 그리는 중이라 따로 연결을 열고, 무엇이 잘못되어도 0 을 준다."""
    if not isinstance(ctx, dict) or not ctx.get("user_id") or ctx.get("must_change_password"):
        return 0
    it = menu.find(PATH)
    if not it or not menu.allowed(it, ctx):
        return 0
    try:
        con = db.connect(_db_key())
        try:
            return unread_count(con, ctx)
        finally:
            con.close()
    except Exception:                                           # 종 아이콘 때문에 화면이 멈추지 않게 한다
        return 0


templates.env.globals["notif_badge"] = badge


# ---------------------------------------------------------------- 화면
@router.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request, show: str = "all", ctx: dict = Depends(guard), con=Depends(get_con)):
    items = collect(con, ctx)
    _remember(ctx["user_id"], items)
    done = read_keys(con, ctx["user_id"])
    for n in items:
        n["read"] = n["key"] in done
    unread = [n for n in items if not n["read"]]
    shown = unread if show == "unread" else items
    shown = sorted(shown, key=lambda n: n["read"])              # 안 읽은 것을 위로. 그 안에서는 최근 것부터
    resp = page(request, ctx, "notifications.html", items=shown, total=len(items), unread=len(unread), show=show,
                ref=pm.ref_date(ctx["ym"]).isoformat(), edu_ref=edu_today().isoformat(), msg=request.session.pop("notif_flash", None))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/notifications")
async def notifications_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    f = await request.form()
    action = f.get("action")
    mine = {n["key"] for n in (_recall(ctx["user_id"]) or collect(con, ctx))}
    if action == "read":
        keys = [k for k in f.getlist("key") if isinstance(k, str) and KEY_RE.match(k) and k in mine]
    elif action == "read_all":
        keys = sorted(mine)
    else:
        keys = []
    n = mark_read(con, ctx["user_id"], keys) if keys else 0
    request.session["notif_flash"] = f"{n}건을 읽음으로 표시했습니다." if n else "읽음으로 표시할 알림이 없습니다."
    back = PATH + ("?show=unread" if f.get("show") == "unread" else "")
    return RedirectResponse(back, status_code=303)
