"""계약 원장에 남긴 조직 경로(본사, 본부, 지점, 센터, 팀)를 꺼내는 함수.

센터와 팀은 사람 이름으로 붙은 경우가 많아 이름이 겹친다. 그래서 조직 하나는 이름이 아니라
위에서부터의 경로(튜플)로 식별한다. 조직 계층 테이블은 이 함수들의 결과로 auth-org 가 만든다.
경로 튜플은 늘 다섯 자리이고 빈 계층은 None 이다. 센터 없이 팀만 있는 경로는 적재 때 경고한다.
"""
LEVELS = ("org_head_office", "org_hq", "org_branch", "org_center", "org_team")
LEVEL_KO = ("본사", "본부", "지점", "센터", "팀")
SEP = " > "


def path_of(row) -> tuple:
    """표준 행이나 contract 행에서 다섯 자리 경로를 만든다."""
    get = row.get if isinstance(row, dict) else (lambda k: row[k])
    out = []
    for k in LEVELS:
        v = get(k)
        v = v.strip() if isinstance(v, str) else v
        out.append(v or None)
    return tuple(out)


def trim(path) -> tuple:
    """끝의 빈 계층을 뺀 경로."""
    p = list(path)
    while p and p[-1] is None:
        p.pop()
    return tuple(p)


def path_text(path) -> str:
    return SEP.join(v for v in path if v)


def has_gap(path) -> bool:
    """중간 계층이 비어 있는가. 예: 센터 없이 팀만 있다."""
    t = trim(path)
    return any(v is None for v in t)


def contract_org_path(con, contract_id) -> tuple | None:
    r = con.execute(f"SELECT {', '.join(LEVELS)} FROM contract WHERE contract_id=?", (contract_id,)).fetchone()
    return path_of(dict(zip(LEVELS, r))) if r else None


def _where(batch_id):
    return ("WHERE last_batch_id=?", (batch_id,)) if batch_id is not None else ("", ())


def org_paths(con, batch_id=None) -> list:
    """원장에 있는 서로 다른 경로와 계약 건수. batch_id 를 주면 그 배치가 마지막으로 실어 온 계약만 본다."""
    where, params = _where(batch_id)
    rows = con.execute(f"SELECT {', '.join(LEVELS)}, COUNT(*) n FROM contract {where} GROUP BY {', '.join(LEVELS)}", params).fetchall()
    out = []
    for r in rows:
        p = trim(path_of(dict(zip(LEVELS, tuple(r)[:5]))))
        if p:
            out.append({"path": p, "text": path_text(p), "depth": len(p), "contracts": r[5]})
    return sorted(out, key=lambda d: tuple(v or "" for v in d["path"]))


def org_nodes(con, batch_id=None) -> list:
    """경로에 나오는 모든 조직 마디. 마디는 경로 앞부분(prefix) 튜플로 식별한다.
    돌려주는 것: [{level, level_ko, name, path, parent, contracts}] 을 위에서 아래로."""
    nodes = {}
    for p in org_paths(con, batch_id):
        path = p["path"]
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            if prefix[-1] is None:
                continue
            parent = tuple(v for v in path[:depth - 1])
            parent = trim(parent) or None
            n = nodes.setdefault(prefix, {"level": LEVELS[depth - 1], "level_ko": LEVEL_KO[depth - 1], "name": prefix[-1],
                                          "path": prefix, "parent": parent, "contracts": 0})
            n["contracts"] += p["contracts"]
    return sorted(nodes.values(), key=lambda d: (len(d["path"]), tuple(v or "" for v in d["path"])))


def shared_names(con, batch_id=None) -> dict:
    """같은 이름이 서로 다른 상위 아래에 나오는 조직. {계층: {이름: 상위 경로 수}}. 이름만으로 식별하면 안 되는 것들이다."""
    seen = {}
    for n in org_nodes(con, batch_id):
        seen.setdefault((n["level_ko"], n["name"]), set()).add(n["parent"])
    out = {}
    for (lv, name), parents in seen.items():
        if len(parents) > 1:
            out.setdefault(lv, {})[name] = len(parents)
    return out
