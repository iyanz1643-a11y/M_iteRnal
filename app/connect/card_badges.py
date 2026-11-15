"""명함 배지의 자격과 유효기간. S3 명함 연동 3차.

명함 사이트의 배지 (public/make/index.html 기준)
- CERTS: 자격 칩. 우수인증설계사, 블루리본, MDRT, COT, TOT, CFP, AFPK, 개인보험심사역, 손해사정사
- ANNUAL: 연도를 넣어야 그려지는 칩. 우수인증설계사, 블루리본, MDRT, COT, TOT. 그림에는 "이름 연도" 로 들어간다
- 그 밖의 자격·수상 칸(certEtc): 쉼표로 나눈 글. 예 "2025 연도대상 금상"
- 배지는 설계사가 명함 만들기에서 고르고 그림 안에 그려진다. 사이트는 자격을 검사하지 않고, 서버에서 지울 수도 없다.

앱이 판정하는 것
| 근거 | 배지 | 칸 |
| --- | --- | --- |
| 인증 확정 기록 agent_certification_award | 우수인증설계사(생명과 손해), MDRT, COT, TOT, 이름에 블루리본이 든 원수사 인증 | certs, 연도 |
| 같은 기록의 award 와 insurer_cert 프로그램 | 영업우수설계사, 명예의전당, 원수사 인증 | etc, "연도 프로그램 이름" |
| 인증 계산 agent_certification_status 의 achieved | 위와 같다. 확정 전이라 expected 로만 둔다 | |
| 조직 부문 수상 확정 org_award_result | "Y년 M월 부문 이름 N위" | etc |

- CFP, AFPK, 개인보험심사역, 손해사정사는 앱에 자료가 없어 판정하지 않는다.
- 임원제도 executive_fc, executive_manager 는 직함이라 배지로 보지 않는다. 가정이다.
- 해촉자와 재직이 아닌 설계사는 판정하지 않는다. 그 명함은 card_remove 의 내리기 후보다.
- 계산은 하지 않는다. 인증은 certification.run 이 남긴 표를 읽고, 조직 부문 수상은 org_award.load 와 org_award.confirmed 를 부른다.
- 이름, 실적 값, 시상 내용, 금액은 결과에 담지 않는다. 시상 내용 공개는 D-45 라 부문 이름과 순위만 쓴다.

유효기간. 결정에 없어 둔 가정이다
- 연도 배지와 연도 수상: 달성 연도 다음 해 12월 31일까지. ANNUAL_VALID_YEARS
- 조직 부문 수상: 수상월 뒤 12개월째 달의 말일까지. ORG_AWARD_VALID_MONTHS
- 만료 예정은 남은 날이 EXPIRY_NOTICE_DAYS(30일) 이하인 것이다.

화면이 부르는 함수
- judge_badges(con, agent_ids, today=None) -> list[Badge]: 범위 안 설계사의 배지 판정. 저장하지 않는다
- expiring_badges(con, agent_ids, today=None, days=30, include_expired=False, linked_only=True) -> list[BadgeNotice]
- reissue_notice(badge, newer=None, today=None) -> str: 재발급 안내 문구
- refresh_badges(con, today=None) -> dict: 판정을 card_badge 표에 남긴다. 월 마감이나 배치가 부른다
- saved_badges(con, agent_ids, include_gone=False) -> list[dict]: 표에 남긴 판정
"""
from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable

from app import db
from app.connect import tables

# 명함 사이트의 목록. public/make/index.html 의 CERTS, ANNUAL 과 같다
CERTS = ("우수인증설계사", "블루리본", "MDRT", "COT", "TOT", "CFP", "AFPK", "개인보험심사역", "손해사정사")
ANNUAL = ("우수인증설계사", "블루리본", "MDRT", "COT", "TOT")

