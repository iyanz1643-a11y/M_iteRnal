"""S1 결정 가운데 누가 무엇을 보는가에 닿는 두 가지를 웹 화면 전체에서 지킨다. 대표님 확정 2026-11-14.

- D-05: 계약자와 피보험자 이름은 저장하되 담당 설계사와 그 직속 관리자만 본다. 목록에서는 가린다.
- D-17: 관리자는 팀원의 수수료와 시상 금액을 보지 않는다. 예상 금액도 같다. 실적과 달성 여부만 본다.

대상 화면은 config/menu.yaml 의 화면 가운데 그 역할에게 열린 것 전부다. built 화면과, 같은 주소의 다른 보기
(달성 현황의 프로모션 고르기와 내 현황, 부문 만들기와 고치기, 구간 입력의 고치기와 시상안 줄, 조직장 화면의 처리 뒤)
와, 메인 관리자가 폼을 보내 받는 화면(사원현황 미리보기, 계약현황으로 조직 만들기, 조직장 지정 오류, 계정 일괄 발급)을 연다.
planned 와 legacy 메뉴도 기획 안내 화면이 열리므로 함께 본다.

자료는 두 가지 임시 DB 다. 하나는 run_demo 의 샘플 전체(수수료, 시상, 환수, 프로모션, 조직 부문)이고, 하나는
tests/test_s1_verify.py 의 가짜 실제 양식(계약자와 피보험자 이름이 원장과 raw_row 에 모두 있다)이다. data/demo.db 는 건드리지 않는다.

이름과 금액 값은 출력하지 않는다. 실패 메시지에는 계정의 역할, 화면 주소, 칸 이름만 넣는다.
"""
import json
import os
import pathlib
import re
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engine import org_award as oa
from app.engine import promotion as pm
from app.web import auth, menu
from tests.test_s1_verify import _build as build_fake_real

PW = "decide-pass-1234"
TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
NAME_KEYS = ("계약자", "피보험자")                      # raw_row 의 원본 머리글. 샘플 양식은 계약자만 있다
# 화면의 숫자 하나. 금액은 won 필터로 '1,234,567' 꼴이 되고, 꾸밈 없이 찍히면 '1234567' 이다.
# 영숫자, 밑줄, 점, 쉼표에 붙은 숫자는 숫자 하나로 보지 않는다. CSRF 토큰, 소수, 날짜의 일부를 걸러 낸다
NUM = re.compile(r"(?<![0-9A-Za-z_.,])(\d{1,3}(?:,\d{3})+|\d+)(?![0-9A-Za-z_,]|\.\d)")
MIN_AMOUNT = 1000                                    # 이보다 작은 값은 건수, 순위, 퍼센트와 겹치기 쉬워 보지 않는다


def _from_menu(role):
    return [it for g in menu.load()["groups"] for it in g["items"] if role in it["roles"]]


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


def _set_password(path):
    con = db.connect(path)
    con.execute("UPDATE user_account SET password_hash=?, must_change_password=0, failed_count=0, locked_until=NULL", (auth.hash_password(PW),))
    con.commit()
    con.close()


def _build_sample(tmp: pathlib.Path) -> pathlib.Path:
    import run_demo
    path = tmp / "sample.db"
    run_demo.main(path, tmp / "raw")
    return path


def _build_fake_real(tmp: pathlib.Path) -> pathlib.Path:
    e = build_fake_real(tmp)
    e["con"].close()
    return tmp / "real.db"


BUILDERS = {"sample": _build_sample, "fake_real": _build_fake_real}


@pytest.fixture(scope="module", params=sorted(BUILDERS))
def env(request):
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"s1decide-{request.param}-"))
    old = {k: os.environ.get(k) for k in ("APP_DB", "CONNECT_DATA_DIR", "CONNECT_STATE_DIR", "APP_UPLOAD_DIR")}
    os.environ.update(CONNECT_DATA_DIR=str(tmp / "data"), CONNECT_STATE_DIR=str(tmp / "state"), APP_UPLOAD_DIR=str(tmp / "up"))
    path = BUILDERS[request.param](tmp)
    _set_password(path)
    from app.web.main import app
    yield {"kind": request.param, "tmp": tmp, "db": path, "app": app}
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)


