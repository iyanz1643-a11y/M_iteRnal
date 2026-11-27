"""조직·설정 묶음의 조직별 설정 /org/settings. 파트 H.

상속, 막기, 적용 월, 이력의 규칙은 app/org/settings.py 한 곳에 있고 이 파일은 그것을 보여 주고 부르기만 한다.
- 보기: 메인 관리자는 모든 조직, 관리자는 자기 조직과 그 아래. 범위 밖 조직을 열면 403 이고 변경 기록에 남는다.
- 바꾸기: 고르고, 확인 단계에서 바뀌는 값과 시작 달을 다시 보여 준 뒤 보낸다. POST 는 앱 전체의 CSRF 검사를 거친다.
  누가 바꿀 수 있는지는 settings.can_edit 와 set_value 가 정한다. 화면은 단추를 그릴지만 정한다.
- 막기와 풀기는 메인 관리자만 한다.
값을 바꾸면 알림함이 담아 둔 목록을 비워 종 아이콘 숫자에 바로 반영한다.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.org import levels
from app.org import settings as st
from app.web import auth, org_labels
from app.web.deps import get_con, guard, page
from app.web.routes import notify

router = APIRouter()
FLASH = "org_settings_flash"
DO_KO = {"set": "바꾸기", "inherit": "위 조직 값 따르기", "lock": "막기", "unlock": "풀기"}
DO_ASK = {"set": "바꿀까요", "inherit": "위 조직의 값을 따르게 할까요", "lock": "막을까요", "unlock": "풀까요"}


def _orgs(con, ctx, keep=()) -> list:
    """조직 선택 상자의 줄. 시작 조직의 위쪽 조직은 뺀다(org_labels). 지금 고른 조직은 남긴다."""
    texts = org_labels.texts(con)
    rows = [{"org_id": r[0], "level": r[1], "text": texts.get(r[0], r[2]), "name": r[2], "path_key": r[3]}
            for r in con.execute("SELECT org_id, level, name, path_key FROM org_unit WHERE active=1") if r[0] in ctx["org_ids"]]
    rows = org_labels.choose(con, rows, keep=keep)
    rows.sort(key=lambda o: (levels.depth(o["level"]) > 0, o["text"]))
    return rows


def _default_org(con, ctx):
    if ctx["role"] == "main":
        return org_labels.start_org_id(con) or st.root_org(con)      # 시작 조직이 있으면 그 조직부터 보인다. 위쪽 조직은 선택 상자에서 숨긴다
    heads = sorted(ctx.get("heads") or [], key=lambda h: levels.depth(h["level"]))
    return heads[0]["org_id"] if heads else None


def _pick_org(request, con, ctx, raw) -> int:
    """고른 조직. 범위 밖이면 기록을 남기고 403 이다. 조직이 무엇인지는 화면에 내지 않는다."""
    org_id = int(raw) if str(raw or "").isdigit() else _default_org(con, ctx)
    if org_id is None or org_id not in ctx["org_ids"]:
        auth.log(con, ctx["login_id"], "access_denied", f"{st.PATH} org={raw}")
        raise HTTPException(403, "이 조직의 설정을 볼 권한이 없습니다.")
    return org_id


def _courses(con) -> list:
    try:
        return [dict(r) for r in con.execute("SELECT course_id, name, required FROM edu_course WHERE status='active' ORDER BY name, course_id")]
    except Exception:                                    # 교육 표가 없는 오래된 DB
        con.rollback()
        return []


def _rows(con, ctx, org_id, texts) -> list:
    ym = st.now_ym(con)                                   # 지금 값은 자료 기준 달이 아니라 지금 달의 값이다. 알림함과 같다
    hist = st.history(con, org_id)
    out = []
    for key, it in st.ITEMS.items():
        eff = st.effective(con, org_id, key, ym)
        own = st.own_rows(con, org_id, key)
        here = next((r for r in own if r["from_ym"] <= ym), None)
        src = eff["source"]
        if src == org_id:
            src_text = "이 조직"
        elif src is not None:
            src_text = f"{texts.get(src, '위 조직')}에서 상속"
        else:
            src_text = "기본값"
        lk_org = eff["locked_at"]
        ok, why = st.can_edit(con, ctx, org_id, key, ym)
        out.append({
            "key": key, **it, "value": eff["value"], "value_text": st.text(con, key, eff["value"]), "src_text": src_text,
            "from_ym": eff["from_ym"], "has_own": bool(here and here["value"] is not None),
            "parts": [{"org": texts.get(p["org_id"], ""), "text": st.text(con, key, p["value"])} for p in eff["parts"]],
            "locked_text": ("이 조직에서 막음" if lk_org == org_id else f"{texts.get(lk_org, '위 조직')}에서 막음") if lk_org else "",
            "live_lock": st.live_lock(con, org_id, key),
            "upcoming": [{"from_ym": r["from_ym"], "text": st.text(con, key, r["value"])} for r in own if r["from_ym"] > ym][::-1],
            "can_edit": ok, "why": why, "n_hist": sum(1 for h in hist if h["key"] == key),
        })
    return out


def _form_values(value) -> list:
    """확인 단계의 숨은 칸에 다시 실을 값."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, bool):
        return ["1" if value else "0"]
    return [str(value)]


