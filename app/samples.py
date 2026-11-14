"""샘플 원본 파일 생성기: 본부 계약현황 엑셀, 유지율 엑셀, 원수사 정산서 엑셀, 시상 공문 PDF.
고정 시드라 내용은 매번 같다. 생성된 파일은 적재기(app/loader)가 실제 파일처럼 읽는다."""
import pathlib
import random
from datetime import date, timedelta

import openpyxl
from openpyxl.styles import Font

from app.masters import SEED
from app.periods import date_ym, month_bounds, ym_range

MONTHS = ym_range("202601", "202610")
AS_OF = "202610"
PREMIUMS = [50000, 80000, 100000, 150000, 200000, 300000, 500000]
CAT_WEIGHT = {"whole_life": 3, "health": 4, "savings": 1, "annuity": 1, "property": 1.5, "auto": 1.5, "general": 1}
STATUS_KO = {"applied": "청약", "inforce": "성립", "withdrawn": "철회", "declined": "반송", "lapsed": "실효", "cancelled": "해지"}
ORG_HEADERS = ["No", "본부", "지점", "사번", "성명", "원수사", "상품명", "구분", "청약번호", "증권번호", "청약일", "계약일", "상태",
               "상태변경일", "납입주기", "납입기간", "보험료", "인정보험료", "건수", "계약자", "비고"]


# ------------------------------------------------------------------ 진실 데이터
def build_truth(con, info, rng=None):
    rng = rng or random.Random(SEED + 7)
    prods = {}
    for r in con.execute("""SELECT p.*, i.code icode, i.name iname, i.sector, c.name cname FROM product p
                            JOIN insurer i ON i.insurer_id=p.insurer_id JOIN product_category c ON c.code=p.product_category"""):
        prods.setdefault(r["icode"], []).append(dict(r))
    icodes = list(prods)
    truth, seq = [], 0
    for ym in MONTHS:
        base = date.fromisoformat(month_bounds(ym)[0])
        for ag in info["agents"]:
            br = ag.get("branch_after") if (ag.get("branch_after") and ym >= "202607") else ag["branch"]
            for _ in range(max(1, round(rng.randint(2, 6) * ag["skill"]))):
                seq += 1
                icode = rng.choices(icodes, weights=[3 if prods[c][0]["sector"] == "life" else 2 for c in icodes])[0]
                p = rng.choices(prods[icode], weights=[CAT_WEIGHT[x["product_category"]] for x in prods[icode]])[0]
                count = 0.5 if rng.random() < 0.05 else 1  # 공동모집
                pm = int(rng.choice(PREMIUMS) * count)
                app_d = base + timedelta(days=rng.randint(0, 19))
                con_d = app_d + timedelta(days=rng.randint(0, 8))
                r = rng.random()
                status, status_date = "inforce", con_d
                if ym == AS_OF and r < 0.18:
                    status, status_date = "applied", app_d
                elif ym == AS_OF and r < 0.22:
                    status, status_date = "declined", app_d
                elif r < 0.28:
                    status, status_date = "withdrawn", con_d + timedelta(days=rng.randint(5, 25))
                elif r < 0.36:
                    status, status_date = "lapsed", con_d + timedelta(days=rng.randint(60, 270))
                elif r < 0.40:
                    status, status_date = "cancelled", con_d + timedelta(days=rng.randint(30, 240))
                if status_date > date(2026, 10, 31):
                    status, status_date = "inforce", con_d
                concluded = status not in ("applied", "declined")
                truth.append(dict(
                    app_no=f"APP{ym}{seq:05d}", policy_no=f"{icode}-{ym}-{seq:05d}" if concluded else None, icode=icode, insurer=p["iname"],
                    product=p["product_name"], product_code=p["product_code"], category=p["cname"], conv=p["conversion_rate"],
                    agent=ag, branch_by_month=br, app_date=app_d, contract_date=con_d if concluded else None, status=status,
                    status_date=status_date, mode="annual" if rng.random() < 0.08 else "monthly", term=rng.choice([10, 15, 20]),
                    premium=pm, recognized=round(pm * p["conversion_rate"]), count=count,
                    holder=rng.choice("김이박최정강조윤장임") + "*" + rng.choice("수영호진민")))
    return truth


