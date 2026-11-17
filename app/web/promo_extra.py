"""프로모션 화면 칸: 최다 달성 조직 보상 글, 달성자가 고르는 보상, 보상 선택과 참석 기록.

계산은 하지 않는다. 달성 여부와 구간은 엔진(app/engine/promotion.py)이 내고, 여기는 사람이 적은 것을 남기고 읽기만 한다.
칸과 표는 schema.sql 의 [promotion_screen] 묶음이고, 오래된 DB 에는 ensure() 가 붙인다.

- top_org_reward: 최다 달성 조직에게 주는 보상 글. 비면 조직별 보기에 최다 달성 조직을 따로 보이지 않는다.
- reward_choices: 달성자가 고르는 보상 코드 목록. 지금은 여행1, 여행2, 금 셋이다 (CHOICE_KO).
- promotion_reward_choice: 달성자마다 고른 보상과 여행 참석 여부. 메인 관리자만 적고 변경 기록에 남긴다.
  사람은 사번으로만 남기고 이름은 남기지 않는다.
- promotion_version_note: 고친 판의 '바뀐 점' 메모. 판 이력(versions)이 고치기의 변경 기록과 함께 읽는다.
"""
import json
import sqlite3

from app import db

BEGIN, END = "-- [promotion_screen:begin]", "-- [promotion_screen:end]"
CHOICE_KO = {"travel1": "여행1", "travel2": "여행2", "gold": "금"}
ATTEND_KO = {"yes": "참석", "no": "불참"}
TRAVEL_CHOICES = ("travel1", "travel2")
CASH_NOTE = "현금 전환 조건: 환율 1,600원 초과, 차수당 30명 미만, 골드바 3.75g 85만원 초과. 적용은 보험사 원문을 따릅니다."


def ensure(con) -> None:
    """[promotion_screen] 묶음을 돌린다. 이미 있는 칸과 표는 건너뛴다."""
    text = db.SCHEMA.read_text(encoding="utf-8")
    block = text[text.index(BEGIN):text.index(END)]
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


def screen(p) -> dict:
    """프로모션의 화면 칸. 칸이 없는 오래된 DB 의 프로모션이면 빈 값이다."""
    raw = p.get("reward_choices") or ""
    return {"top_org_reward": (p.get("top_org_reward") or "").strip() or None,
            "reward_choices": [c for c in raw.split(",") if c in CHOICE_KO]}


def is_travel(p) -> bool:
    """참석 여부를 적는 여행 시상인가. 유형이 여행이거나 구간 보상이 여행이다."""
    return p["kind"] == "travel" or any(t["reward_kind"] == "travel" for t in p.get("tiers", ()))


def set_screen(con, promo_id, *, by, top_org_reward=None, reward_choices=None) -> list:
    """화면 칸을 바꾼다. 바뀐 칸 이름 목록을 돌려주고, 바뀌었으면 변경 기록에 남긴다."""
    ensure(con)
    row = con.execute("SELECT top_org_reward, reward_choices FROM promotion WHERE promo_id=?", (promo_id,)).fetchone()
    if not row:
        raise ValueError("없는 프로모션입니다.")
    choices = [c for c in (reward_choices or []) if c in CHOICE_KO]
    if reward_choices and len(choices) != len(set(reward_choices)):
        raise ValueError("고르는 보상 값이 맞지 않습니다.")
    after = {"top_org_reward": (top_org_reward or "").strip()[:100] or None, "reward_choices": ",".join(choices) or None}
    before = {"top_org_reward": row["top_org_reward"], "reward_choices": row["reward_choices"]}
    changed = sorted(k for k in after if after[k] != before[k])
    if changed:
        con.execute("UPDATE promotion SET top_org_reward=?, reward_choices=? WHERE promo_id=?", (after["top_org_reward"], after["reward_choices"], promo_id))
        con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                    (db.now(), by, "promotion_screen", f"{promo_id}:{','.join(changed)}",
                     json.dumps({k: before[k] for k in changed}, ensure_ascii=False), json.dumps({k: after[k] for k in changed}, ensure_ascii=False)))
        con.commit()
    return changed


def choices(con, promo_id) -> dict:
    """{agent_id: {choice, attend, recorded_by, recorded_at}}"""
    ensure(con)
    return {r["agent_id"]: dict(r) for r in con.execute(
        "SELECT agent_id, choice, attend, recorded_by, recorded_at FROM promotion_reward_choice WHERE promo_id=?", (promo_id,))}


