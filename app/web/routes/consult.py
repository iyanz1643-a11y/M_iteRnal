"""보장분석 상담 신청 랜딩 /consult. 일반 고객이 보는 공개 화면이다. docs/10 6.1.

메뉴 밖의 주소라 guard 를 거치지 않는다. 그래서 아래를 지킨다.

| 무엇 | 규칙 |
| --- | --- |
| 스위치 | 환경 변수 APP_CONSULT_OPEN 이 1 일 때만 폼이 열린다. 기본은 꺼짐이고 꺼져 있으면 안내 화면만 보이고 신청을 받지 않는다 |
| 수집 항목 | 이름, 연락처, 지역, 상담 희망 시간. 수집과 이용 동의는 필수다. D-29 제안대로 둔 가정 |
| 문구 | 준법 확인 전이다. 화면과 동의 문구에 준법 확인 전 표시를 둔다. 게시 전 7나이츠 심사와 광고 심의를 받는다 |
| 남용 막기 | 모든 요청에 app/ops/ratelimit.py 의 landing_ip 규칙. 신청에는 consult_ip 규칙을 따로 센다. 꿀단지 칸 website 가 차 있으면 받은 척하고 버린다 |
| 중복 | 같은 연락처가 하루 안에 다시 오면 새 DB 를 만들지 않는다 |
| 내보내는 것 | 신청 뒤에도 배정 여부, 담당, 건수를 보이지 않는다. 접속 IP 는 저장하지 않는다 |
| 들어간 뒤 | 신청은 DB 로 들어가 지역 매칭 배정을 탄다. app/sales/leads.py 의 intake |

주소는 tests/test_web_access.py 의 OPEN_ROUTES 와 scripts/check_context.py 의 NON_MENU_ROUTES 에 함께 적었다.
CSRF 는 앱 전체의 csrf_protect 가 그대로 막는다. 로그인 전 세션에도 토큰이 있다.
"""
import os
import time as clock

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.ops import ratelimit
from app.sales import core
from app.sales import leads as ld
from app.web.deps import get_con, templates
from app.web.routes.landing import PUBLIC_HEADERS

router = APIRouter()
SWITCH = "APP_CONSULT_OPEN"
RATE_ENV, RATE_DEFAULT = "APP_RATE_CONSULT_IP", "5/3600"        # 한 IP 에서 한 시간에 신청 다섯 번
HONEYPOT = "website"


def is_open() -> bool:
    """메인 관리자가 켜는 스위치. 기본 꺼짐."""
    return os.environ.get(SWITCH, "").strip() == "1"


def throttle(request: Request) -> None:
    """모든 요청의 IP 기준 제한. 초대 화면과 같은 landing_ip 규칙이다. 넘치면 429."""
    ratelimit.landing(request)


def submit_wait(request: Request) -> int:
    """신청 횟수 제한. ratelimit 의 저장소에 consult_ip 열쇠로 센다. 막히면 기다릴 초, 아니면 0 이고 한 번 센다."""
    n, sec = ratelimit.parse(os.environ.get(RATE_ENV, ""), RATE_DEFAULT)
    if n <= 0 or sec <= 0:
        return 0
    key, store = f"consult_ip:{ratelimit.client_ip(request)}", ratelimit.LIMITER.store
    if store.count(key, sec) >= n:
        first = store.oldest(key, sec)
        return max(1, int(sec - (clock.monotonic() - first)) + 1) if first is not None else int(sec)
    store.hit(key, sec)
    return 0


def _render(request, status_code=200, **kw):
    resp = templates.TemplateResponse(request, "consult.html", status_code=status_code, context={
        "open": is_open(), "REGIONS": core.REGIONS, "TIMES": core.TIMES, "form": {}, **kw})
    resp.headers.update(PUBLIC_HEADERS)
    return resp


@router.get("/consult", response_class=HTMLResponse, dependencies=[Depends(throttle)])
def consult_form(request: Request):
    """신청 폼. 스위치가 꺼져 있으면 안내만 보인다. DB 를 열지 않는다."""
    return _render(request)


@router.post("/consult", response_class=HTMLResponse, dependencies=[Depends(throttle)])
def consult_submit(request: Request, name: str = Form(""), phone: str = Form(""), region: str = Form(""), time: str = Form(""),
                   consent: str = Form(""), website: str = Form(""), con=Depends(get_con)):
    if not is_open():
        return _render(request, status_code=403)
    keep = {"name": name[:40], "phone": phone[:20], "region": region, "time": time, "consent": consent == "1"}
    wait = submit_wait(request)
    if wait:
        resp = _render(request, status_code=429, form=keep, error="신청이 많아 잠시 막았습니다. 잠시 뒤에 다시 시도해 주세요.")
        resp.headers["Retry-After"] = str(wait)
        return resp
    if website.strip():                                  # 꿀단지 칸. 사람에게는 보이지 않는다. 받은 척하고 아무것도 남기지 않는다
        return _render(request, done=True)
    if consent != "1":
        return _render(request, status_code=400, form=keep, error="개인정보 수집과 이용에 동의해 주셔야 신청을 받을 수 있습니다.")
    if not time or time not in core.TIMES:
        return _render(request, status_code=400, form=keep, error="상담 희망 시간을 골라 주세요.")
    try:
        ld.intake(con, name=name, phone=phone, region=region, preferred_time=time, consent=True)
    except ValueError as e:
        con.rollback()
        return _render(request, status_code=400, form=keep, error=str(e))
    return _render(request, done=True)
