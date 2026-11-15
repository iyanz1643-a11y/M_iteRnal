"""S2 남은 화면 일: 조직 부문 시상 내용의 공개 규칙(D-45 가정), 위 관리자의 공지 보기(가정), 프로모션 기준 안내(D-08 가정),
계약 원장 /perf/contracts (D-05, D-17).

| 무엇 | 보는 것 |
| --- | --- |
| 시상 내용 | 조직 부문 시상과 명예의전당이 같은 순위별 시상표를 세 역할 모두에게 보인다. 합계는 없다. 두 화면이 awards.py 한 곳을 쓴다 |
| 공지 | 지점 대상 공지를 그 위의 관리자도 본다. 다른 지점 관리자와 대상 밖 설계사는 못 본다. 역할이 설계사인 글은 관리자가 못 본다 |
| 기준 안내 | 달성 현황, 내 달성 현황, 내 시상 진척에 보험사 기준, 기준일, 철회 제외 안내가 있다 |
| 계약 목록 | 이름을 가린다. 수수료 칸이 없다. 거르기가 범위를 넘지 않는다 |
| 계약 상세 | 담당 설계사와 직속 관리자(그 달 조직장과 대리)만 이름을 본다. 위 관리자는 가린 채로, 범위 밖 관리자와 설계사는 403 |
| 메인 관리자 | 사유를 적어야 이름을 본다. 사유와 본 일이 변경 기록에 남는다 |
| CSV | 이름을 가린 채로 범위 안 계약만 담는다. 내려받은 일이 변경 기록에 남는다 |

자료는 run_demo 의 샘플을 임시 폴더에 새로 만든다. data/demo.db 는 건드리지 않는다.
사람 이름과 금액은 실패 메시지에 넣지 않는다. 화면 주소, 계약 번호, 칸 이름만 넣는다.
"""
import csv
import io
import json
import os
import pathlib
import re
import shutil
import tempfile
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import org_award as oa
from app.periods import ym_add
from app.web import auth

PW = "finish-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
ROOT = pathlib.Path(db.ROOT)


class Client(TestClient):
    """요청마다 이 시험의 DB 를 가리킨다. 다른 시험 모듈이 APP_DB 를 바꿔 두어도 섞이지 않는다."""

    def __init__(self, app, db_path):
        super().__init__(app, follow_redirects=False)
        self.db_path = str(db_path)

    def request(self, *a, **kw):
        os.environ["APP_DB"] = self.db_path
        return super().request(*a, **kw)

    def post(self, url, data=None, **kw):
        if data is not None and "csrf_token" not in data:
            data = {**data, "csrf_token": TOKEN.search(self.get("/login").text).group(1)}
        return super().post(url, data=data, **kw)


def fill_names(con):
    """샘플 계약의 계약자 이름은 이미 가린 꼴(가운데 *)이라 가림을 알아볼 수 없다. 이 사본에만 가리지 않은 가짜 이름을 넣는다.
    피보험자도 넣는다. 가짜 이름은 계약 번호로 만들어 서로 다르다."""
    con.execute("UPDATE contract SET policyholder_name='시험고객' || printf('%04d', contract_id) || '님', insured_name='시험피보' || printf('%04d', contract_id) || '님'")


def _uid(con, agent_id):
    return con.execute("SELECT login_id FROM user_account WHERE agent_id=?", (agent_id,)).fetchone()[0]


