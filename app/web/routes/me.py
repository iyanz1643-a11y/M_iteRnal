"""내 정보와 비밀번호 바꾸기."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.web import auth
from app.web.deps import get_con, guard, page

router = APIRouter()


@router.get("/me", response_class=HTMLResponse)
def me(request: Request, ctx: dict = Depends(guard)):
    return page(request, ctx, "me.html", message=None, error=None)


@router.post("/me", response_class=HTMLResponse)
def me_password(request: Request, old: str = Form(...), new: str = Form(...), new2: str = Form(...), ctx: dict = Depends(guard), con=Depends(get_con)):
    u = con.execute("SELECT password_hash FROM user_account WHERE user_id=?", (ctx["user_id"],)).fetchone()
    error = None
    if not auth.verify_password(old, u["password_hash"]):
        error = "지금 비밀번호가 맞지 않습니다."
    elif new != new2:
        error = "새 비밀번호 두 칸이 서로 다릅니다."
    elif len(new) < 8 or new == old:
        error = "새 비밀번호는 8자 이상이고 지금 것과 달라야 합니다."
    if error:
        return page(request, ctx, "me.html", message=None, error=error)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0 WHERE user_id=?", (auth.hash_password(new), ctx["user_id"]))
    auth.log(con, ctx["login_id"], "password_change")
    return page(request, {**ctx, "must_change_password": False}, "me.html", message="비밀번호를 바꿨습니다.", error=None)
