"""제도의 기관별 기준 버전과 공통 도우미 (파트 I, S4).

기준 버전
- 판정할 때 쓴 기준 줄의 사본을 standard_version 에 남기고, 판정이 어느 판을 썼는지 standard_use 에 잇는다.
- 판은 (scope, std_key, year) 안에서 1 부터 센다. 같은 해 기준이 고쳐지면 내용 해시가 달라 판이 하나 는다.
  같은 내용이면 같은 판을 다시 쓴다. 해가 바뀌면 같은 내용이어도 그 해의 1판이 새로 생긴다.
- 인증 계산 run(certification.run)과 커리어 회차(career.open_round)가 판정할 때 record() 를 부른다.
- 커리어 기준 표에는 주관 기관 칸이 없어 자사(CAREER_ISSUER)로 본다. 가정이다.

메인 관리자 확인(require_main)은 promotion._require_main 과 같은 규칙이다. 계정 표에서 다시 확인한다.

D-33 (열린 결정, 지금 동작 그대로 둔 가정). 인증의 연 인정보험료와 연 초회보험료는 올해 1월부터 기준월까지 계약일로 세고
철회된 계약은 뺀다. 업적 집계는 성립한 달에 더하고 철회한 달에 뺀다. 바꾸지 않고 화면에 보일 설명만 BASIS_NOTE 로 둔다.
"""
import hashlib
import json

from app import db

BEGIN, END = "-- [program_s4:begin]", "-- [program_s4:end]"
CAREER_ISSUER = "자사"                                    # 커리어 기준의 주관 기관. 표에 칸이 없어 둔 가정
REVOKE_MIN = 5                                          # 무르는 사유의 최소 글자 수. 프로모션 확정 무르기 D-50 가정과 같다
ESTIMATE_LABEL = "예상"

# D-33 기준 설명. 업적 집계와 다르게 세는 지표에만 붙인다
D33_METRICS = ("annual_recognized_premium", "annual_first_premium", "org_recognized_premium")
BASIS_NOTE = ("인증의 연 보험료 지표는 올해 1월부터 기준월까지 계약일로 세고 철회된 계약은 뺍니다. "
              "실적 화면은 성립한 달에 더하고 철회한 달에 빼므로 숫자가 다를 수 있습니다.")
BASIS_DECISION = "D-33"


def ensure(con) -> None:
    """오래된 DB 에 schema.sql 의 [program_s4] 묶음을 돌린다. 이미 있는 표와 색인은 건너뛴다."""
    text = db.SCHEMA.read_text(encoding="utf-8")
    block = text[text.index(BEGIN):text.index(END)]
    body = "\n".join(line.split("--", 1)[0] for line in block.splitlines())
    for stmt in (x.strip() for x in body.split(";")):
        if stmt:
            con.execute(stmt)


def require_main(con, by) -> str:
    """메인 관리자만 통과한다. by 는 login_id 이거나 auth.context() 의 결과다. 계정 표에서 다시 확인한다."""
    if isinstance(by, dict):
        if by.get("role") != "main":
            raise PermissionError("메인 관리자만 할 수 있습니다.")
        by = by.get("login_id")
    row = con.execute("SELECT is_main_admin FROM user_account WHERE login_id=? AND status='active'", (by,)).fetchone() if by else None
    if not row or not row["is_main_admin"]:
        raise PermissionError("메인 관리자만 할 수 있습니다.")
    return by


def clean_reason(reason) -> str:
    reason = " ".join((reason or "").split())
    if len(reason) < REVOKE_MIN:
        raise ValueError(f"무르는 사유를 {REVOKE_MIN}자 이상 적어야 합니다.")
    return reason


def scope_ids(agent_ids) -> list:
    """범위 안 설계사 목록을 정수 목록으로. 순서는 받은 차례, 겹친 것은 뺀다."""
    out = []
    for x in agent_ids or ():
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v not in out:
            out.append(v)
    return out


def estimate(value) -> dict:
    """예상 값. 화면은 label 을 값 옆에 붙인다."""
    return {"value": value, "is_estimate": True, "label": ESTIMATE_LABEL}


def basis(metric) -> dict | None:
    """그 지표에 붙일 기준 설명. D-33 이 닿는 지표만 돌려준다."""
    if metric not in D33_METRICS:
        return None
    return {"note": BASIS_NOTE, "decision": BASIS_DECISION, "assumed": True}


