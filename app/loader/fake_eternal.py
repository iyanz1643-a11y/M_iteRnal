"""가짜 이터널 양식 생성기. 실제 계약현황(56열)과 사원현황의 머리글과 값의 꼴만 따른다.

이름, 사번, 금액, 증권번호는 모두 임의 값이다. 실제 자료가 없을 때 적재기를 시험하는 데 쓴다.
만든 파일은 저장소 밖(시험의 임시 폴더나 data/ 아래)에 둔다.

    python -m app.loader.fake_eternal data/fake      # 가짜 계약현황과 사원현황을 data/fake 에 만든다

샘플에 일부러 넣는 것: 센터와 팀이 빈 행, 둘 이상의 지점 아래 나오는 센터 이름, 둘 이상의 센터 아래 나오는 팀 이름,
철회와 실효와 해지와 반송 행, 일시납 행, 분류표에 없는 상품종류 하나, 원수사 마스터에 없는 보험사 하나,
설계사 마스터에 없는 모집사원번호 하나, 모집과 수금이 다른 행.
"""
import pathlib
import random
import sys

import openpyxl

# 실제 계약현황의 머리글. 이 순서 그대로 1행에 있다. 매핑(config/mapping/eternal_contract.yaml)과 따로 적어 서로를 검사한다.
CONTRACT_HEADERS = [
    "No.", "맵핑상태", "맵핑구분", "본사", "본부", "지점", "센터", "팀", "모집사원번호", "모집사원명", "계약일자", "보험사", "증권번호",
    "계약자", "피보험자", "상품명", "초회보험료", "수금보험료", "1차년성적", "2차년성적", "3차년성적", "4차년성적", "인정보험료",
    "계약상태", "납입회차", "최종납입년월", "납입기간", "납입기간명", "계약종류", "상품종류", "납입주기(방법)", "수금사원번호",
    "수금사원명", "CA코드", "상품코드", "수기성적", "모바일청약", "시작일자", "종료일자", "자동이체일", "영수일자", "본인·가족계약",
    "외부이관여부", "엑셀등록여부", "확인서징구(개인)", "확인서징구(비교)", "확인서징구(금소법)", "확인서징구(종신)", "최초등록자",
    "최초등록자명", "최종수정자", "최종수정자명", "수정일시", "최종상태변경일", "최종수금일", "비고",
]
# 사원현황의 머리글. 뒤의 민감 열 넷은 실제 양식에 있다고 확인된 묶음(소득, 이행보증과 예치금, 계좌)을 가짜로 붙인 것이다.
EMPLOYEE_HEADERS = [
    "NO", "사원번호", "사원명", "총괄", "본부", "지점", "직할지점", "팀", "주민번호", "보험업계고유번호", "음양구분", "생년월일", "메뉴그룹",
    "직위구분", "업무구분", "수당직급", "입사일자", "퇴사일자", "4대보험(취득)", "4대보험(상실)", "위촉서류", "생보자격구분", "생보등록사",
    "생보등록일", "생보말소일", "손보자격구분", "손보등록사", "손보등록일", "손보말소일", "계좌번호", "소득(전년)", "이행보증보험금액", "예치금",
]
SENSITIVE_EMPLOYEE = ["주민번호", "보험업계고유번호", "음양구분", "생년월일", "4대보험(취득)", "4대보험(상실)", "계좌번호", "소득(전년)", "이행보증보험금액", "예치금"]

# 원수사 마스터에 있다고 보는 보험사. 이름은 총괄이 확인한 실제 보험사 이름의 예다.
MASTER_INSURERS = {
    "흥국생명": "life", "KB라이프": "life", "삼성생명": "life", "라이나생명": "life",
    "흥국화재": "nonlife", "KB손보": "nonlife", "DB손보": "nonlife", "메리츠화재": "nonlife", "삼성화재": "nonlife",
    "현대해상": "nonlife", "한화손보": "nonlife", "롯데손보": "nonlife",
}
UNKNOWN_INSURER = "가상손해보험"
UNKNOWN_PRODUCT_TYPE = "손보재물"
UNKNOWN_AGENT_CODE = "ZZ000001"
LIFE_TYPES = ["생보보장", "종신", "생보치매", "생보치아", "생보변액연금", "생보연금", "생보암", "생보정기", "생보단체", "생보저축", "경영인정기"]
NONLIFE_TYPES = ["손보보장", "손보운전자", "손보어린이", "손보암", "손보치매", "손보상해", "손보간병", "손보치아"]

SURNAMES = "가나다라마바사아자차카타파하"
SYLLABLES = "가람별솔빛새온누리한결다온하랑나래보미시루아라이음조은찬슬"
HEAD_OFFICE = "가상본사"
SHARED_CENTER = "누리센터"   # 둘 이상의 지점 아래 나온다
SHARED_TEAM = "다온팀"       # 둘 이상의 센터 아래 나온다


def fake_name(rng) -> str:
    return rng.choice(SURNAMES) + rng.choice(SYLLABLES) + rng.choice(SYLLABLES)


