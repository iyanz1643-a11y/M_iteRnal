"""프로모션: 게시, 구간 입력, 달성 현황 계산, 비교, 지난 프로모션.

프로모션 하나 = 기간 창(하나 이상) + 구간표 + 결합 규칙 + 보상 + 조건.
  - rate      정률 시책. 대상 계약의 보험료에 지급률을 곱한 예상액
  - weekly    주차 시상. 창 하나의 실적을 구간표에 대입
  - linked    연계 시상. 창마다 달성해야 하고 낮은 쪽 구간을 적용 (combine_rule = all)
  - monthly / membership / travel / activity  누적 실적을 구간표에 대입
달성 현황은 저장하지 않고 계약 원장에서 그때그때 계산한다. 모두 예상이며 확정은 보험사 정산을 따른다.
비교(compare)는 진행 중 프로모션을 실적 10만 원당 예상 보상, 구간, 조건으로 나란히 놓는다. 지난 프로모션(past)은 끝난
프로모션의 사람별 최종 결과를 progress() 로 낸다. 메인 관리자가 confirm_results() 로 확정하면 그 뒤에는 확정한 값을 낸다.

실적 기준 (D-08, 열린 결정이라 제안대로 둔 가정).
- 프로모션은 보험사 규칙대로 기간 안 계약 가운데 성립한 것을 세고 철회, 청약, 반송은 세지 않는다. 지금 상태로 센다.
  업적 집계(성립한 달에 더하고 철회한 달에 뺀다)와 다를 수 있다. 화면은 progress() 의 basis 글을 그대로 보여 준다.
- 기준일은 프로모션마다 고른다. 새로 만들 때의 기본값은 영수일자(DEFAULT_DATE_BASIS)다. 영수일자가 빈 계약은 계약일자로 센다.
  실제 계약현황의 '영수일자' 열은 contract.receipt_date 에 실린다 (config/mapping/eternal_contract.yaml). 샘플 계약에는 영수일자가 없다.

원안과 배포안 (D-21, 열린 결정이라 제안대로 둔 가정). 지금 금액(reward_value, rate)은 배포안이다. 원안은 promotion_tier.original_value 와
promotion_rate.original_rate 에 메인 관리자가 넣은 것만 있고, 없으면 원문 확인이다. 원안을 보여 줄지는 화면이 역할로 정한다.

S3 확정 기능. 아래 규칙은 열린 결정이 아니면 스스로 정한 것이고, 돈에 닿는 것은 제안대로 둔 가정이다.
- 창별 구간표: 창마다 달성하는 결합 규칙(all)에서 창마다 다른 문턱을 둔다. window_tiers={창 순번: 구간표}. 구간 수는 모든 표가 같다.
  창마다 닿은 구간 번호 가운데 낮은 쪽을 적용하고, 보상은 공통 구간표에서 읽는다. 공통 구간표가 없으면 마지막 창의 구간표에서 읽는다 (가정).
  창이 하나면 창별 구간표는 공통 구간표로 접는다. 합산(sum)에는 창별 구간표를 두지 않는다. 기존 프로모션은 모두 공통 구간표라 그대로 돈다.
- 확정 순위: 확정할 때의 재직자 전체에서 실적 큰 순이다. 같은 값은 같은 순위이고 다음 사람은 그만큼 밀린다(D-06 과 같은 꼴). 실적이 0 이하면 순위가 없다.
  범위와 상관없이 같은 사람은 같은 순위다. 확정 전에도 같은 규칙으로 계산한 예상 순위를 낸다.
- 여행 시상 명단 (D-20 제안대로 둔 가정): 달성자 명단은 본인, 직속 관리자, 메인 관리자만 본다. travel_roster() 가 범위 안 설계사 목록을 받아
  그 가운데 이 사람이 볼 수 있는 사람만 남긴다. 직속 관리자는 D-05 와 같은 뜻이다 (orgs.direct_heads).
- 13회차 확정: 결과 확정 때 센 계약마다 persist.inst13_judge 로 유지, 실효, 판정 전을 가른다. 실효 계약을 빼고 확정 때의 규칙으로 다시 계산해
  구간이나 금액이 줄면 환수, 그대로면 지급이다. 판정 전 계약이 하나라도 있으면 그 사람은 판정 전이다. 보상 전체를 13회차 유지에 거는 것으로 본다 (가정).
  confirm_inst13() 은 지급과 환수가 정해진 사람만 남기고, 남긴 줄은 바뀌지 않는다. 판정 전인 사람은 다음에 다시 확정한다.
- 확정 무르기 (D-50 제안대로 둔 가정): 메인 관리자가 사유를 5자 이상 적고 무른다. 확정 줄은 지우지 않고 voided_at, voided_by, void_reason 을 채운다.
  무른 뒤에는 다시 계산한 마감 전 예상을 보이고 다시 확정할 수 있다. 무른 확정에 달린 13회차 판정은 함께 보이지 않는다.

조건 확장 (11월 17일). 값은 promotion 의 [promotion_rules] 칸이고 p["rules"] 로 읽는다. 칸이 비면 예전 동작 그대로다.
- 세는 기준 count_basis: issued(성립 기준, 기본) 또는 application(청약 기준). 청약 기준은 청약일로 세고 철회는 뺀다(대표님 확인).
  반송(declined)과 해지(cancelled)도 빼는 것은 '취소 제외'를 옮긴 가정이다 (D-85).
- 결합 규칙 both, either: 조건 1(창마다 창별 구간표 달성, all 과 같다)과 조건 2(창 합계를 공통 구간표에 대입)를 묶는다.
  both 는 둘 다(낮은 쪽 구간), either 는 하나만(높은 쪽 구간) 맞으면 된다. 창별 구간표와 공통 구간표가 모두 있어야 한다.
- 가중 인정 boost: 매월 날짜 구간(month_days) 또는 날짜 목록(ranges)에 든 계약의 실적을 factor 배로 인정한다.
  cap 은 배수를 적용할 금액의 상한이고 프로모션 실적 지표로 잰다. 넘는 금액은 1배로 센다. cap_scope 는 contract(건마다, 기본)
  또는 range(사람마다 그 가중 구간의 합)다 (D-84 가정). applies_to 가 each 면 조건 1(창 실적)에만 배수를 쓰고 합계는 원래 값이다.
- 상품 이름 product_include, product_exclude: 상품 이름(상품 마스터 이름, 없으면 원장의 상품명 원문)에 낱말이 드는지 본다.
  공백과 대소문자는 무시한다. 목록이 있는데 이름이 빈 계약은 세지 않고 progress() 의 unnamed 로 따로 센다 (D-86 가정).
- 본인 계약 제외 exclude_self: 원장의 본인·가족계약 칸(self_family)이 1 인 계약을 뺀다. 가족 계약도 함께 빠진다 (D-87 가정).
- 빼는 상태 drop_statuses: 세는 상태 가운데 더 뺄 것. 13회 이내 품보는 원장 상태 해지(cancelled)로 본다 (D-92 가정).
- 딸린 시책 parent_promo_id: 다른 프로모션(부모)의 달성자에게 붙는 시책이다. 실적과 구간은 부모의 것을 그대로 쓰고,
  대상 직급(target_levels, 커리어 직급 코드. D-89 가정)과 조직 달성 인원 문턱(gate_min_achievers, gate_org_path)을 더 본다.
  문턱은 그 조직 재직자 전체의 부모 달성 인원으로 센다. 범위와 상관없이 같은 숫자이고 사람은 내지 않는다 (D-88 가정).
- 13회차 유지율 문턱 retention_min, retention_ref: 표시만 한다. retention() 이 사람마다 센 계약의 유지율과 문턱 충족을 낸다 (D-90 가정).
- 조직별 진척 org_progress(): 조직 하나 바로 아래 조직마다 대상, 가동, 달성, 근접, 미가동 인원과 달성 인원 순위를 낸다.
  사람별 값은 progress() 를 그대로 합친다. 금액 합계는 amounts 에 따로 둔다.
"""
import calendar
import json
import sqlite3
from datetime import date

from app import db
from app.engine import classify, orgs, persist

KIND_KO = {"rate": "정률 시책", "weekly": "주차 시상", "linked": "연계 시상", "monthly": "월 구간 시상",
           "travel": "여행 시상", "activity": "가동 시상", "membership": "멤버십"}
METRIC_KO = {"premium_monthly": "보험료", "converted_premium": "인정보험료", "policy_count": "건수"}
METRIC_UNIT = {"premium_monthly": "원", "converted_premium": "원", "policy_count": "건"}
BASIS_KO = {"receipt_date": "영수일자", "contract_date": "계약일자", "application_date": "청약일자"}
DEFAULT_DATE_BASIS = "receipt_date"                          # D-08 제안. 새 프로모션의 기본값
BASIS_FALLBACK = {"receipt_date": "contract_date"}           # 그 날짜가 빈 계약은 이 날짜로 센다
COMBINE_KO = {"single": "창 하나", "all": "창마다 달성", "sum": "창을 합산",
              "both": "창마다 달성과 합산을 모두", "either": "창마다 달성과 합산 가운데 하나"}
PAIRED = ("both", "either")                                   # 조건 1(창마다 달성)과 조건 2(합산)를 묶는 결합
EACH_RULES = ("all",) + PAIRED                                # 창마다 구간을 보는 결합
COUNT_BASIS_KO = {"issued": "성립 기준", "application": "청약 기준"}
APPLIED_COUNTED = ("applied", "inforce", "lapsed", "matured")  # 청약 기준에서 세는 상태. 철회, 반송, 해지는 뺀다 (D-85)
DROPPABLE = ("applied", "inforce", "lapsed", "cancelled", "matured")
NAME_SQL = "COALESCE(pr.product_name, c.product_code_raw)"      # 상품 이름. 뷰 v_contract_extract 의 '상품' 과 같은 식
RULE_DEFAULTS = {"count_basis": "issued", "boost": None, "product_include": [], "product_exclude": [], "exclude_self": False,
                 "drop_statuses": [], "parent_promo_id": None, "target_levels": [], "gate_min_achievers": None, "gate_org_path": None,
                 "retention_min": None, "retention_ref": None, "retention_basis": "count"}
REWARD_KO = {"fixed": "정액", "rate": "실적의 비율", "travel": "여행", "goods": "물품"}
TIMING_KO = {"next_month": "익월", "inst13": "13회차", "next_month+inst13": "익월과 13회차", "year1": "1차년", "year2": "2차년",
             "mixed": "혼합", "unspecified": "표기 없음"}
NO_TIMING = "지급 시점 표기 없음"
COUNTED = ("inforce", "lapsed", "cancelled", "matured")       # 성립한 계약. 청약, 반송, 철회는 세지 않는다
FROM_LINE_KIND = {"rate_promo": "rate", "activity": "activity", "consecutive": "linked", "tier": "monthly"}
IN_KIND = ("travel", "goods")                                 # 보상이 물품이나 행사인 구간


def ref_date(ym: str) -> date:
    """기준일. 기준 월이 이번 달이면 오늘, 지난 달이면 그 달의 말일이다."""
    today = date.today()
    if today.strftime("%Y%m") == ym:
        return today
    y, m = int(ym[:4]), int(ym[4:])
    return date(y, m, calendar.monthrange(y, m)[1])


def timing_ko(code) -> str | None:
    """지급 시점 코드의 한국어. 비었거나 표기 없음(unspecified)이면 None 이다. 모르는 코드는 원문 그대로 둔다."""
    if not code or code == "unspecified":
        return None
    return TIMING_KO.get(code, code)


def timing_text(code) -> str:
    """'익월 지급' 처럼 붙여 쓰는 글. 표기가 없으면 '지급 시점 표기 없음' 이다."""
    ko = timing_ko(code)
    return f"{ko} 지급" if ko else NO_TIMING


def basis_note(p) -> str:
    """달성 현황의 실적 기준 설명 (D-08 가정). 화면이 그대로 보여 준다."""
    b = p["date_basis"]
    head = f"기준: {BASIS_KO[b]}"
    if b in BASIS_FALLBACK:
        head += f", {BASIS_KO[b]}가 없는 계약은 {BASIS_KO[BASIS_FALLBACK[b]]}"
    if _r(p)["count_basis"] == "application":
        return f"{head}. 기간 안 청약을 세고 철회, 반송, 해지는 뺍니다. 보험사 규칙대로 세어 업적 집계와 다를 수 있습니다."
    return f"{head}. 기간 안 성립 계약만 세고 철회는 뺍니다. 보험사 규칙대로 세어 업적 집계와 다를 수 있습니다."


def _r(p) -> dict:
    """프로모션의 조건 확장 값. 칸이 없는 오래된 DB 나 손으로 만든 dict 는 기본값이다."""
    return p.get("rules") or RULE_DEFAULTS


def _date_expr(basis: str) -> str:
    if basis not in BASIS_KO:
        raise ValueError(basis)
    if basis in BASIS_FALLBACK:
        return f"COALESCE(c.{basis}, c.{BASIS_FALLBACK[basis]})"
    return f"c.{basis}"


# ---------------------------------------------------------------- 등록과 게시
def _clean_tiers(tiers) -> list:
    return sorted((float(m), k, (float(v) if v not in (None, "") else None), (lab or None)) for m, k, v, lab in tiers)


def _normalize(kind, windows, tiers, combine_rule, metric, date_basis, categories, window_tiers=None):
    """등록과 고치기가 함께 쓰는 검사. window_tiers 는 {창 순번(1부터): 구간표}다. 돌려줄 때 비지 않은 표만 남긴다."""
    if kind not in KIND_KO or metric not in METRIC_KO or date_basis not in BASIS_KO:
        raise ValueError("유형, 지표, 기준일 값이 맞지 않습니다.")
    windows = [(l, f, t) for l, f, t in windows if f and t]
    if not windows:
        raise ValueError("기간 창이 하나 이상 있어야 합니다.")
    for l, f, t in windows:
        if date.fromisoformat(f) > date.fromisoformat(t):
            raise ValueError(f"기간 창 '{l}' 의 시작일이 종료일보다 늦습니다.")
    combine_rule = combine_rule or ("all" if kind == "linked" else ("sum" if len(windows) > 1 else "single"))
    if combine_rule not in COMBINE_KO or (combine_rule == "single" and len(windows) > 1):
        raise ValueError("창이 둘 이상이면 결합 규칙을 정해야 합니다.")
    if combine_rule in PAIRED:
        if len(windows) < 2 or kind == "rate":
            raise ValueError("조건을 묶는 결합은 창이 둘 이상인 구간 시상에서만 씁니다.")
        if not tiers or len([s for s, rows in (window_tiers or {}).items() if rows]) != len(windows):
            raise ValueError("조건을 묶는 결합은 창마다 구간표(조건 1)와 합산 구간표(조건 2)가 모두 있어야 합니다.")
    if len(windows) == 1:
        combine_rule = "single"
    tiers = _clean_tiers(tiers)
    wt = {}
    for seq, rows in (window_tiers or {}).items():
        if not 1 <= int(seq) <= len(windows):
            raise ValueError(f"없는 창의 구간표입니다: {seq}")
        rows = _clean_tiers(rows)
        if rows:
            wt[int(seq)] = rows
    if wt:
        if kind == "rate":
            raise ValueError("정률 시책에는 구간표를 두지 않습니다.")
        if len(windows) == 1:                                         # 창이 하나면 그 창의 구간표가 곧 공통 구간표다
            if tiers:
                raise ValueError("창이 하나면 구간표도 하나만 둡니다.")
            tiers, wt = wt[1], {}
        elif combine_rule not in EACH_RULES:
            raise ValueError("창별 구간표는 창마다 달성하는 결합 규칙에서만 씁니다.")
        elif not tiers and len(wt) != len(windows):
            raise ValueError("공통 구간표가 없으면 창마다 구간표가 있어야 합니다.")
        elif len({len(t) for t in [tiers, *wt.values()] if t}) != 1:
            raise ValueError("창별 구간표와 공통 구간표의 구간 수가 같아야 합니다.")
    if kind != "rate" and not tiers and not wt:
        raise ValueError("구간표가 비어 있습니다.")
    for table in [tiers, *wt.values()]:
        if any(k not in REWARD_KO for _, k, _, _ in table) or len({m for m, *_ in table}) != len(table):
            raise ValueError("구간의 보상 종류가 맞지 않거나 기준값이 겹칩니다.")
    if isinstance(categories, (list, tuple, set)):
        categories = ",".join(sorted(categories)) or None
    return windows, tiers, combine_rule, (categories or None), wt