def _copy(env, name):
    dst = env["tmp"] / f"{name}.db"
    shutil.copy(env["db"], dst)
    return dst


def login(env, login_id, path=None):
    c = Client(env["app"], path or env["db"])
    r = c.post("/login", data={"login_id": login_id, "password": PW})
    assert r.status_code == 303, (login_id, r.status_code)
    return c


# ---------------------------------------------------------------- 사람 고르기
def _people(con):
    """메인 관리자, 조직 단계마다 한 명씩의 관리자, 설계사 둘. 관리자와 설계사는 사번으로 들어간다."""
    out = [("main", "admin")]
    seen = set()
    for r in con.execute("""SELECT a.internal_code code, o.level, a.agent_id FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                            JOIN org_unit o ON o.org_id=m.org_id JOIN user_account u ON u.agent_id=a.agent_id
                            WHERE a.status='active' AND m.effective_to IS NULL ORDER BY o.level, a.internal_code"""):
        if r["level"] not in seen:
            seen.add(r["level"])
            out.append((f"manager:{r['level']}", r["code"]))
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    agents = [r for r in con.execute("""SELECT a.agent_id, a.internal_code code, COUNT(c.contract_id) n FROM agent a
                                        JOIN user_account u ON u.agent_id=a.agent_id LEFT JOIN contract c ON c.agent_id=a.agent_id
                                        WHERE a.status='active' GROUP BY a.agent_id ORDER BY n DESC, a.internal_code""") if r["agent_id"] not in heads]
    out += [("agent", agents[0]["code"]), ("agent", agents[-1]["code"])]
    return out


def _ctx(con, login_id):
    uid = con.execute("SELECT user_id FROM user_account WHERE login_id=?", (login_id,)).fetchone()[0]
    return auth.context(con, uid)


# ---------------------------------------------------------------- 열 화면
def _screens(con, ctx):
    """이 역할로 여는 GET 주소. 메뉴 전부와 같은 주소의 다른 보기."""
    role = ctx["role"]
    paths = [it["path"] for it in _from_menu(role)]
    promos = [r[0] for r in con.execute("SELECT promo_id FROM promotion ORDER BY promo_id")]
    divisions = [r[0] for r in con.execute("SELECT division_id FROM org_award_division ORDER BY division_id")]
    if role in ("main", "manager"):
        paths += [f"/promo/progress?p={p}" for p in promos]
        paths += ["/promo/org-awards?new=1"] + [f"/promo/org-awards?edit={d}" for d in divisions if oa.can_manage(ctx, oa.load(con, d)["org_id"])]
    if role in ("manager", "agent"):
        paths.append("/promo/progress?view=me")
    if role == "main":
        paths += [f"/promo/tiers?edit={p}" for p in promos]
        paths += [f"/promo/tiers?line={r[0]}" for r in con.execute("SELECT line_id FROM incentive_plan_line WHERE recipient_level='fc' ORDER BY line_id LIMIT 5")]
        paths += ["/org/heads?done=assign"]
    return paths


