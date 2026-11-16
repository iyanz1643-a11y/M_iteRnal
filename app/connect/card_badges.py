"""명함 배지의 자격과 본인 공개. S3 명함 연동 3차, 11월 16일 대표님 결정 D-55, D-76 에서 D-79 로 고침.

배지 종류는 config/card_badges.yaml 한 곳에 둔다. D-76
- 종류마다 키, 이름, 원천, 원천의 줄을 고르는 조건, 명함 칸(certs, etc), 연도 표기, 명함 사이트 자격 이름의 짝이 있다.
- 새 종류는 그 파일에 한 줄을 더하면 판정 대상이 된다. 코드는 고치지 않는다.
- 원천의 줄은 등록부 SOURCES 의 함수가 읽는다. 지금 등록된 원천은 아래 셋이다. 새 원천만 함수를 하나 등록한다.

| 원천 | 읽는 것 | 근거 |
| --- | --- | --- |
| certification | 인증 확정 기록 agent_certification_award 와 인증 계산의 달성. 인증, 수상, 원수사 인증, 지점·본부 시상 프로그램이 모두 이 표다 | award, 계산은 calc |
| org_award | 조직 부문 수상 확정 org_award_result | org_award |
| education | 교육 수료 edu_completion. 뺀 수료는 넣지 않는다. 설정에 과정을 적어야 배지가 된다 | edu |

- 계산은 하지 않는다. 인증은 certification.awards 와 certification.latest_status 로, 조직 부문 수상은 org_award.load 와 org_award.confirmed 로 읽는다.
- 인증 계산에서 달성했지만 확정 전인 배지는 expected 다. 명함에 올릴 수 없고 공개 항목도 아니다.
- 설정에 맞는 종류가 없는 프로그램은 배지가 아니다. 임원제도 executive_fc, executive_manager 는 직함이라 설정에 넣지 않았다. 가정이다.
- CFP, AFPK, 개인보험심사역, 손해사정사는 앱에 자료가 없어 판정하지 않는다. 명함 사이트의 본인 입력으로 둔다.
- 이름, 실적 값, 시상 내용, 금액은 결과에 담지 않는다. 조직 부문 수상은 부문 이름과 순위만 쓴다. D-45
- 배지의 추가와 변경은 광고 심의 대상이 아니다. D-78

유효기간. D-55
- 자격이 생긴 배지는 퇴사 전까지 유지한다. 연도가 지났다고 내리지 않는다. valid_until 과 days_left 는 비어 있다.
- 연도 배지는 연도 표기를 그대로 둔다. 다음 해 자격이 생기면 새 연도 배지가 더해진다. 옛 연도 배지도 그대로 유효하다.
- 퇴사자와 재직이 아닌 설계사는 판정하지 않는다. 그 명함은 card_remove 의 내리기 후보이고, 공개 항목은 모두 비공개로 돌린다.
- 만료가 없어 expiring_badges 는 늘 빈 목록이다. 옛 화면이 부르므로 이름은 남긴다. 새 연도 자격의 안내는 new_year_notices 다.

본인 공개. D-77
- 공개 항목은 자격이 생긴 배지 하나하나다. 열쇠는 배지 열쇠와 같다. 명예의전당의 수상과 자격 기록도 같은 열쇠로 거른다.
- 랭킹 순위는 공개 항목이 아니다. D-81
- 기본은 비공개다. card_publish 에 줄이 없으면 비공개다. 본인 계정만 바꾼다. 메인 관리자도 남의 것은 바꾸지 못한다.
- 바꾼 일은 변경 기록 audit_log 의 card_publish 에 열쇠와 앞뒤 값만 남긴다.
- 퇴사하면 unpublish_departed 가 모두 비공개로 돌린다. refresh_badges 가 부른다. 그 전에도 published_keys 와 published_items 는 퇴사자를 빼고 돌려준다.

본인 입력 자격 표기. D-79
- 같은 자격의 검증 배지가 생기면 명함 사이트의 본인 입력 표기를 없앤다. 막는 일은 명함 사이트가 한다.
- 앱은 verified_cert_keys 로 그 사람에게 검증 배지가 있는 자격을 명함 사이트 CERTS 이름으로 돌려준다. 짝은 설정의 site_cert 다.

화면이 부르는 함수
- judge_badges(con, agent_ids, today=None) -> list[Badge]: 범위 안 설계사의 배지 판정. 저장하지 않는다
- publish_items(con, agent_id, today=None) -> list[PublishItem]: 본인의 공개 항목과 공개 여부. 내 명함의 공개 토글
- set_publish(con, agent_id, item_key, published, by, today=None) -> Outcome: 본인이 항목 하나의 공개를 바꾼다
- published_keys(con, agent_ids, today=None) -> dict[int, set[str]]: 공개한 열쇠. 명예의전당 거르기
- published_items(con, agent_ids, today=None) -> list[PublishItem]: 공개했고 지금도 자격이 있는 항목
- program_key(code, name, category, year) / org_award_key(division_id, ym): 명예의전당의 줄을 공개 열쇠로 바꾼다
- verified_cert_keys(con, agent_id, today=None) -> list[str]: 검증 배지가 있는 명함 사이트 자격 이름
- new_year_notices(con, agent_ids, today=None, days=30, linked_only=True) -> list[BadgeNotice]: 새 연도 자격 안내
- refresh_badges(con, today=None) -> dict: 판정을 card_badge 에 남기고 퇴사자 공개를 거둔다. 월 마감이나 배치가 부른다
- saved_badges(con, agent_ids, include_gone=False) -> list[dict]: 표에 남긴 판정
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable

import yaml

from app import db
from app.connect import tables
from app.connect.base import FAILED, OK, Outcome

CONFIG_FILE = db.ROOT / "config" / "card_badges.yaml"

# 칸, 근거, 상태
FIELD_CERTS, FIELD_ETC = "certs", "etc"
FIELDS = (FIELD_CERTS, FIELD_ETC)
BASIS_AWARD, BASIS_CALC, BASIS_ORG, BASIS_EDU = "award", "calc", "org_award", "edu"
VALID, EXPECTED = "valid", "expected"
EXPIRING, EXPIRED = "expiring", "expired"        # 옛 상태. D-55 로 만료가 없어져 판정은 이 둘을 내지 않는다. 화면이 아직 이름을 읽는다
STATUS_KO = {VALID: "유효", EXPECTED: "확정 전", EXPIRING: "만료 예정", EXPIRED: "만료"}
BASIS_KO = {BASIS_AWARD: "인증 확정", BASIS_CALC: "인증 계산 달성", BASIS_ORG: "조직 부문 수상 확정", BASIS_EDU: "교육 수료"}
VALIDITY_KO = "퇴사 전까지"

NOTICE_DAYS = 30                 # 새 연도 자격 안내를 내는 날 수. 자격이 생긴 날부터 센다
EXPIRY_NOTICE_DAYS = NOTICE_DAYS  # 옛 이름. 명함 승인 화면이 아직 읽는다

DENIED = "denied"
UNCHANGED = "unchanged"
AUDIT_PUBLISH = "card_publish"
SYSTEM = "system"
OWN_ONLY = "공개 여부는 본인만 바꿀 수 있습니다"

BEGIN, END = "-- [card_badge:begin]", "-- [card_badge:end]"
TABLE = "card_badge"
PUB_BEGIN, PUB_END = "-- [card_publish:begin]", "-- [card_publish:end]"
PUB_TABLE = "card_publish"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")
_YM = re.compile(r"^\d{6}$")


# ------------------------------------------------------------------ 값
@dataclass(frozen=True)
class Badge:
    """설계사 한 명의 배지 하나. 이름, 실적 값, 시상 내용은 없다."""
    agent_id: int
    key: str                      # cert:우수인증설계사:2025 / award:SALES_EXCELLENT:2025 / org_award:12:202610
    kind: str                     # 설정의 kind. cert / award / org_award 같은 열쇠 앞머리
    badge: str                    # 배지 이름. 명함 자격 칩 이름, 또는 프로그램 이름이나 부문 이름
    card_field: str               # certs / etc
    label: str                    # 명함에 보이는 글
    basis: str                    # award / calc / org_award / edu
    status: str                   # valid / expected
    year: int | None = None
    period_ym: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None   # 퇴사 전까지라 비어 있다. D-55
    days_left: int | None = None     # 만료가 없어 비어 있다
    source_ref: str | None = None
    group: str = ""               # 같은 배지의 다른 연도를 묶는다. 같은 칩, 같은 프로그램, 같은 부문
    config_key: str = ""          # 설정 badges 의 key
    origin: str = ""              # 원천의 한국어 이름. 인증, 수상, 조직 부문 수상 같은 것
    site_cert: str | None = None  # 명함 사이트 CERTS 의 이름

    @property
    def eligible(self) -> bool:
        """지금 명함에 올려도 되는가. 확정된 근거가 있다. 퇴사 전까지 유지한다."""
        return self.status == VALID

    @property
    def status_ko(self) -> str:
        return STATUS_KO.get(self.status, self.status)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(eligible=self.eligible, status_ko=self.status_ko, basis_ko=BASIS_KO.get(self.basis, self.basis),
                 validity_ko=VALIDITY_KO)
        return d


@dataclass(frozen=True)
class BadgeNotice:
    """배지 하나에 붙는 안내. 지금은 새 연도 자격이 생긴 안내뿐이다."""
    badge: Badge                  # 앞 연도의 배지
    slug: str | None              # 이어진 명함 주소. 없으면 None
    notice: str                   # 화면과 알림에 그대로 보여도 되는 존댓말
    newer_label: str | None = None   # 새 연도 배지 글

    @property
    def agent_id(self) -> int:
        return self.badge.agent_id

    def to_dict(self) -> dict:
        return {**self.badge.to_dict(), "slug": self.slug, "notice": self.notice, "newer_label": self.newer_label}


@dataclass(frozen=True)
class PublishItem:
    """본인 공개 항목 하나. 자격이 생긴 배지 하나와 그 공개 여부. 이름, 실적 값, 시상 내용은 없다."""
    agent_id: int
    item_key: str                 # 배지 열쇠와 같다
    label: str
    origin: str
    kind: str
    card_field: str
    year: int | None = None
    period_ym: str | None = None
    published: bool = False       # 줄이 없으면 비공개다
    changed_at: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published_ko"] = "공개" if self.published else "비공개"
        return d


# ------------------------------------------------------------------ 설정
_CACHE: dict = {}


def _as_list(v) -> list:
    if v is None:
        return []
    return [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]


def load_config(path: Path | str | None = None) -> dict:
    """config/card_badges.yaml 을 읽어 다듬는다. 바뀐 시각이 같으면 담아 둔 값을 쓴다.
    돌려주는 값은 {site_certs, site_annual, badges, problems}. 잘못된 종류는 badges 에서 빼고 problems 에 적는다."""
    path = Path(path or CONFIG_FILE)
    try:
        stamp = (path.stat().st_mtime_ns, tuple(SOURCES))
    except OSError:
        return {"site_certs": (), "site_annual": (), "badges": [], "problems": [f"{path.name} 이 없습니다"]}
    hit = _CACHE.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    site = raw.get("site") or {}
    certs, annual = tuple(_as_list(site.get("certs"))), tuple(_as_list(site.get("annual")))
    badges, problems, seen = [], [], set()
    for i, b in enumerate(raw.get("badges") or []):
        if not isinstance(b, dict):
            problems.append(f"badges {i + 1}번째 줄이 종류 꼴이 아닙니다")
            continue
        key = str(b.get("key") or "").strip()
        why = []
        if not key:
            why.append("key 가 없습니다")
        elif key in seen:
            why.append("key 가 겹칩니다")
        if str(b.get("source") or "") not in SOURCES:
            why.append(f"등록되지 않은 원천 {b.get('source')!r} 입니다")
        if b.get("card_field") not in FIELDS:
            why.append("card_field 는 certs 나 etc 입니다")
        site_cert = b.get("site_cert")
        if site_cert is not None and str(site_cert) not in certs:
            why.append("site_cert 가 명함 사이트 CERTS 에 없습니다")
        match = b.get("match") or {}
        if not isinstance(match, dict) or set(match) - {"code", "category", "name_contains"}:
            why.append("match 는 code, category, name_contains 만 씁니다")
            match = {}
        if why:
            problems.append(f"{key or i + 1}: " + ", ".join(why))
            continue
        seen.add(key)
        badges.append({
            "key": key, "name": str(b["name"]) if b.get("name") else None, "origin": str(b.get("origin") or ""),
            "source": str(b["source"]), "kind": str(b.get("kind") or key), "card_field": b["card_field"],
            "year_label": bool(b.get("year_label", False)), "label": str(b["label"]) if b.get("label") else None,
            "site_cert": str(site_cert) if site_cert is not None else None,
            "codes": set(_as_list(match.get("code"))), "categories": set(_as_list(match.get("category"))),
            "name_contains": str(match["name_contains"]) if match.get("name_contains") else None,
        })
    cfg = {"site_certs": certs, "site_annual": annual, "badges": badges, "problems": problems}
    _CACHE[str(path)] = (stamp, cfg)
    return cfg


def config_problems(path=None) -> list:
    """설정에서 빠진 종류와 그 까닭. 비어 있어야 한다."""
    return list(load_config(path)["problems"])


# ------------------------------------------------------------------ 원천 등록부
SOURCES: dict[str, Callable] = {}


def register_source(name: str):
    """원천을 읽는 함수를 등록한다. 함수는 (con, agent_ids) 를 받아 사실 dict 를 돌려준다.
    사실의 칸: agent_id, ref(코드나 번호), name, category, year, ym(YYYYMM), at(YYYY-MM-DD), basis, rank.
    이름, 실적 값, 시상 내용, 금액은 담지 않는다."""
    def deco(fn):
        SOURCES[name] = fn
        return fn
    return deco


@register_source("certification")
def _read_certification(con, ids) -> list:
    from app.engine import certification
    out = []
    for r in certification.awards(con, ids):
        out.append({"agent_id": r["agent_id"], "ref": r["code"], "name": r["name"], "category": r["category"], "year": r["year"],
                    "ym": None, "at": r["awarded_at"], "basis": BASIS_AWARD, "rank": None})
    # 가장 늦은 기준월의 완료 run 하나만 본다. certification.latest_status 가 고른다
    for r in certification.latest_status(con, ids, achieved_only=True):
        out.append({"agent_id": r["agent_id"], "ref": r["code"], "name": r["name"], "category": r["category"], "year": r["year"],
                    "ym": None, "at": None, "basis": BASIS_CALC, "rank": None})
    return out


@register_source("org_award")
def _read_org_award(con, ids) -> list:
    from app.engine import org_award
    keep = set(ids)
    q = ",".join("?" * len(ids))
    pairs = con.execute(f"SELECT DISTINCT division_id, period_ym FROM org_award_result WHERE agent_id IN ({q}) ORDER BY division_id, period_ym",
                        ids).fetchall()
    names: dict = {}
    out = []
    for division_id, ym in pairs:
        if division_id not in names:
            d = org_award.load(con, division_id)
            names[division_id] = (d or {}).get("name") or "조직 부문 시상"
        for row in org_award.confirmed(con, division_id, ym):
            if row["agent_id"] not in keep:
                continue
            out.append({"agent_id": row["agent_id"], "ref": str(division_id), "name": names[division_id], "category": "org_award",
                        "year": None, "ym": str(ym), "at": row.get("confirmed_at"), "basis": BASIS_ORG, "rank": row["rank"]})
    return out


@register_source("education")
def _read_education(con, ids) -> list:
    q = ",".join("?" * len(ids))
    try:
        rows = con.execute(f"""SELECT e.agent_id, c.code, c.name, c.kind, e.completed_on FROM edu_completion e
                               JOIN edu_course c ON c.course_id=e.course_id
                               WHERE e.agent_id IN ({q}) AND e.voided_at IS NULL ORDER BY e.agent_id, e.completed_on""", ids).fetchall()
    except Exception:                                   # 교육 표가 없는 오래된 DB
        return []
    return [{"agent_id": r[0], "ref": r[1], "name": r[2], "category": r[3], "year": int(str(r[4])[:4]) if _DAY.match(str(r[4] or "")) else None,
             "ym": None, "at": r[4], "basis": BASIS_EDU, "rank": None} for r in rows]


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


def _matches(spec: dict, fact: dict) -> bool:
    if spec["codes"] and str(fact.get("ref") or "") not in spec["codes"]:
        return False
    if spec["categories"] and str(fact.get("category") or "") not in spec["categories"]:
        return False
    if spec["name_contains"] and spec["name_contains"] not in str(fact.get("name") or ""):
        return False
    return True


def _spec_for(specs: list, fact: dict) -> dict | None:
    return next((s for s in specs if _matches(s, fact)), None)


def _label(spec: dict, name: str, fact: dict) -> str:
    year, ym = fact.get("year"), fact.get("ym")
    if spec["label"]:
        tmpl = spec["label"]
    elif spec["card_field"] == FIELD_CERTS:
        tmpl = "{name} {year}" if spec["year_label"] else "{name}"
    else:
        tmpl = "{year} {name}" if spec["year_label"] else "{name}"
    vals = {"name": name, "year": year if year is not None else (int(ym[:4]) if ym else ""), "code": fact.get("ref") or "",
            "y": int(ym[:4]) if ym else (year or ""), "m": int(ym[4:6]) if ym else "", "rank": fact.get("rank") or ""}
    try:
        return " ".join(tmpl.format(**vals).split())
    except (KeyError, IndexError, ValueError):
        return name


def _badge_from(spec: dict, fact: dict) -> Badge | None:
    """사실 하나를 설정의 종류 하나로 배지로 만든다. 연도 표기 종류인데 연도와 수상월이 없으면 None."""
    year = int(fact["year"]) if fact.get("year") is not None else None
    ym = str(fact["ym"]) if fact.get("ym") and _YM.match(str(fact["ym"])) else None
    if spec["year_label"] and year is None and ym is None:
        return None
    name = spec["name"] or str(fact.get("name") or spec["key"])
    ident = spec["name"] or str(fact.get("ref") or "")
    group = f"{spec['kind']}:{ident}"
    key = f"{group}:{ym or year}" if spec["year_label"] else group
    basis = fact.get("basis") or BASIS_AWARD
    return Badge(agent_id=int(fact["agent_id"]), key=key, kind=spec["kind"], badge=name, card_field=spec["card_field"],
                 label=_label(spec, name, fact), basis=basis, status=EXPECTED if basis == BASIS_CALC else VALID,
                 year=year, period_ym=ym, valid_from=None if basis == BASIS_CALC else _day(fact.get("at")),
                 source_ref=str(fact["ref"]) if fact.get("ref") is not None else None, group=group,
                 config_key=spec["key"], origin=spec["origin"], site_cert=spec["site_cert"])


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


def _active(con, ids: list) -> list:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return [r[0] for r in con.execute(f"SELECT agent_id FROM agent WHERE agent_id IN ({q}) AND status='active'", ids)]


def _present(con, ids: list, today: date) -> list:
    """재직 중이고 해촉일이 오늘보다 늦거나 없는 설계사. 공개를 보이는 기준이다. 내리기 후보의 기준과 맞춘다."""
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return [r[0] for r in con.execute(
        f"""SELECT agent_id FROM agent WHERE agent_id IN ({q}) AND status='active'
            AND (termination_date IS NULL OR substr(termination_date,1,10)>?)""", [*ids, today.isoformat()])]


# ------------------------------------------------------------------ 판정
def judge_badges(con, agent_ids: Iterable, *, today: date | None = None, config: dict | None = None) -> list:
    """범위 안 재직 설계사의 배지를 설정대로 판정한다. 저장하지 않는다. 화면은 guard 가 준 설계사 목록을 넘긴다.
    돌려주는 값은 Badge 의 목록. 설계사, 칸, 묶음, 연도 차례다. today 는 옛 호출과 맞추려고 받는다. 판정에 날짜를 쓰지 않는다."""
    ids = _active(con, _ids(agent_ids))
    if not ids:
        return []
    cfg = config or load_config()
    specs_by_source: dict = {}
    for s in cfg["badges"]:
        specs_by_source.setdefault(s["source"], []).append(s)
    found: dict = {}
    for source, fn in SOURCES.items():
        if source not in specs_by_source:
            continue
        # 원천 하나의 줄은 그 원천을 쓰는 종류 가운데 처음 맞는 하나에만 든다
        specs = specs_by_source[source]
        for fact in fn(con, ids):
            spec = _spec_for(specs, fact)
            if spec is None:
                continue
            b = _badge_from(spec, fact)
            if b is not None:
                _merge(found, b)
    order = {a: i for i, a in enumerate(ids)}
    return sorted(found.values(), key=lambda b: (order.get(b.agent_id, 0), b.card_field, b.group, b.year or 0, b.period_ym or ""))


def program_key(code: str, name: str, category: str, year, *, config: dict | None = None) -> str | None:
    """인증 프로그램 기록 하나의 배지 열쇠. 명예의전당의 수상 이력 줄을 공개 열쇠로 바꿀 때 쓴다. 배지가 아니면 None."""
    cfg = config or load_config()
    fact = {"agent_id": 0, "ref": code, "name": name, "category": category, "year": year, "ym": None, "at": None, "basis": BASIS_AWARD}
    spec = _spec_for([s for s in cfg["badges"] if s["source"] == "certification"], fact)
    b = _badge_from(spec, fact) if spec else None
    return b.key if b else None


def org_award_key(division_id, ym, *, config: dict | None = None) -> str | None:
    """조직 부문 수상 하나의 배지 열쇠. 명예의전당의 부문 수상자 줄을 공개 열쇠로 바꿀 때 쓴다. 배지가 아니면 None."""
    cfg = config or load_config()
    fact = {"agent_id": 0, "ref": str(division_id), "name": "", "category": "org_award", "year": None, "ym": str(ym), "at": None,
            "basis": BASIS_ORG, "rank": 1}
    spec = _spec_for([s for s in cfg["badges"] if s["source"] == "org_award"], fact)
    b = _badge_from(spec, fact) if spec else None
    return b.key if b else None


def verified_cert_keys(con, agent_id, *, today: date | None = None) -> list:
    """그 사람에게 검증 배지가 있는 자격을 명함 사이트 CERTS 이름으로 돌려준다. D-79
    명함 사이트가 이 자격의 본인 입력 표기를 막는 데 쓴다. 확정 전 배지는 넣지 않는다. 퇴사자는 빈 목록이다.
    차례는 명함 사이트 CERTS 의 차례다."""
    today = today or date.today()
    ids = _present(con, _ids([agent_id]), today)
    if not ids:
        return []
    have = {b.site_cert for b in judge_badges(con, ids, today=today) if b.eligible and b.site_cert}
    certs = load_config()["site_certs"]
    return [c for c in certs if c in have]


# ------------------------------------------------------------------ 안내
def reissue_notice(badge: Badge, newer: Badge | None = None, *, today: date | None = None) -> str:
    """배지 안내 문구. 만료가 없어 새 연도 자격이 생긴 때만 안내한다. newer 가 없으면 빈 글이다.
    옛 배지는 퇴사 전까지 그대로 둘 수 있다고 함께 알린다."""
    if newer is None:
        return ""
    head = f"{newer.label} 자격이 생겼습니다."
    if badge.card_field == FIELD_CERTS and newer.card_field == FIELD_CERTS and newer.year:
        act = f"원하시면 명함 만들기의 자격·수상에서 {newer.badge} 연도를 {newer.year} 로 바꿔 다시 신청해 주세요."
    else:
        act = f"원하시면 명함 만들기의 그 밖의 자격·수상 칸에 {newer.label} 를 더해 다시 신청해 주세요."
    return f"{head} {act} 지금 명함의 {badge.label} 은 퇴사 전까지 그대로 두셔도 됩니다."


def expiring_badges(con, agent_ids: Iterable, *, today: date | None = None, days: int = NOTICE_DAYS,
                    include_expired: bool = False, linked_only: bool = True, link_table=None) -> list:
    """만료 예정 목록. D-55 로 배지는 퇴사 전까지 유지되어 만료가 없다. 늘 빈 목록이다.
    옛 화면이 부르므로 이름과 인자는 남긴다. 새 연도 자격의 안내는 new_year_notices 다."""
    return []


def new_year_notices(con, agent_ids: Iterable, *, today: date | None = None, days: int = NOTICE_DAYS,
                     linked_only: bool = True, link_table=None) -> list:
    """같은 묶음에 앞 연도 배지가 있는데 days 일 안에 새 연도 자격이 확정된 배지의 안내. 화면은 guard 가 준 설계사 목록을 넘긴다.
    linked_only 가 참이면 명함 주소가 이어진 설계사만 담는다. 돌려주는 값은 BadgeNotice 의 목록. 새 자격이 늦게 생긴 차례다."""
    today = today or date.today()
    since = (today - timedelta(days=max(0, int(days)))).isoformat()
    badges = [b for b in judge_badges(con, agent_ids, today=today) if b.eligible]
    if link_table is None and linked_only:
        from app.connect.card_link import CardLinkTable
        link_table = CardLinkTable(con)
    by_group: dict = {}
    for b in badges:
        if b.year is not None or b.period_ym is not None:
            by_group.setdefault((b.agent_id, b.group), []).append(b)
    out = []
    for (agent_id, _), group in by_group.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda b: (b.year or 0, b.period_ym or ""))
        newest, prev = group[-1], group[-2]
        if not newest.valid_from or not (since <= newest.valid_from <= today.isoformat()):
            continue
        slug = link_table.slug_for(agent_id) if link_table is not None else None
        if linked_only and not slug:
            continue
        out.append(BadgeNotice(badge=prev, slug=slug, notice=reissue_notice(prev, newest, today=today), newer_label=newest.label))
    return sorted(out, key=lambda n: (n.newer_label is None, n.badge.agent_id, n.badge.key))


# ------------------------------------------------------------------ 본인 공개
def ensure_publish(con) -> None:
    tables.ensure_block(con, PUB_BEGIN, PUB_END, (PUB_TABLE,))


def _rows(con, ids: list) -> dict:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {(r[0], r[1]): (bool(r[2]), r[3]) for r in con.execute(
        f"SELECT agent_id, item_key, published, changed_at FROM {PUB_TABLE} WHERE agent_id IN ({q})", ids)}


def _item(b: Badge, row) -> PublishItem:
    published, at = row if row else (False, None)
    return PublishItem(agent_id=b.agent_id, item_key=b.key, label=b.label, origin=b.origin, kind=b.kind, card_field=b.card_field,
                       year=b.year, period_ym=b.period_ym, published=bool(published), changed_at=at)


def publish_items(con, agent_id, *, today: date | None = None) -> list:
    """본인 한 사람의 공개 항목. 자격이 생긴 배지마다 한 줄이고 공개 여부가 붙는다. 기본은 비공개다.
    확정 전 배지는 담지 않는다. 퇴사자는 빈 목록이다. 화면은 로그인한 본인의 설계사 번호만 넘긴다."""
    today = today or date.today()
    ensure_publish(con)
    ids = _present(con, _ids([agent_id]), today)
    if not ids:
        return []
    rows = _rows(con, ids)
    return [_item(b, rows.get((b.agent_id, b.key))) for b in judge_badges(con, ids, today=today) if b.eligible]


def _audit(con, actor, agent_id, before, after) -> None:
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), str(actor or "")[:64], AUDIT_PUBLISH, f"agent:{agent_id}",
                 json.dumps(before, ensure_ascii=False) if before is not None else None, json.dumps(after, ensure_ascii=False)))


def _own(con, by, agent_id) -> bool:
    """by 계정이 활성이고 그 계정의 설계사 번호가 agent_id 인가. 메인 관리자라도 본인이 아니면 거짓이다."""
    r = con.execute("SELECT agent_id FROM user_account WHERE login_id=? AND status='active'", (str(by or ""),)).fetchone()
    return r is not None and r[0] is not None and int(r[0]) == int(agent_id)


def set_publish(con, agent_id, item_key: str, published: bool, by: str, *, today: date | None = None) -> Outcome:
    """본인이 항목 하나의 공개 여부를 바꾼다. 예외를 올리지 않는다.
    돌려주는 값은 Outcome(status, value=PublishItem 또는 None, reason). reason 은 화면에 그대로 보여도 되는 존댓말이다.
    status: ok(바꿈) / unchanged(이미 그 값) / denied(본인이 아님, 퇴사, 자격 없는 항목) / failed
    by 는 로그인한 계정의 login_id 다. 그 계정의 설계사 번호가 agent_id 여야 한다. 바꾼 일은 변경 기록 card_publish 에 남는다."""
    today = today or date.today()
    try:
        agent_id = int(agent_id)
    except (TypeError, ValueError):
        return Outcome(DENIED, reason=OWN_ONLY)
    item_key, published = str(item_key or ""), bool(published)
    try:
        ensure_publish(con)
        if not _own(con, by, agent_id):
            return Outcome(DENIED, reason=OWN_ONLY)
        rows = _rows(con, [agent_id])
        cur = rows.get((agent_id, item_key))
        was = bool(cur[0]) if cur else False
        if published:
            if not _present(con, [agent_id], today):
                return Outcome(DENIED, reason="퇴사하셔서 공개할 수 없습니다")
            badge = next((b for b in judge_badges(con, [agent_id], today=today) if b.key == item_key and b.eligible), None)
            if badge is None:
                return Outcome(DENIED, reason="자격이 확인된 항목만 공개할 수 있습니다")
        else:
            badge = next((b for b in judge_badges(con, [agent_id], today=today) if b.key == item_key), None)
        if was == published:
            item = _item(badge, cur) if badge else None
            return Outcome(UNCHANGED, value=item, reason="이미 공개로 두셨습니다" if published else "이미 비공개입니다")
        now = db.now()
        with tables.write(con, "card_publish"):
            con.execute(f"""INSERT INTO {PUB_TABLE}(agent_id, item_key, published, changed_by, changed_at) VALUES(?,?,?,?,?)
                            ON CONFLICT(agent_id, item_key) DO UPDATE SET published=excluded.published,
                            changed_by=excluded.changed_by, changed_at=excluded.changed_at""",
                        (agent_id, item_key, int(published), str(by)[:64], now))
            _audit(con, by, agent_id, {"item_key": item_key, "published": was}, {"item_key": item_key, "published": published})
    except Exception:
        return Outcome(FAILED, reason="공개 여부를 저장하지 못했습니다. 잠시 뒤 다시 해 주세요")
    item = _item(badge, (published, now)) if badge else None
    return Outcome(OK, value=item, reason="공개로 바꿨습니다" if published else "비공개로 바꿨습니다")


def published_keys(con, agent_ids: Iterable, *, today: date | None = None) -> dict:
    """본인이 공개한 항목의 열쇠. {설계사 번호: 열쇠 집합}. 공개한 것이 없는 설계사는 담지 않는다.
    퇴사자는 표를 거두기 전이라도 뺀다. 명예의전당은 이 열쇠에 든 줄만 보인다. D-77, D-80"""
    today = today or date.today()
    ensure_publish(con)
    ids = _present(con, _ids(agent_ids), today)
    out: dict = {}
    for (agent_id, key), (on, _) in _rows(con, ids).items():
        if on:
            out.setdefault(agent_id, set()).add(key)
    return out


def published_items(con, agent_ids: Iterable, *, today: date | None = None) -> list:
    """본인이 공개했고 지금도 자격이 있는 항목. 화면은 guard 가 준 설계사 목록을 넘긴다.
    돌려주는 값은 PublishItem 의 목록. 설계사, 칸, 묶음, 연도 차례다."""
    today = today or date.today()
    keys = published_keys(con, agent_ids, today=today)
    if not keys:
        return []
    rows = _rows(con, list(keys))
    return [_item(b, rows.get((b.agent_id, b.key))) for b in judge_badges(con, list(keys), today=today)
            if b.eligible and b.key in keys.get(b.agent_id, ())]


def unpublish_departed(con, *, today: date | None = None, by: str = SYSTEM) -> dict:
    """퇴사자와 재직이 아닌 설계사의 공개 항목을 모두 비공개로 돌린다. 줄은 지우지 않는다. 다시 돌려도 같다.
    설계사마다 변경 기록 card_publish 를 한 줄 남긴다. 돌려주는 값은 건수 {agents, items}."""
    today = today or date.today()
    ensure_publish(con)
    rows = con.execute(
        f"""SELECT p.agent_id, p.item_key FROM {PUB_TABLE} p JOIN agent a ON a.agent_id=p.agent_id
            WHERE p.published=1 AND (a.status<>'active' OR (a.termination_date IS NOT NULL AND substr(a.termination_date,1,10)<=?))
            ORDER BY p.agent_id, p.item_key""", (today.isoformat(),)).fetchall()
    if not rows:
        return {"agents": 0, "items": 0}
    by_agent: dict = {}
    for agent_id, key in rows:
        by_agent.setdefault(agent_id, []).append(key)
    now = db.now()
    with tables.write(con, "card_unpublish"):
        for agent_id, keys in by_agent.items():
            q = ",".join("?" * len(keys))
            con.execute(f"UPDATE {PUB_TABLE} SET published=0, changed_by=?, changed_at=? WHERE agent_id=? AND item_key IN ({q})",
                        (by, now, agent_id, *keys))
            _audit(con, by, agent_id, {"published": keys}, {"published": [], "reason": "departed"})
    return {"agents": len(by_agent), "items": len(rows)}


# ------------------------------------------------------------------ 표에 남기기
def ensure(con) -> None:
    tables.ensure_block(con, BEGIN, END, (TABLE,))


def refresh_badges(con, *, today: date | None = None) -> dict:
    """재직 설계사 모두의 판정을 card_badge 에 남기고, 퇴사자의 공개를 거둔다. 다시 돌려도 같다.
    근거가 없어진 줄은 지우지 않고 gone_at 을 채운다. 돌려주는 값은 건수 {judged, new, gone, unpublished}. 이름과 금액은 없다."""
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
    off = unpublish_departed(con, today=today)
    return {"judged": len(badges), "new": new, "gone": len(gone), "unpublished": off["items"]}


def saved_badges(con, agent_ids: Iterable, *, include_gone: bool = False) -> list:
    """card_badge 에 남긴 판정. 화면은 guard 가 준 설계사 목록을 넘긴다."""
    ensure(con)
    ids = _ids(agent_ids)
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    sql = f"SELECT * FROM {TABLE} WHERE agent_id IN ({q})" + ("" if include_gone else " AND gone_at IS NULL")
    return [dict(r) for r in con.execute(sql + " ORDER BY agent_id, card_field, badge_key", ids)]


# 명함 사이트의 목록. 설정 파일의 site 와 같다. 화면이 연도 칸을 정할 때 읽는다
CERTS = load_config()["site_certs"]
ANNUAL = load_config()["site_annual"]
