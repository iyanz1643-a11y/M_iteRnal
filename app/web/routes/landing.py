"""앱 소개와 초대 랜딩. 로그인 전의 설계사와 관리자가 보는 공개 화면이다. docs/10 6.1.

메뉴 밖의 주소라 guard 를 거치지 않는다. 그래서 두 가지를 지킨다.
- /welcome 은 DB 를 열지 않는다. 사람, 조직, 실적, 금액을 하나도 내지 않는 고정 안내다.
- /invite 는 초대 토큰을 가진 사람에게 그 계정의 사번과 링크 만료 시각만 보인다. 맞지 않는 토큰은 모두 같은 말로 거절한다.
주소는 tests/test_web_access.py 의 OPEN_ROUTES 와 scripts/check_context.py 의 NON_MENU_ROUTES 에 함께 적었다.

초대 링크는 /invite?t=토큰 이다. 열면 토큰을 세션에 옮기고 토큰 없는 /invite 로 넘긴다. 주소창, 방문 기록, Referer 에 토큰이 남지 않게 하려는 것이다.
요청 횟수 제한은 invite_throttle 이 app/ops/ratelimit.py 의 IP 기준 규칙을 부른다. 토큰은 256비트 무작위 값이라 맞혀 보기로는 찾을 수 없다.
화면 문구는 7나이츠 심사 전이라 게시 전 확인이 필요하다. docs/10 6.1 에 적었다.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.ops import ratelimit
from app.web import invite as iv
from app.web.deps import get_con, templates

router = APIRouter()
TOKEN_KEY = "invite_token"
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"}


def invite_throttle(request: Request) -> None:
    """초대 화면의 요청 횟수 제한. app/ops/ratelimit.py 의 landing_ip 규칙(IP 기준)이다. 넘치면 429.

    tests/test_web_s3_landing.py 가 초대 주소 셋 모두에 이 의존성이 달려 있는지 본다."""
    ratelimit.landing(request)


def _render(request, name, status_code=200, **kw):
    resp = templates.TemplateResponse(request, name, status_code=status_code, context={"TTL_DAYS": iv.TTL_DAYS, **kw})
    resp.headers.update(PUBLIC_HEADERS)
    return resp


@router.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request):
    """앱 소개, 휴대폰 홈 화면에 추가하는 법, 초대 링크 안내. DB 를 열지 않는다."""
    return _render(request, "welcome.html")


@router.get("/invite", response_class=HTMLResponse, dependencies=[Depends(invite_throttle)])
def invite_form(request: Request, t: str | None = None, con=Depends(get_con)):
    if t:
        request.session[TOKEN_KEY] = t
        return RedirectResponse("/invite", status_code=303, headers=PUBLIC_HEADERS)
    inv = iv.lookup(con, request.session.get(TOKEN_KEY) or "")
    if not inv:
        request.session.pop(TOKEN_KEY, None)
        return _render(request, "invite.html", status_code=410, bad=iv.INVALID)
    return _render(request, "invite.html", inv=inv)


@router.post("/invite", response_class=HTMLResponse, dependencies=[Depends(invite_throttle)])
def invite_set(request: Request, password: str = Form(""), password2: str = Form(""), con=Depends(get_con)):
    token = request.session.get(TOKEN_KEY) or ""
    try:
        login_id = iv.redeem(con, token, password, password2)
    except iv.InviteError as e:
        if str(e) == iv.INVALID:
            request.session.pop(TOKEN_KEY, None)
            return _render(request, "invite.html", status_code=410, bad=iv.INVALID)
        return _render(request, "invite.html", status_code=400, inv=iv.lookup(con, token), error=str(e))
    request.session.pop(TOKEN_KEY, None)
    return _render(request, "invite.html", done=login_id)