def _open_all(c, con, ctx, tmp):
    """{주소: HTML}. 열리지 않는 화면이 있으면 그 자리에서 실패한다."""
    pages = {}
    for path in _screens(con, ctx):
        r = c.get(path)
        assert r.status_code == 200, (ctx["role"], path, r.status_code)
        pages[path] = r.text
    if ctx["role"] == "main":
        from app.loader import fake_eternal as fe
        staff, _ = fe.make_employee_file(tmp / "staff_upload.xlsx")
        r = c.post("/data/staff", data={"action": "preview", "effective_ym": ctx["ym"]},
                   files={"file": ("staff.xlsx", staff.read_bytes(), "application/octet-stream")})
        assert r.status_code == 200, ("main", "POST /data/staff preview", r.status_code)
        pages["POST /data/staff preview"] = r.text
        r = c.post("/org/heads", data={"action": "assign", "code": "NO-SUCH-CODE", "from_ym": ctx["ym"]})
        assert r.status_code == 400, ("main", "POST /org/heads", r.status_code)
        pages["POST /org/heads 오류"] = r.text
        r = c.post("/org/accounts", data={"action": "issue_all"})
        assert r.status_code == 200, ("main", "POST /org/accounts issue_all", r.status_code)
        pages["POST /org/accounts issue_all"] = r.text
        r = c.post("/data/staff", data={"action": "contract_orgs"})                      # 조직을 바꾸는 처리라 마지막에 한다
        assert r.status_code == 200, ("main", "POST /data/staff contract_orgs", r.status_code)
        pages["POST /data/staff contract_orgs"] = r.text
    return pages


# ================================================================ D-05 계약자와 피보험자 이름
def _holder_names(con) -> set:
    names = set()
    for col in ("policyholder_name", "insured_name"):
        names |= {r[0].strip() for r in con.execute(f"SELECT DISTINCT {col} FROM contract WHERE {col} IS NOT NULL")}
    for (raw,) in con.execute("SELECT raw_json FROM raw_row"):
        row = json.loads(raw)
        names |= {str(row[k]).strip() for k in NAME_KEYS if row.get(k) not in (None, "")}
    return {n for n in names if n and n != "-"}


def _mask_holder_names(path):
    """DB 사본에서 계약자와 피보험자 이름만 지운다. 나머지는 원본과 같다."""
    con = db.connect(path)
    con.execute("UPDATE contract SET policyholder_name=NULL, insured_name=NULL")
    for rid, raw in con.execute("SELECT id, raw_json FROM raw_row").fetchall():
        row = json.loads(raw)
        if any(k in row for k in NAME_KEYS):
            con.execute("UPDATE raw_row SET raw_json=? WHERE id=?", (json.dumps({k: ("" if k in NAME_KEYS else v) for k, v in row.items()}, ensure_ascii=False), rid))
    con.commit()
    con.close()


def test_d05_no_holder_or_insured_name_on_any_screen(env):
    """D-05. 계약자와 피보험자 이름은 어느 역할의 어느 화면에도 나오지 않는다. 지금은 이름을 보여 주는 화면이 없다.

    알아보는 법: 원장(contract)과 raw_row 에서 이름 값을 읽는다. DB 를 둘로 복사해 하나에서는 그 이름 칸만 지운다.
    같은 계정으로 두 사본의 같은 화면을 열어 이름마다 나온 횟수를 센다. 횟수가 다르면 그 화면이 계약자나 피보험자 칸을 그린 것이다.
    가짜 이름이 설계사 이름이나 조직 이름, 화면 문구와 우연히 같아도 두 사본에서 똑같이 나오므로 잘못 걸리지 않는다.

    나중에 계약 상세 화면을 만들 때: 계약자와 피보험자 이름은 그 계약의 담당 설계사(contract.agent_id)와 그 설계사의
    직속 관리자에게만 보여야 한다. 직속 관리자는 계약 귀속 월에 담당 설계사가 속한 조직의 조직장과 그 기간의 대리다.
    위 조직의 관리자, 다른 설계사, 목록 화면에서는 가린다. 메인 관리자를 어떻게 할지는 결정에 없으므로 그때 여쭙는다.
    그 화면이 생기면 이 시험의 기대를 그대로 두지 말고, 담당 설계사와 직속 관리자는 상세에서 보고
    나머지 계정과 모든 목록 화면은 보지 못하는 시험으로 나눈다.
    """
    con = db.connect(env["db"])
    names = _holder_names(con)
    people = _people(con)
    con.close()
    assert len(names) >= 20, "원장과 raw_row 에 계약자 이름이 있어야 시험이 뜻이 있다"
    plain, masked = _copy(env, "d05_plain"), _copy(env, "d05_masked")
    _mask_holder_names(masked)
    leaks = []
    for label, login_id in people:
        seen = {}
        for path in (plain, masked):
            con = db.connect(path)
            ctx = _ctx(con, login_id)
            seen[path] = _open_all(login(env, login_id, path), con, ctx, env["tmp"])
            con.close()
        assert seen[plain].keys() == seen[masked].keys()
        for screen, html in seen[plain].items():
            other = seen[masked][screen]
            if any(html.count(n) != other.count(n) for n in names):
                leaks.append(f"{label} {screen}: 계약자 또는 피보험자")
    assert not leaks, leaks


