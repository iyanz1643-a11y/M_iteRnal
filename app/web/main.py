"""웹 화면의 진입점. 앱을 만들고, 세션과 CSRF 를 걸고, 메뉴 묶음별 라우터를 등록한다.

    uvicorn app.web.main:app --port 8000        # APP_DB 로 DB 파일, APP_SECRET 으로 세션 키를 바꾼다

주소는 app/web/routes/ 에, 권한 검사 guard 와 공통 도우미는 app/web/deps.py 에 있다.
요청 번호, 오류 기록, 느린 요청 기록은 app/ops/observe.py, 상태 점검 /healthz 는 app/ops/health.py 다.
미들웨어는 바깥부터 세션, 관측 순서다. 관측이 세션 안쪽에 있어야 오류 기록에 사용자 번호를 남긴다.
"""
import contextlib
import os
import pathlib
import secrets
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.ops import health, observe
from app.ops import schema as ops_schema
from app.web.deps import HERE, PASSWORD_PATH, NeedLogin, NeedPasswordChange, csrf_protect, guard, page, templates
from app.web.routes import auth, board, card, home, me, notify, org, promo, schedule, staff, team
from app.web.routes import contracts as contract_routes
from app.web.routes import data as data_routes
from app.web.routes import edu as edu_routes
from app.web.routes import jobs as job_routes
from app.web.routes import landing as landing_routes
from app.web.routes import masters as master_routes
from app.web.routes import my as my_routes
from app.web.routes import org_settings as settings_routes
from app.web.routes import perf as perf_routes
from app.web.routes import persistency as persist_routes
from app.web.routes import settle as settle_routes


def _db_path() -> str:
    return str(os.environ.get("APP_DB") or db.DEFAULT_DB)


@contextlib.asynccontextmanager
async def lifespan(_app):
    """켤 때 한 번. DB 가 이미 있으면 운영 기반 묶음(세션 버전, 작업 표, 스키마 버전)을 맞춘다. 없는 DB 는 만들지 않는다."""
    observe.setup_logging()
    p = pathlib.Path(_db_path())
    if p.is_file():
        con = db.connect(p)
        try:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_account'").fetchone():
                ops_schema.ensure(con)
        except sqlite3.DatabaseError:
            pass
        finally:
            con.close()
    yield


app = FastAPI(title="GA 업무 앱", docs_url=None, redoc_url=None, dependencies=[Depends(csrf_protect)], lifespan=lifespan)
app.add_middleware(observe.ObserveMiddleware)                  # 세션보다 먼저 더해 안쪽에 둔다
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("APP_SECRET") or secrets.token_hex(32),
                   same_site="lax", https_only=os.environ.get("APP_HTTPS") == "1", max_age=60 * 60 * 8)


class AppStatic(StaticFiles):
    """정적 파일. 자료가 없는 파일만 두며 guard 를 거치지 않는다. 메뉴의 화면 주소가 아니라 check_context 와 시험의 예외 목록에도 없다.

    휴대폰 홈 화면 추가의 앱 정보 파일과 서비스 워커도 여기 있다. 서비스 워커는 /static/sw.js 에 두되
    Service-Worker-Allowed 머리글로 앱 전체(/)의 오프라인 안내를 맡긴다. 늘 새 판을 받도록 브라우저 캐시는 쓰지 않는다.
    서비스 워커가 무엇을 보관하는지는 static/sw.js 의 머리말과 tests/test_s2_screens.py 에 있다.
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
    return templates.TemplateResponse(request, "error.html", {"code": exc.status_code, "message": exc.detail}, status_code=exc.status_code,
                                      headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def _server_error(request: Request, exc: Exception):
    """잡지 못한 오류. 기록은 observe 가 이미 남겼다. 화면에는 요청 번호만 보인다. 예외 내용은 보이지 않는다."""
    rid = observe.request_id(request.scope)
    return templates.TemplateResponse(request, "error.html", {"code": 500, "message": f"잠시 문제가 생겼습니다. 다시 시도해 주세요. 요청 번호 {rid}"},
                                      status_code=500, headers={"X-Request-ID": rid, "Cache-Control": "no-store"})


for r in (auth, home, team, org, staff, me, promo, card):
    app.include_router(r.router)
for r in (notify, schedule, board):          # 공통 묶음의 알림함, 일정, 공지와 자료실
    app.include_router(r.router)
for r in (perf_routes, contract_routes, my_routes, persist_routes):   # 실적 묶음, 계약 원장, 내 진척의 실적 요약과 시상 진척, 유지율 두 화면
    app.include_router(r.router)
app.include_router(edu_routes.router)         # 교육 묶음, 내 교육, 교육 진척도, 수료자 명부 안내
app.include_router(landing_routes.router)     # 로그인 전 공개 화면: 앱 소개 /welcome, 초대 링크 /invite. guard 예외 목록에 함께 적는다
app.include_router(job_routes.router)         # 데이터 묶음의 뒤에서 하는 작업 /data/jobs. 작업을 넣고 상태만 본다
app.include_router(data_routes.router)        # 데이터 묶음의 계약 엑셀, 시상안, 품질검증, 적재 이력. 기존 운영 도구에서 옮김
app.include_router(master_routes.router)      # 조직·설정 묶음의 마스터 /org/masters. 보험사, 상품, 구분
app.include_router(settings_routes.router)      # 조직·설정 묶음의 조직별 설정 /org/settings. 파트 H
app.include_router(settle_routes.router)        # 수수료·점검 묶음 다섯 화면과 데이터 묶음의 정산서와 유지율 /data/statement. 파트 G


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """브라우저가 페이지를 열면 스스로 부르는 주소. 자료가 없는 주소라 guard 없이 내용 없이 답한다.

    guard 를 거치지 않는 주소는 tests/test_web_access.py 의 OPEN_ROUTES 와 scripts/check_context.py 의
    NON_MENU_ROUTES 에 함께 적는다. 자료를 돌려주는 주소는 여기에 두지 않는다.
    """
    return Response(status_code=204)


@app.get("/healthz", include_in_schema=False)
def healthz():
    """상태 점검. DB 에 닿는지와 스키마 버전만 답한다. 자료, 건수, 경로는 내지 않는다. 로그인과 guard 없이 연다.

    OPEN_ROUTES 와 NON_MENU_ROUTES 에 함께 적었다. 운영의 부하 분산기와 컨테이너 상태 점검이 부른다.
    """
    out = health.check(_db_path())
    return JSONResponse(out, status_code=200 if out["status"] == health.OK else 503, headers={"Cache-Control": "no-store"})


# 반드시 마지막에 등록한다. 앞의 라우터가 잡지 못한 주소만 여기로 온다.
# 브라우저가 스스로 부르는 다른 주소도 여기서 guard 를 거친다. 로그인 전이면 로그인 화면으로 넘기고
# 로그인 폼의 CSRF 토큰은 남긴다 (deps.clear_login)
@app.get("/{rest:path}", response_class=HTMLResponse)
def planned(request: Request, ctx: dict = Depends(guard)):
    """아직 새 화면이 없는 메뉴: 무엇이 들어오는지, 어느 단계인지, 지금 어디서 쓰는지를 보여 준다."""
    return page(request, ctx, "planned.html")