# certification_program.code 와 명함 자격 칩의 짝
CERT_BADGES = {"EXCELLENT_FC_LIFE": "우수인증설계사", "EXCELLENT_FC_NONLIFE": "우수인증설계사", "MDRT": "MDRT", "COT": "COT", "TOT": "TOT"}
BLUE_RIBBON = "블루리본"                          # 이름에 이 말이 든 원수사 인증을 블루리본 칩으로 본다. 가정이다
ETC_CATEGORIES = ("award", "insurer_cert")         # 그 밖의 자격·수상 칸으로 보내는 프로그램 종류

ANNUAL_VALID_YEARS = 1
ORG_AWARD_VALID_MONTHS = 12
EXPIRY_NOTICE_DAYS = 30

# 종류, 칸, 근거, 상태
CERT, AWARD, ORG_AWARD = "cert", "award", "org_award"
FIELD_CERTS, FIELD_ETC = "certs", "etc"
BASIS_AWARD, BASIS_CALC, BASIS_ORG = "award", "calc", "org_award"
VALID, EXPIRING, EXPIRED, EXPECTED = "valid", "expiring", "expired", "expected"
STATUS_KO = {VALID: "유효", EXPIRING: "만료 예정", EXPIRED: "만료", EXPECTED: "확정 전"}
BASIS_KO = {BASIS_AWARD: "인증 확정", BASIS_CALC: "인증 계산 달성", BASIS_ORG: "조직 부문 수상 확정"}

BEGIN, END = "-- [card_badge:begin]", "-- [card_badge:end]"
TABLE = "card_badge"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Badge:
    """설계사 한 명의 배지 하나. 이름, 실적 값, 시상 내용은 없다."""
    agent_id: int
    key: str                      # cert:우수인증설계사:2025 / award:SALES_EXCELLENT:2025 / org_award:12:202610
    kind: str                     # cert / award / org_award
    badge: str                    # 명함 자격 칩 이름, 또는 프로그램 이름이나 부문 이름
    card_field: str               # certs / etc
    label: str                    # 명함에 보이는 글
    basis: str                    # award / calc / org_award
    status: str                   # valid / expiring / expired / expected
    year: int | None = None
    period_ym: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    days_left: int | None = None  # 오늘부터 유효 마지막 날까지. 지났으면 음수
    source_ref: str | None = None
    group: str = ""               # 새 배지가 옛 배지를 잇는 묶음. 같은 칩, 같은 프로그램, 같은 부문

    @property
    def eligible(self) -> bool:
        """지금 명함에 올려도 되는가. 확정된 근거가 있고 유효기간 안이다."""
        return self.status in (VALID, EXPIRING)

    @property
    def status_ko(self) -> str:
        return STATUS_KO.get(self.status, self.status)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(eligible=self.eligible, status_ko=self.status_ko, basis_ko=BASIS_KO.get(self.basis, self.basis))
        return d


@dataclass(frozen=True)
class BadgeNotice:
    """만료 예정이나 만료된 배지 하나와 재발급 안내."""
    badge: Badge
    slug: str | None              # 이어진 명함 주소. 없으면 None
    notice: str                   # 화면과 알림에 그대로 보여도 되는 존댓말
    newer_label: str | None = None   # 바꿔 넣을 새 배지 글

    @property
    def agent_id(self) -> int:
        return self.badge.agent_id

    def to_dict(self) -> dict:
        return {**self.badge.to_dict(), "slug": self.slug, "notice": self.notice, "newer_label": self.newer_label}


# ------------------------------------------------------------------ 규칙
def _ids(agent_ids) -> list:
    out = []
    for x in agent_ids or ():
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v not in out:
            out.append(v)
    return out


def _day(v) -> str | None:
    v = str(v or "")
    return v[:10] if _DAY.match(v) else None


def annual_until(year: int) -> date:
    return date(int(year) + ANNUAL_VALID_YEARS, 12, 31)


def org_award_until(ym: str) -> date:
    y, m = int(ym[:4]), int(ym[4:6])
    k = y * 12 + (m - 1) + ORG_AWARD_VALID_MONTHS
    y2, m2 = divmod(k, 12)
    return date(y2, m2 + 1, calendar.monthrange(y2, m2 + 1)[1])


