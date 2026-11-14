"""컨텍스트 신선도 점검. docs/13_AI운영설계.md 1.3 의 검사를 한다.

    python scripts/check_context.py          # 점검. 오류가 있으면 1 로 끝난다
    python scripts/check_context.py --fix    # 메뉴에서 뽑는 표를 다시 만든 뒤 점검

검사 함수는 글을 받아 문제 목록을 돌려준다. 시험에서 글만 바꿔 넣어 부를 수 있다.
"""
import argparse
import datetime as dt
import pathlib
import re
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
MENU = ROOT / "config" / "menu.yaml"
CLAUDE_MD = ROOT / "CLAUDE.md"
PLAN = ROOT / "docs" / "10_개발계획.md"
STRUCT = ROOT / "docs" / "11_개발구조.md"
AIOPS = ROOT / "docs" / "13_AI운영설계.md"
AGENTS = ROOT / ".claude" / "agents"
WEB = ROOT / "app" / "web"

ROLE_KO = {"main": "메인 관리자", "manager": "관리자", "agent": "설계사"}
SCOPE_KO = {"all": "전체", "org": "자기 조직", "self": "본인"}
STATUS_KO = {"built": "새 화면", "legacy": "기존 도구", "planned": "기획"}
GROUP_HEAD = "| 묶음 | 화면 수 | 보는 역할 | 화면 |\n| --- | --- | --- | --- |\n"
SCREEN_HEAD = "| 묶음 | 화면 | 주소 | 메인 관리자 | 관리자 | 설계사 | 단계 | 상태 |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n"
NON_MENU_ROUTES = {"/login", "/logout", "/favicon.ico", "/{rest:path}"}
CLAUDE_MAX_LINES = 150
STALE_DAYS = 14
PHONE = re.compile(r"(?<![0-9])01[016789]-?[0-9]{3,4}-?[0-9]{4}(?![0-9])")
RRN = re.compile(r"(?<![0-9])[0-9]{6}-[1-4][0-9]{6}(?![0-9])")
TEXT_SUFFIX = {".md", ".py", ".yaml", ".yml", ".html", ".css", ".sql", ".csv", ".txt", ".toml", ".json", ".js"}


# ---------------------------------------------------------------- 표 만들기
def group_table(menu: dict) -> str:
    rows = []
    for g in menu["groups"]:
        roles = [ROLE_KO[r] for r in ("main", "manager", "agent") if any(r in it["roles"] for it in g["items"])]
        rows.append(f"| {g['name']} | {len(g['items'])} | {', '.join(roles)} | {', '.join(it['name'] for it in g['items'])} |")
    return GROUP_HEAD + "\n".join(rows)


def screen_table(menu: dict) -> str:
    rows = []
    for g in menu["groups"]:
        for it in g["items"]:
            r = it["roles"]
            rows.append(f"| {g['name']} | {it['name']} | `{it['path']}` | {SCOPE_KO.get(r.get('main'), '없음')} | {SCOPE_KO.get(r.get('manager'), '없음')} "
                        f"| {SCOPE_KO.get(r.get('agent'), '없음')} | {it['phase']} | {STATUS_KO[it['status']]} |")
    return SCREEN_HEAD + "\n".join(rows)


def _table_after(text: str, head: str) -> tuple:
    """머리 줄부터 표가 끝나는 곳까지의 위치. 없으면 None."""
    a = text.find(head)
    if a < 0:
        return None
    b = a + len(head)
    lines = text[b:].split("\n")
    n = 0
    for ln in lines:
        if not ln.startswith("|"):
            break
        n += len(ln) + 1
    return a, b + max(n - 1, 0)


def replace_table(text: str, head: str, table: str) -> str:
    pos = _table_after(text, head)
    if pos is None:
        return text
    return text[:pos[0]] + table + text[pos[1]:]


# ---------------------------------------------------------------- 검사
def check_routes(menu: dict, route_sources: list) -> list:
    """built 화면은 주소가 있어야 하고, 메뉴 밖의 화면 주소는 없어야 한다."""
    routes = set()
    for src in route_sources:
        routes |= set(re.findall(r'@(?:app|router)\.get\(\s*"([^"]+)"', src))
    items = {it["path"]: it for g in menu["groups"] for it in g["items"]}
    out = []
    for path, it in items.items():
        if it["status"] == "built" and path not in routes:
            out.append(("error", f"화면과 메뉴: {path} 는 menu.yaml 에서 새 화면인데 주소가 없습니다"))
    for path in sorted(routes - NON_MENU_ROUTES):
        if path not in items:
            out.append(("error", f"화면과 메뉴: {path} 는 주소가 있는데 menu.yaml 에 없습니다"))
        elif items[path]["status"] != "built":
            out.append(("error", f"화면과 메뉴: {path} 는 주소가 있는데 menu.yaml 상태가 {items[path]['status']} 입니다"))
    return out


