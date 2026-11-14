"""교육: 과정, 차수, 수료, 진척도, 명함 수료 코드 발급 기록.

수료의 원본은 수료자 명부다(D-09 제안, 가정). 명부 적재기는 record_completion 으로 한 줄씩 넣는다.
진척도와 기한은 표에 남기지 않고 그때그때 계산한다. 같은 날짜로 부르면 언제나 같은 값이 나온다.

모든 조회는 범위 안 설계사 목록 agent_ids 를 받아서만 한다. 그 밖의 사람은 세지도 보여 주지도 않는다.
퇴사자는 진척도의 모수에서 빼고, 수료 기록은 남겨 과정별 수료자 목록에 퇴사 표시와 함께 보인다.

표의 정의는 schema/schema.sql 의 [education:begin] 묶음이다.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
import sqlite3
from datetime import date, timedelta

from app import db
from app.engine import orgs

BEGIN, END = "-- [education:begin]", "-- [education:end]"
TABLES = ("edu_course", "edu_session", "edu_completion", "edu_card_code")

KIND_KO = {"newbie": "신인", "refresher": "보수", "product": "상품", "compliance": "준법"}
TARGET_KO = {"fc": "설계사", "manager": "관리자", "all": "전원"}
RULE_KO = {"none": "기한 없음", "hire_days": "위촉 뒤 N일", "fixed": "정한 날", "yearly": "매년"}
SOURCE_KO = {"roster": "수료자 명부", "manual": "관리자 확인", "sample": "샘플"}
STATE_KO = {"done": "수료", "overdue": "기한 지남", "soon": "기한 임박", "open": "미수료"}
SESSION_STATUS = ("planned", "held", "cancelled")
# 명함 사이트 public/make/index.html 의 EDU_COURSES 와 같다. 키는 명함 과정 코드, 값은 명함에 찍히는 과정 이름이다
CARD_COURSES = {"ABC": "ABC 과정"}
CARD_STATUS = ("requested", "issued", "failed", "revoked")
SOON_DAYS = 7          # 기한 7일 전부터 임박이다. 알림 기준(docs/09 의 교육 기한)과 같다

_MD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
_STATEMENTS: list | None = None


# ---------------------------------------------------------------- 표 준비
def statements() -> list:
    """schema.sql 의 교육 묶음을 문 하나씩 나눠 돌려준다."""
    global _STATEMENTS
    if _STATEMENTS is None:
        text = db.SCHEMA.read_text(encoding="utf-8")
        i, j = text.find(BEGIN), text.find(END)
        if i < 0 or j < i:
            raise RuntimeError("schema.sql 에 교육 표 묶음이 없습니다")
        body = "\n".join(re.sub(r"--.*$", "", line) for line in text[i + len(BEGIN):j].splitlines())
        out, buf = [], ""
        for part in body.split(";"):
            buf += part + ";"
            if sqlite3.complete_statement(buf):
                if buf.strip(" \n;"):
                    out.append(buf.strip())
                buf = ""
        _STATEMENTS = out
    return list(_STATEMENTS)


def ensure(con) -> None:
    """교육 표가 없는 오래된 DB 에서 표를 만든다. 이미 있으면 아무것도 하지 않는다."""
    have = {r[0] for r in con.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({','.join('?' * len(TABLES))})", TABLES)}
    if have == set(TABLES):
        return
    for sql in statements():
        con.execute(sql)
    con.commit()


# ---------------------------------------------------------------- 도우미
def _in(ids):
    ids = sorted(set(ids)) or [-1]
    return ",".join("?" * len(ids)), ids


def _day(v, what="날짜") -> str:
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{what}는 YYYY-MM-DD 로 넣어야 합니다.") from None


def _as_of(as_of) -> date:
    if as_of is None:
        return date.today()
    return as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])


def _audit(con, by, action, target, before=None, after=None):
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), by, action, str(target),
                 json.dumps(before, ensure_ascii=False) if before is not None else None,
                 json.dumps(after, ensure_ascii=False) if after is not None else None))


def _yearly_deadline(md: str, year: int) -> date:
    m, d = int(md[:2]), int(md[3:])
    return date(year, m, min(d, calendar.monthrange(year, m)[1]))


def cycle_key(course: dict, day) -> str:
    """수료 하나가 어느 주기의 몫인가. 매년 기한인 과정은 수료한 해, 그 밖은 once 다."""
    return str(day)[:4] if course["deadline_rule"] == "yearly" else "once"


# ---------------------------------------------------------------- 과정과 차수 쓰기
COURSE_FIELDS = ("code", "name", "kind", "required", "target_role", "target_levels", "target_hired_from", "owner_org_id",
                 "deadline_rule", "deadline_days", "deadline_date", "deadline_md", "card_course_code", "note")


def _clean_course(con, f: dict) -> dict:
    f = {k: (v.strip() if isinstance(v, str) else v) for k, v in f.items()}
    f = {k: (None if v == "" else v) for k, v in f.items()}
    if not f.get("code") or not f.get("name"):
        raise ValueError("과정 코드와 과정명을 넣어야 합니다.")
    if f.get("kind") not in KIND_KO:
        raise ValueError("유형은 신인, 보수, 상품, 준법 가운데 하나입니다.")
    f["required"] = 1 if f.get("required") else 0
    f["target_role"] = f.get("target_role") or "fc"
    if f["target_role"] not in TARGET_KO:
        raise ValueError("대상 역할 값이 맞지 않습니다.")
    if f.get("target_levels"):
        levels = [x.strip() for x in str(f["target_levels"]).split(",") if x.strip()]
        known = {r["code"] for r in con.execute("SELECT code FROM career_level")}
        if set(levels) - known:
            raise ValueError("없는 직급 코드가 있습니다.")
        f["target_levels"] = ",".join(levels) or None
    if f.get("target_hired_from"):
        f["target_hired_from"] = _day(f["target_hired_from"], "위촉일 기준")
    if f.get("owner_org_id") is not None and not con.execute("SELECT 1 FROM org_unit WHERE org_id=?", (f["owner_org_id"],)).fetchone():
        raise ValueError("없는 조직입니다.")
    rule = f.get("deadline_rule") or "none"
    if rule not in RULE_KO:
        raise ValueError("기한 규칙 값이 맞지 않습니다.")
    f["deadline_rule"] = rule
    days, fixed, md = f.get("deadline_days"), f.get("deadline_date"), f.get("deadline_md")
    f["deadline_days"] = f["deadline_date"] = f["deadline_md"] = None
    if rule == "hire_days":
        try:
            f["deadline_days"] = int(days)
        except (TypeError, ValueError):
            raise ValueError("위촉 뒤 며칠인지 넣어야 합니다.") from None
        if f["deadline_days"] <= 0:
            raise ValueError("위촉 뒤 날 수는 1 이상입니다.")
    elif rule == "fixed":
        f["deadline_date"] = _day(fixed, "기한")
    elif rule == "yearly":
        if not md or not _MD.match(str(md)):
            raise ValueError("매년 기한은 MM-DD 로 넣어야 합니다.")
        f["deadline_md"] = str(md)
    if f.get("card_course_code") and f["card_course_code"] not in CARD_COURSES:
        raise ValueError("명함 사이트에 없는 수료 코드 과정입니다.")
    return f


def save_course(con, *, by, course_id=None, **fields) -> int:
    """과정을 새로 만들거나 고친다. 고치기 전과 뒤를 변경 기록에 남긴다.

    누가 할 수 있는지는 화면의 guard 가 정한다. 메인 관리자는 전체, 조직 관리자는 자기 조직을 연 조직으로 둔다.
    """
    unknown = set(fields) - set(COURSE_FIELDS)
    if unknown:
        raise ValueError(f"모르는 칸: {sorted(unknown)}")
    before = course(con, course_id) if course_id else None
    if course_id and not before:
        raise ValueError("없는 과정입니다.")
    f = _clean_course(con, {**dict.fromkeys(COURSE_FIELDS), **({k: before[k] for k in COURSE_FIELDS} if before else {}), **fields})
    if before and before["deadline_rule"] != f["deadline_rule"] and "yearly" in (before["deadline_rule"], f["deadline_rule"]) and \
            con.execute("SELECT 1 FROM edu_completion WHERE course_id=? AND voided_at IS NULL", (course_id,)).fetchone():
        raise ValueError("수료 기록이 있는 과정은 매년 기한과 다른 기한 사이를 바꿀 수 없습니다. 새 과정을 만드세요.")
    dup = con.execute("SELECT course_id FROM edu_course WHERE code=?", (f["code"],)).fetchone()
    if dup and dup["course_id"] != course_id:
        raise ValueError("같은 과정 코드가 이미 있습니다.")
    cols = list(COURSE_FIELDS)
    if before:
        con.execute(f"UPDATE edu_course SET {', '.join(c + '=?' for c in cols)}, updated_by=?, updated_at=? WHERE course_id=?",
                    [f[c] for c in cols] + [by, db.now(), course_id])
    else:
        course_id = con.execute(f"INSERT INTO edu_course({', '.join(cols)}, created_by, created_at) VALUES({','.join('?' * (len(cols) + 2))})",
                                [f[c] for c in cols] + [by, db.now()]).lastrowid
    pick = lambda d: {k: d[k] for k in COURSE_FIELDS} if d else None
    _audit(con, by, "edu_course_update" if before else "edu_course_create", course_id, pick(before), pick(course(con, course_id)))
    con.commit()
    return course_id


def set_course_status(con, course_id, status, by):
    if status not in ("active", "closed"):
        raise ValueError(status)
    con.execute("UPDATE edu_course SET status=?, updated_by=?, updated_at=? WHERE course_id=?", (status, by, db.now(), course_id))
    _audit(con, by, f"edu_course_{status}", course_id)
    con.commit()


def save_session(con, *, by, course_id, date_from, date_to=None, online=False, place=None, capacity=None, status="planned",
                 note=None, session_no=None, session_id=None) -> int:
    """차수를 새로 만들거나 고친다. 차수 번호를 비우면 그 과정의 다음 번호를 준다."""
    if not course(con, course_id):
        raise ValueError("없는 과정입니다.")
    date_from = _day(date_from, "시작일")
    date_to = _day(date_to or date_from, "종료일")
    if date_to < date_from:
        raise ValueError("종료일이 시작일보다 앞섭니다.")
    if status not in SESSION_STATUS:
        raise ValueError("차수 상태 값이 맞지 않습니다.")
    place = (place or "").strip() or None
    if not online and not place:
        raise ValueError("장소를 넣거나 온라인으로 표시해야 합니다.")
    if capacity not in (None, ""):
        capacity = int(capacity)
        if capacity <= 0:
            raise ValueError("정원은 1 이상입니다.")
    else:
        capacity = None
    vals = (date_from, date_to, 1 if online else 0, place, capacity, status, note)
    if session_id:
        before = con.execute("SELECT * FROM edu_session WHERE session_id=? AND course_id=?", (session_id, course_id)).fetchone()
        if not before:
            raise ValueError("없는 차수입니다.")
        con.execute("UPDATE edu_session SET date_from=?, date_to=?, online=?, place=?, capacity=?, status=?, note=? WHERE session_id=?",
                    (*vals, session_id))
        _audit(con, by, "edu_session_update", session_id, dict(before), {"date_from": date_from, "date_to": date_to, "status": status})
    else:
        if session_no is None:
            session_no = con.execute("SELECT COALESCE(MAX(session_no),0)+1 FROM edu_session WHERE course_id=?", (course_id,)).fetchone()[0]
        try:
            session_id = con.execute(
                """INSERT INTO edu_session(course_id, session_no, date_from, date_to, online, place, capacity, status, note, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (course_id, int(session_no), *vals, by, db.now())).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("같은 차수 번호가 이미 있습니다.") from None
        _audit(con, by, "edu_session_create", session_id, None, {"course_id": course_id, "session_no": int(session_no), "date_from": date_from})
    con.commit()
    return session_id


