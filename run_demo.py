"""샘플 데이터로 전체 파이프라인을 돌린다.

    python run_demo.py            # data/demo.db 생성 후 202601~202610 계산
"""
import sys

from app import db
from app.engine import career, certification, commission, incentive, performance, reconcile
from app.seed import AS_OF, MONTHS, seed, seed_statements


def main(path=db.DEFAULT_DB):
    con = db.init_db(path)
    info = seed(con)
    print(f"[seed] 설계사 {len(info['agents'])}명, 계약 {len(info['contracts'])}건")
    for ym in MONTHS:
        _, n_c = commission.run(con, ym)
        _, n_p = performance.run(con, ym)
        _, n_i, n_cb = incentive.run(con, ym)
        print(f"[{ym}] 수수료 {n_c}라인, 업적 {n_p}행, 시상 {n_i}건(환수 {n_cb})")
    injected = seed_statements(con, AS_OF)
    print(f"[statement] {AS_OF} 보험사 정산서 적재, 의도적 차이 {len(injected)}건 주입")
    _, n_d = reconcile.run(con, AS_OF)
    print(f"[reconcile] 차이 {n_d}건")
    _, n_cert = certification.run(con, 2026, AS_OF)
    print(f"[certification] {n_cert}행")

    print("\n== 대사 차이 유형별 ==")
    print(db.df(con, "SELECT i.code 보험사, d.diff_type 유형, COUNT(*) 건수, SUM(d.diff_amount) 차이합 FROM reconciliation_diff d JOIN insurer i ON i.insurer_id=d.insurer_id JOIN calc_run r ON r.run_id=d.run_id AND r.status='done' GROUP BY 1,2").to_string(index=False))
    print("\n== 주입한 차이 ==")
    for x in injected:
        print("  ", x)
    print(f"\n== {AS_OF} 구간시상 상위 ==")
    print(db.df(con, """SELECT a.internal_code 사번, a.name 이름, r.name 규칙, ir.metric_value 지표값, ir.tier_no 구간, ir.reward_amount 시상액, ir.disqualify_reason 미자격사유
                        FROM incentive_result ir JOIN agent a ON a.agent_id=ir.agent_id JOIN incentive_rule r ON r.rule_id=ir.rule_id
                        JOIN calc_run cr ON cr.run_id=ir.run_id AND cr.status='done' WHERE ir.period_ym=? ORDER BY ir.reward_amount DESC LIMIT 8""", (AS_OF,)).to_string(index=False))
    print("\n== 시상 환수 ==")
    print(db.df(con, "SELECT period_ym 귀속월, COUNT(*) 건수, SUM(amount) 환수액 FROM incentive_clawback GROUP BY 1").to_string(index=False))
    print("\n== 승격 후보 ==")
    print(career.promotion_candidates(con, AS_OF).head(8).to_string(index=False))
    print("\n== 인증 진척 (MDRT 상위) ==")
    print(db.df(con, """SELECT a.internal_code 사번, a.name 이름, p.code 프로그램, s.progress_rate 진척률, s.achieved 달성, s.projected_achieved 연말예상
                        FROM agent_certification_status s JOIN agent a ON a.agent_id=s.agent_id JOIN certification_program p ON p.program_id=s.program_id
                        JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' ORDER BY p.code, s.progress_rate DESC LIMIT 8""").to_string(index=False))
    con.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else db.DEFAULT_DB)
