"""영업 바탕의 샘플. 가짜 이름과 가짜 번호만 쓴다. 실제 고객 자료는 쓰지 않는다. run_demo.py 가 부른다.

| 무엇 | 샘플 |
| --- | --- |
| 맡은 지역 | 재직 설계사 앞에서부터 지역 한두 곳 |
| DB | 상담 신청 넷(배정 셋, 본부 풀 하나), 회사 DB 여럿. 진행 중, 방치, 체결, 종료가 섞인다 |
| 고객 | 체결한 DB 둘을 고객으로 바꾸고 그 설계사의 계약 하나씩을 잇는다 |
| 면담 | 직속 팀원이 가장 많은 조직장의 팀원 둘에게 면담 기록 |
| 가이드 | 공지와 자료실에 가이드 분류 글 셋. 첨부는 없다 |

번호는 가운데 마디가 0000 인 가짜 꼴로 만든다. 이름은 샘플 두 글자에 차례 글자를 붙인다.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.sales import coaching, core
from app.sales import customers as cs
from app.sales import leads as ld
from app.web import board as bd

SYLL = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허"
BY = "sample"


def _name(i: int) -> str:
    return "샘플" + SYLL[i % len(SYLL)] + ("" if i < len(SYLL) else SYLL[(i // len(SYLL)) % len(SYLL)])


def _phone(i: int) -> str:
    return "010" + "0000" + f"{i:04d}"


def _ago(con, lead_id, days: int, day: date):
    t = (day - timedelta(days=days)).isoformat() + " 10:00:00"
    con.execute("UPDATE sales_lead SET assigned_at=?, created_at=?, last_activity_at=NULL WHERE lead_id=?", (t, t, lead_id))


def seed(con, day: date | None = None) -> dict:
    """샘플을 넣는다. 이미 있으면 넣지 않는다. 넣은 건수."""
    core.ensure(con)
    bd.ensure(con)
    if con.execute("SELECT 1 FROM sales_lead WHERE created_by=? LIMIT 1", (BY,)).fetchone():
        return {"leads": 0, "customers": 0, "coaching": 0, "guides": 0}
    day = day or core.today()
    agents = [r["agent_id"] for r in con.execute(
        "SELECT agent_id FROM agent WHERE status='active' AND agent_id NOT IN (SELECT agent_id FROM org_manager) ORDER BY internal_code")]
    if len(agents) < 4:
        return {"leads": 0, "customers": 0, "coaching": 0, "guides": 0}
    for i, aid in enumerate(agents[:8]):
        ld.set_regions(con, aid, [core.REGIONS[i % 4], core.REGIONS[(i + 4) % 9]], BY)
    n, leads = 0, []

    def make(region, source="company", time=None, agent=None, pool_org=None):
        nonlocal n
        n += 1
        lid = ld.create(con, name=_name(n), phone=_phone(n), region=region, source=source, by=BY, preferred_time=time, consent=True,
                        pool_org_id=pool_org)
        if agent:
            ld.assign(con, lid, agent, BY)
        leads.append(lid)
        return lid

    a0, a1, a2, a3 = agents[:4]
    # 진행 중과 활동
    for k, (agent, days, acts) in enumerate([(a0, 2, ["call"]), (a0, 6, ["call", "consult"]), (a0, 1, []), (a1, 3, ["call", "consult", "proposal"]),
                                             (a1, 12, []), (a2, 9, ["call"]), (a2, 4, ["call", "consult", "proposal", "application"]),
                                             (a3, 5, [])]):
        lid = make(core.REGIONS[k % 4], "landing" if k < 3 else "company", "weekday_pm", agent)
        _ago(con, lid, days, day)
        for j, kind in enumerate(acts):
            d = (day - timedelta(days=max(days - j - 1, 0))).isoformat()
            ld.add_activity(con, lid, agent, kind, d, {"call": "통화 연결, 상담 약속", "consult": "보장 점검 자료 받음",
                                                       "proposal": "제안서 설명", "application": "청약 진행"}[kind], BY, day)
        if acts and days >= 9:                          # 방치로 보이게 마지막 활동을 뒤로 돌린다
            con.execute("UPDATE sales_lead SET last_activity_at=? WHERE lead_id=?", ((day - timedelta(days=days)).isoformat() + " 10:00:00", lid))
    # 체결과 고객
    custs = 0
    for agent in (a0, a1):
        lid = make("서울", "referral", None, agent)
        _ago(con, lid, 20, day)
        for kind in ("call", "consult", "proposal", "application"):
            ld.add_activity(con, lid, agent, kind, (day - timedelta(days=15)).isoformat(), None, BY, day)
        cid = ld.convert(con, lid, agent, BY)
        k = con.execute("SELECT contract_id FROM contract WHERE agent_id=? AND contract_date IS NOT NULL ORDER BY contract_date LIMIT 1", (agent,)).fetchone()
        if k:
            cs.link(con, cid, agent, k["contract_id"], BY)
        cs.set_plan(con, cid, agent, BY, next_action="보장 점검 결과 설명", next_on=(day + timedelta(days=3 + custs)).isoformat(),
                    birth_md=(day + timedelta(days=10 + custs)).strftime("%m-%d"))
        custs += 1
    # 종료 하나, 미배정 셋
    lid = make("부산", "event", None, a3)
    ld.close(con, lid, a3, BY, "연락 안 됨")
    make("경기", "landing", "weekend")
    branch = con.execute("SELECT org_id FROM org_unit WHERE level='branch' ORDER BY org_id LIMIT 1").fetchone()
    make("인천", "company", None, None, branch["org_id"] if branch else None)
    make("제주", "landing", "any")
    # 면담
    head = con.execute("""SELECT m.agent_id, m.org_id FROM org_manager m
                          WHERE m.effective_to IS NULL ORDER BY (SELECT COUNT(*) FROM agent_org_history h WHERE h.org_id=m.org_id
                          AND h.effective_to IS NULL AND h.agent_id<>m.agent_id) DESC, m.id LIMIT 1""").fetchone()
    n_co = 0
    if head:
        members = [r["agent_id"] for r in con.execute(
            "SELECT agent_id FROM agent_org_history WHERE org_id=? AND effective_to IS NULL AND agent_id<>? ORDER BY agent_id LIMIT 2", (head["org_id"], head["agent_id"]))]
        for i, aid in enumerate(members):
            coaching.add(con, head["agent_id"], aid, (day - timedelta(days=20 + i * 5)).isoformat(), "주 20통 통화, 보수교육 수료",
                         (day + timedelta(days=(-2 if i else 2))).isoformat(), BY, day)
            n_co += 1
    # 가이드
    n_g = 0
    for cat, title in (("guide", "보장분석 가이드 샘플"), ("script", "TA 스크립트 샘플"), ("needs", "니즈환기 자료 샘플")):
        bd.create(con, BY, {"category": cat, "title": title, "body": "샘플 자료입니다. 실제 자료는 메인 관리자가 심사를 거쳐 올립니다.",
                            "target_org_id": None, "target_role": None, "fc_visible": None}, [])
        n_g += 1
    con.commit()
    return {"leads": len(leads), "customers": custs, "coaching": n_co, "guides": n_g}
