"""화면이 쓰는 조직별 설정 값. 규칙은 app/org/settings.py 의 effective 한 곳이고, 이 파일은 사람에 맞는 조직을 골라 부르기만 한다.

- 사람마다 소속 조직의 값을 쓴다. 보는 사람은 settings.person_org(ctx), 목록의 설계사는 자료 기준 달의 소속 이력이다.
  소속 이력이 없는 설계사는 GA 의 값을 쓴다.
- 달은 알림과 같은 settings.now_ym 이다. 이번 달부터 바꾼 값이 바로 듣는다.
- 설정 표가 없거나 읽지 못하면 항목의 기본값을 쓴다. 화면은 설정 때문에 멈추지 않는다.

화면에 연결한 항목
| 항목 | 쓰는 곳 |
| --- | --- |
| notify.promo_due_days | 알림함의 마감 임박 (routes/notify.py) |
| promo.near_pct | 달성 현황과 알림함의 구간 근접 (routes/promo.py near) |
| award.default_top_n | 조직 부문 시상 새 부문 폼의 선발 인원 (routes/promo.py) |
| home.cards | 홈에 보일 카드 (routes/home.py, home.html) |
| notify.edu_due_days | 교육 화면의 임박 문구 (routes/edu.py, team.py). 판정은 엔진이 하고 화면은 결과의 soon_days 를 모은다. 결과가 없을 때만 보는 사람의 값 |
"""
from __future__ import annotations

import sqlite3

from app.org import settings as st

DUE_DAYS_KEY = "notify.promo_due_days"
NEAR_KEY = "promo.near_pct"
TOP_N_KEY = "award.default_top_n"
HOME_KEY = "home.cards"


def default(key: str):
    return st.item(key)["default"]


def _value(con, org_id, key: str, ym: str):
    if org_id is None:
        return default(key)
    try:
        return st.effective(con, org_id, key, ym)["value"]
    except (sqlite3.OperationalError, st.SettingError):
        con.rollback()
        return default(key)


def _ym(con) -> str | None:
    try:
        return st.now_ym(con)
    except sqlite3.OperationalError:
        con.rollback()
        return None


def for_ctx(con, ctx: dict, key: str):
    """보는 사람의 조직이 지금 달에 쓰는 값."""
    ym = _ym(con)
    if ym is None:
        return default(key)
    try:
        org = st.person_org(con, ctx)
    except sqlite3.OperationalError:
        con.rollback()
        org = None
    return _value(con, org, key, ym)


def for_agents(con, agent_ids, key: str, data_ym: str) -> dict:
    """{agent_id: 값}. 설계사마다 data_ym 의 소속 조직 값이다. 같은 조직은 한 번만 읽는다."""
    ids = sorted({int(a) for a in agent_ids or () if a is not None})
    if not ids:
        return {}
    ym = _ym(con)
    if ym is None:
        return {a: default(key) for a in ids}
    q = ",".join("?" * len(ids))
    home = {r[0]: r[1] for r in con.execute(
        f"""SELECT h.agent_id, h.org_id FROM agent_org_history h WHERE h.agent_id IN ({q}) AND h.effective_from<=?
            AND (h.effective_to IS NULL OR h.effective_to>=?) ORDER BY h.effective_from""", (*ids, data_ym, data_ym))}
    root = st.root_org(con)
    cache: dict = {}
    out = {}
    for a in ids:
        org = home.get(a, root)
        if org not in cache:
            cache[org] = _value(con, org, key, ym)
        out[a] = cache[org]
    return out


def ratio(pct) -> float:
    """구간 근접 비율의 퍼센트를 0과 1 사이 값으로."""
    return float(pct) / 100.0


def pct_text(pcts) -> str:
    """여러 사람의 구간 근접 비율을 한 줄로. 모두 같으면 그 값, 다르면 범위다."""
    vals = sorted({int(p) for p in pcts})
    if not vals:
        vals = [int(default(NEAR_KEY))]
    if len(vals) == 1:
        return f"{vals[0]}%"
    return f"소속 조직마다 {vals[0]}~{vals[-1]}%"


EDU_DAYS_KEY = "notify.edu_due_days"


def days_text(days, fallback=None) -> str:
    """사람마다 다를 수 있는 날짜 수를 한 줄로. 모두 같으면 'N일', 다르면 범위다. 값이 없으면 fallback 을 쓴다.
    교육 기한 임박처럼 엔진이 사람마다 낸 soon_days 를 화면 문구로 바꿀 때 쓴다."""
    vals = sorted({int(d) for d in days if d is not None})
    if not vals and fallback is not None:
        vals = [int(fallback)]
    if not vals:
        return ""
    if len(vals) == 1:
        return f"{vals[0]}일"
    return f"소속 조직마다 {vals[0]}~{vals[-1]}일"