@pytest.fixture(scope="module")
def env():
    import run_demo
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="s2finish-"))
    keys = ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR", "APP_BOARD_DIR")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"),
                      APP_BOARD_DIR=str(tmp / "board"))
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    fill_names(con)
    con.commit()
    ym = auth.current_ym(con)
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    # 지점 B 와 그 조직장, B 의 위 조직(본부) 조직장, 다른 본부의 지점장
    b = con.execute("""SELECT m.org_id, m.agent_id, o.parent_id FROM org_manager m JOIN org_unit o ON o.org_id=m.org_id
                       WHERE o.level='branch' AND m.effective_to IS NULL ORDER BY o.org_id LIMIT 1""").fetchone()
    up = con.execute("SELECT agent_id FROM org_manager WHERE org_id=? AND effective_to IS NULL", (b["parent_id"],)).fetchone()[0]
    ctx = lambda a: auth.context(con, con.execute("SELECT user_id FROM user_account WHERE agent_id=?", (a,)).fetchone()[0])
    up_ctx = ctx(up)
    other = next(r[0] for r in con.execute("""SELECT m.agent_id FROM org_manager m JOIN org_unit o ON o.org_id=m.org_id
                                              WHERE o.level='branch' AND m.effective_to IS NULL ORDER BY o.org_id""")
                 if r[0] != b["agent_id"] and b["org_id"] not in ctx(r[0])["org_ids"] and r[0] not in up_ctx["agent_ids"])
    # 지점 B 에 바로 속한 설계사 가운데 계약자 이름이 있는 계약이 가장 많은 사람과, 다른 지점의 설계사
    agents = [r[0] for r in con.execute("""SELECT h.agent_id FROM agent_org_history h JOIN agent a ON a.agent_id=h.agent_id
                                            JOIN contract c ON c.agent_id=h.agent_id AND c.policyholder_name IS NOT NULL
                                            WHERE h.org_id=? AND h.effective_to IS NULL AND a.status='active'
                                            GROUP BY h.agent_id ORDER BY COUNT(c.contract_id) DESC, h.agent_id""", (b["org_id"],))
              if r[0] not in heads]
    who, mate = agents[0], agents[1]
    outsider = next(r[0] for r in con.execute("""SELECT a.agent_id FROM agent a JOIN user_account u ON u.agent_id=a.agent_id
                                                 WHERE a.status='active' ORDER BY a.agent_id""")
                    if r[0] not in up_ctx["agent_ids"] and r[0] not in heads)
    from app.web.main import app
    e = {"tmp": tmp, "db": path, "app": app, "con": con, "ym": ym, "branch": b["org_id"], "hq": b["parent_id"],
         "head": b["agent_id"], "up": up, "other": other, "who": who, "mate": mate, "outsider": outsider}
    e["login_of"] = lambda a: _uid(con, a)
    yield e
    con.close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


_clients = {}


def login(env, login_id):
    key = (str(env["db"]), login_id)
    if key not in _clients:
        c = Client(env["app"], env["db"])
        r = c.post("/login", data={"login_id": login_id, "password": PW})
        assert r.status_code == 303, (login_id, r.status_code)
        _clients[key] = c
    return _clients[key]


def as_agent(env, agent_id):
    return login(env, env["login_of"](agent_id))


def holder(con, contract_id) -> tuple:
    r = con.execute("SELECT policyholder_name, insured_name FROM contract WHERE contract_id=?", (contract_id,)).fetchone()
    return r[0], r[1]


def masked(name) -> str:
    """시험이 스스로 만드는 가린 이름. 화면 코드의 mask 를 부르지 않는다."""
    s = re.sub(r"\s+", "", name)
    return "*" if len(s) == 1 else (s[0] + "*" if len(s) == 2 else s[0] + "*" * (len(s) - 2) + s[-1])


def audit_rows(con, action, since_id):
    return [dict(r) for r in con.execute("SELECT * FROM audit_log WHERE action=? AND id>? ORDER BY id", (action, since_id))]


def last_audit(con):
    return con.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]


# ================================================================ D-45 시상 내용의 공개 규칙
PRIZES = {1: "시험상품 삼십만 원", 2: "시험상품 이십만 원", 3: "시험상품 십만 원"}