def _write_children(con, pid, windows, tiers, window_tiers=None):
    for i, (l, f, t) in enumerate(windows, start=1):
        con.execute("INSERT INTO promotion_window(promo_id, seq, label, date_from, date_to) VALUES(?,?,?,?,?)", (pid, i, l or f"{i}구간", f, t))
    for i, (m, k, v, lab) in enumerate(tiers, start=1):
        con.execute("INSERT INTO promotion_tier(promo_id, tier_no, min_value, reward_kind, reward_value, reward_label) VALUES(?,?,?,?,?,?)", (pid, i, m, k, v, lab))
    for seq, rows in sorted((window_tiers or {}).items()):              # 창별 구간표. 칸이 없는 오래된 DB 는 부르는 쪽이 ensure() 를 먼저 돌린다
        for i, (m, k, v, lab) in enumerate(rows, start=1):
            con.execute("INSERT INTO promotion_tier(promo_id, window_seq, tier_no, min_value, reward_kind, reward_value, reward_label) VALUES(?,?,?,?,?,?,?)",
                        (pid, seq, i, m, k, v, lab))


# ---------------------------------------------------------------- 조건 확장 (모듈 설명의 '조건 확장')
RULE_COLS = ("count_basis", "boost_json", "product_include", "product_exclude", "exclude_self", "drop_statuses", "parent_promo_id",
             "target_levels", "gate_min_achievers", "gate_org_path", "retention_min", "retention_ref", "retention_basis")


def _names(v) -> list:
    """상품 이름 목록. 목록이거나 줄바꿈으로 나눈 글을 받는다. 빈 것과 겹치는 것을 뺀다."""
    if not v:
        return []
    items = v.splitlines() if isinstance(v, str) else list(v)
    out = []
    for x in (str(i).strip() for i in items):
        if x and x not in out:
            out.append(x)
    return out


def _norm_name(s) -> str:
    return "".join(str(s or "").split()).casefold()


def _clean_boost(b, metric, combine_rule):
    if not b:
        return None
    if metric == "policy_count":
        raise ValueError("건수 기준에는 가중 인정을 두지 않습니다.")
    factor = float(b.get("factor") or 0)
    if factor <= 1:
        raise ValueError("가중 배수는 1보다 커야 합니다.")
    md, rg = b.get("month_days"), b.get("ranges")
    if bool(md) == bool(rg):
        raise ValueError("가중 기간은 매월 날짜 구간이나 날짜 목록 가운데 하나로 넣습니다.")
    out = {"factor": factor, "month_days": None, "ranges": None}
    if md:
        a, z = int(md[0]), int(md[1])
        if not 1 <= a <= z <= 31:
            raise ValueError("매월 날짜 구간이 맞지 않습니다.")
        out["month_days"] = [a, z]
    else:
        out["ranges"] = []
        for f, t in rg:
            if date.fromisoformat(f) > date.fromisoformat(t):
                raise ValueError("가중 기간의 시작일이 종료일보다 늦습니다.")
            out["ranges"].append([f, t])
    cap = b.get("cap")
    out["cap"] = None if cap in (None, "") else float(cap)
    if out["cap"] is not None and out["cap"] <= 0:
        raise ValueError("가중 인정 한도는 0보다 커야 합니다.")
    out["cap_scope"] = b.get("cap_scope") or "contract"
    if out["cap_scope"] not in ("contract", "range"):
        raise ValueError("가중 인정 한도의 범위가 맞지 않습니다.")
    out["applies_to"] = b.get("applies_to") or "all"
    if out["applies_to"] not in ("all", "each") or (out["applies_to"] == "each" and combine_rule not in EACH_RULES):
        raise ValueError("조건 1에만 쓰는 가중 인정은 창마다 달성하는 결합에서만 씁니다.")
    return out


def _clean_rules(con, rules, *, kind, metric, date_basis, combine_rule, promo_id=None) -> dict:
    """조건 확장 값을 검사해 RULE_DEFAULTS 꼴로 돌려준다. 준 키만 바꾸고 나머지는 기본값이다."""
    unknown = set(rules or {}) - set(RULE_DEFAULTS)
    if unknown:
        raise ValueError(f"모르는 조건입니다: {', '.join(sorted(unknown))}")
    r = {**RULE_DEFAULTS, **(rules or {})}
    if r["count_basis"] not in COUNT_BASIS_KO:
        raise ValueError("세는 기준이 맞지 않습니다.")
    if r["count_basis"] == "application" and date_basis != "application_date":
        raise ValueError("청약 기준으로 세면 기준일도 청약일자여야 합니다.")
    r["boost"] = _clean_boost(r["boost"], metric, combine_rule)
    r["product_include"], r["product_exclude"] = _names(r["product_include"]), _names(r["product_exclude"])
    r["exclude_self"] = bool(r["exclude_self"])
    r["drop_statuses"] = sorted(set(r["drop_statuses"] or ()))
    if set(r["drop_statuses"]) - set(DROPPABLE):
        raise ValueError("빼는 상태 값이 맞지 않습니다.")
    r["target_levels"] = sorted({str(x).strip().upper() for x in (r["target_levels"] or ()) if str(x).strip()})
    if r["parent_promo_id"] is not None:
        r["parent_promo_id"] = int(r["parent_promo_id"])
        par = con.execute("SELECT * FROM promotion WHERE promo_id=?", (r["parent_promo_id"],)).fetchone()
        if not par or r["parent_promo_id"] == promo_id:
            raise ValueError("딸린 시책의 부모 프로모션이 없습니다.")
        if "parent_promo_id" in par.keys() and par["parent_promo_id"]:
            raise ValueError("딸린 시책에 다시 딸린 시책은 두지 않습니다.")
        if kind == "rate" or par["kind"] == "rate":
            raise ValueError("정률 시책에는 딸린 시책을 두지 않습니다.")
    elif r["target_levels"] or r["gate_min_achievers"]:
        raise ValueError("대상 직급과 조직 달성 인원 문턱은 딸린 시책에서만 씁니다.")
    if r["gate_min_achievers"] not in (None, ""):
        r["gate_min_achievers"] = int(r["gate_min_achievers"])
        if r["gate_min_achievers"] < 1:
            raise ValueError("조직 달성 인원 문턱은 1명 이상입니다.")
    else:
        r["gate_min_achievers"] = None
    if r["gate_org_path"] and not con.execute("SELECT 1 FROM org_unit WHERE path_key=?", (r["gate_org_path"],)).fetchone():
        raise ValueError("문턱을 셀 조직이 없습니다.")
    r["gate_org_path"] = r["gate_org_path"] or None
    if r["retention_min"] not in (None, ""):
        v = float(r["retention_min"])
        v = v / 100 if v > 1 else v                              # 95 와 0.95 를 같게 받는다
        if not 0 < v <= 1:
            raise ValueError("13회차 유지율 문턱이 맞지 않습니다.")
        r["retention_min"] = v
    else:
        r["retention_min"] = None
    if r["retention_ref"]:
        date.fromisoformat(r["retention_ref"])
    r["retention_ref"] = r["retention_ref"] or None
    if r["retention_basis"] not in ("count", "amount"):
        raise ValueError("유지율 기준이 맞지 않습니다.")
    return r


def _rules_row(r) -> tuple:
    return (r["count_basis"], json.dumps(r["boost"]) if r["boost"] else None,
            json.dumps(r["product_include"], ensure_ascii=False) if r["product_include"] else None,
            json.dumps(r["product_exclude"], ensure_ascii=False) if r["product_exclude"] else None,
            1 if r["exclude_self"] else 0, ",".join(r["drop_statuses"]) or None, r["parent_promo_id"], ",".join(r["target_levels"]) or None,
            r["gate_min_achievers"], r["gate_org_path"], r["retention_min"], r["retention_ref"], r["retention_basis"])


def _write_rules(con, pid, r) -> None:
    con.execute(f"UPDATE promotion SET {', '.join(f'{c}=?' for c in RULE_COLS)} WHERE promo_id=?", (*_rules_row(r), pid))


def _read_rules(row: dict) -> dict:
    if "count_basis" not in row:
        return dict(RULE_DEFAULTS)
    return {"count_basis": row["count_basis"] or "issued", "boost": json.loads(row["boost_json"]) if row["boost_json"] else None,
            "product_include": json.loads(row["product_include"]) if row["product_include"] else [],
            "product_exclude": json.loads(row["product_exclude"]) if row["product_exclude"] else [],
            "exclude_self": bool(row["exclude_self"]), "drop_statuses": row["drop_statuses"].split(",") if row["drop_statuses"] else [],
            "parent_promo_id": row["parent_promo_id"], "target_levels": row["target_levels"].split(",") if row["target_levels"] else [],
            "gate_min_achievers": row["gate_min_achievers"], "gate_org_path": row["gate_org_path"],
            "retention_min": row["retention_min"], "retention_ref": row["retention_ref"], "retention_basis": row["retention_basis"] or "count"}


def create(con, *, period_ym, name, kind, windows, tiers=(), rates=(), by, recipient_level="fc", insurer_id=None, insurer_name=None,
           sector=None, categories=None, target_note=None, metric="premium_monthly", date_basis=DEFAULT_DATE_BASIS, per_contract_cap=None,
           combine_rule=None, pay_timing=None, caution=None, source_line_id=None, publish=False, window_tiers=None, inst13_required=False,
           rules=None) -> int:
    """windows: [(label, from, to)], tiers: [(min_value, reward_kind, reward_value, reward_label)], rates: [(option, basis, rate, timing)]
    window_tiers: {창 순번: [(min_value, reward_kind, reward_value, reward_label)]} 창별 구간표. 모듈 설명의 규칙이다.
    inst13_required: 13회차 유지 계약만 인정하는 조건. 지급 시점에 inst13 이 들면 이 값과 상관없이 13회차 조건이 있다.
    rules: 조건 확장 {count_basis, boost, product_include, product_exclude, exclude_self, drop_statuses, parent_promo_id,
           target_levels, gate_min_achievers, gate_org_path, retention_min, retention_ref, retention_basis}. 준 키만 쓴다."""
    windows, tiers, combine_rule, categories, wt = _normalize(kind, windows, tiers, combine_rule, metric, date_basis, categories, window_tiers)
    clean = _clean_rules(con, rules, kind=kind, metric=metric, date_basis=date_basis, combine_rule=combine_rule) if rules else None
    if wt or inst13_required or clean:
        ensure(con)
    cur = con.execute(
        """INSERT INTO promotion(period_ym, name, kind, recipient_level, insurer_id, insurer_name, sector, categories, target_note, metric, date_basis,
                                 per_contract_cap, combine_rule, pay_timing, caution, source_line_id, status, created_by, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)""",
        (period_ym, name.strip(), kind, recipient_level, insurer_id, insurer_name, sector or None, categories, target_note, metric, date_basis,
         per_contract_cap or None, combine_rule, pay_timing, caution, source_line_id, by, db.now()))
    pid = cur.lastrowid
    _write_children(con, pid, windows, tiers, wt)
    if inst13_required:
        con.execute("UPDATE promotion SET inst13_required=1 WHERE promo_id=?", (pid,))
    if clean:
        _write_rules(con, pid, clean)
    for i, (opt, basis, rate, timing) in enumerate(rates, start=1):
        con.execute("INSERT INTO promotion_rate(promo_id, seq, option_label, basis, rate, pay_timing) VALUES(?,?,?,?,?,?)", (pid, i, opt, basis, rate, timing))
    con.commit()
    if publish:
        set_status(con, pid, "published", by)
    return pid


def _snapshot(p):
    return {"name": p["name"], "kind": p["kind"], "insurer_name": p["insurer_name"], "sector": p["sector"], "categories": p["categories"],
            "metric": p["metric"], "date_basis": p["date_basis"], "per_contract_cap": p["per_contract_cap"], "combine_rule": p["combine_rule"],
            "pay_timing": p["pay_timing"], "target_note": p["target_note"], "caution": p["caution"],
            "windows": [(w["label"], w["date_from"], w["date_to"]) for w in p["windows"]],
            "tiers": [(t["min_value"], t["reward_kind"], t["reward_value"], t["reward_label"]) for t in p["tiers"] if t.get("window_seq") is None],
            "window_tiers": {str(seq): [(t["min_value"], t["reward_kind"], t["reward_value"], t["reward_label"]) for t in rows]
                             for seq, rows in sorted(p["window_tiers"].items())},
            "inst13_required": bool(p.get("inst13_required")), "rules": _r(p)}


def update(con, promo_id, *, by, name, windows, tiers=(), kind=None, insurer_id=None, insurer_name=None, sector=None, categories=None, target_note=None,
           metric="premium_monthly", date_basis=DEFAULT_DATE_BASIS, per_contract_cap=None, combine_rule=None, pay_timing=None, caution=None,
           window_tiers=None, inst13_required=None, rules=None) -> dict:
    """프로모션을 고친다. 게시 중이어도 고칠 수 있고, 고치기 전과 뒤를 변경 기록에 남긴다.
    정률 시책은 유형과 지급률을 바꾸지 않는다. 지급률은 시상안 원장에서 온 것이기 때문이다.
    window_tiers 와 inst13_required 가 None 이면 지금 값을 그대로 둔다. 창별 구간표를 지우려면 {} 를 넘긴다.
    rules 가 None 이면 조건 확장을 그대로 두고, dict 면 준 키만 바꾼다. 바꾼 뒤의 값 전체를 다시 검사한다."""
    before = _load(con, promo_id)
    if not before:
        raise ValueError("없는 프로모션입니다.")
    if not (name or "").strip():
        raise ValueError("이름을 넣어야 합니다.")
    kind = "rate" if before["kind"] == "rate" else (kind or before["kind"])
    if window_tiers is None:
        window_tiers = {seq: [(t["min_value"], t["reward_kind"], t["reward_value"], t["reward_label"]) for t in rows]
                        for seq, rows in before["window_tiers"].items()}
    if kind == "rate":
        tiers, window_tiers = (), {}
    windows, tiers, combine_rule, categories, wt = _normalize(kind, windows, tiers, combine_rule, metric, date_basis, categories, window_tiers)
    merged = {**_r(before), **(rules or {})}
    clean = None
    if rules or merged != RULE_DEFAULTS:
        clean = _clean_rules(con, merged, kind=kind, metric=metric, date_basis=date_basis, combine_rule=combine_rule, promo_id=promo_id)
    if wt or inst13_required is not None or clean:
        ensure(con)
    if clean:
        _write_rules(con, promo_id, clean)
    con.execute("""UPDATE promotion SET name=?, kind=?, insurer_id=?, insurer_name=?, sector=?, categories=?, target_note=?, metric=?, date_basis=?,
                   per_contract_cap=?, combine_rule=?, pay_timing=?, caution=? WHERE promo_id=?""",
                (name.strip(), kind, insurer_id, insurer_name, sector or None, categories, target_note, metric, date_basis, per_contract_cap or None,
                 combine_rule, pay_timing, caution, promo_id))
    if inst13_required is not None:
        con.execute("UPDATE promotion SET inst13_required=? WHERE promo_id=?", (1 if inst13_required else 0, promo_id))
    kept = {(t.get("window_seq"), float(t["min_value"])): t["original_value"] for t in _tier_rows(con, promo_id) if t.get("original_value") is not None}
    con.execute("DELETE FROM promotion_window WHERE promo_id=?", (promo_id,))
    con.execute("DELETE FROM promotion_tier WHERE promo_id=?", (promo_id,))
    _write_children(con, promo_id, windows, tiers, wt)
    for t in _tier_rows(con, promo_id):                          # 원안(D-21)은 같은 표의 문턱이 같은 구간에 그대로 둔다. 문턱이 바뀐 구간은 원문 확인이 된다
        v = kept.get((t.get("window_seq"), float(t["min_value"])))
        if v is not None:
            con.execute("UPDATE promotion_tier SET original_value=? WHERE tier_id=?", (v, t["tier_id"]))
    after = _load(con, promo_id)
    b, a = _snapshot(before), _snapshot(after)
    changed = sorted(k for k in a if json.dumps(a[k], ensure_ascii=False, default=str) != json.dumps(b[k], ensure_ascii=False, default=str))
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), by, "promotion_update", f"{promo_id}:{','.join(changed) or '바뀐 것 없음'}",
                 json.dumps({k: b[k] for k in changed}, ensure_ascii=False, default=str), json.dumps({k: a[k] for k in changed}, ensure_ascii=False, default=str)))
    con.commit()
    return {"promo_id": promo_id, "changed": changed}


