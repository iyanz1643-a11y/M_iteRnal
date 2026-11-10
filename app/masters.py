"""마스터 샘플 데이터: 구분, 원수사, 상품, 조직(본부·지점·관리자), 설계사, 규칙, 인증·임원·어워드 프로그램.
계약·정산서·유지율·시상 구간은 여기서 넣지 않는다. 그것들은 samples.py 가 만든 엑셀·PDF 를 적재기로 읽어 들인다."""
import random

from app import db

SEED = 42

CATEGORIES = [  # code, 이름, 시상 대상 기본값
    ("whole_life", "종신", 1), ("health", "건강", 1), ("savings", "저축", 0), ("annuity", "연금", 0),
    ("property", "재물", 0), ("auto", "자동차", 0), ("general", "일반", 0),
]
INSURERS = [("SAM", "샘플생명", "life"), ("DAE", "대한생명", "life"), ("HAN", "한빛손해", "nonlife"), ("MIR", "미래화재", "nonlife")]
# 업권별 취급 구분: (구분 코드, 상품명 꼬리, 인정률, 초회 수수료율, 계속 2~12회 요율)
LINEUP = {
    "life": [("whole_life", "든든종신보험", 1.0, 8.0, 0.30), ("health", "건강보험", 0.8, 5.0, 0.30),
             ("savings", "저축보험", 0.3, 1.0, 0.10), ("annuity", "연금보험", 0.5, 2.5, 0.10)],
    "nonlife": [("health", "건강종합보험", 0.8, 5.0, 0.30), ("property", "재물보험", 0.5, 3.0, 0.20),
                ("auto", "자동차보험", 0.1, 0.8, 0.0), ("general", "일반보험", 0.2, 1.0, 0.0)],
}
ABBR = {"whole_life": "WL", "health": "HL", "savings": "SV", "annuity": "AN", "property": "PR", "auto": "AU", "general": "GN"}
ORGS = {"서울본부": ["강남지점", "종로지점"], "부산본부": ["해운대지점", "서면지점"]}
SURNAMES = "김이박최정강조윤장임한오서신권황안송류전홍"
GIVEN = ["민준", "서연", "도윤", "지우", "하준", "지호", "수아", "예준", "하은", "시우", "유진", "지민", "현우", "서준", "지아", "은우",
         "채원", "준서", "다은", "건우", "소율", "태윤", "나은", "승현"]
MGR_NAMES = ["박정호", "이미경", "최영수", "김현정", "정대현", "한수진"]