@pytest.fixture(scope="module")
def division(env):
    """본부에 연 부문 하나. 전월에 세 명을 확정해 둔다. 이번 달에도 적용된다."""
    con, prev = env["con"], ym_add(env["ym"], -1)
    did = oa.save(con, by="admin", org_id=env["hq"], name="시험 공개 부문", metric="converted_premium", top_n=3, effective_from=prev,
                  rewards=list(PRIZES.items()))
    ranked = [env["mate"], env["who"], env["head"]]               # 2위가 설계사 본인이 아니어도 그 줄의 시상이 보이는지 본다
    for rank, agent in enumerate(ranked, start=1):
        con.execute("""INSERT INTO org_award_result(division_id, period_ym, rank_no, agent_id, value, reward_label, confirmed_by, confirmed_at)
                       VALUES(?,?,?,?,?,?,?,?)""", (did, prev, rank, agent, 1000 * (10 - rank), PRIZES[rank], "admin", db.now()))
    con.commit()
    return did


def _prize_chips(html) -> dict:
    return {int(k): v for k, v in re.findall(r'data-prize-rank="(\d+)">\d+위 · ([^<]+)</span>', html)}


def _part(html) -> str:
    """시험 부문의 묶음 하나. 부문 이름에서 그 카드의 끝까지다."""
    return html.split("시험 공개 부문", 1)[1].split("</section>", 1)[0]


@pytest.mark.parametrize("role", ["main", "manager", "agent"])
def test_d45_prize_table_is_public_and_same_on_both_screens(env, division, role):
    """순위별 시상 내용은 세 역할 모두에게 같은 상품표로 보인다. 조직 부문 시상과 명예의전당이 같다."""
    c = login(env, "admin") if role == "main" else as_agent(env, env["head"] if role == "manager" else env["who"])
    awards, hall = c.get("/promo/org-awards"), c.get("/perf/hall")
    assert awards.status_code == hall.status_code == 200
    for screen, html in (("/promo/org-awards", awards.text), ("/perf/hall", hall.text)):
        assert "시험 공개 부문" in html, (role, screen)
        assert _prize_chips(_part(html)) == PRIZES, (role, screen, "순위별 시상표")
        assert "모두에게 공개하는 상품표" in _part(html), (role, screen)
    # 명예의전당의 수상자 줄: 남의 줄에도 그 순위의 시상이 적힌다. 1위는 다른 설계사다
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", _part(hall.text), re.S)
    assert sum(1 for r in rows if PRIZES[1] in r) == 1, (role, "1위 줄의 시상")


def test_d45_no_sum_of_rewards_for_manager(env, division):
    """관리자에게 팀원이 받을 금액의 합계는 없다. 시상 글을 더한 값도, 합계라는 칸도 없다."""
    from app.web import awards
    c = as_agent(env, env["head"])
    for path in ("/promo/org-awards", "/perf/hall"):
        html = c.get(path).text
        part = _part(html).replace(awards.PUBLIC_NOTE, "")
        assert "육십만" not in part and "합계" not in part, (path, "시상 합계")


def test_d45_both_screens_use_one_rule():
    """두 화면이 같은 규칙 한 곳(app/web/awards.py)을 부르고, 역할로 시상 글을 지우는 코드가 남아 있지 않다."""
    perf = (ROOT / "app/web/routes/perf.py").read_text(encoding="utf-8")
    promo = (ROOT / "app/web/routes/promo.py").read_text(encoding="utf-8")
    assert "awards.label_rows(" in perf and "awards.label_rows(" in promo
    assert "awards.confirmed_prizes(" in perf and "awards.prizes(" in promo
    assert not re.search(r'\["reward"\]\s*=\s*None', perf + promo), "화면 라우터에서 시상 글을 지우는 줄"
    from app.web import awards
    rows = [{"rank": 1, "agent_id": 1, "winner": True, "reward": "가"}, {"rank": 4, "agent_id": 2, "winner": False, "reward": "나"}]
    assert [r["reward"] for r in awards.label_rows([dict(r) for r in rows])] == ["가", None]
    assert awards.prizes({2: "나", 1: "가", 4: "라"}, top_n=3) == [{"rank": 1, "reward": "가"}, {"rank": 2, "reward": "나"}]