def test_d05_fake_real_db_keeps_both_names_for_later(env):
    """D-05 의 '저장한다' 쪽. 가짜 실제 양식은 원장의 두 칸과 raw_row 에 이름을 남긴다. 그래야 위 시험이 실제로 무언가를 가린다."""
    con = db.connect(env["db"])
    try:
        n_holder, n_insured = con.execute("SELECT COUNT(policyholder_name), COUNT(insured_name) FROM contract").fetchone()
        assert n_holder > 0
        if env["kind"] == "fake_real":
            assert n_insured > 0
            raw = [json.loads(r[0]) for r in con.execute("""SELECT r.raw_json FROM raw_row r JOIN import_batch b ON b.batch_id=r.batch_id
                                                           WHERE b.data_type='org_contract'""")]
            assert raw and all(k in raw[0] for k in NAME_KEYS)
    finally:
        con.close()


def test_d05_web_code_does_not_read_holder_names():
    """D-05 의 걸림 줄. 지금 웹 코드는 계약자와 피보험자 칸을 읽지 않는다.

    계약 상세 화면을 만들어 이 시험이 걸리면, 위 docstring 의 규칙대로 담당 설계사와 직속 관리자만 보게 하고
    그 범위를 확인하는 시험으로 이 시험을 바꾼다. 범위 검사는 app/web/deps.py 의 guard 와 ctx['agent_ids'] 로만 한다.
    """
    web = pathlib.Path(db.ROOT) / "app" / "web"
    pat = re.compile(r"policyholder_name|insured_name|raw_json|raw_row|계약자|피보험자")
    hits = [str(p.relative_to(web)) for p in sorted(web.rglob("*")) if p.suffix in (".py", ".html") and pat.search(p.read_text(encoding="utf-8"))]
    assert not hits, hits


# ================================================================ D-17 수수료와 시상 금액
def _ints(x):
    """금액 하나가 화면에 찍힐 수 있는 정수들. won 필터는 버림, 다른 곳은 반올림일 수 있다."""
    if x is None:
        return set()
    x = abs(float(x))
    return {v for v in (int(x), round(x)) if v >= MIN_AMOUNT}


def _tokens(html) -> set:
    return {int(t.replace(",", "")) for t in NUM.findall(html)}


def _cards(con, ym):
    ref = pm.ref_date(ym)
    return [pm._load(con, r[0]) for r in con.execute("SELECT promo_id FROM promotion ORDER BY promo_id")], ref


