"""유지율과 유지 위험, 13회차 도래. 웹 화면(유지율, 내 유지율, 일정, 알림)이 부르는 함수다.

모든 함수는 범위 안 설계사 목록(agent_ids)을 받아서만 읽는다. 계약자와 피보험자 이름은 돌려주지 않는다 (D-05).
계약을 가리킬 때는 증권번호 끝 네 자리(policy_tail)만 둔다.

유지율. 원천은 유지율 현황 파일을 실은 persistency 표다. 원수사별 유지 건수와 대상 건수를 더해 나눈다.
설계사, 조직, 업권, 보험사 단위가 모두 같은 합산이라 시상 조건, 인증, 조직 화면이 같은 값을 쓴다. rate() 가 그 규칙이다.
기준 월 이하에서 가장 최근 달의 값을 쓴다. 대상이 0건이면 유지율은 None 이고 화면은 '자료 없음'으로 보인다.

유지 위험과 13회차 도래는 계약 원장에서 그때그때 계산한다. 아래 기준은 결정이 아니라 가정이다. 납입 자료가 확보되면 다시 본다.
- 마지막 납입 달: 계약현황의 최종납입년월(last_paid_ym). 비었으면 납입 회차 표(premium_payment)의 마지막 달.
  둘 다 없으면 판단하지 않고 no_data 로 센다. 샘플 자료는 최종납입년월이 없고 납입 회차 표만 있다.
- 미납 회차: 기준 월 앞 달까지 내야 했는데 내지 않은 회차 수. 기준 월 자체는 아직 낼 수 있어 세지 않는다.
  월납이고 유지 중(inforce)인 계약만 본다. 연납, 일시납, 3월납, 6월납은 달 단위로 밀림을 셀 수 없어 뺀다.
- 미납 1회는 unpaid(미납), 2회 이상은 lapse_risk(실효 위험)다. 보통 두 달 밀리면 납입최고를 거쳐 실효된다는 관행을 따른 가정이다.
- lapsed 는 기준 월과 그 앞 두 달 안에 이미 실효된 계약이다. 13회차 시책 환수와 이어진다.
- 13회차 도래일: 계약일에서 12개월 뒤 같은 날. 그 달에 그날이 없으면 말일이다. 월납의 13회차, 연납과 3월납과 6월납의
  같은 달 납입이 모두 이날에 온다. 일시납은 13회차 납입이 없어 뺀다. 유지 중(inforce)인 계약만 센다.
"""
import calendar
from datetime import date, timedelta

from app.periods import date_ym, ym_add, ym_diff

KIND_KO = {"p13": "13회차", "p25": "25회차"}
SECTOR_KO = {"life": "생명보험", "nonlife": "손해보험"}
RISK_KO = {"unpaid": "미납", "lapse_risk": "실효 위험", "lapsed": "실효"}
LAPSE_AFTER = 2              # 미납 회차가 이만큼이면 실효 위험
LAPSED_MONTHS = 3            # 기준 월을 넣어 이 개월 안에 실효된 계약을 lapsed 로 센다
DUE_AHEAD = 3                # 13회차 도래 목록의 기본 기간(개월)
RATE_NOTE = "유지율 현황 파일의 원수사별 유지 건수와 대상 건수를 더해 나눈 값입니다."
RISK_NOTE = ("마지막 납입 달은 계약현황의 최종납입년월이고, 비었으면 납입 회차 기록입니다. 기준 월 앞 달까지 밀린 회차가 1회면 미납, "
             "2회 이상이면 실효 위험입니다. 월납 유지 계약만 봅니다.")
DUE_NOTE = "13회차 도래일은 계약일에서 12개월 뒤 같은 날입니다. 유지 중인 계약만 세고 일시납은 뺍니다."


# ---------------------------------------------------------------- 유지율
def _latest(con, ids, kind, ym):
    marks = ",".join("?" * len(ids))
    return con.execute(f"SELECT MAX(period_ym) m FROM persistency WHERE period_ym<=? AND kind=? AND agent_id IN ({marks})", [ym, kind] + ids).fetchone()["m"]


