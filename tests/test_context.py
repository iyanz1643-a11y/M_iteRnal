"""컨텍스트 신선도 점검. 저장소가 통과하는지, 그리고 어긋난 경우를 실제로 잡는지 본다."""
import datetime as dt
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check_context", ROOT / "scripts" / "check_context.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

MENU = {"groups": [
    {"id": "home", "name": "홈", "items": [{"id": "home", "name": "홈", "path": "/", "roles": {"main": "all", "agent": "self"}, "phase": 2, "status": "built"}]},
    {"id": "promo", "name": "프로모션", "items": [
        {"id": "a", "name": "목록", "path": "/promo/list", "roles": {"main": "all"}, "phase": 2, "status": "built"},
        {"id": "b", "name": "비교", "path": "/promo/compare", "roles": {"main": "all"}, "phase": 2, "status": "planned"}]},
]}
ROUTES = ['@app.get("/", response_class=X)\n@app.get("/promo/list")\n@app.get("/login")\n@app.get("/{rest:path}")']


def errors(problems):
    return [m for lv, m in problems if lv == "error"]


def test_repository_passes():
    assert errors(cc.check_all()) == []


def test_routes_and_menu_must_agree():
    assert errors(cc.check_routes(MENU, ROUTES)) == []
    missing = ['@app.get("/")']
    assert any("/promo/list" in m and "주소가 없습니다" in m for m in errors(cc.check_routes(MENU, missing)))
    extra = ROUTES + ['@router.get("/promo/compare")', '@app.get("/secret")']
    msgs = errors(cc.check_routes(MENU, extra))
    assert any("/promo/compare" in m and "planned" in m for m in msgs) and any("/secret" in m for m in msgs)


def test_tables_are_regenerated_from_the_menu():
    doc = "앞\n\n" + cc.group_table(MENU) + "\n\n뒤\n\n" + cc.screen_table(MENU) + "\n\n끝\n"
    assert errors(cc.check_tables(MENU, {"d": doc})) == []
    stale = doc.replace("| 프로모션 | 2 |", "| 프로모션 | 3 |")
    assert errors(cc.check_tables(MENU, {"d": stale}))
    fixed = cc.replace_table(cc.replace_table(stale, cc.GROUP_HEAD, cc.group_table(MENU)), cc.SCREEN_HEAD, cc.screen_table(MENU))
    assert fixed == doc


def test_decision_ids():
    plan = "### 7.1 확정\n\n| 날짜 | 결정 |\n| --- | --- |\n| 11월 13일 | 정함 |\n\n### 7.2 열린\n\n| ID | 질문 |\n| --- | --- |\n| D-01 | 하나 |\n| D-02 | 둘 |\n"
    assert errors(cc.check_decisions(plan, {"x.md": "D-01 과 D-02 를 본다"})) == []
    assert any("D-09" in m for m in errors(cc.check_decisions(plan, {"x.md": "D-09 를 본다"})))
    assert any("두 번" in m for m in errors(cc.check_decisions(plan + "| D-02 | 또 |\n", {})))
    undated = plan.replace("| 11월 13일 | 정함 |", "| 언젠가 | 정함 |")
    assert any("날짜가 없습니다" in m for m in errors(cc.check_decisions(undated, {})))


def test_confirmed_decision_ids_stay_valid():
    plan = ("### 7.1 확정\n\n| 날짜 | 결정 |\n| --- | --- |\n| 11월 13일 | 정함 |\n| 11월 14일 | D-01 센터를 쓴다 |\n\n"
            "### 7.2 열린\n\n| ID | 질문 |\n| --- | --- |\n| D-02 | 둘 |\n")
    assert cc.confirmed_ids(plan) == ({"D-01"}, [])
    assert errors(cc.check_decisions(plan, {"x.md": "D-01 에서 정했고 D-02 는 남았다"})) == []
    mention = plan.replace("| 11월 13일 | 정함 |", "| 11월 13일 | 정함. D-03 은 그대로다 |")
    assert any("D-03" in m for m in errors(cc.check_decisions(mention, {"x.md": "D-03 을 본다"})))
    both = plan + "| D-01 | 또 |\n"
    assert any("D-01" in m and "함께" in m for m in errors(cc.check_decisions(both, {})))
    twice = plan.replace("| 11월 14일 | D-01 센터를 쓴다 |", "| 11월 14일 | D-01 센터를 쓴다 |\n| 11월 15일 | D-01 다시 |")
    assert any("7.1 에 두 번" in m for m in errors(cc.check_decisions(twice, {})))


def test_claude_md_length_and_date():
    good = "# 글\n\n## 지금 어디까지 왔나\n\n갱신: 2026-11-14\n"
    assert cc.check_claude_md(good, dt.date(2026, 11, 20)) == []
    assert [lv for lv, _ in cc.check_claude_md(good, dt.date(2026, 12, 20))] == ["warn"]
    assert errors(cc.check_claude_md("# 글\n\n## 지금 어디까지 왔나\n\n없음\n", None))
    assert errors(cc.check_claude_md(good + "줄\n" * 200, None))


def test_pii_patterns_are_caught_without_printing_values():
    phone = "0" + "10-" + "1234-" + "5678"
    rrn = "900101-" + "1234567"
    msgs = errors(cc.check_pii({"a.md": f"연락처 {phone}", "b.md": f"번호 {rrn}", "c.md": "사번 A0001, 금액 1,030,000원, 20260213"}))
    assert len(msgs) == 2 and all(phone not in m and rrn not in m for m in msgs)


def test_agent_definitions():
    aiops = "### 4.2 에이전트\n\n| 에이전트 | 이름 |\n| --- | --- |\n| data-loader | 적재 |\n| verifier | 검증 |\n\n### 4.3\n"
    ok = "---\nname: {n}\ndescription: 설명\ntools: Read, Bash\n---\n본문\n"
    files = {"data-loader": ok.format(n="data-loader"), "verifier": ok.format(n="verifier")}
    assert errors(cc.check_agents(files, aiops)) == []
    assert any("tools" in m for m in errors(cc.check_agents({**files, "verifier": "---\nname: verifier\ndescription: 설명\n---\n"}, aiops)))
    assert any("name" in m for m in errors(cc.check_agents({**files, "verifier": ok.format(n="other")}, aiops)))
    assert any("표에 없습니다" in m for m in errors(cc.check_agents({**files, "extra": ok.format(n="extra")}, aiops)))
    assert any("정의 파일이 없습니다" in m for m in errors(cc.check_agents({"data-loader": files["data-loader"]}, aiops)))


def test_real_agent_files_match_the_plan():
    agents = {p.stem for p in (ROOT / ".claude" / "agents").glob("*.md")}
    assert agents == {"data-loader", "calc-engine", "web-screen", "auth-org", "platform", "connector", "ai-features", "verifier", "docs-keeper"}
