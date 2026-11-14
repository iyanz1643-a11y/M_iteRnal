"""자료에서 조직 트리, 설계사, 소속 이력, 조직장 지정을 만든다. 다시 돌려도 같은 결과가 나온다.

조직은 이름이 아니라 위에서부터의 경로로 식별한다. 그 경로가 org_unit.path_key 이고 뿌리(GA)는 하나뿐이다.
    뿌리      'ga'
    그 아래   'hq:가상1본부 > branch:가상1-1지점 > center:누리센터'
계층 코드를 붙여 두므로 중간 계층이 빈 경로와 찬 경로가 섞이지 않는다. 계층의 짝은 config/org_levels.yaml 이다.

- sync_staff: 사원현황 파서(app/loader/eternal_employee.py)의 rows 로 조직, 설계사, 소속 이력, 조직장을 맞춘다.
- sync_contract_orgs: 실제 계약현황의 조직 경로(app/loader/org_path.py)로 조직 마디를 만든다.
두 함수 모두 dry_run=True 면 같은 일을 한 뒤 되돌린다. 미리보기와 반영의 건수가 같게 하려고 그렇게 한다.

퇴사일이 있으면 설계사를 퇴사로 바꾸고 소속과 조직장 지정을 닫지만 계정은 중지하지 않는다. 계정 화면의 중지 후보 목록에 나온다.
파일에 없는 설계사는 건드리지 않는다. 사람 이름은 돌려주는 changes 에만 담는다. 화면에는 보여도 되지만 로그와 시험 출력에 쓰지 않는다.
"""
from collections import Counter

from app import db
from app.loader import org_path
from app.org import levels
from app.periods import ym_add

SEP = " > "
STAT_KEYS = ("rows", "new_orgs", "new_agents", "moved", "terminated", "reinstated", "placed", "heads_new", "heads_end",
             "renamed", "no_org", "head_without_org", "missing")
STAT_KO = {"rows": "읽은 사람", "new_orgs": "새 조직", "new_agents": "새 설계사", "moved": "이동", "terminated": "퇴사",
           "reinstated": "복직", "placed": "소속 없던 설계사 배치", "heads_new": "조직장 지정", "heads_end": "조직장 지정 끝",
           "renamed": "이름이 바뀐 사람", "no_org": "조직 칸이 빈 사람", "head_without_org": "직위의 계층이 경로에 없는 사람",
           "missing": "파일에 없는 재직 설계사"}


# ---------------------------------------------------------------- 경로
def key_of(steps) -> str:
    return SEP.join(f"{lv}:{name}" for lv, name in steps) if steps else levels.root_code()


def steps_from(source: str, values: dict) -> tuple:
    """파일의 조직 칸 {열: 값} 을 ([(계층, 이름)], 뿌리 이름) 으로. 빈 칸은 건너뛴다."""
    root = levels.root_code()
    steps, root_name = [], None
    for col, lv in levels.source(source).items():
        v = values.get(col)
        v = v.strip() if isinstance(v, str) else v
        if not v:
            continue
        if lv == root:
            root_name = v
        else:
            steps.append((lv, str(v)))
    return steps, root_name


def _root(con, name=None, stats=None) -> int:
    code = levels.root_code()
    r = con.execute("SELECT org_id FROM org_unit WHERE path_key=?", (code,)).fetchone()
    if r:
        return r[0]
    r = con.execute("SELECT org_id FROM org_unit WHERE level=? AND parent_id IS NULL ORDER BY org_id LIMIT 1", (code,)).fetchone()
    if r:
        con.execute("UPDATE org_unit SET path_key=? WHERE org_id=?", (code, r[0]))
        return r[0]
    if stats is not None:
        stats["new_orgs"] += 1
    return con.execute("INSERT INTO org_unit(parent_id, name, level, path_key) VALUES(NULL,?,?,?)",
                       (name or levels.load()["root_name"], code, code)).lastrowid


def root_id(con, name=None) -> int:
    return _root(con, name)


def ensure_path(con, steps, root_name=None, stats=None) -> int:
    """경로의 조직을 위에서부터 찾고 없으면 만든다. 마지막 조직의 org_id 를 돌려준다. 커밋하지 않는다."""
    parent = _root(con, root_name, stats)
    for i, (lv, name) in enumerate(steps):
        key = key_of(steps[:i + 1])
        r = con.execute("SELECT org_id FROM org_unit WHERE path_key=?", (key,)).fetchone()
        if r:
            parent = r[0]
            continue
        parent = con.execute("INSERT INTO org_unit(parent_id, name, level, path_key) VALUES(?,?,?,?)", (parent, name, lv, key)).lastrowid
        if stats is not None:
            stats["new_orgs"] += 1
    return parent