def _status(until: date, today: date, expected: bool) -> tuple[str, int]:
    left = (until - today).days
    if expected:
        return EXPECTED, left
    if left < 0:
        return EXPIRED, left
    return (EXPIRING if left <= EXPIRY_NOTICE_DAYS else VALID), left


def _spec(code: str, name: str, category: str):
    """프로그램 하나를 (종류, 배지, 칸) 으로. 배지가 아니면 None."""
    if code in CERT_BADGES:
        return CERT, CERT_BADGES[code], FIELD_CERTS
    if category == "insurer_cert" and BLUE_RIBBON in (name or ""):
        return CERT, BLUE_RIBBON, FIELD_CERTS
    if category in ETC_CATEGORIES:
        return AWARD, name, FIELD_ETC
    return None


def _cert_badge(agent_id, code, name, category, year, awarded_at, basis, today) -> Badge | None:
    spec = _spec(code, name, category)
    if spec is None or year is None:
        return None
    kind, badge, field = spec
    year = int(year)
    until = annual_until(year)
    status, left = _status(until, today, basis == BASIS_CALC)
    if kind == CERT:
        key, label, group = f"cert:{badge}:{year}", f"{badge} {year}", f"cert:{badge}"
    else:
        key, label, group = f"award:{code}:{year}", f"{year} {badge}", f"award:{code}"
    return Badge(agent_id=agent_id, key=key, kind=kind, badge=badge, card_field=field, label=label, basis=basis, status=status,
                 year=year, valid_from=_day(awarded_at) if basis == BASIS_AWARD else None, valid_until=until.isoformat(),
                 days_left=left, source_ref=code, group=group)


def _merge(found: dict, b: Badge) -> None:
    """같은 배지가 둘이면 확정을 계산보다 앞에 두고, 근거 코드를 합치고, 이른 시작일을 쓴다."""
    cur = found.get((b.agent_id, b.key))
    if cur is None:
        found[(b.agent_id, b.key)] = b
        return
    if cur.basis == BASIS_CALC and b.basis != BASIS_CALC:
        cur, b = b, cur
    elif b.basis == BASIS_CALC and cur.basis != BASIS_CALC:
        return
    refs = ",".join(sorted(set((cur.source_ref or "").split(",")) | set((b.source_ref or "").split(",")) - {""}))
    starts = [d for d in (cur.valid_from, b.valid_from) if d]
    found[(b.agent_id, b.key)] = Badge(**{**asdict(cur), "source_ref": refs or None, "valid_from": min(starts) if starts else None})