def set_status(con, promo_id, status, by):
    if status not in ("draft", "published", "withdrawn"):
        raise ValueError(status)
    con.execute("UPDATE promotion SET status=?, published_by=CASE WHEN ?='published' THEN ? ELSE published_by END, "
                "published_at=CASE WHEN ?='published' THEN ? ELSE published_at END WHERE promo_id=?", (status, status, by, status, db.now(), promo_id))
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), by, f"promotion_{status}", str(promo_id)))
    con.commit()


def delete_draft(con, promo_id):
    row = con.execute("SELECT status FROM promotion WHERE promo_id=?", (promo_id,)).fetchone()
    if not row or row["status"] == "published":
        raise ValueError("게시 중인 프로모션은 지울 수 없습니다. 먼저 회수하세요.")
    for t in ("promotion_window", "promotion_tier", "promotion_rate", "promotion"):
        con.execute(f"DELETE FROM {t} WHERE promo_id=?", (promo_id,))
    con.commit()


def publish_from_line(con, line_id, by, categories=None, sector="auto") -> int:
    """자동 해석된 정률 시책 줄을 승인하고 프로모션으로 게시한다. 기간이 날짜로 읽힌 줄만 된다."""
    ln = con.execute("""SELECT l.*, d.period_ym FROM incentive_plan_line l JOIN incentive_plan_doc d ON d.plan_doc_id=l.plan_doc_id WHERE l.line_id=?""", (line_id,)).fetchone()
    if not ln:
        raise ValueError("없는 줄입니다.")
    if ln["line_kind"] != "rate_promo" or ln["parse_status"] != "auto" or not ln["period_from"]:
        raise ValueError("자동 해석된 정률 시책만 바로 게시할 수 있습니다. 나머지는 구간과 조건을 입력하세요.")
    if con.execute("SELECT 1 FROM promotion WHERE source_line_id=? AND status!='withdrawn'", (line_id,)).fetchone():
        raise ValueError("이미 프로모션으로 만든 줄입니다.")
    comps = con.execute("SELECT option_label, basis, rate, pay_timing FROM incentive_plan_component WHERE line_id=? ORDER BY seq", (line_id,)).fetchall()
    con.execute("UPDATE incentive_plan_line SET review_status='approved', reviewed_by=?, reviewed_at=? WHERE line_id=?", (by, db.now(), line_id))
    return create(con, period_ym=ln["period_ym"], name=ln["category_raw"] or "시책", kind="rate", recipient_level=ln["recipient_level"],
                  windows=[("시책 기간", ln["period_from"], ln["period_to"])],
                  rates=[(c["option_label"], c["basis"], c["rate"], c["pay_timing"]) for c in comps],
                  insurer_id=ln["insurer_id"], insurer_name=ln["insurer_name"], sector=(ln["sector"] if sector == "auto" else sector),
                  categories=categories, target_note=ln["criteria_raw"], pay_timing=ln["pay_timing"],
                  caution="원안과 다르면 원안을 따릅니다.", source_line_id=line_id, by=by, publish=True)


# ---------------------------------------------------------------- 조회
def _load(con, promo_id) -> dict | None:
    p = con.execute("SELECT * FROM promotion WHERE promo_id=?", (promo_id,)).fetchone()
    if not p:
        return None
    p = dict(p)
    p["windows"] = [dict(r) for r in con.execute("SELECT * FROM promotion_window WHERE promo_id=? ORDER BY seq", (promo_id,))]
    rows = _tier_rows(con, promo_id)
    common = [t for t in rows if t.get("window_seq") is None]
    p["window_tiers"] = {}                                        # {창 순번: 그 창의 구간표}. 창별 구간표가 없으면 비어 있다
    for t in rows:
        if t.get("window_seq") is not None:
            p["window_tiers"].setdefault(t["window_seq"], []).append(t)
    last = p["window_tiers"][max(p["window_tiers"])] if p["window_tiers"] else []
    p["tiers"] = common or last                                   # 보상을 읽는 구간표. 공통이 없으면 마지막 창의 구간표 (가정)
    p["rates"] = [dict(r) for r in con.execute("SELECT * FROM promotion_rate WHERE promo_id=? ORDER BY seq", (promo_id,))]
    p["date_from"], p["date_to"] = p["windows"][0]["date_from"], p["windows"][-1]["date_to"]
    p["kind_ko"], p["metric_ko"], p["unit"] = KIND_KO[p["kind"]], METRIC_KO[p["metric"]], METRIC_UNIT[p["metric"]]
    p["timing_ko"] = timing_ko(p["pay_timing"])
    p["category_list"] = p["categories"].split(",") if p["categories"] else []
    p["inst13"] = has_inst13(p)
    p["rules"] = _read_rules(p)
    return p


def _tier_rows(con, promo_id) -> list:
    return [dict(r) for r in con.execute("SELECT * FROM promotion_tier WHERE promo_id=? ORDER BY tier_no, tier_id", (promo_id,))]


def tiers_for(p, w) -> list:
    """그 창의 구간표. 창별 구간표가 없으면 공통 구간표다."""
    return p.get("window_tiers", {}).get(w["seq"]) or p["tiers"]


def has_inst13(p) -> bool:
    """13회차 유지 조건이 있는가. 13회차 유지 계약만 인정하는 조건(inst13_required)이 켜졌거나 지급 시점에 13회차가 든다."""
    return bool(p.get("inst13_required")) or "inst13" in (p.get("pay_timing") or "") or any("inst13" in (r["pay_timing"] or "") for r in p.get("rates", ()))


def in_kind(p) -> bool:
    """보상이 물품이나 행사(여행)인 프로모션인가. 여행 시상이거나 구간 보상이 여행이나 물품뿐이다."""
    return p["kind"] == "travel" or (bool(p["tiers"]) and all(t["reward_kind"] in IN_KIND for t in p["tiers"]))


def listing(con, ym, role, include_unpublished=False) -> list:
    """그 달에 걸친 프로모션. 설계사와 관리자에게는 게시된 설계사 몫만 보인다."""
    y, m = int(ym[:4]), int(ym[4:])
    first, last = f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
    sql = """SELECT DISTINCT p.promo_id FROM promotion p JOIN promotion_window w ON w.promo_id=p.promo_id
             WHERE (p.period_ym=? OR (w.date_from<=? AND w.date_to>=?))"""
    params = [ym, last, first]
    if role != "main":
        sql += " AND p.status='published' AND p.recipient_level='fc'"
    elif not include_unpublished:
        sql += " AND p.status='published'"
    return [_load(con, r["promo_id"]) for r in con.execute(sql + " ORDER BY p.promo_id", params)]


def headline(p) -> dict:
    """카드에 크게 쓸 한 가지."""
    if p["kind"] == "rate":
        plain = [r for r in p["rates"] if not r["option_label"]]
        if len(p["rates"]) == 1 and plain:
            return {"big": f"{plain[0]['rate'] * 100:g}%", "small": "지급률"}
        if p["rates"] and len(plain) == len(p["rates"]):
            return {"big": " + ".join(f"{r['rate'] * 100:g}%" for r in plain), "small": " + ".join(timing_ko(r["pay_timing"]) or "지급" for r in plain)}
        return {"big": None, "small": "조건별 지급률. 원문 확인"}
    top = p["tiers"][-1] if p["tiers"] else None
    if not top:
        return {"big": None, "small": "구간표 없음"}
    return {"big": reward_text(top), "small": f"최고 구간 · {fmt_value(top['min_value'], p)} 이상"}


def fmt_value(v, p) -> str:
    return f"{v:g}건" if p["metric"] == "policy_count" else f"{int(v):,}원"


def reward_text(t, value=None) -> str:
    if t["reward_kind"] == "fixed":
        return f"{int(t['reward_value'] or 0):,}원"
    if t["reward_kind"] == "rate":
        return f"{(t['reward_value'] or 0) * 100:g}%"
    return t["reward_label"] or REWARD_KO[t["reward_kind"]]


def window_state(w, ref: date) -> dict:
    f, t = date.fromisoformat(w["date_from"]), date.fromisoformat(w["date_to"])
    if ref < f:
        return {"state": "upcoming", "days_left": (t - ref).days}
    if ref > t:
        return {"state": "ended", "days_left": 0}
    return {"state": "active", "days_left": (t - ref).days}


def due_label(p, ref: date) -> tuple:
    f, t = date.fromisoformat(p["date_from"]), date.fromisoformat(p["date_to"])
    if ref < f:
        return f"{f.month}월 {f.day}일 시작", ""
    if ref > t:
        return f"{t.month}월 {t.day}일 종료", ""
    left = (t - ref).days
    return ("오늘 마감" if left == 0 else f"D-{left}"), ("bad" if left <= 3 else "blue")


# ---------------------------------------------------------------- 달성 현황
def _statuses(p) -> tuple:
    """세는 계약 상태. 성립 기준은 COUNTED, 청약 기준은 APPLIED_COUNTED 에서 drop_statuses 를 뺀다."""
    r = _r(p)
    base = APPLIED_COUNTED if r["count_basis"] == "application" else COUNTED
    return tuple(s for s in base if s not in r["drop_statuses"])


def _window_sql(p, w, agent_ids) -> tuple:
    """한 창에서 세는 계약의 SQL. 계약마다 한 줄 {agent_id, contract_id, v, d, nm}. v 는 건당 인정 한도를 적용한 값,
    d 는 기준일, nm 은 상품 이름이다. 상품 이름과 가중 인정은 _counted() 가 이 줄들에 적용한다."""
    col = p["metric"]
    capped = bool(p["per_contract_cap"]) and col != "policy_count"
    expr = f"MIN(c.{col}, ?)" if capped else f"c.{col}"
    q = ",".join("?" * len(agent_ids))
    statuses = _statuses(p)
    dexpr = _date_expr(p["date_basis"])
    sql = f"""SELECT c.agent_id, c.contract_id, {expr} v, {dexpr} d, {NAME_SQL} nm FROM contract c {classify.JOINS}
              WHERE c.agent_id IN ({q}) AND c.status IN ({','.join('?' * len(statuses))}) AND {dexpr} BETWEEN ? AND ?"""
    params = ([p["per_contract_cap"]] if capped else []) + list(agent_ids) + list(statuses) + [w["date_from"], w["date_to"]]
    if p["insurer_id"]:
        sql += " AND c.insurer_id=?"
        params.append(p["insurer_id"])
    if p["sector"]:
        sql += f" AND {classify.SECTOR}=?"
        params.append(p["sector"])
    if p["category_list"]:
        sql += f" AND {classify.CATEGORY} IN ({','.join('?' * len(p['category_list']))})"
        params += p["category_list"]
    if _r(p)["exclude_self"]:
        sql += " AND COALESCE(c.self_family, 0)=0"
    return sql + " ORDER BY c.agent_id, c.contract_id", params


def _boost_key(b, d):
    """가중 구간의 열쇠. 매월 날짜 구간이면 그 달, 날짜 목록이면 그 순번이다. 가중 구간 밖이면 None."""
    if not d or len(d) < 10:
        return None
    if b["month_days"]:
        return d[:7] if b["month_days"][0] <= int(d[8:10]) <= b["month_days"][1] else None
    return next((i for i, (f, t) in enumerate(b["ranges"]) if f <= d <= t), None)


def _apply_boost(b, rows) -> None:
    """줄마다 value(합계에 쓰는 값)와 value_each(창 조건에 쓰는 값)를 채운다. 모듈 설명의 가중 인정 규칙이다 (D-84 가정).
    한도의 범위가 range 면 사람마다 가중 구간의 앞 계약부터(기준일, 계약 번호 순) 한도를 나눠 쓴다."""
    for r in rows:
        r["value_each"] = r["value"]
    if not b:
        return
    left = {}
    for r in sorted(rows, key=lambda x: (x["agent_id"], x["date"], x["contract_id"])):
        key = _boost_key(b, r["date"])
        if key is None:
            continue
        v = r["value"]
        if b["cap"] is None:
            part = v
        elif b["cap_scope"] == "contract":
            part = min(v, b["cap"])
        else:
            k = (r["agent_id"], key)
            part = min(v, left.get(k, b["cap"]))
            left[k] = left.get(k, b["cap"]) - max(part, 0)
        boosted = v + max(part, 0) * (b["factor"] - 1)
        r["value_each"] = boosted
        if b["applies_to"] == "all":
            r["value"] = boosted


def _counted(con, p, agent_ids) -> tuple:
    """세는 계약 줄과 이름이 없어 세지 않은 계약 수. ([{agent_id, contract_id, window_seq, value, value_each, date}], {agent_id: 건수}).
    value 는 합계(조건 2와 정률)에, value_each 는 창 조건(조건 1)에 쓴다. 가중 인정이 없거나 모든 조건에 쓰면 둘이 같다."""
    rows, unnamed = [], {}
    ids = list(agent_ids)
    if not ids:
        return rows, unnamed
    r = _r(p)
    inc, exc = [_norm_name(x) for x in r["product_include"]], [_norm_name(x) for x in r["product_exclude"]]
    seen = set()
    for w in p["windows"]:
        sql, params = _window_sql(p, w, ids)
        for x in con.execute(sql, params):
            if inc or exc:
                nm = _norm_name(x["nm"])
                if not nm:
                    if (x["agent_id"], x["contract_id"]) not in seen:
                        seen.add((x["agent_id"], x["contract_id"]))
                        unnamed[x["agent_id"]] = unnamed.get(x["agent_id"], 0) + 1
                    continue
                if (inc and not any(k in nm for k in inc)) or any(k in nm for k in exc):
                    continue
            rows.append({"agent_id": x["agent_id"], "contract_id": x["contract_id"], "window_seq": w["seq"], "value": float(x["v"] or 0),
                         "date": (x["d"] or "")[:10]})
    _apply_boost(r["boost"], rows)
    return rows, unnamed


def _sums(p, agent_ids, rows) -> tuple:
    """({agent_id: [창마다 value_each]}, {agent_id: [창마다 value]})"""
    ix = {w["seq"]: i for i, w in enumerate(p["windows"])}
    each = {a: [0.0] * len(p["windows"]) for a in agent_ids}
    plain = {a: [0.0] * len(p["windows"]) for a in agent_ids}
    for x in rows:
        each[x["agent_id"]][ix[x["window_seq"]]] += x["value_each"]
        plain[x["agent_id"]][ix[x["window_seq"]]] += x["value"]
    return each, plain