def backfill(con, root_name=None) -> int:
    """경로 칸이 빈 예전 조직에 경로를 채운다. 뿌리 없이 맨 위에 있던 조직은 뿌리 아래로 옮긴다. 커밋하지 않는다."""
    if not con.execute("SELECT 1 FROM org_unit WHERE path_key IS NULL").fetchone():
        return 0
    rid = _root(con, root_name)
    con.execute("UPDATE org_unit SET parent_id=? WHERE parent_id IS NULL AND org_id<>?", (rid, rid))
    rows = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute("SELECT org_id, parent_id, name, level, path_key FROM org_unit")}

    def chain(oid):
        out = []
        while oid is not None and oid != rid:
            parent, name, lv, _ = rows[oid]
            out.append((lv, name))
            oid = parent
        return out[::-1]
    n = 0
    for oid, (_, _, _, key) in rows.items():
        if key is None and oid != rid:
            con.execute("UPDATE org_unit SET path_key=? WHERE org_id=?", (key_of(chain(oid)), oid))
            n += 1
    return n


def org_texts(con) -> dict:
    """{org_id: 뿌리 아래부터 이름을 이은 글}. 화면 표시용."""
    rows = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT org_id, parent_id, name, level FROM org_unit")}
    root = levels.root_code()
    out = {}

    def text(oid):
        if oid in out:
            return out[oid]
        parent, name, lv = rows[oid]
        if lv == root and parent is None:
            out[oid] = name
        else:
            up = text(parent) if parent in rows and rows[parent][2] != root else ""
            out[oid] = f"{up}{SEP}{name}" if up else name
        return out[oid]
    for oid in rows:
        text(oid)
    return out


def _finish(con, dry_run):
    if dry_run:
        con.rollback()
    else:
        con.commit()


# ---------------------------------------------------------------- 계약현황의 조직 경로
def contract_orgs(con, batch_id=None, stats=None) -> Counter:
    stats = stats if stats is not None else Counter()
    backfill(con)
    for n in org_path.org_nodes(con, batch_id):
        steps, root_name = steps_from("contract", dict(zip(org_path.LEVELS, n["path"])))
        ensure_path(con, steps, root_name, stats)
        stats["nodes"] += 1
    return stats


def sync_contract_orgs(con, batch_id=None, dry_run=False) -> dict:
    """계약 원장의 조직 경로에서 조직 마디를 만든다. 돌려주는 것: {nodes, new_orgs}."""
    stats = contract_orgs(con, batch_id)
    _finish(con, dry_run)
    return {"nodes": stats["nodes"], "new_orgs": stats["new_orgs"]}


# ---------------------------------------------------------------- 사원현황
def _ym(d):
    return d[:4] + d[5:7] if d else None


def _close_heads(con, agent_id, to_ym):
    """그 사람의 열린 조직장 지정을 to_ym 에 닫는다. 시작보다 앞이면 시작 달에 닫는다."""
    rows = con.execute("SELECT id, effective_from FROM org_manager WHERE agent_id=? AND (effective_to IS NULL OR effective_to>?)",
                       (agent_id, to_ym)).fetchall()
    for r in rows:
        con.execute("UPDATE org_manager SET effective_to=? WHERE id=?", (max(to_ym, r[1]), r[0]))
    return len(rows)


def _staff_heads(con, agent_id, position, steps, ym, stats, change):
    role = levels.position_role(position)
    want = None
    if role:
        lv = levels.level_of_role(role)
        idx = next((i for i, (lvl, _) in enumerate(steps) if lvl == lv), None)
        if idx is None:
            stats["head_without_org"] += 1
        else:
            oid = con.execute("SELECT org_id FROM org_unit WHERE path_key=?", (key_of(steps[:idx + 1]),)).fetchone()[0]
            want = (oid, role)
    prev = ym_add(ym, -1)
    for r in con.execute("""SELECT id, org_id, role FROM org_manager WHERE agent_id=? AND source='staff'
                            AND (effective_to IS NULL OR effective_to>=?)""", (agent_id, ym)).fetchall():
        if (r["org_id"], r["role"]) != want:
            con.execute("UPDATE org_manager SET effective_to=? WHERE id=?", (prev, r["id"]))
            stats["heads_end"] += 1
            change("heads_end", r["org_id"], None, levels.head_ko().get(r["role"], r["role"]))
    if want and not con.execute("""SELECT 1 FROM org_manager WHERE agent_id=? AND org_id=? AND role=? AND effective_from<=?
                                   AND (effective_to IS NULL OR effective_to>=?)""", (agent_id, want[0], want[1], ym, ym)).fetchone():
        con.execute("INSERT INTO org_manager(org_id, agent_id, role, effective_from, source, created_by, created_at) VALUES(?,?,?,?,'staff','사원현황',?)",
                    (want[0], agent_id, want[1], ym, db.now()))
        stats["heads_new"] += 1
        change("heads_new", None, want[0], levels.head_ko().get(want[1], want[1]))


