"""가짜 수료자 명부 생성기. 시험과 샘플용이다. 실제 명부 양식은 아직 없어(D-09) 머리글은 샘플 양식의 가정이다.

사번과 이름은 DB 의 설계사(masters.seed 의 가짜 설계사)에서 가져온다. 연락처, 주민번호, 생년월일 칸에는
어떤 개인정보 꼴에도 맞지 않는 가짜 값을 넣어 적재기가 버리는지 시험한다.
만든 파일은 저장소 밖(시험의 임시 폴더나 data/ 아래)에 둔다.

    python -m app.loader.fake_edu_roster data/fake      # data/demo.db 의 설계사와 과정으로 가짜 명부를 만든다

dirty=True 이면 일부러 넣는 줄: 마스터에 없는 사번, 사번 빈 줄, 과정 목록에 없는 과정, 읽을 수 없는 수료일,
사번과 이름이 다른 줄, 파일 안에서 겹친 줄. DIRTY 의 숫자는 몇 번째 줄인가이고, 머리글이 1행이라 파일의 행 번호는 하나 더 크다.
"""
import csv
import pathlib
import random
import sys

from app.loader.fake_eternal import fake_name, write_xlsx

HEADERS = ["No", "사번", "성명", "소속", "과정코드", "과정명", "차수", "교육일", "수료일", "연락처", "주민번호", "생년월일", "비고"]
# 같은 양식의 다른 머리글 이름. 매핑의 aliases 를 시험한다
ALT_HEADERS = {"사번": "사원번호", "성명": "이름", "과정코드": "교육코드", "과정명": "교육명", "차수": "회차", "수료일": "이수일자"}
SENSITIVE = ["연락처", "주민번호", "생년월일"]

UNKNOWN_AGENT_CODE = "ZZ999999"
UNKNOWN_COURSE = "NO-SUCH"
FAKE_PHONE_PREFIX = "x-fake-tel-"
FAKE_RRN_PREFIX = "x-fake-id-"
# 이름 불일치 줄과 없는 사번 줄에 넣는 이름. 마스터에 없으므로 DB 에 남으면 안 된다
MISMATCH_NAME = "가짜불일치"
UNKNOWN_NAME = "가짜없는사람"
DIRTY = {"unknown_agent": 1, "blank_agent": 2, "unknown_course": 3, "bad_date": 4, "name_mismatch": 5, "dup_in_file": 6}


def agents_from_db(con, job_type="fc") -> list:
    return [dict(r) for r in con.execute("SELECT agent_id, internal_code code, name FROM agent WHERE job_type=? ORDER BY internal_code",
                                         (job_type,))]


def sessions_from_db(con, course_code) -> list:
    return [dict(r) for r in con.execute(
        """SELECT s.session_no, s.date_from, s.date_to FROM edu_session s JOIN edu_course c ON c.course_id=s.course_id
           WHERE c.code=? AND s.status!='cancelled' ORDER BY s.session_no""", (course_code,))]


def fake_roster_rows(people, course, sessions=None, n=None, seed=21, dirty=False, start=0) -> list:
    """가짜 명부 줄. 머리글을 키로 하는 dict 목록이다. 값은 실제 파일처럼 글자로 둔다.

    people: [{code, name}], course: {code, name}, sessions: [{session_no, date_from, date_to}].
    차수가 있으면 차수를 돌려 쓰고 수료일은 차수 종료일이다. 없으면 수료일만 2026-10-01 부터 하루씩 늘린다.
    n 이 없으면 사람 수만큼, start 번째 사람부터 한 사람씩 쓴다.
    """
    rng = random.Random(seed)
    sessions = sessions or []
    people = people[start:]
    n = len(people) if n is None else n
    rows = []
    for i in range(n):
        p = people[i % len(people)]
        s = sessions[i % len(sessions)] if sessions else None
        done = s["date_to"] if s else f"2026-10-{1 + i % 28:02d}"
        rows.append({
            "No": i + 1, "사번": p["code"], "성명": p["name"], "소속": "가상지점", "과정코드": course["code"], "과정명": course["name"],
            "차수": f"{s['session_no']}차" if s else "", "교육일": s["date_from"] if s else "", "수료일": done.replace("-", "."),
            "연락처": f"{FAKE_PHONE_PREFIX}{rng.randint(1000, 9999)}", "주민번호": f"{FAKE_RRN_PREFIX}{rng.randint(10**5, 10**6 - 1)}",
            "생년월일": f"x-fake-birth-{rng.randint(1, 99)}", "비고": "",
        })
    if dirty:
        _spoil(rows)
    return rows