def _values(con, p, agent_ids) -> dict:
    """{agent_id: [창마다의 실적]}. 합계에 쓰는 값(value)이다."""
    ids = list(agent_ids)
    return _sums(p, ids, _counted(con, p, ids)[0])[1]


def _contract_rows(con, p, agent_ids) -> list:
    """창마다 센 계약. [{agent_id, contract_id, window_seq, value, value_each}]. _values() 의 합계가 value 의 합이다.
    딸린 시책이면 부모 프로모션의 계약이다."""
    src = _parent(con, p) or p
    return [{k: x[k] for k in ("agent_id", "contract_id", "window_seq", "value", "value_each")} for x in _counted(con, src, list(agent_ids))[0]]


def _tier_of(tiers, value):
    hit = None
    for t in tiers:
        if value >= t["min_value"]:
            hit = t
    return hit


def progress(con, p, agent_ids, ref: date) -> dict:
    """{agent_id: 달성 현황}. 금액과 구간은 모두 예상이다. basis 는 실적 기준 설명 글이다 (D-08, basis_note()).
    unnamed 는 상품 이름 조건이 있는데 이름이 비어 세지 않은 계약 수다. 딸린 시책은 _attached() 가 낸다."""
    ids = list(dict.fromkeys(agent_ids))
    if _r(p)["parent_promo_id"]:
        return _attached(con, p, ids, ref)
    rows, unnamed = _counted(con, p, ids)
    each, plain = _sums(p, ids, rows)
    states = [window_state(w, ref) for w in p["windows"]]
    basis = basis_note(p)
    out = {}
    for a in ids:
        res = _judge(p, each[a], states, basis, plain[a])
        res["unnamed"] = unnamed.get(a, 0)
        out[a] = res
    return out


def _judge(p, per_window, states, basis, plain=None) -> dict:
    """창마다의 실적 하나를 달성 현황 하나로. progress() 와 13회차 다시 계산이 함께 쓴다.
    per_window 는 창 조건에 쓰는 값, plain 은 합계에 쓰는 값이다. plain 이 없으면 같다. value 는 plain 의 합이다.
    창별 구간표가 있으면 창마다 그 창의 표로 구간을 정하고, 낮은 쪽 구간 번호의 보상을 p["tiers"] 에서 읽는다."""
    plain = list(per_window) if plain is None else list(plain)
    ended_all = all(s["state"] == "ended" for s in states)
    wins = [{**w, **s, "value": v, "plain": pv, "tier": (_tier_of(tiers_for(p, w), v) if p["combine_rule"] in EACH_RULES else None)}
            for w, s, v, pv in zip(p["windows"], states, per_window, plain)]
    total = sum(plain)
    res = {"windows": wins, "value": total, "tier": None, "next": None, "reward": None, "amount": None, "status": "in_progress", "broken": False, "capped": False,
           "basis": basis, "date_basis": p["date_basis"]}
    if p["kind"] == "rate":
        plain_rates = [r for r in p["rates"] if not r["option_label"]]
        if plain_rates and len(plain_rates) == len(p["rates"]):
            res["amount"] = sum(r["rate"] for r in plain_rates) * total
            res["parts"] = [{"timing": timing_text(r["pay_timing"]),
                             "amount": r["rate"] * total} for r in plain_rates]
        res["status"] = "ended" if ended_all else ("achieved" if total > 0 else "in_progress")
        return res
    if p["combine_rule"] in PAIRED:
        tier = _judge_paired(p, wins, total, res)
    else:
        if p["combine_rule"] == "all":
            done = [w for w in wins if w["state"] == "ended"]
            res["broken"] = any(w["tier"] is None for w in done)          # 끝난 창을 놓치면 뒤 창은 무효다
            reached = [w["tier"]["tier_no"] if w["tier"] else 0 for w in wins]
            low = min(reached)
            tier = next((t for t in p["tiers"] if t["tier_no"] == low), None) if low else None
            focus = next((w for w in wins if w["state"] == "active"), wins[-1])
            base = focus["value"]
            scale = tiers_for(p, focus)
        else:
            tier, base = _tier_of(p["tiers"], total), total
            scale = p["tiers"]
        nxt = next((t for t in scale if t["min_value"] > base), None)
        if p["combine_rule"] == "all":
            closed = [w["tier"]["tier_no"] if w["tier"] else 0 for w in wins if w["state"] == "ended"]
            cap = min(closed) if closed else None                         # 끝난 창의 구간보다 위로는 올라갈 수 없다
            if cap is not None and (nxt is None or nxt["tier_no"] > cap):
                nxt = None
                res["capped"] = bool(p["tiers"]) and 0 < cap < p["tiers"][-1]["tier_no"]
        if nxt and not res["broken"]:
            pay = next((t for t in p["tiers"] if t["tier_no"] == nxt["tier_no"]), nxt)     # 보상은 보상 구간표에서 읽는다
            res["next"] = {"tier_no": nxt["tier_no"], "min_value": nxt["min_value"], "gap": nxt["min_value"] - base, "reward": reward_text(pay)}
        top = scale[-1]["min_value"] if scale else 0
        res["pct"] = min(base / top * 100, 100) if top else 0
    res["tier"] = tier
    if tier:
        res["reward"] = reward_text(tier)
        if tier["reward_kind"] == "fixed":
            res["amount"] = tier["reward_value"]
        elif tier["reward_kind"] == "rate":
            res["amount"] = (tier["reward_value"] or 0) * total
    if res["broken"]:
        res["status"] = "missed"
    elif ended_all:
        res["status"] = "achieved" if tier else "missed"
    else:
        res["status"] = "achieved" if tier and not res["next"] else "in_progress"
    return res


def _judge_paired(p, wins, total, res):
    """결합 both, either. 조건 1은 창마다 창별 구간표(all 과 같은 규칙), 조건 2는 합계를 공통 구간표에 대입한다.
    both 는 두 조건의 구간 번호 가운데 낮은 쪽, either 는 높은 쪽을 쓴다. 보상은 공통 구간표에서 읽는다.
    다음 구간은 both 면 뒤처진 조건에서, either 면 남은 비율이 작은 조건에서 고른다. res 를 채우고 구간을 돌려준다."""
    both = p["combine_rule"] == "both"
    done = [w for w in wins if w["state"] == "ended"]
    each_no = min(w["tier"]["tier_no"] if w["tier"] else 0 for w in wins)
    each_broken = any(w["tier"] is None for w in done)
    closed = [w["tier"]["tier_no"] if w["tier"] else 0 for w in done]
    each_cap = min(closed) if closed else None
    focus = next((w for w in wins if w["state"] == "active"), wins[-1])
    hit = _tier_of(p["tiers"], total)
    sum_no = hit["tier_no"] if hit else 0
    no = min(each_no, sum_no) if both else max(each_no, sum_no)
    tier = next((t for t in p["tiers"] if t["tier_no"] == no), None) if no else None
    scale = tiers_for(p, focus)
    n_each = None if each_broken else next((t for t in scale if t["min_value"] > focus["value"]), None)
    if n_each and each_cap is not None and n_each["tier_no"] > each_cap:
        n_each = None
    n_sum = next((t for t in p["tiers"] if t["min_value"] > total), None)
    cands = []
    if n_each:
        cands.append({"path": "each", "t": n_each, "base": focus["value"], "no": each_no})
    if n_sum:
        cands.append({"path": "sum", "t": n_sum, "base": total, "no": sum_no})
    if both:
        cands = [c for c in cands if c["no"] == no]                      # 뒤처진 조건만 다음 구간을 정한다
        pick = max(cands, key=lambda c: (c["t"]["min_value"] - c["base"]) / c["t"]["min_value"], default=None)
        if no == each_no and not n_each:                                  # 조건 1이 막히면 더 오를 수 없다
            pick = None
    else:
        cands = [c for c in cands if c["t"]["tier_no"] > no]
        pick = min(cands, key=lambda c: (c["t"]["min_value"] - c["base"]) / c["t"]["min_value"], default=None)
    res["broken"] = both and each_broken
    res["capped"] = bool(both and each_cap is not None and p["tiers"] and 0 < each_cap < p["tiers"][-1]["tier_no"] and pick is None)
    if pick and not res["broken"]:
        pay = next((t for t in p["tiers"] if t["tier_no"] == pick["t"]["tier_no"]), pick["t"])
        res["next"] = {"tier_no": pick["t"]["tier_no"], "min_value": pick["t"]["min_value"], "gap": pick["t"]["min_value"] - pick["base"],
                       "reward": reward_text(pay), "path": pick["path"]}
    each_top = scale[-1]["min_value"] if scale else 0
    sum_top = p["tiers"][-1]["min_value"] if p["tiers"] else 0
    each_pct = min(focus["value"] / each_top * 100, 100) if each_top else 0
    sum_pct = min(total / sum_top * 100, 100) if sum_top else 0
    res["pct"] = min(each_pct, sum_pct) if both else max(each_pct, sum_pct)
    res["paths"] = {"each": {"tier_no": each_no or None, "broken": each_broken, "value": focus["value"], "pct": each_pct},
                    "sum": {"tier_no": sum_no or None, "broken": False, "value": total, "pct": sum_pct}}
    return tier


# ---------------------------------------------------------------- 딸린 시책 (모듈 설명의 '조건 확장')
GATE_NOTE = "조직 달성 인원은 그 조직 재직자 전체에서 셉니다. 보는 범위와 상관없이 같은 숫자입니다."


def is_achieved(p, r) -> bool:
    """달성 인원에 넣는가. 구간에 닿았거나, 정률 시책이면 대상 실적이 있다. 팀원 달성 현황의 달성 수와 같은 기준이다."""
    return bool(r["tier"]) or (p["kind"] == "rate" and r["value"] > 0)


def _parent(con, p):
    pid = _r(p)["parent_promo_id"]
    return _load(con, pid) if pid else None


def _org_ym(p, ref: date) -> str:
    """조직 소속을 볼 달. 기준일을 프로모션 기간 안으로 당긴 달이다."""
    d = min(max(ref, date.fromisoformat(p["date_from"])), date.fromisoformat(p["date_to"]))
    return d.strftime("%Y%m")


def _org_by_path(con, path_key):
    if path_key:
        row = con.execute("SELECT org_id, name, level, path_key FROM org_unit WHERE path_key=?", (path_key,)).fetchone()
    else:
        row = con.execute("SELECT org_id, name, level, path_key FROM org_unit WHERE parent_id IS NULL ORDER BY org_id LIMIT 1").fetchone()
    return dict(row) if row else None


def org_gate(con, p, ref: date, ym: str | None = None) -> dict | None:
    """딸린 시책의 조직 달성 인원 문턱. 문턱이 없으면 None 이다 (D-88 가정).
    부모 프로모션의 달성 인원을 그 조직(gate_org_path, 비면 맨 위 조직) 재직자 전체에서 센다. 사람은 내지 않고 숫자만 낸다.
    돌려주는 것: {org_path, org_name, min, n, open, left, ym, note}. open 은 n >= min 이고 left 는 모자란 인원이다."""
    r = _r(p)
    if not r["gate_min_achievers"]:
        return None
    parent = _parent(con, p) or p
    org = _org_by_path(con, r["gate_org_path"])
    ym = ym or _org_ym(parent, ref)
    pool = orgs.members(con, org["org_id"], ym, fc_only=False, active_only=True) if org else []
    res = progress(con, parent, pool, ref)
    n = sum(1 for a in pool if is_achieved(parent, res[a]))
    m = r["gate_min_achievers"]
    return {"org_path": org["path_key"] if org else None, "org_name": org["name"] if org else None, "min": m, "n": n, "open": n >= m,
            "left": max(m - n, 0), "ym": ym, "note": GATE_NOTE}


def _levels(con, agent_ids, ym) -> dict:
    """{agent_id: 커리어 직급 코드}. 그 달의 agent_career_history 다. 없으면 빠진다 (D-89 가정)."""
    ids = sorted(set(agent_ids))
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    out = {}
    for x in con.execute(f"""SELECT h.agent_id, l.code FROM agent_career_history h JOIN career_level l ON l.level_id=h.level_id
                             WHERE h.agent_id IN ({q}) AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
                             ORDER BY h.effective_from""", (*ids, ym, ym)):
        out[x["agent_id"]] = (x["code"] or "").upper()                  # 한 달에 둘이면 나중 것
    return out


def _attached(con, p, ids, ref: date) -> dict:
    """딸린 시책의 달성 현황. 실적, 창, 다음 구간은 부모의 것이다. 대상 직급이 아니면 status 가 not_target 이고 target 이 거짓이다.
    부모를 달성하고 대상 직급이며 조직 문턱이 열리면 부모의 구간 번호로 이 시책의 구간표에서 보상을 읽는다. 표가 짧으면 마지막 구간이다.
    gate 는 org_gate() 의 결과이고 문턱이 없으면 None 이다. parent 는 {promo_id, tier_no, status} 다."""
    parent = _parent(con, p)
    r = _r(p)
    pres = progress(con, parent, ids, ref)
    gate = org_gate(con, p, ref)
    levels = _levels(con, ids, _org_ym(parent, ref)) if r["target_levels"] else None
    ended_all = date.fromisoformat(parent["date_to"]) < ref
    out = {}
    for a in ids:
        pr = pres[a]
        target = levels is None or levels.get(a) in r["target_levels"]
        ok = is_achieved(parent, pr)
        tier = None
        if target and ok and (gate is None or gate["open"]) and p["tiers"]:
            no = pr["tier"]["tier_no"] if pr["tier"] else 1
            tier = next((t for t in p["tiers"] if t["tier_no"] == no), p["tiers"][-1])
        res = {**pr, "tier": tier, "reward": reward_text(tier) if tier else None, "amount": None, "basis": basis_note(parent),
               "target": target, "level": levels.get(a) if levels is not None else None, "gate": gate,
               "parent": {"promo_id": parent["promo_id"], "tier_no": pr["tier"]["tier_no"] if pr["tier"] else None, "status": pr["status"]}}
        if tier and tier["reward_kind"] == "fixed":
            res["amount"] = tier["reward_value"]
        elif tier and tier["reward_kind"] == "rate":
            res["amount"] = (tier["reward_value"] or 0) * pr["value"]
        if not target:
            res.update(status="not_target", next=None)
        elif pr["status"] == "missed" or (ended_all and not tier):
            res["status"] = "missed"
        elif tier:
            res["status"] = "achieved"
        else:
            res["status"] = "in_progress"
        out[a] = res
    return out


# ---------------------------------------------------------------- 13회차 유지율 문턱 (표시만, D-90 가정)
RETENTION_NOTE = ("13회차 유지율은 이 프로모션에서 센 계약 가운데 판정이 난 계약의 유지 비율입니다. 판정 전 계약은 빼고, "
                  "기준일이 되기 전에는 예상입니다. 환수 계산은 하지 않고 표시만 합니다.")


