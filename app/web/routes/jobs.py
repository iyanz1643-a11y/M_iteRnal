"""데이터 묶음의 뒤에서 하는 작업 /data/jobs. 메인 관리자만 쓴다.

화면은 작업을 넣고 상태만 본다. 계산과 적재는 워커 python -m app.worker 가 한다. 종류와 규칙은 app/ops/jobs.py 다.
화면에서 넣을 수 있는 것은 업적 run, 랭킹 run, 둘 다, 알림 정리, 폴더 가져오기, 구글 드라이브 가져오기다.
큰 적재는 파일을 저장소에 둔 뒤 명령줄로 넣는다. 화면에서 폴더나 파일 경로를 받지 않는다.
화면에는 작업 번호, 종류, 귀속월, 상태, 건수, 예외 종류만 보인다. 이름과 금액은 작업 표에 없다.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.ops import jobs
from app.web import auth
from app.web.deps import get_con, guard, page

router = APIRouter()
PATH = "/data/jobs"
ACTIONS = {"perf_run": "업적 run 다시 돌리기", "rank_run": "랭킹 run 다시 돌리기", "refresh": "업적과 랭킹 함께",
           "notify_sweep": "알림 정리", "connect_watch": "폴더 가져오기", "gdrive": "구글 드라이브 가져오기"}


def _months(con) -> list:
    rows = [r[0] for r in con.execute("SELECT DISTINCT period_ym FROM calc_run WHERE run_type='performance' AND status='done' ORDER BY 1 DESC")]
    return rows or [auth.current_ym(con)]


@router.get("/data/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return page(request, ctx, "data_jobs.html", rows=jobs.recent(con, 50), counts=jobs.counts(con), months=_months(con),
                ACTIONS=ACTIONS, STATUS_KO=jobs.STATUS_KO, added=request.query_params.get("added"))


@router.post("/data/jobs", response_class=HTMLResponse)
def jobs_add(request: Request, action: str = Form(...), ym: str = Form(""), ctx: dict = Depends(guard), con=Depends(get_con)):
    if action not in ACTIONS:
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    by = ctx["login_id"]
    try:
        if action == "refresh":
            ids = jobs.enqueue_refresh(con, ym, by=by) if jobs.YM.match(ym or "") else []
            if not ids:
                raise jobs.JobError("귀속월은 YYYYMM 꼴이어야 합니다.")
        else:
            ids = [jobs.enqueue(con, action, {"ym": ym} if action in ("perf_run", "rank_run") else {}, by=by)]
    except jobs.JobError as e:
        raise HTTPException(400, str(e)) from None
    auth.log(con, by, "job_enqueue", f"{action}:{','.join(map(str, ids))}")
    return RedirectResponse(f"{PATH}?added={len(ids)}", status_code=303)