def _branch_at(ag, ym):
    return ag["branch_after"] if (ag.get("branch_after") and ym >= "202607") else ag["branch"]


def _status_asof(c, ym):
    if c["status"] in ("applied", "declined"):
        return c["status"], c["status_date"]
    if date_ym(c["status_date"].isoformat()) <= ym:
        return c["status"], c["status_date"]
    return "inforce", c["contract_date"]


# ------------------------------------------------------------------ 본부 계약현황 엑셀
def write_org_contract_files(truth, info, raw_dir):
    """본부 × 월 파일. 그 달의 신규 청약·성립 건과, 이전 달 계약 중 그 달에 철회·실효·해지된 건을 담는다."""
    out = []
    hqs = sorted({b["hq"] for b in info["branches"]})
    for ym in MONTHS:
        for hq in hqs:
            rows = []
            for c in truth:
                br = _branch_at(c["agent"], ym)
                if br["hq"] != hq:
                    continue
                app_ym = date_ym(c["app_date"].isoformat())
                changed_now = c["status"] in ("withdrawn", "lapsed", "cancelled") and date_ym(c["status_date"].isoformat()) == ym
                if app_ym == ym or (app_ym < ym and changed_now):
                    st, sd = _status_asof(c, ym)
                    rows.append((c, br, st, sd))
            extra = _injected_rows(rows, hq, ym)
            path = pathlib.Path(raw_dir) / "internal" / ym / f"{ym}_{hq}_계약현황.xlsx"
            _write_org_file(path, hq, ym, rows, extra, text_style=(hq == "부산본부"))
            out.append(dict(path=path, hq=hq, ym=ym))
    return out


def _injected_rows(rows, hq, ym):
    """품질검증을 보여주기 위해 서울본부 10월 파일에 일부러 넣는 문제 행."""
    if not (hq == "서울본부" and ym == AS_OF and len(rows) > 6):
        return []
    c0, br0, st0, sd0 = rows[2]
    d = date(2026, 10, 12)
    def row(**kw):
        base = dict(본부=hq, 지점=br0["name"], 사번=c0["agent"]["code"], 성명=c0["agent"]["name"], 원수사="샘플생명", 상품명="샘플생명 건강보험",
                    구분="건강", 청약번호="", 증권번호="", 청약일=d, 계약일=d, 상태="성립", 상태변경일=d, 납입주기="월납", 납입기간=20,
                    보험료=100000, 인정보험료=80000, 건수=1, 계약자="테*트", 비고="")
        base.update(kw)
        return base
    return [
        row(사번="A9999", 성명="미등록", 청약번호="APP20261090001", 증권번호="SAM-202610-90001", 비고="[샘플] 마스터에 없는 사번"),
        row(성명=c0["agent"]["name"] + "A", 청약번호="APP20261090002", 증권번호="SAM-202610-90002", 비고="[샘플] 성명 불일치"),
        row(상품명="샘플생명 신상품보험", 청약번호="APP20261090003", 증권번호="SAM-202610-90003", 비고="[샘플] 마스터에 없는 상품"),
        row(인정보험료=100000, 청약번호="APP20261090004", 증권번호="SAM-202610-90004", 비고="[샘플] 인정보험료가 인정률과 다름"),
        row(청약번호=rows[4][0]["app_no"], 증권번호=(rows[4][0]["policy_no"] or ""), 원수사=rows[4][0]["insurer"], 상품명=rows[4][0]["product"],
            구분=rows[4][0]["category"], 사번=rows[4][0]["agent"]["code"], 성명=rows[4][0]["agent"]["name"], 상태=STATUS_KO[rows[4][2]],
            청약일=rows[4][0]["app_date"], 계약일=rows[4][0]["contract_date"], 상태변경일=rows[4][3],
            보험료=rows[4][0]["premium"], 인정보험료=rows[4][0]["recognized"], 건수=rows[4][0]["count"], 비고="[샘플] 청약번호 중복"),
    ]