def retention(con, p, agent_ids, ref: date) -> dict:
    """사람마다 센 계약의 13회차 유지율과 문턱 충족. 표시만 한다. agent_ids 는 범위 안 설계사 목록이다.
    판정일은 기준일과 retention_ref 가운데 이른 날이다. 비율은 건수 기준(count) 또는 실적 기준(amount)이다.
    돌려주는 것: {threshold, basis, ref_date, judged_on, final, note, people: {agent_id: {n, kept, lapsed, pending, rate, rate_count,
                rate_amount, meets}}}. meets 는 판정 난 계약이 없으면 None 이다. final 은 판정일이 retention_ref 에 닿았는가다."""
    r = _r(p)
    if not r["retention_min"]:
        raise ValueError("13회차 유지율 문턱이 없는 프로모션입니다.")
    end = date.fromisoformat(r["retention_ref"]) if r["retention_ref"] else None
    judged_on = min(ref, end) if end else ref
    ids = list(dict.fromkeys(agent_ids))
    rows = _contract_rows(con, p, ids)
    judg = persist.inst13_judge(con, [x["contract_id"] for x in rows], judged_on)
    people = {a: {"n": 0, "kept": 0, "lapsed": 0, "pending": 0, "amt_kept": 0.0, "amt_lapsed": 0.0} for a in ids}
    seen = set()
    for x in rows:
        if x["contract_id"] in seen:
            continue
        seen.add(x["contract_id"])
        j = judg.get(x["contract_id"], {"judgement": "pending"})["judgement"]
        d = people[x["agent_id"]]
        d["n"] += 1
        d[j] += 1
        if j in ("kept", "lapsed"):
            d[f"amt_{j}"] += x["value"]
    for d in people.values():
        judged, amt = d["kept"] + d["lapsed"], d["amt_kept"] + d["amt_lapsed"]
        d["rate_count"] = d["kept"] / judged if judged else None
        d["rate_amount"] = d["amt_kept"] / amt if amt else None
        d["rate"] = d["rate_amount"] if r["retention_basis"] == "amount" else d["rate_count"]
        d["meets"] = None if d["rate"] is None else d["rate"] >= r["retention_min"] - 1e-9
    return {"threshold": r["retention_min"], "basis": r["retention_basis"], "ref_date": r["retention_ref"], "judged_on": judged_on.isoformat(),
            "final": bool(end and ref >= end), "note": RETENTION_NOTE, "people": people}


# ---------------------------------------------------------------- 조직별 진척
ORG_NOTE = ("조직 줄은 그 조직과 아래 전부에 소속인 범위 안 설계사의 합입니다. 퇴사자의 퇴사 전 실적은 실적 합계에 넣고 "
            "대상, 달성, 순위에서는 뺍니다. 달성 인원 순위는 바로 아래 조직끼리 견줍니다.")
NEAR_KEY = "promo.near_pct"                                   # app/org/settings.py 의 항목. 화면은 app/web/org_values.NEAR_KEY


def near(r, ratio: float) -> bool:
    """구간 근접. 실적이 있고 다음 구간까지 남은 양이 다음 문턱의 ratio 안이다. app/web/routes/promo.py 의 near 와 같은 규칙이다."""
    return bool(r.get("next") and r["value"] > 0 and r["next"]["gap"] <= r["next"]["min_value"] * ratio)


def _near_pcts(con, homes) -> dict:
    """{agent_id: 구간 근접 퍼센트}. 소속 조직의 지금 달 설정 값이고 소속이 없으면 맨 위 조직의 값이다. 읽지 못하면 기본값이다."""
    from app.org import settings as st  # 웹 로그인 문맥을 함께 불러오므로 쓸 때만 부른다

    default = st.item(NEAR_KEY)["default"]
    try:
        now, root = st.now_ym(con), st.root_org(con)
    except sqlite3.OperationalError:
        return {a: default for a in homes}
    cache, out = {}, {}
    for a, org in homes.items():
        org = org if org is not None else root
        if org not in cache:
            try:
                cache[org] = st.effective(con, org, NEAR_KEY, now)["value"] if org is not None else default
            except (st.SettingError, sqlite3.OperationalError, TypeError, ValueError):
                cache[org] = default
        out[a] = cache[org]
    return out


def _homes(con, agent_ids, ym) -> dict:
    """{agent_id: {org_id, active}}. 범위 안 설계사 가운데 그 달에 소속 이력이 있는 사람. 한 달에 이력이 둘이면 나중 것이다."""
    ids = sorted(set(agent_ids or ()))
    out = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = ",".join("?" * len(part))
        for x in con.execute(f"""SELECT h.agent_id, h.org_id, a.status FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id
                                 WHERE h.agent_id IN ({q}) AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
                                 ORDER BY h.effective_from, h.id""", (*part, ym, ym)):
            out[x["agent_id"]] = {"org_id": x["org_id"], "active": x["status"] == "active"}
    return out


def _org_row(p, shape, unit, members, people, res, pcts, direct=False) -> tuple:
    """조직 줄 하나와 그 줄의 예상 시상액 합계. shape 는 창을 읽는 프로모션이다. 딸린 시책이면 부모다."""
    targets = [a for a in members if people[a]["active"] and res[a].get("target", True)]
    left = [a for a in members if not people[a]["active"] and res[a].get("target", True)]
    achieved = [a for a in targets if is_achieved(p, res[a])]
    active = [a for a in targets if res[a]["value"] > 0]
    n = len(targets)
    wins = None
    if len(shape["windows"]) > 1 and shape["combine_rule"] in EACH_RULES:
        wins = [{"seq": w["seq"], "label": w["label"], "n_achieved": sum(1 for a in targets if res[a]["windows"][i]["tier"])}
                for i, w in enumerate(shape["windows"])]
    amounts = [res[a]["amount"] for a in targets if res[a]["amount"] is not None]
    row = {"org_id": unit["org_id"], "path_key": unit["path_key"], "name": unit["name"], "level": unit["level"], "direct": direct, "rank": None,
           "n_target": n, "n_active": len(active), "n_achieved": len(achieved),
           "n_near": sum(1 for a in targets if near(res[a], float(pcts.get(a, 30)) / 100.0)), "n_inactive": n - len(active),
           "achieve_rate": len(achieved) / n if n else None, "windows": wins,
           "value": sum(res[a]["value"] for a in targets + left), "value_left": sum(res[a]["value"] for a in left), "n_left": len(left),
           "unnamed": sum(res[a].get("unnamed", 0) for a in targets + left), "agent_ids": targets, "left_ids": left}
    return row, (sum(amounts) if amounts else None)


def org_progress(con, p, org_key, agent_ids, ref: date, *, ym: str | None = None, near_pct=None, keep_empty=False) -> dict:
    """프로모션 하나의 조직별 진척. org_key 조직(path_key)의 바로 아래 조직마다 한 줄을 낸다.

    agent_ids 는 범위 안 설계사 목록이고 그 밖의 사람은 세지 않는다. 사람별 값은 progress() 를 그대로 합친다.
    ym 은 소속을 볼 달이다. 비면 기준일을 프로모션 기간 안으로 당긴 달이다. 팀원 달성 현황과 맞추려면 화면의 자료 기준 달을 넘긴다.
    near_pct 는 구간 근접 퍼센트다. 숫자면 모두에게, dict 면 사람마다, None 이면 소속 조직의 설정(promo.near_pct)이다.
    keep_empty 가 거짓이면 범위 안 사람이 없는 아래 조직은 내지 않는다.
    퇴사자(D-42)는 실적 합계(value)에 넣고 대상, 가동, 달성, 근접, 미가동, 순위에서는 뺀다. 딸린 시책은 대상 직급인 사람만 대상이다.

    돌려주는 것: {promo_id, name, org, ym, ref, basis, estimate_note, note, metric, unit, windows, rows, total, amounts, gate, near_pcts}
    - rows: [{org_id, path_key, name, level, direct, rank, n_target, n_active, n_achieved, n_near, n_inactive, achieve_rate,
              windows, value, value_left, n_left, unnamed, agent_ids, left_ids}]
      direct 가 참인 줄은 org_key 조직에 바로 소속인 사람이고 순위가 없다. rank 는 아래 조직끼리 달성 인원 큰 순이고 0명이면 None 이다.
      windows 는 창이 여럿이고 창마다 달성하는 결합일 때 [{seq, label, n_achieved}], 아니면 None 이다
    - total: rows 를 더한 줄. rank, agent_ids, left_ids 는 없다
    - amounts: {by_org: {path_key: 예상 시상액 합계 또는 None}, total, in_kind, note}. 메인 관리자에게만 넘긴다 (D-17)
    - gate: 딸린 시책의 조직 달성 인원 문턱(org_gate). 없으면 None
    """
    org = _org_by_path(con, org_key) if org_key else None
    if not org:
        raise ValueError("없는 조직입니다.")
    ym = ym or _org_ym(p, ref)
    homes = _homes(con, agent_ids, ym)
    under = orgs.descendants(con, org["org_id"])
    people = {a: h for a, h in homes.items() if h["org_id"] in under}
    res = progress(con, p, sorted(people), ref)
    shape = _parent(con, p) or p
    if isinstance(near_pct, dict):
        pcts = {a: near_pct.get(a, 30) for a in people}
    elif near_pct is not None:
        pcts = {a: near_pct for a in people}
    else:
        pcts = _near_pcts(con, {a: h["org_id"] for a, h in people.items()})
    rows, by_org = [], {}
    direct = sorted(a for a, h in people.items() if h["org_id"] == org["org_id"])
    if direct:
        row, amt = _org_row(p, shape, org, direct, people, res, pcts, direct=True)
        rows.append(row)
        by_org[org["path_key"]] = amt
    kids = [dict(x) for x in con.execute("SELECT org_id, name, level, path_key FROM org_unit WHERE parent_id=? ORDER BY path_key, org_id", (org["org_id"],))]
    for k in kids:
        sub = orgs.descendants(con, k["org_id"])
        mem = sorted(a for a, h in people.items() if h["org_id"] in sub)
        if not mem and not keep_empty:
            continue
        row, amt = _org_row(p, shape, k, mem, people, res, pcts)
        rows.append(row)
        by_org[k["path_key"]] = amt
    ranked = [x for x in rows if not x["direct"]]
    order = sorted((x["n_achieved"] for x in ranked if x["n_achieved"] > 0), reverse=True)
    first = {}
    for i, v in enumerate(order, start=1):
        first.setdefault(v, i)
    for x in ranked:
        x["rank"] = first.get(x["n_achieved"])
    keys = ("n_target", "n_active", "n_achieved", "n_near", "n_inactive", "value", "value_left", "n_left", "unnamed")
    total = {k: sum(x[k] for x in rows) for k in keys}
    total["achieve_rate"] = total["n_achieved"] / total["n_target"] if total["n_target"] else None
    total["windows"] = None
    if rows and rows[0]["windows"] is not None:
        total["windows"] = [{**w, "n_achieved": sum(x["windows"][i]["n_achieved"] for x in rows)} for i, w in enumerate(rows[0]["windows"])]
    amts = [v for v in by_org.values() if v is not None]
    return {"promo_id": p["promo_id"], "name": p["name"], "org": org, "ym": ym, "ref": ref.isoformat(), "basis": basis_note(shape),
            "estimate_note": ESTIMATE_NOTE, "note": ORG_NOTE, "metric": p["metric"], "unit": p["unit"],
            "windows": [{"seq": w["seq"], "label": w["label"], "date_from": w["date_from"], "date_to": w["date_to"]} for w in shape["windows"]],
            "rows": rows, "total": total, "near_pcts": sorted({int(v) for v in pcts.values()}),
            "amounts": {"by_org": by_org, "total": sum(amts) if amts else None, "in_kind": in_kind(p), "note": ESTIMATE_NOTE},
            "gate": org_gate(con, p, ref) if _r(p)["gate_min_achievers"] else None}


# ---------------------------------------------------------------- 원안 (D-21)
# D-21 은 열려 있다. 제안대로 지금 금액(reward_value, rate)을 배포안으로 보고, 원안은 메인 관리자가 넣은 original_value, original_rate 만 쓴다.
# 원안 칸이 비면 원문 확인이다. 원안 칸은 schema.sql 의 [promotion_plan] 묶음이고 오래된 DB 에는 ensure() 가 붙인다.
PLAN_BEGIN, PLAN_END = "-- [promotion_plan:begin]", "-- [promotion_plan:end]"
CONFIRM_BEGIN, CONFIRM_END = "-- [promotion_confirm:begin]", "-- [promotion_confirm:end]"
RULES_BEGIN, RULES_END = "-- [promotion_rules:begin]", "-- [promotion_rules:end]"
MISSING_ORIGINAL = "원문 확인"


def ensure(con) -> None:
    """원안 칸, 확정 결과 표, S3 확정 기능의 칸과 표, 조건 확장 칸이 없는 오래된 DB 에서 schema.sql 의 [promotion_plan],
    [promotion_confirm], [promotion_rules] 묶음을 차례로 돌린다. 이미 있는 칸과 표는 건너뛴다."""
    text = db.SCHEMA.read_text(encoding="utf-8")
    for begin, end in ((PLAN_BEGIN, PLAN_END), (CONFIRM_BEGIN, CONFIRM_END), (RULES_BEGIN, RULES_END)):
        block = text[text.index(begin):text.index(end)]
        body = "\n".join(line.split("--", 1)[0] for line in block.splitlines())
        for stmt in (x.strip() for x in body.split(";")):
            if not stmt:
                continue
            words = stmt.split()
            if words[:2] == ["ALTER", "TABLE"] and words[3:5] == ["ADD", "COLUMN"]:
                if words[5] in {r[1] for r in con.execute(f"PRAGMA table_info({words[2]})")}:
                    continue
            con.execute(stmt)
    con.commit()


def _require_main(con, by) -> str:
    """메인 관리자만 통과한다. by 는 login_id 이거나 auth.context() 의 결과다. 계정 표에서 다시 확인한다."""
    if isinstance(by, dict):
        if by.get("role") != "main":
            raise PermissionError("메인 관리자만 할 수 있습니다.")
        by = by.get("login_id")
    row = con.execute("SELECT is_main_admin FROM user_account WHERE login_id=? AND status='active'", (by,)).fetchone() if by else None
    if not row or not row["is_main_admin"]:
        raise PermissionError("메인 관리자만 할 수 있습니다.")
    return by


def has_original(p) -> bool:
    return any(t.get("original_value") is not None for t in p["tiers"]) or any(r.get("original_rate") is not None for r in p["rates"])


def _original_view(p) -> dict | None:
    """원안 칸으로 바꿔 끼운 프로모션. 원안이 하나도 없으면 None 이다. 원안이 빈 금액 구간과 지급률은 missing 이다."""
    if not has_original(p):
        return None
    tiers = []
    for t in p["tiers"]:
        money = t["reward_kind"] in ("fixed", "rate")
        miss = money and t.get("original_value") is None
        tiers.append({**t, "reward_value": None if miss else (t.get("original_value") if money else t["reward_value"]), "missing": miss})
    rates = [{**r, "rate": r.get("original_rate"), "missing": r.get("original_rate") is None} for r in p["rates"]]
    return {**p, "tiers": tiers, "rates": rates}


def set_original(con, promo_id, *, by, tiers=None, rates=None) -> dict:
    """원안 보상을 넣는다. 메인 관리자만 한다. tiers 는 {구간 번호: 원안 보상}, rates 는 {조각 순번: 원안 지급률}이다.
    구간 번호는 보상을 읽는 구간표(p["tiers"])의 번호다. 창별 구간표만 있으면 마지막 창의 표다.
    값이 None 이면 원안을 비운다(원문 확인). 바꾸기 전과 뒤를 변경 기록에 남긴다."""
    by = _require_main(con, by)
    ensure(con)
    p = _load(con, promo_id)
    if not p:
        raise ValueError("없는 프로모션입니다.")
    known_t = {t["tier_no"]: t for t in p["tiers"]}
    known_r = {r["seq"]: r for r in p["rates"]}
    before, after = {}, {}
    for no, v in (tiers or {}).items():
        if int(no) not in known_t:
            raise ValueError(f"없는 구간입니다: {no}")
        if known_t[int(no)]["reward_kind"] not in ("fixed", "rate"):
            raise ValueError("여행과 물품 구간에는 원안 금액을 넣지 않습니다.")
        v = None if v in (None, "") else float(v)
        before[f"tier{no}"], after[f"tier{no}"] = known_t[int(no)].get("original_value"), v
        con.execute("UPDATE promotion_tier SET original_value=? WHERE tier_id=?", (v, known_t[int(no)]["tier_id"]))
    for seq, v in (rates or {}).items():
        if int(seq) not in known_r:
            raise ValueError(f"없는 지급률 조각입니다: {seq}")
        v = None if v in (None, "") else float(v)
        before[f"rate{seq}"], after[f"rate{seq}"] = known_r[int(seq)].get("original_rate"), v
        con.execute("UPDATE promotion_rate SET original_rate=? WHERE promo_id=? AND seq=?", (v, promo_id, int(seq)))
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), by, "promotion_original", f"{promo_id}:{','.join(sorted(after)) or '바뀐 것 없음'}",
                 json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)))
    con.commit()
    return {"promo_id": promo_id, "changed": sorted(after)}


