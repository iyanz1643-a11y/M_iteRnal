"""DB(리드)와 활동 기록, 배정과 회수. 커밋은 부르는 쪽이 한다. intake 만 스스로 커밋한다.

| 무엇 | 규칙 |
| --- | --- |
| 상태 | new 미배정, assigned 배정, working 첫 활동 뒤, won 고객으로 바꿈, lost 종료 |
| 활동 | 통화, 상담, 제안, 청약. 담당 본인만 남긴다. 청약은 본인이 적는 활동 기록이고 업적 집계와 따로다 |
| 지역 매칭 | 그 지역을 맡겠다고 정해 둔 재직 설계사 가운데 보유 한도 밑인 사람. 들고 있는 DB 가 적은 사람, 배정받은 지 오래된 사람, 사번 순 |
| 보유 한도 | 담당이 들고 있는 assigned 와 working DB 수. 기본 30 |
| 방치 | 들고 있는 DB 에 마지막 활동이나 배정 뒤 stale 일 동안 활동이 없음. 기본 7일. warn 일부터 예고한다 |
| 회수 | 방치 DB 를 관리자가 회수한다. 담당의 소속 조직 풀로 돌아가고 회수 횟수가 오른다 |
| 풀 | 미배정 DB 는 pool_org_id 조직의 관리자와 메인 관리자가 배정한다. NULL 은 메인 관리자만 본다 |

배정과 회수의 기본값은 core.DEFAULTS 이고 가정이다. 조직별 설정에 DB 배정 항목이 생기면 그 값을 쓴다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app import db
from app.sales import core


def _in(ids):
    ids = list(ids) or [-1]
    return ",".join("?" * len(ids)), ids


def _row(r) -> dict | None:
    return dict(r) if r else None


def get(con, lead_id) -> dict | None:
    return _row(con.execute("SELECT * FROM sales_lead WHERE lead_id=?", (lead_id,)).fetchone())


def idle_days(lead: dict, day: date) -> int | None:
    """마지막 활동이나 배정 뒤 지난 날 수. 들고 있는 DB 가 아니면 None."""
    if lead.get("status") not in core.OPEN:
        return None
    last = lead.get("last_activity_at") or lead.get("assigned_at")
    if not last:
        return None
    return max((day - date.fromisoformat(last[:10])).days, 0)


def decorate(lead: dict, day: date) -> dict:
    lead["status_ko"] = core.STATUS_KO.get(lead["status"], lead["status"])
    lead["tone"] = core.STATUS_TONE.get(lead["status"], "")
    lead["source_ko"] = core.SOURCE_KO.get(lead["source"], lead["source"])
    lead["time_ko"] = core.TIMES.get(lead.get("preferred_time") or "", "")
    lead["idle"] = idle_days(lead, day)
    stale, warn = core.setting("stale"), core.setting("warn")
    lead["stale"] = lead["idle"] is not None and lead["idle"] >= stale
    lead["warn"] = lead["idle"] is not None and warn <= lead["idle"] < stale
    lead["left"] = (stale - lead["idle"]) if lead["idle"] is not None else None
    return lead


# ---------------------------------------------------------------- 만들기
def create(con, *, name, phone, region, source, by, preferred_time=None, memo=None, consent=False, agent_id=None, pool_org_id=None) -> int:
    """DB 한 건. 틀리면 ValueError. agent_id 가 있으면 바로 그 사람에게 배정한 것으로 둔다(직접 등록)."""
    nm, ph = core.clean_name(name), core.clean_phone(phone)
    if not nm:
        raise ValueError("이름을 두 글자 이상 한글이나 영문으로 적어 주세요.")
    if not ph:
        raise ValueError("휴대폰 번호를 다시 적어 주세요.")
    if region not in core.REGIONS:
        raise ValueError("지역을 골라 주세요.")
    if source not in core.SOURCE_KO:
        raise ValueError("출처를 골라 주세요.")
    if preferred_time and preferred_time not in core.TIMES:
        raise ValueError("상담 희망 시간을 골라 주세요.")
    if not consent:
        raise ValueError("개인정보 수집과 이용에 동의해야 접수됩니다.")
    now = db.now()
    status = "assigned" if agent_id else "new"
    cur = con.execute("""INSERT INTO sales_lead(source, name, phone, region, preferred_time, memo, consent_at, consent_version, status, agent_id,
                                                pool_org_id, assigned_at, assigned_by, created_by, created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (source, nm, ph, region, preferred_time or None, core.clean_text(memo, core.MEMO_MAX), now, core.CONSENT_VERSION, status,
                       agent_id, pool_org_id, now if agent_id else None, by if agent_id else None, by, now))
    lid = cur.lastrowid
    if agent_id:
        _log(con, lid, "assign", None, agent_id, None, "직접 등록", by)
    core.audit(con, by, "sales_lead_create", f"lead:{lid}", {"source": source, "region": region, "assigned": bool(agent_id)})
    return lid