def rate(con, agent_ids, kind, ym, insurer_id=None, sector=None):
    """ym 이하 가장 최근 월의 유지율. (rate, 유지, 대상) 을 돌려준다. 데이터가 없으면 (0.0, 0, 0)."""
    ids = list(agent_ids)
    if not ids:
        return 0.0, 0, 0
    marks = ",".join("?" * len(ids))
    latest = _latest(con, ids, kind, ym)
    if not latest:
        return 0.0, 0, 0
    sql = f"""SELECT COALESCE(SUM(p.numerator),0) n, COALESCE(SUM(p.denominator),0) d FROM persistency p JOIN insurer i ON i.insurer_id=p.insurer_id
              WHERE p.period_ym=? AND p.kind=? AND p.agent_id IN ({marks})"""
    params = [latest, kind] + ids
    if insurer_id:
        sql += " AND p.insurer_id=?"
        params.append(insurer_id)
    if sector and sector != "all":
        sql += " AND i.sector=?"
        params.append(sector)
    r = con.execute(sql, params).fetchone()
    return (round(r["n"] / r["d"], 4) if r["d"] else 0.0), r["n"], r["d"]


def _cell(n, d) -> dict:
    return {"rate": round(n / d, 4) if d else None, "kept": n, "total": d}


def rates(con, agent_ids, ym) -> dict:
    """범위 전체의 13회차와 25회차 유지율. rate() 와 같은 합산이다.

    돌려주는 것: {ym, note, kinds: [{kind, kind_ko, period_ym, rate, kept, total, by_sector: [...], by_insurer: [...]}]}
    rate 는 0 에서 1 사이이고 대상이 없으면 None 이다. period_ym 은 실제로 쓴 자료 달이다(기준 월 이하에서 가장 최근).
    """
    ids = sorted(set(agent_ids or ()))
    out = {"ym": ym, "note": RATE_NOTE, "kinds": []}
    for kind in KIND_KO:
        row = {"kind": kind, "kind_ko": KIND_KO[kind], "period_ym": None, **_cell(0, 0), "by_sector": [], "by_insurer": []}
        latest = _latest(con, ids, kind, ym) if ids else None
        if latest:
            marks = ",".join("?" * len(ids))
            got = con.execute(f"""SELECT i.insurer_id, i.name insurer, i.sector, SUM(p.numerator) n, SUM(p.denominator) d
                                  FROM persistency p JOIN insurer i ON i.insurer_id=p.insurer_id
                                  WHERE p.period_ym=? AND p.kind=? AND p.agent_id IN ({marks}) GROUP BY i.insurer_id ORDER BY i.name""",
                              [latest, kind] + ids).fetchall()
            n, d = sum(r["n"] for r in got), sum(r["d"] for r in got)
            row.update(period_ym=latest, **_cell(n, d))
            for sec in SECTOR_KO:
                sn, sd = sum(r["n"] for r in got if r["sector"] == sec), sum(r["d"] for r in got if r["sector"] == sec)
                if sd:
                    row["by_sector"].append({"sector": sec, "sector_ko": SECTOR_KO[sec], **_cell(sn, sd)})
            row["by_insurer"] = [{"insurer_id": r["insurer_id"], "insurer": r["insurer"], "sector": r["sector"], **_cell(r["n"], r["d"])} for r in got]
        out["kinds"].append(row)
    return out