# 인증·임원·어워드 기준 값은 모두 샘플이다. 실제 운영 시 공식 공고·사내 규정 값으로 바꾼다.
PROGRAMS = [
    # code, name, issuer, category, target, target_role, sector, insurer_code, [(group, metric, param, op, value)]
    ("MDRT", "MDRT 회원", "MDRT", "mdrt", "agent", None, "all", None,
     [("commission", "annual_commission", None, ">=", 48_000_000), ("premium", "annual_first_premium", None, ">=", 85_000_000)]),
    ("EXCELLENT_FC_LIFE", "우수인증설계사(생명보험)", "생명보험협회", "association_cert", "agent", None, "life", None,
     [("all", "tenure_months", None, ">=", 36), ("all", "persistency_13", None, ">=", 0.90), ("all", "persistency_25", None, ">=", 0.80),
      ("all", "mis_selling_count", None, "=", 0)]),
    ("EXCELLENT_FC_NONLIFE", "우수인증설계사(손해보험)", "손해보험협회", "association_cert", "agent", None, "nonlife", None,
     [("all", "tenure_months", None, ">=", 36), ("all", "persistency_13", None, ">=", 0.90), ("all", "persistency_25", None, ">=", 0.80),
      ("all", "mis_selling_count", None, "=", 0)]),
    ("SAM_ACE", "샘플생명 ACE 클럽", "샘플생명", "insurer_cert", "agent", None, "life", "SAM",
     [("all", "annual_recognized_premium", None, ">=", 1_500_000), ("all", "persistency_13", None, ">=", 0.90)]),
    ("HAN_BLUE", "한빛손해 블루리본", "한빛손해", "insurer_cert", "agent", None, "nonlife", "HAN",
     [("all", "annual_recognized_premium", None, ">=", 800_000), ("all", "persistency_13", None, ">=", 0.88)]),
    ("EXEC_FC_DIRECTOR", "설계사 임원제도: 명예이사", "자사", "executive_fc", "agent", None, "all", None,
     [("all", "annual_recognized_premium", None, ">=", 3_500_000), ("all", "persistency_13", None, ">=", 0.88), ("all", "tenure_months", None, ">=", 36)]),
    ("EXEC_FC_VP", "설계사 임원제도: 명예상무", "자사", "executive_fc", "agent", None, "all", None,
     [("all", "annual_recognized_premium", None, ">=", 5_000_000), ("all", "persistency_13", None, ">=", 0.90), ("all", "tenure_months", None, ">=", 48)]),
    ("EXEC_MGR_BRANCH", "관리자 임원제도: 이사 지점장", "자사", "executive_manager", "manager", "branch_head", "all", None,
     [("all", "org_recognized_premium", None, ">=", 18_000_000), ("all", "org_persistency_13", None, ">=", 0.87),
      ("all", "org_headcount", None, ">=", 5), ("all", "org_active_ratio", None, ">=", 0.8)]),
    ("EXEC_MGR_HQ", "관리자 임원제도: 상무 본부장", "자사", "executive_manager", "manager", "hq_head", "all", None,
     [("all", "org_recognized_premium", None, ">=", 38_000_000), ("all", "org_persistency_13", None, ">=", 0.87), ("all", "org_headcount", None, ">=", 10)]),
    ("SALES_EXCELLENT", "영업우수설계사", "자사", "award", "agent", None, "all", None,
     [("all", "rank_recognized_premium", None, "<=", 5), ("all", "persistency_13", None, ">=", 0.85), ("all", "mis_selling_count", None, "=", 0)]),
    ("HALL_OF_FAME", "명예의전당", "자사", "award", "agent", None, "all", None,
     [("all", "award_count", "SALES_EXCELLENT", ">=", 3)]),
]