def sync_staff(con, rows, effective_ym, dry_run=False) -> dict:
    """사원현황 rows 로 조직과 설계사를 맞춘다. effective_ym 은 이동과 조직장 지정이 시작되는 달이다.

    돌려주는 것: {stats: {STAT_KEYS: 건수}, changes: [{kind, code, name, before, after, note}]}.
    changes 의 before 와 after 는 조직 경로의 글이다. name 은 화면에만 쓴다."""
    stats, changes = Counter(), []
    prev = ym_add(effective_ym, -1)
    backfill(con)
    seen = set()
    for r in rows:
        stats["rows"] += 1
        code = r["employee_no"]
        seen.add(code)

        def change(kind, before=None, after=None, note=None, _r=r):
            changes.append({"kind": kind, "code": _r["employee_no"], "name": _r["name"], "before": before, "after": after, "note": note})
        steps, root_name = steps_from("employee", r.get("org") or {})
        if not steps:
            stats["no_org"] += 1
        org_id = ensure_path(con, steps, root_name, stats)
        term, hire = r.get("termination_date"), r.get("hire_date")
        a = con.execute("SELECT * FROM agent WHERE internal_code=?", (code,)).fetchone()

        if not a:
            start = min(_ym(hire), effective_ym) if hire else effective_ym
            aid = con.execute("INSERT INTO agent(internal_code, name, hire_date, termination_date, status, job_type) VALUES(?,?,?,?,?,'fc')",
                              (code, r["name"], hire, term, "terminated" if term else "active")).lastrowid
            stats["new_agents"] += 1
            change("new_agents", None, org_id, "퇴사자" if term else None)
            if term:
                con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from, effective_to) VALUES(?,?,?,?)",
                            (aid, org_id, start, max(start, _ym(term))))
                stats["terminated"] += 1
                continue
            con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (aid, org_id, start))
            _staff_heads(con, aid, r.get("position"), steps, effective_ym, stats, change)
            continue

        aid = a["agent_id"]
        if r["name"] and r["name"] != a["name"]:
            con.execute("UPDATE agent SET name=? WHERE agent_id=?", (r["name"], aid))
            stats["renamed"] += 1
        if hire and not a["hire_date"]:
            con.execute("UPDATE agent SET hire_date=? WHERE agent_id=?", (hire, aid))
        cur = con.execute("SELECT * FROM agent_org_history WHERE agent_id=? AND effective_to IS NULL ORDER BY effective_from DESC LIMIT 1",
                          (aid,)).fetchone()

        if term:
            if a["status"] != "terminated" or a["termination_date"] != term:
                was_active = a["status"] != "terminated"
                con.execute("UPDATE agent SET status='terminated', termination_date=? WHERE agent_id=?", (term, aid))
                if cur:
                    con.execute("UPDATE agent_org_history SET effective_to=? WHERE id=?", (max(cur["effective_from"], _ym(term)), cur["id"]))
                _close_heads(con, aid, _ym(term))
                con.execute("UPDATE org_deputy SET date_to=? WHERE agent_id=? AND date_to>? AND ended_at IS NULL", (term, aid, term))
                if was_active:
                    stats["terminated"] += 1
                    change("terminated", cur["org_id"] if cur else None, None, term)
            continue

        if a["status"] == "terminated":
            con.execute("UPDATE agent SET status='active', termination_date=NULL WHERE agent_id=?", (aid,))
            stats["reinstated"] += 1
            change("reinstated", None, org_id)
            if cur:                                        # 퇴사로 닫혔어야 하지만 열려 있으면 그 달 앞에서 닫는다
                con.execute("UPDATE agent_org_history SET effective_to=? WHERE id=?", (max(cur["effective_from"], prev), cur["id"]))
            cur = None
            con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (aid, org_id, effective_ym))
        elif cur is None:
            con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (aid, org_id, effective_ym))
            stats["placed"] += 1
            change("placed", None, org_id)
        elif cur["org_id"] != org_id:
            if cur["effective_from"] >= effective_ym:
                con.execute("UPDATE agent_org_history SET org_id=? WHERE id=?", (org_id, cur["id"]))
            else:
                con.execute("UPDATE agent_org_history SET effective_to=? WHERE id=?", (prev, cur["id"]))
                con.execute("INSERT INTO agent_org_history(agent_id, org_id, effective_from) VALUES(?,?,?)", (aid, org_id, effective_ym))
            stats["moved"] += 1
            change("moved", cur["org_id"], org_id)
        _staff_heads(con, aid, r.get("position"), steps, effective_ym, stats, change)

    active = {x[0] for x in con.execute("SELECT internal_code FROM agent WHERE status='active'")}
    stats["missing"] = len(active - seen)
    texts = org_texts(con)                                 # 미리보기에서 새로 만든 조직은 되돌리면 사라지므로 그 전에 글로 바꾼다
    for c in changes:
        c["before"], c["after"] = texts.get(c["before"], ""), texts.get(c["after"], "")
    _finish(con, dry_run)
    return {"stats": {k: stats[k] for k in STAT_KEYS}, "changes": changes}


def termination_candidates(con) -> list:
    """퇴사일이 있는데 계정이 아직 쓰이는 사람. 자동으로 중지하지 않고 메인 관리자가 본다."""
    return [dict(r) for r in con.execute(
        """SELECT u.user_id, u.login_id, u.display_name, a.agent_id, a.internal_code, a.termination_date, u.last_login_at
           FROM user_account u JOIN agent a ON a.agent_id=u.agent_id
           WHERE a.termination_date IS NOT NULL AND u.status='active' AND u.is_main_admin=0 ORDER BY a.termination_date, a.internal_code""")]