def record(con, p, *, by, allowed: dict, picks: dict) -> int:
    """보상 선택과 참석 여부를 남긴다. 메인 관리자인지는 부르는 쪽(라우터)이 먼저 본다.

    allowed 는 {agent_id: 사번} 으로 기록할 수 있는 달성자다. picks 는 {agent_id: (choice 또는 None, attend 또는 None)} 이다.
    allowed 밖의 사람이나 모르는 값이 있으면 아무것도 남기지 않고 ValueError 를 낸다.
    금을 고르면 참석 여부를 비운다. 여행 시상이 아니고 여행을 고르지도 않았으면 참석 여부를 받지 않는다.
    바뀐 사람 수를 돌려준다. 바뀐 사람이 있으면 사번으로 전과 뒤를 변경 기록에 남긴다."""
    ensure(con)
    opts = screen(p)["reward_choices"]
    travel = is_travel(p)
    now = choices(con, p["promo_id"])
    rows = []
    for aid, (choice, attend) in picks.items():
        if aid not in allowed:
            raise ValueError("달성자가 아닌 사람은 기록하지 않습니다.")
        choice = choice or None
        attend = attend or None
        if choice is not None and choice not in opts:
            raise ValueError("고를 수 없는 보상입니다.")
        if attend is not None and attend not in ATTEND_KO:
            raise ValueError("참석 여부 값이 맞지 않습니다.")
        if choice == "gold" or not (travel or choice in TRAVEL_CHOICES):
            attend = None
        old = now.get(aid) or {}
        if (old.get("choice"), old.get("attend")) != (choice, attend):
            rows.append((aid, choice, attend, old))
    if not rows:
        return 0
    at = db.now()
    for aid, choice, attend, _old in rows:
        con.execute("""INSERT INTO promotion_reward_choice(promo_id, agent_id, choice, attend, recorded_by, recorded_at) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(promo_id, agent_id) DO UPDATE SET choice=excluded.choice, attend=excluded.attend,
                       recorded_by=excluded.recorded_by, recorded_at=excluded.recorded_at""", (p["promo_id"], aid, choice, attend, by, at))
    before = {allowed[a]: {"choice": o.get("choice"), "attend": o.get("attend")} for a, _c, _t, o in rows}
    after = {allowed[a]: {"choice": c, "attend": t} for a, c, t, _o in rows}
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (at, by, "promotion_reward_choice", f"{p['promo_id']}:{len(rows)}명", json.dumps(before, ensure_ascii=False),
                 json.dumps(after, ensure_ascii=False)))
    con.commit()
    return len(rows)


# ---------------------------------------------------------------- 판 이력
# 프로모션의 판은 등록한 판(1차 원안)과 고친 판(2차 변경부터)이다. 고치기의 변경 기록(audit_log 의 promotion_update)을 그대로 읽는다.
# 바뀐 것이 없는 고치기는 판으로 세지 않는다. 바뀐 점은 칸 이름과 보상 이름의 앞뒤만 보인다. 금액은 넣지 않는다.
VERSION_FIELD_KO = {"name": "이름", "kind": "유형", "insurer_name": "보험사", "sector": "업권", "categories": "구분", "metric": "기준 실적",
                    "date_basis": "기준일", "per_contract_cap": "건당 한도", "combine_rule": "결합 규칙", "pay_timing": "지급 시점",
                    "target_note": "대상 상품", "caution": "유의사항", "windows": "기간 창", "tiers": "구간", "window_tiers": "창별 구간표",
                    "inst13_required": "13회차 유지 조건", "rules": "조건 확장"}
NO_CHANGE = "바뀐 것 없음"


def _updates(con, promo_ids=None) -> list:
    sql = "SELECT id, at, actor, target, before_json, after_json FROM audit_log WHERE action='promotion_update'"
    rows = []
    for r in con.execute(sql + " ORDER BY at, id"):
        pid, _, fields = (r["target"] or "").partition(":")
        if not pid.isdigit() or not fields or fields == NO_CHANGE:
            continue
        if promo_ids is not None and int(pid) not in promo_ids:
            continue
        rows.append({**dict(r), "promo_id": int(pid), "fields": fields.split(",")})
    return rows


def revised_counts(con, promo_ids) -> dict:
    """{promo_id: 고친 판 수}. 카드의 '변경' 표시에 쓴다."""
    ids = set(promo_ids)
    out = {}
    for r in _updates(con, ids):
        out[r["promo_id"]] = out.get(r["promo_id"], 0) + 1
    return out


def _labels(rows) -> list:
    """구간표 줄 [(기준, 종류, 값, 이름)] 에서 보상 이름만. 같은 이름은 한 번."""
    out = []
    for t in rows or []:
        lab = t[3] if len(t) > 3 else None
        if lab and lab not in out:
            out.append(lab)
    return out


