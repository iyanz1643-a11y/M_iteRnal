"""조직별 설정. 파트 H. 화면은 /org/settings(app/web/routes/org_settings.py)다.

- 상속: 값은 GA 에서 아래로 내려간다. 아래 조직이 자기 값을 두면 그 조직과 그 아래는 그 값을 쓴다. 아무 데도 없으면 ITEMS 의 기본값이다.
  더하기 항목(merge=union)은 위 조직의 값에 아래 조직의 값을 더한다. 아래에서 위의 것을 뺄 수 없다.
- 막기: 메인 관리자가 어느 조직에서 항목을 막으면 그 조직의 값이 아래 전부에 그대로 간다. 막은 조직의 아래에서는 누구도 값을 둘 수 없고,
  막은 조직 자신과 그 아래에서 관리자는 바꿀 수 없다. 막기에도 시작 달과 끝 달이 있다.
- 적용 월: 값마다 시작 달이 있다. 어느 달의 값은 그 달까지 시작한 줄 가운데 가장 늦은 것이다. 지난달을 계산하면 그때 값을 쓴다.
  시작 달은 first_open_ym 보다 앞일 수 없다. 지난 달의 값을 뒤늦게 바꾸지 않는다.
- 이력: 줄을 지우거나 고치지 않는다. 바꿀 때마다 줄을 더한다. 값이 None 인 줄은 위 조직의 값을 따르게 돌린 것이다.
  막기를 풀면 그 줄의 끝 달만 채운다.

권한. 메인 관리자는 모든 조직, 관리자는 자기 조직과 그 아래 가운데 WRITE_LEVELS 계층의 조직장이나 대리일 때만 바꾼다.
docs/09 2.4 의 조직별 설정 줄이 지사장·본부장과 지점장에게만 자기 조직을 주고 팀장에게는 없음이라 그렇게 했다.
센터장은 그 표에 칸이 없어 막아 두었다. 가정이다. 관리자가 바꿀 수 없는 항목(main_only)도 있다.

여기 함수는 쓰기 뒤에 스스로 커밋하고 변경 기록을 남긴다. 권한 밖 시도는 SettingDenied 를 내고 access_denied 로 남긴다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime

from app import db
from app.org import levels
from app.periods import ym_add
from app.web import auth  # 자료 기준 달과 조직 아래 찾기는 로그인 문맥과 같은 함수를 쓴다

BEGIN, END = "-- [org_settings:begin]", "-- [org_settings:end]"
YM = re.compile(r"^20\d\d(0[1-9]|1[0-2])$")
WRITE_LEVELS = {"hq", "branch"}          # 관리자가 설정을 바꾸는 조직장 계층. docs/09 2.4. 센터와 팀은 보기만 한다. 가정이다
PATH = "/org/settings"


class SettingError(ValueError):
    """값이나 달이 맞지 않는 요청."""


class SettingDenied(PermissionError):
    """권한 밖 요청. 화면은 403 으로 돌려준다."""


# ---------------------------------------------------------------- 항목
# kind: bool / int / multi(choices 의 부분집합) / courses(교육 과정 번호 목록)
# merge: override(아래가 덮어씀) / union(아래가 더함)
# main_only: 관리자는 바꾸지 못한다.  used_by: 값을 읽는 곳. 비면 아직 값만 저장한다
NOTIFY_KINDS = ["promo_new", "promo_due", "tier_near", "edu_due", "inst13_due", "board_new"]
HOME_CARDS = {"promos": "마감이 가까운 프로모션", "my_rank": "내 순위", "goal": "목표", "team_top": "인정보험료 상위"}

ITEMS: dict[str, dict] = {
    "notify.promo_new": {"group": "알림", "name": "새 프로모션 알림", "kind": "bool", "default": True,
                         "help": "게시된 프로모션을 알림함에 띄웁니다.", "used_by": "알림함"},
    "notify.promo_due": {"group": "알림", "name": "마감 임박 알림", "kind": "bool", "default": True,
                         "help": "프로모션 마감이 가까우면 알림함에 띄웁니다.", "used_by": "알림함"},
    "notify.tier_near": {"group": "알림", "name": "구간 근접 알림", "kind": "bool", "default": True,
                         "help": "다음 구간까지 가까워지면 알림함에 띄웁니다.", "used_by": "알림함"},
    "notify.edu_due": {"group": "알림", "name": "교육 기한 알림", "kind": "bool", "default": True,
                       "help": "필수 과정의 기한이 가까우면 알림함에 띄웁니다.", "used_by": "알림함"},
    "notify.inst13_due": {"group": "알림", "name": "13회차 도래 알림", "kind": "bool", "default": True,
                          "help": "이번 주 13회차 납입일이 오는 계약의 건수를 알림함에 띄웁니다.", "used_by": "알림함"},
    "notify.board_new": {"group": "알림", "name": "새 공지 알림", "kind": "bool", "default": True,
                         "help": "새 공지를 알림함에 띄웁니다.", "used_by": "알림함"},
    "notify.promo_due_days": {"group": "알림", "name": "마감 임박 알림 날짜 수", "kind": "int", "default": 3, "min": 1, "max": 14, "unit": "일",
                              "help": "마감 며칠 전부터 마감 임박으로 띄울지 정합니다.", "used_by": ""},
    "notify.edu_due_days": {"group": "알림", "name": "교육 기한 임박 날짜 수", "kind": "int", "default": 7, "min": 1, "max": 30, "unit": "일",
                            "help": "기한 며칠 전부터 임박으로 볼지 정합니다.", "used_by": ""},
    "promo.near_pct": {"group": "프로모션", "name": "구간 근접 비율", "kind": "int", "default": 30, "min": 5, "max": 50, "unit": "%",
                       "help": "다음 구간까지 남은 양이 문턱의 이 비율 안에 들면 구간 근접으로 봅니다.", "used_by": ""},
    "award.default_top_n": {"group": "조직 부문 시상", "name": "기본 선발 인원", "kind": "int", "default": 3, "min": 1, "max": 20, "unit": "명",
                            "help": "새 부문을 만들 때 선발 인원 칸에 미리 넣는 값입니다. 부문마다 바꿀 수 있습니다.", "used_by": ""},
    "home.cards": {"group": "홈", "name": "홈에 보일 카드", "kind": "multi", "default": list(HOME_CARDS), "choices": HOME_CARDS,
                   "help": "홈 화면에 보일 카드를 고릅니다.", "used_by": ""},
    "edu.extra_required": {"group": "교육", "name": "필수 과정 추가", "kind": "courses", "merge": "union", "default": [],
                           "help": "이 조직과 그 아래에 필수로 더할 과정입니다. 위 조직이 더한 과정은 아래에서 뺄 수 없습니다.", "used_by": ""},
    "board.manager_post": {"group": "공지", "name": "관리자 공지 올리기", "kind": "bool", "default": False, "main_only": True,
                           "help": "관리자가 자기 조직에 공지를 올리게 합니다. 지금은 메인 관리자만 올립니다.", "used_by": ""},
}
GROUP_ORDER = ["알림", "프로모션", "조직 부문 시상", "홈", "교육", "공지"]


def item(key: str) -> dict:
    if key not in ITEMS:
        raise SettingError("없는 설정 항목입니다.")
    return ITEMS[key]


# ---------------------------------------------------------------- 표
def ensure(con) -> None:
    """설정 표가 없는 오래된 DB 에서 schema.sql 의 조직별 설정 묶음을 돌린다."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='org_setting'").fetchone():
        return
    text = db.SCHEMA.read_text(encoding="utf-8")
    con.executescript(text[text.index(BEGIN):text.index(END)])
    con.commit()