def _write_org_file(path, hq, ym, rows, extra, text_style=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "계약현황"
    ws["A1"] = f"{ym[:4]}년 {int(ym[4:])}월 {hq} 계약 현황"
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(ORG_HEADERS)
    fmt_d = (lambda d: d.strftime("%Y.%m.%d") if d else "") if text_style else (lambda d: d if d else "")
    fmt_m = (lambda v: f"{v:,}") if text_style else (lambda v: v)
    n, tot_p, tot_r, tot_c = 0, 0, 0, 0
    for c, br, st, sd in rows:
        n += 1
        ws.append([n, hq, br["name"], c["agent"]["code"], c["agent"]["name"], c["insurer"], c["product"], c["category"], c["app_no"],
                   c["policy_no"] or "", fmt_d(c["app_date"]), fmt_d(c["contract_date"]), STATUS_KO[st], fmt_d(sd),
                   "연납" if c["mode"] == "annual" else "월납", c["term"], fmt_m(c["premium"]), fmt_m(c["recognized"]), c["count"], c["holder"], ""])
        tot_p, tot_r, tot_c = tot_p + c["premium"], tot_r + c["recognized"], tot_c + c["count"]
    for e in extra:
        n += 1
        ws.append([n] + [fmt_d(e[h]) if h in ("청약일", "계약일", "상태변경일") else e[h] for h in ORG_HEADERS[1:]])
        tot_p, tot_r, tot_c = tot_p + e["보험료"], tot_r + e["인정보험료"], tot_c + e["건수"]
    ws.append(["", "", "", "합계", "", "", "", "", "", "", "", "", "", "", "", "", fmt_m(tot_p), fmt_m(tot_r), tot_c, "", ""])
    wb.save(path)


# ------------------------------------------------------------------ 유지율 엑셀
def write_persistency_files(con, info, raw_dir, rng=None):
    rng = rng or random.Random(SEED + 11)
    insurers = [r["name"] for r in con.execute("SELECT name FROM insurer ORDER BY insurer_id")]
    base = {ag["code"]: rng.uniform(0.80, 0.97) for ag in info["agents"]}
    out = []
    for ym in MONTHS:
        path = pathlib.Path(raw_dir) / "internal" / ym / f"{ym}_유지율현황.xlsx"
        path.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "유지율"
        ws["A1"] = f"{ym[:4]}년 {int(ym[4:])}월 원수사별 유지율 현황"
        ws.append(["사번", "성명", "원수사", "13회차 대상", "13회차 유지", "25회차 대상", "25회차 유지"])
        for ag in info["agents"]:
            for ins in insurers:
                r13 = min(0.99, max(0.6, base[ag["code"]] + rng.uniform(-0.05, 0.05)))
                d13, d25 = rng.randint(6, 14), rng.randint(5, 12)
                ws.append([ag["code"], ag["name"], ins, d13, round(d13 * r13), d25, round(d25 * max(0.5, r13 - rng.uniform(0.04, 0.12)))])
        wb.save(path)
        out.append(dict(path=path, ym=ym))
    return out


# ------------------------------------------------------------------ 원수사 정산서 엑셀
def write_statement_files(con, period_ym, raw_dir, rng=None):
    """내부 계산과 같은 규칙으로 정산서를 만들되, 대사 화면을 보여주기 위해 일부러 차이를 넣는다. 원수사마다 양식이 다르다."""
    from app.engine.commission import compute
    rng = rng or random.Random(SEED + 1)
    lines = compute(con, period_ym)
    codes = {(r["agent_id"], r["insurer_id"]): r["insurer_agent_code"] for r in con.execute("SELECT * FROM agent_insurer_code")}
    pay_date = date.fromisoformat(month_bounds(period_ym)[1]) + timedelta(days=9)
    out, injected = [], []
    for ins in con.execute("SELECT * FROM insurer ORDER BY insurer_id").fetchall():
        iid, icode = ins["insurer_id"], ins["code"]
        rows = [dict(l) for l in lines if l["insurer_id"] == iid]
        rng.shuffle(rows)
        recs, hits = [], 0
        drop = {r["contract_id"] for r in [x for x in rows if x["commission_type"] == "renewal"][:2]} if icode == "HAN" else set()
        for i, r in enumerate(rows):
            if r["contract_id"] in drop and r["commission_type"] == "renewal":
                injected.append((icode, r["policy_no"], "internal_only (원수사 미지급)"))
                continue
            amt, rate, basis = r["amount"], r["rate"], r["basis_amount"]
            if icode == "SAM" and r["commission_type"] == "initial" and r["product_code"] == "SAM-HL" and hits < 2:
                hits += 1
                rate = 4.8
                amt = round(basis * rate)
                injected.append((icode, r["policy_no"], "rate (요율 5.0 → 4.8)"))
            if icode == "DAE" and r["commission_type"] == "initial" and hits < 2 and i % 5 == 0:
                hits += 1
                basis = round(basis * 0.9)
                amt = round(basis * rate)
                injected.append((icode, r["policy_no"], "conversion (기준보험료 90%)"))
            amt = int(amt // 10 * 10) if amt >= 0 else -int((-amt) // 10 * 10)  # 10원 절사
            recs.append(dict(policy_no=r["policy_no"], agent_code=codes.get((r["agent_id"], iid)), product_code=r["product_code"],
                             ctype=r["commission_type"], inst=r["installment_no"], basis=basis, rate=rate, amount=amt))
        if icode == "DAE":
            for r in rows[:2]:
                recs.append(dict(policy_no=r["policy_no"], agent_code=codes.get((r["agent_id"], iid)), product_code=r["product_code"],
                                 ctype="promo", inst=None, basis=None, rate=None, amount=50000))
                injected.append((icode, r["policy_no"], "insurer_only (시책 단독 지급)"))
        if icode == "SAM":
            recs.append(dict(policy_no=f"SAM-{period_ym}-99999", agent_code=recs[0]["agent_code"], product_code="SAM-WL", ctype="initial",
                             inst=1, basis=100000, rate=8.0, amount=800000))
            injected.append((icode, f"SAM-{period_ym}-99999", "missing_contract (원장에 없는 계약)"))
        if icode == "MIR":
            lapsed = con.execute("""SELECT c.*, p.product_code FROM contract c JOIN product p ON p.product_id=c.product_id
                                    WHERE c.insurer_id=? AND c.status='lapsed' AND p.product_category='health' ORDER BY c.contract_id LIMIT 1""", (iid,)).fetchone()
            if lapsed:
                recs.append(dict(policy_no=lapsed["policy_no"], agent_code=codes.get((lapsed["agent_id"], iid)), product_code=lapsed["product_code"],
                                 ctype="renewal", inst=9, basis=lapsed["premium_monthly"], rate=0.3, amount=round(lapsed["premium_monthly"] * 0.3)))
                injected.append((icode, lapsed["policy_no"], "status_mismatch (내부 실효, 원수사는 계속 지급)"))
            recs[0]["agent_code"] = "M999999"  # 매핑 없는 설계사 코드 → 품질 경고
        path = pathlib.Path(raw_dir) / icode / period_ym / f"{period_ym}_{icode}_수수료정산서.xlsx"
        path.parent.mkdir(parents=True, exist_ok=True)
        {"SAM": _stmt_sam, "DAE": _stmt_dae}.get(icode, _stmt_nonlife)(path, ins, period_ym, recs, pay_date)
        out.append(dict(path=path, icode=icode, ym=period_ym))
    return out, injected


def _stmt_sam(path, ins, ym, recs, pay_date):
    ko = {"initial": "신계약", "renewal": "계속", "clawback": "환수", "promo": "시책"}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "지급명세"
    ws["A1"] = f"{ins['name']} 수수료 지급명세서"
    ws["A2"] = f"정산월: {ym[:4]}-{ym[4:]}"
    ws.append([])
    ws.append(["증권번호", "설계사코드", "상품코드", "수수료구분", "회차", "기준보험료", "지급률", "지급액", "지급일"])
    for r in recs:
        ws.append([r["policy_no"], r["agent_code"], r["product_code"], ko[r["ctype"]], r["inst"], r["basis"],
                   f"{r['rate'] * 100:g}%" if r["rate"] is not None else None, r["amount"], pay_date])
    ws.append(["합계", None, None, None, None, None, None, sum(r["amount"] for r in recs), None])
    wb.save(path)


def _stmt_dae(path, ins, ym, recs, pay_date):
    ko = {"initial": "초회", "renewal": "유지", "clawback": "환수", "promo": "프로모션"}
    money = lambda v: None if v is None else (f"({-v:,})" if v < 0 else f"{v:,}")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "수수료내역"
    ws["A1"] = f"{ins['name']} {ym[:4]}년 {int(ym[4:])}월 수수료 내역"
    ws.append(["계약번호", "FC코드", "상품", "구분", "납입회차", "보험료", "수수료율", "수수료", "지급일자"])
    for r in recs:
        ws.append([f"{r['policy_no'][:3]} {r['policy_no'][3:9]} {r['policy_no'][9:]}", r["agent_code"], r["product_code"], ko[r["ctype"]], r["inst"], money(r["basis"]), r["rate"],
                   money(r["amount"]), pay_date.strftime("%Y.%m.%d")])
    ws.append(["합계", None, None, None, None, None, None, money(sum(r["amount"] for r in recs)), None])
    wb.save(path)


def _stmt_nonlife(path, ins, ym, recs, pay_date):
    ko = {"initial": "신", "renewal": "계", "clawback": "환", "promo": "시"}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["증권No", "모집인코드", "상품코드", "종류", "회차", "기준액", "율", "금액", "지급일"])
    for r in recs:
        ws.append([r["policy_no"], r["agent_code"], r["product_code"], ko[r["ctype"]], r["inst"], r["basis"], r["rate"], abs(r["amount"]),
                   int(pay_date.strftime("%Y%m%d"))])
    ws.append(["TOTAL", None, None, None, None, None, None, sum(abs(r["amount"]) for r in recs), None])
    wb.save(path)


# ------------------------------------------------------------------ 시상 공문 PDF
PDF_SPECS = [
    dict(file="2026_본사_시상제도안내.pdf", title="2026년 영업 시상 제도 안내", sender="본사 영업지원팀", period="적용 기간: 2026년 1월 ~ 2026년 12월",
         sections=[
             dict(head="[기본시상] 월간 인정보험료 시상", cats="종신, 건강", metric="인정보험료 (월)", col="지급률",
                  rows=[("30만원", "60만원", "10%"), ("60만원", "100만원", "20%"), ("100만원", "-", "30%")],
                  cond="13회차 유지율 85% 이상, 2건 이상", claw="지급 후 6개월 이내 해지·실효·철회 시 비례 환수"),
             dict(head="[추가시상] 종신 집중 추가시상", cats="종신", metric="인정보험료 (월)", col="지급액",
                  rows=[("20만원", "50만원", "5만원"), ("50만원", "-", "15만원")],
                  cond="13회차 유지율 85% 이상", claw="지급 후 6개월 이내 해지·실효·철회 시 비례 환수"),
             dict(head="[기본시상] 분기 인정보험료 시상", cats="종신, 건강", metric="인정보험료 (분기)", col="지급률",
                  rows=[("150만원", "-", "5%")], cond="13회차 유지율 85% 이상", claw=None)]),
    dict(file="202610_샘플생명_시책.pdf", title="샘플생명 2026년 10월 신계약 시책", sender="샘플생명", period="대상 월: 2026년 10월",
         sections=[
             dict(head="[기본시상] 샘플생명 10월 시책", cats="종신, 건강", metric="인정보험료 (월)", col="지급액",
                  rows=[("15만원", "30만원", "10만원"), ("30만원", "-", "30만원")], cond=None, claw="지급 후 3개월 이내 해지·실효·철회 시 비례 환수"),
             dict(head="[추가시상] 샘플생명 종신 건수 추가시상", cats="종신", metric="건수 (월)", col="지급액",
                  rows=[("2건", "3건", "5만원"), ("3건", "-", "15만원")], cond=None, claw=None)]),
    dict(file="202611_한빛손해_시책.pdf", title="한빛손해 2026년 11월 시책 안내", sender="한빛손해", period="대상 월: 2026년 11월",
         sections=[
             dict(head="[기본시상] 한빛손해 11월 건강 시책", cats="건강", metric="인정보험료 (월)", col="지급률",
                  rows=[("10만원", "30만원", "20%"), ("30만원", "-", "40%")], cond="2건 이상", claw="지급 후 3개월 이내 해지·실효·철회 시 비례 환수"),
             dict(head="[추가시상] 한빛손해 운전자 추가시상", cats="건강, 운전자", metric="건수 (월)", col="지급액",
                  rows=[("3건", "-", "10만원")], cond=None, claw=None)]),
]


def write_incentive_pdfs(raw_dir):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = "HYGothic-Medium"
    pdfmetrics.registerFont(UnicodeCIDFont(font))
    h1 = ParagraphStyle("h1", fontName=font, fontSize=16, leading=24)
    h2 = ParagraphStyle("h2", fontName=font, fontSize=12, leading=20)
    body = ParagraphStyle("body", fontName=font, fontSize=10, leading=16)
    out = []
    for spec in PDF_SPECS:
        path = pathlib.Path(raw_dir) / "pdf" / spec["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        story = [Paragraph(spec["title"], h1), Paragraph(f"발신: {spec['sender']}", body), Paragraph(spec["period"], body), Spacer(1, 10)]
        for i, s in enumerate(spec["sections"], start=1):
            story += [Paragraph(f"{i}. {s['head']}", h2), Paragraph(f"대상 구분: {s['cats']}", body), Paragraph(f"평가 지표: {s['metric']}", body), Spacer(1, 4)]
            t = Table([["구간(이상)", "구간(미만)", s["col"]]] + [list(r) for r in s["rows"]], colWidths=[110, 110, 110])
            t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), 10), ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
            story += [t, Spacer(1, 4)]
            if s["cond"]:
                story.append(Paragraph(f"지급 조건: {s['cond']}", body))
            if s["claw"]:
                story.append(Paragraph(f"환수: {s['claw']}", body))
            story.append(Spacer(1, 10))
        SimpleDocTemplate(str(path), pagesize=A4, title=spec["title"]).build(story)
        out.append(path)
    return out