def check_tables(menu: dict, docs: dict) -> list:
    """메뉴에서 뽑는 표가 문서와 같은지. docs = {이름: 글}."""
    out = []
    want = {GROUP_HEAD: group_table(menu), SCREEN_HEAD: screen_table(menu)}
    for name, text in docs.items():
        for head, table in want.items():
            pos = _table_after(text, head)
            if pos is not None and text[pos[0]:pos[1]] != table:
                out.append(("error", f"화면 표: {name} 의 표가 menu.yaml 과 다릅니다. --fix 로 다시 만드세요"))
    return out


def _confirmed_section(plan_text: str) -> str:
    return plan_text.split("### 7.1", 1)[1].split("### 7.2", 1)[0] if "### 7.1" in plan_text else ""


def decision_ids(plan_text: str) -> tuple:
    """docs/10 7.2 의 열린 결정 ID 와 겹친 ID."""
    sec = plan_text.split("### 7.2", 1)[1] if "### 7.2" in plan_text else ""
    ids = re.findall(r"^\| (D-\d{2}) \|", sec, flags=re.M)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    return set(ids), dup


def confirmed_ids(plan_text: str) -> tuple:
    """docs/10 7.1 에서 확정된 결정 ID 와 겹친 ID. 결정 칸의 첫머리에 적은 ID 만 센다."""
    ids = re.findall(r"^\| \d{1,2}월 \d{1,2}일 \| (D-\d{2})(?![0-9])", _confirmed_section(plan_text), flags=re.M)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    return set(ids), dup


def check_decisions(plan_text: str, others: dict) -> list:
    """다른 문서가 가리키는 ID 는 7.2 의 열린 결정이나 7.1 의 확정된 결정에 있어야 한다."""
    out = []
    ids, dup = decision_ids(plan_text)
    done, done_dup = confirmed_ids(plan_text)
    if not ids:
        out.append(("error", "결정 ID: docs/10 7.2 에 열린 결정 표가 없습니다"))
    for d in dup:
        out.append(("error", f"결정 ID: {d} 가 docs/10 7.2 에 두 번 있습니다"))
    for d in done_dup:
        out.append(("error", f"결정 ID: {d} 가 docs/10 7.1 에 두 번 있습니다"))
    for d in sorted(ids & done):
        out.append(("error", f"결정 ID: {d} 가 7.1 확정과 7.2 열린 결정에 함께 있습니다. 7.2 의 행을 지우세요"))
    for ln in _confirmed_section(plan_text).splitlines():
        if ln.startswith("| ") and not ln.startswith(("| 날짜", "| ---")) and not re.match(r"^\| \d{1,2}월 \d{1,2}일 \|", ln):
            out.append(("error", f"결정 ID: 확정된 결정에 날짜가 없습니다: {ln[:40]}"))
    for name, text in others.items():
        for ref in sorted(set(re.findall(r"(?<![A-Za-z0-9])D-\d{2}(?![0-9])", text)) - ids - done):
            out.append(("error", f"결정 ID: {name} 가 가리키는 {ref} 가 docs/10 7.1 과 7.2 에 없습니다"))
    return out


def check_claude_md(text: str, last_commit: dt.date | None) -> list:
    out = []
    n = text.count("\n") + 1
    if n > CLAUDE_MAX_LINES:
        out.append(("error", f"안내 글 길이: CLAUDE.md 가 {n}줄로 {CLAUDE_MAX_LINES}줄을 넘습니다"))
    sec = text.split("## 지금 어디까지 왔나", 1)
    m = re.search(r"갱신:\s*(\d{4}-\d{2}-\d{2})", sec[1]) if len(sec) > 1 else None
    if not m:
        out.append(("error", "상태 날짜: CLAUDE.md 의 지금 어디까지 왔나에 '갱신: YYYY-MM-DD' 가 없습니다"))
    elif last_commit and (last_commit - dt.date.fromisoformat(m.group(1))).days > STALE_DAYS:
        out.append(("warn", f"상태 날짜: 지금 어디까지 왔나가 마지막 커밋보다 {STALE_DAYS}일 넘게 오래됐습니다"))
    return out


