"""내 정보: 비밀번호 바꾸기와 관리자 본인의 명함 승인 권한 요청. D-44.

명함 승인 권한 칸은 관리자에게만 보인다. 메인 관리자는 늘 권한이 있고, 설계사는 대상이 아니다.
요청은 app/org/card_grant.py 의 request 만 부르고 언제나 로그인한 본인 이름으로 보낸다. 폼에 다른 계정을 넣어도 쓰지 않는다.
같은 사람의 대기 중인 요청은 하나라서 두 번 눌러도 새로 생기지 않는다.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.ops import sessions
from app.org import card_grant as cg
from app.web import auth
from app.web.deps import get_con, guard, page

router = APIRouter()
CARD_FLASH = "me_card_flash"


def card_state(con, ctx) -> dict | None:
    """관리자 본인의 명함 승인 권한 상태. 관리자가 아니면 None.

    state 는 on(지금 권한이 있음), pending(요청 처리 중), rejected(반려됨), none(없음) 가운데 하나다.
    반려는 가장 최근 요청이 반려이고 그 뒤 권한이 켜지지 않았을 때다."""
    if ctx["role"] != "manager":
        return None
    if ctx.get("card_approver"):
        return {"state": "on", "req": None}
    req = cg.my_request(con, ctx["login_id"])
    if req and req["status"] == "pending":
        return {"state": "pending", "req": req}
    if req and req["status"] == "rejected":
        return {"state": "rejected", "req": req}
    return {"state": "none", "req": req}


def _page(request, ctx, con, message=None, error=None, card_msg=None, card_err=None, status_code=200):
    return page(request, ctx, "me.html", status_code=status_code, message=message, error=error,
                card=card_state(con, ctx), card_msg=card_msg, card_err=card_err)


@router.get("/me", response_class=HTMLResponse)
def me(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return _page(request, ctx, con, card_msg=request.session.pop(CARD_FLASH, None))


def _card_request(request, ctx, con):
    if ctx["role"] != "manager":
        auth.log(con, ctx["login_id"], "access_denied", "/me card_request")
        raise HTTPException(403, "명함 승인 권한은 조직장이나 대리인 관리자만 요청할 수 있습니다.")
    if ctx["must_change_password"]:
        return _page(request, ctx, con, card_err="비밀번호를 먼저 바꿔 주세요. 그다음 권한을 요청할 수 있습니다.", status_code=400)
    try:
        res = cg.request(con, ctx["login_id"], by=ctx["login_id"])        # 언제나 본인 것만
    except cg.GrantError as e:
        con.rollback()
        return _page(request, ctx, con, card_err=str(e), status_code=400)
    request.session[CARD_FLASH] = ("명함 승인 권한을 요청했습니다. 메인 관리자가 승인하면 메뉴에 명함 승인이 보입니다." if res["created"]
                                   else "이미 요청이 처리 중입니다. 메인 관리자의 승인을 기다려 주세요.")
    return RedirectResponse("/me#card-request", status_code=303)


@router.post("/me", response_class=HTMLResponse)
def me_post(request: Request, action: str = Form(""), old: str = Form(""), new: str = Form(""), new2: str = Form(""),
            ctx: dict = Depends(guard), con=Depends(get_con)):
    if action == "card_request":
        return _card_request(request, ctx, con)
    if action not in ("", "password"):
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    u = con.execute("SELECT password_hash FROM user_account WHERE user_id=?", (ctx["user_id"],)).fetchone()
    error = None
    if not old or not new or not new2:
        error = "세 칸을 모두 넣어 주세요."
    elif not auth.verify_password(old, u["password_hash"]):
        error = "지금 비밀번호가 맞지 않습니다."
    elif new != new2:
        error = "새 비밀번호 두 칸이 서로 다릅니다."
    elif len(new) < 8 or new == old:
        error = "새 비밀번호는 8자 이상이고 지금 것과 달라야 합니다."
    if error:
        return _page(request, ctx, con, error=error)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0 WHERE user_id=?", (auth.hash_password(new), ctx["user_id"]))
    sessions.bump(con, ctx["user_id"])                       # 다른 기기와 복사한 쿠키는 끊고 이 기기만 새 버전으로 이어 간다
    auth.log(con, ctx["login_id"], "password_change")
    sessions.stamp(request, con, ctx["user_id"])
    return _page(request, {**ctx, "must_change_password": False}, con, message="비밀번호를 바꿨습니다.")
