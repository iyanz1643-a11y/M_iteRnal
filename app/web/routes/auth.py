"""로그인과 나가기. 메뉴 밖의 주소라 guard 대신 로그인 자체를 검사한다."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.web import auth
from app.web.deps import get_con, renew_csrf, templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, login_id: str = Form(...), password: str = Form(...), con=Depends(get_con)):
    user, error = auth.login(con, login_id, password)
    if not user:
        return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=401)
    request.session.clear()
    request.session["uid"] = user["user_id"]
    renew_csrf(request)                                   # 로그인 전 토큰은 버리고 새로 만든다
    return RedirectResponse("/me" if user["must_change_password"] else "/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