def check_pii(files: dict) -> list:
    """files = {경로: 글}. 휴대폰과 주민번호 꼴을 찾는다. 찾은 값은 출력하지 않는다."""
    out = []
    for path, text in files.items():
        for kind, rx in (("휴대폰 번호", PHONE), ("주민번호", RRN)):
            hits = rx.findall(text)
            if hits:
                out.append(("error", f"개인정보 꼴: {path} 에 {kind} 꼴이 {len(hits)}곳 있습니다"))
    return out


def _frontmatter(text: str) -> dict | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def check_agents(agent_files: dict, aiops_text: str) -> list:
    """agent_files = {파일 이름 줄기: 글}. docs/13 4.2 표와 이름이 같아야 한다."""
    out = []
    for stem, text in agent_files.items():
        fm = _frontmatter(text)
        if fm is None:
            out.append(("error", f"에이전트 정의: {stem} 의 머리말을 읽을 수 없습니다"))
            continue
        for key in ("name", "description", "tools"):
            if not fm.get(key):
                out.append(("error", f"에이전트 정의: {stem} 에 {key} 가 없습니다"))
        if fm.get("name") and fm["name"] != stem:
            out.append(("error", f"에이전트 정의: {stem} 의 name 이 파일 이름과 다릅니다"))
    sec = aiops_text.split("### 4.2", 1)[1].split("###", 1)[0] if "### 4.2" in aiops_text else ""
    listed = set(re.findall(r"^\| ([a-z][a-z0-9-]+) \|", sec, flags=re.M))
    for stem in sorted(set(agent_files) - listed):
        out.append(("error", f"에이전트 정의: {stem} 가 docs/13 4.2 표에 없습니다"))
    for name in sorted(listed - set(agent_files)):
        out.append(("error", f"에이전트 정의: docs/13 4.2 의 {name} 에 정의 파일이 없습니다"))
    return out


# ---------------------------------------------------------------- 저장소에서 모으기
def _git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def repo_text_files() -> dict:
    names = set(_git("ls-files").splitlines()) | set(_git("ls-files", "--others", "--exclude-standard").splitlines())
    out = {}
    for n in sorted(names):
        p = ROOT / n
        if p.suffix.lower() in TEXT_SUFFIX and p.is_file() and not n.startswith("data/"):
            out[n] = p.read_text(encoding="utf-8", errors="ignore")
    return out


def last_commit_date() -> dt.date | None:
    s = _git("log", "-1", "--format=%cs").strip()
    return dt.date.fromisoformat(s) if s else None


def load_menu() -> dict:
    return yaml.safe_load(MENU.read_text(encoding="utf-8"))


def fix() -> list:
    """메뉴에서 뽑는 표를 다시 만든다. 바꾼 파일 목록을 돌려준다."""
    menu, changed = load_menu(), []
    for path in (PLAN, STRUCT):
        text = path.read_text(encoding="utf-8")
        new = replace_table(replace_table(text, GROUP_HEAD, group_table(menu)), SCREEN_HEAD, screen_table(menu))
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path.relative_to(ROOT).as_posix())
    return changed


def check_all() -> list:
    menu = load_menu()
    plan, struct, aiops = (p.read_text(encoding="utf-8") for p in (PLAN, STRUCT, AIOPS))
    claude = CLAUDE_MD.read_text(encoding="utf-8")
    files = repo_text_files()
    routes = [p.read_text(encoding="utf-8") for p in WEB.rglob("*.py")]
    agents = {p.stem: p.read_text(encoding="utf-8") for p in sorted(AGENTS.glob("*.md"))}
    refs = {n: t for n, t in files.items() if n.endswith(".md") and n != "docs/10_개발계획.md"}
    return (check_routes(menu, routes)
            + check_tables(menu, {"docs/10_개발계획.md": plan, "docs/11_개발구조.md": struct})
            + check_decisions(plan, refs)
            + check_claude_md(claude, last_commit_date())
            + check_pii(files)
            + check_agents(agents, aiops))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fix", action="store_true", help="메뉴에서 뽑는 표를 다시 만든 뒤 점검한다")
    args = ap.parse_args(argv)
    if args.fix:
        for c in fix():
            print(f"다시 만듦: {c}")
    problems = check_all()
    for level, msg in problems:
        print(f"[{'오류' if level == 'error' else '경고'}] {msg}")
    errors = sum(1 for lv, _ in problems if lv == "error")
    print(f"컨텍스트 점검: 오류 {errors}, 경고 {len(problems) - errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