# ------------------------------------------------------------------ 보험사별 시상안 엑셀 (표준 6열, 병합 셀)
PLAN_HEADERS = ["보험사", "시상대상", "분류", "기간", "달성기준", "시상내용"]
PLAN_SHEETS = [
    ("1.생보사", "1. 생보사", [
        ("샘플생명", "총괄", "판매활성화 시책", "2026년 10월", "종신보험 전 상품", "월초P * 50%(13회차 지급)"),
        ("", "", "", "", "건강보험 (간편·갱신 포함)", "월초P * 100%(13회차 지급)"),
        ("", "지사", "판매활성화 시책", "2026년 10월", "종신보험 전 상품", "익월 15% + 13회 100%"),
        ("", "FC", "현금 시책", "2026년 10월 (청약월 기준)", "건강보험", "월납보험료 * 200%(1차년)"),
        ("", "", "", "", "종신보험 7년납",
         "1. 월납보험료 * 250%(1차년)\n2. 월납보험료 * 150%(1차년)"),
        ("", "", "건강보험 가동 시책 (13회차 입금시)", "2026년 10월 01일 ~ 12일", "", ""),
        ("", "", "9~10월 연속 가동 시책", "2026년 9~10월 / 10~11월", "건강보험", ""),
        ("", "비고", "※ 2회차 보험료 미입금 시 미지급", "", "※ 13회차 미유지 시 전액 환수", ""),
        ("대한생명", "FC", "기본 시책", "2026년 10월", "보장성 전 상품", "월초P * 300%(익월) + 월초P * 200%(13회)"),
        ("", "", "업적 구간 시책", "2026년 10월", "※ 구간별 중복지급 없음", ""),
    ]),
    ("2.손보사", "2. 손보사", [
        ("한빛손해", "GA", "인보험 대표시상", "2026년 10월", "인보험 (단체 포함)\n※ 실손·펫보험 제외", "190%"),
        ("한빛손해", "FC", "인보험", "2026년 10월", "인보험", "200% (익월)"),
        ("", "", "재물보험", "2026년 10월", "장기재물", "80% (보장P 10% 미만시 부지급)"),
        ("", "", "인보험 주차 시상", "2026년 10월 01일 ~ 11일", "", "13회차 유지"),
        ("미래화재", "FC *첨부파일 반드시 참조", "인보험 활성화 시상", "2026년 10월", "인보험 (단체·실손 제외)", "200%"),
        ("", "", "3분기 클럽 시상", "2026년 08월~10월", "", "13회 유지시 지급"),
        ("", "비고", "", "", "※ 시상금 지급 후 7회차 미유지 시 전액 환수", ""),
    ]),
]


