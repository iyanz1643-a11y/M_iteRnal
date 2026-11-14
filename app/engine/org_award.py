"""조직 부문 시상: 부문 등록, 순위 계산, 확정.

부문 하나 = 지표(인정보험료, 보험료, 건수) + 조건(업권, 구분) + 범위(부문을 연 조직과 그 아래) + 선발 인원.
이터널의 인정업적 부문과 손해보험 건수 부문이 이 틀로 들어간다.
"""
import calendar
import json

from app import db
from app.engine.performance import ACTIVE_STATUSES as ACTIVE

METRIC_KO = {"converted_premium": "인정보험료", "premium_monthly": "보험료", "policy_count": "건수"}
SECTOR_KO = {"life": "생명보험", "nonlife": "손해보험", None: "전 업권", "": "전 업권"}
SHOW_TOP = 10          # 설계사에게는 상위 10명과 본인 순위만 보인다


def _descendants(con, org_id) -> set:
    out, frontier = {org_id}, {org_id}
    while frontier:
        q = ",".join("?" * len(frontier))
        frontier = {r["org_id"] for r in con.execute(f"SELECT org_id FROM org_unit WHERE parent_id IN ({q})", tuple(frontier))} - out
        out |= frontier
    return out


def _ancestors(con, org_id) -> set:
    out = set()
    while org_id is not None and org_id not in out:
        out.add(org_id)
        r = con.execute("SELECT parent_id FROM org_unit WHERE org_id=?", (org_id,)).fetchone()
        org_id = r["parent_id"] if r else None
    return out


def _clean(name, metric, sector, categories, top_n, min_value, rewards, effective_from):
    if not (name or "").strip():
        raise ValueError("부문 이름을 넣어야 합니다.")
    if metric not in METRIC_KO:
        raise ValueError("지표 값이 맞지 않습니다.")
    if sector not in ("life", "nonlife", None, ""):
        raise ValueError("업권 값이 맞지 않습니다.")
    top_n = int(top_n or 0)
    if not 1 <= top_n <= 50:
        raise ValueError("선발 인원은 1명에서 50명 사이입니다.")
    if not (effective_from and len(effective_from) == 6 and effective_from.isdigit()):
        raise ValueError("적용 시작 월은 YYYYMM 꼴입니다.")
    if isinstance(categories, (list, tuple, set)):
        categories = ",".join(sorted(categories))
    rewards = [(int(r), (lab or "").strip()) for r, lab in rewards if (lab or "").strip()]
    if any(not 1 <= r <= top_n for r, _ in rewards) or len({r for r, _ in rewards}) != len(rewards):
        raise ValueError("시상 내용의 순위가 선발 인원을 넘거나 겹칩니다.")
    return name.strip(), metric, (sector or None), (categories or None), top_n, (float(min_value) if min_value not in (None, "") else None), rewards