def _has_tables(con) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='org_setting'").fetchone())


# ---------------------------------------------------------------- 조직
def _parents(con) -> dict:
    return {r[0]: r[1] for r in con.execute("SELECT org_id, parent_id FROM org_unit")}


def chain(con, org_id: int) -> list:
    """뿌리부터 org_id 까지의 조직 번호. 없는 조직이면 SettingError."""
    parents = _parents(con)
    if org_id not in parents:
        raise SettingError("없는 조직입니다.")
    out, cur = [], org_id
    while cur is not None and cur not in out:
        out.append(cur)
        cur = parents.get(cur)
    return list(reversed(out))


def root_org(con):
    r = con.execute("SELECT org_id FROM org_unit WHERE parent_id IS NULL AND level=? ORDER BY org_id LIMIT 1", (levels.root_code(),)).fetchone()
    if not r:
        r = con.execute("SELECT org_id FROM org_unit WHERE parent_id IS NULL ORDER BY org_id LIMIT 1").fetchone()
    return r[0] if r else None


def person_org(con, ctx: dict):
    """그 사람에게 적용할 조직. 메인 관리자는 GA, 나머지는 소속 조직, 소속이 없으면 맡은 조직 가운데 가장 위, 그것도 없으면 GA."""
    if ctx.get("role") != "main":
        if ctx.get("home_org"):
            return ctx["home_org"]["org_id"]
        heads = sorted(ctx.get("heads") or [], key=lambda h: levels.depth(h["level"]))
        if heads:
            return heads[0]["org_id"]
    return root_org(con)