def write_incentive_plan(raw_dir, ym=AS_OF):
    """병합 셀·셀 안 줄바꿈·비표준 시트가 섞인 시상안 엑셀을 만든다."""
    from openpyxl.styles import Alignment

    path = pathlib.Path(raw_dir) / "incentive_plan" / f"{ym}_보험사별_시상안.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, banner, rows in PLAN_SHEETS:
        ws = wb.create_sheet(title)
        ws["A1"] = banner
        ws["A1"].font = Font(bold=True, size=14)
        ws["A2"] = "※ 시상 원안과 상이 할 경우 원안을 준수하여 주시기 바랍니다."
        ws.append(PLAN_HEADERS)
        first = ws.max_row + 1
        for r in rows:
            ws.append([v if v != "" else None for v in r])
        for col in (1, 2, 3, 4):                                   # 위 칸과 같은 값이면 병합한다 (빈칸 = 위와 같음)
            start = first
            for rno in range(first + 1, ws.max_row + 2):
                blank = rno <= ws.max_row and ws.cell(rno, col).value is None and ws.cell(rno, 2).value != "비고" \
                    and (col <= 2 or (ws.cell(rno, 1).value is None and ws.cell(rno, 2).value is None and ws.cell(rno, 3).value is None))
                if not blank:
                    if rno - 1 > start:
                        ws.merge_cells(start_row=start, start_column=col, end_row=rno - 1, end_column=col)
                    start = rno
        for row in ws.iter_rows(min_row=first):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="center")
    ws = wb.create_sheet("3.주차시상 비교")                          # 표준 양식이 아닌 시트: 적재하지 않고 사유를 남긴다
    ws.append(["구분", "보험사", "기간", "대상", "시상율", "구간별 실적 및 시상"])
    ws.append(["1주", "한빛", "10월 1일 ~ 5일", "인보험", "200%", "5만↑ 15만원"])
    wb.save(path)
    return path