# ------------------------------------------------------------------ 판정
def judge_badges(con, agent_ids: Iterable, *, today: date | None = None) -> list:
    """범위 안 재직 설계사의 배지를 판정한다. 저장하지 않는다. 화면은 guard 가 준 설계사 목록을 넘긴다.
    돌려주는 값은 Badge 의 목록. 설계사, 칸, 배지, 연도 차례다."""
    today = today or date.today()
    ids = _ids(agent_ids)
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    ids = [r[0] for r in con.execute(f"SELECT agent_id FROM agent WHERE agent_id IN ({q}) AND status='active'", ids)]
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    found: dict = {}

    # 인증 확정 기록
    for r in con.execute(f"""SELECT w.agent_id, w.year, w.awarded_at, p.code, p.name, p.category
                             FROM agent_certification_award w JOIN certification_program p ON p.program_id=w.program_id
                             WHERE w.agent_id IN ({q})""", ids):
        b = _cert_badge(r[0], r[3], r[4], r[5], r[1], r[2], BASIS_AWARD, today)
        if b:
            _merge(found, b)

    # 인증 계산의 달성. 가장 늦은 기준월의 완료 run 하나만 본다
    run = con.execute("""SELECT run_id FROM calc_run WHERE run_type='certification' AND status='done'
                         ORDER BY period_ym DESC, run_id DESC LIMIT 1""").fetchone()
    if run:
        for r in con.execute(f"""SELECT s.agent_id, s.year, p.code, p.name, p.category
                                 FROM agent_certification_status s JOIN certification_program p ON p.program_id=s.program_id
                                 WHERE s.run_id=? AND s.achieved=1 AND s.agent_id IN ({q})""", [run[0], *ids]):
            b = _cert_badge(r[0], r[2], r[3], r[4], r[1], None, BASIS_CALC, today)
            if b:
                _merge(found, b)

    # 조직 부문 수상 확정. 이름과 실적 값과 시상 내용은 버린다
    from app.engine import org_award
    keep = set(ids)
    pairs = con.execute(f"SELECT DISTINCT division_id, period_ym FROM org_award_result WHERE agent_id IN ({q}) ORDER BY division_id, period_ym",
                        ids).fetchall()
    names: dict = {}
    for division_id, ym in pairs:
        if division_id not in names:
            d = org_award.load(con, division_id)
            names[division_id] = (d or {}).get("name") or "조직 부문 시상"
        for row in org_award.confirmed(con, division_id, ym):
            if row["agent_id"] not in keep:
                continue
            until = org_award_until(ym)
            status, left = _status(until, today, False)
            _merge(found, Badge(agent_id=row["agent_id"], key=f"org_award:{division_id}:{ym}", kind=ORG_AWARD, badge=names[division_id],
                                card_field=FIELD_ETC, label=f"{int(ym[:4])}년 {int(ym[4:6])}월 {names[division_id]} {row['rank']}위",
                                basis=BASIS_ORG, status=status, period_ym=ym, valid_from=_day(row.get("confirmed_at")),
                                valid_until=until.isoformat(), days_left=left, source_ref=str(division_id), group=f"org_award:{division_id}"))

    order = {a: i for i, a in enumerate(ids)}
    return sorted(found.values(), key=lambda b: (order.get(b.agent_id, 0), b.card_field, b.group, b.year or 0, b.period_ym or ""))


# ------------------------------------------------------------------ 만료 예정과 재발급 안내
def _newer(badge: Badge, pool: list) -> Badge | None:
    """같은 묶음에서 이 배지보다 늦고 지금 올릴 수 있는 배지 가운데 가장 늦은 것."""
    later = [b for b in pool if b.agent_id == badge.agent_id and b.group == badge.group and b.eligible and b.key != badge.key
             and (b.valid_until or "") > (badge.valid_until or "")]
    return max(later, key=lambda b: b.valid_until or "") if later else None


def reissue_notice(badge: Badge, newer: Badge | None = None, *, today: date | None = None) -> str:
    """만료 예정이나 만료된 배지의 재발급 안내. 명함 그림 안의 배지는 서버에서 지울 수 없어 설계사가 다시 만들도록 안내한다."""
    today = today or date.today()
    gone = badge.valid_until is not None and badge.valid_until < today.isoformat()
    head = (f"{badge.label} 배지의 유효기간이 {badge.valid_until} 에 끝났습니다." if gone
            else f"{badge.label} 배지의 유효기간이 {badge.valid_until} 에 끝납니다.")
    mid = "명함 그림 안의 배지는 앱이나 명함 사이트에서 지울 수 없습니다."
    if newer is not None and badge.card_field == FIELD_CERTS and newer.card_field == FIELD_CERTS and newer.year:
        act = f"명함 만들기의 자격·수상에서 {badge.badge} 연도를 {newer.year} 로 바꿔 명함을 다시 만들고 다시 신청해 주세요."
    elif newer is not None:
        act = f"명함 만들기의 그 밖의 자격·수상 칸에서 {badge.label} 대신 {newer.label} 로 바꿔 명함을 다시 만들고 다시 신청해 주세요."
    elif badge.card_field == FIELD_CERTS:
        act = f"명함 만들기의 자격·수상에서 {badge.badge} 배지를 빼고 명함을 다시 만들어 다시 신청해 주세요."
    else:
        act = f"명함 만들기의 그 밖의 자격·수상 칸에서 {badge.label} 를 지우고 명함을 다시 만들어 다시 신청해 주세요."
    return f"{head} {mid} {act} 새 명함이 승인되면 그림이 바뀝니다."