def duplicate(con, phone, hours: int = 24) -> bool:
    """같은 연락처의 신청이 최근에 있었는가. 상담 신청이 두 번 들어와 DB 가 두 개 생기지 않게 한다."""
    ph = core.clean_phone(phone)
    if not ph:
        return False
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    return con.execute("SELECT 1 FROM sales_lead WHERE phone=? AND created_at>=?", (ph, since)).fetchone() is not None


def intake(con, *, name, phone, region, preferred_time, consent) -> dict:
    """상담 신청 랜딩의 신청 한 건. DB 로 넣고 지역 매칭 배정을 탄다. 커밋한다.

    같은 연락처가 하루 안에 다시 오면 새로 만들지 않는다. 돌려주는 값은 화면에 싣지 않는다."""
    core.ensure(con)
    if duplicate(con, phone):
        return {"lead_id": None, "agent_id": None, "duplicate": True}
    lid = create(con, name=name, phone=phone, region=region, source="landing", by="consult", preferred_time=preferred_time, consent=consent)
    cands = [r["agent_id"] for r in con.execute("SELECT agent_id FROM agent WHERE status='active'")]
    aid = pick(con, region, cands)
    if aid:
        assign(con, lid, aid, "consult", action="auto")
    con.commit()
    return {"lead_id": lid, "agent_id": aid, "duplicate": False}


# ---------------------------------------------------------------- 목록
def for_agent(con, agent_id, day: date, status: str = "") -> list:
    sql, params = "SELECT * FROM sales_lead WHERE agent_id=?", [agent_id]
    if status == "open":
        sql += " AND status IN ('assigned','working')"
    elif status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY CASE status WHEN 'assigned' THEN 0 WHEN 'working' THEN 1 WHEN 'won' THEN 2 ELSE 3 END, COALESCE(last_activity_at, assigned_at) ASC, lead_id"
    return [decorate(dict(r), day) for r in con.execute(sql, params)]


def pool(con, ctx, day: date) -> list:
    """이 사람이 배정할 수 있는 미배정 DB. 메인 관리자는 전부, 관리자는 풀 조직이 맡은 조직 안인 것."""
    sql, params = "SELECT * FROM sales_lead WHERE status='new'", []
    if ctx["role"] != "main":
        q, ids = _in(ctx.get("org_ids") or [])
        sql += f" AND pool_org_id IN ({q})"
        params += ids
    sql += " ORDER BY created_at, lead_id"
    return [decorate(dict(r), day) for r in con.execute(sql, params)]


def held(con, agent_ids, day: date) -> list:
    """범위 안 사람이 들고 있는 DB."""
    q, ids = _in(agent_ids)
    return [decorate(dict(r), day) for r in con.execute(
        f"SELECT * FROM sales_lead WHERE agent_id IN ({q}) AND status IN ('assigned','working') ORDER BY assigned_at, lead_id", ids)]


def stale(con, agent_ids, day: date) -> list:
    return [r for r in held(con, agent_ids, day) if r["stale"]]


def in_scope(con, ctx, lead: dict, agent_ids) -> bool:
    """배정 화면의 범위. 담당이 범위 안이거나, 미배정이면 풀 조직이 맡은 조직 안. 메인 관리자는 전부."""
    if ctx["role"] == "main":
        return True
    if lead["agent_id"]:
        return lead["agent_id"] in set(agent_ids)
    return lead["pool_org_id"] in (ctx.get("org_ids") or set())


# ---------------------------------------------------------------- 활동
def activities(con, lead_id) -> list:
    out = [dict(r) for r in con.execute("SELECT * FROM sales_activity WHERE lead_id=? ORDER BY done_on DESC, activity_id DESC", (lead_id,))]
    for a in out:
        a["kind_ko"] = core.KIND_KO.get(a["kind"], a["kind"])
    return out


