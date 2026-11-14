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
from app.web.routes import auth, card, home, me, notify, org, promo, schedule, staff, team
from app.web.routes import edu as edu_routes
from app.web.routes import my as my_routes
from app.web.routes import perf as perf_routes

app = FastAPI(title="GA 업무 앱", docs_url=None, redoc_url=None, dependencies=[Depends(csrf_protect)])
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("APP_SECRET") or secrets.token_hex(32),
                   same_site="lax", https_only=os.environ.get("APP_HTTPS") == "1", max_age=60 * 60 * 8)


class AppStatic(StaticFiles):
    """정적 파일. 자료가 없는 파일만 두며 guard 를 거치지 않는다. 메뉴의 화면 주소가 아니라 check_context 와 시험의 예외 목록에도 없다.

    휴대폰 홈 화면 추가의 앱 정보 파일과 서비스 워커도 여기 있다. 서비스 워커는 /static/sw.js 에 두되
    Service-Worker-Allowed 머리글로 앱 전체(/)의 오프라인 안내를 맡긴다. 늘 새 판을 받도록 브라우저 캐시는 쓰지 않는다.
    서비스 워커가 무엇을 보관하는지는 static/sw.js 의 머리말과 tests/test_pwa.py 에 있다.
    """
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path == "sw.js":
            resp.headers["Service-Worker-Allowed"] = "/"
            resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", AppStatic(directory=HERE / "static"), name="static")


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(NeedPasswordChange)
async def _need_password(request: Request, exc: NeedPasswordChange):
    return RedirectResponse(PASSWORD_PATH, status_code=303)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "error.html", {"code": exc.status_code, "message": exc.detail}, status_code=exc.status_code)


for r in (auth, home, team, org, staff, me, promo, card):
    app.include_router(r.router)
for r in (notify, schedule):                 # 공통 묶음의 알림함과 일정
    app.include_router(r.router)
for r in (perf_routes, my_routes):            # 실적 묶음과 내 진척의 실적 요약, 시상 진척
    app.include_router(r.router)
app.include_router(edu_routes.router)         # 교육 묶음, 내 교육, 교육 진척도, 수료자 명부 안내


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