def expiring_badges(con, agent_ids: Iterable, *, today: date | None = None, days: int = EXPIRY_NOTICE_DAYS,
                    include_expired: bool = False, linked_only: bool = True, link_table=None) -> list:
    """days 일 안에 유효기간이 끝나는 배지와 재발급 안내. 화면은 guard 가 준 설계사 목록을 넘긴다.
    include_expired 가 참이면 이미 끝난 배지도 담는다. linked_only 가 참이면 명함 주소가 이어진 설계사만 담는다.
    돌려주는 값은 BadgeNotice 의 목록. 남은 날이 적은 차례다."""
    today = today or date.today()
    days = max(0, int(days))
    badges = judge_badges(con, agent_ids, today=today)
    if link_table is None:
        from app.connect.card_link import CardLinkTable
        link_table = CardLinkTable(con)
    out = []
    for b in badges:
        if b.basis == BASIS_CALC or b.days_left is None:
            continue
        if not (0 <= b.days_left <= days or (include_expired and b.days_left < 0)):
            continue
        slug = link_table.slug_for(b.agent_id)
        if linked_only and not slug:
            continue
        newer = _newer(b, badges)
        out.append(BadgeNotice(badge=b, slug=slug, notice=reissue_notice(b, newer, today=today), newer_label=newer.label if newer else None))
    return sorted(out, key=lambda n: (n.badge.days_left, n.badge.agent_id, n.badge.key))


# ------------------------------------------------------------------ 표에 남기기
def ensure(con) -> None:
    tables.ensure_block(con, BEGIN, END, (TABLE,))


def refresh_badges(con, *, today: date | None = None) -> dict:
    """재직 설계사 모두의 판정을 card_badge 에 남긴다. 다시 돌려도 같다. 근거가 없어진 줄은 지우지 않고 gone_at 을 채운다.
    돌려주는 값은 건수 {judged, new, gone}. 이름과 금액은 없다."""
    today = today or date.today()
    ensure(con)
    ids = [r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active' ORDER BY agent_id")]
    badges = judge_badges(con, ids, today=today)
    now = db.now()
    have = {(r[0], r[1]): r[2] for r in con.execute(f"SELECT agent_id, badge_key, gone_at FROM {TABLE}")}
    seen, new = set(), 0
    with tables.write(con, "card_badge"):
        for b in badges:
            seen.add((b.agent_id, b.key))
            vals = (b.kind, b.badge, b.card_field, b.year, b.period_ym, b.label, b.basis, b.source_ref, b.valid_from, b.valid_until, b.status)
            if (b.agent_id, b.key) in have:
                con.execute(f"""UPDATE {TABLE} SET kind=?, badge=?, card_field=?, year=?, period_ym=?, label=?, basis=?, source_ref=?,
                                valid_from=?, valid_until=?, status=?, judged_at=?, gone_at=NULL WHERE agent_id=? AND badge_key=?""",
                            (*vals, now, b.agent_id, b.key))
            else:
                con.execute(f"""INSERT INTO {TABLE}(agent_id, badge_key, kind, badge, card_field, year, period_ym, label, basis, source_ref,
                                valid_from, valid_until, status, first_judged_at, judged_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (b.agent_id, b.key, *vals, now, now))
                new += 1
        gone = [k for k, g in have.items() if k not in seen and g is None]
        for agent_id, key in gone:
            con.execute(f"UPDATE {TABLE} SET gone_at=? WHERE agent_id=? AND badge_key=?", (now, agent_id, key))
    return {"judged": len(badges), "new": new, "gone": len(gone)}


def saved_badges(con, agent_ids: Iterable, *, include_gone: bool = False) -> list:
    """card_badge 에 남긴 판정. 화면은 guard 가 준 설계사 목록을 넘긴다. 상태는 남긴 때의 값이다."""
    ensure(con)
    ids = _ids(agent_ids)
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    sql = f"SELECT * FROM {TABLE} WHERE agent_id IN ({q})" + ("" if include_gone else " AND gone_at IS NULL")
    return [dict(r) for r in con.execute(sql + " ORDER BY agent_id, card_field, badge_key", ids)]
