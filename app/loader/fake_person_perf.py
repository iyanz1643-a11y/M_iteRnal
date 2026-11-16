"""가짜 인별실적 파일 생성기. 2026-11-16 에 확인한 실제 인별실적 파일의 머리글 꼴만 따른다.

이름, 사번, 금액은 모두 임의 값이다. 실제 자료가 없을 때 app/loader/person_perf.py 를 시험하는 데 쓴다.
실제 파일처럼 "정리"(부분집합, 열 순서가 다르다), 무실적 시트, "인별" 을 두고, "인별" 에는 직위 칸이 둘이고 마지막에 합계 행이 있다.

    python -m app.loader.fake_person_perf data/fake      # 가짜 3개월 인별실적을 data/fake 에 만든다
"""
import pathlib
import random
import sys

import openpyxl

from app.loader import fake_eternal as fe

# 보험사 묶음. 실제 파일의 37개 가운데 일부다. 이름 꼴(IM라이프, 농협생명)이 마스터와 다른 것도 넣었다
INSURERS = ["동양생명", "한화생명", "IM라이프", "흥국생명", "농협생명", "KB라이프", "삼성화재", "KB손보", "DB손보", "흥국화재"]
FIXED_GROUPS = [
    ("{total}", ["건수", "월납보험료", "건당보험료", "원수사성적", "글로벌성적"]),
    ("유실적(0.1만이상)", ["건수", "월납보험료"]),
    ("유실적(10만이상)", ["건수", "월납보험료"]),
    ("정예(50만이상)", ["건수", "보험료"]),
    ("일시납", ["건수", "보험료", "원수사성적"]),
    ("자동차", ["건수", "보험료", "글로벌성적"]),
    ("일반", ["건수", "보험료", "글로벌성적"]),
]
PERSON_INBYEOL = ["No.", "직위", "사원번호", "직위", "사원명", "총괄", "본부", "지점", "직할지점", "팀"]
PERSON_JEONGRI = ["No.", "사원번호", "사원명", "직위", "총괄", "본부", "지점", "직할지점"]


def fake_people(roster=None, seed=21, zero_every=4) -> list:
    """사람 한 줄은 {사원번호, 사원명, 직위, 총괄, 본부, 지점, 직할지점, 팀, values: {(묶음, 칸): 값}, insurers: {보험사: (건수, 월납)}}.
    zero_every 번째마다 무실적이다. Total 건수와 월납보험료는 보험사별 값의 합이다."""
    rng = random.Random(seed)
    roster = roster or fe.fake_roster()
    out = []
    for i, a in enumerate(roster):
        _, hq, br, center, team = a["path"]
        ins = {}
        if i % zero_every:
            for name in rng.sample(INSURERS, rng.randint(1, 4)):
                n = rng.randint(1, 5)
                ins[name] = (n, n * rng.randint(3, 30) * 10_000)
        cnt = sum(v[0] for v in ins.values())
        mp = sum(v[1] for v in ins.values())
        v = {
            ("total", "건수"): cnt, ("total", "월납보험료"): mp, ("total", "건당보험료"): mp // cnt if cnt else 0,
            ("total", "원수사성적"): mp * 12, ("total", "글로벌성적"): mp * 10,
            ("유실적(0.1만이상)", "건수"): None, ("유실적(0.1만이상)", "월납보험료"): None,
            ("유실적(10만이상)", "건수"): cnt if mp >= 100_000 else 0, ("유실적(10만이상)", "월납보험료"): mp if mp >= 100_000 else 0,
            ("정예(50만이상)", "건수"): cnt if mp >= 500_000 else 0, ("정예(50만이상)", "보험료"): mp if mp >= 500_000 else 0,
            ("일시납", "건수"): 0, ("일시납", "보험료"): 0, ("일시납", "원수사성적"): 0,
            ("자동차", "건수"): 1 if i % 3 == 0 and cnt else 0, ("자동차", "보험료"): 450_000 if i % 3 == 0 and cnt else 0,
            ("자동차", "글로벌성적"): 1234.5 if i % 3 == 0 and cnt else 0,
            ("일반", "건수"): 0, ("일반", "보험료"): 0, ("일반", "글로벌성적"): 0,
        }
        out.append({"사원번호": a["code"], "사원명": a["name"], "직위": a["position"], "총괄": "가상총괄", "본부": hq, "지점": br,
                    "직할지점": center or "", "팀": team or "", "values": v, "insurers": ins})
    return out


