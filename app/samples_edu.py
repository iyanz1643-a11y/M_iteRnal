"""교육 샘플: 과정 4개, 차수, 수료 기록. run_demo.py 가 부른다.

실제 과정 목록과 수료자 명부 양식은 아직 없다(D-09). 과정 이름, 기한, 차수는 모두 샘플이다.
ABC 과정만 명함 사이트 EDU_COURSES 의 과정 코드 ABC 와 이름을 맞췄다. 유형을 상품으로 둔 것은 가정이다.
수료는 명부 적재기가 아직 없어 source='sample' 로 넣고 배치는 비운다. 사람은 masters.seed 의 가짜 설계사만 쓴다.
다른 샘플의 숫자가 바뀌지 않게 난수를 쓰지 않고 설계사 순번으로 정한다.
"""
from app.engine import education as edu

BY = "교육담당(데모)"
REF_DATE = "2026-10-31"          # 샘플의 기준일. 샘플 실적의 마지막 달(202610) 말일이다

COURSES = [
    dict(code="NB-01", name="신인 기본과정", kind="newbie", required=True, target_role="fc", target_hired_from="2025-01-01",
         deadline_rule="hire_days", deadline_days=90, note="샘플. 2025년 이후 위촉한 설계사가 위촉 뒤 90일 안에 듣는다"),
    dict(code="CP-2026", name="2026 준법 교육", kind="compliance", required=True, target_role="all",
         deadline_rule="yearly", deadline_md="11-06", note="샘플. 해마다 다시 듣는다"),
    dict(code="RF-MGR", name="관리자 코칭 과정", kind="refresher", required=True, target_role="manager",
         deadline_rule="fixed", deadline_date="2026-12-31", note="샘플"),
    dict(code="ABC", name="ABC 과정", kind="product", required=False, target_role="fc", card_course_code="ABC",
         note="샘플. 명함 수료 코드 과정 ABC 와 잇는다. 유형은 가정"),
]
SESSIONS = {
    "NB-01": [("2025-03-15", "2025-03-15", False, "서울본부 교육장", 30, "held"),
              ("2025-07-12", "2025-07-12", False, "부산본부 교육장", 30, "held"),
              ("2025-11-08", "2025-11-08", True, None, None, "held")],
    "CP-2026": [("2026-06-20", "2026-06-20", True, None, None, "held"),
                ("2026-09-19", "2026-09-19", False, "서울본부 교육장", 40, "held")],
    "RF-MGR": [("2026-08-22", "2026-08-22", False, "서울본부 교육장", 12, "held")],
    "ABC": [("2026-10-17", "2026-10-17", True, None, None, "held"),
            ("2026-11-21", "2026-11-21", False, "부산본부 교육장", 20, "planned")],
}
ABC_DONE = (0, 2, 5, 7, 11, 14, 18, 21)        # ABC 과정을 마친 설계사의 순번


def seed(con) -> dict:
    """샘플 과정, 차수, 수료를 넣고 건수를 돌려준다."""
    edu.ensure(con)
    cid, sid = {}, {}
    for c in COURSES:
        cid[c["code"]] = edu.save_course(con, by=BY, **c)
        for no, (f, t, online, place, cap, status) in enumerate(SESSIONS[c["code"]], start=1):
            sid[(c["code"], no)] = edu.save_session(con, by=BY, course_id=cid[c["code"]], session_no=no, date_from=f, date_to=t,
                                                    online=online, place=place, capacity=cap, status=status)
    fcs = [dict(r) for r in con.execute("SELECT agent_id, hire_date FROM agent WHERE job_type='fc' ORDER BY internal_code")]
    mgrs = [dict(r) for r in con.execute("SELECT agent_id FROM agent WHERE job_type='manager' ORDER BY internal_code")]
    n = 0

    def put(agent_id, code, no):
        nonlocal n
        r = edu.record_completion(con, agent_id=agent_id, course_id=cid[code], session_id=sid[(code, no)], source="sample",
                                  by=BY, agent_ids=None, commit=False)
        n += r["created"]

    nb_dates = [s[0] for s in SESSIONS["NB-01"]]
    for i, a in enumerate(fcs):
        if a["hire_date"] and a["hire_date"] >= "2025-01-01" and i % 3 != 0:
            no = next((k for k, d in enumerate(nb_dates, start=1) if d >= a["hire_date"]), None)
            if no:
                put(a["agent_id"], "NB-01", no)
        if i % 4 != 1:
            put(a["agent_id"], "CP-2026", 1 if i % 2 == 0 else 2)
        if i in ABC_DONE:
            put(a["agent_id"], "ABC", 1)
    for j, m in enumerate(mgrs):
        if j % 3 != 2:
            put(m["agent_id"], "CP-2026", 1)
        if j % 2 == 0:
            put(m["agent_id"], "RF-MGR", 1)
    con.commit()
    return {"courses": len(cid), "required": sum(1 for c in COURSES if c["required"]), "sessions": len(sid), "completions": n}


def summary_lines(con, as_of=REF_DATE) -> list:
    """run_demo.py 의 교육 줄. 전체 범위의 필수 과정 진척도다."""
    everyone = [r[0] for r in con.execute("SELECT agent_id FROM agent")]
    p = edu.org_progress(con, everyone, as_of)
    t = p["total"]
    pct = lambda r: "-" if r is None else f"{r * 100:.1f}%"
    out = [f"[education] {as_of} 기준 필수 과정 수료율 {pct(t['rate'])} (대상 {t['targets']}건 중 수료 {t['done']}건) · "
           f"기한 임박 {t['soon']}건 · 기한 지남 {t['overdue']}건 · 필수 과정을 모두 마친 사람 {t['people_all_done']}/{t['people']}명"]
    for c in p["courses"]:
        out.append(f"   {c['course_code']} {c['course_name']} ({c['kind_ko']}, {c['deadline_ko']}): 대상 {c['targets']}명, 수료 {c['done']}명 "
                   f"({pct(c['rate'])}), 미수료 {c['not_done']}명, 임박 {c['soon']}명, 지남 {c['overdue']}명")
    return out
