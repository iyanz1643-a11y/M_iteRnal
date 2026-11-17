"""샘플 프로모션: 흥국화재 BOOM-UP, DB생명 여행시상, DB생명 이터널 Convention 추가 시책, 삼성화재 드림+, 삼성화재 10~11월 브릿지.

미리보기에서 조직별 진척, 문턱 카드, 내 달성 현황, 브릿지, 보상 선택 기록을 볼 수 있게 run_demo 가 맨 끝에 부른다.
실제 원수사 마스터(seed_real_insurers)를 넣은 뒤에 불러야 보험사 이름이 맞는다.

- 조건은 대표님이 확인한 조건표를 따른다. 금액은 그 표의 숫자만 쓴다. 브릿지 시상 금액은 비워 '원문 확인' 으로 보인다.
- 드림+ 의 단계 문턱은 대표님이 주신 포스터 값이다(11월 17일). 브릿지의 단계 문턱은 조건표에 없어 만든 시험용 구간이고 실제 구간은 원문 확인이다.
- 드림+ 는 지난 차수(7월 19일에서 9월)를 하나 더 두고 메인 관리자 이름으로 확정한다. 보상 선택 기록을 미리 보려는 것이다.
- 조직은 이터널총괄본부와 예하 본부 넷, 그 아래 지점이다. 실제 파일의 조직 칸 모양을 따른다(_eternal).
- 계약은 계약 원장에 바로 넣는 가짜 계약이다. 날짜는 2026-11-16 까지만 두어 10월 창이 끝나고 11월 창이 진행 중인 모습이 된다.
  업적 집계에도 들어가므로 이미 계산한 달의 업적과 랭킹을 다시 계산한다(_recalc). 같은 사람의 같은 숫자를 맞추려는 것이다.
- 사번은 T 로 시작한다. 기존 샘플(A, M)보다 뒤에 서서 사번 순으로 고르는 시험과 화면이 예전과 같은 사람을 먼저 고른다.
- 자료 기준 달은 기존 샘플대로 10월이다. 10월 말 기준으로 지난 진척을 본다. open_month() 로 11월을 열면 기준일이 오늘이 된다.
- Convention 의 대상 직급은 조건표에 없어 비워 둔다(전원 대상). 가정이다.
"""
import random

from app import db
from app.engine import performance, ranking
from app.engine import promotion as pm
from app.web import promo_extra

Q4 = [("10월", "2026-10-01", "2026-10-31"), ("11월", "2026-11-01", "2026-11-30"), ("12월", "2026-12-01", "2026-12-31")]
BRIDGE_WINDOWS = [("10월 19일에서 31일", "2026-10-19", "2026-10-31"), ("11월 1일에서 15일", "2026-11-01", "2026-11-15")]
NEXT_BRIDGE = [("11월 16일에서 30일", "2026-11-16", "2026-11-30"), ("12월 1일에서 15일", "2026-12-01", "2026-12-15")]
# 브릿지 시험용 단계. 첫 창은 문턱 하나(D-93), 둘째 창은 다섯 단계다. 금액은 비워 원문 확인으로 보인다
BRIDGE_GATE, BRIDGE_STEPS = 100_000, [100_000, 200_000, 300_000, 400_000, 500_000]
DREAM_WINDOWS = [("10월 19일에서 31일", "2026-10-19", "2026-10-31"), ("11월", "2026-11-01", "2026-11-30"), ("12월", "2026-12-01", "2026-12-31")]
PAST_WINDOWS = [("7월 19일에서 31일", "2026-07-19", "2026-07-31"), ("8월", "2026-08-01", "2026-08-31"), ("9월", "2026-09-01", "2026-09-30")]
# 드림+ 단계 문턱. 대표님이 주신 포스터 값이다. 창은 10월 19일에서 31일, 11월, 12월이고 단계마다 10/20/20, 20/30/30, 30/50/50, 50/100/100, 80/150/150만이다
DREAM_STEPS = {1: [100_000, 200_000, 300_000, 500_000, 800_000], 2: [200_000, 300_000, 500_000, 1_000_000, 1_500_000],
               3: [200_000, 300_000, 500_000, 1_000_000, 1_500_000]}
