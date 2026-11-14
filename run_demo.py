"""샘플 원본 파일(엑셀·PDF)을 만들고, 적재기로 읽어 전체 파이프라인을 돌린다.

    python run_demo.py      # data/raw/ 에 샘플 파일 생성 → data/demo.db 적재·계산 → 요약 출력
"""
import shutil
import sys

from app import db, masters, samples
from app.engine import career, certification, commission, incentive, payments, performance, ranking, reconcile
from app.loader import excel_loader, incentive_plan, pdf_incentive
from app.samples import AS_OF, MONTHS
from app.web import auth as web_auth

RAW = db.ROOT / "data" / "raw"


def main(path=db.DEFAULT_DB):
    con = db.init_db(path)
    info = masters.seed(con)
    hq_id = {r["name"]: r["org_id"] for r in con.execute("SELECT * FROM org_unit WHERE level='hq'")}
    print(f"[accounts] 샘플 계정 {web_auth.seed_accounts(con)}개 (admin 과 사번, 비밀번호는 DEMO_PASSWORD 또는 demo1234)")

    # 1) 샘플 원본 파일 생성
    shutil.rmtree(RAW, ignore_errors=True)
    truth = samples.build_truth(con, info)
    org_files = samples.write_org_contract_files(truth, info, RAW)
    per_files = samples.write_persistency_files(con, info, RAW)
    pdfs = samples.write_incentive_pdfs(RAW)
    print(f"[samples] 본부 계약현황 {len(org_files)}개, 유지율 {len(per_files)}개, 시상 PDF {len(pdfs)}개 → {RAW.relative_to(db.ROOT)}/")

    # 2) 시상 공문 PDF 추출 → 초안. 본사 제도·샘플생명 10월 시책은 승인, 11월 한빛손해 공문은 검토 대기로 둔다
    for p in pdfs:
        doc_id, parsed = pdf_incentive.save_draft(con, p)
        n_t = sum(len(s["tiers"]) for s in parsed["sections"])
        state = "검토 대기"
        if "202611" not in p.name:
            pdf_incentive.approve(con, doc_id, "정산담당(데모)")
            state = "승인"
        print(f"[pdf] {p.name}: 규칙 {len(parsed['sections'])}개, 구간 {n_t}개, 적용 {parsed['effective_from']}~{parsed['effective_to']} → {state}")

    # 2-1) 보험사별 시상안 엑셀 → 시상안 원장 (원문 보존 + 해석 초안)
    plan = incentive_plan.load(con, samples.write_incentive_plan(RAW), AS_OF)
    incentive_plan.print_summary(incentive_plan.summary(con, plan["plan_doc_id"]))

    # 3) 월별: 엑셀 적재 → 납입 반영 → 수수료 → 업적 → 시상 → 랭킹
    for ym in MONTHS:
        ins = upd = blk = wrn = 0
        for f in [f for f in org_files if f["ym"] == ym]:
            s = excel_loader.load_org_contract(con, f["path"], ym, hq_id[f["hq"]], by=f"{f['hq']} 담당")
            ins, upd, blk, wrn = ins + s["inserted"], upd + s["updated"], blk + s["block"], wrn + s["warn"]
        excel_loader.load_persistency(con, next(f["path"] for f in per_files if f["ym"] == ym), ym)
        payments.accrue(con, ym)
        _, n_c = commission.run(con, ym)
        performance.run(con, ym)
        _, n_i, n_cb = incentive.run(con, ym)
        ranking.run(con, ym)
        print(f"[{ym}] 계약 신규 {ins} / 상태변경 {upd} (차단 {blk}, 경고 {wrn}) · 수수료 {n_c}라인 · 시상 {n_i}건 (환수 {n_cb})")

    print(f"[promotion] 샘플 프로모션 {samples.seed_promotions(con)}건 게시 (시상안의 정률 시책과 주차·연계·여행·가동 예시)")

    print(f"[org award] 샘플 조직 부문 {samples.seed_org_awards(con)}개 등록 (본부별 인정업적·손해보험 건수 부문, 지점 부문 예시)")

    # 4) 원수사 정산서 엑셀 생성 → 적재 → 대사
    stmt_files, injected = samples.write_statement_files(con, AS_OF, RAW)
    for f in stmt_files:
        s = excel_loader.load_statement(con, f["path"], f["icode"], AS_OF)
        print(f"[statement] {f['path'].name}: {s['rows']}행 (차단 {s['block']}, 경고 {s['warn']})")
    _, n_d = reconcile.run(con, AS_OF)
    _, n_cert = certification.run(con, 2026, AS_OF)

    show = lambda title, sql, params=(): print(f"\n== {title} ==\n" + db.df(con, sql, params).to_string(index=False))
    show("품질검증 (10월 배치)", """SELECT b.file_name 파일, q.severity 등급, q.check_code 검사, COUNT(*) 건수 FROM quality_issue q
         JOIN import_batch b ON b.batch_id=q.batch_id WHERE b.period_ym=? GROUP BY 1,2,3 ORDER BY 1,2""", (AS_OF,))
    show(f"{AS_OF} 구분별 추출 집계", """SELECT 구분, CASE 시상대상구분 WHEN 1 THEN '대상' ELSE '-' END 시상, SUM(건수) 건수, SUM(보험료) 보험료, SUM(인정보험료) 인정보험료
         FROM v_contract_extract WHERE 귀속월=? AND 상태 NOT IN ('applied','declined','withdrawn') GROUP BY 1,2 ORDER BY 5 DESC""", (AS_OF,))
    show(f"대사 차이 {n_d}건", """SELECT i.code 원수사, d.diff_type 유형, COUNT(*) 건수, SUM(d.diff_amount) 차이합 FROM reconciliation_diff d
         JOIN insurer i ON i.insurer_id=d.insurer_id JOIN calc_run r ON r.run_id=d.run_id AND r.status='done' GROUP BY 1,2""")
    print("\n== 정산서에 일부러 넣은 차이 ==")
    for x in injected:
        print("  ", x)
    show(f"{AS_OF} 시상 (규칙별)", """SELECT r.name 규칙, r.rule_kind 종류, COUNT(*) 대상, SUM(ir.qualified) 자격, SUM(ir.reward_amount) 시상액
         FROM incentive_result ir JOIN incentive_rule r ON r.rule_id=ir.rule_id JOIN calc_run cr ON cr.run_id=ir.run_id AND cr.status='done'
         WHERE ir.period_ym=? GROUP BY 1,2""", (AS_OF,))
    show("전월(202609) 랭킹 상위 5 (공유 대상)", """SELECT s.rank_no 순위, a.internal_code 사번, a.name 성명, o.name 지점, s.value 인정보험료 FROM ranking_snapshot s
         JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' JOIN agent a ON a.agent_id=s.agent_id LEFT JOIN org_unit o ON o.org_id=s.org_id
         WHERE s.period_type='month' AND s.period_key='202609' AND s.metric='recognized_premium' AND s.scope='all' ORDER BY s.rank_no LIMIT 5""")
    show(f"인증·임원·어워드 {n_cert}행", """SELECT p.name 프로그램, p.category 분류, COUNT(*) 대상, SUM(s.achieved) 달성, SUM(s.projected_achieved) 연말예상, ROUND(AVG(s.progress_rate),2) 평균진척
         FROM agent_certification_status s JOIN certification_program p ON p.program_id=s.program_id JOIN calc_run r ON r.run_id=s.run_id AND r.status='done'
         GROUP BY p.program_id ORDER BY p.sort_order""")
    print("\n== 승격 후보 ==")
    print(career.promotion_candidates(con, AS_OF).head(6).to_string(index=False))
    # 실제 원수사 32곳은 샘플 파일을 다 만든 뒤에 넣는다. 샘플 유지율과 정산서가 원수사마다 파일을 만들기 때문이다
    print(f"\n[insurers] 실제 원수사 {masters.seed_real_insurers(con)}곳을 마스터에 넣었습니다 (샘플 원수사 {len(masters.INSURERS)}곳은 그대로)")
    con.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else db.DEFAULT_DB)