# ---------------------------------------------------------------- 수료 쓰기
def record_completion(con, *, agent_id, course_id, by, agent_ids, completed_on=None, session_id=None, source="manual",
                      batch_id=None, row_no=None, commit=True) -> dict:
    """수료 한 줄을 넣는다. 같은 사람, 같은 과정, 같은 주기에 살아 있는 수료가 있으면 넣지 않고 그 줄을 돌려준다.

    agent_ids 는 쓰는 사람의 범위다. 범위 밖 사람이면 넣지 않는다. 메인 관리자와 명부 적재기는 None 을 넘긴다.
    돌려주는 값: {"completion_id", "created"(새로 넣었으면 참), "duplicate_of"(겹친 줄의 번호 또는 None)}.
    """
    if source not in SOURCE_KO:
        raise ValueError("수료 출처 값이 맞지 않습니다.")
    if agent_ids is not None and agent_id not in set(agent_ids):
        raise PermissionError("범위 밖 설계사입니다.")
    if not con.execute("SELECT 1 FROM agent WHERE agent_id=?", (agent_id,)).fetchone():
        raise ValueError("없는 설계사입니다.")
    c = course(con, course_id)
    if not c:
        raise ValueError("없는 과정입니다.")
    if session_id is not None:
        s = con.execute("SELECT * FROM edu_session WHERE session_id=? AND course_id=?", (session_id, course_id)).fetchone()
        if not s:
            raise ValueError("이 과정의 차수가 아닙니다.")
        if s["status"] == "cancelled":
            raise ValueError("취소된 차수입니다.")
        completed_on = completed_on or s["date_to"]
    if not completed_on:
        raise ValueError("수료일을 넣어야 합니다.")
    completed_on = _day(completed_on, "수료일")
    if batch_id is not None and not con.execute("SELECT 1 FROM import_batch WHERE batch_id=?", (batch_id,)).fetchone():
        raise ValueError("없는 명부 배치입니다.")
    key = cycle_key(c, completed_on)
    live = con.execute("SELECT completion_id FROM edu_completion WHERE agent_id=? AND course_id=? AND cycle_key=? AND voided_at IS NULL",
                       (agent_id, course_id, key)).fetchone()
    if live:
        return {"completion_id": live["completion_id"], "created": False, "duplicate_of": live["completion_id"]}
    cid = con.execute(
        """INSERT INTO edu_completion(agent_id, course_id, session_id, completed_on, cycle_key, source, batch_id, row_no, recorded_by, recorded_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (agent_id, course_id, session_id, completed_on, key, source, batch_id, row_no, by, db.now())).lastrowid
    if source == "manual":
        _audit(con, by, "edu_completion_confirm", cid, None, {"agent_id": agent_id, "course_id": course_id, "session_id": session_id,
                                                             "completed_on": completed_on})
    if commit:
        con.commit()
    return {"completion_id": cid, "created": True, "duplicate_of": None}


def void_completion(con, completion_id, *, by, reason, agent_ids) -> None:
    """수료 한 줄을 뺀다. 지우지 않고 뺀 때와 사람과 사유를 채운다."""
    r = con.execute("SELECT * FROM edu_completion WHERE completion_id=?", (completion_id,)).fetchone()
    if not r:
        raise ValueError("없는 수료 기록입니다.")
    if agent_ids is not None and r["agent_id"] not in set(agent_ids):
        raise PermissionError("범위 밖 설계사입니다.")
    if r["voided_at"]:
        return
    if not (reason or "").strip():
        raise ValueError("빼는 사유를 넣어야 합니다.")
    con.execute("UPDATE edu_completion SET voided_at=?, voided_by=?, void_reason=? WHERE completion_id=?", (db.now(), by, reason.strip(), completion_id))
    _audit(con, by, "edu_completion_void", completion_id, {"agent_id": r["agent_id"], "course_id": r["course_id"]}, {"reason": reason.strip()})
    con.commit()


def void_batch(con, batch_id, *, by, reason) -> int:
    """명부 배치 하나에서 들어온 수료를 모두 뺀다. 명부를 고쳐 다시 올릴 때 앞 배치에 쓴다. 뺀 줄 수를 돌려준다.

    지우지 않으므로 앞 배치의 줄과 원본 파일은 그대로 남고, 새 배치로 다시 만들 수 있다.
    """
    n = con.execute("UPDATE edu_completion SET voided_at=?, voided_by=?, void_reason=? WHERE batch_id=? AND voided_at IS NULL",
                    (db.now(), by, reason, batch_id)).rowcount
    _audit(con, by, "edu_batch_void", batch_id, None, {"voided": n, "reason": reason})
    con.commit()
    return n


# ---------------------------------------------------------------- 과정과 차수 읽기
def _course_row(r) -> dict:
    d = dict(r)
    d["kind_ko"] = KIND_KO.get(d["kind"], d["kind"])
    d["target_ko"] = TARGET_KO.get(d["target_role"], d["target_role"])
    d["level_list"] = d["target_levels"].split(",") if d["target_levels"] else []
    d["card_course_name"] = CARD_COURSES.get(d["card_course_code"]) if d["card_course_code"] else None
    d["deadline_ko"] = deadline_text(d)
    return d


def deadline_text(c: dict) -> str:
    rule = c["deadline_rule"]
    if rule == "hire_days":
        return f"위촉 뒤 {c['deadline_days']}일"
    if rule == "fixed":
        return f"{c['deadline_date']}까지"
    if rule == "yearly":
        return f"매년 {int(c['deadline_md'][:2])}월 {int(c['deadline_md'][3:])}일까지"
    return "기한 없음"


def course(con, course_id) -> dict | None:
    r = con.execute("SELECT * FROM edu_course WHERE course_id=?", (course_id,)).fetchone()
    return _course_row(r) if r else None


def list_courses(con, org_ids=None, include_closed=False) -> list:
    """과정 목록. org_ids 가 None 이면 전체(메인 관리자). 아니면 전체 과정과, 그 조직들 또는 그 위 조직이 연 과정이다.

    과정 자체는 사람 자료가 아니어서 설계사 목록 대신 조직 범위로 거른다.
    """
    rows = [_course_row(r) for r in con.execute("SELECT * FROM edu_course ORDER BY required DESC, code")]
    if not include_closed:
        rows = [c for c in rows if c["status"] == "active"]
    if org_ids is None:
        return rows
    seen = set()
    for o in org_ids:
        seen |= orgs.ancestors(con, o)
    return [c for c in rows if c["owner_org_id"] is None or c["owner_org_id"] in seen]


def sessions(con, course_id, agent_ids) -> list:
    """과정의 차수와, 차수마다 범위 안 수료자 수. 퇴사자 수료도 기록이라 함께 센다."""
    q, ids = _in(agent_ids)
    return [dict(r) for r in con.execute(
        f"""SELECT s.*, (SELECT COUNT(*) FROM edu_completion c WHERE c.session_id=s.session_id AND c.voided_at IS NULL
                         AND c.agent_id IN ({q})) completers
            FROM edu_session s WHERE s.course_id=? ORDER BY s.date_from, s.session_no""", (*ids, course_id))]


def completers(con, course_id, agent_ids, session_id=None) -> list:
    """과정의 수료자 목록. 범위 안 사람만. 퇴사자의 수료도 남아 있으므로 left 표시와 함께 보인다."""
    q, ids = _in(agent_ids)
    cond, extra = "", []
    if session_id is not None:
        cond, extra = " AND c.session_id=?", [session_id]
    return [dict(r) for r in con.execute(
        f"""SELECT c.completion_id, c.agent_id, a.internal_code code, a.name, a.termination_date,
                   (a.status='terminated') AS "left", c.completed_on, c.cycle_key, c.source, c.batch_id, s.session_no, s.session_id
            FROM edu_completion c JOIN agent a ON a.agent_id=c.agent_id LEFT JOIN edu_session s ON s.session_id=c.session_id
            WHERE c.course_id=? AND c.voided_at IS NULL AND c.agent_id IN ({q}){cond}
            ORDER BY c.completed_on, a.internal_code""", (course_id, *ids, *extra))]


# ---------------------------------------------------------------- 사람별 현황
def _people(con, agent_ids, ref: date) -> list:
    """범위 안 사람과 기준일의 소속, 직급, 퇴사 여부."""
    q, ids = _in(agent_ids)
    ym = ref.strftime("%Y%m")
    out = []
    for r in con.execute(
            f"""SELECT a.agent_id, a.internal_code code, a.name, a.job_type, a.hire_date, a.status, a.termination_date,
                       (SELECT h.org_id FROM agent_org_history h WHERE h.agent_id=a.agent_id AND h.effective_from<=?
                         ORDER BY h.effective_from DESC, h.id DESC LIMIT 1) org_id,
                       (SELECT l.code FROM agent_career_history ch JOIN career_level l ON l.level_id=ch.level_id
                         WHERE ch.agent_id=a.agent_id AND ch.effective_from<=? AND (ch.effective_to IS NULL OR ch.effective_to>=?)
                         ORDER BY ch.effective_from DESC LIMIT 1) level_code
                FROM agent a WHERE a.agent_id IN ({q}) ORDER BY a.internal_code""", (ym, ym, ym, *ids)):
        p = dict(r)
        term = p["termination_date"]
        # 퇴사: 퇴사일이 기준일 이전이거나, 퇴사 상태인데 퇴사일이 비어 있으면 퇴사로 본다
        p["left"] = bool(term and term[:10] <= ref.isoformat()) or (p["status"] == "terminated" and not term)
        out.append(p)
    return out


def _applies(con, c: dict, p: dict, anc_cache: dict) -> bool:
    if c["target_role"] != "all" and p["job_type"] != c["target_role"]:
        return False
    if c["level_list"] and p["level_code"] not in c["level_list"]:
        return False
    if c["target_hired_from"] and not (p["hire_date"] and p["hire_date"][:10] >= c["target_hired_from"]):
        return False
    if c["owner_org_id"] is not None:
        if p["org_id"] is None:
            return False
        if p["org_id"] not in anc_cache:
            anc_cache[p["org_id"]] = orgs.ancestors(con, p["org_id"])
        if c["owner_org_id"] not in anc_cache[p["org_id"]]:
            return False
    return True


def due_date(c: dict, p: dict, ref: date) -> date | None:
    """그 사람의 그 과정 기한. 매년 기한은 기준일이 든 해의 기한이다. 위촉일이 없으면 위촉 뒤 기한을 정할 수 없어 None 이다."""
    rule = c["deadline_rule"]
    if rule == "hire_days":
        return date.fromisoformat(p["hire_date"][:10]) + timedelta(days=c["deadline_days"]) if p["hire_date"] else None
    if rule == "fixed":
        return date.fromisoformat(c["deadline_date"])
    if rule == "yearly":
        return _yearly_deadline(c["deadline_md"], ref.year)
    return None


def _completions(con, agent_ids, ref: date) -> dict:
    """(agent_id, course_id, cycle_key) 마다 기준일까지의 살아 있는 수료."""
    q, ids = _in(agent_ids)
    return {(r["agent_id"], r["course_id"], r["cycle_key"]): dict(r) for r in con.execute(
        f"""SELECT c.*, s.session_no FROM edu_completion c LEFT JOIN edu_session s ON s.session_id=c.session_id
            WHERE c.voided_at IS NULL AND c.completed_on<=? AND c.agent_id IN ({q})""", (ref.isoformat(), *ids))}


def _cell(c: dict, p: dict, done: dict | None, ref: date, soon_days: int) -> dict:
    due = due_date(c, p, ref)
    days_left = (due - ref).days if due else None
    if done:
        state = "done"
    elif days_left is not None and days_left < 0:
        state = "overdue"
    elif days_left is not None and days_left <= soon_days:
        state = "soon"
    else:
        state = "open"
    return {"course_id": c["course_id"], "course_code": c["code"], "course_name": c["name"], "kind": c["kind"], "kind_ko": c["kind_ko"],
            "required": bool(c["required"]), "due": due.isoformat() if due else None, "days_left": days_left,
            "state": state, "state_ko": STATE_KO[state],
            "completed_on": done["completed_on"] if done else None, "session_no": done["session_no"] if done else None,
            "late": bool(done and due and done["completed_on"] > due.isoformat()),
            "completion_id": done["completion_id"] if done else None}


def _matrix(con, agent_ids, as_of, required_only=True, soon_days=SOON_DAYS, course_id=None):
    ref = _as_of(as_of)
    people = _people(con, agent_ids, ref)
    courses = [c for c in list_courses(con) if (c["required"] or not required_only) and (course_id is None or c["course_id"] == course_id)]
    done = _completions(con, [p["agent_id"] for p in people], ref)
    anc: dict = {}
    for p in people:
        p["courses"] = []
        for c in courses:
            if _applies(con, c, p, anc):
                key = (p["agent_id"], c["course_id"], str(ref.year) if c["deadline_rule"] == "yearly" else "once")
                p["courses"].append(_cell(c, p, done.get(key), ref, soon_days))
    return ref, people, courses


def person_status(con, agent_ids, as_of=None, required_only=True, soon_days=SOON_DAYS) -> list:
    """사람별 과정 현황. 범위 안 사람마다 대상 과정의 기한, 수료일, 상태(done, overdue, soon, open)를 낸다.

    required_total 과 required_done 은 필수 과정만 센다. rate 는 필수 과정이 없으면 None 이다.
    퇴사자도 줄은 나오고 left 가 참이다. 진척도 합계(org_progress)에서는 빠진다.
    """
    _, people, _ = _matrix(con, agent_ids, as_of, required_only, soon_days)
    out = []
    for p in people:
        req = [x for x in p["courses"] if x["required"]]
        n_done = sum(x["state"] == "done" for x in req)
        out.append({k: p[k] for k in ("agent_id", "code", "name", "job_type", "hire_date", "termination_date", "left", "org_id", "level_code")} | {
            "courses": p["courses"], "required_total": len(req), "required_done": n_done,
            "rate": round(n_done / len(req), 4) if req else None,
            "overdue": sum(x["state"] == "overdue" for x in req), "soon": sum(x["state"] == "soon" for x in req)})
    return out


def my_education(con, agent_id, agent_ids, as_of=None, soon_days=SOON_DAYS) -> dict:
    """내 교육 화면. 필수 과정 현황, 남은 필수 과정, 수료 이력(필수 아닌 과정 포함)."""
    if agent_id not in set(agent_ids):
        raise PermissionError("범위 밖 설계사입니다.")
    rows = person_status(con, [agent_id], as_of, required_only=False, soon_days=soon_days)
    if not rows:
        return {"agent_id": agent_id, "required": [], "remaining": [], "history": [], "rate": None}
    me = rows[0]
    history = [dict(r) for r in con.execute(
        """SELECT c.completion_id, c.completed_on, c.source, c.course_id, e.code course_code, e.name course_name, e.kind, s.session_no,
                  s.online, s.place
           FROM edu_completion c JOIN edu_course e ON e.course_id=c.course_id LEFT JOIN edu_session s ON s.session_id=c.session_id
           WHERE c.agent_id=? AND c.voided_at IS NULL ORDER BY c.completed_on DESC, e.code""", (agent_id,))]
    req = [x for x in me["courses"] if x["required"]]
    remaining = sorted([x for x in req if x["state"] != "done"], key=lambda x: (x["days_left"] is None, x["days_left"] or 0, x["course_code"]))
    return {"agent_id": agent_id, "left": me["left"], "required": req, "remaining": remaining, "history": history,
            "optional": [x for x in me["courses"] if not x["required"]],
            "required_total": me["required_total"], "required_done": me["required_done"], "rate": me["rate"],
            "overdue": me["overdue"], "soon": me["soon"]}


# ---------------------------------------------------------------- 조직 진척도
def _course_totals(courses, people) -> list:
    out = []
    for c in courses:
        cells = [(p, x) for p in people for x in p["courses"] if x["course_id"] == c["course_id"]]
        live = [(p, x) for p, x in cells if not p["left"]]
        n_done = sum(x["state"] == "done" for _, x in live)
        out.append({"course_id": c["course_id"], "course_code": c["code"], "course_name": c["name"], "kind_ko": c["kind_ko"],
                    "deadline_ko": c["deadline_ko"], "targets": len(live), "done": n_done, "not_done": len(live) - n_done,
                    "rate": round(n_done / len(live), 4) if live else None,
                    "overdue": sum(x["state"] == "overdue" for _, x in live), "soon": sum(x["state"] == "soon" for _, x in live),
                    "left_excluded": len(cells) - len(live)})
    return out


def org_progress(con, agent_ids, as_of=None, soon_days=SOON_DAYS) -> dict:
    """범위 안 설계사의 필수 과정 진척도. 과정별 대상, 수료, 미수료, 수료율, 기한 지남, 기한 임박.

    모수는 기준일에 퇴사하지 않은 대상자다. 퇴사자는 left_excluded 로 따로 세고 수료 기록은 지우지 않는다.
    total 은 사람과 과정의 짝을 센 합계다. people_all_done 은 필수 과정을 모두 마친 사람 수다.
    """
    ref, people, courses = _matrix(con, agent_ids, as_of, True, soon_days)
    by_course = _course_totals(courses, people)
    live = [p for p in people if not p["left"] and p["courses"]]
    targets = sum(c["targets"] for c in by_course)
    done = sum(c["done"] for c in by_course)
    return {"as_of": ref.isoformat(), "courses": by_course,
            "total": {"targets": targets, "done": done, "not_done": targets - done, "rate": round(done / targets, 4) if targets else None,
                      "overdue": sum(c["overdue"] for c in by_course), "soon": sum(c["soon"] for c in by_course),
                      "people": len(live), "people_all_done": sum(all(x["state"] == "done" for x in p["courses"]) for p in live),
                      "left_excluded": sum(1 for p in people if p["left"] and p["courses"])}}


def progress_by_org(con, agent_ids, level, as_of=None, soon_days=SOON_DAYS) -> list:
    """조직별 진척도. 사람을 기준일 소속의 위 조직 가운데 level(hq, branch, center, team) 계층으로 묶는다.

    그 계층의 위 조직이 없는 사람(지점 직속인데 팀으로 묶을 때)은 자기 소속 조직으로 묶는다. 범위 밖 사람은 들어오지 않는다.
    """
    ref, people, courses = _matrix(con, agent_ids, as_of, True, soon_days)
    info = {r["org_id"]: dict(r) for r in con.execute("SELECT org_id, name, level, parent_id, path_key FROM org_unit")}
    groups: dict = {}
    for p in people:
        g = p["org_id"]
        o = g
        while o is not None and info.get(o, {}).get("level") != level:
            o = info.get(o, {}).get("parent_id")
        groups.setdefault(o if o is not None else g, []).append(p)
    out = []
    for org_id, members in groups.items():
        by_course = _course_totals(courses, members)
        targets = sum(c["targets"] for c in by_course)
        done = sum(c["done"] for c in by_course)
        o = info.get(org_id, {})
        out.append({"org_id": org_id, "org_name": o.get("name"), "org_level": o.get("level"), "path_key": o.get("path_key"),
                    "people": sum(1 for p in members if not p["left"] and p["courses"]),
                    "targets": targets, "done": done, "not_done": targets - done, "rate": round(done / targets, 4) if targets else None,
                    "overdue": sum(c["overdue"] for c in by_course), "soon": sum(c["soon"] for c in by_course), "courses": by_course})
    out.sort(key=lambda r: (r["rate"] is None, r["rate"] if r["rate"] is not None else 0, r["path_key"] or "", r["org_id"] or 0))
    return out


def not_done(con, agent_ids, as_of=None, course_id=None, soon_days=SOON_DAYS) -> list:
    """미수료자 목록. 필수 과정을 마치지 않은 재직자와 과정의 짝. 기한 지남, 임박, 여유 순이고 같으면 기한이 이른 순이다."""
    _, people, _ = _matrix(con, agent_ids, as_of, True, soon_days, course_id)
    order = {"overdue": 0, "soon": 1, "open": 2}
    rows = [{"agent_id": p["agent_id"], "code": p["code"], "name": p["name"], "org_id": p["org_id"], **x}
            for p in people if not p["left"] for x in p["courses"] if x["state"] != "done"]
    rows.sort(key=lambda r: (order[r["state"]], r["due"] is None, r["due"] or "", r["code"], r["course_code"]))
    return rows


def due_soon(con, agent_ids, as_of=None, soon_days=SOON_DAYS) -> list:
    """기한 임박 목록. 기한까지 soon_days 일 안에 든 미수료 필수 과정. 홈의 할 일과 알림이 쓴다."""
    return [r for r in not_done(con, agent_ids, as_of, soon_days=soon_days) if r["state"] == "soon"]


# ---------------------------------------------------------------- 명함 수료 코드
def normalize_card_code(code: str) -> str:
    """명함 사이트의 eduNorm 과 같은 정리. 공백을 없애고 대문자로 바꾸며 ABC-XXXX-XXXX 꼴이면 하이픈을 맞춘다."""
    c = re.sub(r"\s+", "", str(code or "")).upper()
    m = re.match(r"^ABC-?([0-9A-Z]{4})-?([0-9A-Z]{4})$", c)
    return f"ABC-{m.group(1)}-{m.group(2)}" if m else re.sub(r"\s+", "", str(code or ""))


def card_code_hash(code: str) -> str:
    """코드 원문의 SHA-256 16진수. 명함 사이트는 이 값의 앞 20자로 코드를 찾는다. 원문은 어디에도 남기지 않는다."""
    return hashlib.sha256(normalize_card_code(code).encode("utf-8")).hexdigest()


def card_code_candidates(con, agent_ids, card_course_code=None) -> list:
    """수료 코드를 발급할 수 있는 수료. 명함 과정이 이어진 과정의 살아 있는 수료 가운데 살아 있는 발급이 없는 재직자다."""
    q, ids = _in(agent_ids)
    cond, extra = "", []
    if card_course_code:
        cond, extra = " AND e.card_course_code=?", [card_course_code]
    return [dict(r) | {"card_course_name": CARD_COURSES.get(r["card_course_code"]), "completed_ym": r["completed_on"][:7]} for r in con.execute(
        f"""SELECT c.completion_id, c.agent_id, a.internal_code code, a.name, e.course_id, e.code course_code, e.name course_name,
                   e.card_course_code, c.completed_on
            FROM edu_completion c JOIN edu_course e ON e.course_id=c.course_id JOIN agent a ON a.agent_id=c.agent_id
            WHERE c.voided_at IS NULL AND e.card_course_code IS NOT NULL AND a.status<>'terminated' AND c.agent_id IN ({q}){cond}
              AND NOT EXISTS (SELECT 1 FROM edu_card_code k WHERE k.agent_id=c.agent_id AND k.card_course_code=e.card_course_code
                              AND k.status IN ('requested','issued'))
            ORDER BY c.completed_on, a.internal_code""", (*ids, *extra))]


def request_card_code(con, completion_id, *, by) -> int:
    """수료 코드 발급 요청을 남긴다. 명함 사이트에 보내는 일은 연결 어댑터가 쓰기 스위치를 보고 한다. 발급은 메인 관리자만 한다(D-44)."""
    r = con.execute("""SELECT c.*, e.card_course_code FROM edu_completion c JOIN edu_course e ON e.course_id=c.course_id
                       WHERE c.completion_id=?""", (completion_id,)).fetchone()
    if not r or r["voided_at"]:
        raise ValueError("살아 있는 수료 기록이 아닙니다.")
    if not r["card_course_code"]:
        raise ValueError("명함 수료 코드가 없는 과정입니다.")
    try:
        iid = con.execute("""INSERT INTO edu_card_code(completion_id, agent_id, card_course_code, completed_ym, requested_by, requested_at)
                             VALUES(?,?,?,?,?,?)""",
                          (completion_id, r["agent_id"], r["card_course_code"], r["completed_on"][:7], by, db.now())).lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("이미 발급했거나 발급 요청이 있습니다.") from None
    _audit(con, by, "edu_card_code_request", iid, None, {"completion_id": completion_id, "card_course_code": r["card_course_code"]})
    con.commit()
    return iid


def record_card_code_issued(con, issue_id, *, serial, code, by) -> None:
    """명함 사이트가 돌려준 일련번호와 코드를 받아 일련번호와 해시만 남긴다. code 는 저장하지 않는다."""
    r = con.execute("SELECT * FROM edu_card_code WHERE issue_id=?", (issue_id,)).fetchone()
    if not r or r["status"] != "requested":
        raise ValueError("발급 요청 상태가 아닙니다.")
    if not (serial or "").strip() or not (code or "").strip():
        raise ValueError("일련번호와 코드를 받아야 합니다.")
    try:
        con.execute("UPDATE edu_card_code SET status='issued', serial=?, code_sha256=?, issued_at=? WHERE issue_id=?",
                    (serial.strip(), card_code_hash(code), db.now(), issue_id))
    except sqlite3.IntegrityError:
        raise ValueError("이미 쓰인 일련번호나 코드입니다.") from None
    _audit(con, by, "edu_card_code_issued", issue_id, None, {"serial": serial.strip()})
    con.commit()


def record_card_code_failed(con, issue_id, *, reason, by) -> None:
    """보내기가 실패한 요청. 실패로 두면 같은 수료로 새 요청을 만들 수 있다."""
    con.execute("UPDATE edu_card_code SET status='failed', fail_reason=? WHERE issue_id=? AND status='requested'", ((reason or "")[:300], issue_id))
    _audit(con, by, "edu_card_code_failed", issue_id, None, {"reason": (reason or "")[:300]})
    con.commit()


def revoke_card_code(con, issue_id, *, by, reason) -> None:
    """발급한 코드를 내린 기록. 지우지 않고 내린 사람과 때를 채운다. 내리기는 메인 관리자만 한다(가정)."""
    r = con.execute("SELECT * FROM edu_card_code WHERE issue_id=?", (issue_id,)).fetchone()
    if not r or r["status"] not in ("requested", "issued"):
        raise ValueError("내릴 수 있는 발급이 아닙니다.")
    con.execute("UPDATE edu_card_code SET status='revoked', revoked_by=?, revoked_at=?, revoke_reason=? WHERE issue_id=?",
                (by, db.now(), reason, issue_id))
    _audit(con, by, "edu_card_code_revoke", issue_id, {"status": r["status"]}, {"reason": reason})
    con.commit()


def card_codes(con, agent_ids, include_revoked=False) -> list:
    """범위 안 사람의 수료 코드 발급 기록. 코드 원문은 없고 일련번호와 상태만 있다."""
    q, ids = _in(agent_ids)
    cond = "" if include_revoked else " AND k.status<>'revoked'"
    return [dict(r) | {"card_course_name": CARD_COURSES.get(r["card_course_code"])} for r in con.execute(
        f"""SELECT k.issue_id, k.completion_id, k.agent_id, a.internal_code code, a.name, k.card_course_code, k.completed_ym, k.status,
                   k.serial, k.requested_by, k.requested_at, k.issued_at, k.revoked_at
            FROM edu_card_code k JOIN agent a ON a.agent_id=k.agent_id WHERE k.agent_id IN ({q}){cond}
            ORDER BY k.requested_at, k.issue_id""", ids)]