# ------------------------------------------------------------------ 샘플 프로모션
def seed_promotions(con, ym=AS_OF, by="정산담당(데모)"):
    """샘플 시상안의 자동 해석 줄을 게시하고, 주차·연계·여행·가동 프로모션을 하나씩 만든다. 금액과 구간은 모두 임의다."""
    from app.engine import promotion as pm

    keyword = [("건강", "health"), ("종신", "whole_life"), ("재물", "property")]
    n = 0
    for ln in con.execute("""SELECT l.line_id, l.criteria_raw, l.category_raw FROM incentive_plan_line l JOIN incentive_plan_doc d ON d.plan_doc_id=l.plan_doc_id
                             WHERE d.period_ym=? AND d.status!='superseded' AND l.recipient_level='fc' AND l.line_kind='rate_promo'
                               AND l.parse_status='auto' AND l.period_from IS NOT NULL""", (ym,)).fetchall():
        text = (ln["criteria_raw"] or "") + (ln["category_raw"] or "")
        cat = next((code for word, code in keyword if word in text), None)
        pm.publish_from_line(con, ln["line_id"], by, categories=[cat] if cat else None)
        n += 1
    ins = {r["name"]: r["insurer_id"] for r in con.execute("SELECT insurer_id, name FROM insurer")}
    y, m = ym[:4], ym[4:]
    d = lambda day, month=m: f"{y}-{month}-{day:02d}"
    pm.create(con, period_ym=ym, name="인보험 4주차 주차 시상", kind="weekly", insurer_id=ins["한빛손해"], insurer_name="한빛손해", sector="nonlife",
              windows=[("4주차", d(19), d(31))], per_contract_cap=200_000, pay_timing="inst13", target_note="인보험 (단체, 실손, 펫 제외)",
              tiers=[(50_000, "fixed", 50_000, None), (100_000, "fixed", 150_000, None), (200_000, "fixed", 400_000, None), (300_000, "fixed", 700_000, None)],
              caution="13회차 유지 시 지급. 건당 20만 원까지 인정.", by=by, publish=True)
    pm.create(con, period_ym=ym, name="3·4주차 연계 시상", kind="linked", insurer_id=ins["샘플생명"], insurer_name="샘플생명", sector="life",
              windows=[("3주차", d(12), d(18)), ("4주차", d(19), d(31))], pay_timing="inst13", target_note="보장성 전 상품",
              tiers=[(50_000, "fixed", 100_000, None), (150_000, "fixed", 300_000, None), (300_000, "fixed", 700_000, None)],
              caution="두 구간을 모두 달성해야 하며 낮은 쪽 구간이 적용됩니다. 한 구간이라도 미달이면 지급하지 않습니다.", by=by, publish=True)
    pm.create(con, period_ym=ym, name="4분기 해외 여행 시상", kind="travel", insurer_id=ins["대한생명"], insurer_name="대한생명", sector="life",
              windows=[("4분기", d(1), f"{y}-12-31")], metric="converted_premium", pay_timing=None, target_note="보장성 전 상품 누적",
              tiers=[(300_000, "goods", None, "건강검진권"), (600_000, "travel", None, "오사카 3박 4일"), (1_000_000, "travel", None, "다낭 4박 5일 동반 1인")],
              caution="현금 대체 불가. 13회차 유지 계약만 합산해 최종 확정합니다.", by=by, publish=True)
    pm.create(con, period_ym=ym, name="손해보험 가동 시상", kind="activity", sector="nonlife", metric="policy_count",
              windows=[(f"{int(m)}월", d(1), d(31))], pay_timing="next_month", target_note="손해보험 전 상품, 건수 기준",
              tiers=[(1, "fixed", 30_000, None), (3, "fixed", 100_000, None), (5, "fixed", 200_000, None)],
              caution="조직 자체 시상의 예시입니다.", by=by, publish=True)
    return n + 4