# ---------------------------------------------------------------- 비교
# 비교는 역할과 상관없이 같은 값을 낸다. 설계사에게 여는 일(D-19)과 원안을 보여 줄지(D-21)는 화면이 정한다.
COMPARE_UNIT = 100_000
PLAN_KO = {"distributed": "배포안", "original": "원안"}
PLAN_NOTE = "지금 금액을 적립금을 뺀 배포안으로 봅니다. 원안 금액은 메인 관리자가 넣은 것만 보이고, 없으면 원문 확인입니다."
ESTIMATE_NOTE = "금액과 구간은 모두 예상입니다. 확정은 보험사 정산을 따릅니다."
RATE_BASIS_KO = {"initial_premium": "월초보험료", "converted_premium": "월납환산보험료", "monthly_premium": "월납보험료", "paid_premium": "입금보험료",
                 "base_premium": "기본보험료", "premium": "보험료", "unspecified": "기준 표기 없음"}
SECTOR_KO = {None: "전 업권", "": "전 업권", "life": "생명보험", "nonlife": "손해보험"}


def _money(p) -> bool:
    return p["metric"] != "policy_count"


def _tier_amount(t):
    """그 구간의 문턱에 딱 닿았을 때의 예상 보상(원). 여행과 물품, 원안이 빈 구간은 금액이 없어 None 이다."""
    if t.get("missing"):
        return None
    if t["reward_kind"] == "fixed":
        return t["reward_value"]
    if t["reward_kind"] == "rate":
        return (t["reward_value"] or 0) * t["min_value"]
    return None


def rule_conditions(p) -> list:
    """조건 확장을 말로 옮긴 글. conditions() 가 붙여 쓰고 화면도 따로 쓸 수 있다."""
    r, out = _r(p), []
    if r["count_basis"] == "application":
        out.append("청약 기준. 철회, 반송, 해지는 세지 않음")
    b = r["boost"]
    if b:
        when = (f"매월 {b['month_days'][0]}일에서 {b['month_days'][1]}일" if b["month_days"]
                else ", ".join(f"{f}에서 {t}" for f, t in b["ranges"]))
        txt = f"{when} 실적 {b['factor']:g}배 인정"
        if b["cap"] is not None:
            txt += f", 배수는 {'건마다' if b['cap_scope'] == 'contract' else '그 기간 합계'} {int(b['cap']):,}원까지"
        if b["applies_to"] == "each":
            txt += ". 창마다 달성 조건에만 적용"
        out.append(txt)
    if r["product_include"]:
        out.append(f"대상 상품 이름 {len(r['product_include'])}개")
    if r["product_exclude"]:
        out.append("제외 상품 " + ", ".join(r["product_exclude"]))
    if r["exclude_self"]:
        out.append("본인 계약 제외")
    if r["drop_statuses"]:
        ko = {"applied": "청약", "inforce": "정상", "lapsed": "실효", "cancelled": "해지", "matured": "만기"}
        out.append("세지 않는 상태 " + ", ".join(ko[s] for s in r["drop_statuses"]))
    if r["parent_promo_id"]:
        out.append("연결 프로모션 달성자 대상" + (f", 직급 {', '.join(r['target_levels'])}" if r["target_levels"] else ""))
    if r["gate_min_achievers"]:
        out.append(f"조직 달성 인원 {r['gate_min_achievers']}명 이상일 때 열림")
    if r["retention_min"]:
        out.append(f"13회차 유지율 {r['retention_min'] * 100:g}% 이상" + (f", {r['retention_ref']} 기준" if r["retention_ref"] else ""))
    return out


def conditions(p, cat_names=None) -> list:
    """비교 표에 나란히 놓을 조건 글. 원문을 바꾸지 않고 칸의 값을 말로 옮긴다. cat_names 는 {구분 코드: 이름}."""
    out = []
    if p["insurer_name"]:
        out.append(f"보험사 {p['insurer_name']}")
    out.append(f"업권 {SECTOR_KO.get(p['sector'], p['sector'])}")
    if p["category_list"]:
        out.append("구분 " + ", ".join((cat_names or {}).get(c, c) for c in p["category_list"]))
    out.append(f"실적 {METRIC_KO[p['metric']]}, 기준일 {BASIS_KO[p['date_basis']]}")
    if len(p["windows"]) > 1:
        tail = {"all": ". 낮은 쪽 구간 적용, 끝난 창을 놓치면 무효", "both": ". 두 조건의 낮은 쪽 구간 적용", "either": ". 두 조건의 높은 쪽 구간 적용"}
        out.append(f"기간 창 {len(p['windows'])}개, {COMBINE_KO[p['combine_rule']]}" + tail.get(p["combine_rule"], ""))
    out += rule_conditions(p)
    if p["per_contract_cap"]:
        out.append(f"건당 {int(p['per_contract_cap']):,}원까지 인정")
    if p["pay_timing"]:
        out.append(f"지급 {timing_ko(p['pay_timing'])}" if timing_ko(p["pay_timing"]) else NO_TIMING)
    if p["pay_timing"] and "inst13" in p["pay_timing"]:
        out.append("13회차 유지 시 지급")
    return out


def _plan(p, unit) -> dict:
    """금액 한 벌(배포안 또는 원안)을 같은 기준에 놓는다. {per_unit, per_unit_entry, parts, tiers, comparable, note, per_unit_basis}"""
    plan = {"per_unit": None, "per_unit_entry": None, "parts": [], "tiers": [], "comparable": False, "note": None,
            "per_unit_basis": f"{METRIC_KO[p['metric']]} {unit:,}원당"}
    if p["kind"] == "rate":
        plain = [r for r in p["rates"] if not r["option_label"]]
        if p["rates"] and len(plain) == len(p["rates"]):
            plan["parts"] = [{"rate": r["rate"], "basis": r["basis"], "basis_ko": RATE_BASIS_KO.get(r["basis"], r["basis"]),
                              "timing_ko": timing_ko(r["pay_timing"]) or NO_TIMING,
                              "per_unit": None if r["rate"] is None else r["rate"] * unit, "missing": bool(r.get("missing"))} for r in plain]
            bases = {RATE_BASIS_KO.get(r["basis"], r["basis"]) for r in plain}
            plan["per_unit_basis"] = f"{' · '.join(sorted(bases))} {unit:,}원당"
            if all(r["rate"] is not None for r in plain):
                plan["per_unit"] = plan["per_unit_entry"] = sum(r["rate"] for r in plain) * unit
                plan["comparable"] = True
            else:
                plan["note"] = f"원안 지급률이 빈 조각이 있습니다. {MISSING_ORIGINAL}"
        else:
            plan["note"] = "조건별 지급률이라 숫자 하나로 견주지 않습니다. 원문 확인"
    else:
        for t in p["tiers"]:
            amt = _tier_amount(t)
            per = amt / t["min_value"] * unit if (amt is not None and _money(p) and t["min_value"] > 0) else None
            plan["tiers"].append({"tier_no": t["tier_no"], "min_value": t["min_value"], "min_text": fmt_value(t["min_value"], p),
                                  "reward_kind": t["reward_kind"], "reward": MISSING_ORIGINAL if t.get("missing") else reward_text(t),
                                  "amount": amt, "per_unit": per, "missing": bool(t.get("missing"))})
        pers = [t["per_unit"] for t in plan["tiers"] if t["per_unit"] is not None]
        if pers:
            plan["per_unit"] = max(pers)
            plan["per_unit_entry"] = plan["tiers"][0]["per_unit"] if plan["tiers"] else None
            plan["comparable"] = True
        elif not _money(p):
            plan["note"] = "건수 기준이라 보험료로 견주지 않습니다. 구간과 보상을 보세요"
        else:
            plan["note"] = "여행이나 물품 보상이라 금액으로 견주지 않습니다. 구간과 보상을 보세요"
    return plan


def compare_row(p, unit=COMPARE_UNIT, cat_names=None) -> dict:
    """프로모션 하나를 같은 기준에 놓는다. 기준은 실적 unit 원(기본 10만 원)당 예상 보상이다.

    - 정률 시책: 지급률의 합 × unit. 조건별 지급률이면 숫자 하나로 줄이지 않고 None 이다
    - 구간 시상: 구간마다 문턱 실적에 딱 닿았을 때의 보상 ÷ 문턱 × unit. per_unit 은 그 가운데 가장 높은 값,
      per_unit_entry 는 첫 구간의 값이다. 여행과 물품, 건수 기준(가동 시상)은 보험료로 견주지 않아 None 이다
    금액은 plan 아래 distributed(배포안)와 original(원안)로 나눈다. 견주는 값(per_unit)은 배포안이다.
    원안은 원안 칸이 하나라도 있을 때만 같은 꼴로 채우고, 원안이 빈 구간과 조각은 missing 이며 글은 '원문 확인'이다.
    원안 칸이 하나도 없으면 original 은 None 이다 (D-21 가정).
    """
    row = {k: p[k] for k in ("promo_id", "period_ym", "name", "kind", "kind_ko", "insurer_name", "sector", "category_list", "metric", "metric_ko",
                             "unit", "date_from", "date_to", "recipient_level", "pay_timing", "timing_ko", "per_contract_cap", "combine_rule", "caution")}
    row.update(sector_ko=SECTOR_KO.get(p["sector"], p["sector"]), windows=[{"label": w["label"], "date_from": w["date_from"], "date_to": w["date_to"]} for w in p["windows"]],
               conditions=conditions(p, cat_names), compare_unit=unit, estimate=True, estimate_note=ESTIMATE_NOTE, plan_assumed=True, plan_note=PLAN_NOTE,
               basis=basis_note(p))
    dist = _plan(p, unit)
    op = _original_view(p)
    orig = _plan(op, unit) if op else None
    row.update(comparable=dist.pop("comparable"), note=dist.pop("note"), per_unit_basis=dist.pop("per_unit_basis"), has_original=orig is not None)
    if orig:
        orig.pop("per_unit_basis")
    row["plan"] = {"distributed": dist, "original": orig}
    row["per_unit"], row["per_unit_entry"] = dist["per_unit"], dist["per_unit_entry"]
    return row


def compare(con, ym, *, ref: date | None = None, unit=COMPARE_UNIT, recipient_level="fc", include_ended=False, promo_ids=None) -> dict:
    """같은 기간에 걸친 게시 중 프로모션을 같은 기준으로 견준다. 역할과 상관없이 같은 값을 낸다.

    recipient_level 은 수령 계층이다. 기본은 설계사 몫(fc)이다. 총괄과 지사 몫(D-18)을 넣을지는 부르는 쪽이 정한다.
    include_ended 가 거짓이면 기준일에 끝난 프로모션은 뺀다. promo_ids 를 주면 그것만 견준다.

    돌려주는 것: {ref, unit, rows, groups, estimate_note, plan_note}
    - rows: compare_row() 의 줄에 phase(active / upcoming / ended), days_left 를 붙인 것. 10만 원당 예상 보상이 큰 순이고
      견줄 수 없는 줄은 뒤에 둔다. plan.original 은 원안 칸이 있을 때만 채운다. 원안을 보여 줄지는 화면이 정한다
    - groups: 실적 기준(metric)이 같은 줄끼리. 보험료와 인정보험료와 건수는 서로 견주지 않는다
    """
    ref = ref or ref_date(ym)
    cat_names = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM product_category")}
    rows = []
    for p in listing(con, ym, "main"):                         # 게시된 것만. 수령 계층은 아래에서 고른다
        if recipient_level and p["recipient_level"] != recipient_level:
            continue
        if promo_ids is not None and p["promo_id"] not in set(promo_ids):
            continue
        f, t = date.fromisoformat(p["date_from"]), date.fromisoformat(p["date_to"])
        phase = "upcoming" if ref < f else ("ended" if ref > t else "active")
        if phase == "ended" and not include_ended:
            continue
        rows.append({**compare_row(p, unit, cat_names), "phase": phase, "days_left": max((t - ref).days, 0) if phase != "ended" else 0})
    rows.sort(key=lambda r: (r["per_unit"] is None, -(r["per_unit"] or 0), r["date_to"], r["promo_id"]))
    groups = []
    for metric in METRIC_KO:
        g = [r for r in rows if r["metric"] == metric]
        if g:
            groups.append({"metric": metric, "metric_ko": METRIC_KO[metric], "rows": g})
    return {"ref": ref.isoformat(), "unit": unit, "rows": rows, "groups": groups, "estimate_note": ESTIMATE_NOTE, "plan_note": PLAN_NOTE}


# ---------------------------------------------------------------- 지난 프로모션과 확정
# 확정 전에는 결과가 마감 전 예상(estimate)이다. 메인 관리자가 confirm_results() 로 확정하면 그때의 값을 promotion_result 에 남기고,
# 그 뒤에 final_results() 는 다시 계산하지 않고 남긴 값(confirmed)을 낸다. 무르면(revoke_confirmation) 다시 마감 전 예상으로 돌아간다.
SETTLE_KO = {"estimate": "마감 전 예상", "confirmed": "확정"}
OUTCOME_KO = {"achieved": "달성", "missed": "미달성"}
CONFIRMED_NOTE = "메인 관리자가 확정한 결과입니다. 지급은 보험사 정산을 따릅니다."
REVOKED_NOTE = "확정을 무른 뒤 다시 계산한 마감 전 예상입니다. 다시 확정하면 그때의 값이 남습니다."
REVOKE_MIN = 5                                                 # 무르는 사유의 최소 글자 수 (D-50 가정)
RANK_NOTE = "순위는 확정할 때의 재직자 전체에서 실적 큰 순입니다. 같은 실적은 같은 순위입니다."


def _eligible(con, agent_ids) -> list:
    """달성 대상. 재직자만이다. 그 달 중간에 퇴사한 사람은 프로모션 달성 대상에서 뺀다 (D-42). 달성 현황 화면과 같은 기준이다."""
    ids = sorted(set(agent_ids or ()))
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return [r["agent_id"] for r in con.execute(f"SELECT agent_id FROM agent WHERE agent_id IN ({q}) AND status='active' ORDER BY agent_id", ids)]