def by_agent(con, agent_ids, ym) -> list:
    """사람별 13회차와 25회차 유지율. 관리자의 유지율 화면이 범위 안 사람을 줄 세울 때 쓴다.
    [{agent_id, code, name, p13: {rate, kept, total}, p25: {...}}] 사번 순. 자료가 없는 사람도 rate None 으로 넣는다."""
    ids = sorted(set(agent_ids or ()))
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    people = {r["agent_id"]: {"agent_id": r["agent_id"], "code": r["code"], "name": r["name"], **{k: _cell(0, 0) for k in KIND_KO}}
              for r in con.execute(f"SELECT agent_id, internal_code code, name FROM agent WHERE agent_id IN ({marks})", ids)}
    for kind in KIND_KO:
        latest = _latest(con, ids, kind, ym)
        if not latest:
            continue
        for r in con.execute(f"""SELECT agent_id, SUM(numerator) n, SUM(denominator) d FROM persistency
                                 WHERE period_ym=? AND kind=? AND agent_id IN ({marks}) GROUP BY agent_id""", [latest, kind] + ids):
            people[r["agent_id"]][kind] = _cell(r["n"], r["d"])
    return sorted(people.values(), key=lambda x: (x["code"] or "", x["agent_id"]))


# ---------------------------------------------------------------- 유지 위험
def _tail(policy_no) -> str | None:
    return policy_no[-4:] if policy_no else None


def risk(con, agent_ids, ym) -> dict:
    """미납과 실효 위험 계약. 기준 월 ym 의 자료로 본다.

    돌려주는 것: {ym, note, counts: {unpaid, lapse_risk, lapsed, no_data}, items: [...]}
    items 는 [{contract_id, agent_id, insurer, policy_tail, contract_date, last_paid_ym, missed, level, level_ko}]. 밀린 회차가 많은 순.
    level 은 unpaid / lapse_risk / lapsed 다. 계약자와 피보험자 이름은 넣지 않는다 (D-05).
    """
    ids = sorted(set(agent_ids or ()))
    out = {"ym": ym, "note": RISK_NOTE, "counts": {"unpaid": 0, "lapse_risk": 0, "lapsed": 0, "no_data": 0}, "items": []}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    live = con.execute(
        f"""SELECT c.contract_id, c.agent_id, c.policy_no, c.contract_date, i.name insurer,
                   COALESCE(c.last_paid_ym, (SELECT MAX(pp.period_ym) FROM premium_payment pp WHERE pp.contract_id=c.contract_id AND pp.period_ym<=?)) paid
            FROM contract c JOIN insurer i ON i.insurer_id=c.insurer_id
            WHERE c.agent_id IN ({marks}) AND c.status='inforce' AND COALESCE(c.payment_mode, 'monthly')='monthly'
              AND c.contract_date IS NOT NULL AND substr(replace(c.contract_date, '-', ''), 1, 6) < ?""",
        [ym, *ids, ym]).fetchall()
    for c in live:
        if not c["paid"]:
            out["counts"]["no_data"] += 1
            continue
        missed = ym_diff(ym, c["paid"]) - 1
        if missed < 1:
            continue
        level = "lapse_risk" if missed >= LAPSE_AFTER else "unpaid"
        out["counts"][level] += 1
        out["items"].append({"contract_id": c["contract_id"], "agent_id": c["agent_id"], "insurer": c["insurer"], "policy_tail": _tail(c["policy_no"]),
                             "contract_date": c["contract_date"], "last_paid_ym": c["paid"], "missed": missed, "level": level, "level_ko": RISK_KO[level]})
    since = ym_add(ym, -(LAPSED_MONTHS - 1))
    for c in con.execute(
            f"""SELECT c.contract_id, c.agent_id, c.policy_no, c.contract_date, i.name insurer, c.last_paid_ym,
                       COALESCE(c.status_date, c.status_changed_date) gone
                FROM contract c JOIN insurer i ON i.insurer_id=c.insurer_id
                WHERE c.agent_id IN ({marks}) AND c.status='lapsed'""", ids):
        gone = date_ym(c["gone"]) if c["gone"] else None
        if not gone or not (since <= gone <= ym):
            continue
        out["counts"]["lapsed"] += 1
        out["items"].append({"contract_id": c["contract_id"], "agent_id": c["agent_id"], "insurer": c["insurer"], "policy_tail": _tail(c["policy_no"]),
                             "contract_date": c["contract_date"], "last_paid_ym": c["last_paid_ym"], "missed": None, "level": "lapsed",
                             "level_ko": RISK_KO["lapsed"]})
    order = {"lapse_risk": 0, "unpaid": 1, "lapsed": 2}
    out["items"].sort(key=lambda x: (order[x["level"]], -(x["missed"] or 0), x["contract_date"] or "", x["contract_id"]))
    return out


