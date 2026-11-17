"""공통 묶음의 일정 /calendar. 프로모션 기간과 마감, 교육 차수, 교육 기한을 한 달 달력과 목록으로 보여 준다.

- 프로모션은 진행 중 프로모션 화면과 같은 pm.listing 으로 고른다. 설계사와 관리자에게는 게시된 설계사 몫만 보인다.
- 교육 차수는 education.list_courses 로 그 사람에게 보이는 과정을 고른 뒤 education.sessions 로 낸다. 수료자 수는 범위 안 사람만 센다.
- 교육 기한은 education.not_done 으로 낸다. 설계사는 본인 것, 관리자와 메인 관리자는 범위 안 사람 수만 적는다.
- 기준일은 둘이다. 프로모션은 자료 기준 달의 pm.ref_date, 교육 기한은 교육 화면과 같은 deps.edu_today(오늘)다.
- 13회차 도래는 persist.inst13_due 로 범위 안 사람의 날짜별 건수만 낸다. 고객 이름도 증권번호도 없다. 유지 위험과 겹치는 건수는
  자료 기준 달(ctx['ym'])의 persist.risk 로 센다. 링크는 유지율 화면이 열린 사람에게는 유지율, 아니면 내 유지율이다.
- 휴대폰에서는 목록이 먼저 보이고 달력은 접어 둔다. view=month 로 열면 휴대폰에서도 달력을 보인다.
"""
from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.engine import education as edu
from app.engine import persist as ps
from app.engine import promotion as pm
from app.web import menu
from app.web.deps import _period_text, edu_today, get_con, guard, page

router = APIRouter()
MONTH_RE = re.compile(r"^(20\d\d)-(0[1-9]|1[0-2])$")
WEEK_KO = ("일", "월", "화", "수", "목", "금", "토")          # 한 주는 일요일부터
KIND_KO = {"promo_start": "프로모션 시작", "promo_end": "프로모션 마감", "edu_session": "교육 차수", "edu_due": "교육 기한",
           "inst13_due": "13회차 도래"}


def _link(ctx, path, query=""):
    it = menu.find(path)
    return (path + query) if it and menu.visible(it, ctx) else None       # 꺼진 모듈의 화면으로는 잇지 않는다


def _scope_ids(con, ctx) -> list:
    """범위 안 설계사. 설계사는 본인, 관리자는 guard 가 준 범위, 메인 관리자는 전부다."""
    if ctx["role"] == "agent":
        return [ctx["agent_id"]] if ctx["agent_id"] else []
    if ctx["role"] == "manager":
        return sorted(ctx["agent_ids"])
    return [r[0] for r in con.execute("SELECT agent_id FROM agent ORDER BY agent_id")]


def _course_orgs(ctx):
    """과정을 고르는 조직 범위. list_courses 는 None 이면 전체다."""
    if ctx["role"] == "main":
        return None
    if ctx["role"] == "manager":
        return sorted(ctx["org_ids"])
    return [ctx["home_org"]["org_id"]] if ctx.get("home_org") else []


def _ev(day, kind, title, sub="", link=None, tone=""):
    return {"day": day, "kind": kind, "kind_ko": KIND_KO[kind], "title": title, "sub": sub, "link": link, "tone": tone}