def first_open_ym(con, today: date | None = None) -> str:
    """새 값이나 막기의 시작 달로 고를 수 있는 가장 이른 달. 자료 기준 달과 이번 달 가운데 늦은 쪽이다. 지난 달은 고치지 않는다."""
    t = today or date.today()
    return max(auth.current_ym(con), f"{t.year}{t.month:02d}")


now_ym = first_open_ym          # 지금 달. 화면의 지금 값과 알림이 쓴다. 새 값을 둘 수 있는 가장 이른 달과 같다


# ---------------------------------------------------------------- 값 다루기
def _stamp() -> str:
    """줄의 시각. 이력의 순서를 지키려고 마이크로초까지 둔다. 앞 19자는 db.now() 와 같은 꼴이다."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def _dump(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _load(s):
    return None if s is None else json.loads(s)


def clean(con, key: str, raw):
    """화면에서 온 값을 항목의 꼴로 바꾼다. raw 가 None 이면 상속으로 돌리는 것이다."""
    it = item(key)
    if raw is None:
        return None
    k = it["kind"]
    if k == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "on", "켬"):
            return True
        if s in ("0", "false", "off", "끔"):
            return False
        raise SettingError(f"{it['name']}: 켬이나 끔을 골라 주세요.")
    if k == "int":
        try:
            n = int(str(raw).strip())
        except ValueError:
            raise SettingError(f"{it['name']}: 숫자를 넣어 주세요.") from None
        if not it["min"] <= n <= it["max"]:
            raise SettingError(f"{it['name']}: {it['min']}에서 {it['max']} 사이로 넣어 주세요.")
        return n
    vals = [str(x).strip() for x in (raw if isinstance(raw, (list, tuple, set)) else [raw]) if str(x).strip()]
    if k == "multi":
        bad = [v for v in vals if v not in it["choices"]]
        if bad:
            raise SettingError(f"{it['name']}: 없는 카드가 있습니다.")
        return [c for c in it["choices"] if c in vals]
    if k == "courses":
        if any(not v.isdigit() for v in vals):
            raise SettingError(f"{it['name']}: 과정을 목록에서 골라 주세요.")
        ids = sorted({int(v) for v in vals})
        if ids:
            q = ",".join("?" * len(ids))
            try:
                ok = {r[0] for r in con.execute(f"SELECT course_id FROM edu_course WHERE status='active' AND course_id IN ({q})", ids)}
            except sqlite3.OperationalError:
                ok = set()
            if set(ids) - ok:
                raise SettingError(f"{it['name']}: 없거나 닫힌 과정이 있습니다.")
        return ids
    raise SettingError("처리할 수 없는 항목입니다.")


def _lock_at(con, org_ids: list, key: str, ym: str):
    """org_ids 가운데 ym 에 key 를 막아 둔 줄. 뿌리에 가까운 것부터 본다."""
    if not org_ids:
        return None
    q = ",".join("?" * len(org_ids))
    rows = {r["org_id"]: dict(r) for r in con.execute(
        f"""SELECT * FROM org_setting_lock WHERE key=? AND org_id IN ({q}) AND from_ym<=? AND (to_ym IS NULL OR to_ym>=?)""",
        (key, *org_ids, ym, ym))}
    for oid in org_ids:
        if oid in rows:
            return rows[oid]
    return None


def effective(con, org_id: int, key: str, ym: str) -> dict:
    """그 조직이 그 달에 쓰는 값.

    돌려주는 것: value(값), source(값을 둔 조직 번호, 기본값이면 None), from_ym(그 값의 시작 달), row_id,
    locked_at(이 조직이나 위에서 막은 조직 번호, 없으면 None), parts(더하기 항목에서 조직별로 더한 값)
    """
    it = item(key)
    if not YM.match(ym or ""):
        raise SettingError("달은 YYYYMM 으로 넣어 주세요.")
    path = chain(con, org_id)
    out = {"key": key, "value": it["default"], "source": None, "from_ym": None, "row_id": None, "locked_at": None, "parts": []}
    if not _has_tables(con):
        return out
    union = it.get("merge") == "union"
    acc = list(it["default"]) if union else None
    for oid in path:
        r = con.execute("""SELECT id, value, from_ym FROM org_setting WHERE org_id=? AND key=? AND from_ym<=?
                           ORDER BY from_ym DESC, id DESC LIMIT 1""", (oid, key, ym)).fetchone()
        if r and r["value"] is not None:
            v = _load(r["value"])
            if union:
                new = [x for x in v if x not in acc]
                acc += new
                if v:
                    out["parts"].append({"org_id": oid, "value": v, "from_ym": r["from_ym"]})
            else:
                out["value"] = v
            out.update(source=oid, from_ym=r["from_ym"], row_id=r["id"])
        lk = _lock_at(con, [oid], key, ym)
        if lk:
            out["locked_at"] = oid
            break
    if union:
        out["value"] = sorted(acc)
    return out


def own_rows(con, org_id: int, key: str) -> list:
    """그 조직에 바로 둔 줄. 최근 것부터."""
    if not _has_tables(con):
        return []
    return [dict(r) | {"value": _load(r["value"])} for r in con.execute(
        "SELECT * FROM org_setting WHERE org_id=? AND key=? ORDER BY from_ym DESC, id DESC", (org_id, key))]


def locked(con, org_id: int, key: str, ym: str) -> dict | None:
    """그 달에 org_id 나 그 위 조직이 key 를 막아 두었으면 그 막기 줄을, 아니면 None. 막은 조직 번호는 org_id 칸이다."""
    item(key)
    if not _has_tables(con):
        return None
    return _lock_at(con, chain(con, org_id), key, ym)


def live_lock(con, org_id: int, key: str) -> dict | None:
    """그 조직에 바로 걸린, 아직 끝 달이 없는 막기 줄."""
    if not _has_tables(con):
        return None
    r = con.execute("SELECT * FROM org_setting_lock WHERE org_id=? AND key=? AND to_ym IS NULL", (org_id, key)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- 권한
def write_orgs(con, ctx: dict) -> set:
    """그 사람이 설정을 바꿀 수 있는 조직. 메인 관리자는 모두, 관리자는 WRITE_LEVELS 의 조직장이나 대리로 맡은 조직과 그 아래."""
    if ctx.get("role") == "main":
        return {r[0] for r in con.execute("SELECT org_id FROM org_unit")}
    if ctx.get("role") != "manager":
        return set()
    tops = [h["org_id"] for h in ctx.get("heads") or [] if h.get("level") in WRITE_LEVELS]
    return auth._descendants(con, tops) & set(ctx.get("org_ids") or ()) if tops else set()


def can_edit(con, ctx: dict, org_id: int, key: str, ym: str) -> tuple[bool, str]:
    """(바꿀 수 있는가, 못 하면 까닭). 화면이 단추를 그릴지 정하고, set_value 가 한 번 더 본다."""
    it = item(key)
    if org_id not in write_orgs(con, ctx):
        return False, "이 조직의 설정을 바꿀 권한이 없습니다."
    if it.get("main_only") and ctx.get("role") != "main":
        return False, "메인 관리자만 바꾸는 항목입니다."
    lk = locked(con, org_id, key, ym)
    if lk and (ctx.get("role") != "main" or lk["org_id"] != org_id):
        return False, "위 조직에서 막아 둔 항목입니다." if lk["org_id"] != org_id else "메인 관리자가 막아 둔 항목입니다."
    return True, ""


def _deny(con, ctx, what: str, why: str):
    con.rollback()
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), ctx.get("login_id"), "access_denied", f"{PATH} {what}"))
    con.commit()
    raise SettingDenied(why)


def _check_ym(con, from_ym: str) -> str:
    from_ym = (from_ym or "").strip()
    if not YM.match(from_ym):
        raise SettingError("시작 달은 YYYYMM 으로 넣어 주세요.")
    first = first_open_ym(con)
    if from_ym < first:
        raise SettingError(f"시작 달은 {first[:4]}년 {int(first[4:])}월부터 고를 수 있습니다. 지난 달의 값은 바꾸지 않습니다.")
    return from_ym


def _audit(con, by, action, org_id, key, from_ym):
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), by, action, f"org={org_id} key={key} {from_ym}~"))


# ---------------------------------------------------------------- 쓰기
def check_set(con, org_id: int, key: str, from_ym: str, by: dict) -> str:
    """set_value 가 쓰기 전에 보는 것. 화면의 확인 단계도 부른다. 권한 밖이면 기록을 남기고 SettingDenied, 값이 틀리면 SettingError.

    돌려주는 것: 다듬은 시작 달."""
    ensure(con)
    it = item(key)
    what = f"set org={org_id} key={key}"
    if org_id not in write_orgs(con, by):
        _deny(con, by, what, "이 조직의 설정을 바꿀 권한이 없습니다.")
    if it.get("main_only") and by.get("role") != "main":
        _deny(con, by, what, "메인 관리자만 바꾸는 항목입니다.")
    from_ym = _check_ym(con, from_ym)
    for ym in (from_ym, first_open_ym(con)):             # 시작 달에도, 지금도 막혀 있지 않아야 한다
        ok, why = can_edit(con, by, org_id, key, ym)
        if not ok:
            if by.get("role") == "main":                 # 메인 관리자가 위에서 막힌 조직에 두려는 것은 권한 밖 시도가 아니다
                raise SettingError(why + " 막은 조직에서 먼저 풀어 주세요.")
            _deny(con, by, what, why)
    return from_ym


def check_lock(con, org_id: int, key: str, from_ym: str, by: dict, op: str = "lock") -> str:
    """막기와 풀기 전에 보는 것. 메인 관리자만 한다."""
    ensure(con)
    item(key)
    if by.get("role") != "main":
        _deny(con, by, f"{op} org={org_id} key={key}", "항목 막기와 풀기는 메인 관리자만 합니다.")
    chain(con, org_id)
    from_ym = _check_ym(con, from_ym)
    lk = live_lock(con, org_id, key)
    if op == "lock" and lk:
        raise SettingError("이미 막아 둔 항목입니다.")
    if op == "unlock" and not lk:
        raise SettingError("이 조직에서 막아 둔 항목이 아닙니다.")
    if op == "unlock" and from_ym < lk["from_ym"]:
        raise SettingError("푸는 달은 막기 시작 달보다 앞일 수 없습니다.")
    return from_ym


def set_value(con, org_id: int, key: str, value, from_ym: str, by: dict, note: str | None = None) -> int:
    """org_id 에 key 의 값을 from_ym 부터 둔다. value 가 None 이면 그 달부터 위 조직의 값을 따른다.

    by 는 guard 가 준 로그인 문맥이다. 권한은 여기서 한 번 더 본다. 줄 번호를 돌려주고 커밋한다.
    """
    from_ym = check_set(con, org_id, key, from_ym, by)
    v = clean(con, key, value)
    cur = con.execute("INSERT INTO org_setting(org_id, key, value, from_ym, set_by, set_at, note) VALUES(?,?,?,?,?,?,?)",
                      (org_id, key, _dump(v), from_ym, by["login_id"], _stamp(), (note or "").strip()[:200] or None))
    _audit(con, by["login_id"], "org_setting_set" if v is not None else "org_setting_inherit", org_id, key, from_ym)
    con.commit()
    return cur.lastrowid


def lock(con, org_id: int, key: str, from_ym: str, by: dict) -> int:
    """메인 관리자만. org_id 에서 key 를 from_ym 부터 막는다. 그 조직의 값이 아래 전부에 간다."""
    from_ym = check_lock(con, org_id, key, from_ym, by, "lock")
    cur = con.execute("INSERT INTO org_setting_lock(org_id, key, from_ym, locked_by, locked_at) VALUES(?,?,?,?,?)",
                      (org_id, key, from_ym, by["login_id"], _stamp()))
    _audit(con, by["login_id"], "org_setting_lock", org_id, key, from_ym)
    con.commit()
    return cur.lastrowid


def unlock(con, org_id: int, key: str, from_ym: str, by: dict) -> int:
    """메인 관리자만. from_ym 부터 막기를 푼다. 줄은 지우지 않고 끝 달을 from_ym 의 전달로 채운다."""
    from_ym = check_lock(con, org_id, key, from_ym, by, "unlock")
    lk = live_lock(con, org_id, key)
    con.execute("UPDATE org_setting_lock SET to_ym=?, unlocked_by=?, unlocked_at=? WHERE lock_id=?",
                (ym_add(from_ym, -1), by["login_id"], _stamp(), lk["lock_id"]))
    _audit(con, by["login_id"], "org_setting_unlock", org_id, key, from_ym)
    con.commit()
    return lk["lock_id"]


# ---------------------------------------------------------------- 읽기
def history(con, org_id: int) -> list:
    """그 조직에 바로 둔 값과 막기의 모든 줄. 최근 것부터. 이름은 넣지 않고 아이디만 둔다."""
    if not _has_tables(con):
        return []
    out = []
    for r in con.execute("SELECT * FROM org_setting WHERE org_id=?", (org_id,)):
        v = _load(r["value"])
        out.append({"at": r["set_at"], "by": r["set_by"], "key": r["key"], "what": "inherit" if v is None else "set", "value": v,
                    "from_ym": r["from_ym"], "to_ym": None, "note": r["note"], "id": r["id"]})
    for r in con.execute("SELECT * FROM org_setting_lock WHERE org_id=?", (org_id,)):
        out.append({"at": r["locked_at"], "by": r["locked_by"], "key": r["key"], "what": "lock", "value": None,
                    "from_ym": r["from_ym"], "to_ym": r["to_ym"], "note": None, "id": r["lock_id"]})
        if r["unlocked_at"]:
            out.append({"at": r["unlocked_at"], "by": r["unlocked_by"], "key": r["key"], "what": "unlock", "value": None,
                        "from_ym": ym_add(r["to_ym"], 1), "to_ym": None, "note": None, "id": r["lock_id"]})
    out.sort(key=lambda h: (h["at"], h["id"]), reverse=True)
    return out


def text(con, key: str, value) -> str:
    """화면에 보일 값의 글."""
    it = item(key)
    if value is None:
        return "위 조직을 따름"
    k = it["kind"]
    if k == "bool":
        return "켬" if value else "끔"
    if k == "int":
        return f"{value}{it.get('unit', '')}"
    if k == "multi":
        return ", ".join(it["choices"].get(v, v) for v in value) or "없음"
    if k == "courses":
        if not value:
            return "없음"
        q = ",".join("?" * len(value))
        try:
            names = {r[0]: r[1] for r in con.execute(f"SELECT course_id, name FROM edu_course WHERE course_id IN ({q})", list(value))}
        except sqlite3.OperationalError:
            names = {}
        return ", ".join(names.get(v, f"과정 {v}번") for v in value)
    return str(value)


def notify_off(con, ctx: dict) -> set:
    """그 사람의 조직에서 지금 달에 꺼 둔 알림 종류. 알림함이 부른다. 표가 없거나 조직이 없으면 빈 집합이다.

    알림은 오늘의 일이라 자료 기준 달이 아니라 now_ym 의 값을 쓴다. 이번 달부터 끈 것이 바로 듣는다."""
    try:
        org = person_org(con, ctx)
        if org is None or not _has_tables(con):
            return set()
        ym = now_ym(con)
        return {k for k in NOTIFY_KINDS if effective(con, org, f"notify.{k}", ym)["value"] is False}
    except (sqlite3.OperationalError, SettingError):
        con.rollback()
        return set()
