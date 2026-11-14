"""데이터 묶음의 사원현황: 엑셀을 올려 미리보기하고 반영한다. 메인 관리자만 쓴다.

올린 파일은 저장소 밖 자료 폴더(data/raw/uploads, APP_UPLOAD_DIR 로 바꿈)에 두었다가 반영하면 지운다.
미리보기와 반영은 같은 동기화 함수를 돌리고, 미리보기는 끝에 되돌린다. 그래서 두 화면의 건수가 같다.
사람 이름은 화면에만 보이고 변경 기록에는 건수만 남긴다.
"""
import os
import pathlib
import re
import secrets
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from app import db
from app.loader.eternal_employee import parse_employee_file
from app.loader.sheet import HeaderMismatch
from app.org import sync
from app.web import auth
from app.web.deps import get_con, guard, page

router = APIRouter()
SESSION_KEY = "staff_upload"
TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
YM = re.compile(r"^20\d\d(0[1-9]|1[0-2])$")
MAX_BYTES = 30 * 1024 * 1024
SHOW = ("new_orgs", "moved", "terminated", "new_agents")
MORE = ("reinstated", "placed", "heads_new", "heads_end", "renamed", "no_org", "head_without_org", "missing")
KIND_KO = {"new_agents": "새 설계사", "moved": "이동", "terminated": "퇴사", "reinstated": "복직", "placed": "배치",
           "heads_new": "조직장 지정", "heads_end": "조직장 지정 끝"}


def upload_dir() -> pathlib.Path:
    d = pathlib.Path(os.environ.get("APP_UPLOAD_DIR") or db.ROOT / "data" / "raw" / "uploads")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(token) -> pathlib.Path | None:
    if not token or not TOKEN.match(token):
        return None
    p = upload_dir() / f"staff_{token}.xlsx"
    return p if p.exists() else None


def _parse(path):
    """(rows, warnings, dropped, error)"""
    try:
        res = parse_employee_file(path)
    except HeaderMismatch as e:
        return None, [], [], f"사원현황 양식이 아닙니다. {e}"
    except Exception:                                          # 엑셀이 아니거나 깨진 파일
        return None, [], [], "파일을 읽지 못했습니다. 사원현황 엑셀 파일인지 확인하세요."
    return res["rows"], res["warnings"], res["dropped_columns"], None


def _contract_preview(con):
    return sync.sync_contract_orgs(con, dry_run=True)


def _page(request, ctx, con, **kw):
    kw.setdefault("preview", None)
    kw.setdefault("result", None)
    kw.setdefault("err", None)
    kw.setdefault("contract", _contract_preview(con))
    kw.setdefault("ym", date.today().strftime("%Y%m"))
    status = kw.pop("status_code", 200)
    return page(request, ctx, "data_staff.html", SHOW=SHOW, MORE=MORE, STAT_KO=sync.STAT_KO, KIND_KO=KIND_KO, status_code=status, **kw)


@router.get("/data/staff", response_class=HTMLResponse)
def staff_form(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return _page(request, ctx, con)


@router.post("/data/staff", response_class=HTMLResponse)
async def staff_action(request: Request, action: str = Form(...), effective_ym: str = Form(""), file: UploadFile | None = File(None),
                       ctx: dict = Depends(guard), con=Depends(get_con)):
    by = ctx["login_id"]
    if action == "contract_orgs":
        res = sync.sync_contract_orgs(con)
        auth.log(con, by, "org_from_contracts", f"마디 {res['nodes']}, 새 조직 {res['new_orgs']}")
        return _page(request, ctx, con, result={"contract": res})

    ym = effective_ym.strip()
    if not YM.match(ym):
        return _page(request, ctx, con, err="반영 월을 YYYYMM 으로 넣으세요.", status_code=400)

    if action == "preview":
        if file is None or not file.filename:
            return _page(request, ctx, con, err="사원현황 엑셀 파일을 고르세요.", ym=ym, status_code=400)
        data = await file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return _page(request, ctx, con, err="파일이 너무 큽니다.", ym=ym, status_code=400)
        old = _file((request.session.get(SESSION_KEY) or {}).get("token"))
        if old:
            old.unlink(missing_ok=True)
        token = secrets.token_urlsafe(24)
        path = upload_dir() / f"staff_{token}.xlsx"
        path.write_bytes(data)
        rows, warnings, dropped, err = _parse(path)
        if err:
            path.unlink(missing_ok=True)
            request.session.pop(SESSION_KEY, None)
            return _page(request, ctx, con, err=err, ym=ym, status_code=400)
        res = sync.sync_staff(con, rows, ym, dry_run=True)
        request.session[SESSION_KEY] = {"token": token, "ym": ym, "file": (file.filename or "")[:120]}
        return _page(request, ctx, con, ym=ym, preview={**res, "warnings": warnings, "dropped": dropped, "file": file.filename, "ym": ym})

    if action == "apply":
        saved = request.session.get(SESSION_KEY) or {}
        path = _file(saved.get("token"))
        if not path or saved.get("ym") != ym:
            return _page(request, ctx, con, err="미리보기한 파일이 없습니다. 파일을 다시 올려 미리보기부터 하세요.", ym=ym, status_code=400)
        rows, warnings, _, err = _parse(path)
        if err:
            return _page(request, ctx, con, err=err, ym=ym, status_code=400)
        res = sync.sync_staff(con, rows, ym)
        path.unlink(missing_ok=True)
        request.session.pop(SESSION_KEY, None)
        st = res["stats"]
        auth.log(con, by, "staff_sync", f"{ym} " + ", ".join(f"{sync.STAT_KO[k]} {st[k]}" for k in SHOW + MORE[:4]))
        no_account = con.execute("""SELECT COUNT(*) FROM agent a WHERE status='active'
                                    AND NOT EXISTS (SELECT 1 FROM user_account u WHERE u.agent_id=a.agent_id)""").fetchone()[0]
        return _page(request, ctx, con, ym=ym, result={"staff": res, "file": saved.get("file"), "no_account": no_account})

    raise HTTPException(400, "처리할 수 없는 요청입니다.")
