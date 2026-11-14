"""웹 화면의 진입점. 앱을 만들고, 세션과 CSRF 를 걸고, 메뉴 묶음별 라우터를 등록한다.

    uvicorn app.web.main:app --port 8000        # APP_DB 로 DB 파일, APP_SECRET 으로 세션 키를 바꾼다

주소는 app/web/routes/ 에, 권한 검사 guard 와 공통 도우미는 app/web/deps.py 에 있다.
"""
import os
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.web.deps import HERE, PASSWORD_PATH, NeedLogin, NeedPasswordChange, csrf_protect, guard, page, templates
from app.web.routes import auth, home, me, org, promo, staff, team

app = FastAPI(title="GA 업무 앱", docs_url=None, redoc_url=None, dependencies=[Depends(csrf_protect)])
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("APP_SECRET") or secrets.token_hex(32),
                   same_site="lax", https_only=os.environ.get("APP_HTTPS") == "1", max_age=60 * 60 * 8)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(NeedPasswordChange)
async def _need_password(request: Request, exc: NeedPasswordChange):
    return RedirectResponse(PASSWORD_PATH, status_code=303)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "error.html", {"code": exc.status_code, "message": exc.detail}, status_code=exc.status_code)


for r in (auth, home, team, org, staff, me, promo):
    app.include_router(r.router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """브라우저가 페이지를 열면 스스로 부르는 주소. 자료가 없는 주소라 guard 없이 내용 없이 답한다.

    guard 를 거치지 않는 주소는 tests/test_web_access.py 의 OPEN_ROUTES 와 scripts/check_context.py 의
    NON_MENU_ROUTES 에 함께 적는다. 자료를 돌려주는 주소는 여기에 두지 않는다.
    """
    return Response(status_code=204)


# 반드시 마지막에 등록한다. 앞의 라우터가 잡지 못한 주소만 여기로 온다.
# 브라우저가 스스로 부르는 다른 주소도 여기서 guard 를 거친다. 로그인 전이면 로그인 화면으로 넘기고
# 로그인 폼의 CSRF 토큰은 남긴다 (deps.clear_login)
@app.get("/{rest:path}", response_class=HTMLResponse)
def planned(request: Request, ctx: dict = Depends(guard)):
    """아직 새 화면이 없는 메뉴: 무엇이 들어오는지, 어느 단계인지, 지금 어디서 쓰는지를 보여 준다."""
    return page(request, ctx, "planned.html")