def _secrets(con, agent_ids, ym) -> dict:
    """{화면에 찍힐 수 있는 정수: {칸 이름}}. 이 사람들의 수수료, 시상, 환수, 예상 시상 금액과 사람별, 달별, 합계."""
    out = {}

    def add(field, *values):
        for v in values:
            for i in _ints(v):
                out.setdefault(i, set()).add(field)
    ids = sorted(agent_ids)
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    stored = {
        "수수료 commission_calc.amount": f"SELECT agent_id, period_ym, amount FROM commission_calc WHERE agent_id IN ({q})",
        "정산서 commission_statement_line.amount": f"SELECT agent_id, period_ym, amount FROM commission_statement_line WHERE agent_id IN ({q})",
        "시상 incentive_result.reward_amount": f"SELECT agent_id, period_ym, reward_amount FROM incentive_result WHERE agent_id IN ({q})",
        "시상 환수 incentive_clawback.amount": f"""SELECT r.agent_id, k.period_ym, k.amount FROM incentive_clawback k
                                                   JOIN incentive_result r ON r.result_id=k.result_id WHERE r.agent_id IN ({q})""",
    }
    for field, sql in stored.items():
        rows = con.execute(sql, ids).fetchall()
        by_agent_month, by_month = {}, {}
        for a, ym_, v in rows:
            add(field, v)
            by_agent_month[(a, ym_)] = by_agent_month.get((a, ym_), 0) + (v or 0)
            by_month[ym_] = by_month.get(ym_, 0) + (v or 0)
        add(field + " 사람별 달 합계", *by_agent_month.values())
        add(field + " 범위 달 합계", *by_month.values())
    for r in con.execute(f"SELECT paid, clawback, net FROM v_agent_monthly_commission WHERE agent_id IN ({q})", ids):
        add("수수료 v_agent_monthly_commission", *tuple(r))
    cards, ref = _cards(con, ym)
    for p in cards:
        res = pm.progress(con, p, ids, ref)
        add("예상 시상 금액", *(x["amount"] for x in res.values()))
        add("예상 시상 지급 시점별 금액", *(part["amount"] for x in res.values() for part in x.get("parts") or []))
        add("예상 시상 범위 합계", sum(x["amount"] or 0 for x in res.values()))
    return out


def _public(con, ym, ids=None) -> set:
    """이 계정의 화면에 나와도 되는 숫자. ids 는 그 계정이 보는 설계사이고 None 이면 전체다. 금액이 우연히 이 값과 같으면 그 금액은 보지 않는다.

    범위 안 사람의 실적(계약의 보험료와 인정보험료, 업적 집계 모든 칸과 범위 합계, 랭킹, 목표와 남은 양), 모든 조직의 조직 집계,
    프로모션 규칙(구간 기준과 보상, 건당 한도), 범위 안 사람의 프로모션 실적과 다음 구간까지 남은 양,
    모두에게 보이는 조직 부문 순위의 값과 수상권까지 남은 양, 연도와 연월 꼴 숫자다.
    """
    out = set()

    def add(*values):
        for v in values:
            out.update(_ints(v))
    ids = sorted(ids) if ids is not None else [r[0] for r in con.execute("SELECT agent_id FROM agent")]
    q = ",".join("?" * len(ids)) or "NULL"
    for r in con.execute(f"SELECT premium, premium_monthly, converted_premium FROM contract WHERE agent_id IN ({q})", ids):
        add(*tuple(r))
    cols = [r[1] for r in con.execute("PRAGMA table_info(agent_performance_summary)") if r[2] in ("NUMERIC", "INTEGER", "REAL")]
    perf = {}
    for r in con.execute(f"SELECT period_type, period_key, {', '.join(cols)} FROM agent_performance_summary WHERE agent_id IN ({q})", ids):
        add(*tuple(r)[2:])
        perf.setdefault((r["period_type"], r["period_key"]), []).append(dict(r))
    for rows in perf.values():
        add(*(sum(x[c] or 0 for x in rows) for c in cols))
    for (v,) in con.execute(f"SELECT value FROM ranking_snapshot WHERE agent_id IN ({q})", ids):
        add(v)
    for r in con.execute(f"SELECT agent_id, target_value FROM agent_goal WHERE agent_id IN ({q})", ids):
        add(r["target_value"])
        for s in con.execute("SELECT converted_premium FROM agent_performance_summary WHERE agent_id=? AND period_type='quarter'", (r["agent_id"],)):
            add(s[0], (r["target_value"] or 0) - (s[0] or 0))
    for r in con.execute("SELECT converted_premium, net_count FROM v_org_performance_rollup"):
        add(*tuple(r))
    for r in con.execute("SELECT min_value, reward_value FROM promotion_tier"):
        add(*tuple(r))
    for (v,) in con.execute("SELECT per_contract_cap FROM promotion"):
        add(v)
    cards, ref = _cards(con, ym)
    for p in cards:
        for x in pm.progress(con, p, ids, ref).values():
            add(x["value"], *(w["value"] for w in x["windows"]))
            if x["next"]:
                add(x["next"]["gap"], x["next"]["min_value"])
    for d in con.execute("SELECT division_id, min_value FROM org_award_division"):
        add(d["min_value"])
        rows = oa.standing(con, oa.load(con, d["division_id"]), ym)
        add(*(r["value"] for r in rows))
        cut = [r["value"] for r in rows if r["winner"]]
        if cut:
            add(*(cut[-1] - r["value"] for r in rows))
    out |= set(range(1900, 2101))
    out |= {y * 100 + m for y in range(1900, 2101) for m in range(1, 13)}
    return out