def add_activity(con, lead_id, agent_id, kind, done_on, result, by, day: date | None = None) -> int:
    """담당 본인의 활동 한 건. 미배정, 다른 사람의 DB, 종료한 DB 에는 남기지 않는다."""
    lead = get(con, lead_id)
    if not lead or lead["agent_id"] != agent_id:
        raise PermissionError("내 DB 가 아닙니다.")
    if lead["status"] not in (*core.OPEN, "won"):
        raise ValueError("종료한 DB 에는 활동을 남기지 않습니다.")
    if kind not in core.KIND_KO:
        raise ValueError("활동 종류를 골라 주세요.")
    d = core.clean_date(done_on, required=True)
    if d > (day or core.today()).isoformat():
        raise ValueError("앞으로의 날짜에는 활동을 남기지 않습니다. 다음 할 일로 적어 주세요.")
    now = db.now()
    cur = con.execute("INSERT INTO sales_activity(lead_id, agent_id, kind, done_on, result, created_by, created_at) VALUES(?,?,?,?,?,?,?)",
                      (lead_id, agent_id, kind, d, core.clean_text(result, core.RESULT_MAX), by, now))
    status = core.KIND_STATUS[kind] if lead["status"] in core.OPEN else lead["status"]
    con.execute("UPDATE sales_lead SET status=?, last_activity_at=? WHERE lead_id=?", (status, now, lead_id))
    return cur.lastrowid


def close(con, lead_id, agent_id, by, reason) -> None:
    lead = get(con, lead_id)
    if not lead or lead["agent_id"] != agent_id:
        raise PermissionError("내 DB 가 아닙니다.")
    if lead["status"] not in core.OPEN:
        raise ValueError("진행 중인 DB 만 종료합니다.")
    con.execute("UPDATE sales_lead SET status='lost', closed_at=?, close_reason=? WHERE lead_id=?",
                (db.now(), core.clean_text(reason, core.MEMO_MAX), lead_id))
    core.audit(con, by, "sales_lead_close", f"lead:{lead_id}")


