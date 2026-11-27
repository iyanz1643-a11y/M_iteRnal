"""공통 묶음의 공지와 자료실 /board. 저장과 보는 사람 규칙은 app/web/board.py 에 있다.

한 주소에서 모두 한다. 모든 요청이 guard 를 거친다.

| 요청 | 하는 일 | 누가 |
| --- | --- | --- |
| GET /board | 목록. ?cat= 분류로 거른다 | 전원. 보이는 글만 |
| GET /board?post=번호 | 글 하나와 첨부 | 그 글이 보이는 사람 |
| GET /board?file=번호 | 첨부 내려받기. attachment, nosniff, no-store. 변경 기록에 남긴다 | 그 글이 보이는 사람 |
| GET /board?new=1, ?edit=번호 | 올리기와 고치기 폼 | 메인 관리자 |
| POST /board action=save | 올리기, 고치기 | 메인 관리자 |
| POST /board action=withdraw | 내리기. 확인 단계를 한 번 거친다 | 메인 관리자 |

보이지 않는 글과 첨부는 없는 것과 같은 404 로 답하고, 있는 것을 연 시도는 권한 밖 접근으로 남긴다.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.web import auth, org_labels
from app.web import board as bd
from app.web.deps import get_con, guard, page

router = APIRouter()
PATH = "/board"
FLASH = "board_flash"


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _deny(con, ctx, what: str, message: str = "메인 관리자만 할 수 있는 일입니다."):
    auth.log(con, ctx["login_id"], "access_denied", f"{PATH} {what}")
    raise HTTPException(403, message)


def _not_found(con, ctx, what: str | None = None):
    if what:                                              # 있는 글이나 첨부를 대상 밖 사람이 열었다
        auth.log(con, ctx["login_id"], "access_denied", f"{PATH} {what}")
    raise HTTPException(404, "없는 자료입니다.")


def _form_defaults(p=None) -> dict:
    if not p:
        return {"post_id": "", "category": "notice", "title": "", "body": "", "target_org_id": "", "target_role": "", "fc_visible": ""}
    return {"post_id": p["post_id"], "category": p["category"], "title": p["title"], "body": p["body"] or "",
            "target_org_id": p["target_org_id"] or "", "target_role": p["target_role"] or "",
            "fc_visible": "" if p["fc_visible"] is None else str(p["fc_visible"])}


def _page(request, ctx, con, *, cat="", open_id=None, form=None, error=None, ask=None, status_code=200):
    bd.ensure(con)
    is_main = ctx["role"] == "main"
    texts = org_labels.texts(con)
    rows = bd.visible_posts(con, ctx, include_withdrawn=is_main)
    live = [p for p in rows if p["status"] == "posted"]
    withdrawn = [p for p in rows if p["status"] != "posted"]
    counts = {k: sum(1 for p in live if p["category"] == k) for k in bd.CATEGORY_KO}
    shown = [p for p in live if not cat or p["category"] == cat]
    for p in rows:
        p["target_ko"] = bd.target_text(p, texts)
    opened = None
    if open_id is not None:
        opened = next((p for p in rows if p["post_id"] == open_id), None)
        if opened is not None:
            opened = {**opened, "files": bd.files(con, open_id)}
    if form is not None and form.get("post_id"):
        form = {**form, "files": bd.files(con, _int(form["post_id"]))}
    orgs = org_labels.options(con, keep={_int(form.get("target_org_id"))}) if is_main and form is not None else []     # 시작 조직의 위쪽 조직은 뺀다. 지금 고른 조직은 남긴다
    resp = page(request, ctx, "board.html", status_code=status_code, is_main=is_main, posts=shown, withdrawn=withdrawn, counts=counts,
                n_live=len(live), cat=cat, opened=opened, form=form, error=error, ask=ask, orgs=orgs,
                flash=request.session.pop(FLASH, None), CATEGORY_KO=bd.CATEGORY_KO, ROLE_KO=bd.ROLE_KO, KIND_KO=bd.KIND_KO,
                ACCEPT=bd.ACCEPT, MAX_MB=bd.MAX_BYTES // (1024 * 1024), MAX_FILES=bd.MAX_FILES, TITLE_MAX=bd.TITLE_MAX, BODY_MAX=bd.BODY_MAX)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _download(request, ctx, con, file_id):
    f = bd.file_row(con, file_id) if file_id is not None else None
    if f is None:
        _not_found(con, ctx)
    p = bd.post(con, f["post_id"])
    if p is None or not bd.can_see(ctx, p, bd.viewer_orgs(con, ctx)):
        _not_found(con, ctx, f"file:{file_id}")
    if f["removed_at"]:
        _not_found(con, ctx)
    path = bd.blob_path(f["sha256"])
    if path is None:
        raise HTTPException(404, "파일을 찾지 못했습니다. 메인 관리자에게 알려 주세요.")
    bd.audit(con, ctx["login_id"], "board_download", f"{p['post_id']}:{f['file_id']}")
    con.commit()
    return FileResponse(path, media_type=bd.content_type(f["name"]), headers={
        "Content-Disposition": bd.disposition(f["name"]), "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/board", response_class=HTMLResponse)
def board(request: Request, cat: str = "", post: str = "", file: str = "", new: str = "", edit: str = "", ask: str = "",
          ctx: dict = Depends(guard), con=Depends(get_con)):
    bd.ensure(con)
    if file:
        return _download(request, ctx, con, _int(file))
    cat = cat if cat in bd.CATEGORY_KO else ""
    is_main = ctx["role"] == "main"
    if (new or edit) and not is_main:
        _deny(con, ctx, "edit")
    form = None
    if new == "1":
        form = _form_defaults()
        if cat:
            form["category"] = cat
    elif edit:
        p = bd.post(con, _int(edit))
        if p is None or p["status"] != "posted":
            _not_found(con, ctx)
        form = _form_defaults(p)
    open_id = None
    if post:
        open_id = _int(post)
        p = bd.post(con, open_id) if open_id is not None else None
        if p is None:
            _not_found(con, ctx)
        if not bd.can_see(ctx, p, bd.viewer_orgs(con, ctx)):
            _not_found(con, ctx, f"post:{open_id}")
    return _page(request, ctx, con, cat=cat, open_id=open_id, form=form, ask="withdraw" if ask == "withdraw" and is_main and open_id else None)


async def _uploads(form) -> list:
    """폼의 새 첨부를 읽어 검사한다. (이름, 꼴, 내용) 목록. 하나라도 틀리면 ValueError 이고 아무것도 쓰지 않는다."""
    out = []
    for up in form.getlist("files"):
        if not getattr(up, "filename", ""):
            continue
        data = await up.read(bd.MAX_BYTES + 1)
        name, kind = bd.check_file(up.filename, data)
        out.append((name, kind, data))
    if len(out) > bd.MAX_FILES:
        raise ValueError(f"첨부는 한 글에 {bd.MAX_FILES}개까지입니다.")
    return out


@router.post("/board", response_class=HTMLResponse)
async def board_post(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    bd.ensure(con)
    form = await request.form()
    action = form.get("action", "")
    if ctx["role"] != "main":                             # 관리자와 설계사는 올리지 않는다. docs/09 2.4
        _deny(con, ctx, action or "post")
    pid = _int(form.get("post_id")) if form.get("post_id") else None
    if action == "withdraw":
        if pid is None or bd.post(con, pid) is None:
            raise HTTPException(400, "처리할 수 없는 요청입니다.")
        if form.get("confirm") != "1":
            return RedirectResponse(f"{PATH}?post={pid}&ask=withdraw#confirm", status_code=303)
        try:
            bd.withdraw(con, pid, ctx["login_id"])
        except ValueError as e:
            con.rollback()
            return _page(request, ctx, con, open_id=pid, error=str(e), status_code=400)
        con.commit()
        _clear_notes()
        request.session[FLASH] = {"kind": "ok", "text": "글을 내렸습니다. 대상자에게 더 보이지 않고 기록은 남습니다."}
        return RedirectResponse(PATH, status_code=303)
    if action != "save":
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    raw = {k: form.get(k, "") for k in ("category", "title", "body", "target_org_id", "target_role", "fc_visible")}
    keep = {**raw, "post_id": pid or ""}
    try:
        fields = bd.clean_fields(con, raw)
        uploads = await _uploads(form)
        if pid:
            bd.update(con, pid, ctx["login_id"], fields, uploads, form.getlist("remove"))
        else:
            pid = bd.create(con, ctx["login_id"], fields, uploads)
    except ValueError as e:
        con.rollback()
        return _page(request, ctx, con, form=keep, error=f"{e} 첨부 파일은 다시 골라 주세요.", status_code=400)
    con.commit()
    _clear_notes()
    request.session[FLASH] = {"kind": "ok", "text": "고친 내용을 저장했습니다." if keep["post_id"] else "올렸습니다. 대상자의 공지와 자료실에 바로 보입니다."}
    return RedirectResponse(f"{PATH}?post={pid}", status_code=303)


def _clear_notes():
    """새 공지가 종 아이콘에 바로 보이게 알림 목록을 비운다."""
    from app.web.routes import notify
    notify.clear_cache()