def _everyone(con) -> list:
    """확정과 순위의 모집단. 지금의 재직자 모두다 (D-42)."""
    return [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' ORDER BY agent_id")]


def ended(con, *, ym=None, ref: date | None = None, recipient_level="fc") -> list:
    """기준일 앞에서 모든 창이 끝난 게시 프로모션. ym 을 주면 그 달에 걸친 것만(월별 보관), 없으면 전부다. 늦게 끝난 것부터."""
    ref = ref or date.today()
    if ym:
        items = listing(con, ym, "main")
    else:
        items = [_load(con, r["promo_id"]) for r in con.execute("SELECT promo_id FROM promotion WHERE status='published' ORDER BY promo_id")]
    items = [p for p in items if (not recipient_level or p["recipient_level"] == recipient_level) and date.fromisoformat(p["date_to"]) < ref]
    return sorted(items, key=lambda p: (p["date_to"], p["promo_id"]), reverse=True)


def original_amount(p, r) -> float | None:
    """progress() 의 결과 하나에 원안을 대입한 금액. 원안 칸이 없거나 그 구간의 원안이 비었으면 None 이다."""
    op = _original_view(p)
    if not op:
        return None
    total = r["value"]
    if p["kind"] == "rate":
        plain = [x for x in op["rates"] if not x["option_label"]]
        if not plain or len(plain) != len(op["rates"]) or any(x["rate"] is None for x in plain):
            return None
        return sum(x["rate"] for x in plain) * total
    if not r["tier"]:
        return None
    t = next((x for x in op["tiers"] if x["tier_no"] == r["tier"]["tier_no"]), None)
    if not t or t["missing"]:
        return None
    if t["reward_kind"] == "fixed":
        return t["reward_value"]
    if t["reward_kind"] == "rate":
        return (t["reward_value"] or 0) * total
    return None


def confirmation(con, promo_id) -> dict | None:
    """살아 있는 확정 한 줄. 없거나 확정 표가 없는 오래된 DB 면 None 이다."""
    try:
        row = con.execute("SELECT * FROM promotion_confirm WHERE promo_id=? AND voided_at IS NULL ORDER BY confirm_id DESC LIMIT 1", (promo_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def confirm_history(con, promo_id) -> list:
    """확정과 무르기의 기록. 새것부터. 메인 관리자 화면이 쓴다. 무른 확정도 지우지 않고 남아 있다.
    [{confirm_id, ref_date, n_rows, confirmed_by, confirmed_at, live, revoked_by, revoked_at, reason}]"""
    try:
        rows = con.execute("SELECT * FROM promotion_confirm WHERE promo_id=? ORDER BY confirm_id DESC", (promo_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"confirm_id": r["confirm_id"], "ref_date": r["ref_date"], "n_rows": r["n_rows"], "confirmed_by": r["confirmed_by"],
             "confirmed_at": r["confirmed_at"], "live": r["voided_at"] is None, "revoked_by": r["voided_by"], "revoked_at": r["voided_at"],
             "reason": r["void_reason"]} for r in rows]


def _who(con, ids, ym) -> dict:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r["agent_id"]: dict(r) for r in con.execute(
        f"""SELECT a.agent_id, a.internal_code code, a.name, o.name org FROM agent a
            LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
            LEFT JOIN org_unit o ON o.org_id=h.org_id WHERE a.agent_id IN ({q})""", (ym, ym, *ids))}


def _summary(rows) -> dict:
    origs = [x["original_amount"] for x in rows if x["original_amount"] is not None]
    return {"n": len(rows), "achieved": sum(1 for x in rows if x["outcome"] == "achieved"),
            "missed": sum(1 for x in rows if x["outcome"] == "missed"), "amount": sum(x["amount"] or 0 for x in rows),
            "original_amount": sum(origs) if origs else None}


def _rank(values: dict) -> dict:
    """{agent_id: 실적} 을 {agent_id: 순위}로. 큰 순이고 같은 값은 같은 순위, 다음 사람은 그만큼 밀린다. 0 이하는 None 이다."""
    order = sorted((v for v in values.values() if v > 0), reverse=True)
    first = {}
    for i, v in enumerate(order, start=1):
        first.setdefault(v, i)
    return {a: (first[v] if v > 0 else None) for a, v in values.items()}


def _estimate(con, p, agent_ids, ref: date) -> list:
    """범위 안 재직자의 마감 전 예상. 순위는 재직자 전체에서 매겨 범위와 상관없이 같다."""
    ids = _eligible(con, agent_ids)
    pool = _everyone(con)
    res = progress(con, p, sorted(set(pool) | set(ids)), ref)
    ranks = _rank({a: res[a]["value"] for a in pool})
    who = _who(con, ids, date.fromisoformat(p["date_to"]).strftime("%Y%m"))
    rows = []
    for a in ids:
        r = res[a]
        if r.get("target") is False:                           # 딸린 시책의 대상 직급이 아닌 사람
            continue
        ok = (r["value"] > 0) if p["kind"] == "rate" else (r["status"] == "achieved")
        rows.append({**who.get(a, {"agent_id": a, "code": None, "name": None, "org": None}), "value": r["value"],
                     "tier_no": r["tier"]["tier_no"] if r["tier"] else None, "reward": r["reward"], "amount": r["amount"],
                     "original_amount": original_amount(p, r), "rank": ranks.get(a),
                     "outcome": "achieved" if ok else "missed", "outcome_ko": OUTCOME_KO["achieved" if ok else "missed"],
                     "settlement": "estimate", "settlement_ko": SETTLE_KO["estimate"], "r": r})
    rows.sort(key=lambda x: (-x["value"], x["code"] or ""))
    return rows


def _confirmed_rows(con, p, head, agent_ids) -> list:
    ids = sorted(set(agent_ids or ()))
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    who = _who(con, ids, date.fromisoformat(p["date_to"]).strftime("%Y%m"))
    rows = []
    for x in con.execute(f"SELECT * FROM promotion_result WHERE confirm_id=? AND agent_id IN ({q})", (head["confirm_id"], *ids)):
        a = x["agent_id"]
        rows.append({**who.get(a, {"agent_id": a, "code": None, "name": None, "org": None}), "value": x["value"], "tier_no": x["tier_no"],
                     "reward": x["reward"], "amount": x["amount"], "original_amount": x["original_amount"],
                     "rank": x["rank_no"] if "rank_no" in x.keys() else None,
                     "outcome": x["outcome"], "outcome_ko": OUTCOME_KO[x["outcome"]],
                     "settlement": "confirmed", "settlement_ko": SETTLE_KO["confirmed"], "r": None})
    rows.sort(key=lambda x: (-x["value"], x["code"] or ""))
    return rows


def final_results(con, p, agent_ids, ref: date | None = None) -> dict:
    """끝난 프로모션 하나의 사람별 최종 달성 결과. agent_ids 는 범위 안 설계사 목록이다.

    돌려주는 것: {promo, settlement, settlement_ko, estimate_note, basis, confirmed_by, confirmed_at, revoked, rank_note, rows, summary}
    - 확정 전(settlement=estimate): 값은 달성 현황 계산(progress)을 그대로 쓴다. 재직자만 대상이다 (D-42)
    - 확정 뒤(settlement=confirmed): 다시 계산하지 않고 confirm_results() 가 남긴 값을 낸다. 대상은 확정할 때의 재직자다
    - revoked: 살아 있는 확정이 없고 무른 확정이 있으면 가장 최근에 무른 것 {confirm_id, revoked_by, revoked_at, reason}. 아니면 None.
      이때 값은 다시 계산한 마감 전 예상이고 estimate_note 가 그렇게 말한다. 사유는 메인 관리자 화면에서만 보인다
    - rows: [{agent_id, code, name, org, value, tier_no, reward, amount, original_amount, rank, outcome, outcome_ko, settlement, settlement_ko, r}]
      값 큰 순. r 은 확정 전에는 progress() 의 결과, 확정 뒤에는 None 이다. rank 는 재직자 전체에서의 순위다 (RANK_NOTE).
      amount 는 배포안 금액이고 관리자 화면에서는 가린다 (D-17). original_amount 는 원안 금액이고 메인 관리자에게만 보인다 (D-21)
    - outcome: 구간 시상은 끝난 뒤 구간이 있으면 달성. 정률 시책은 대상 실적이 있으면 달성이다
    - summary: {n, achieved, missed, amount, original_amount}
    """
    ref = ref or date.today()
    end = date.fromisoformat(p["date_to"])
    if ref <= end:
        raise ValueError("아직 끝나지 않은 프로모션입니다.")
    head = confirmation(con, p["promo_id"])
    if head:
        rows = _confirmed_rows(con, p, head, agent_ids)
        return {"promo": p, "settlement": "confirmed", "settlement_ko": SETTLE_KO["confirmed"], "estimate_note": CONFIRMED_NOTE,
                "basis": head["basis_note"] or basis_note(p), "confirmed_by": head["confirmed_by"], "confirmed_at": head["confirmed_at"],
                "revoked": None, "rank_note": RANK_NOTE, "rows": rows, "summary": _summary(rows)}
    gone = next((h for h in confirm_history(con, p["promo_id"]) if not h["live"]), None)
    revoked = {k: gone[k] for k in ("confirm_id", "revoked_by", "revoked_at", "reason")} if gone else None
    rows = _estimate(con, p, agent_ids, ref)
    return {"promo": p, "settlement": "estimate", "settlement_ko": SETTLE_KO["estimate"],
            "estimate_note": f"{REVOKED_NOTE} {ESTIMATE_NOTE}" if revoked else ESTIMATE_NOTE,
            "basis": basis_note(p), "confirmed_by": None, "confirmed_at": None, "revoked": revoked, "rank_note": RANK_NOTE,
            "rows": rows, "summary": _summary(rows)}


def _rule(p) -> dict:
    """확정할 때의 규칙. 13회차 판정이 이 규칙으로 다시 계산한다."""
    keep_t = ("tier_no", "min_value", "reward_kind", "reward_value", "reward_label", "original_value", "window_seq")
    keep_r = ("seq", "option_label", "basis", "rate", "pay_timing", "original_rate")
    return {"kind": p["kind"], "combine_rule": p["combine_rule"], "metric": p["metric"],
            "windows": [{k: w[k] for k in ("seq", "label", "date_from", "date_to")} for w in p["windows"]],
            "tiers": [{k: t.get(k) for k in keep_t} for t in p["tiers"]],
            "window_tiers": {str(seq): [{k: t.get(k) for k in keep_t} for t in rows] for seq, rows in p["window_tiers"].items()},
            "rates": [{k: r.get(k) for k in keep_r} for r in p["rates"]], "rules": _r(p)}


def _with_rule(p, rule_json) -> dict:
    """확정할 때의 규칙을 끼운 프로모션. 규칙이 없는 예전 확정이면 지금 규칙 그대로다."""
    if not rule_json:
        return p
    rule = json.loads(rule_json)
    return {**p, "kind": rule["kind"], "combine_rule": rule["combine_rule"], "metric": rule["metric"], "windows": rule["windows"],
            "tiers": rule["tiers"], "window_tiers": {int(k): v for k, v in rule["window_tiers"].items()}, "rates": rule["rates"],
            "rules": rule.get("rules") or _r(p)}


def confirm_results(con, promotion_id, by, ref: date | None = None) -> dict:
    """끝난 프로모션의 사람별 결과를 확정해 남긴다. 메인 관리자만 부른다. by 는 login_id 이거나 auth.context() 의 결과다.

    대상은 확정하는 때의 재직자 모두다 (D-42). 값은 그때의 final_results() 예상과 같다. 이미 확정했으면 막는다.
    무른 뒤에는 다시 확정할 수 있고, 새 확정 줄이 생긴다. 사람마다 순위(rank_no), 센 계약(promotion_result_contract),
    그때의 규칙(rule_json)을 함께 남긴다. 여행과 물품 시상도 같은 함수로 달성자 명단을 확정한다. 금액 칸은 비고 보상 글과 순위가 남는다.
    돌려주는 것: 확정 뒤의 final_results() 다. 메인 관리자 범위라 모든 대상이 들어 있다.
    """
    by = _require_main(con, by)
    p = _load(con, promotion_id)
    if not p:
        raise ValueError("없는 프로모션입니다.")
    if p["status"] != "published":
        raise ValueError("게시한 프로모션만 확정합니다.")
    ref = ref or date.today()
    if ref <= date.fromisoformat(p["date_to"]):
        raise ValueError("아직 끝나지 않은 프로모션입니다.")
    ensure(con)
    if confirmation(con, promotion_id):
        raise ValueError("이미 확정한 프로모션입니다.")
    everyone = _everyone(con)
    rows = _estimate(con, p, everyone, ref)
    at = db.now()
    cid = con.execute("""INSERT INTO promotion_confirm(promo_id, ref_date, date_basis, basis_note, n_rows, confirmed_by, confirmed_at, rule_json)
                         VALUES(?,?,?,?,?,?,?,?)""", (promotion_id, ref.isoformat(), p["date_basis"], basis_note(p), len(rows), by, at,
                                                      json.dumps(_rule(p), ensure_ascii=False))).lastrowid
    con.executemany("""INSERT INTO promotion_result(confirm_id, promo_id, agent_id, value, tier_no, reward, amount, original_amount, outcome, rank_no)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    [(cid, promotion_id, x["agent_id"], x["value"], x["tier_no"], x["reward"], x["amount"], x["original_amount"], x["outcome"], x["rank"])
                     for x in rows])
    con.executemany("""INSERT INTO promotion_result_contract(confirm_id, promo_id, agent_id, contract_id, window_seq, value, value_each)
                       VALUES(?,?,?,?,?,?,?)""",
                    [(cid, promotion_id, c["agent_id"], c["contract_id"], c["window_seq"], c["value"], c["value_each"])
                     for c in _contract_rows(con, p, [x["agent_id"] for x in rows])])
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)",
                (at, by, "promotion_confirm", f"{promotion_id}:{len(rows)}명"))
    con.commit()
    return final_results(con, p, everyone, ref)


def revoke_confirmation(con, promotion_id, by, reason) -> dict:
    """확정을 무른다 (D-50 제안대로 둔 가정). 메인 관리자만 하고 사유를 REVOKE_MIN 자 이상 적어야 한다.

    확정 줄과 결과 줄은 지우지 않는다. promotion_confirm 의 voided_at, voided_by, void_reason 만 채운다.
    무른 뒤 final_results() 는 다시 계산한 마감 전 예상을 내고, confirm_results() 로 다시 확정할 수 있다.
    그 확정에 달린 13회차 판정은 함께 보이지 않는다. 변경 기록에는 사유의 길이만 남긴다.
    돌려주는 것: {promo_id, confirm_id, revoked_by, revoked_at, reason}
    """
    by = _require_main(con, by)
    reason = " ".join((reason or "").split())
    if len(reason) < REVOKE_MIN:
        raise ValueError(f"무르는 사유를 {REVOKE_MIN}자 이상 적어야 합니다.")
    head = confirmation(con, promotion_id)
    if not head:
        raise ValueError("무를 확정이 없습니다.")
    at = db.now()
    con.execute("UPDATE promotion_confirm SET voided_by=?, voided_at=?, void_reason=? WHERE confirm_id=? AND voided_at IS NULL",
                (by, at, reason, head["confirm_id"]))
    con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)",
                (at, by, "promotion_revoke", f"{promotion_id}:{head['confirm_id']}", json.dumps({"reason_len": len(reason)})))
    con.commit()
    return {"promo_id": promotion_id, "confirm_id": head["confirm_id"], "revoked_by": by, "revoked_at": at, "reason": reason}


def past(con, agent_ids, *, ym=None, ref: date | None = None, recipient_level="fc") -> list:
    """지난 프로모션 목록과 사람별 최종 결과. agent_ids 는 범위 안 설계사 목록이다. [final_results(), ...] 늦게 끝난 것부터."""
    ref = ref or date.today()
    return [final_results(con, p, agent_ids, ref) for p in ended(con, ym=ym, ref=ref, recipient_level=recipient_level)]


# ---------------------------------------------------------------- 여행 시상 명단 (D-20)
# D-20 은 열린 결정이다. 제안대로 달성자 명단은 본인, 직속 관리자, 메인 관리자만 본다. 가정이다.
ROSTER_NOTE = "달성자 명단은 본인, 직속 관리자, 메인 관리자만 봅니다."


def roster_viewable(con, ctx, agent_ids, ym) -> list:
    """범위 안 설계사 목록 가운데 이 사람이 여행 시상 명단에서 볼 수 있는 사람 (D-20 가정).
    메인 관리자는 범위 전부, 관리자는 본인과 ym 달에 자기가 직속 관리자인 사람, 설계사는 본인만이다. ctx 는 auth.context() 의 결과다."""
    ids = sorted(set(agent_ids or ()))
    me = ctx.get("agent_id")
    if ctx.get("role") == "main":
        return ids
    if ctx.get("role") == "manager" and me:
        return [a for a in ids if a == me or me in orgs.direct_heads(con, a, ym)]
    return [a for a in ids if me and a == me]


def travel_roster(con, p, ctx, agent_ids, ref: date | None = None) -> dict:
    """여행처럼 보상이 물품이나 행사인 프로모션의 달성자 명단. agent_ids 는 범위 안 설계사 목록이고, 그 가운데 D-20 으로 볼 수 있는 사람만 남긴다.

    끝난 프로모션은 final_results() 를 따른다. 확정했으면 확정한 명단, 아니면 마감 전 예상이다. 진행 중이면 지금까지 구간에 닿은 사람의 예상이다.
    달성자는 구간(tier_no)이 있는 사람이다. 순위는 재직자 전체에서의 순위라 보는 사람과 상관없이 같다.
    돌려주는 것: {promo, phase, settlement, settlement_ko, estimate_note, roster_note, rank_note, confirmed_by, confirmed_at, rows, n}
    rows: [{agent_id, code, name, org, value, tier_no, reward, rank, outcome, outcome_ko, settlement, settlement_ko}] 순위 순.
    금액 칸은 넣지 않는다. 여행과 물품은 금액이 없다.
    """
    if p["kind"] == "rate":
        raise ValueError("정률 시책에는 달성자 명단이 없습니다.")
    ref = ref or date.today()
    end = date.fromisoformat(p["date_to"])
    ym = min(ref, end).strftime("%Y%m")
    ids = roster_viewable(con, ctx, agent_ids, ym)
    if ref > end:
        res = final_results(con, p, ids, ref)
        phase, rows = "ended", res["rows"]
        head = {k: res[k] for k in ("settlement", "settlement_ko", "estimate_note", "confirmed_by", "confirmed_at")}
    else:
        phase, rows = "active", _estimate(con, p, ids, ref)
        head = {"settlement": "estimate", "settlement_ko": "진행 중 예상", "estimate_note": ESTIMATE_NOTE, "confirmed_by": None, "confirmed_at": None}
    keep = ("agent_id", "code", "name", "org", "value", "tier_no", "reward", "rank", "outcome", "outcome_ko", "settlement", "settlement_ko")
    got = [{k: x.get(k) for k in keep} for x in rows if x["tier_no"] is not None]
    got.sort(key=lambda x: (x["rank"] or 10 ** 9, x["code"] or ""))
    return {"promo": p, "phase": phase, **head, "roster_note": ROSTER_NOTE, "rank_note": RANK_NOTE, "rows": got, "n": len(got)}


# ---------------------------------------------------------------- 13회차 확정
# 결과 확정 때 센 계약마다 13회차 유지를 persist.inst13_judge 로 가른다. 보상 전체를 13회차 유지에 거는 것으로 본다 (가정).
# 실효 계약을 빼고 확정 때의 규칙으로 다시 계산한다. 구간이나 금액이 줄거나 미달성이 되면 환수, 그대로면 지급, 판정 전 계약이 있으면 판정 전이다.
DECISION_KO = {"pay": "지급", "clawback": "환수", "pending": "판정 전"}
INST13_NOTE = ("13회차 유지 조건은 보상 전체에 거는 것으로 봅니다. 실효 계약을 빼고 다시 계산해 구간이나 금액이 줄면 환수, 그대로면 지급입니다. "
               "판정 전 계약이 있으면 판정 전이고 금액은 예상입니다. 지급과 환수는 보험사 정산을 따릅니다.")
_EPS = 1e-6


def _tails(con, contract_ids) -> dict:
    ids = sorted(set(contract_ids))
    out = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = ",".join("?" * len(part))
        out.update({r["contract_id"]: (r["policy_no"][-4:] if r["policy_no"] else None)
                    for r in con.execute(f"SELECT contract_id, policy_no FROM contract WHERE contract_id IN ({q})", part)})
    return out


def _inst13_live(rule_p, base, contracts, judg) -> dict:
    """한 사람의 13회차 판정을 다시 계산한다. base 는 promotion_result 한 줄, contracts 는 그 사람이 확정 때 센 계약이다."""
    seq_ix = {w["seq"]: i for i, w in enumerate(rule_p["windows"])}
    per = [0.0] * len(rule_p["windows"])
    each = [0.0] * len(rule_p["windows"])
    items, n = [], {"kept": 0, "lapsed": 0, "pending": 0}
    for c in contracts:
        j = judg.get(c["contract_id"]) or persist.judge13(None, None, None, None, date.today())
        n[j["judgement"]] += 1
        if j["judgement"] != "lapsed" and c["window_seq"] in seq_ix:
            per[seq_ix[c["window_seq"]]] += c["value"]
            each[seq_ix[c["window_seq"]]] += c["value"] if c.get("value_each") is None else c["value_each"]
        items.append({"contract_id": c["contract_id"], "window_seq": c["window_seq"], "value": c["value"], **j})
    r = _judge(rule_p, each, [{"state": "ended", "days_left": 0}] * len(per), None, per)
    tier_kept = r["tier"]["tier_no"] if r["tier"] else None
    ok = (r["value"] > 0) if rule_p["kind"] == "rate" else (r["status"] == "achieved")
    lost = ((tier_kept or 0) < (base["tier_no"] or 0) or (r["amount"] or 0) < (base["amount"] or 0) - _EPS or not ok)
    decision = "pending" if n["pending"] else ("clawback" if lost else "pay")
    return {"decision": decision, "value_kept": r["value"], "tier_no_kept": tier_kept, "reward_kept": r["reward"], "amount_kept": r["amount"],
            "original_amount_kept": original_amount(rule_p, r), "outcome_kept": "achieved" if ok else "missed",
            "n_kept": n["kept"], "n_lapsed": n["lapsed"], "n_pending": n["pending"], "contracts": items}


def inst13_results(con, p, agent_ids, ref: date | None = None) -> dict:
    """13회차 유지 조건이 붙은 프로모션의 사람별 지급과 환수 판정. agent_ids 는 범위 안 설계사 목록이다.

    결과를 확정한 뒤에만 판정한다. 대상은 확정 결과에서 달성한 사람이다. 확정 때 센 계약으로 판정하고, 그 기록이 없는 예전 확정은 지금 원장의 계약으로 판정한다.
    confirm_inst13() 으로 남긴 사람은 남긴 값(settlement=confirmed)을, 아닌 사람은 기준일로 다시 판정한 값(settlement=estimate)을 낸다.
    돌려주는 것: {promo, available, note, basis_note, judge_note, ref, confirm_id, contracts_from, rows, summary}
    - available: 결과 확정이 없으면 False 이고 rows 는 비어 있다
    - rows: [{agent_id, code, name, org, value, tier_no, amount, original_amount, decision, decision_ko, value_kept, tier_no_kept, reward_kept,
              amount_kept, original_amount_kept, clawback_amount, outcome_kept, n_kept, n_lapsed, n_pending, settlement, settlement_ko,
              judged_by, judged_at, contracts}]
      contracts: [{contract_id, policy_tail, window_seq, value, due_date, judgement, judgement_ko, reason, reason_ko, paid_ym}]
      clawback_amount 는 환수일 때 확정 금액에서 다시 계산한 금액을 뺀 값이다. 판정 전이면 None 이다. 금액은 관리자 화면에서 가린다 (D-17)
    - summary: {n, pay, clawback, pending, amount, amount_kept, clawback_amount}
    """
    if not p["inst13"]:
        raise ValueError("13회차 조건이 없는 프로모션입니다.")
    ref = ref or date.today()
    out = {"promo": p, "available": False, "note": INST13_NOTE, "judge_note": persist.INST13_NOTE, "ref": ref.isoformat(), "confirm_id": None,
           "contracts_from": None, "rows": [], "summary": {"n": 0, "pay": 0, "clawback": 0, "pending": 0, "amount": 0, "amount_kept": 0, "clawback_amount": 0}}
    head = confirmation(con, p["promo_id"])
    ids = sorted(set(agent_ids or ()))
    if not head:
        out["note"] = "결과를 먼저 확정해야 13회차를 판정합니다."
        return out
    ensure(con)
    out.update(available=True, confirm_id=head["confirm_id"])
    if not ids:
        return out
    cid = head["confirm_id"]
    q = ",".join("?" * len(ids))
    base = {r["agent_id"]: dict(r) for r in con.execute(
        f"SELECT * FROM promotion_result WHERE confirm_id=? AND outcome='achieved' AND agent_id IN ({q})", (cid, *ids))}
    frozen = {r["agent_id"]: dict(r) for r in con.execute(f"SELECT * FROM promotion_inst13 WHERE confirm_id=? AND agent_id IN ({q})", (cid, *ids))}
    frozen_c = {}
    if frozen:
        fq = ",".join("?" * len(frozen))
        for r in con.execute(f"SELECT * FROM promotion_inst13_contract WHERE inst13_id IN ({fq}) ORDER BY id", [f["inst13_id"] for f in frozen.values()]):
            frozen_c.setdefault(r["inst13_id"], []).append(dict(r))
    open_ids = sorted(set(base) - set(frozen))
    snap = con.execute("SELECT 1 FROM promotion_result_contract WHERE confirm_id=? LIMIT 1", (cid,)).fetchone()
    out["contracts_from"] = "confirm" if snap else "ledger"
    contracts = {}
    if open_ids:
        oq = ",".join("?" * len(open_ids))
        got = ([dict(r) for r in con.execute(f"SELECT agent_id, contract_id, window_seq, value, value_each FROM promotion_result_contract WHERE confirm_id=? AND agent_id IN ({oq})",
                                            (cid, *open_ids))] if snap else _contract_rows(con, p, open_ids))
        for c in got:
            contracts.setdefault(c["agent_id"], []).append(c)
    judg = persist.inst13_judge(con, [c["contract_id"] for cs in contracts.values() for c in cs], ref)
    rule_p = _with_rule(p, head.get("rule_json"))
    who = _who(con, sorted(base), date.fromisoformat(p["date_to"]).strftime("%Y%m"))
    rows = []
    for a, b in base.items():
        row = {**who.get(a, {"agent_id": a, "code": None, "name": None, "org": None}), "value": b["value"], "tier_no": b["tier_no"],
               "amount": b["amount"], "original_amount": b["original_amount"]}
        if a in frozen:
            f = frozen[a]
            cs = [{"contract_id": c["contract_id"], "window_seq": c["window_seq"], "value": c["value"], "due_date": c["due_date"], "judgement": c["judgement"],
                   "judgement_ko": persist.INST13_KO[c["judgement"]], "reason": c["reason"], "reason_ko": persist.INST13_REASON_KO[c["reason"]],
                   "paid_ym": c["paid_ym"]} for c in frozen_c.get(f["inst13_id"], [])]
            row.update(decision=f["decision"], value_kept=f["value_kept"], tier_no_kept=f["tier_no_kept"], reward_kept=None, amount_kept=f["amount_kept"],
                       original_amount_kept=f["original_amount_kept"], outcome_kept=f["outcome_kept"], n_kept=f["n_kept"], n_lapsed=f["n_lapsed"], n_pending=0,
                       settlement="confirmed", judged_by=f["judged_by"], judged_at=f["judged_at"], contracts=cs)
            t = next((x for x in rule_p["tiers"] if x["tier_no"] == f["tier_no_kept"]), None)
            row["reward_kept"] = reward_text(t) if t else None
        else:
            live = _inst13_live(rule_p, b, contracts.get(a, []), judg)
            row.update(live, settlement="estimate", judged_by=None, judged_at=None)
        row["decision_ko"] = DECISION_KO[row["decision"]]
        row["settlement_ko"] = SETTLE_KO[row["settlement"]]
        row["clawback_amount"] = ((b["amount"] or 0) - (row["amount_kept"] or 0)) if row["decision"] == "clawback" and b["amount"] is not None else None
        rows.append(row)
    tails = _tails(con, [c["contract_id"] for x in rows for c in x["contracts"]])
    for x in rows:
        for c in x["contracts"]:
            c["policy_tail"] = tails.get(c["contract_id"])
    rows.sort(key=lambda x: (-x["value"], x["code"] or ""))
    out["rows"] = rows
    out["summary"] = {"n": len(rows), **{k: sum(1 for x in rows if x["decision"] == k) for k in DECISION_KO},
                      "amount": sum(x["amount"] or 0 for x in rows),
                      "amount_kept": sum(x["amount_kept"] or 0 for x in rows if x["decision"] != "pending"),
                      "clawback_amount": sum(x["clawback_amount"] or 0 for x in rows)}
    return out


def confirm_inst13(con, promotion_id, by, ref: date | None = None) -> dict:
    """13회차 판정을 확정해 남긴다. 메인 관리자만 부른다. 결과 확정이 먼저 있어야 한다.

    지급이나 환수로 정해진 사람만 promotion_inst13 과 promotion_inst13_contract 에 남긴다. 판정 전인 사람은 남기지 않고 다음에 다시 부른다.
    한 번 남긴 사람은 (confirm_id, agent_id) 가 하나라 다시 남기지 않고 값도 바뀌지 않는다.
    돌려주는 것: 남긴 뒤의 inst13_results() 에 n_new(이번에 남긴 사람 수)를 붙인 것. 메인 관리자 범위라 모든 대상이 들어 있다.
    """
    by = _require_main(con, by)
    p = _load(con, promotion_id)
    if not p:
        raise ValueError("없는 프로모션입니다.")
    if not p["inst13"]:
        raise ValueError("13회차 조건이 없는 프로모션입니다.")
    ensure(con)
    head = confirmation(con, promotion_id)
    if not head:
        raise ValueError("결과를 먼저 확정해야 13회차를 확정합니다.")
    ref = ref or date.today()
    everyone = [r["agent_id"] for r in con.execute("SELECT agent_id FROM promotion_result WHERE confirm_id=?", (head["confirm_id"],))]
    res = inst13_results(con, p, everyone, ref)
    at, n_new = db.now(), 0
    for x in res["rows"]:
        if x["settlement"] != "estimate" or x["decision"] == "pending":
            continue
        iid = con.execute("""INSERT INTO promotion_inst13(confirm_id, promo_id, agent_id, ref_date, decision, value, value_kept, tier_no, tier_no_kept,
                                                          amount, amount_kept, original_amount, original_amount_kept, outcome_kept, n_kept, n_lapsed,
                                                          judged_by, judged_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (head["confirm_id"], promotion_id, x["agent_id"], ref.isoformat(), x["decision"], x["value"], x["value_kept"], x["tier_no"],
                           x["tier_no_kept"], x["amount"], x["amount_kept"], x["original_amount"], x["original_amount_kept"], x["outcome_kept"],
                           x["n_kept"], x["n_lapsed"], by, at)).lastrowid
        con.executemany("""INSERT INTO promotion_inst13_contract(inst13_id, contract_id, window_seq, value, due_date, judgement, reason, paid_ym)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        [(iid, c["contract_id"], c["window_seq"], c["value"], c["due_date"], c["judgement"], c["reason"], c["paid_ym"]) for c in x["contracts"]])
        n_new += 1
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (at, by, "promotion_inst13", f"{promotion_id}:{n_new}명"))
    con.commit()
    return {**inst13_results(con, p, everyone, ref), "n_new": n_new}
