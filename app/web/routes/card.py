"""영업·제안 묶음의 명함 승인. D-44.

- 메인 관리자는 모든 대기 요청을, 관리자(조직장과 대리)는 자기 범위 안 설계사에 이어진 요청만 다룬다.
- 범위는 guard 가 준 ctx['agent_ids'] 를 그대로 넘긴다. 이 파일은 범위를 다시 계산하지 않는다.
- 승인과 반려는 app/connect/card_decide.py 의 decide_card 로만 보낸다. 클라이언트의 approve, reject 를 바로 부르지 않는다.
- 이름과 소속은 앱 DB 의 설계사 표에서 읽는다. 명함 사이트의 이름과 연락처는 쓰지 않는다.
- 반려 사유는 앱에 저장하지 않는다. 세션, 변경 기록, 화면 되돌림 어디에도 남기지 않는다.
- 잇기는 메인 관리자만 한다. CardLinkTable 이 계정을 한 번 더 본다.
- 수료 코드 발급은 이 화면에 없다. 발급은 /edu/completions 의 메인 관리자 묶음이다(app/web/routes/edu.py).
- 메인 관리자 전용 묶음(S3 명함 연동 3차)이 같은 화면 아래에 있다. 관리자에게는 그리지 않고 값도 읽지 않는다.
  새 연도 자격 안내는 card_badges.new_year_notices 를 guard 가 준 설계사 목록으로 부른다. 명함 주소가 이어진 사람만이다.
  배지와 명함은 퇴사 전까지 유지해 만료가 없다. D-55
  해촉자 명함 내리기 후보는 card_remove.removal_candidates, 내리기는 card_remove.remove_card 로만 보낸다.
  내리기는 확인 단계를 한 번 거친다. 확인 칸 confirm=1 이 없는 POST 는 확인 단계로 돌린다. 관리자가 보내면 403 이다.
  내리기 사유는 명함 사이트의 공개 상태 조회로 누구나 본다. 앱에는 저장하지 않고 변경 기록에도 길이만 남는다.
  후보와 목록의 이름과 소속은 앱 DB 의 설계사 표에서 읽는다. 해촉자는 그 달 소속이 없으면 마지막 소속을 쓴다.
- 관리자는 승인 요청을 메인 관리자가 /org/heads 에서 승인했거나 바로 켠 사람만 이 화면을 연다. 메뉴의 requires: card_approver 를 guard 가 본다.
- 관리자 본인의 명함은 메인 관리자가 처리한다. 목록에 본인 줄이 섞여 와도 승인과 반려 단추와 확인 단계를 열지 않는다.
- 신청본 미리보기는 같은 주소에 ?preview=주소 로 연다. guard 를 같은 규칙으로 거치고 card_decide.preview_card 만 부른다.
  다듬은 HTML 을 sandbox 값이 빈 iframe 의 srcdoc 에 넣고, 그림은 data: 로 넣는다. 가져온 신청본은 저장하지 않는다.

내 명함 /sales/card 는 관리자와 설계사 본인이 쓴다. card_site.my_card 로 이어진 주소의 공개 상태를 한 번 읽는다(GET 만).
- 명함에 올릴 수 있는 배지는 card_badges.judge_badges 를 본인 한 사람으로 부른다. 상태는 유효와 확정 전 둘이다.
  확정 전은 인증 계산에서 달성했지만 확정 전인 배지라 명함에 올릴 수 없다고 적는다. 배지는 퇴사 전까지 유지한다. D-55
- 유효한 배지마다 공개와 비공개를 고른다. 목록과 공개 여부는 card_badges.publish_items, 바꾸기는 card_badges.set_publish 로만 한다.
  기본은 비공개이고 본인만 바꾼다. 설계사 번호는 세션의 ctx 에서만 읽고 폼으로 받지 않는다. D-77
- 새 연도 자격 안내는 card_badges.new_year_notices 를 본인 한 사람으로 부른다.
- 상태는 공개, 대기, 반려, 없음 넷으로 보인다. 반려 사유는 D-39 가 열려 있어 보여 주지 않고 반려됨만 보인다. CardStatus 에도 사유가 없다.
- 주소가 이어지지 않았으면 메인 관리자에게 연결을 요청하라고 안내하고 명함 만들기 사이트 링크를 둔다. 주소는 사이트 주소 뒤 /make/ 로 둔 가정이다.
- 사이트 주소가 설정되지 않았거나 읽지 못하면 연결 표에 남은 마지막 확인 상태와 시각을 보여 준다.

홈의 할 일 알림은 pending_count 를 부른다. 담아 두는 것은 사이트의 전체 대기 목록이고, 범위와 본인 빼기는 부를 때마다
그 사람의 ctx 로 다시 거른다. 그래서 담아 둔 목록을 여러 사람이 함께 써도 남의 범위가 섞이지 않는다. 키가 없으면 부르지 않고, 있으면 시간 제한이 짧은 호출 한 번의 결과를
이 프로세스 안에 잠깐 담아 두고 쓴다. 실패도 같은 시간 동안 담아 두어 홈이 거듭 기다리지 않게 한다.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup

from app.connect import card_badges as cb
from app.connect import card_remove as cr
from app.connect.base import FAILED, NOT_CONFIGURED, OK, Outcome
from app.connect.card_decide import ALREADY, DENIED, IN_FLIGHT, decide_card, pending_for_scope, preview_card
from app.connect.card_html import iframe_tag
from app.connect.card_link import CardLinkTable, LinkError
from app.connect.card_site import NO_PENDING, NOT_FOUND, REASON_MAX, SLUG_RE, TOO_BIG, UNKNOWN, WRITE_OFF, CardFile, CardSiteClient, my_card
from app.connect.state import StateStore
from app.org.sync import org_texts
from app.web import auth
from app.web.deps import _in, _q, get_con, guard, page

router = APIRouter()
PATH = "/sales/card-approve"
KST = timezone(timedelta(hours=9))
FLASH = "card_flash"
HOME_TTL = 120                    # 홈 알림에 쓰는 목록을 담아 두는 초
HOME_TIMEOUT = 2.0                # 홈에서 부르는 호출의 시간 제한 초

# 판단 결과마다 화면에 보이는 한 줄. 존댓말
RESULT_LINE = {
    DENIED: "처리할 수 없는 요청이라 보내지 않았습니다.",
    ALREADY: "이미 처리한 요청입니다. 명함 사이트에 다시 보내지 않았습니다.",
    IN_FLIGHT: "같은 명함을 지금 처리하고 있습니다. 잠시 뒤 목록을 다시 확인해 주세요.",
    NO_PENDING: "대기 중인 요청이 없어 보내지 않았습니다. 이미 처리되었을 수 있습니다.",
    WRITE_OFF: "명함 사이트 쓰기가 아직 꺼져 있어 보내지 않았습니다. 지금은 명함 사이트 관리 화면에서 처리해 주세요.",
    NOT_CONFIGURED: "명함 사이트 연동 키가 아직 없어 보내지 않았습니다.",
    FAILED: "명함 사이트에 보내지 못했습니다. 잠시 뒤 다시 해 주세요.",
}
GOOD = {OK}
# 미리보기 결과마다 보이는 안내. 존댓말
PREVIEW_LINE = {
    DENIED: "이 신청본은 보여 드릴 수 없습니다. 범위 밖이거나 처리 권한이 없는 요청입니다.",
    NO_PENDING: "승인 대기 중인 신청이 아니라 신청본을 보여 드리지 않습니다. 이미 처리되었을 수 있습니다.",
    NOT_FOUND: "명함 사이트에 이 신청본 파일이 없습니다. 이미 처리되었거나 신청이 바뀌었을 수 있습니다.",
    TOO_BIG: "신청본 파일이 커서 앱에서 보여 드리지 못합니다. 명함 사이트 관리 화면에서 확인해 주세요.",
    NOT_CONFIGURED: "명함 사이트 연동 키가 아직 없어 신청본을 가져오지 않았습니다.",
    FAILED: "신청본을 가져오지 못했습니다. 잠시 뒤 다시 해 주세요.",
}
PREVIEW_FILE = "index.html"       # 미리보기로 여는 신청본 파일. 첫 화면 하나만 본다
NEUTRAL = {ALREADY, IN_FLIGHT, NO_PENDING, WRITE_OFF, NOT_CONFIGURED}


def card_client(con, *, quick: bool = False) -> CardSiteClient:
    """명함 사이트 클라이언트. 연결 상태는 같은 앱 DB 의 연결 상태 표에 남긴다. 시험은 이 함수를 바꿔 끼운다."""
    kw = {"timeout": HOME_TIMEOUT, "attempts": 1} if quick else {"timeout": 8.0, "attempts": 2, "backoff": 0.5}
    return CardSiteClient(state=StateStore(con=con), **kw)


# ---------------------------------------------------------------- 목록 담아 두기
class _Snapshot:
    """관리자 목록을 한 번 읽은 결과. pending_for_scope 가 쓰는 admin_list 와 card_url 만 낸다. 이름과 연락처는 처음부터 없다."""

    def __init__(self, out: Outcome, client: CardSiteClient):
        self.out = out
        self.at = datetime.now(KST)
        self.base_url = client.base_url
        self._card_url = client.card_url

    def admin_list(self) -> Outcome:
        return self.out

    def card_url(self, slug):
        return self._card_url(slug)


_LOCK = threading.Lock()
_CACHE: dict = {}                  # {사이트 주소: (monotonic 시각, _Snapshot)}


def _fetch(client: CardSiteClient) -> _Snapshot:
    snap = _Snapshot(client.admin_list(), client)
    with _LOCK:
        _CACHE[client.base_url] = (time.monotonic(), snap)
    return snap


def _cached(client: CardSiteClient) -> _Snapshot | None:
    with _LOCK:
        hit = _CACHE.get(client.base_url)
    if hit and time.monotonic() - hit[0] < HOME_TTL:
        return hit[1]
    return None


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _self_of(ctx) -> dict:
    """pending_for_scope 가 관리자 본인의 요청을 빼도록 넘기는 값. 메인 관리자에게는 영향이 없다."""
    return {"actor": ctx["login_id"], "self_agent_ids": [ctx["agent_id"]] if ctx.get("agent_id") is not None else []}


def pending_count(con, ctx) -> dict | None:
    """홈 할 일의 명함 승인 대기 건수. 키가 없거나 읽지 못하면 None 이다. {n, at_label}"""
    if ctx["role"] not in ("main", "manager") or not ctx.get("card_approver"):
        return None
    client = card_client(con, quick=True)
    if not client.admin_configured:
        return None
    try:
        snap = _cached(client) or _fetch(client)
        res = pending_for_scope(snap, CardLinkTable(con), ctx["agent_ids"], ctx["role"] == "main", **_self_of(ctx))
    except Exception:                                            # 홈은 명함 사이트 때문에 멈추지 않는다
        return None
    if res.status != OK:
        return None
    n = len(res.value["linked"]) + len(res.value["unlinked"])
    return {"n": n, "at_label": _hm(snap.at)}


# ---------------------------------------------------------------- 표시 도우미
def _hm(t: datetime) -> str:
    return f"{t.month}월 {t.day}일 {t.hour}시 {t.minute:02d}분"


def _when(iso: str | None) -> str:
    """명함 사이트의 시각(UTC ISO)을 한국 시각 글로."""
    if not iso:
        return "시각 없음"
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)[:16]
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return _hm(t.astimezone(KST))


def _people(con, ym, agent_ids) -> dict:
    """{agent_id: {code, name, org, left}}. 앱 DB 의 설계사 표에서만 읽는다."""
    if not agent_ids:
        return {}
    q, ids = _in(agent_ids)
    texts = org_texts(con)
    rows = _q(con, f"""SELECT a.agent_id, a.internal_code code, a.name, a.status='terminated' AS "left",
                              COALESCE((SELECT h.org_id FROM agent_org_history h WHERE h.agent_id=a.agent_id AND h.effective_from<=?
                                AND (h.effective_to IS NULL OR h.effective_to>=?) ORDER BY h.effective_from DESC LIMIT 1),
                                (SELECT h.org_id FROM agent_org_history h WHERE h.agent_id=a.agent_id
                                  ORDER BY h.effective_from DESC LIMIT 1)) org_id
                       FROM agent a WHERE a.agent_id IN ({q})""", (ym, ym, *ids))
    return {r["agent_id"]: {**r, "org": texts.get(r["org_id"], "")} for r in rows}


def _mine(ctx, agent_id) -> bool:
    """관리자 본인의 명함인가. 본인 명함은 메인 관리자가 처리한다."""
    return ctx["role"] != "main" and agent_id is not None and agent_id == ctx.get("agent_id")


def _rows(con, ctx, cards) -> list:
    who = _people(con, ctx["ym"], {c.agent_id for c in cards if c.agent_id is not None})
    out = []
    for c in cards:
        p = who.get(c.agent_id) or {}
        out.append({"slug": c.slug, "agent_id": c.agent_id, "code": p.get("code"), "name": p.get("name"), "org": p.get("org"),
                    "left": bool(p.get("left")), "requested": _when(c.requested_at), "live": c.live, "live_url": c.live_url,
                    "replaces": c.replaces_live, "conflict": c.conflict, "mine": _mine(ctx, c.agent_id)})
    out.sort(key=lambda r: (r["requested"] == "시각 없음", r["requested"], r["slug"]))
    return out


def _agent_choices(con, ym) -> list:
    """메인 관리자가 잇기에서 고르는 재직 설계사. 이어진 주소가 없는 사람만."""
    texts = org_texts(con)
    rows = _q(con, """SELECT a.agent_id, a.internal_code code, a.name,
                             (SELECT h.org_id FROM agent_org_history h WHERE h.agent_id=a.agent_id AND h.effective_from<=?
                               AND (h.effective_to IS NULL OR h.effective_to>=?) ORDER BY h.effective_from DESC LIMIT 1) org_id
                      FROM agent a WHERE a.status='active'
                        AND NOT EXISTS (SELECT 1 FROM card_link l WHERE l.agent_id=a.agent_id AND l.unlinked_at IS NULL)
                      ORDER BY a.internal_code""", (ym, ym))
    for r in rows:
        r["org"] = texts.get(r["org_id"], "")
    return rows


# ---------------------------------------------------------------- 메인 관리자 전용 묶음. 새 연도 자격 안내와 해촉자 명함 내리기
TOOLS = "main-tools"              # 묶음의 앵커
NOTICE_SHOW = 50                  # 새 연도 자격 안내에 한 번에 싣는 줄 수
AGENT_STATUS_KO = {"terminated": "해촉", "suspended": "정지", "active": "해촉일 지남"}
REMOVE_REASON_HELP = "사유는 명함 사이트에서 누구나 볼 수 있습니다. 비워 두면 사이트 기본 문구가 들어갑니다."
# 내리기 결과 여덟 가지마다 보이는 한 줄. 존댓말
REMOVE_LINE = {
    OK: "명함을 내렸습니다. 이 명함 링크는 이제 열리지 않습니다.",
    DENIED: "내리지 않았습니다. 권한이 없거나 사유를 고쳐야 합니다.",
    IN_FLIGHT: "같은 명함을 지금 내리고 있습니다. 잠시 뒤 목록을 다시 확인해 주세요.",
    cr.NOT_LIVE: "공개 중이거나 승인 대기 중인 명함이 없어 보내지 않았습니다. 이미 내려갔을 수 있습니다.",
    UNKNOWN: "명함 사이트에 보냈지만 내려갔는지 확인하지 못했습니다. 명함 사이트 관리 화면에서 확인한 뒤 필요하면 다시 해 주세요.",
    WRITE_OFF: "명함 사이트 쓰기가 아직 꺼져 있어 보내지 않았습니다. 지금은 명함 사이트 관리 화면에서 내려 주세요.",
    NOT_CONFIGURED: "명함 사이트 연동 키가 아직 없어 보내지 않았습니다.",
    FAILED: "명함 사이트에 보내지 못했습니다. 잠시 뒤 다시 해 주세요.",
}
REMOVE_NEUTRAL = {IN_FLIGHT, cr.NOT_LIVE, UNKNOWN, WRITE_OFF, NOT_CONFIGURED}


def _today() -> date:
    """배지 판정의 오늘. 시험은 이 함수를 바꿔 끼운다."""
    return date.today()


def _day_ko(iso: str | None) -> str:
    """'2026-12-31' 을 '2026년 12월 31일' 로."""
    if not iso or len(str(iso)) < 10:
        return ""
    v = str(iso)
    return f"{v[:4]}년 {int(v[5:7])}월 {int(v[8:10])}일"


def _notice_rows(con, ctx) -> list:
    """범위 안 설계사 가운데 명함 주소가 이어진 사람의 새 연도 자격 안내. 새 자격이 늦게 생긴 차례다. 만료는 없다. D-55"""
    notices = cb.new_year_notices(con, ctx["agent_ids"], today=_today())
    who = _people(con, ctx["ym"], {n.agent_id for n in notices})
    out = []
    for n in notices:
        p = who.get(n.agent_id) or {}
        b = n.badge
        out.append({"agent_id": b.agent_id, "code": p.get("code"), "name": p.get("name"), "org": p.get("org"), "left": bool(p.get("left")),
                    "slug": n.slug, "label": b.label, "newer": n.newer_label, "notice": n.notice,
                    "field_ko": FIELD_KO.get(b.card_field, b.card_field)})
    return out


def _candidate_rows(con, ctx, client, refresh: bool) -> tuple:
    """해촉자 명함 내리기 후보. (상태, 줄, 안내). 이름은 앱 DB 의 설계사 이름이다."""
    out = cr.removal_candidates(con, actor=ctx["login_id"], is_main=ctx["role"] == "main", client=client,
                                refresh=refresh and client.configured)
    cands = out.value or []
    who = _people(con, ctx["ym"], {c.agent_id for c in cands})
    rows = []
    for c in cands:
        p = who.get(c.agent_id) or {}
        rows.append({"agent_id": c.agent_id, "slug": c.slug, "code": p.get("code"), "name": p.get("name"), "org": p.get("org"), "left": False,
                     "agent_status_ko": AGENT_STATUS_KO.get(c.agent_status, c.agent_status), "term": _day_ko(c.termination_date),
                     "live": c.live, "state_ko": "공개 중" if c.live else (c.last_status_ko if c.last_status else "확인 전"),
                     "pending": c.last_status == "pending", "checked": _local(c.last_checked_at), "url": c.url})
    return out.status, rows, out.reason


def _tools(con, ctx, client, *, refresh=False, ask_remove=None) -> dict:
    """메인 관리자 전용 묶음의 값. 하나가 실패해도 다른 하나는 보인다."""
    tools = {"notices": [], "notices_more": 0, "notices_error": False, "cand_state": OK, "cands": [], "cand_note": "",
             "rm_confirm": None, "rm_missing": False, "days": cb.NOTICE_DAYS, "reason_help": REMOVE_REASON_HELP,
             "configured": client.configured, "at_label": _hm(datetime.now(KST)), "as_of": _day_ko(_today().isoformat()), "refreshed": refresh}
    try:
        rows = _notice_rows(con, ctx)
        tools["notices"], tools["notices_more"] = rows[:NOTICE_SHOW], max(len(rows) - NOTICE_SHOW, 0)
    except sqlite3.Error:
        con.rollback()
        tools["notices_error"] = True
    tools["cand_state"], tools["cands"], tools["cand_note"] = _candidate_rows(con, ctx, client, refresh)
    if ask_remove:
        row = next((r for r in tools["cands"] if r["slug"] == ask_remove), None)
        tools["rm_confirm"] = row
        tools["rm_missing"] = row is None
    return tools


# ---------------------------------------------------------------- 화면
def _page(request, ctx, con, *, flash=None, ask=None, status_code=200, refresh=False, ask_remove=None):
    is_main = ctx["role"] == "main"
    client = card_client(con)
    links = CardLinkTable(con)
    snap = _fetch(client) if client.admin_configured else None
    res = pending_for_scope(snap or client, links, ctx["agent_ids"], is_main, **_self_of(ctx))
    if snap is not None and snap.out.status == OK:
        try:
            links.record_entries(snap.out.value or [])          # 이어진 주소의 마지막 상태를 연결 표에 적는다
        except Exception:
            pass
    linked = _rows(con, ctx, res.value["linked"]) if res.status == OK else []
    unlinked = _rows(con, ctx, res.value["unlinked"]) if res.status == OK and is_main else []
    if not is_main:                                              # 범위 밖이 섞이지 않게 한 번 더 거른다
        linked = [r for r in linked if r["agent_id"] in ctx["agent_ids"]]
    # 확인 단계. 지금 목록에 있는 요청만 연다
    confirm = None
    if ask:
        do, slug = ask
        row = next((r for r in linked + unlinked if r["slug"] == slug), None)
        if row is None:
            flash = flash or {"kind": "warn", "text": "목록에 없는 요청입니다. 이미 처리되었거나 볼 수 없는 요청입니다."}
        elif row["mine"]:
            flash = flash or {"kind": "warn", "text": "본인의 명함은 메인 관리자가 처리합니다."}
        else:
            confirm = {"do": do, **row}
    state = "ok" if res.status == OK else ("not_configured" if res.status == NOT_CONFIGURED else "failed")
    tools = _tools(con, ctx, client, refresh=refresh, ask_remove=ask_remove) if is_main else None
    if tools and tools["rm_missing"]:
        flash = flash or {"kind": "warn", "text": "내리기 후보에 없는 명함입니다. 이미 내려갔거나 재직 중인 설계사의 명함입니다."}
    resp = page(request, ctx, "card_approve.html", status_code=status_code, linked=linked, unlinked=unlinked, is_main=is_main, tools=tools,
                state=state, at_label=_hm(snap.at) if snap else None, write_on=client.write_enabled, flash=flash, confirm=confirm,
                choices=_agent_choices(con, ctx["ym"]) if is_main and unlinked else [], reason_max=REASON_MAX)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _preview_page(request, ctx, con, slug: str):
    """신청본 미리보기. 범위, 권한, 대기 확인, 다듬기, 변경 기록은 preview_card 가 한다."""
    is_main = ctx["role"] == "main"
    client = card_client(con)
    if not SLUG_RE.match(slug):
        out_status, out_reason, value = FAILED, "명함 주소가 바르지 않습니다.", None
        code = 400
    else:
        out = preview_card(con, client, slug=slug, path=PREVIEW_FILE, actor=ctx["login_id"], scope_agent_ids=ctx["agent_ids"],
                           is_main=is_main, inline_images=True)
        out_status, out_reason, value = out.status, out.reason, out.value
        code = 403 if out.status == DENIED else 200
    frame, row = None, None
    if out_status == OK and isinstance(value, CardFile):
        doc = value.data.decode("utf-8", errors="replace")
        frame = Markup(iframe_tag(srcdoc=doc, title="명함 신청본 미리보기", extra=(("class", "card-frame"),)))
        agent_id = CardLinkTable(con).agent_for(slug)
        p = _people(con, ctx["ym"], {agent_id}).get(agent_id, {}) if agent_id is not None else {}
        row = {"slug": slug, "agent_id": agent_id, "code": p.get("code"), "name": p.get("name"), "org": p.get("org"),
               "left": bool(p.get("left")), "mine": _mine(ctx, agent_id)}
    elif out_status == OK:
        out_status, out_reason = FAILED, "보여 드릴 수 있는 신청본이 아닙니다."
    notice = None
    if out_status != OK:
        detail = out_reason if out_status in (DENIED, NO_PENDING, FAILED) and out_reason else ""
        notice = {"kind": "warn" if out_status in (NO_PENDING, NOT_FOUND, TOO_BIG, NOT_CONFIGURED) else "bad",
                  "text": PREVIEW_LINE.get(out_status, PREVIEW_LINE[FAILED]), "detail": detail, "state": out_status}
    resp = page(request, ctx, "card_preview.html", status_code=code, frame=frame, row=row, notice=notice, slug=slug,
                is_main=is_main, write_on=client.write_enabled, at_label=_hm(datetime.now(KST)))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@router.get("/sales/card-approve", response_class=HTMLResponse)
def card_approve(request: Request, do: str = "", slug: str = "", preview: str | None = None, refresh: str = "", ctx: dict = Depends(guard),
                 con=Depends(get_con)):
    if preview is not None:
        return _preview_page(request, ctx, con, preview.strip().lower())
    flash = request.session.pop(FLASH, None)
    slug = slug.strip().lower()
    ask = (do, slug) if do in ("approve", "reject") and SLUG_RE.match(slug) else None
    is_main = ctx["role"] == "main"
    ask_remove = slug if is_main and do == "remove" and SLUG_RE.match(slug) else None   # 관리자에게는 내리기 확인 단계를 열지 않는다
    return _page(request, ctx, con, flash=flash, ask=ask, refresh=is_main and refresh == "1", ask_remove=ask_remove)


def _remove_flash(out: Outcome) -> dict:
    """내리기 결과 여덟 가지의 안내. 사유 원문은 담지 않는다. 거부와 실패와 확인 필요만 엔진의 설명을 붙인다."""
    line = REMOVE_LINE.get(out.status, REMOVE_LINE[FAILED])
    if out.status == OK and getattr(out.value, "confirmed_by_status", False):
        line += " 응답은 받지 못했지만 공개 상태에서 내려간 것을 확인했습니다."
    detail = out.reason if out.status in (DENIED, FAILED, UNKNOWN) and out.reason else ""
    kind = "ok" if out.status == OK else ("warn" if out.status in REMOVE_NEUTRAL else "bad")
    return {"kind": kind, "text": line, "detail": detail, "state": out.status}


def _remove(request, ctx, con, slug: str, reason: str, confirm: str):
    """해촉자 명함 내리기. 메인 관리자만, 확인 단계를 거친 뒤에만 remove_card 로 보낸다."""
    if ctx["role"] != "main":
        auth.log(con, ctx["login_id"], "access_denied", f"{PATH} remove")
        raise HTTPException(403, "명함 내리기는 메인 관리자만 합니다.")
    if confirm != "1":                                            # 확인 단계를 거치지 않은 요청은 확인 단계로 돌린다
        return RedirectResponse(f"{PATH}?do=remove&slug={slug}#rm-confirm", status_code=303)
    if len(reason.strip()) > REASON_MAX:
        return _page(request, ctx, con, flash={"kind": "bad", "text": f"사유는 {REASON_MAX}자까지 적을 수 있습니다. 줄여서 다시 적어 주세요."},
                     ask_remove=slug, status_code=400)
    out = cr.remove_card(con, card_client(con), slug=slug, reason=reason, actor=ctx["login_id"], is_main=True)
    flash = _remove_flash(out)
    if out.status == DENIED:                                      # 사유를 고쳐 다시 하도록 확인 단계를 다시 연다. 사유는 되돌려 싣지 않는다
        return _page(request, ctx, con, flash=flash, ask_remove=slug, status_code=400)
    request.session[FLASH] = flash
    return RedirectResponse(f"{PATH}#{TOOLS}", status_code=303)


def _flash_for(out: Outcome, action: str) -> dict:
    if out.status == OK:
        text = "승인했습니다. 이 명함 링크가 이제 공개됩니다." if action == "approve" else "반려했습니다. 사유는 명함 사이트로만 보냈습니다."
        return {"kind": "ok", "text": text}
    line = RESULT_LINE.get(out.status, RESULT_LINE[FAILED])
    detail = out.reason if out.status in (DENIED, FAILED) and out.reason else ""
    return {"kind": "warn" if out.status in NEUTRAL else "bad", "text": line, "detail": detail}


@router.post("/sales/card-approve", response_class=HTMLResponse)
def card_approve_action(request: Request, action: str = Form(...), slug: str = Form(""), reason: str = Form(""), confirm: str = Form(""),
                        code: str = Form(""), ctx: dict = Depends(guard), con=Depends(get_con)):
    is_main = ctx["role"] == "main"
    slug = slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "명함 주소가 바르지 않습니다.")

    if action == "remove":
        return _remove(request, ctx, con, slug, reason, confirm)

    if action == "link":
        if not is_main:
            raise HTTPException(403, "명함 주소는 메인 관리자만 잇습니다.")
        a = con.execute("SELECT agent_id FROM agent WHERE internal_code=? AND status='active'", (code.strip().upper(),)).fetchone()
        if a is None:
            return _page(request, ctx, con, flash={"kind": "bad", "text": "재직 중인 설계사의 사번을 넣어 주세요."}, status_code=400)
        try:
            CardLinkTable(con).link(a["agent_id"], slug, ctx["login_id"])
        except LinkError as e:
            con.rollback()
            return _page(request, ctx, con, flash={"kind": "bad", "text": f"잇지 못했습니다. {e}"}, status_code=400)
        request.session[FLASH] = {"kind": "ok", "text": "이었습니다. 이어서 승인하거나 반려해 주세요."}
        return RedirectResponse(f"{PATH}#req-{slug}", status_code=303)

    if action not in ("approve", "reject"):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    if action == "approve" and confirm != "1":                    # 승인은 확인 단계를 한 번 거친다
        return RedirectResponse(f"{PATH}?do=approve&slug={slug}", status_code=303)
    if action == "reject" and len(reason.strip()) > REASON_MAX:
        return _page(request, ctx, con, flash={"kind": "bad", "text": f"반려 사유는 {REASON_MAX}자까지 적을 수 있습니다. 줄여서 다시 적어 주세요."},
                     ask=("reject", slug), status_code=400)
    out = decide_card(con, card_client(con), slug=slug, action=action, actor=ctx["login_id"], scope_agent_ids=ctx["agent_ids"],
                      is_main=is_main, reason=reason if action == "reject" else "")
    flash = _flash_for(out, action)
    if out.status == DENIED:
        return _page(request, ctx, con, flash=flash, status_code=403)
    request.session[FLASH] = flash
    return RedirectResponse(PATH, status_code=303)


# ---------------------------------------------------------------- 내 명함
MY_STATE_KO = {"live": "공개", "pending": "대기", "rejected": "반려됨", "none": "없음"}
MAKE_PATH = "/make/"              # 명함 만들기 화면. 명함 사이트 public/make/index.html 을 사이트 주소 뒤에 둔 가정이다


# 배지 상태의 화면 이름. 배지는 퇴사 전까지 유지해 만료가 없다. D-55
BADGE_KO = {cb.VALID: "유효", cb.EXPECTED: "확정 전"}
BADGE_TONE = {cb.VALID: "good", cb.EXPECTED: ""}
BADGE_ORDER = {cb.VALID: 0, cb.EXPECTED: 1}
FIELD_KO = {cb.FIELD_CERTS: "자격·수상 배지", cb.FIELD_ETC: "그 밖의 자격·수상 칸"}
EXPECTED_NOTE = "확정 전, 명함에 올릴 수 없습니다. 인증이 확정되면 올릴 수 있습니다."
KEEP_NOTE = "배지와 명함은 퇴사 전까지 유지됩니다. 해가 바뀌어도 지난 연도 배지를 그대로 두셔도 됩니다."
PUBLISH_HELP = "공개로 두면 명예의전당에서 다른 분께도 이 항목이 보입니다. 기본은 비공개이고, 공개 여부는 본인만 바꿀 수 있습니다."
MY_FLASH = "card_my_flash"
MY_PATH = "/sales/card"


def _how(b) -> str:
    """명함 만들기에서 이 배지를 넣는 법."""
    if b.card_field == cb.FIELD_CERTS and b.badge in cb.ANNUAL and b.year:
        return f"명함 만들기의 자격·수상에서 {b.badge} 를 고르고 연도에 {b.year} 를 넣습니다."
    if b.card_field == cb.FIELD_CERTS:
        return f"명함 만들기의 자격·수상에서 {b.badge} 를 고릅니다."
    return f"명함 만들기의 그 밖의 자격·수상 칸에 {b.label} 로 적습니다."


def my_badges(con, agent_id, today: date | None = None) -> dict | None:
    """본인 한 사람의 배지와 공개 여부. 설계사 번호가 없으면 None. 판정과 공개는 card_badges 가 하고 이 함수는 화면 꼴로 묶을 뿐이다."""
    if agent_id is None:
        return None
    today = today or _today()
    try:
        badges = cb.judge_badges(con, [agent_id], today=today)
        pub = {i.item_key: i for i in cb.publish_items(con, agent_id, today=today)}
        notices = cb.new_year_notices(con, [agent_id], today=today, linked_only=False)
    except sqlite3.Error:
        con.rollback()
        return {"rows": [], "error": True, "as_of": _day_ko(today.isoformat()), "counts": {}, "notices": []}
    rows = []
    for b in badges:
        if b.agent_id != agent_id:                                # 본인 것만
            continue
        item = pub.get(b.key)
        rows.append({"key": b.key, "label": b.label, "status": b.status, "status_ko": BADGE_KO.get(b.status, b.status),
                     "tone": BADGE_TONE.get(b.status, ""), "eligible": b.eligible, "field_ko": FIELD_KO.get(b.card_field, b.card_field),
                     "basis_ko": cb.BASIS_KO.get(b.basis, b.basis), "origin": b.origin, "how": _how(b) if b.eligible else "",
                     "can_publish": item is not None, "published": bool(item and item.published),
                     "changed": _local(item.changed_at) if item and item.changed_at else "",
                     "expected_note": EXPECTED_NOTE if b.status == cb.EXPECTED else None})
    rows.sort(key=lambda r: (BADGE_ORDER.get(r["status"], 9), r["label"]))
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in BADGE_KO}
    return {"rows": rows, "error": False, "as_of": _day_ko(today.isoformat()), "counts": counts,
            "eligible": sum(1 for r in rows if r["eligible"]), "published": sum(1 for r in rows if r["published"]),
            "notices": [{"notice": n.notice, "newer": n.newer_label} for n in notices if n.agent_id == agent_id]}


def _my_state(status: str, live: bool) -> str:
    """사이트 상태를 화면의 넷으로. 공개 중이면 공개다. 내린 명함과 신청 없음은 없음이다."""
    if live:
        return "live"
    return status if status in ("pending", "rejected") else "none"


def _local(s: str | None) -> str:
    """db.now() 꼴(앱 시각)의 글을 '11월 14일 9시 05분' 으로."""
    if not s:
        return ""
    try:
        return _hm(datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return str(s)[:16]


@router.get("/sales/card", response_class=HTMLResponse)
def my_card_page(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    client = card_client(con)
    links = CardLinkTable(con)
    agent_id = ctx.get("agent_id")
    slug = links.slug_for(agent_id) if agent_id is not None else None
    view = {"slug": slug, "make_url": f"{client.base_url}{MAKE_PATH}" if client.base_url else None, "state": None, "fresh": False,
            "url": None, "new_request": None, "live_at": None, "checked": None, "problem": None}
    if slug is None:
        view["problem"] = "unlinked"
    elif not client.configured:
        view["problem"] = "not_configured"
    else:
        out = my_card(client, links, agent_id)            # GET 한 번. 읽은 상태와 시각은 연결 표에 적는다
        if out.status == OK and out.value is not None:
            st = out.value
            view.update(state=_my_state(st.status, st.live), fresh=True, url=st.url, live_at=_when(st.live_at) if st.live_at else None,
                        new_request=st.status if st.live and st.status in ("pending", "rejected") else None)
        else:
            view["problem"] = "failed"
    if slug is not None:
        row = links.get(agent_id) or {}
        if not view["fresh"] and row.get("last_status"):
            view["state"] = _my_state(row["last_status"], bool(row.get("last_live")))
            view["url"] = client.card_url(slug) if row.get("last_live") else None
        view["checked"] = _local(row.get("last_checked_at"))
    view["state_ko"] = MY_STATE_KO.get(view["state"]) if view["state"] else None
    resp = page(request, ctx, "card_my.html", v=view, badges=my_badges(con, agent_id), flash=request.session.pop(MY_FLASH, None),
                keep_note=KEEP_NOTE, publish_help=PUBLISH_HELP)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/sales/card", response_class=HTMLResponse)
def my_card_publish(request: Request, action: str = Form(""), item_key: str = Form(""), published: str = Form(""),
                    ctx: dict = Depends(guard), con=Depends(get_con)):
    """배지 하나의 공개 여부를 본인이 바꾼다. D-77. 설계사 번호는 세션에서만 읽는다. 본인 확인과 자격 확인은 set_publish 가 한 번 더 한다."""
    if action != "publish" or published not in ("0", "1") or not item_key or len(item_key) > 200:
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    agent_id = ctx.get("agent_id")
    if agent_id is None:
        raise HTTPException(403, "공개 여부는 본인만 바꿀 수 있습니다.")
    out = cb.set_publish(con, agent_id, item_key, published == "1", ctx["login_id"], today=_today())
    kind = "ok" if out.status == OK else ("warn" if out.status == cb.UNCHANGED else "bad")
    text = (out.reason or "공개 여부를 저장하지 못했습니다. 잠시 뒤 다시 해 주세요").rstrip(".") + "."
    request.session[MY_FLASH] = {"kind": kind, "text": text}
    return RedirectResponse(f"{MY_PATH}#badges", status_code=303)