def _checkable(con, ctx, others, ym):
    """이 계정이 보면 안 되는 금액에서, 본인 금액과 이 계정의 화면에 나와도 되는 숫자와 겹치는 값을 뺀다."""
    secret = _secrets(con, others, ym)
    own = _secrets(con, {ctx["agent_id"]} if ctx["agent_id"] else set(), ym)
    public = _public(con, ym, ctx["agent_ids"])
    return {v: f for v, f in secret.items() if v not in public and v not in own}


def _leaks(pages, checkable, label):
    out = []
    for screen, html in pages.items():
        hit = _tokens(html) & checkable.keys()
        if hit:
            out.append(f"{label} {screen}: " + ", ".join(sorted({f for v in hit for f in checkable[v]})))
    return out


@pytest.fixture(scope="module")
def d17(env):
    """D-17 시험이 함께 쓰는 사본, 사람, 화면에 나와도 되는 숫자."""
    path = _copy(env, "d17")
    con = db.connect(path)
    ym = auth.current_ym(con)
    people = [(label, login_id, _ctx(con, login_id)) for label, login_id in _people(con)]
    yield {"path": path, "con": con, "ym": ym, "people": people, "public": _public(con, ym)}            # 메인 관리자의 것. 대조군이 쓴다
    con.close()


def test_d17_manager_sees_no_member_commission_or_award_amount(env, d17):
    """D-17. 관리자는 팀원의 수수료, 시상, 환수, 예상 시상 금액을 어느 화면에서도 보지 않는다. 본인 것은 내 현황에서 본다.

    알아보는 법: 샘플 DB 에서 관리자 범위 안 팀원(본인 제외)의 금액을 미리 읽는다. 저장된 수수료와 시상, 환수는 줄마다,
    사람별 달 합계, 범위 달 합계로, 예상 시상은 프로모션 엔진이 계산한 사람별 금액, 지급 시점별 금액, 범위 합계로 읽는다.
    화면 HTML 에서 숫자 토큰을 뽑아 이 값과 같은 것이 있는지 본다. 우연히 겹치는 것을 피하려고
    1,000 보다 작은 값, 본인 금액과 같은 값, 그 관리자의 화면에 나와도 되는 숫자(_public: 범위 안 실적, 조직 집계,
    프로모션 규칙, 조직 부문 순위, 연도와 연월)와 같은 값은 보지 않는다. 샘플은 금액이 둥근 수라 실적과 겹치는 값이 많고,
    그래도 관리자마다 수십에서 백여 개의 금액이 남는다. 남은 값이 다섯 개보다 적으면 시험이 실패한다.
    """
    con, ym = d17["con"], d17["ym"]
    managers = [(label, login_id, ctx) for label, login_id, ctx in d17["people"] if ctx["role"] == "manager"]
    assert managers
    leaks, checked = [], 0
    for label, login_id, ctx in managers:
        others = ctx["agent_ids"] - {ctx["agent_id"]}
        checkable = _checkable(con, ctx, others, ym)
        checked += len(checkable)
        pages = _open_all(login(env, login_id, d17["path"]), con, ctx, env["tmp"])
        leaks += _leaks(pages, checkable, label)
    assert checked >= 5, "볼 금액이 거의 없으면 이 시험은 아무것도 지키지 못한다"
    assert not leaks, leaks