# ------------------------------------------------------------------ 샘플 조직 부문 시상
def seed_org_awards(con, ym=AS_OF, by="정산담당(데모)"):
    """본부마다 인정업적 부문과 손해보험 건수 부문을, 지점 하나에 지점 부문을 만든다. 시상 내용은 임의다."""
    from app.engine import org_award as oa

    n = 0
    for hq in con.execute("SELECT org_id FROM org_unit WHERE level='hq' ORDER BY org_id").fetchall():
        oa.save(con, by=by, org_id=hq["org_id"], name="인정업적 부문", metric="converted_premium", top_n=3, effective_from=ym[:4] + "01",
                rewards=[(1, "상품권 50만 원"), (2, "상품권 30만 원"), (3, "상품권 20만 원")], note="한 달 인정보험료 합계로 순위를 매긴다.")
        oa.save(con, by=by, org_id=hq["org_id"], name="손해보험 건수 부문", metric="policy_count", sector="nonlife", top_n=3, effective_from=ym[:4] + "01",
                rewards=[(1, "상품권 30만 원"), (2, "상품권 20만 원"), (3, "상품권 10만 원")], note="손해보험 성립 건수로 순위를 매긴다.")
        n += 2
    branch = con.execute("SELECT org_id FROM org_unit WHERE level='branch' ORDER BY org_id LIMIT 1").fetchone()
    oa.save(con, by=by, org_id=branch["org_id"], name="지점 건강보험 부문", metric="premium_monthly", categories=["health"], top_n=1, min_value=100_000,
            effective_from=ym, rewards=[(1, "지점장 포상")], note="지점이 스스로 연 부문의 예시다.")
    return n + 1