DB_V1_AT, DB_V2_AT = "2026-09-28 10:00:00", "2026-11-05 10:00:00"      # DB생명 여행시상 1차 원안과 2차 변경 날짜. 샘플이라 정해 둔다
DB_V1_FLOORS, DB_V2_FLOORS = (100_000, 300_000, 400_000), (100_000, 200_000, 400_000)
DB_V2_BOOST = [["2026-10-01", "2026-10-14"], ["2026-12-01", "2026-12-14"]]     # 2차 변경: 11월은 2배 인정이 없다
DB_V2_NOTE = ("11월 기준 30만에서 20만으로, 10월 미달성자 조건 1-2 신설(11월 25만, 12월 45만), 2배 인정에서 11월 제외, "
              "여행지 싱가포르와 2027년 11월 확정")
EXCLUDE = ["단체", "실손", "펫", "캣", "재물", "The 라이프업"]
CASH_TEXT = "현금 전환 조건: 환율 1,600원 초과, 차수당 30명 미만, 골드바 3.75g 85만원 초과. 적용은 보험사 원문을 따릅니다."
PRODUCTS = {
    "흥국화재": [("HK-H01", "흥국화재 건강종합보험", "health"), ("HK-A01", "흥국화재 자동차보험", "auto")],
    "DB생명": [("DBL-H01", "DB생명 건강보험", "health"), ("DBL-W01", "DB생명 종신보험", "whole_life")],
    "삼성화재": [("SF-H01", "삼성화재 건강보험", "health"), ("SF-S01", "삼성화재 실손보험", "health"), ("SF-P01", "삼성화재 재물보험", "property")],
}


def _products(con) -> dict:
    out = {}
    for ins, rows in PRODUCTS.items():
        iid = con.execute("SELECT insurer_id FROM insurer WHERE name=?", (ins,)).fetchone()
        if not iid:
            raise ValueError(f"보험사 마스터에 {ins} 가 없습니다. seed_real_insurers 를 먼저 부릅니다.")
        for code, name, cat in rows:
            row = con.execute("SELECT product_id FROM product WHERE insurer_id=? AND product_code=?", (iid[0], code)).fetchone()
            pid = row[0] if row else con.execute(
                "INSERT INTO product(insurer_id, product_code, product_name, product_category, conversion_rate, effective_from) VALUES(?,?,?,?,?,?)",
                (iid[0], code, name, cat, 1.0, "202601")).lastrowid
            out[name] = (iid[0], pid)
        out[ins] = (iid[0], None)
    con.commit()
    return out