# ---------------------------------------------------------------- 13회차 도래
def due13(contract_date: str) -> date:
    """13회차 도래일. 계약일에서 12개월 뒤 같은 날, 없으면 그 달 말일."""
    d = date.fromisoformat(contract_date[:10])
    y = d.year + 1
    return date(y, d.month, min(d.day, calendar.monthrange(y, d.month)[1]))


def inst13_due(con, agent_ids, date_from, date_to, *, ym=None) -> dict:
    """date_from 에서 date_to 까지(둘 다 포함) 13회차가 도래하는 유지 계약의 날짜별 건수.

    ym 을 주면 그 기준 월의 유지 위험(risk)과 겹치는 건수를 at_risk 로 붙인다.
    돌려주는 것: {date_from, date_to, note, total, at_risk, days: [{date, count, at_risk}], months: [{ym, count, at_risk}]}
    계약자 이름도 증권번호도 넣지 않는다. 날짜와 건수만이다.
    """
    f = date.fromisoformat(str(date_from)[:10])
    t = date.fromisoformat(str(date_to)[:10])
    ids = sorted(set(agent_ids or ()))
    out = {"date_from": f.isoformat(), "date_to": t.isoformat(), "note": DUE_NOTE, "total": 0, "at_risk": 0, "days": [], "months": []}
    if not ids or f > t:
        return out
    marks = ",".join("?" * len(ids))
    # 도래일이 [f, t] 이면 계약일은 [f - 1년, t - 1년] 언저리다. 말일 조정 때문에 앞뒤로 며칠 넓게 읽고 아래에서 다시 거른다
    lo, hi = (f.replace(day=1) - timedelta(days=370)).isoformat(), (t + timedelta(days=5)).isoformat()
    rows = con.execute(f"""SELECT contract_id, contract_date FROM contract
                           WHERE agent_id IN ({marks}) AND status='inforce' AND COALESCE(payment_mode, '')!='single'
                             AND contract_date IS NOT NULL AND contract_date BETWEEN ? AND ?""", [*ids, lo, hi]).fetchall()
    risky = {x["contract_id"] for x in risk(con, ids, ym)["items"]} if ym else set()
    days, months = {}, {}
    for r in rows:
        d = due13(r["contract_date"])
        if not (f <= d <= t):
            continue
        hit = r["contract_id"] in risky
        for bucket, key in ((days, d.isoformat()), (months, d.strftime("%Y%m"))):
            b = bucket.setdefault(key, {"count": 0, "at_risk": 0})
            b["count"] += 1
            b["at_risk"] += hit
        out["total"] += 1
        out["at_risk"] += hit
    out["days"] = [{"date": k, **v} for k, v in sorted(days.items())]
    out["months"] = [{"ym": k, **v} for k, v in sorted(months.items())]
    return out


def overview(con, agent_ids, ym, *, ref: date | None = None, months_ahead=DUE_AHEAD) -> dict:
    """내 유지율과 유지율 화면을 한 번에. {ym, rates, risk, due}.
    due 는 기준일 다음 날부터 months_ahead 개월 뒤까지의 13회차 도래다. ref 를 주지 않으면 기준 월의 말일이다.
    """
    if ref is None:
        y, m = int(ym[:4]), int(ym[4:])
        ref = date(y, m, calendar.monthrange(y, m)[1])
    end_ym = ym_add(ref.strftime("%Y%m"), months_ahead)
    ey, em = int(end_ym[:4]), int(end_ym[4:])
    end = date(ey, em, min(ref.day, calendar.monthrange(ey, em)[1]))
    return {"ym": ym, "ref": ref.isoformat(), "rates": rates(con, agent_ids, ym), "risk": risk(con, agent_ids, ym),
            "due": inst13_due(con, agent_ids, ref + timedelta(days=1), end, ym=ym)}