def seed(con, rng=None):
    rng = rng or random.Random(SEED)
    ts = db.now()
    for i, (code, name, elig) in enumerate(CATEGORIES):
        con.execute("INSERT INTO product_category(code, name, incentive_eligible, sort_order) VALUES(?,?,?,?)", (code, name, elig, i))

    # ---------- 원수사 / 상품 / 수수료율 / 환수율 ----------
    ins_id = {}
    for k, (code, name, sector) in enumerate(INSURERS):
        ins_id[code] = con.execute("INSERT INTO insurer(code, name, sector) VALUES(?,?,?)", (code, name, sector)).lastrowid
        bump = 0.5 * (k % 2)  # 원수사별 요율 차이
        for cat, tail, conv, init_rate, renew in LINEUP[sector]:
            pcode = f"{code}-{ABBR[cat]}"
            con.execute("INSERT INTO product(insurer_id, product_code, product_name, product_category, conversion_rate, effective_from) VALUES(?,?,?,?,?,?)",
                        (ins_id[code], pcode, f"{name} {tail}", cat, conv, "202401"))
            rules = [("initial", 1, 1, init_rate + (bump if init_rate >= 2 else 0))]
            if renew:
                rules += [("renewal", 2, 12, renew), ("renewal", 13, 24, renew / 2), ("renewal", 25, None, round(renew / 6, 3))]
            for ctype, f, t, rate in rules:
                con.execute("""INSERT INTO commission_rate_rule(insurer_id, product_category, commission_type, installment_from, installment_to, basis, rate, effective_from, priority)
                               VALUES(?,?,?,?,?,'monthly_premium',?,?,100)""", (ins_id[code], cat, ctype, f, t, rate, "202401"))
        for ef, et, cr in ((1, 6, 1.0), (7, 12, 0.5), (13, 24, 0.2)):
            con.execute("INSERT INTO clawback_rule(insurer_id, elapsed_from, elapsed_to, clawback_rate, effective_from) VALUES(?,?,?,?,?)", (ins_id[code], ef, et, cr, "202401"))
    sam_wl = con.execute("SELECT product_id FROM product WHERE product_code='SAM-WL'").fetchone()[0]
    con.execute("""INSERT INTO commission_rate_rule(insurer_id, product_id, commission_type, installment_from, installment_to, basis, rate, effective_from, priority, note)
                   VALUES(?,?,'initial',1,1,'monthly_premium',9.0,'202407',10,'SAM-WL 개별 요율 (상품군 요율보다 우선)')""", (ins_id["SAM"], sam_wl))

    # ---------- 직급 ----------
    lvl = {}
    for code, name, order in (("FC", "설계사", 1), ("SFC", "선임설계사", 2), ("SM", "세일즈매니저", 3)):
        lvl[code] = con.execute("INSERT INTO career_level(code, name, rank_order) VALUES(?,?,?)", (code, name, order)).lastrowid
    for frm, to, metric, ptype, op, val in (
        ("FC", "SFC", "converted_premium", "quarter", ">=", 1_200_000), ("FC", "SFC", "persistency_13", "month", ">=", 0.85),
        ("FC", "SFC", "tenure_months", "month", ">=", 12),
        ("SFC", "SM", "converted_premium", "quarter", ">=", 1_800_000), ("SFC", "SM", "persistency_13", "month", ">=", 0.88),
        ("SFC", "SM", "tenure_months", "month", ">=", 24)):
        con.execute("INSERT INTO career_promotion_criteria(from_level_id, to_level_id, metric, period_type, operator, value, effective_from) VALUES(?,?,?,?,?,?,?)",
                    (lvl[frm], lvl[to], metric, ptype, op, val, "202501"))

    # ---------- 조직: 본부 → 지점, 관리자(본부장·지점장) ----------
    branches, mgr_no = [], 0
    for hq, names in ORGS.items():
        hq_id = con.execute("INSERT INTO org_unit(parent_id, name, level) VALUES(NULL,?,'hq')", (hq,)).lastrowid
        units = [(hq_id, "hq_head")]
        for b in names:
            b_id = con.execute("INSERT INTO org_unit(parent_id, name, level) VALUES(?,?,'branch')", (hq_id, b)).lastrowid
            branches.append(dict(org_id=b_id, name=b, hq=hq, hq_id=hq_id))
            units.append((b_id, "branch_head"))
        for org_id, role in units:
            mgr_no += 1
            mid = con.execute("INSERT INTO agent(internal_code, name, hire_date, status, job_type) VALUES(?,?,?,'active','manager')",
                              (f"M{mgr_no:04d}", MGR_NAMES[mgr_no - 1], f"{2012 + mgr_no}-03-01")).lastrowid
            con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,'202401')", (mid, org_id))
            con.execute("INSERT INTO org_manager(org_id, agent_id, role, effective_from) VALUES(?,?,?,'202401')", (org_id, mid, role))

    # ---------- 설계사 ----------
    agents = []
    for i in range(24):
        code = f"A{i + 1:04d}"
        name = rng.choice(SURNAMES) + GIVEN[i]
        hire = f"{2020 + rng.randint(0, 5)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        if hire > "2025-09-01":
            hire = f"2025-{rng.randint(1, 9):02d}-01"
        aid = con.execute("INSERT INTO agent(internal_code, name, hire_date, status, job_type) VALUES(?,?,?,'active','fc')", (code, name, hire)).lastrowid
        br = branches[i % len(branches)]
        con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,'202401')", (aid, br["org_id"]))
        for icode, _, _ in INSURERS:
            con.execute("INSERT INTO agent_insurer_code(agent_id, insurer_id, insurer_agent_code, effective_from) VALUES(?,?,?,'202401')",
                        (aid, ins_id[icode], f"{icode[0]}{10000 + i * 7:06d}"))
        level = "SM" if hire < "2022-01-01" and i % 4 == 0 else ("SFC" if hire < "2024-01-01" else "FC")
        con.execute("INSERT INTO agent_career_history(agent_id, level_id, effective_from, reason) VALUES(?,?,'202401','initial')", (aid, lvl[level]))
        agents.append(dict(agent_id=aid, code=code, name=name, hire=hire, branch=br, skill=rng.uniform(0.6, 1.6)))
    # 조직 이동 1명 (202607 에 다른 본부 지점으로)
    mover = agents[3]
    new_br = next(b for b in branches if b["hq"] != mover["branch"]["hq"])
    con.execute("UPDATE agent_org_history SET effective_to='202606' WHERE agent_id=?", (mover["agent_id"],))
    con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,'202607')", (mover["agent_id"], new_br["org_id"]))
    mover["branch_after"] = new_br

    # ---------- 인증 · 임원제도 · 어워드 프로그램 ----------
    prog_id = {}
    for order, (code, name, issuer, cat, target, role, sector, icode, crits) in enumerate(PROGRAMS):
        prog_id[code] = con.execute(
            "INSERT INTO certification_program(code, name, issuer, category, target, target_role, sector, insurer_id, sort_order) VALUES(?,?,?,?,?,?,?,?,?)",
            (code, name, issuer, cat, target, role, sector, ins_id.get(icode), order)).lastrowid
        for group, metric, param, op, val in crits:
            con.execute("INSERT INTO certification_criteria(program_id, year, metric, metric_param, operator, value, criteria_group, note) VALUES(?,2026,?,?,?,?,?,'샘플 값')",
                        (prog_id[code], metric, param, op, val, group))
    # 과거 수상 이력 (명예의전당 누적 판정용)
    for idx, years in ((0, (2023, 2024, 2025)), (6, (2024, 2025)), (10, (2023, 2025)), (15, (2025,))):
        for y in years:
            con.execute("INSERT INTO agent_certification_award(agent_id, program_id, year, awarded_at, certificate_no) VALUES(?,?,?,?,?)",
                        (agents[idx]["agent_id"], prog_id["SALES_EXCELLENT"], y, f"{y + 1}-01-15", f"SE-{y}-{idx + 1:03d}"))
    con.execute("INSERT INTO agent_certification_award(agent_id, program_id, year, awarded_at, certificate_no) VALUES(?,?,2025,'2026-02-01','MDRT-2025-001')",
                (agents[0]["agent_id"], prog_id["MDRT"]))

    # ---------- 외부 지표 · 목표 ----------
    from app.periods import ym_range
    for ag in agents:
        for ym in ym_range("202601", "202610"):
            con.execute("INSERT INTO agent_external_metric(agent_id, period_ym, metric, value, source) VALUES(?,?,'mis_selling_count',?,'협회')",
                        (ag["agent_id"], ym, 1 if rng.random() < 0.015 else 0))
            con.execute("INSERT INTO agent_external_metric(agent_id, period_ym, metric, value, source) VALUES(?,?,'complaint_count',?,'원수사')",
                        (ag["agent_id"], ym, 1 if rng.random() < 0.03 else 0))
        con.execute("INSERT INTO agent_goal(agent_id, period_type, period_key, metric, target_value, set_by, set_at) VALUES(?,'quarter','2026Q4','converted_premium',?,'manager',?)",
                    (ag["agent_id"], rng.choice([800_000, 1_200_000, 1_800_000]), ts))
        con.execute("INSERT INTO agent_goal(agent_id, period_type, period_key, metric, target_value, set_by, set_at) VALUES(?,'year','2026','net_count',?,'self',?)",
                    (ag["agent_id"], rng.choice([25, 35, 45]), ts))
    con.commit()
    return dict(agents=agents, branches=branches, ins_id=ins_id)