# ================================================================ 공지: 위 관리자
def _post(c, *, title, org="", role=""):
    data = {"action": "save", "category": "notice", "title": title, "body": "본문입니다", "target_org_id": org, "target_role": role, "post_id": ""}
    return c.post("/board", data=data)


def test_upper_manager_sees_branch_notice(env):
    admin = login(env, "admin")
    child = env["con"].execute("SELECT org_id FROM org_unit WHERE parent_id=? ORDER BY org_id LIMIT 1", (env["branch"],)).fetchone()
    posts = [("지점 공지 위관리자", env["branch"], ""), ("지점 설계사만 위관리자", env["branch"], "agent")]
    if child:
        posts.append(("지점 아래 조직 공지", child[0], ""))
    for title, org, role in posts:
        assert _post(admin, title=title, org=org, role=role).status_code == 303
    seen = lambda a: {t for t, _, _ in posts if f">{t}<" in as_agent(env, a).get("/board").text}
    assert seen(env["up"]) == {t for t, _, r in posts if r == ""}               # 본부장은 지점과 그 아래 대상 공지를 본다. 설계사만 글은 아니다
    assert "지점 공지 위관리자" in seen(env["head"])
    assert seen(env["other"]) == set()                                          # 다른 본부의 지점장
    assert seen(env["who"]) == {"지점 공지 위관리자", "지점 설계사만 위관리자"}       # 설계사는 지금처럼 대상 안일 때만
    assert seen(env["outsider"]) == set()
    pid = env["con"].execute("SELECT post_id FROM board_post WHERE title='지점 공지 위관리자'").fetchone()[0]
    assert as_agent(env, env["up"]).get(f"/board?post={pid}").status_code == 200
    assert as_agent(env, env["other"]).get(f"/board?post={pid}").status_code == 404


# ================================================================ D-08 기준 안내
def test_promo_progress_shows_basis_note(env):
    for c, paths in ((as_agent(env, env["who"]), ("/promo/progress", "/my/awards")), (as_agent(env, env["head"]), ("/promo/progress", "/promo/progress?view=me")),
                     (login(env, "admin"), ("/promo/progress",))):
        for path in paths:
            html = c.get(path).text
            notes = re.findall(r'data-note="basis">([^<]+)<', html)
            assert notes, (path, "기준 안내")
            for n in notes:
                assert "업적 집계와 다를 수 있습니다" in n and "보험사" in n, (path, "보험사 기준")
                assert "철회" in n and "일까지" in n, (path, "철회 제외와 기준일")


# ================================================================ 계약 원장
def _contract(env, agent_id):
    """그 설계사의 계약 가운데 계약자 이름이 있고, 업적 귀속 월에 지점 B 소속이던 것."""
    return env["con"].execute("""SELECT c.contract_id, c.perf_ym FROM contract c JOIN agent_org_history h ON h.agent_id=c.agent_id AND h.org_id=?
                                 AND h.effective_from<=c.perf_ym AND (h.effective_to IS NULL OR h.effective_to>=c.perf_ym)
                                 WHERE c.agent_id=? AND c.policyholder_name IS NOT NULL AND c.perf_ym IS NOT NULL
                                 ORDER BY c.contract_id LIMIT 1""", (env["branch"], agent_id)).fetchone()