def test_d17_detector_finds_amounts_where_they_are_allowed(env, d17):
    """대조군. 같은 방법이 금액을 실제로 찾아내는지 본다. 그래야 위 시험의 통과가 뜻이 있다.

    메인 관리자의 달성 현황에는 예상 시상 칸이 있다. 설계사 본인의 달성 현황에는 자기 예상 시상이 보이는 것이 허용된다.
    """
    con, ym, public = d17["con"], d17["ym"], d17["public"]
    main = next(ctx for _, _, ctx in d17["people"] if ctx["role"] == "main")
    everyone = {r[0] for r in con.execute("SELECT agent_id FROM agent WHERE status='active'")}
    checkable = {v: f for v, f in _secrets(con, everyone, ym).items() if v not in public and any(x.startswith("예상 시상 금액") for x in f)}
    c = login(env, "admin", d17["path"])
    seen = set()
    for p in [r[0] for r in con.execute("SELECT promo_id FROM promotion ORDER BY promo_id")]:
        seen |= _tokens(c.get(f"/promo/progress?p={p}").text) & checkable.keys()
    assert main and seen, "메인 관리자의 달성 현황에서 예상 시상 금액을 찾지 못했다"
    # 본인 금액이 화면에 나와도 되는 숫자와 겹치지 않는 설계사를 찾아 그 사람으로 들어간다
    heads = {r[0] for r in con.execute("SELECT agent_id FROM org_manager")}
    tried = 0
    for aid, login_id in con.execute("""SELECT a.agent_id, u.login_id FROM agent a JOIN user_account u ON u.agent_id=a.agent_id
                                        WHERE a.status='active' ORDER BY a.internal_code""").fetchall():
        own = {v for v, f in _secrets(con, {aid}, ym).items() if v not in public and "예상 시상 금액" in f}
        if aid in heads or not own:
            continue
        tried += 1
        html = login(env, login_id, d17["path"]).get("/promo/progress").text
        assert _tokens(html) & own, "설계사 본인의 달성 현황에서 자기 예상 시상 금액을 찾지 못했다"
        if tried >= 3:
            break
    assert tried, "본인 예상 시상 금액을 알아볼 수 있는 설계사가 없다"


def test_d17_agent_sees_no_other_agents_amounts(env, d17):
    """설계사는 자기 금액만 본다. 다른 설계사의 수수료, 시상, 예상 시상 금액은 어느 화면에도 없다."""
    con, ym = d17["con"], d17["ym"]
    everyone = {r[0] for r in con.execute("SELECT agent_id FROM agent")}
    leaks = []
    for label, login_id, ctx in d17["people"]:
        if ctx["role"] != "agent":
            continue
        checkable = _checkable(con, ctx, everyone - {ctx["agent_id"]}, ym)
        assert checkable
        leaks += _leaks(_open_all(login(env, login_id, d17["path"]), con, ctx, env["tmp"]), checkable, label)
    assert not leaks, leaks


def test_d17_manager_team_progress_has_no_amount_column(env, d17):
    """칸 이름으로도 본다. 관리자의 팀원 달성 현황에는 예상 시상 칸이 없고, 메인 관리자에게는 있다."""
    con = d17["con"]
    promos = [r[0] for r in con.execute("""SELECT promo_id FROM promotion WHERE status='published' AND recipient_level='fc' ORDER BY promo_id""")]
    assert promos
    for label, login_id, ctx in d17["people"]:
        if ctx["role"] not in ("main", "manager"):
            continue
        c = login(env, login_id, d17["path"])
        for p in promos:
            html = c.get(f"/promo/progress?p={p}").text
            assert ("<th class=\"num\">예상 시상</th>" in html) == (ctx["role"] == "main"), (label, f"/promo/progress?p={p}")