def convert(con, lead_id, agent_id, by) -> int:
    """체결한 DB 를 고객으로 바꾼다. 이름과 연락처를 고객으로 옮기고 DB 는 won 이 된다. 고객 번호를 돌려준다."""
    lead = get(con, lead_id)
    if not lead or lead["agent_id"] != agent_id:
        raise PermissionError("내 DB 가 아닙니다.")
    if lead["status"] not in core.OPEN:
        raise ValueError("진행 중인 DB 만 고객으로 바꿉니다.")
    now = db.now()
    cur = con.execute("""INSERT INTO sales_customer(lead_id, agent_id, name, phone, region, created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                      (lead_id, agent_id, lead["name"], lead["phone"], lead["region"], by, now))
    cid = cur.lastrowid
    con.execute("UPDATE sales_lead SET status='won', customer_id=?, closed_at=? WHERE lead_id=?", (cid, now, lead_id))
    core.audit(con, by, "sales_lead_won", f"lead:{lead_id}", {"customer_id": cid})
    return cid


# ---------------------------------------------------------------- 지역 매칭
def regions_of(con, agent_ids) -> dict:
    q, ids = _in(agent_ids)
    out: dict = {}
    for r in con.execute(f"SELECT agent_id, region FROM sales_region_pref WHERE agent_id IN ({q})", ids):
        out.setdefault(r["agent_id"], []).append(r["region"])
    for k in out:
        out[k].sort(key=core.REGIONS.index)
    return out


def set_regions(con, agent_id, regions, by) -> list:
    keep = [r for r in core.REGIONS if r in set(regions or [])]
    con.execute("DELETE FROM sales_region_pref WHERE agent_id=?", (agent_id,))
    now = db.now()
    for r in keep:
        con.execute("INSERT INTO sales_region_pref(agent_id, region, set_by, set_at) VALUES(?,?,?,?)", (agent_id, r, by, now))
    core.audit(con, by, "sales_region_set", f"agent:{agent_id}", {"regions": keep})
    return keep


def open_counts(con, agent_ids) -> dict:
    q, ids = _in(agent_ids)
    return {r["agent_id"]: r["n"] for r in con.execute(
        f"SELECT agent_id, COUNT(*) n FROM sales_lead WHERE agent_id IN ({q}) AND status IN ('assigned','working') GROUP BY agent_id", ids)}


def pick(con, region, candidates) -> int | None:
    """지역 매칭. 그 지역을 맡은 재직 설계사 가운데 보유 한도 밑인 사람 하나. 없으면 None."""
    cands = sorted(set(candidates or []))
    if not cands or region not in core.REGIONS:
        return None
    q, ids = _in(cands)
    rows = [dict(r) for r in con.execute(
        f"""SELECT a.agent_id, a.internal_code,
                   (SELECT MAX(assigned_at) FROM sales_lead l WHERE l.agent_id=a.agent_id) last_at
            FROM agent a JOIN sales_region_pref p ON p.agent_id=a.agent_id AND p.region=?
            WHERE a.agent_id IN ({q}) AND a.status='active'""", (region, *ids))]
    if not rows:
        return None
    held_n = open_counts(con, [r["agent_id"] for r in rows])
    hold = core.setting("hold")
    rows = [r for r in rows if held_n.get(r["agent_id"], 0) < hold]
    rows.sort(key=lambda r: (held_n.get(r["agent_id"], 0), r["last_at"] or "", r["internal_code"]))
    return rows[0]["agent_id"] if rows else None


def _log(con, lead_id, action, from_id, to_id, pool_org_id, reason, by):
    con.execute("""INSERT INTO sales_assign_log(lead_id, action, from_agent_id, to_agent_id, pool_org_id, reason, by_login, at)
                   VALUES(?,?,?,?,?,?,?,?)""", (lead_id, action, from_id, to_id, pool_org_id, reason, by, db.now()))


def assign(con, lead_id, agent_id, by, action="assign") -> None:
    """미배정 DB 를 한 사람에게. 재직자이고 보유 한도 밑이어야 한다."""
    lead = get(con, lead_id)
    if not lead or lead["status"] != "new":
        raise ValueError("미배정 DB 만 배정합니다.")
    a = con.execute("SELECT status FROM agent WHERE agent_id=?", (agent_id,)).fetchone()
    if not a or a["status"] != "active":
        raise ValueError("재직 중인 설계사에게만 배정합니다.")
    if open_counts(con, [agent_id]).get(agent_id, 0) >= core.setting("hold"):
        raise ValueError(f"보유 한도 {core.setting('hold')}건이 찼습니다. 다른 분께 배정해 주세요.")
    con.execute("UPDATE sales_lead SET status='assigned', agent_id=?, assigned_at=?, assigned_by=?, last_activity_at=NULL WHERE lead_id=?",
                (agent_id, db.now(), by, lead_id))
    _log(con, lead_id, action, None, agent_id, lead["pool_org_id"], None, by)


def auto_assign(con, lead_ids, candidates, by) -> tuple:
    """여러 건을 지역 매칭으로. (배정한 수, 남은 수)."""
    n = left = 0
    for lid in lead_ids:
        lead = get(con, lid)
        if not lead or lead["status"] != "new":
            continue
        aid = pick(con, lead["region"], candidates)
        if aid:
            assign(con, lid, aid, by, action="auto")
            n += 1
        else:
            left += 1
    return n, left


def move_pool(con, lead_id, org_id, by) -> None:
    """미배정 DB 를 조직 풀로 넘긴다. 메인 관리자가 본부 풀의 DB 를 조직에 나눌 때 쓴다."""
    lead = get(con, lead_id)
    if not lead or lead["status"] != "new":
        raise ValueError("미배정 DB 만 넘깁니다.")
    if not con.execute("SELECT 1 FROM org_unit WHERE org_id=?", (org_id,)).fetchone():
        raise ValueError("조직을 다시 골라 주세요.")
    con.execute("UPDATE sales_lead SET pool_org_id=? WHERE lead_id=?", (org_id, lead_id))
    _log(con, lead_id, "pool", None, None, org_id, None, by)


def reclaim(con, lead_id, by, ym, reason="방치") -> None:
    """들고 있는 DB 를 회수해 담당의 소속 조직 풀로 돌린다. 활동 기록은 지우지 않는다."""
    lead = get(con, lead_id)
    if not lead or lead["status"] not in core.OPEN:
        raise ValueError("들고 있는 DB 만 회수합니다.")
    org = core.home_org(con, lead["agent_id"], ym) or core.last_org(con, lead["agent_id"])
    con.execute("""UPDATE sales_lead SET status='new', agent_id=NULL, pool_org_id=?, assigned_at=NULL, assigned_by=NULL,
                   reclaim_count=reclaim_count+1 WHERE lead_id=?""", (org, lead_id))
    _log(con, lead_id, "reclaim", lead["agent_id"], None, org, reason, by)


def history(con, lead_id) -> list:
    return [dict(r) for r in con.execute(
        """SELECT g.*, fa.name from_name, ta.name to_name, o.name pool_name FROM sales_assign_log g
           LEFT JOIN agent fa ON fa.agent_id=g.from_agent_id LEFT JOIN agent ta ON ta.agent_id=g.to_agent_id
           LEFT JOIN org_unit o ON o.org_id=g.pool_org_id WHERE g.lead_id=? ORDER BY g.id DESC""", (lead_id,))]