def test_contract_list_masks_names_and_has_no_commission(env):
    con = env["con"]
    for c, ids in ((as_agent(env, env["who"]), {env["who"]}), (as_agent(env, env["head"]), None), (login(env, "admin"), None)):
        html = c.get("/perf/contracts?ym=all").text
        shown = [int(x) for x in re.findall(r'data-contract="(\d+)"', html)]
        assert shown, "목록이 비었다"
        for cid in shown:
            name, insured = holder(con, cid)
            leak = name in html or (insured is not None and insured in html)
            assert not leak, (cid, "목록의 계약자 또는 피보험자 이름")
            if ids is not None:
                assert con.execute("SELECT agent_id FROM contract WHERE contract_id=?", (cid,)).fetchone()[0] in ids, (cid, "범위 밖 계약")
        ok = masked(holder(con, shown[0])[0]) in html
        assert ok, "가린 이름"
        table = re.search(r'<table class="contract-table">.*?</table>', html, re.S).group(0)
        assert "수수료" not in table and "시상" not in table, "수수료와 시상 칸"
    # 설계사는 다른 조직을 골라 넓힐 수 없다
    assert as_agent(env, env["who"]).get(f"/perf/contracts?org={env['branch']}").status_code == 403
    assert as_agent(env, env["head"]).get(f"/perf/contracts?org={env['hq']}").status_code == 403


def test_contract_detail_names_follow_direct_manager_rule(env):
    con = env["con"]
    row = _contract(env, env["who"])
    assert row, "지점 B 설계사의 계약"
    cid = row["contract_id"]
    name, insured = holder(con, cid)
    url = f"/perf/contracts?id={cid}"
    start = last_audit(con)
    # 담당 설계사, 직속 관리자(그 달 지점장)는 본다
    for who, why in ((env["who"], "own"), (env["head"], "head")):
        r = as_agent(env, who).get(url)
        assert r.status_code == 200 and name in r.text and 'data-names="shown"' in r.text, (cid, why)
        if insured:
            assert insured in r.text, (cid, why, "피보험자")
    views = audit_rows(con, "contract_name_view", start)
    assert [json.loads(v["after_json"])["why"] for v in views] == ["own", "head"]
    assert all(v["target"] == f"contract:{cid}" for v in views)
    # 위 조직의 관리자는 범위 안이라 열리지만 이름은 가린다. 같은 지점 다른 설계사는 범위 밖이다
    r = as_agent(env, env["up"]).get(url)
    assert r.status_code == 200 and name not in r.text and masked(name) in r.text and 'data-names="masked"' in r.text
    start = last_audit(con)
    for who in (env["other"], env["mate"], env["outsider"]):
        r = as_agent(env, who).get(url)
        assert r.status_code == 403 and name not in r.text, (cid, "범위 밖")
    denied = audit_rows(con, "access_denied", start)
    assert len(denied) == 3 and all(d["target"] == f"/perf/contracts contract:{cid}" for d in denied)
    assert not audit_rows(con, "contract_name_view", start)


