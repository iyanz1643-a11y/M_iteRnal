"""대조용 미리보기 DB 를 만든다. 실제 실적 파일로 앱 화면의 프로모션 진척을 실제 진척과 견주어 보려는 것이다.

    TRIAL_PASSWORD=... python scripts/build_trial_db.py                      # 기본 경로로 만든다
    TRIAL_PASSWORD=... python scripts/build_trial_db.py --out data/real/trial.db --src data/real/drive_20261117/사원현황 \
        --months data/real/trial_months.yaml [--heads data/real/org_heads_local.yaml] [--month-sum-contracts]
    TRIAL_PASSWORD=... python scripts/build_trial_db.py --out data/real/trial2.db --contracts data/real/.../20261118_계약현황.xlsx

- DB 는 data/real/ 아래에만 둔다(D-75 와 같은 성격). 저장소 안의 다른 곳, 운영 DB, 샘플 DB 에는 쓰지 않는다.
  이미 있는 파일은 이 스크립트가 만든 DB(PRAGMA user_version 표시)일 때만 지우고 다시 만든다.
- 사원현황 파일 없이 실적 파일의 조직 칸으로 조직과 사람을 만든다(대표님 지시 2026-11-17). 읽기는 app/loader/trial_perf.py 다.
- 월은 --months 의 YAML 로 받는다. 키는 파일 이름, 값은 YYYYMM 이다. 비어 있으면 그 파일의 실적은 넣지 않는다.
  파일이 없으면 원본 폴더의 파일 이름으로 빈 틀을 만든다. 월이 모두 비어도 조직, 사람, 계정, 프로모션 등록까지는 한다.
- 로그인 아이디는 사번이 아니라 T0001 꼴의 대조용 번호다. 사번 순으로 붙인다. 메인 관리자는 admin 이다.
  비밀번호는 환경 변수 TRIAL_PASSWORD 다. 화면에 찍지 않는다.
- 조직장은 --heads 의 YAML 이름을 실적 파일의 사원명과 맞춰 지정한다. 맞는 사람이 없거나 둘 이상이면 지정하지 않고 건수만 보고한다.
- 프로모션 넷은 tests/test_promo_org_progress.py 의 시험용 정의와 같은 조건으로 등록한다. 금액 구간은 시험용 숫자다.
- 계약 원장에는 기본으로 아무것도 넣지 않는다. --month-sum-contracts 를 주면 보험사별 월 합계를 '그 달 대표 계약 1건' 으로 넣는다
  (연결 방식 가). 만든 행은 상품명에 '월 합계 대표 계약(대조용 추정)' 이 붙는다. 프로모션 유의사항에 추정의 한계를 적는다.
- --contracts <파일> 은 실제 계약 단위 계약현황(56열 또는 58열)을 계약 원장에 넣는다(app/loader/eternal_contract.py).
  이 인자가 있으면 --month-sum-contracts 와 월 설정(--months)은 쓰지 않는다. 실적 합계 파일의 월 가정으로 agent_period_perf 를 채우지 않는다.
  사람은 실적 합계 파일의 조직 칸에서 만든 사람에 계약현황의 모집사원을 더한다. 같은 사번은 같은 사람이고 조직은 계약현황의 가장 늦은 계약을 따른다.
  계약자와 피보험자 같은 이름 칸은 계약 원장과 원본 행에 비워 둔다(REDACT). 흥국화재 BOOM-UP 만 게시하고 나머지 셋은 초안으로 둔다.
  계약을 넣은 뒤 계약이 있는 달마다 업적 집계(app/engine/performance.run)를 돌려 업적 화면과 프로모션이 같은 계약을 보게 한다.
- 출력은 건수와 파일 이름뿐이다. 사람 이름, 사번, 금액은 찍지 않는다.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from app import db, masters  # noqa: E402
from app.engine import performance  # noqa: E402
from app.engine import promotion as pm  # noqa: E402
from app.loader import eternal_contract as ec  # noqa: E402
from app.loader import trial_perf as tp  # noqa: E402
from app.loader.sheet import HeaderMismatch  # noqa: E402
from app.loader.transforms import normalize_header  # noqa: E402
from app.org import heads, levels, sync  # noqa: E402

TRIAL_MARK = 20261117                      # PRAGMA user_version. 이 스크립트가 만든 DB 라는 표시
REAL_DIR = db.ROOT / "data" / "real"
DEFAULTS = {"out": REAL_DIR / "trial.db", "src": REAL_DIR / "drive_20261117" / "사원현황", "months": REAL_DIR / "trial_months.yaml",
            "heads": REAL_DIR / "org_heads_local.yaml"}
BY = "대조용 DB"
FROM_YM = "202601"                         # 달이 하나도 없을 때 소속과 조직장 지정의 시작 달
YM_RX = re.compile(r"^20\d{2}(0[1-9]|1[0-2])$")

# ------------------------------------------------------------------ 프로모션 (시험용 정의와 같은 조건)
PROMO_YM = "202610"
Q4 = [("10월", "2026-10-01", "2026-10-31"), ("11월", "2026-11-01", "2026-11-30"), ("12월", "2026-12-01", "2026-12-31")]
DREAM = {1: [100_000, 200_000, 300_000, 500_000, 800_000], 2: [200_000, 300_000, 500_000, 1_000_000, 1_500_000],
         3: [200_000, 300_000, 500_000, 1_000_000, 1_500_000]}
DREAM_WINDOWS = [("10월 19일에서 31일", "2026-10-19", "2026-10-31"), ("11월", "2026-11-01", "2026-11-30"), ("12월", "2026-12-01", "2026-12-31")]
EXCLUDE = ["단체", "실손", "펫", "캣", "재물", "The 라이프업"]
TRIAL_NOTE = "대조용 미리보기입니다. 실적은 보험사별 월 합계로 만든 추정이고 계약 단위로 세지 않습니다. 구간 금액은 시험용 숫자입니다."
# 계약현황을 넣을 때 값을 비울 원본 머리글. 계약자와 피보험자는 적재하지 않는다. 설계사 이름은 설계사 마스터에 쓰므로 비우지 않는다.
REDACT = ("계약자", "피보험자", "수금사원명", "최초등록자명", "최종수정자명")
CONTRACT_NOTE = ("대조용 미리보기입니다. 2026-11-18 계약현황의 계약 단위 자료로 셉니다. 이 파일에 청약일자 칸이 없어 계약일자를 청약일로 썼고 "
                 "영수일자는 쓰지 않았습니다. 철회와 반송은 뺐습니다. 상품 거르기를 하지 않아 인정보험료가 0 인 계약은 값으로 빠집니다. "
                 "금액 구간은 시상안 원문으로 확인하지 않은 대조용 값입니다.")
DRAFT_NOTE = "대조용 초안입니다. 게시하지 않았고 실제 계약 자료로 확인하지 않았습니다. 구간 금액은 시험용 숫자입니다."
LIMITS = {
    "흥국": "청약 기준과 장기 인보험 여부를 가르지 못해 손보 시트의 월 합계를 모두 셉니다. 실제보다 크게 나올 수 있습니다.",
    "DB": "건강보험만 세는 거르기와 본인 계약 제외를 하지 못해 실제보다 크게 나올 수 있습니다. "
          "1, 2주차 2배 인정을 하지 못해 월별 조건은 실제보다 작게 나올 수 있습니다.",
    "삼성": "실손, 펫 같은 제외 상품과 13회 이내 품보를 빼지 못해 실제보다 크게 나올 수 있습니다. "
            "10월 합계가 10월 31일 하루에 몰려 10월 19일에서 31일 창에 10월 전체가 들어갑니다.",
}


def _tiers(*rows):
    return [(m, "travel", None, label) for m, label in rows]


def _insurer(con, name):
    r = con.execute("SELECT insurer_id FROM insurer WHERE name=?", (name,)).fetchone()
    if not r:
        raise ValueError(f"원수사 마스터에 {name} 가 없습니다")
    return r[0]


def register_promotions(con, gate_org_path=None, contracts=False) -> dict:
    """프로모션 넷을 등록한다. 조건은 tests/test_promo_org_progress.py 의 _heungkuk, _db_life, 시험 Convention, _samsung 과 같다.
    Convention 의 대상 직급(target_levels)은 이 DB 에 커리어 직급이 없어 뺀다. 넣으면 모두 대상이 아니게 된다.
    contracts 가 거짓이면 넷 모두 게시한다(월 합계 대표 계약 연결). 참이면 흥국화재 BOOM-UP 만 게시하고 나머지는 초안이다.
    참일 때 BOOM-UP 은 실적 지표 인정보험료, 보험사 흥국화재(흥국생명 아님), 청약 기준(철회와 반송을 뺀다), 상품 거르기 없음이다."""
    out = {}
    pub_other = not contracts
    boom_note = f"{CONTRACT_NOTE}" if contracts else f"{TRIAL_NOTE} {LIMITS['흥국']}"
    boom = dict(
        period_ym=PROMO_YM, name="흥국화재 BOOM-UP (대조용)", kind="travel", windows=Q4, combine_rule="either", insurer_name="흥국화재",
        insurer_id=_insurer(con, "흥국화재"), date_basis="application_date", tiers=_tiers((400_000, "여행")),
        window_tiers={1: _tiers((50_000, "여행")), 2: _tiers((100_000, "여행")), 3: _tiers((150_000, "여행"))},
        rules={"count_basis": "application", "retention_min": 95, "retention_ref": "2027-12-31"},
        caution=boom_note, by=BY, publish=True)
    if contracts:
        out["boom"] = pm.create(con, metric="converted_premium", target_note="흥국화재 손해보험. 상품 거르기 없음. 대조용 가정", **boom)
    else:
        out["boom"] = pm.create(con, categories=["health"], target_note="흥국화재 장기 인보험. 대조용 추정", **boom)
    if contracts:       # 실제 계약 단위 자료. 2차 변경판(조건 1-1, 1-2, 2)이고 적용 상품은 건강 계열 가정이다. 보험료는 인정보험료로 센다(대조용 가정)
        db_rules = {"boost": {"factor": 2, "ranges": [["2026-10-01", "2026-10-14"], ["2026-12-01", "2026-12-14"]], "cap": 100_000, "applies_to": "each"},
                    "exclude_self": True, "alt_chains": [{"key": "c12", "label": "조건 1-2", "windows": {2: 250_000, 3: 450_000}, "requires_missed": [1]}]}
        out["db"] = pm.create(
            con, period_ym=PROMO_YM, name="DB생명 여행시상 (대조용)", kind="travel", windows=Q4, combine_rule="either", insurer_name="DB생명",
            insurer_id=_insurer(con, "DB생명"), sector="life", categories=["health"], metric="converted_premium", date_basis="contract_date",
            tiers=_tiers((1_000_000, "싱가포르")),
            window_tiers={1: _tiers((100_000, "싱가포르")), 2: _tiers((200_000, "싱가포르")), 3: _tiers((400_000, "싱가포르"))},
            rules=db_rules, target_note="DB생명 건강 계열(보장, 치매). 종신 제외는 가정. 본인 계약 제외. 대조용 가정",
            caution=CONTRACT_NOTE, by=BY, publish=True)
    else:
        out["db"] = pm.create(
            con, period_ym=PROMO_YM, name="DB생명 여행시상 (대조용)", kind="travel", windows=Q4, combine_rule="either", insurer_name="DB생명",
            insurer_id=_insurer(con, "DB생명"), categories=["health"], date_basis="contract_date", tiers=_tiers((1_000_000, "싱가포르")),
            window_tiers={1: _tiers((100_000, "싱가포르")), 2: _tiers((300_000, "싱가포르")), 3: _tiers((400_000, "싱가포르"))},
            rules={"boost": {"factor": 2, "month_days": [1, 14], "cap": 100_000, "applies_to": "each"}, "exclude_self": True},
            target_note="DB생명 건강보험. 본인 계약 제외. 대조용 추정", caution=f"{TRIAL_NOTE} {LIMITS['DB']}", by=BY, publish=pub_other)
    rules = {"parent_promo_id": out["db"], "gate_min_achievers": 4}
    if gate_org_path:
        rules["gate_org_path"] = gate_org_path
    out["conv"] = pm.create(
        con, period_ym=PROMO_YM, name="DB생명 Convention (대조용)", kind="travel", windows=Q4, tiers=_tiers((1, "추가 시책 원문 확인")),
        combine_rule="sum", date_basis="contract_date", rules=rules, target_note="DB생명 여행시상 달성자. 대조용 추정",
        caution=DRAFT_NOTE if contracts else f"{TRIAL_NOTE} 여행시상의 실적을 그대로 씁니다. {LIMITS['DB']}", by=BY, publish=pub_other)
    wt = {s: [(m, "goods", None, f"{i}단계 선택형") for i, m in enumerate(ms, start=1)] for s, ms in DREAM.items()}
    out["dream"] = pm.create(
        con, period_ym=PROMO_YM, name="삼성화재 드림+ (대조용)", kind="linked", windows=DREAM_WINDOWS, combine_rule="all", window_tiers=wt,
        insurer_name="삼성화재", insurer_id=_insurer(con, "삼성화재"), date_basis="contract_date", inst13_required=True,
        rules={"product_exclude": EXCLUDE, "drop_statuses": ["cancelled"]}, target_note="삼성화재 보험료. 제외 상품 있음. 대조용 추정",
        caution=DRAFT_NOTE if contracts else f"{TRIAL_NOTE} {LIMITS['삼성']}", by=BY, publish=pub_other)
    return out


# ------------------------------------------------------------------ 조직장
def assign_heads(con, heads_cfg: dict, people: dict, agents: dict, from_ym: str) -> dict:
    """heads_cfg 의 이름을 사원명과 맞춰 조직장을 지정한다. 이름은 돌려주지도 찍지도 않는다.
    parent 는 총괄본부(지점 칸) 이름, bonbu 는 [{org, head_name}] 으로 예하 본부(센터 칸) 이름이다.
    돌려주는 것: {wanted, assigned, no_match, many_match, no_org, outside}. outside 는 지정했지만 그 조직 밖에 있는 사람 수다."""
    m = tp.load_map(tp.RANK)
    lv = m["org_levels"]
    by_name = {}
    for code, p in people.items():
        by_name.setdefault(normalize_header(p["name"] or ""), []).append(code)
    stats = dict(wanted=0, assigned=0, no_match=0, many_match=0, no_org=0, outside=0)
    targets = []
    parent = (heads_cfg or {}).get("parent")
    if parent and heads_cfg.get("parent_head_name"):
        targets.append(((("org_general", parent),), heads_cfg["parent_head_name"]))
    for b in (heads_cfg or {}).get("bonbu") or []:
        if parent and b.get("org") and b.get("head_name"):
            targets.append(((("org_general", parent), ("org_hq", b["org"])), b["head_name"]))
    for want, name in targets:
        stats["wanted"] += 1
        codes = by_name.get(normalize_header(name), [])
        if not codes:
            stats["no_match"] += 1
            continue
        if len(codes) > 1:
            stats["many_match"] += 1
            continue
        key_level, key_name = lv[want[-1][0]], want[-1][1]
        parent_name = want[0][1]
        cand = [r for r in con.execute("SELECT org_id, path_key FROM org_unit WHERE level=? AND name=?", (key_level, key_name))]
        if len(want) > 1:
            cand = [r for r in cand if f"{lv['org_general']}:{parent_name}" in (r["path_key"] or "").split(sync.SEP)]
        if len(cand) != 1:
            stats["no_org"] += 1
            continue
        code = codes[0]
        org = people[code]["org"]
        if any(org.get(f) != v for f, v in want):
            stats["outside"] += 1
        heads.assign_head(con, cand[0]["org_id"], agents[code], from_ym, by=BY)
        stats["assigned"] += 1
    con.commit()
    return stats


# ------------------------------------------------------------------ 계정
def trial_accounts(con, password: str) -> int:
    """admin 과 설계사 계정을 만든다. 설계사 아이디는 사번 순으로 T0001, T0002 ... 이다. 첫 접속 때 비밀번호를 바꾸게 하지 않는다."""
    from app.web.auth import hash_password

    h = hash_password(password)
    now = db.now()
    con.execute("INSERT INTO user_account(login_id, password_hash, display_name, is_main_admin, must_change_password, created_at) VALUES('admin',?,?,1,0,?)",
                (h, "메인 관리자", now))
    n = 1
    rows = con.execute("SELECT agent_id, name FROM agent WHERE status='active' ORDER BY internal_code").fetchall()
    for i, a in enumerate(rows, start=1):
        con.execute("INSERT INTO user_account(login_id, password_hash, agent_id, display_name, must_change_password, created_at) VALUES(?,?,?,?,0,?)",
                    (f"T{i:04d}", h, a["agent_id"], a["name"], now))
        n += 1
    con.commit()
    return n


# ------------------------------------------------------------------ 월 설정
def read_months(path: pathlib.Path, files: list) -> tuple:
    """{파일 이름: YYYYMM 또는 None}. 파일이 없으면 빈 틀을 쓰고 모두 None 이다. 돌려주는 것: (months, 알림 목록)."""
    notes = []
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# 대조용 DB 의 파일별 월. 키는 원본 폴더의 파일 이름, 값은 YYYYMM 이다. 비어 있으면 그 파일의 실적은 넣지 않는다.",
                 "# 같은 달에 인별업적순위 하나와 보험사별 하나를 둘 수 있다. 본문이 같은 파일은 하나만 넣는다.",
                 "# 실제 자료의 설정이다. data/ 는 저장소에서 빠져 있다."]
        lines += [f"\"{f}\": " for f in sorted(files)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        notes.append(f"월 설정 틀을 만들었습니다: {path}")
        return {f: None for f in files}, notes
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit("월 설정은 '파일 이름: YYYYMM' 꼴이어야 합니다")
    out = {}
    for k, v in raw.items():
        v = None if v in (None, "") else str(v).strip()
        if v is not None and not YM_RX.match(v):
            raise SystemExit(f"월 설정의 값이 YYYYMM 꼴이 아닙니다: {k}")
        out[str(k)] = v
    for k in sorted(set(out) - set(files)):
        notes.append(f"월 설정에 있지만 원본 폴더에 없는 파일: {k}")
    for f in files:
        out.setdefault(f, None)
    return out, notes


# ------------------------------------------------------------------ 만들기
def _guard_out(out: pathlib.Path):
    out = out.resolve()
    if out == db.DEFAULT_DB.resolve():
        raise SystemExit("샘플 DB 에는 쓰지 않습니다")
    try:
        out.relative_to(db.ROOT)
        inside = True
    except ValueError:
        inside = False
    if inside:
        try:
            out.relative_to(REAL_DIR.resolve())
        except ValueError as e:
            raise SystemExit("저장소 안에서는 data/real/ 아래에만 만듭니다") from e
    if out.exists():
        con = db.connect(out)
        mark = con.execute("PRAGMA user_version").fetchone()[0]
        con.close()
        if mark != TRIAL_MARK:
            raise SystemExit("이미 있는 파일이 이 스크립트가 만든 대조용 DB 가 아니라 지우지 않습니다")


def contract_ym(path) -> str:
    """계약현황 파일 이름 앞의 날짜(YYYYMMDD)에서 달을 뽑는다. 없으면 REF 달이다. 적재 묶음의 이름표일 뿐 계약의 달이 아니다."""
    m = re.match(r"^(20\d{2})(0[1-9]|1[0-2])", pathlib.Path(path).name)
    return m.group(1) + m.group(2) if m else tp.REF_YM


def load_contracts(con, path, agents) -> dict:
    """계약현황 한 파일을 계약 원장에 넣고 계약이 있는 달마다 업적 집계를 돌린다. 이름 칸(REDACT)은 비운다.
    돌려주는 것: 건수와 상태별 건수, 업적 집계를 돌린 달. 사람 이름, 사번, 금액은 담지 않는다."""
    s = ec.load_contract(con, path, contract_ym(path), by=BY, redact=REDACT)
    con.commit()
    by_status = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM contract GROUP BY status ORDER BY status")}
    yms = [r[0] for r in con.execute("SELECT DISTINCT perf_ym FROM contract WHERE perf_ym IS NOT NULL ORDER BY perf_ym")]
    runs = {ym: performance.run(con, ym, BY)[1] for ym in yms}
    con.commit()
    return {"mapping_version": con.execute("SELECT mapping_version FROM import_batch WHERE batch_id=?", (s["batch_id"],)).fetchone()[0],
            "file_rows": s["file_rows"], "loaded": s["loaded"], "skipped": s["skipped"], "inserted": s["inserted"], "by_status": by_status,
            "dropped_columns": len(s["dropped_columns"]), "sums_ok": s["block"] == 0, "perf_runs": runs, "agents": len(agents),
            "blank_names": con.execute("SELECT COUNT(*) FROM contract WHERE policyholder_name IS NOT NULL OR insured_name IS NOT NULL").fetchone()[0],
            "no_agent": con.execute("SELECT COUNT(*) FROM contract WHERE agent_id IS NULL").fetchone()[0],
            "issues": {f"{r[0]}/{r[1]}": r[2] for r in con.execute(
                "SELECT severity, check_code, COUNT(*) FROM quality_issue WHERE batch_id=? GROUP BY severity, check_code", (s["batch_id"],))}}


def build(out, src, months_path, heads_path=None, password=None, month_sum=False, contracts=None) -> dict:
    """대조용 DB 를 만든다. 돌려주는 것은 건수와 파일 이름뿐인 보고다.
    contracts 는 실제 계약현황 파일이다. 있으면 월 합계 대표 계약과 월 설정은 쓰지 않고 실적 표(agent_period_perf)에는 아무것도 넣지 않는다."""
    out, src, months_path = pathlib.Path(out), pathlib.Path(src), pathlib.Path(months_path)
    contracts = pathlib.Path(contracts) if contracts else None
    if contracts and not contracts.is_file():
        raise SystemExit(f"계약현황 파일이 없습니다: {contracts.name}")
    if not password:
        raise SystemExit("환경 변수 TRIAL_PASSWORD 가 없습니다")
    from app.web.invite import password_error

    err = password_error(password, password, "admin")
    if err:
        raise SystemExit(f"TRIAL_PASSWORD 가 비밀번호 규칙에 맞지 않습니다: {err}")
    _guard_out(out)
    report = {"notes": [], "blocked_files": [], "duplicates": [], "months": {}}
    cpeople = None
    if contracts:
        if month_sum:
            report["notes"].append("--contracts 가 있어 --month-sum-contracts 는 쓰지 않습니다")
            month_sum = False
        try:
            cpeople = ec.read_people(contracts, contract_ym(contracts))     # DB 에 쓰기 전에 머리글을 검사한다. 맞지 않으면 HeaderMismatch
        except HeaderMismatch as e:
            raise SystemExit(f"계약현황 머리글이 매핑과 달라 만들지 않았습니다: {' / '.join(e.reasons)}") from e

    # 1. 파일 읽기 (DB 에 쓰기 전)
    files = sorted(p for p in src.glob("*.xlsx") if tp.kind_of(p))
    reads = []
    for p in files:
        try:
            reads.append(tp.read_file(p))
        except HeaderMismatch as e:
            report["blocked_files"].append((p.name, " / ".join(e.reasons)))
    for r in reads:
        if r["blocked"]:
            report["blocked_files"].append((r["file"], " / ".join(msg for sev, code, msg, _row in r["issues"] if sev == "block" and code == "multi_month")))
    reads = [r for r in reads if not r["blocked"]]
    reads, report["duplicates"] = tp.dedupe(reads)
    if contracts:
        months, notes = {f.name: None for f in files}, ["--contracts 가 있어 월 설정과 실적 합계 파일의 월 실적은 쓰지 않습니다. 사람과 조직만 읽습니다"]
    else:
        months, notes = read_months(months_path, [p.name for p in files])
    report["notes"] += notes
    # 본문이 같아 버린 파일에 달이 있으면 남긴 파일의 달로 옮긴다. 둘의 달이 다르면 그 묶음은 넣지 않는다.
    month_of = {r["file"]: months.get(r["file"]) for r in reads}
    for dropped, kept in report["duplicates"]:
        d, k = months.get(dropped), month_of.get(kept)
        if d and not k:
            month_of[kept] = d
        elif d and k and d != k:
            report["notes"].append(f"본문이 같은 두 파일의 달이 다릅니다: {kept}, {dropped}. 그 파일의 실적은 넣지 않습니다")
            month_of[kept] = None

    # 2. 스키마와 마스터
    con = db.init_db(out)
    con.execute(f"PRAGMA user_version={TRIAL_MARK}")
    con.executemany("INSERT INTO product_category(code, name, incentive_eligible, sort_order) VALUES(?,?,?,?)",
                    [(c, n, e, i) for i, (c, n, e) in enumerate(masters.CATEGORIES)])
    masters.seed_real_insurers(con)

    # 3. 조직, 사람, 조직장, 계정
    dated = sorted(v for v in month_of.values() if v)
    from_ym = dated[0] if dated else FROM_YM
    people = tp.pick_people(reads, month_of)
    if cpeople is not None:
        people, report["contract_people"] = tp.merge_contract_people(people, cpeople)
    built = tp.build_people(con, people, from_ym)
    report["people"] = len(built["agents"])
    report["people_stats"] = built["stats"]
    heads_cfg = yaml.safe_load(pathlib.Path(heads_path).read_text(encoding="utf-8")) if heads_path and pathlib.Path(heads_path).exists() else None
    report["heads"] = assign_heads(con, heads_cfg, people, built["agents"], from_ym) if heads_cfg else "조직장 파일 없음"
    report["accounts"] = trial_accounts(con, password)

    # 4. 프로모션
    branch = [r[0] for r in con.execute("SELECT path_key FROM org_unit WHERE level=?", (tp.load_map(tp.RANK)["org_levels"]["org_general"],))]
    report["promotions"] = len(register_promotions(con, branch[0] if len(branch) == 1 else None, contracts=bool(contracts)))

    # 5. 달마다 실적
    by_month = {}
    for r in reads:
        ym = month_of.get(r["file"])
        if ym:
            by_month.setdefault(ym, {}).setdefault(r["kind"], []).append(r)
    report["unassigned_files"] = sorted(r["file"] for r in reads if not month_of.get(r["file"]))
    for ym in sorted(by_month):
        g = by_month[ym]
        if any(len(v) > 1 for v in g.values()):
            report["months"][ym] = "같은 달에 같은 양식의 파일이 둘 이상이라 넣지 않았습니다"
            continue
        rank, ins = (g.get(tp.RANK) or [None])[0], (g.get(tp.INSURER) or [None])[0]
        res = tp.load_month(con, ym, rank=rank, ins=ins, agents=built["agents"], by=BY)
        line = {k: v for k, v in res.items() if k not in ("perf_ids",)}
        if month_sum and ins:
            line["month_sum"] = tp.month_sum_contracts(con, ym, ins, built["agents"], res["insurer_batch"])
        report["months"][ym] = line

    # 5b. 계약현황의 계약 단위 자료
    if contracts:
        report["contract_load"] = load_contracts(con, contracts, built["agents"])

    # 6. 건수
    report["orgs"] = con.execute("SELECT COUNT(*) FROM org_unit").fetchone()[0]
    report["orgs_by_level"] = {levels.level_ko().get(r[0], r[0]): r[1] for r in con.execute("SELECT level, COUNT(*) FROM org_unit GROUP BY level ORDER BY level")}
    report["contracts"] = con.execute("SELECT COUNT(*) FROM contract").fetchone()[0]
    con.close()
    return report


def _print(report: dict):
    for n in report["notes"]:
        print(f"알림: {n}")
    for f, why in report["blocked_files"]:
        print(f"막은 파일: {f}: {why}")
    for d, k in report["duplicates"]:
        print(f"본문이 같아 건너뛴 파일: {d} (남긴 파일 {k})")
    print(f"조직 {report['orgs']}곳: " + ", ".join(f"{k} {v}" for k, v in report["orgs_by_level"].items()))
    s = report["people_stats"]
    print(f"설계사 {report['people']}명. 여러 조직 경로로 나온 사람 {s.get('multi_path', 0)}, 지점이 비어 위 조직에 붙은 사람 {s.get('short_path', 0)}, "
          f"중간 계층이 빈 사람 {s.get('gap', 0)}")
    h = report["heads"]
    if isinstance(h, dict):
        print(f"조직장 지정: 대상 {h['wanted']}, 지정 {h['assigned']}, 이름이 없음 {h['no_match']}, 이름이 둘 이상 {h['many_match']}, "
              f"조직을 못 찾음 {h['no_org']}, 그 조직 밖의 사람 {h['outside']}")
    else:
        print(f"조직장 지정: {h}")
    print(f"계정 {report['accounts']}개 (admin 과 T 번호)")
    print(f"프로모션 {report['promotions']}개")
    for ym, line in report["months"].items():
        if isinstance(line, str):
            print(f"{ym}: {line}")
            continue
        print(f"{ym}: 사람 {line['people']}, 인별업적순위 {line.get('from_rank', 0)}, 보험사별만 {line.get('from_insurer_only', 0)}, "
              f"보험사 줄 {line.get('insurer_rows', 0)}, 합계 대조 {'맞음' if line['sums_match'] else '틀림'}, "
              f"누적과 보험사별 건수가 다른 사람 {line.get('rank_vs_insurer_count_diff', 0)}, 마스터에 없는 보험사 {len(line['unmapped_insurers'])}"
              + (f", 월 합계 계약 {line['month_sum']['contracts']}" if line.get("month_sum") else ""))
    if report["unassigned_files"]:
        print(f"달이 없어 실적을 넣지 않은 파일 {len(report['unassigned_files'])}개: " + ", ".join(report["unassigned_files"]))
    cp = report.get("contract_people")
    if cp is not None:
        print(f"계약현황 모집사원: 실적 파일에도 있는 사람 {cp.get('both', 0)}, 계약현황에만 있는 사람 {cp.get('only_contract', 0)}, "
              f"실적 파일에만 있는 사람 {cp.get('only_rank', 0)}, 조직이 달라져 옮긴 사람 {cp.get('org_moved', 0)}")
    cl = report.get("contract_load")
    if cl:
        print(f"계약현황 적재({cl['mapping_version']}): 행 {cl['file_rows']}, 넣은 계약 {cl['loaded']}, 막힌 행 {cl['skipped']}, 대조 {'맞음' if cl['sums_ok'] else '틀림'}, "
              f"상태별 " + ", ".join(f"{k} {v}" for k, v in cl["by_status"].items()) + f", 버린 민감 열 {cl['dropped_columns']}, "
              f"이름이 남은 계약 {cl['blank_names']}, 설계사가 빈 계약 {cl['no_agent']}")
        print("업적 집계를 돌린 달: " + ", ".join(f"{ym} ({n}줄)" for ym, n in cl["perf_runs"].items()))
        print("품질 기록: " + ", ".join(f"{k} {v}" for k, v in sorted(cl["issues"].items())))
    print(f"계약 원장 {report['contracts']}건")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(DEFAULTS["out"]), help="만들 DB 경로. 저장소 안이면 data/real/ 아래만")
    ap.add_argument("--src", default=str(DEFAULTS["src"]), help="원본 폴더")
    ap.add_argument("--months", default=str(DEFAULTS["months"]), help="파일별 월 설정 YAML")
    ap.add_argument("--heads", default=str(DEFAULTS["heads"]), help="조직장 이름 YAML. 없으면 지정하지 않는다")
    ap.add_argument("--contracts", default=None, help="실제 계약현황 파일(56열 또는 58열). 있으면 월 합계 대표 계약과 월 설정은 쓰지 않는다")
    ap.add_argument("--month-sum-contracts", action="store_true", help="보험사별 월 합계를 대표 계약 1건으로 계약 원장에 넣는다 (대조용 추정)")
    args = ap.parse_args(argv)
    report = build(args.out, args.src, args.months, args.heads, os.environ.get("TRIAL_PASSWORD"), args.month_sum_contracts, args.contracts)
    _print(report)
    print(f"만든 DB: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
