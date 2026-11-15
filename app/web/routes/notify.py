"""공통 묶음의 알림함 /notifications. D-10 제안대로 앱 안 알림만 한다. 카카오와 푸시는 보내지 않는다.

알림은 표에 남기지 않고 열 때마다 원본에서 만든다. 표에는 읽음 표시만 둔다(schema.sql 의 [notifications:begin] 묶음).
범위와 권한은 각 원본 화면과 같다. 이 파일은 범위를 새로 정하지 않고 guard 가 준 ctx 를 그대로 쓴다.

| 종류 | 받는 사람 | 원본 | 범위 |
| --- | --- | --- | --- |
| 새 프로모션 | 설계사, 관리자 | 진행 중 프로모션 | 게시된 설계사 몫. 끝나지 않은 것 |
| 마감 임박 | 전원 | 진행 중 프로모션 | 기준일에 진행 중이고 마감까지 그 사람 조직의 마감 임박 알림 날짜 수 이내. 기본 3일 |
| 구간 근접 | 설계사, 관리자 | 달성 현황 | 본인. 관리자는 범위 안 재직 팀원의 사람 수만. 금액은 넣지 않는다 |
| 교육 기한 | 설계사, 관리자 | education.due_soon | 본인. 관리자는 범위 안 팀원의 과정별 사람 수 |
| 명함 승인 대기 | card_approver 인 메인 관리자와 관리자 | card.pending_count | 명함 승인 화면과 같은 범위와 본인 빼기 |
| 명함 권한 요청 | 메인 관리자 | card_grant.pending_requests | 조직장과 대리 화면과 같다 |
| 새 공지 | 공지의 대상 안인 사람 | board.recent_notices | 공지와 자료실과 같은 보는 사람 규칙. 최근 board.NOTICE_DAYS 일. 올린 사람은 뺀다 |
| 13회차 도래 | 설계사, 관리자 | persist.inst13_due | 본인. 관리자는 범위 안. 이번 주(일요일부터 토요일) 건수 하나만. 계약을 가리키는 것은 넣지 않는다 |
| DB 배정 | 설계사, 관리자 | app/sales/leads.py | 본인. 배정받은 지 new_days 일 안이고 활동이 없는 DB 의 건수 |
| DB 방치 | 설계사, 관리자 | app/sales/leads.py | 본인은 회수 예고와 회수 대상 건수. 관리자는 범위 안 팀원의 방치 건수 |
| 미배정 DB | 메인 관리자, 관리자 | app/sales/leads.py | 배정 화면의 풀과 같은 범위. 건수만 |
| 다음 면담 | 관리자 | app/sales/coaching.py | 범위 안 팀원 가운데 다음 면담이 지났거나 3일 안인 사람 수 |

- 프로모션 알림의 기준일은 프로모션 화면과 같은 pm.ref_date(자료 기준 달)이다.
- 교육 기한은 팀 교육 진척도와 같은 deps.edu_today(오늘)로 따진다. 교육은 실적 자료와 상관없는 날짜 일이다.
- 알림 글에는 고객 이름, 연락처, 시상 금액을 넣지 않는다. 영업 알림은 건수와 사람 수만 적는다. 영업 알림 넷은 조직별 설정의 항목이 아직 없어 끌 수 없다. 구간 근접에는 본인의 남은 실적만 적는다.
- 조직별 설정(app/org/settings.py)에서 그 사람의 조직이 꺼 둔 종류는 내지 않는다. 명함 승인 대기와 권한 요청은 끌 수 없다.
- 마감 임박 날짜 수(notify.promo_due_days)와 구간 근접 비율(promo.near_pct)도 조직별 설정이다. 본인 몫은 보는 사람 조직의 값,
  관리자의 팀원 수는 팀원마다 소속 조직의 값으로 센다. app/web/org_values.py 가 고른다.
- 13회차 도래의 이번 주는 오늘이 든 주이고 일정 화면과 같이 일요일에 시작한다. 메인 관리자에게는 띄우지 않는다.
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
from app.engine import persist as ps
from app.engine import promotion as pm
from app.org import card_grant as cg
from app.org import settings as org_settings
from app.sales import coaching, core
from app.sales import leads as ld
from app.web import board as bd
from app.web import menu, org_values
from app.web.deps import _cards, edu_today, get_con, guard, page, templates
from app.web.routes import card
from app.web.routes.promo import _scope_agents, near, near_pcts

router = APIRouter()
PATH = "/notifications"
BEGIN, END = "-- [notifications:begin]", "-- [notifications:end]"
DUE_DAYS = org_values.default(org_values.DUE_DAYS_KEY)   # docs/09 6장: 프로모션 마감 3일 전. 조직별 설정이 없을 때의 기본값이다
BADGE_TTL = 60               # 종 아이콘의 숫자에 쓰는 목록을 담아 두는 초
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*:[0-9A-Za-z:_\-]{1,80}$")
KIND_KO = {"promo_new": "새 프로모션", "promo_due": "마감 임박", "tier_near": "구간 근접", "edu_due": "교육 기한",
           "card_pending": "명함 승인 대기", "card_grant": "명함 권한 요청", "board_new": "새 공지", "inst13_due": "13회차 도래",
           "sales_new": "DB 배정", "sales_stale": "DB 방치", "sales_pool": "미배정 DB", "coach_due": "다음 면담"}
TONE = {"promo_new": "blue", "promo_due": "bad", "tier_near": "good", "edu_due": "warn", "card_pending": "warn", "card_grant": "warn",
        "board_new": "blue", "inst13_due": "warn", "sales_new": "blue", "sales_stale": "warn", "sales_pool": "blue", "coach_due": "warn"}


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


def due_days(con, ctx) -> int:
    """그 사람 조직의 마감 임박 알림 날짜 수."""
    return int(org_values.for_ctx(con, ctx, org_values.DUE_DAYS_KEY))


def _promo_notes(con, ctx, ref: date) -> list:
    out = []
    days = due_days(con, ctx)
    my_pct = org_values.for_ctx(con, ctx, org_values.NEAR_KEY)
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
        if c["phase"] == "active" and 0 <= left <= days:
            out.append(_note("promo_due", f"{c['promo_id']}:{c['date_to']}", f"{c['name']} {'오늘 마감' if left == 0 else f'마감 {left}일 전'}",
                             f"{c['insurer_name'] or '전 보험사'} · {c['date_to'][5:7].lstrip('0')}월 {c['date_to'][8:].lstrip('0')}일까지입니다.",
                             ref.isoformat(), _link(ctx, "/promo/list")))
    # 구간 근접. 진행 중인 구간 시상만 본다. 정률 시책에는 구간이 없다
    if role == "main":
        return out
    tiered = [c for c in cards if c["phase"] == "active" and c["kind"] != "rate" and c["tiers"]]
    team = [a["agent_id"] for a in _scope_agents(con, ctx)] if role == "manager" else []
    pcts = near_pcts(con, ctx, [a for a in team if a != ctx["agent_id"]]) if tiered and team else {}
    for c in tiered:
        ids = sorted(set(team) | ({ctx["agent_id"]} if ctx["agent_id"] else set()))
        if not ids:
            continue
        res = pm.progress(con, c, ids, ref)
        me = res.get(ctx["agent_id"]) if ctx["agent_id"] else None
        if me and near(me, org_values.ratio(my_pct)):
            nx = me["next"]
            gap = f"{nx['gap']:g}건" if c["metric"] == "policy_count" else f"{int(nx['gap']):,}원"
            out.append(_note("tier_near", f"{c['promo_id']}:{nx['tier_no']}", f"{c['name']} {nx['tier_no']}구간까지 {gap} 남았습니다",
                             f"{c['metric_ko']} 기준 예상입니다. 마감은 {c['due']}입니다.", ref.isoformat(),
                             _link(ctx, "/promo/progress", f"?p={c['promo_id']}&view=me" if role == "manager" else "")))
        if role == "manager":
            n = sum(1 for a in team if a != ctx["agent_id"] and near(res[a], org_values.ratio(pcts[a])))
            if n:
                out.append(_note("tier_near", f"team:{c['promo_id']}:{n}", f"{c['name']} 팀원 {n}명이 다음 구간에 가깝습니다",
                                 f"다음 구간까지 {org_values.pct_text(pcts.values())} 이내입니다. 달성 현황에서 사람별로 봅니다.", ref.isoformat(),
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


def this_week(day: date) -> tuple:
    """day 가 든 주의 일요일과 토요일. 일정 화면의 한 주와 같다."""
    start = day - timedelta(days=(day.weekday() + 1) % 7)
    return start, start + timedelta(days=6)


def _due_notes(con, ctx) -> list:
    """13회차 도래 n건(이번 주). 설계사는 본인, 관리자는 범위 안 사람의 건수 하나만 낸다. 숫자만이다."""
    if ctx["role"] == "main":
        return []
    ids = sorted(ctx["agent_ids"]) if ctx["role"] == "manager" else ([ctx["agent_id"]] if ctx["agent_id"] else [])
    if not ids:
        return []
    today = date.today()
    first, last = this_week(today)
    n = ps.inst13_due(con, ids, first, last)["total"]
    if not n:
        return []
    who = "범위 안 팀원과 본인의" if ctx["role"] == "manager" else "내"
    span = f"{first.month}월 {first.day}일부터 {last.month}월 {last.day}일까지"
    return [_note("inst13_due", f"{first.isoformat()}:{n}", f"13회차 도래 {n}건(이번 주)", f"{span} {who} 유지 계약에 13회차 납입일이 옵니다.",
                  today.isoformat(), _link(ctx, "/calendar", f"?m={today.strftime('%Y-%m')}"))]


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


def _sales_notes(con, ctx) -> list:
    """DB 배정, 방치, 미배정, 다음 면담. 건수만 적고 고객 이름과 연락처는 넣지 않는다. 표가 없는 오래된 DB 면 없다."""
    day = core.today()
    out = []
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_coaching'").fetchone():
            return []
        me = ctx.get("agent_id")
        if me and ctx["role"] != "main":
            mine = ld.for_agent(con, me, day, "open")
            fresh = [r for r in mine if r["status"] == "assigned" and r["idle"] is not None and r["idle"] < core.setting("new_days")]
            if fresh:
                out.append(_note("sales_new", f"{day.isoformat()}:{len(fresh)}", f"새 DB {len(fresh)}건이 배정됐습니다",
                                 "내 DB 에서 통화 결과를 남겨 주세요.", day.isoformat(), _link(ctx, "/sales/leads")))
            warn = [r for r in mine if r["warn"]]
            stale = [r for r in mine if r["stale"]]
            if warn or stale:
                title = f"회수 대상 DB {len(stale)}건" if stale else f"DB {len(warn)}건이 곧 회수 대상입니다"
                out.append(_note("sales_stale", f"me:{day.isoformat()}:{len(warn)}:{len(stale)}", title,
                                 f"활동 없이 {core.setting('stale')}일이 지나면 관리자가 회수할 수 있습니다.", day.isoformat(), _link(ctx, "/sales/leads")))
        if ctx["role"] == "manager":
            team = [a for a in sorted(ctx["agent_ids"]) if a != me]
            n = len(ld.stale(con, team, day)) if team else 0
            if n:
                out.append(_note("sales_stale", f"team:{day.isoformat()}:{n}", f"팀 방치 DB {n}건", "DB 배정과 성과에서 회수하거나 독려합니다.",
                                 day.isoformat(), _link(ctx, "/sales/assign")))
            due = coaching.due(con, team, day)
            if due:
                over = sum(1 for d in due if d["state"] == "over")
                out.append(_note("coach_due", f"{day.isoformat()}:{len(due)}", f"다음 면담 {len(due)}명" + (f" · 지남 {over}명" if over else ""),
                                 f"다음 면담이 지났거나 {coaching.DUE_DAYS}일 안인 팀원입니다.", day.isoformat(), _link(ctx, "/team/coaching")))
        if ctx["role"] in ("main", "manager"):
            n = len(ld.pool(con, ctx, day))
            if n:
                out.append(_note("sales_pool", f"{day.isoformat()}:{n}", f"미배정 DB {n}건", "지역 매칭으로 배정합니다.", day.isoformat(),
                                 _link(ctx, "/sales/assign")))
    except sqlite3.OperationalError:
        con.rollback()
        return []
    return out


def collect(con, ctx, light: bool = False) -> list:
    """그 사람의 알림 전부. 읽음 여부는 붙이지 않는다. 최근 것부터."""
    ref = pm.ref_date(ctx["ym"])
    out = (_promo_notes(con, ctx, ref) + _edu_notes(con, ctx) + _due_notes(con, ctx) + _approval_notes(con, ctx, ref, light)
           + _board_notes(con, ctx) + _sales_notes(con, ctx))
    off = org_settings.notify_off(con, ctx)                     # 조직별 설정에서 그 사람의 조직이 그 달에 꺼 둔 종류. 승인 대기 둘은 끌 수 없다
    out = [n for n in out if n["kind"] not in off]
    order = {"card_grant": 0, "card_pending": 0, "promo_due": 1, "edu_due": 2, "inst13_due": 2, "board_new": 3, "tier_near": 4, "promo_new": 5,
             "sales_stale": 2, "coach_due": 2, "sales_new": 3, "sales_pool": 3}
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
                ref=pm.ref_date(ctx["ym"]).isoformat(), edu_ref=edu_today().isoformat(), msg=request.session.pop("notif_flash", None),
                due_days=due_days(con, ctx))
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