class _Book:
    """가짜 계약을 넣는다. 증권번호와 청약번호는 'PS' 로 시작해 샘플 적재분과 겹치지 않는다."""

    def __init__(self, con, prods):
        self.con, self.prods, self.n = con, prods, 0

    def add(self, agent, day, premium, product, *, status="inforce", self_family=None, raw=None):
        self.n += 1
        iid, pid = self.prods[product]
        concluded = status not in ("applied", "declined")
        self.con.execute(
            """INSERT INTO contract(insurer_id, policy_no, product_id, product_code_raw, agent_id, application_no, application_date, contract_date,
                                    premium, premium_monthly, converted_premium, policy_count, status, status_date, payment_mode, self_family, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (iid, f"PS{self.n:05d}" if concluded else None, pid, raw, agent, f"PSA{self.n:05d}", day, day if concluded else None,
             premium, premium, premium, 1, status, day, "monthly", self_family, db.now()))


# 이터널총괄본부 샘플 조직. 실제 계약현황의 조직 칸과 같은 모양이다: 본부 칸=직할본부, 지점 칸=이터널, 센터 칸=예하 본부, 팀 칸=지점.
# 화면 이름은 config/org_labels.yaml 이 '본부', '지점' 으로 바꾼다. 지점을 어느 본부 아래에 두는지는 샘플에서 정한 것이다.
# 인원은 실제 비율을 줄여 둔다. 피닉스본부는 실제로 작아 샘플도 작다. 사람 이름은 모두 지어낸 샘플 이름이다.
ETERNAL = [("hq", "직할본부"), ("branch", "이터널")]
BONBU = [("해온본부", ["해온2지점", "해오름지점"], 6, "강다온"), ("금탑본부", ["금탑1지점", "금탑2지점", "금탑3지점"], 7, "문하람"),
         ("H본부", ["HP지점", "HC지점", "부평지점"], 9, "배시온"), ("피닉스본부", ["직할지점"], 2, "엄라온")]
FAMILY, GIVEN = "김이박최정조윤장임한오서신권황안송류홍", ["가온", "나래", "다솜", "라온", "마루", "바다", "사랑", "아라", "자람", "하늘", "누리", "보람"]


def _eternal(con, ym) -> dict:
    """이터널총괄본부와 예하 본부, 지점, 샘플 설계사와 본부장을 만든다. {본부 이름: [설계사 agent_id]}.
    본부장은 앱의 조직장 지정(app/org/heads.assign_head)으로 정하고 변경 기록에 남긴다. 계정은 seed_accounts 로 만든다."""
    from app.org import heads, sync
    from app.web import auth as web_auth

    sync.ensure_path(con, ETERNAL)
    out, n_fc = {}, 0
    rng = random.Random(1117)
    for i, (bonbu, branches, n, head_name) in enumerate(BONBU, start=1):
        bon = sync.ensure_path(con, ETERNAL + [("center", bonbu)])
        teams = [sync.ensure_path(con, ETERNAL + [("center", bonbu), ("team", t)]) for t in branches]
        head = con.execute("INSERT INTO agent(internal_code, name, hire_date, status, job_type) VALUES(?,?,?,?,?)",
                           (f"T{i:04d}", head_name, "2020-01-01", "active", "manager")).lastrowid
        con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (head, bon, "202601"))
        heads.assign_head(con, bon, head, "202601", by="정산담당(데모)")
        con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)",
                    (db.now(), "정산담당(데모)", "org_head_assign", f"org={bon} agent=T{i:04d} 202601~"))
        out[bonbu] = []
        for k in range(n):
            n_fc += 1
            name = rng.choice(FAMILY) + GIVEN[(n_fc * 5) % len(GIVEN)]
            aid = con.execute("INSERT INTO agent(internal_code, name, hire_date, status, job_type) VALUES(?,?,?,?,?)",
                              (f"T{100 + n_fc:04d}", name, "2023-03-01", "active", "fc")).lastrowid
            con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (aid, teams[k % len(teams)], "202601"))
            out[bonbu].append(aid)
    con.commit()
    web_auth.seed_accounts(con)
    return out




def _recalc(con, since="202607") -> list:
    """샘플 계약이 든 달 가운데 이미 업적을 계산한 달의 업적과 랭킹을 다시 계산한다. 홈의 내 순위(랭킹 기록)와 랭킹 화면(그때 계산)이 같아야 한다."""
    months = [r[0] for r in con.execute("""SELECT DISTINCT period_ym FROM calc_run WHERE run_type='performance' AND status='done' AND period_ym>=?
                                           ORDER BY period_ym""", (since,))]
    for ym in months:
        performance.run(con, ym, "샘플 프로모션")
        ranking.run(con, ym, "샘플 프로모션")
    return months


def open_month(con, ym="202611") -> str:
    """미리보기용. 그 달의 업적과 랭킹을 계산해 자료 기준 달로 연다. 이번 달이면 프로모션 기준일이 오늘이 된다(pm.ref_date).
    수수료와 시상은 계산하지 않는다. 기존 샘플 계약은 10월까지라 그 달 업적은 이터널 샘플 계약만 잡힌다. 시험 DB 에는 쓰지 않는다."""
    performance.run(con, ym, "샘플 미리보기")
    ranking.run(con, ym, "샘플 미리보기")
    return ym


def seed(con, ym="202610", by="정산담당(데모)") -> dict:
    """프로모션과 지난 드림+ 를 만들고 계약을 넣는다. 돌려주는 것: {열쇠: promo_id}"""
    pm.ensure(con)
    promo_extra.ensure(con)
    prods = _products(con)
    book = _Book(con, prods)
    rng = random.Random(20261117)
    g = _eternal(con, ym)
    # 두 줄로 나눠 아래에서 고른다. seoul 은 H본부 9명, 피닉스본부 2명, 해온본부 1명. busan 은 해온본부 3명, 금탑본부 7명, 해온본부 2명
    seoul = g["H본부"] + g["피닉스본부"] + g["해온본부"][5:]
    busan = g["해온본부"][:3] + g["금탑본부"] + g["해온본부"][3:5]

    # ---------------- DB생명 여행시상: 합산 100만으로 달성 12명(H본부 7, 해온본부 3, 금탑본부 2), 근접 3명, 연속만 진행 2명, 본인 계약, 종신
    db_hit = seoul[:7] + busan[:5]
    for i, a in enumerate(db_hit):
        if i % 3 == 0:                                         # 셋 가운데 하나는 1·2주차 계약이 있어 연속 조건에 2배로 든다
            book.add(a, "2026-10-03", 60_000, "DB생명 건강보험")
            book.add(a, "2026-10-20", 400_000, "DB생명 건강보험")
        else:
            book.add(a, "2026-10-20", 450_000 + rng.choice((0, 30_000)), "DB생명 건강보험")
        book.add(a, "2026-11-10", 600_000, "DB생명 건강보험")
    for a in seoul[7:9] + busan[5:6]:                          # 합산 78만. 100만까지 22만이라 구간 근접
        book.add(a, "2026-10-22", 300_000, "DB생명 건강보험")
        book.add(a, "2026-11-12", 480_000, "DB생명 건강보험")
    for a in busan[6:8]:                                       # 연속 조건만 따라가는 사람
        book.add(a, "2026-10-21", 150_000, "DB생명 건강보험")
        book.add(a, "2026-11-11", 350_000, "DB생명 건강보험")
    book.add(seoul[9], "2026-10-21", 500_000, "DB생명 건강보험", self_family=1)   # 본인 계약은 세지 않는다
    book.add(seoul[9], "2026-10-23", 30_000, "DB생명 건강보험")
    book.add(busan[8], "2026-10-24", 200_000, "DB생명 종신보험")                 # 건강보험이 아니라 세지 않는다
    book.add(busan[9], "2026-10-25", 80_000, "DB생명 건강보험")                  # 10월 10만을 놓쳤지만 '하나만' 이라 합산과 추가안이 남는다
    book.add(busan[9], "2026-11-11", 260_000, "DB생명 건강보험")                 # 추가안 11월 25만을 채움
    book.add(seoul[9], "2026-11-12", 150_000, "DB생명 건강보험")                 # 10월 미달성. 추가안 11월 25만까지 10만이 남음

    # ---------------- 흥국화재 BOOM-UP: 청약 기준, 지표는 인정보험료(대표님 지시, 11월 17일). 샘플 계약은 인정보험료를 보험료와 같게 넣는다(_Book.add)
    # 10월과 11월을 채운 사람, 작게 따라가는 사람, 10월을 놓친 사람, 11월 근접, 철회
    hk = "흥국화재 건강종합보험"
    for i, a in enumerate(seoul[:5] + busan[:3]):
        book.add(a, "2026-10-15", 100_000, hk, status="applied" if i % 2 else "inforce")
        book.add(a, "2026-11-13", 150_000, hk)
    for a in (seoul[5], busan[3]):
        book.add(a, "2026-10-16", 50_000, hk)
        book.add(a, "2026-11-14", 100_000, hk)
    for a in (seoul[6], busan[4]):
        book.add(a, "2026-10-17", 40_000, hk)                  # 10월 5만에 못 미침. 10월이 끝나면 '모두' 라 무효
    book.add(seoul[7], "2026-10-17", 60_000, hk)
    book.add(seoul[7], "2026-11-09", 80_000, hk)               # 11월 10만 근접
    book.add(busan[5], "2026-10-18", 300_000, hk, status="withdrawn")   # 철회는 세지 않는다
    book.add(busan[5], "2026-10-19", 200_000, "흥국화재 자동차보험")       # 장기 인보험이 아니라 세지 않는다

    # ---------------- 삼성화재 드림+ 와 10~11월 브릿지: 같은 계약을 센다. 단계가 섞인 사람, 근접, 제외 상품, 품보(해지), 이름 없는 계약
    # 드림+ 포스터 단계로 보면 첫 창과 11월이 1·2단계, 3·4단계, 1·1단계, 3·2단계인 사람, 11월 20만에 가까운 사람, 11월 실적이 없는 사람,
    # 첫 창 10만을 놓친 사람이 섞인다
    sf = "삼성화재 건강보험"
    plan = [(seoul[0], (160_000, 320_000)), (seoul[1], (450_000, 1_100_000)), (seoul[2], (110_000, 210_000)), (busan[0], (320_000, 420_000)),
            (busan[1], (110_000, 110_000)), (seoul[3], (100_000, 0)), (busan[6], (60_000, 80_000))]
    for a, (oct_v, nov_v) in plan:
        book.add(a, "2026-10-25", oct_v, sf)
        if nov_v:
            book.add(a, "2026-11-12", nov_v, sf)
    book.add(seoul[4], "2026-10-27", 80_000, sf)               # 첫 창 10만 근접. 10월이 끝나면 놓침
    book.add(busan[2], "2026-10-26", 300_000, "삼성화재 실손보험")   # 제외 상품
    book.add(busan[2], "2026-10-28", 300_000, sf, status="cancelled")  # 13회 이내 품보는 해지로 본다
    book.add(busan[3], "2026-10-29", 90_000, "삼성화재", raw=None)    # 상품 이름이 없어 세지 않고 따로 센다
    book.add(seoul[0], "2026-11-16", 150_000, sf)              # 다음 브릿지(11월 16일에서)의 첫 창
    book.add(busan[1], "2026-11-16", 80_000, sf)

    # ---------------- 지난 드림+ (7월 19일에서 9월). 확정해서 보상 선택 기록을 미리 본다. 1단계, 2단계, 3단계 달성자와 9월을 놓친 사람, 첫 창을 놓친 사람
    past_plan = [(seoul[0], (160_000, 320_000, 320_000)), (seoul[1], (210_000, 320_000, 520_000)), (busan[0], (110_000, 210_000, 110_000)),
                 (busan[1], (320_000, 520_000, 1_020_000)), (seoul[2], (60_000, 0, 0))]
    for a, vals in past_plan:
        for (_label, f, _t), v in zip(PAST_WINDOWS, vals):
            if v:
                book.add(a, f[:8] + ("25" if f.endswith("19") else "12"), v, sf)
    con.commit()

    ins = lambda name: prods[name][0]
    tiers = lambda label, *ms: [(m, "travel", None, label) for m in ms]
    out = {}
    out["boom"] = pm.create(
        con, period_ym=ym, name="흥국화재 BOOM-UP", kind="travel", insurer_id=ins("흥국화재"), insurer_name="흥국화재", sector="nonlife",
        categories=["health"], windows=Q4, combine_rule="both", date_basis="application_date", metric="converted_premium",
        tiers=tiers("제주 2박 3일", 400_000), window_tiers={1: tiers("제주 2박 3일", 50_000), 2: tiers("제주 2박 3일", 100_000),
                                                            3: tiers("제주 2박 3일", 150_000)},
        rules={"count_basis": "application", "retention_min": 95}, target_note="흥국화재 장기 인보험",
        caution="10월에서 12월 청약 기준, 철회 제외. 월 5만, 10만, 15만 이상 연속 달성과 3개월 합산 40만 이상을 모두 채웁니다. "
                "13회차 유지율 95%를 함께 봅니다. 최다 달성 본부의 총무가 참석합니다. 시험용 샘플입니다.", by=by, publish=True)
    # DB생명 여행시상은 판이 둘이다(원문 동영상 확인). 1차 원안을 만들고 2차 변경으로 고친다. 판 날짜는 샘플이라 정해 둔다.
    # 1차: 10만, 30만, 40만 연속 또는 합산 100만. 2배 인정은 매월 1~2주차. 여행지 추후 공지.
    # 2차: 조건 1-1(10월 달성자) 10만, 20만, 40만. 조건 1-2(10월 미달성자) 11월 25만, 12월 45만. 조건 2 합산 100만.
    #      2배 인정은 10월과 12월의 1~2주차만(11월 제외), 원보험료 10만원까지, 연속 조건에만. 보상 300만원 상당 싱가포르 여행, 2027년 11월.
    db_common = dict(kind="travel", insurer_id=ins("DB생명"), insurer_name="DB생명", sector="life", categories=["health"], windows=Q4,
                     combine_rule="either", date_basis="contract_date", target_note="DB생명 건강보험. 본인 계약 제외")
    db_tiers = lambda label, floors: dict(tiers=tiers(label, 1_000_000), window_tiers={i: tiers(label, m) for i, m in enumerate(floors, start=1)})
    v1_rules = {"boost": {"factor": 2, "month_days": [1, 14], "cap": 100_000, "applies_to": "each"}, "exclude_self": True}
    out["db"] = pm.create(
        con, period_ym=ym, name="DB생명 여행시상", rules=v1_rules, **db_common, **db_tiers("해외여행 (여행지 추후 공지)", DB_V1_FLOORS),
        caution="월 10만, 30만, 40만 연속 또는 3개월 합산 100만 가운데 하나를 채웁니다. 1, 2주차 실적은 원보험료 10만원까지 2배로 세고 "
                "연속 조건에만 씁니다. 여행지는 추후 공지합니다. 현금은 없습니다. 시험용 샘플입니다.", by=by, publish=True)
    con.execute("UPDATE promotion SET created_at=? WHERE promo_id=?", (DB_V1_AT, out["db"]))
    alt = {"alt_chains": [{"key": "c12", "label": "조건 1-2", "windows": {2: 250_000, 3: 450_000}, "requires_missed": [1]}]}
    v2_rules = {"boost": {"factor": 2, "ranges": DB_V2_BOOST, "cap": 100_000, "applies_to": "each"}, "exclude_self": True, **alt}
    pm.update(con, out["db"], name="DB생명 여행시상", rules=v2_rules, **db_common, **db_tiers("싱가포르 여행 (300만원 상당, 2027년 11월)", DB_V2_FLOORS),
              caution="조건 1-1(10월 달성자) 10만, 20만, 40만 연속, 조건 1-2(10월 미달성자) 11월 25만과 12월 45만 연속, 조건 2 3개월 합산 100만 가운데 "
                      "하나를 채웁니다. 10월과 12월의 1~2주차 실적은 원보험료 10만원까지 2배로 세고 연속 조건에만 씁니다. 11월은 2배 인정이 없습니다. "
                      "추가 수수료 250%, 추가 멤버십 시상은 시상안 원문 확인. 현금은 없습니다. 시험용 샘플입니다.", by=by)
    promo_extra.add_version_note(con, out["db"], DB_V2_NOTE, by=by)
    con.execute("UPDATE audit_log SET at=? WHERE id=(SELECT MAX(id) FROM audit_log WHERE action='promotion_update' AND target LIKE ?)",
                (DB_V2_AT, f"{out['db']}:%"))
    con.commit()
    out["conv"] = pm.create(
        con, period_ym=ym, name="DB생명 이터널 Convention 추가 시책", kind="travel", insurer_id=ins("DB생명"), insurer_name="DB생명",
        sector="life", windows=Q4, combine_rule="sum", date_basis="contract_date",
        tiers=[(1, "travel", None, "Convention 추가 시책 원문 확인")],
        rules={"parent_promo_id": out["db"], "gate_min_achievers": 15, "gate_org_path": " > ".join(f"{lv}:{n}" for lv, n in ETERNAL)},
        target_note="DB생명 여행시상 달성자",
        caution="여행시상에 딸린 시책입니다. 이터널 전체 달성자가 15명 이상이면 열립니다. 최다 달성 본부장이 행사에 동참합니다. 시험용 샘플입니다.",
        by=by, publish=True)
    dream = lambda: {s: [(m, "goods", None, f"{i}단계 선택형") for i, m in enumerate(ms, start=1)] for s, ms in DREAM_STEPS.items()}
    samsung = dict(insurer_id=ins("삼성화재"), insurer_name="삼성화재", sector="nonlife", date_basis="contract_date",
                   rules={"product_exclude": EXCLUDE, "drop_statuses": ["cancelled"]},
                   target_note="삼성화재 보험료. 단체, 실손, 펫, 캣, 재물, The 라이프업 제외. 13회 이내 품보와 철회 제외")
    common = dict(kind="linked", combine_rule="all", inst13_required=True, by=by, publish=True,
                  caution="창마다 단계를 채워야 하고 낮은 쪽 단계가 적용됩니다. 단계마다 여행1, 여행2, 금 가운데 하나를 고릅니다. 13회차 유지 조건이 있습니다. "
                          + CASH_TEXT + " 단계 문턱은 포스터 값입니다. 시험용 샘플입니다.", **samsung)
    out["dream"] = pm.create(con, period_ym=ym, name="삼성화재 드림+", windows=DREAM_WINDOWS, window_tiers=dream(), **common)
    out["dream_past"] = pm.create(con, period_ym="202609", name="삼성화재 드림+ 지난 차수", windows=PAST_WINDOWS, window_tiers=dream(), **common)
    steps = [(m, "fixed", None, None) for m in BRIDGE_STEPS]       # 금액을 비워 '원문 확인' 으로 보인다
    out["bridge"] = pm.create(
        con, period_ym=ym, name="삼성화재 10~11월 연속가동 브릿지", kind=pm.BRIDGE, windows=BRIDGE_WINDOWS, combine_rule="all",
        tiers=steps, window_tiers={1: [(BRIDGE_GATE, "fixed", None, None)], 2: steps},
        caution="10월 19일에서 31일과 11월 1일에서 15일을 연속으로 채웁니다. 첫 창은 문턱이고 단계는 둘째 창이 정합니다. "
                "단계 문턱은 시험용 숫자이고 시상 금액은 원문 확인입니다. 시험용 샘플입니다.", by=by, publish=True, **samsung)
    nxt = pm.copy_bridge(con, out["bridge"], period_ym="202611", windows=NEXT_BRIDGE, by=by, name="삼성화재 11~12월 연속가동 브릿지")
    pm.set_status(con, nxt["promo_id"], "published", by)
    out["bridge_next"] = nxt["promo_id"]

    promo_extra.set_screen(con, out["boom"], by=by, top_org_reward="최다 달성 본부의 총무 참석", reward_choices=None)
    promo_extra.set_screen(con, out["conv"], by=by, top_org_reward="최다 달성 본부장 행사 동참", reward_choices=None)
    for k in ("dream", "dream_past"):
        promo_extra.set_screen(con, out[k], by=by, top_org_reward=None, reward_choices=list(promo_extra.CHOICE_KO))
    pm.confirm_results(con, out["dream_past"], "admin")
    _recalc(con)
    return out
