"""로그인과 나가기. 메뉴 밖의 주소라 guard 대신 로그인 자체를 검사한다.

로그인은 app/ops/ratelimit.py 의 IP 와 아이디 기준 실패 횟수 제한을 거친다. 세션 버전은 app/ops/sessions.py 다.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.ops import ratelimit, sessions
from app.web import auth
from app.web.deps import get_con, renew_csrf, templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, login_id: str = Form(...), password: str = Form(...), con=Depends(get_con)):
    wait = ratelimit.login_wait(request, login_id)        # IP 와 아이디의 실패 횟수 제한. 막힌 요청은 DB 를 보지 않는다
    if wait:
        minutes = max(1, (wait + 59) // 60)
        return templates.TemplateResponse(request, "login.html", {"error": f"시도가 많아 잠시 막았습니다. {minutes}분 뒤에 다시 시도해 주세요."},
                                          status_code=429, headers={"Retry-After": str(wait)})
    user, error = auth.login(con, login_id, password)
    if not user:
        ratelimit.login_failed(request, login_id)
        return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=401)
    request.session.clear()
    request.session["uid"] = user["user_id"]
    sessions.stamp(request, con, user["user_id"])         # 쿠키에 계정의 세션 버전을 적는다. 로그아웃하면 버전이 올라 이 쿠키는 끝난다
    renew_csrf(request)                                   # 로그인 전 토큰은 버리고 새로 만든다
    return RedirectResponse("/me" if user["must_change_password"] else "/", status_code=303)


@router.get("/logout")
def logout(request: Request, con=Depends(get_con)):
    """나가기. 쿠키가 아직 살아 있으면 계정의 세션 버전을 올려 복사해 둔 쿠키도 함께 끝낸다. 그 사람의 다른 기기도 나간다."""
    uid = request.session.get("uid")
    if uid and sessions.matches(request, con, uid):
        sessions.bump(con, uid)
        con.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
