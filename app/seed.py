"""임의(샘플) 데이터 생성. 고정 시드라 실행할 때마다 같은 결과가 나온다."""
import hashlib
import json
import random
from datetime import date, timedelta

from app import db
from app.periods import date_ym, month_bounds, ym_range

SEED = 42
MONTHS = ym_range("202601", "202610")
AS_OF = "202610"

INSURERS = [("SAM", "샘플생명"), ("HAN", "한빛손해"), ("DAE", "대한생명")]
CATEGORIES = {"life": 1.0, "health": 0.8, "annuity": 0.5}
INITIAL_RATES = {"SAM": {"life": 8.0, "health": 5.0, "annuity": 3.0},
                 "HAN": {"life": 7.5, "health": 4.5, "annuity": 2.5},
                 "DAE": {"life": 8.5, "health": 5.5, "annuity": 3.5}}
PREMIUMS = [50000, 80000, 100000, 150000, 200000, 300000, 500000]
SURNAMES = "김이박최정강조윤장임한오서신권황안송류전홍"
GIVEN = ["민준", "서연", "도윤", "지우", "하준", "지호", "수아", "예준", "하은", "시우", "유진", "지민", "현우", "서준", "지아", "은우", "채원", "준서", "다은", "건우"]


def _hash(s):
    return hashlib.sha256(s.encode()).hexdigest()