def _header(person_cols, total_label, insurers=INSURERS, extra_groups=()):
    h1, h2 = list(person_cols), [None] * len(person_cols)
    groups = [(total_label if g == "{total}" else g, subs) for g, subs in FIXED_GROUPS] + list(extra_groups) + [(n, ["건수", "월납보험료"]) for n in insurers]
    for g, subs in groups:
        for k, s in enumerate(subs):
            h1.append(g if k == 0 else None)
            h2.append(s)
    return h1, h2, groups


def _line(p, i, person_cols, groups, total_label):
    row = []
    for c in person_cols:
        row.append(i + 1 if c == "No." else p[c])
    for g, subs in groups:
        for s in subs:
            if g == total_label:
                row.append(p["values"][("total", s)])
            elif (g, s) in p["values"]:
                row.append(p["values"][(g, s)])
            elif g in p["insurers"]:
                row.append(p["insurers"][g][0] if s == "건수" else p["insurers"][g][1])
            else:
                row.append(0)
    return row


def write_person_perf(path, people, *, total_label="월간Total", sheet="인별", insurers=INSURERS, person_cols=None,
                      extra_groups=(), summary=True, work_sheets=True, rename=None):
    """people 로 인별실적 파일을 쓴다. rename 은 {원래 1행 머리글: 바꿀 이름} 으로 머리글이 바뀐 파일을 만든다."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    if work_sheets:
        ws = wb.create_sheet("정리")
        h1, h2, groups = _header(PERSON_JEONGRI, "2026년03~05월간Total", insurers)
        ws.append(h1)
        ws.append(h2)
        for i, p in enumerate(people[:-2]):
            ws.append(_line(p, i, PERSON_JEONGRI, groups, "2026년03~05월간Total"))
        ws = wb.create_sheet("무실적")
        ws.append([])
        ws.append(["No.", "사원번호", "사원명", "직위", "건수", "월납보험료"])
        for i, p in enumerate(x for x in people if not x["values"][("total", "건수")]):
            ws.append([i + 1, p["사원번호"], p["사원명"], p["직위"], 0, 0])
    ws = wb.create_sheet(sheet)
    cols = person_cols or PERSON_INBYEOL
    h1, h2, groups = _header(cols, total_label, insurers, extra_groups)
    if rename:
        h1 = [rename.get(h, h) if isinstance(h, str) else h for h in h1]
    ws.append(h1)
    ws.append(h2)
    for i, p in enumerate(people):
        ws.append(_line(p, i, cols, groups, total_label))
    if summary:
        total = ["합계" if c == "No." else None for c in cols]
        for g, subs in groups:
            for s in subs:
                vals = []
                for p in people:
                    if g == total_label:
                        vals.append(p["values"][("total", s)])
                    elif (g, s) in p["values"]:
                        vals.append(p["values"][(g, s)])
                    elif g in p["insurers"]:
                        vals.append(p["insurers"][g][0] if s == "건수" else p["insurers"][g][1])
                total.append(sum(v or 0 for v in vals) if any(v is not None for v in vals) else None)
        total[0] = None                                   # 실제 파일의 합계 행은 사원번호와 사원명이 빈 행이다
        ws.append(total)
    wb.save(path)
    return path


if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "data/fake")
    ppl = fake_people()
    print(f"가짜 인별실적 {len(ppl)}명: {write_person_perf(out / 'fake_202603-05_인별실적.xlsx', ppl)}")