def _money(v) -> str:
    return f"{v:,}"


def fake_roster(seed=11) -> list:
    """가짜 설계사 40명. 조직 경로는 (본사, 본부, 지점, 센터, 팀). 센터 없는 사람 9명, 팀까지 있는 사람 9명."""
    rng = random.Random(seed)
    branches = [(f"가상{h}본부", f"가상{h}-{b}지점") for h in range(1, 5) for b in range(1, 3)]   # 본부 4개, 지점 8개
    paths = []
    # 센터 없음 9명
    for i in range(9):
        hq, br = branches[i % len(branches)]
        paths.append((HEAD_OFFICE, hq, br, None, None))
    # 센터만 22명. 공유 센터는 지점 둘 아래에 둔다
    centers = {}
    for i, (_hq, br) in enumerate(branches):
        own = f"{rng.choice(SYLLABLES)}{rng.choice(SYLLABLES)}{i}센터"
        centers[br] = [own, SHARED_CENTER] if i in (0, 5) else [own]
    for i in range(22):
        hq, br = branches[i % len(branches)]
        c = centers[br][(i // len(branches)) % len(centers[br])]
        paths.append((HEAD_OFFICE, hq, br, c, None))
    # 팀까지 9명. 공유 팀은 서로 다른 센터 둘 아래에 둔다
    for i in range(9):
        hq, br = branches[i % len(branches)]
        c = centers[br][0]
        team = SHARED_TEAM if i in (0, 3) else f"{rng.choice(SYLLABLES)}{rng.choice(SYLLABLES)}{i}팀"
        paths.append((HEAD_OFFICE, hq, br, c, team))
    codes = set()
    roster = []
    for i, p in enumerate(paths):
        while True:
            code = "MT" + f"{rng.randint(100000, 999999)}"
            if code not in codes:
                codes.add(code)
                break
        roster.append({"code": code, "name": fake_name(rng), "path": p, "position": "FC" if i % 10 else "지점장",
                       "hire_date": f"20{rng.randint(18, 25):02d}{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}"})
    return roster


def fake_contract_rows(n=300, period_ym="202602", seed=7, roster=None) -> list:
    """가짜 계약현황 행. 머리글을 키로 하는 dict 목록이다. 값은 실제 파일처럼 글자로 둔다(No. 만 숫자)."""
    rng = random.Random(seed)
    roster = roster or fake_roster()
    y, mo = int(period_ym[:4]), int(period_ym[4:])
    special = {5: "철회", 17: "실효", 29: "해지", 41: "반송", 53: "철회", 65: "실효"}
    rows, policies = [], set()
    for i in range(n):
        ag = roster[i % len(roster)] if i < len(roster) else rng.choice(roster)
        if i == 77:
            insurer, sector = UNKNOWN_INSURER, "nonlife"
        else:
            insurer = rng.choice(list(MASTER_INSURERS))
            sector = MASTER_INSURERS[insurer]
        ptype = UNKNOWN_PRODUCT_TYPE if i == 88 else rng.choice(LIFE_TYPES if sector == "life" else NONLIFE_TYPES)
        single = i in (12, 140)
        premium = rng.randint(100, 1500) * 1000 if single else rng.randint(10, 3000) * 10 + rng.choice([0, 0, 50])
        recognized = int(premium * (0.1 if single else rng.choice([0.5, 0.8, 1.0, 1.2, 1.5])))
        day = rng.randint(1, 28)
        cdate = f"{y}{mo:02d}{day:02d}"
        status = special.get(i, "정상")
        sday = min(28, day + rng.randint(1, 10)) if status != "정상" else day
        scd = f"{y}{mo:02d}{sday:02d}"
        while True:
            pol = f"{rng.choice('ABCDEFGH')}{rng.randint(10**9, 10**10 - 1)}"
            if pol not in policies:
                policies.add(pol)
                break
        collector = ag if rng.random() > 0.1 else rng.choice(roster)
        code = UNKNOWN_AGENT_CODE if i == 99 else ag["code"]
        hq_office, hq, br, center, team = ag["path"]
        yn = lambda p=0.1: "Y" if rng.random() < p else "N"
        rows.append({
            "No.": i + 1, "맵핑상태": "맵핑완료", "맵핑구분": "자동", "본사": hq_office, "본부": hq, "지점": br,
            "센터": center or "", "팀": team or "", "모집사원번호": code, "모집사원명": ag["name"], "계약일자": cdate,
            "보험사": insurer, "증권번호": pol, "계약자": fake_name(rng), "피보험자": fake_name(rng),
            "상품명": f"가상 {ptype} 상품 {rng.randint(1, 9)}형", "초회보험료": _money(premium), "수금보험료": _money(premium),
            "1차년성적": _money(recognized), "2차년성적": "0", "3차년성적": "0", "4차년성적": "0", "인정보험료": _money(recognized),
            "계약상태": status, "납입회차": "1", "최종납입년월": f"{y}{mo:02d}",
            "납입기간": "1" if single else str(rng.choice([10, 20, 30])), "납입기간명": "일시납" if single else "",
            "계약종류": "생보" if sector == "life" else "손보", "상품종류": ptype, "납입주기(방법)": "일시납" if single else "월납",
            "수금사원번호": collector["code"], "수금사원명": collector["name"], "CA코드": f"CA{rng.randint(1000, 9999)}",
            "상품코드": f"P{rng.randint(10000, 99999)}", "수기성적": "0", "모바일청약": yn(0.6),
            "시작일자": cdate, "종료일자": f"{y + 20}{mo:02d}{day:02d}", "자동이체일": str(rng.choice([5, 10, 15, 20, 25])),
            "영수일자": cdate, "본인·가족계약": yn(0.05), "외부이관여부": yn(0.02), "엑셀등록여부": yn(0.1),
            "확인서징구(개인)": yn(0.3), "확인서징구(비교)": yn(0.3), "확인서징구(금소법)": yn(0.5), "확인서징구(종신)": yn(0.2) if ptype == "종신" else "N",
            "최초등록자": "SYS0001", "최초등록자명": "가상관리", "최종수정자": "SYS0001", "최종수정자명": "가상관리",
            "수정일시": f"{y}{mo:02d}{sday:02d} 10:00:00", "최종상태변경일": scd, "최종수금일": cdate, "비고": "",
        })
    return rows


def fake_employee_rows(roster=None, seed=13) -> list:
    """가짜 사원현황 행. 민감 열에도 가짜 값을 넣어 버려지는지 시험한다."""
    rng = random.Random(seed)
    roster = roster or fake_roster()
    rows = []
    for i, a in enumerate(roster):
        _, hq, br, center, team = a["path"]
        left = i == 3
        rows.append({
            "NO": i + 1, "사원번호": a["code"], "사원명": a["name"], "총괄": "가상총괄", "본부": hq, "지점": br,
            "직할지점": center or "", "팀": team or "",
            # 민감 열의 가짜 값. 어떤 개인정보 꼴에도 맞지 않게 만든다
            "주민번호": f"{rng.randint(0, 999999):06d}-0{rng.randint(0, 999999):06d}", "보험업계고유번호": f"K{rng.randint(10**7, 10**8 - 1)}",
            "음양구분": "양", "생년월일": f"19{rng.randint(60, 99)}0101", "메뉴그룹": "설계사", "직위구분": a["position"], "업무구분": "영업",
            "수당직급": "FC", "입사일자": a["hire_date"], "퇴사일자": "20260131" if left else "",
            "4대보험(취득)": a["hire_date"], "4대보험(상실)": "", "위촉서류": "완료",
            "생보자격구분": "등록", "생보등록사": "흥국생명", "생보등록일": a["hire_date"], "생보말소일": "20260131" if left else "",
            "손보자격구분": "등록" if i % 3 else "미등록", "손보등록사": "DB손보" if i % 3 else "", "손보등록일": a["hire_date"] if i % 3 else "",
            "손보말소일": "",
            "계좌번호": f"{rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(100000, 999999)}",
            "소득(전년)": _money(rng.randint(1000, 9000) * 10000), "이행보증보험금액": _money(rng.randint(1, 5) * 1000000),
            "예치금": _money(rng.randint(0, 3) * 100000),
        })
    return rows


def write_xlsx(path, headers, rows, sheet="Sheet1", extra_sheets=None):
    """1행에 머리글, 2행부터 값. rows 는 머리글을 키로 하는 dict 목록."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(list(headers))
    for r in rows:
        ws.append([r.get(h, "") for h in headers])
    for name, grid in (extra_sheets or {}).items():
        es = wb.create_sheet(name)
        for line in grid:
            es.append(line)
    wb.save(path)
    return path


def make_contract_file(path, n=300, period_ym="202602", seed=7, roster=None):
    rows = fake_contract_rows(n, period_ym, seed, roster)
    return write_xlsx(path, CONTRACT_HEADERS, rows, sheet="계약현황"), rows


def make_employee_file(path, roster=None):
    rows = fake_employee_rows(roster)
    # 실제 파일처럼 정리 시트를 뒤에 하나 더 둔다. 사람 행이 없는 요약이다
    summary = [["구분", "인원"], ["FC", sum(1 for r in rows if r["직위구분"] == "FC")], ["지점장", sum(1 for r in rows if r["직위구분"] == "지점장")]]
    return write_xlsx(path, EMPLOYEE_HEADERS, rows, sheet="조회", extra_sheets={"정리": summary}), rows


if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "data/fake")
    roster = fake_roster()
    c, crow = make_contract_file(out / "fake_contract_202602.xlsx", roster=roster)
    e, erow = make_employee_file(out / "fake_employee.xlsx", roster=roster)
    print(f"가짜 계약현황 {len(crow)}행: {c}")
    print(f"가짜 사원현황 {len(erow)}행: {e}")