def seed(con, rng=None):
    rng = rng or random.Random(SEED)
    ts = db.now()

    # ---------- 보험사 / 상품 ----------
    ins_id = {}
    for code, name in INSURERS:
        cur = con.execute("INSERT INTO insurer(code, name) VALUES(?,?)", (code, name))
        ins_id[code] = cur.lastrowid
    products = []  # (product_id, insurer_code, category, code)
    for code, _ in INSURERS:
        for i, (cat, conv) in enumerate(CATEGORIES.items(), start=1):
            pcode = f"P{code[0]}{i:03d}"
            names = {"life": "종신보험", "health": "건강보험", "annuity": "연금보험"}
            cur = con.execute(
                "INSERT INTO product(insurer_id, product_code, product_name, product_category, conversion_rate, effective_from) VALUES(?,?,?,?,?,?)",
                (ins_id[code], pcode, f"{dict(INSURERS)[code]} {names[cat]}", cat, conv, "202401"))
            products.append((cur.lastrowid, code, cat, pcode, conv))

    # ---------- 조직 ----------
    hq_ids, team_ids = {}, []
    for hq in ("서울본부", "부산본부"):
        cur = con.execute("INSERT INTO org_unit(parent_id, name, level) VALUES(NULL,?,'hq')", (hq,))
        hq_ids[hq] = cur.lastrowid
        for t in (1, 2):
            cur = con.execute("INSERT INTO org_unit(parent_id, name, level) VALUES(?,?,'team')", (hq_ids[hq], f"{hq} {t}팀"))
            team_ids.append((cur.lastrowid, hq))

    # ---------- 직급 ----------
    lvl = {}
    for code, name, order in (("FC", "설계사", 1), ("SFC", "선임설계사", 2), ("SM", "세일즈매니저", 3)):
        cur = con.execute("INSERT INTO career_level(code, name, rank_order) VALUES(?,?,?)", (code, name, order))
        lvl[code] = cur.lastrowid
    for frm, to, metric, ptype, op, val in (
        ("FC", "SFC", "converted_premium", "quarter", ">=", 1500000),
        ("FC", "SFC", "persistency_13", "month", ">=", 0.85),
        ("FC", "SFC", "tenure_months", "month", ">=", 12),
        ("SFC", "SM", "converted_premium", "quarter", ">=", 2500000),
        ("SFC", "SM", "persistency_13", "month", ">=", 0.88),
        ("SFC", "SM", "tenure_months", "month", ">=", 24),
    ):
        con.execute("INSERT INTO career_promotion_criteria(from_level_id, to_level_id, metric, period_type, operator, value, effective_from) VALUES(?,?,?,?,?,?,?)",
                    (lvl[frm], lvl[to], metric, ptype, op, val, "202501"))

    # ---------- 설계사 ----------
    agents = []
    for i in range(20):
        code = f"A{i + 1:04d}"
        name = rng.choice(SURNAMES) + GIVEN[i]
        hire = date(2021 + rng.randint(0, 4), rng.randint(1, 12), rng.randint(1, 28))
        if hire > date(2025, 9, 1):
            hire = date(2025, rng.randint(1, 9), 1)
        cur = con.execute("INSERT INTO agent(internal_code, name, hire_date, status) VALUES(?,?,?,'active')", (code, name, hire.isoformat()))
        aid = cur.lastrowid
        team, hq = rng.choice(team_ids)
        con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (aid, team, "202401"))
        for icode, _ in INSURERS:
            con.execute("INSERT INTO agent_insurer_code(agent_id, insurer_id, insurer_agent_code, effective_from) VALUES(?,?,?,?)",
                        (aid, ins_id[icode], f"{icode[0]}{10000 + i * 7:06d}", "202401"))
        level = "SM" if hire.year <= 2022 and i % 5 == 0 else ("SFC" if hire.year <= 2023 else "FC")
        con.execute("INSERT INTO agent_career_history(agent_id, level_id, effective_from, reason) VALUES(?,?,?,'initial')", (aid, lvl[level], "202401"))
        agents.append(dict(agent_id=aid, code=code, name=name, hire=hire, team=team, hq=hq))
    # 조직 이동 1명 (202607)
    mover = agents[3]
    con.execute("UPDATE agent_org_history SET effective_to='202606' WHERE agent_id=?", (mover["agent_id"],))
    new_team = next(t for t, hq in team_ids if hq != mover["hq"])
    con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (mover["agent_id"], new_team, "202607"))

    # ---------- 수수료율 / 환수 규칙 ----------
    for icode, rates in INITIAL_RATES.items():
        for cat, r in rates.items():
            con.execute("INSERT INTO commission_rate_rule(insurer_id, product_category, commission_type, installment_from, installment_to, basis, rate, effective_from, priority) VALUES(?,?,?,?,?,?,?,?,100)",
                        (ins_id[icode], cat, "initial", 1, 1, "monthly_premium", r, "202401"))
            con.execute("INSERT INTO commission_rate_rule(insurer_id, product_category, commission_type, installment_from, installment_to, basis, rate, effective_from, priority) VALUES(?,?,?,?,?,?,?,?,100)",
                        (ins_id[icode], cat, "renewal", 2, 12, "monthly_premium", 0.30, "202401"))
            con.execute("INSERT INTO commission_rate_rule(insurer_id, product_category, commission_type, installment_from, installment_to, basis, rate, effective_from, priority) VALUES(?,?,?,?,?,?,?,?,100)",
                        (ins_id[icode], cat, "renewal", 13, 24, "monthly_premium", 0.15, "202401"))
            con.execute("INSERT INTO commission_rate_rule(insurer_id, product_category, commission_type, installment_from, installment_to, basis, rate, effective_from, priority) VALUES(?,?,?,?,?,?,?,?,100)",
                        (ins_id[icode], cat, "renewal", 25, None, "monthly_premium", 0.05, "202401"))
        for ef, et, cr in ((1, 6, 1.0), (7, 12, 0.5), (13, 24, 0.2)):
            con.execute("INSERT INTO clawback_rule(insurer_id, elapsed_from, elapsed_to, clawback_rate, effective_from) VALUES(?,?,?,?,?)",
                        (ins_id[icode], ef, et, cr, "202401"))
    # 상품 개별 요율 (SAM 종신 9.0, 202407~)
    sam_life = next(p for p in products if p[1] == "SAM" and p[2] == "life")
    con.execute("INSERT INTO commission_rate_rule(insurer_id, product_id, commission_type, installment_from, installment_to, basis, rate, effective_from, priority, note) VALUES(?,?,?,?,?,?,?,?,10,?)",
                (ins_id["SAM"], sam_life[0], "initial", 1, 1, "monthly_premium", 9.0, "202407", "PS001 개별 요율"))

    # ---------- 적재 배치 (내부 본부 업로드) ----------
    batch_internal = {}
    for hq, hid in hq_ids.items():
        fname = f"{AS_OF}_{hq}_contracts_v1.xlsx"
        cur = con.execute(
            "INSERT INTO import_batch(data_type, upload_org_id, period_ym, file_name, file_path, file_hash, received_via, received_at, loaded_at, loaded_by, row_count, status) VALUES('org_contract',?,?,?,?,?,'manual',?,?,?,0,'active')",
            (hid, AS_OF, fname, f"raw/internal/{AS_OF}/{fname}", _hash(fname), ts, ts, "seed"))
        batch_internal[hq] = cur.lastrowid

    # ---------- 계약 ----------
    seq = 0
    contracts = []
    for ym in MONTHS:
        d_first, _ = month_bounds(ym)
        base = date.fromisoformat(d_first)
        for ag in agents:
            for _ in range(rng.randint(2, 6)):
                seq += 1
                p = rng.choice(products)
                pid, icode, cat, pcode, conv = p
                mode = "annual" if rng.random() < 0.1 else "monthly"
                pm = rng.choice(PREMIUMS)
                premium = pm * 12 if mode == "annual" else pm
                app_d = base + timedelta(days=rng.randint(0, 19))
                con_d = app_d + timedelta(days=rng.randint(0, 8))
                app_no = f"APP{ym}{seq:05d}"
                pol_no = f"{icode}{ym}{seq:05d}"
                r = rng.random()
                status, status_date, withdrawal, policy = "inforce", con_d, None, pol_no
                contract_date = con_d
                if ym == AS_OF and r < 0.20:
                    status, status_date, policy, contract_date = "applied", app_d, None, None
                elif ym == AS_OF and r < 0.25:
                    status, status_date, policy, contract_date = "declined", app_d, None, None
                elif r < 0.31:
                    status = "withdrawn"
                    withdrawal = con_d + timedelta(days=rng.randint(5, 25))
                    status_date = withdrawal
                elif r < 0.39:
                    status = "lapsed"
                    status_date = con_d + timedelta(days=rng.randint(60, 270))
                elif r < 0.43:
                    status = "cancelled"
                    status_date = con_d + timedelta(days=rng.randint(30, 240))
                if status_date > date(2026, 10, 31):  # 아직 안 일어난 사건
                    status, status_date, withdrawal = "inforce", con_d, None
                batch = batch_internal[ag["hq"]]
                cur = con.execute(
                    """INSERT INTO contract(insurer_id, policy_no, product_id, product_code_raw, agent_id, insurer_agent_code, policyholder_name,
                       application_no, application_date, contract_date, perf_date, perf_ym, payment_mode, payment_term, premium, premium_monthly,
                       converted_premium, status, status_date, withdrawal_date, source_batch_id, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ins_id[icode], policy, pid, pcode, ag["agent_id"], None, rng.choice(SURNAMES) + "*" + rng.choice("수영호진민"),
                     app_no, app_d.isoformat(), contract_date.isoformat() if contract_date else None,
                     contract_date.isoformat() if contract_date else None, date_ym(contract_date.isoformat()) if contract_date else None,
                     mode, rng.choice([10, 15, 20]), premium, pm, round(pm * conv), status, status_date.isoformat(),
                     withdrawal.isoformat() if withdrawal else None, batch, ts))
                cid = cur.lastrowid
                con.execute("INSERT INTO contract_status_history(contract_id, status, status_date, batch_id, created_at) VALUES(?,?,?,?,?)",
                            (cid, "applied", app_d.isoformat(), batch, ts))
                if status != "applied":
                    if status != "declined":
                        con.execute("INSERT INTO contract_status_history(contract_id, status, status_date, batch_id, created_at) VALUES(?,?,?,?,?)",
                                    (cid, "inforce", con_d.isoformat(), batch, ts))
                    if status not in ("inforce",):
                        con.execute("INSERT INTO contract_status_history(contract_id, status, status_date, batch_id, created_at) VALUES(?,?,?,?,?)",
                                    (cid, status, status_date.isoformat(), batch, ts))
                # 납입 회차
                if status in ("inforce", "lapsed", "cancelled", "withdrawn"):
                    start_ym = date_ym(con_d.isoformat())
                    if status == "withdrawn":
                        end_ym = start_ym
                    elif status == "lapsed":
                        end_ym = max(start_ym, date_ym((status_date - timedelta(days=45)).isoformat()))
                    elif status == "cancelled":
                        end_ym = date_ym(status_date.isoformat())
                    else:
                        end_ym = AS_OF
                    end_ym = min(end_ym, AS_OF)
                    pay_months = [start_ym] if mode == "annual" else ym_range(start_ym, end_ym)
                    for k, pym in enumerate(pay_months, start=1):
                        con.execute("INSERT INTO premium_payment(contract_id, period_ym, installment_no, amount, paid_date, batch_id) VALUES(?,?,?,?,?,?)",
                                    (cid, pym, k, premium, month_bounds(pym)[0], batch))
                contracts.append(dict(contract_id=cid, policy=policy, icode=icode, agent=ag, status=status))
        for hq, bid in batch_internal.items():
            pass
    for hq, bid in batch_internal.items():
        n = con.execute("SELECT COUNT(*) FROM contract WHERE source_batch_id=?", (bid,)).fetchone()[0]
        con.execute("UPDATE import_batch SET row_count=? WHERE batch_id=?", (n, bid))

    # ---------- 유지율 / 외부 지표 ----------
    for ag in agents:
        base_rate = rng.uniform(0.78, 0.97)
        for ym in MONTHS:
            rate = min(0.99, max(0.6, base_rate + rng.uniform(-0.03, 0.03)))
            denom = rng.randint(15, 40)
            con.execute("INSERT INTO persistency(agent_id, period_ym, kind, numerator, denominator, rate) VALUES(?,?,?,?,?,?)",
                        (ag["agent_id"], ym, "p13", round(denom * rate), denom, round(rate, 3)))
            con.execute("INSERT INTO agent_external_metric(agent_id, period_ym, metric, value, source) VALUES(?,?,?,?,?)",
                        (ag["agent_id"], ym, "mis_selling_count", 1 if rng.random() < 0.02 else 0, "생명보험협회"))
            con.execute("INSERT INTO agent_external_metric(agent_id, period_ym, metric, value, source) VALUES(?,?,?,?,?)",
                        (ag["agent_id"], ym, "complaint_count", 1 if rng.random() < 0.03 else 0, "보험사"))

    # ---------- 시상 규칙 (PDF 공문 → 승인된 규칙) ----------
    doc_path = f"raw/internal/2026/{AS_OF}/{AS_OF}_HQ_incentive_v1.pdf"
    cur = con.execute("INSERT INTO incentive_source_doc(insurer_id, period_ym, title, file_path, file_hash, received_at, extraction_method, extraction_status, reviewed_by, reviewed_at) VALUES(NULL,?,?,?,?,?,'text','reviewed','정산담당',?)",
                      (AS_OF, "2026년 월간 환산P 시상 안내", doc_path, _hash(doc_path), ts, ts))
    doc_internal = cur.lastrowid
    cur = con.execute("INSERT INTO incentive_rule(name, insurer_id, source_doc_id, status, period_type, metric, scope, clawback_months, effective_from, effective_to, approved_by, approved_at) VALUES(?,NULL,?,'active','month','converted_premium','all',6,'202601',NULL,'정산담당',?)",
                      ("월간 환산P 시상", doc_internal, ts))
    r1 = cur.lastrowid
    for t, (mn, mx, val) in enumerate(((500000, 1000000, 0.10), (1000000, 2000000, 0.20), (2000000, None, 0.30)), start=1):
        con.execute("INSERT INTO incentive_tier(rule_id, tier_no, min_value, max_value, reward_type, reward_value) VALUES(?,?,?,?,'rate',?)", (r1, t, mn, mx, val))
    con.execute("INSERT INTO incentive_condition(rule_id, condition_type, operator, value) VALUES(?,'persistency_13','>=','0.85')", (r1,))
    con.execute("INSERT INTO incentive_condition(rule_id, condition_type, operator, value) VALUES(?,'min_count','>=','2')", (r1,))

    sam_doc = f"raw/SAM/2026/{AS_OF}/{AS_OF}_SAM_incentive_v1.pdf"
    cur = con.execute("INSERT INTO incentive_source_doc(insurer_id, period_ym, title, file_path, file_hash, received_at, extraction_method, extraction_status, reviewed_by, reviewed_at) VALUES(?,?,?,?,?,?,'ocr','reviewed','정산담당',?)",
                      (ins_id["SAM"], AS_OF, "샘플생명 10월 신계약 시책", sam_doc, _hash(sam_doc), ts, ts))
    doc_sam = cur.lastrowid
    cur = con.execute("INSERT INTO incentive_rule(name, insurer_id, source_doc_id, status, period_type, metric, scope, scope_value, clawback_months, effective_from, effective_to, approved_by, approved_at) VALUES(?,?,?,'active','month','converted_premium','insurer','SAM',3,?,?,'정산담당',?)",
                      ("샘플생명 10월 시책", ins_id["SAM"], doc_sam, AS_OF, AS_OF, ts))
    r2 = cur.lastrowid
    for t, (mn, mx, val) in enumerate(((300000, 600000, 100000), (600000, None, 300000)), start=1):
        con.execute("INSERT INTO incentive_tier(rule_id, tier_no, min_value, max_value, reward_type, reward_value) VALUES(?,?,?,?,'fixed',?)", (r2, t, mn, mx, val))
    # 분기 시상 (내부)
    cur = con.execute("INSERT INTO incentive_rule(name, insurer_id, source_doc_id, status, period_type, metric, scope, clawback_months, effective_from, effective_to, approved_by, approved_at) VALUES(?,NULL,?,'active','quarter','converted_premium','all',0,'202601',NULL,'정산담당',?)",
                      ("분기 환산P 시상", doc_internal, ts))
    r3 = cur.lastrowid
    con.execute("INSERT INTO incentive_tier(rule_id, tier_no, min_value, max_value, reward_type, reward_value) VALUES(?,1,2000000,NULL,'rate',0.05)", (r3,))
    con.execute("INSERT INTO incentive_condition(rule_id, condition_type, operator, value) VALUES(?,'persistency_13','>=','0.85')", (r3,))
    # 검토 대기 중인 11월 공문 (추출만 된 상태)
    nov = f"raw/HAN/2026/202611/202611_HAN_incentive_v1.pdf"
    con.execute("INSERT INTO incentive_source_doc(insurer_id, period_ym, title, file_path, file_hash, received_at, extraction_method, extraction_status) VALUES(?,?,?,?,?,?,'text','extracted')",
                (ins_id["HAN"], "202611", "한빛손해 11월 시책 (검토 대기)", nov, _hash(nov), ts))

    # ---------- 인증 프로그램 (기준 값은 샘플, 매년 공식 공고 값으로 교체) ----------
    cur = con.execute("INSERT INTO certification_program(code, name, issuer, basis_period) VALUES('MDRT','MDRT 회원','MDRT','calendar_year')")
    mdrt = cur.lastrowid
    con.execute("INSERT INTO certification_criteria(program_id, year, metric, operator, value, criteria_group, note) VALUES(?,2026,'annual_commission','>=',35000000,'commission','샘플 값')", (mdrt,))
    con.execute("INSERT INTO certification_criteria(program_id, year, metric, operator, value, criteria_group, note) VALUES(?,2026,'annual_first_premium','>=',60000000,'premium','샘플 값')", (mdrt,))
    cur = con.execute("INSERT INTO certification_program(code, name, issuer, basis_period) VALUES('EXCELLENT_FC_LIFE','우수인증설계사(생보)','생명보험협회','calendar_year')")
    exc = cur.lastrowid
    for metric, op, val in (("tenure_months", ">=", 36), ("persistency_13", ">=", 0.90), ("mis_selling_count", "=", 0)):
        con.execute("INSERT INTO certification_criteria(program_id, year, metric, operator, value, criteria_group, note) VALUES(?,2026,?,?,?,'all','샘플 값')", (exc, metric, op, val))

    # ---------- 목표 ----------
    for ag in agents:
        con.execute("INSERT INTO agent_goal(agent_id, period_type, period_key, metric, target_value, set_by, set_at) VALUES(?,?,?,?,?,?,?)",
                    (ag["agent_id"], "quarter", "2026Q4", "converted_premium", rng.choice([1500000, 2500000, 3500000]), "manager", ts))
        con.execute("INSERT INTO agent_goal(agent_id, period_type, period_key, metric, target_value, set_by, set_at) VALUES(?,?,?,?,?,?,?)",
                    (ag["agent_id"], "year", "2026", "net_count", rng.choice([20, 30, 40]), "self", ts))

    con.commit()
    return dict(agents=agents, contracts=contracts, ins_id=ins_id)


def seed_statements(con, period_ym=AS_OF, rng=None):
    """보험사 정산서: 내부 계산과 같은 규칙으로 만들되 일부러 차이를 넣는다."""
    from app.engine.commission import compute

    rng = rng or random.Random(SEED + 1)
    ts = db.now()
    lines = compute(con, period_ym)
    by_ins = {}
    for l in lines:
        by_ins.setdefault(l["insurer_id"], []).append(l)
    code_map = {(r["agent_id"], r["insurer_id"]): r["insurer_agent_code"] for r in con.execute("SELECT agent_id, insurer_id, insurer_agent_code FROM agent_insurer_code")}
    injected = []
    for ins in con.execute("SELECT * FROM insurer"):
        iid, icode = ins["insurer_id"], ins["code"]
        fname = f"{period_ym}_{icode}_statement_v1.xlsx"
        cur = con.execute(
            "INSERT INTO import_batch(insurer_id, data_type, period_ym, file_name, file_path, file_hash, received_via, received_at, loaded_at, loaded_by, mapping_version, status) VALUES(?,?,?,?,?,?,'portal',?,?,'seed','v2','active')",
            (iid, "statement", period_ym, fname, f"raw/{icode}/2026/{period_ym}/{fname}", _hash(fname), ts, ts))
        bid = cur.lastrowid
        rows = list(by_ins.get(iid, []))
        rng.shuffle(rows)
        out = []
        drop = set()
        if icode == "HAN":  # 내부 단독 (보험사 미지급) 2건
            drop = {r["contract_id"] for r in rows[:2] if r["commission_type"] == "initial"} or {rows[0]["contract_id"]}
        rate_hits = 0
        for i, r in enumerate(rows):
            if r["contract_id"] in drop and r["commission_type"] != "clawback":
                injected.append((icode, r["policy_no"], "internal_only"))
                continue
            amt, rate, basis = r["amount"], r["rate"], r["basis_amount"]
            if icode == "SAM" and r["commission_type"] == "initial" and r["product_code"] == "PS002" and rate_hits < 2:
                rate_hits += 1
                rate = 4.8  # 요율 차이
                amt = round(basis * rate)
                injected.append((icode, r["policy_no"], "rate"))
            if icode == "DAE" and r["commission_type"] == "initial" and i % 9 == 0:
                basis = round(basis * 0.9)  # 기준금액(환산) 차이
                amt = round(basis * rate)
                injected.append((icode, r["policy_no"], "conversion"))
            amt = int(amt // 10 * 10) if amt > 0 else int(-((-amt) // 10 * 10))  # 10원 절사
            out.append((bid, iid, period_ym, r["policy_no"], code_map.get((r["agent_id"], iid)), r["agent_id"], r["product_code"],
                        r["commission_type"], r["installment_no"], basis, rate, amt, month_bounds(period_ym)[1], f"지급명세!R{i + 4}"))
        if icode == "DAE":  # 보험사 단독 시책 지급 2건
            for r in rows[:2]:
                out.append((bid, iid, period_ym, r["policy_no"], code_map.get((r["agent_id"], iid)), r["agent_id"], r["product_code"],
                            "promo", None, None, None, 50000, month_bounds(period_ym)[1], "시책!R2"))
                injected.append((icode, r["policy_no"], "insurer_only"))
        if icode == "SAM":  # 원장에 없는 계약 1건
            out.append((bid, iid, period_ym, f"SAM{period_ym}99999", code_map.get((rows[0]["agent_id"], iid)), rows[0]["agent_id"], "PS001",
                        "initial", 1, 100000, 8.0, 800000, month_bounds(period_ym)[1], "지급명세!R999"))
            injected.append((icode, f"SAM{period_ym}99999", "missing_contract"))
        con.executemany(
            """INSERT INTO commission_statement_line(batch_id, insurer_id, period_ym, policy_no, insurer_agent_code, agent_id, product_code,
               commission_type, installment_no, basis_amount, rate, amount, pay_date, raw_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", out)
        con.execute("UPDATE import_batch SET row_count=? WHERE batch_id=?", (len(out), bid))
    con.commit()
    return injected