def _spoil(rows) -> None:
    """DIRTY 의 자리에 문제 줄을 만든다. 줄이 7줄보다 적으면 있는 만큼만 만든다."""
    def at(key):
        i = DIRTY[key] - 1
        return rows[i] if i < len(rows) else None

    if (r := at("unknown_agent")) is not None:
        r["사번"], r["성명"] = UNKNOWN_AGENT_CODE, UNKNOWN_NAME
    if (r := at("blank_agent")) is not None:
        r["사번"] = ""
    if (r := at("unknown_course")) is not None:
        r["과정코드"], r["과정명"] = UNKNOWN_COURSE, "없는 과정"
    if (r := at("bad_date")) is not None:
        r["수료일"], r["차수"], r["교육일"] = "2026.13.45", "", ""
    if (r := at("name_mismatch")) is not None:
        r["성명"] = MISMATCH_NAME
    if (r := at("dup_in_file")) is not None and len(rows) > DIRTY["dup_in_file"]:
        src = rows[DIRTY["dup_in_file"]]                # 다음 줄과 같은 사람, 같은 과정으로 만든다
        r.update({k: src[k] for k in ("사번", "성명", "과정코드", "과정명", "차수", "교육일", "수료일")})


def write_csv(path, headers, rows, encoding="utf-8-sig"):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow([r.get(h, "") for h in headers])
    return path


def make_roster_file(path, people, course, sessions=None, n=None, seed=21, dirty=False, alt_headers=False, start=0,
                     drop=(), encoding="utf-8-sig"):
    """가짜 명부를 xlsx 또는 csv 로 쓴다. 확장자가 .csv 면 CSV 다. drop 에 준 머리글은 빼고 쓴다.
    alt_headers 면 ALT_HEADERS 의 다른 이름으로 머리글을 쓴다. 돌려주는 것: (경로, 줄 목록)."""
    rows = fake_roster_rows(people, course, sessions, n=n, seed=seed, dirty=dirty, start=start)
    headers = [h for h in HEADERS if h not in drop]
    if alt_headers:
        rows = [{ALT_HEADERS.get(k, k): v for k, v in r.items()} for r in rows]
        headers = [ALT_HEADERS.get(h, h) for h in headers]
    if str(path).lower().endswith(".csv"):
        return write_csv(path, headers, rows, encoding=encoding), rows
    return write_xlsx(path, headers, rows, sheet="수료자"), rows


def fake_people(n=10, seed=5) -> list:
    """DB 없이 쓸 가짜 사람. 사번은 FK 로 시작한다."""
    rng = random.Random(seed)
    return [{"code": f"FK{1000 + i}", "name": fake_name(rng)} for i in range(n)]


if __name__ == "__main__":
    from app import db
    from app.engine import education as edu

    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "data/fake")
    con = db.connect(db.DEFAULT_DB)
    edu.ensure(con)
    c = con.execute("SELECT code, name FROM edu_course ORDER BY required DESC, code LIMIT 1").fetchone()
    if not c:
        sys.exit("과정이 없습니다. python run_demo.py 를 먼저 돌려 주세요")
    path, rows = make_roster_file(out / f"fake_edu_roster_{c['code']}.xlsx", agents_from_db(con), dict(c), sessions_from_db(con, c["code"]),
                                  dirty=True)
    print(f"가짜 수료자 명부 {len(rows)}줄: {path}")