def _page(request, ctx, con, org_id, edit=None, ask=None, err=None, msg=None, status_code=200):
    texts = org_labels.texts(con)
    rows = _rows(con, ctx, org_id, texts)
    sections = [{"name": g, "rows": [r for r in rows if r["group"] == g]} for g in st.GROUP_ORDER]
    hist = st.history(con, org_id)
    for h in hist:
        it = st.ITEMS.get(h["key"])
        h["name"] = it["name"] if it else h["key"]
        h["what_ko"] = {"set": "값", "inherit": "위 조직을 따름", "lock": "막음", "unlock": "풂"}[h["what"]]
        h["value_text"] = st.text(con, h["key"], h["value"]) if it and h["what"] == "set" else ""
    form = next((r for r in rows if r["key"] == edit), None) if edit else None
    if form and not form["can_edit"] and ctx["role"] != "main":
        form = None
    resp = page(request, ctx, "org_settings.html", status_code=status_code, now_ym=st.now_ym(con), orgs=_orgs(con, ctx, keep={org_id}), org_id=org_id, org_text=texts.get(org_id, ""),
                sections=sections, hist=hist[:100], n_hist=len(hist), form=form, ask=ask, err=err, msg=msg, first_ym=st.first_open_ym(con),
                courses=_courses(con), writable=org_id in st.write_orgs(con, ctx), is_main=ctx["role"] == "main")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/org/settings", response_class=HTMLResponse)
def org_settings(request: Request, org: str = "", edit: str = "", ctx: dict = Depends(guard), con=Depends(get_con)):
    org_id = _pick_org(request, con, ctx, org)
    if edit and edit not in st.ITEMS:
        edit = ""
    return _page(request, ctx, con, org_id, edit=edit or None, msg=request.session.pop(FLASH, None))


@router.post("/org/settings", response_class=HTMLResponse)
async def org_settings_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    f = await request.form()
    action, key = f.get("action", ""), f.get("key", "")
    org_id = _pick_org(request, con, ctx, f.get("org_id"))
    if action not in DO_KO or key not in st.ITEMS:
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    it = st.ITEMS[key]
    from_ym = (f.get("from_ym") or "").strip()
    raw = f.getlist("value") if it["kind"] in ("multi", "courses") else f.get("value")
    if action == "inherit":
        raw = None
    try:
        if action in ("set", "inherit"):
            from_ym = st.check_set(con, org_id, key, from_ym, ctx)      # 확인 단계에서도 막는다. 권한 밖이면 기록이 남는다
            value = st.clean(con, key, raw)
            if f.get("confirm") != "1":
                cur = st.effective(con, org_id, key, st.now_ym(con))
                ask = {"action": action, "do": DO_KO[action], "q": DO_ASK[action], "key": key, "name": it["name"], "from_ym": from_ym,
                       "old": st.text(con, key, cur["value"]), "new": st.text(con, key, value) if value is not None else "위 조직의 값",
                       "values": _form_values(value), "note": (f.get("note") or "").strip()[:200]}
                return _page(request, ctx, con, org_id, ask=ask)
            st.set_value(con, org_id, key, value, from_ym, ctx, note=f.get("note"))
            done = f"{it['name']} 설정을 {from_ym[:4]}년 {int(from_ym[4:])}월부터 " + ("바꿨습니다." if value is not None else "위 조직의 값을 따르게 했습니다.")
        else:
            from_ym = st.check_lock(con, org_id, key, from_ym, ctx, action)
            if f.get("confirm") != "1":
                ask = {"action": action, "do": DO_KO[action], "q": DO_ASK[action], "key": key, "name": it["name"], "from_ym": from_ym, "values": [], "note": ""}
                return _page(request, ctx, con, org_id, ask=ask)
            (st.lock if action == "lock" else st.unlock)(con, org_id, key, from_ym, ctx)
            done = f"{it['name']} 설정을 {from_ym[:4]}년 {int(from_ym[4:])}월부터 " + ("막았습니다. 아래 조직은 이 조직의 값을 씁니다." if action == "lock" else "풀었습니다.")
    except st.SettingDenied as e:
        raise HTTPException(403, str(e)) from None
    except st.SettingError as e:
        con.rollback()
        return _page(request, ctx, con, org_id, edit=key if action in ("set", "inherit") else None, err=str(e), status_code=400)
    notify.clear_cache()
    request.session[FLASH] = done
    return RedirectResponse(f"/org/settings?org={org_id}", status_code=303)