# ---------------------------------------------------------------- 기준 버전
def content_hash(criteria: list) -> str:
    raw = json.dumps(sorted(criteria, key=lambda c: json.dumps(c, sort_keys=True, ensure_ascii=False)), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _label(year, version_no) -> str:
    return f"{year}년 기준 {version_no}판"


def _row(r) -> dict:
    d = dict(r)
    d["criteria"] = json.loads(d.pop("criteria_json"))
    d["label"] = _label(d["year"], d["version_no"])
    return d


def find(con, scope, std_key, year, criteria) -> dict | None:
    """같은 내용의 판이 이미 있으면 그 판. 없으면 None. 남기지 않는다."""
    ensure(con)
    r = con.execute("SELECT * FROM standard_version WHERE scope=? AND std_key=? AND year=? AND content_hash=?",
                    (scope, std_key, int(year), content_hash(criteria))).fetchone()
    return _row(r) if r else None


def record(con, scope, std_key, year, criteria, *, issuer=None, by="system", used_in=None, ref_id=None) -> int:
    """기준 줄 criteria(dict 목록)의 판을 찾거나 새로 남기고 version_id 를 돌려준다. 커밋은 부른 쪽이 한다.
    used_in 과 ref_id 를 주면 그 판정이 이 판을 썼다고 standard_use 에 잇는다."""
    ensure(con)
    year, h = int(year), content_hash(criteria)
    r = con.execute("SELECT version_id FROM standard_version WHERE scope=? AND std_key=? AND year=? AND content_hash=?",
                    (scope, std_key, year, h)).fetchone()
    if r:
        vid = r["version_id"]
    else:
        n = con.execute("SELECT COALESCE(MAX(version_no),0) n FROM standard_version WHERE scope=? AND std_key=? AND year=?",
                        (scope, std_key, year)).fetchone()["n"]
        vid = con.execute("""INSERT INTO standard_version(scope, std_key, issuer, year, version_no, content_hash, criteria_json, recorded_by, recorded_at)
                             VALUES(?,?,?,?,?,?,?,?,?)""",
                          (scope, std_key, issuer, year, n + 1, h, json.dumps(criteria, ensure_ascii=False, sort_keys=True), str(by), db.now())).lastrowid
    if used_in and ref_id is not None:
        con.execute("INSERT OR IGNORE INTO standard_use(version_id, used_in, ref_id, used_at) VALUES(?,?,?,?)", (vid, used_in, int(ref_id), db.now()))
    return vid


def get(con, version_id) -> dict | None:
    """판 하나. {version_id, scope, std_key, issuer, year, version_no, label, content_hash, criteria, recorded_by, recorded_at}"""
    if version_id is None:
        return None
    ensure(con)
    r = con.execute("SELECT * FROM standard_version WHERE version_id=?", (version_id,)).fetchone()
    return _row(r) if r else None


def used_by(con, used_in, ref_id) -> dict:
    """그 판정(run 또는 회차)이 쓴 판들. {std_key: 판}"""
    ensure(con)
    return {r["std_key"]: _row(r) for r in con.execute(
        """SELECT v.* FROM standard_use u JOIN standard_version v ON v.version_id=u.version_id
           WHERE u.used_in=? AND u.ref_id=? ORDER BY v.std_key""", (used_in, int(ref_id)))}


def versions(con, *, scope=None, std_key=None, year=None, issuer=None) -> list:
    """남은 판 목록. 기관, 기준, 해, 판 차례. 판마다 그 판을 쓴 판정 수(n_used)를 붙인다."""
    ensure(con)
    sql, params = "SELECT v.*, (SELECT COUNT(*) FROM standard_use u WHERE u.version_id=v.version_id) n_used FROM standard_version v WHERE 1=1", []
    for col, val in (("scope", scope), ("std_key", std_key), ("year", year), ("issuer", issuer)):
        if val is not None:
            sql += f" AND v.{col}=?"
            params.append(val)
    return [_row(r) for r in con.execute(sql + " ORDER BY v.issuer, v.scope, v.std_key, v.year, v.version_no", params)]


def cert_criteria(con, program_id, year) -> list:
    """인증 프로그램 한 해의 기준 줄. 판의 내용이 되는 칸만 담는다."""
    return [dict(r) for r in con.execute(
        """SELECT criteria_group, metric, metric_param, operator, value, evidence_path FROM certification_criteria
           WHERE program_id=? AND year=? ORDER BY criteria_group, metric, COALESCE(metric_param,''), id""", (program_id, int(year)))]


def career_criteria(con, from_level_id, to_level_id, ym) -> list:
    """그 달에 걸린 커리어 단계 한 짝의 기준 줄."""
    return [dict(r) for r in con.execute(
        """SELECT metric, period_type, operator, value, effective_from, effective_to FROM career_promotion_criteria
           WHERE from_level_id=? AND to_level_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)
           ORDER BY metric, period_type, id""", (from_level_id, to_level_id, ym, ym))]


def standards(con, year, as_of_ym) -> list:
    """주관 기관별 기준. 인증 프로그램은 그 해 기준, 커리어는 as_of_ym 에 걸린 기준이다.

    돌려주는 것: [{issuer, items: [{scope, std_key, name, category, criteria, version}]}] 기관 이름 차례.
    version 은 지금 기준과 내용이 같은 판이다. 아직 판정에 쓰지 않았으면 None 이고 화면은 '판정 전'으로 보인다."""
    ensure(con)
    groups = {}
    for p in con.execute("SELECT * FROM certification_program WHERE active=1 ORDER BY sort_order, code"):
        crit = cert_criteria(con, p["program_id"], year)
        if not crit:
            continue
        groups.setdefault(p["issuer"] or "미지정", []).append(
            {"scope": "cert", "std_key": p["code"], "name": p["name"], "category": p["category"], "criteria": crit,
             "version": find(con, "cert", p["code"], year, crit)})
    pairs = con.execute(
        """SELECT DISTINCT c.from_level_id, c.to_level_id, f.code fc, f.name fn, t.code tc, t.name tn, f.rank_order fo
           FROM career_promotion_criteria c JOIN career_level f ON f.level_id=c.from_level_id JOIN career_level t ON t.level_id=c.to_level_id
           WHERE c.effective_from<=? AND (c.effective_to IS NULL OR c.effective_to>=?) ORDER BY f.rank_order, t.rank_order""",
        (as_of_ym, as_of_ym)).fetchall()
    for r in pairs:
        crit = career_criteria(con, r["from_level_id"], r["to_level_id"], as_of_ym)
        key = f"{r['fc']}>{r['tc']}"
        groups.setdefault(CAREER_ISSUER, []).append(
            {"scope": "career", "std_key": key, "name": f"{r['fn']}에서 {r['tn']}", "category": "career", "criteria": crit,
             "version": find(con, "career", key, int(as_of_ym[:4]), crit)})
    return [{"issuer": k, "items": v} for k, v in sorted(groups.items())]