def events(con, ctx, first: date, last: date, ref: date, edu_ref: date | None = None) -> tuple:
    """(그 달에 걸친 프로모션 기간 목록, 하루짜리 일정 목록). ref 는 프로모션 기준일, edu_ref 는 교육 기한 기준일이다."""
    edu_ref = edu_ref or edu_today()
    ym = first.strftime("%Y%m")
    spans, evs = [], []
    for p in pm.listing(con, ym, ctx["role"]):
        if ctx["role"] != "main" and p["recipient_level"] != "fc":
            continue
        f, t = date.fromisoformat(p["date_from"]), date.fromisoformat(p["date_to"])
        phase = "upcoming" if ref < f else ("ended" if ref > t else "active")
        link = _link(ctx, "/promo/list") if phase != "ended" else _link(ctx, "/promo/archive")
        spans.append({**{k: p[k] for k in ("promo_id", "name", "insurer_name", "kind_ko", "date_from", "date_to", "timing_ko")},
                      "period_text": _period_text(p), "phase": phase, "link": link})
        who = p["insurer_name"] or "전 보험사"
        if first <= f <= last:
            evs.append(_ev(f.isoformat(), "promo_start", p["name"], f"{who} · {p['kind_ko']}", link, "blue"))
        if first <= t <= last:
            evs.append(_ev(t.isoformat(), "promo_end", p["name"], f"{who} · {p['kind_ko']}", link, "bad" if phase == "active" and (t - ref).days <= 3 else ""))
    scope = _scope_ids(con, ctx)
    try:
        sess_link = _link(ctx, "/edu/sessions")
        for c in edu.list_courses(con, _course_orgs(ctx)):
            for s in edu.sessions(con, c["course_id"], scope):
                f, t = date.fromisoformat(s["date_from"]), date.fromisoformat(s["date_to"] or s["date_from"])
                if t < first or f > last:
                    continue
                where = "온라인" if s["online"] else (s["place"] or "장소 미정")
                sub = f"{s['session_no']}차 · {where}" + (f" · 정원 {s['capacity']}명" if s["capacity"] else "")
                if ctx["role"] != "agent" and s["completers"]:
                    sub += f" · 수료 {s['completers']}명"
                status = {"cancelled": "취소", "held": "진행함"}.get(s["status"])
                evs.append(_ev(max(f, first).isoformat(), "edu_session", c["name"] + (f" ({status})" if status else ""), sub, sess_link,
                               "" if s["status"] == "cancelled" else "good"))
        rows = edu.not_done(con, scope, as_of=edu_ref)
    except sqlite3.OperationalError:                         # 교육 표가 없는 오래된 DB
        con.rollback()
        rows = []
    groups: dict = {}
    for r in rows:
        if r["due"] and first.isoformat() <= r["due"] <= last.isoformat():
            groups.setdefault((r["due"], r["course_id"], r["course_name"]), []).append(r)
    for (due, _cid, name), rs in sorted(groups.items()):
        mine = [r for r in rs if r["agent_id"] == ctx["agent_id"]]
        tone = "bad" if any(r["state"] == "overdue" for r in rs) else ("warn" if any(r["state"] == "soon" for r in rs) else "")
        if ctx["role"] == "agent":
            evs.append(_ev(due, "edu_due", f"{name} 기한", "필수 과정 · " + mine[0]["state_ko"], _link(ctx, "/my/education"), tone))
        else:
            sub = f"미수료 {len(rs)}명" + (" · 본인 포함" if mine else "")
            evs.append(_ev(due, "edu_due", f"{name} 기한", sub, _link(ctx, "/team/education") or _link(ctx, "/edu/courses"), tone))
    due = ps.inst13_due(con, scope, first, last, ym=ctx["ym"])
    due_link = _link(ctx, "/perf/persistency") or _link(ctx, "/my/persistency")
    for d in due["days"]:
        sub = "유지 중인 계약의 13회차 납입일" + (f" · 유지 위험과 겹침 {d['at_risk']}건" if d["at_risk"] else "")
        evs.append(_ev(d["date"], "inst13_due", f"13회차 도래 {d['count']}건", sub, due_link, "warn" if d["at_risk"] else ""))
    order = {"promo_end": 0, "edu_due": 1, "inst13_due": 2, "edu_session": 3, "promo_start": 4}
    evs.sort(key=lambda e: (e["day"], order[e["kind"]], e["title"]))
    return spans, evs


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, m: str | None = None, view: str | None = None, ctx: dict = Depends(guard), con=Depends(get_con)):
    today = date.today()
    hit = MONTH_RE.match(m or "")
    y, mo = (int(hit.group(1)), int(hit.group(2))) if hit else (today.year, today.month)
    first = date(y, mo, 1)
    last = date(y, mo, calendar.monthrange(y, mo)[1])
    ref = pm.ref_date(ctx["ym"])
    edu_ref = edu_today()
    spans, evs = events(con, ctx, first, last, ref, edu_ref)
    by_day: dict = {}
    for e in evs:
        by_day.setdefault(e["day"], []).append(e)
    weeks = []
    d = first - timedelta(days=(first.weekday() + 1) % 7)
    while d <= last or d.weekday() != 6:
        if d.weekday() == 6:
            weeks.append([])
        weeks[-1].append({"date": d.isoformat(), "day": d.day, "in": d.month == mo, "today": d == today, "events": by_day.get(d.isoformat(), [])})
        d += timedelta(days=1)
    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m = (last + timedelta(days=1)).strftime("%Y-%m")
    days = [{"date": k, "wd": WEEK_KO[(date.fromisoformat(k).weekday() + 1) % 7], "events": v} for k, v in sorted(by_day.items())]
    return page(request, ctx, "calendar.html", y=y, mo=mo, weeks=weeks, days=days, spans=spans, n=len(evs), view=view if view in ("month", "list") else None,
                prev_m=prev_m, next_m=next_m, this_m=today.strftime("%Y-%m"), today=today.isoformat(), ref=ref.isoformat(), edu_ref=edu_ref.isoformat(), WEEK_KO=WEEK_KO)