def add_version_note(con, promo_id, note, *, by) -> bool:
    """가장 최근 고치기(promotion_update) 한 줄에 '바뀐 점' 메모를 붙인다. 바뀐 것이 없는 고치기면 붙이지 않는다."""
    note = (note or "").strip()[:300]
    if not note:
        return False
    ensure(con)
    ups = _updates(con, {promo_id})
    if not ups:
        return False
    con.execute("""INSERT INTO promotion_version_note(promo_id, audit_id, note, written_by, written_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(audit_id) DO UPDATE SET note=excluded.note, written_by=excluded.written_by, written_at=excluded.written_at""",
                (promo_id, ups[-1]["id"], note, by, db.now()))
    con.commit()
    return True


def _notes(con, promo_id) -> dict:
    try:
        return {r["audit_id"]: r["note"] for r in con.execute("SELECT audit_id, note FROM promotion_version_note WHERE promo_id=?", (promo_id,))}
    except sqlite3.OperationalError:                             # 표가 없는 오래된 DB
        return {}


def _man(v) -> str:
    v = float(v or 0)
    return f"{v / 10_000:g}만" if v % 10_000 == 0 else f"{int(v):,}"


def _floors(wt) -> list:
    """창별 구간표 {창: [(기준, ...)]} 에서 창마다 첫 구간 기준."""
    return [_man(rows[0][0]) for _seq, rows in sorted((wt or {}).items(), key=lambda x: int(x[0])) if rows]


def _boost_ko(b) -> str | None:
    if not b:
        return None
    if b.get("month_days"):
        d = b["month_days"]
        return f"2배 인정 매월 {d[0]}~{d[1]}일" if b.get("factor") == 2 else f"{b.get('factor'):g}배 인정 매월 {d[0]}~{d[1]}일"
    months = sorted({f"{int(f[5:7])}월" for f, _t in b.get("ranges") or []}, key=lambda x: int(x[:-1]))
    return f"{b.get('factor', 2):g}배 인정 {', '.join(months)}"


def _summary(snap) -> list:
    """판 하나의 요약: 창 기준, 합산 기준, 가중 인정, 보상 이름. 금액은 기준 실적만 쓰고 시상 금액은 넣지 않는다."""
    out = []
    fl = _floors(snap.get("window_tiers"))
    if fl:
        out.append("연속 " + ", ".join(fl))
    tiers = snap.get("tiers") or []
    if fl and tiers:
        out.append(f"합산 {_man(tiers[0][0])}")
    bk = _boost_ko((snap.get("rules") or {}).get("boost"))
    if bk:
        out.append(bk)
    labs = _labels(tiers)
    if labs:
        out.append("보상 " + ", ".join(labs))
    return out


def versions(con, p) -> list:
    """판 이력 [{no, kind, kind_ko, at, fields_ko, points}]. 1차는 등록한 날의 원안이다. 고친 판이 없으면 빈 목록이다."""
    ups = _updates(con, {p["promo_id"]})
    if not ups:
        return []
    notes = _notes(con, p["promo_id"])
    now = {"window_tiers": {str(k): [(t["min_value"], t["reward_kind"], t["reward_value"], t["reward_label"]) for t in rows]
                            for k, rows in (p.get("window_tiers") or {}).items()},
           "tiers": [(t["min_value"], t["reward_kind"], t["reward_value"], t["reward_label"]) for t in p.get("tiers", ()) if t.get("window_seq") is None],
           "rules": p.get("rules") or {}}
    snaps = [now]                                                # 지금 판에서 거꾸로 앞 판을 만든다
    for u in reversed(ups):
        snaps.insert(0, {**snaps[0], **json.loads(u["before_json"] or "{}")})
    out = [{"no": 1, "kind": "original", "kind_ko": "원안", "at": (p.get("created_at") or "")[:10], "fields_ko": [], "points": [],
            "summary": _summary(snaps[0])}]
    for i, u in enumerate(ups, start=2):
        before, after = json.loads(u["before_json"] or "{}"), json.loads(u["after_json"] or "{}")
        points = []
        for key in ("tiers", "window_tiers"):
            if key in after:
                b = _labels(before.get(key) if key == "tiers" else [t for rows in (before.get(key) or {}).values() for t in rows])
                a = _labels(after.get(key) if key == "tiers" else [t for rows in (after.get(key) or {}).values() for t in rows])
                if a != b and a:
                    text = f"보상: {', '.join(b) or '-'} → {', '.join(a)}"
                    if text not in points:
                        points.append(text)
        if notes.get(u["id"]):
            points.insert(0, notes[u["id"]])
        out.append({"no": i, "kind": "revised", "kind_ko": "변경", "at": (u["at"] or "")[:10],
                    "fields_ko": [VERSION_FIELD_KO.get(f, f) for f in u["fields"]], "points": points, "summary": _summary(snaps[i - 1])})
    return out