def save(con, *, by, org_id, name, metric, top_n, effective_from, sector=None, categories=None, min_value=None, rewards=(), note=None,
         effective_to=None, division_id=None) -> int:
    """부문을 새로 만들거나 고친다. 고칠 때는 고치기 전과 뒤를 변경 기록에 남긴다."""
    name, metric, sector, categories, top_n, min_value, rewards = _clean(name, metric, sector, categories, top_n, min_value, rewards, effective_from)
    if not con.execute("SELECT 1 FROM org_unit WHERE org_id=?", (org_id,)).fetchone():
        raise ValueError("없는 조직입니다.")
    before = load(con, division_id) if division_id else None
    if division_id:
        if not before:
            raise ValueError("없는 부문입니다.")
        con.execute("""UPDATE org_award_division SET org_id=?, name=?, metric=?, sector=?, categories=?, top_n=?, min_value=?, effective_from=?, effective_to=?,
                       note=?, updated_by=?, updated_at=? WHERE division_id=?""",
                    (org_id, name, metric, sector, categories, top_n, min_value, effective_from, effective_to or None, note, by, db.now(), division_id))
        con.execute("DELETE FROM org_award_reward WHERE division_id=?", (division_id,))
    else:
        division_id = con.execute(
            """INSERT INTO org_award_division(org_id, name, metric, sector, categories, top_n, min_value, effective_from, effective_to, note, created_by, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, name, metric, sector, categories, top_n, min_value, effective_from, effective_to or None, note, by, db.now())).lastrowid
    for rank_no, label in rewards:
        con.execute("INSERT INTO org_award_reward(division_id, rank_no, reward_label) VALUES(?,?,?)", (division_id, rank_no, label))
    after = load(con, division_id)
    pick = lambda d: {k: d[k] for k in ("org_id", "name", "metric", "sector", "categories", "top_n", "min_value", "effective_from", "effective_to", "rewards")} if d else None
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), by, "org_award_update" if before else "org_award_create", str(division_id),
                 json.dumps(pick(before), ensure_ascii=False) if before else None, json.dumps(pick(after), ensure_ascii=False)))
    con.commit()
    return division_id


def set_status(con, division_id, status, by):
    if status not in ("active", "inactive"):
        raise ValueError(status)
    con.execute("UPDATE org_award_division SET status=?, updated_by=?, updated_at=? WHERE division_id=?", (status, by, db.now(), division_id))
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), by, f"org_award_{status}", str(division_id)))
    con.commit()


def load(con, division_id) -> dict | None:
    d = con.execute("""SELECT d.*, o.name org_name, o.level org_level FROM org_award_division d JOIN org_unit o ON o.org_id=d.org_id WHERE d.division_id=?""",
                    (division_id,)).fetchone()
    if not d:
        return None
    d = dict(d)
    d["rewards"] = {r["rank_no"]: r["reward_label"] for r in con.execute("SELECT rank_no, reward_label FROM org_award_reward WHERE division_id=? ORDER BY rank_no", (division_id,))}
    d["metric_ko"], d["sector_ko"] = METRIC_KO[d["metric"]], SECTOR_KO[d["sector"]]
    d["category_list"] = d["categories"].split(",") if d["categories"] else []
    d["unit"] = "건" if d["metric"] == "policy_count" else "원"
    return d


def can_manage(ctx, org_id) -> bool:
    """메인 관리자는 모든 조직, 관리자는 자기 조직과 그 아래의 부문을 다룬다."""
    return ctx["role"] == "main" or (ctx["role"] == "manager" and org_id in ctx["org_ids"])


def visible(con, ctx, ym, include_inactive=False) -> list:
    """그 달에 적용되는 부문 가운데 이 사람이 볼 수 있는 것. 자기 조직의 위 조직이 연 부문과, 자기 범위 안의 조직이 연 부문이다."""
    rows = [load(con, r["division_id"]) for r in con.execute(
        "SELECT division_id FROM org_award_division WHERE effective_from<=? AND (effective_to IS NULL OR effective_to>=?) ORDER BY org_id, division_id", (ym, ym))]
    if not include_inactive:
        rows = [d for d in rows if d["status"] == "active"]
    if ctx["role"] == "main":
        return rows
    mine = set(ctx["org_ids"])
    for org in [ctx["home_org"]["org_id"]] if ctx.get("home_org") else []:
        mine |= _ancestors(con, org)
    for h in ctx.get("heads", []):
        mine |= _ancestors(con, h["org_id"])
    return [d for d in rows if d["org_id"] in mine]


def standing(con, d, ym) -> list:
    """부문의 그 달 순위. 업적 집계와 같은 규칙을 쓴다: 성립한 달에 더하고, 철회한 달에 뺀다. 같은 값이면 같은 순위다."""
    y, m = int(ym[:4]), int(ym[4:])
    first, last = f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
    orgs = _descendants(con, d["org_id"])
    q = ",".join("?" * len(orgs))
    cond, extra = "", []
    if d["sector"]:
        cond += " AND i.sector=?"
        extra.append(d["sector"])
    if d["category_list"]:
        cond += f" AND pr.product_category IN ({','.join('?' * len(d['category_list']))})"
        extra += d["category_list"]

    def total(where, params):
        sql = f"""SELECT c.agent_id, SUM(c.{d['metric']}) v FROM contract c JOIN insurer i ON i.insurer_id=c.insurer_id
                  LEFT JOIN product pr ON pr.product_id=c.product_id WHERE {where}{cond} GROUP BY c.agent_id"""
        return {r["agent_id"]: r["v"] or 0 for r in con.execute(sql, params + extra)}
    made = total(f"c.status IN ({','.join('?' * len(ACTIVE))}) AND c.contract_date BETWEEN ? AND ?", [*ACTIVE, first, last])
    undone = total("c.status='withdrawn' AND c.withdrawal_date BETWEEN ? AND ?", [first, last])
    people = {r["agent_id"]: dict(r) for r in con.execute(
        f"""SELECT a.agent_id, a.internal_code code, a.name, o.name org FROM agent a
            JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?) AND h.org_id IN ({q})
            JOIN org_unit o ON o.org_id=h.org_id WHERE a.status='active'""", (ym, ym, *orgs))}
    rows = [{**people[a], "v": made.get(a, 0) - undone.get(a, 0)} for a in people]
    rows = [r for r in rows if r["v"] > 0 and (not d["min_value"] or r["v"] >= d["min_value"])]
    rows.sort(key=lambda r: (-r["v"], r["code"]))
    rank, prev = 0, None
    for i, r in enumerate(rows, start=1):
        if r["v"] != prev:
            rank, prev = i, r["v"]
        r["rank"], r["value"] = rank, r.pop("v")
        r["winner"] = rank <= d["top_n"]
        r["reward"] = d["rewards"].get(rank) if r["winner"] else None
    return rows


def confirm(con, division_id, ym, by) -> int:
    """그 달의 수상자를 확정해 남긴다. 다시 확정하면 앞의 확정을 바꾼다."""
    d = load(con, division_id)
    if not d:
        raise ValueError("없는 부문입니다.")
    winners = [r for r in standing(con, d, ym) if r["winner"]]
    con.execute("DELETE FROM org_award_result WHERE division_id=? AND period_ym=?", (division_id, ym))
    for r in winners:
        con.execute("INSERT INTO org_award_result(division_id, period_ym, rank_no, agent_id, value, reward_label, confirmed_by, confirmed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (division_id, ym, r["rank"], r["agent_id"], r["value"], r["reward"], by, db.now()))
    con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), by, "org_award_confirm", f"{division_id}:{ym}:{len(winners)}명"))
    con.commit()
    return len(winners)


def confirmed(con, division_id, ym) -> list:
    return [dict(r) for r in con.execute(
        """SELECT r.rank_no rank, r.agent_id, a.name, a.internal_code code, r.value, r.reward_label reward, r.confirmed_by, r.confirmed_at
           FROM org_award_result r JOIN agent a ON a.agent_id=r.agent_id WHERE r.division_id=? AND r.period_ym=? ORDER BY r.rank_no, a.internal_code""",
        (division_id, ym))]


def view_rows(rows, ctx) -> list:
    """메인 관리자는 전부, 그 밖에는 상위 10명과 자기 범위의 사람만 본다."""
    if ctx["role"] == "main":
        return rows
    return [r for r in rows if r["rank"] <= SHOW_TOP or r["agent_id"] in ctx["agent_ids"]]