def test_contract_detail_deputy_of_that_month(env):
    """대리는 그 달과 기간이 겹칠 때 직속 관리자다. 기간이 겹치지 않으면 가린다."""
    con = env["con"]
    row = _contract(env, env["who"])
    cid, ym = row["contract_id"], row["perf_ym"]
    name = holder(con, cid)[0]
    today = date.today()
    later = f"{today.year + 1}-12-31"
    month_first = f"{ym[:4]}-{ym[4:]}-01"
    con.execute("""INSERT INTO org_deputy(org_id, agent_id, date_from, date_to, reason, created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                (env["branch"], env["mate"], month_first, later, "시험", "admin", db.now()))
    con.commit()
    try:
        _clients.pop((str(env["db"]), env["login_of"](env["mate"])), None)
        r = as_agent(env, env["mate"]).get(f"/perf/contracts?id={cid}")
        assert r.status_code == 200 and name in r.text, (cid, "그 달의 대리")
        con.execute("UPDATE org_deputy SET date_from=? WHERE agent_id=? AND org_id=?", (today.isoformat(), env["mate"], env["branch"]))
        con.commit()
        if today.isoformat()[:7].replace("-", "") > ym:
            r = as_agent(env, env["mate"]).get(f"/perf/contracts?id={cid}")
            assert r.status_code == 200 and name not in r.text, (cid, "그 달이 아닌 대리")
    finally:
        con.execute("DELETE FROM org_deputy WHERE agent_id=? AND org_id=?", (env["mate"], env["branch"]))
        con.commit()
        _clients.pop((str(env["db"]), env["login_of"](env["mate"])), None)


def test_main_admin_sees_names_only_after_reason(env):
    con = env["con"]
    cid = _contract(env, env["who"])["contract_id"]
    name = holder(con, cid)[0]
    admin = login(env, "admin")
    r = admin.get(f"/perf/contracts?id={cid}")
    assert r.status_code == 200 and name not in r.text and 'name="reason"' in r.text
    start = last_audit(con)
    r = admin.post("/perf/contracts", data={"action": "reveal", "id": cid, "reason": "짧음"})
    assert r.status_code == 400 and name not in r.text
    assert not audit_rows(con, "contract_name_view", start)
    r = admin.post("/perf/contracts", data={"action": "reveal", "id": cid, "reason": "보험사 민원 확인 시험"})
    assert r.status_code == 200 and name in r.text
    views = audit_rows(con, "contract_name_view", start)
    assert len(views) == 1 and views[0]["actor"] == "admin" and views[0]["target"] == f"contract:{cid}"
    assert json.loads(views[0]["after_json"]) == {"why": "main", "reason": "보험사 민원 확인 시험"}
    # 관리자는 사유를 적어도 볼 수 없다
    start = last_audit(con)
    r = as_agent(env, env["up"]).post("/perf/contracts", data={"action": "reveal", "id": cid, "reason": "보험사 민원 확인 시험"})
    assert r.status_code == 403 and name not in r.text
    assert audit_rows(con, "access_denied", start) and not audit_rows(con, "contract_name_view", start)


def test_csv_export_is_masked_scoped_and_logged(env):
    con = env["con"]
    for who, scope in (("admin", None), (env["head"], "org"), (env["who"], "self")):
        c = login(env, who) if who == "admin" else as_agent(env, who)
        start = last_audit(con)
        r = c.get("/perf/contracts?ym=all&export=csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
        body = r.content.decode("utf-8-sig")
        lines = list(csv.reader(io.StringIO(body)))
        head, data = lines[0], lines[1:]
        assert "계약자" in head and "피보험자" in head and not any("수수료" in h for h in head)
        assert data, (scope, "CSV 가 비었다")
        nos = {(d[0] or d[1]) for d in data}
        ids = [r[0] for r in con.execute("SELECT contract_id FROM contract WHERE COALESCE(policy_no, application_no) IN ({})".format(
            ",".join("?" * len(nos))), sorted(nos))]
        for cid in ids:
            name, insured = holder(con, cid)
            assert name not in body and (insured is None or insured not in body), (scope, cid, "CSV 의 이름")
        if scope == "self":
            assert {r[0] for r in con.execute(f"SELECT DISTINCT agent_id FROM contract WHERE contract_id IN ({','.join('?' * len(ids))})", ids)} == {env["who"]}
        if scope == "org":
            ctx = auth.context(con, con.execute("SELECT user_id FROM user_account WHERE agent_id=?", (env["head"],)).fetchone()[0])
            want = con.execute(f"SELECT COUNT(*) FROM contract WHERE agent_id IN ({','.join('?' * len(ctx['agent_ids']))})",
                               sorted(ctx["agent_ids"])).fetchone()[0]
            assert len(data) == want, (scope, "범위 안 계약 수")
        logs = audit_rows(con, "contract_export", start)
        assert len(logs) == 1 and logs[0]["target"] == f"rows:{len(data)}" and json.loads(logs[0]["after_json"])["masked"] is True


def test_audit_page_labels_contract_actions(env):
    html = login(env, "admin").get("/org/audit").text
    assert "계약자·피보험자 이름 조회" in html or "계약 원장 내려받음" in html
    from app.web import audit_view as av
    assert av.action_ko("contract_name_view") != "contract_name_view" and av.action_ko("contract_export") != "contract_export"
    assert av.target_ko("access_denied", "/perf/contracts contract:7", av._Names(env["con"])) == "화면: 계약 원장 · 계약 7번"
