"""Streamlit 화면.  실행:  streamlit run app/ui.py"""
import json
import pathlib
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402
from app.engine import career, certification, commission, incentive, performance, reconcile  # noqa: E402
from app.periods import period_key  # noqa: E402

st.set_page_config(page_title="GA 수수료·시상·업적관리", layout="wide")


@st.cache_resource
def get_con():
    return db.connect()


con = get_con()


def q(sql, params=()):
    return db.df(con, sql, params)


def months():
    return [r["period_ym"] for r in con.execute("SELECT DISTINCT period_ym FROM calc_run ORDER BY 1")]


def won(v):
    return f"{int(v):,}" if pd.notna(v) else "-"


st.sidebar.title("GA 정산관리")
page = st.sidebar.radio("메뉴", ["대시보드", "데이터 수집", "마스터", "업적", "수수료", "구간시상", "점검(대사)", "커리어패스", "인증 관리", "마감"])
ym = st.sidebar.selectbox("기준 월", months()[::-1] if months() else ["202610"])

# ---------------------------------------------------------------- 대시보드
if page == "대시보드":
    st.title(f"{ym} 마감 현황")
    c1, c2, c3, c4 = st.columns(4)
    stmt = q("SELECT COUNT(DISTINCT b.insurer_id) n FROM import_batch b WHERE b.data_type='statement' AND b.period_ym=? AND b.status='active'", (ym,)).iloc[0, 0]
    tot = q("SELECT COUNT(*) FROM insurer WHERE active=1").iloc[0, 0]
    c1.metric("정산서 수신 보험사", f"{stmt} / {tot}")
    comm = q("SELECT COALESCE(SUM(amount),0) v FROM commission_calc c JOIN calc_run r ON r.run_id=c.run_id AND r.status='done' WHERE c.period_ym=?", (ym,)).iloc[0, 0]
    c2.metric("내부 수수료 순액", won(comm))
    opn = q("SELECT COUNT(*) n, COALESCE(SUM(ABS(diff_amount)),0) a FROM reconciliation_diff d JOIN calc_run r ON r.run_id=d.run_id AND r.status='done' WHERE d.period_ym=? AND d.status='open'", (ym,)).iloc[0]
    c3.metric("미처리 대사 차이", f"{int(opn['n'])}건", f"{won(opn['a'])}원")
    inc = q("SELECT COALESCE(SUM(reward_amount),0) v FROM incentive_result i JOIN calc_run r ON r.run_id=i.run_id AND r.status='done' WHERE i.period_ym=?", (ym,)).iloc[0, 0]
    c4.metric("구간시상 총액", won(inc))
    st.subheader("단계별 진행")
    steps = q("SELECT run_type 단계, MAX(finished_at) 완료시각, note 비고 FROM calc_run WHERE period_ym=? AND status='done' GROUP BY run_type", (ym,))
    st.dataframe(steps, hide_index=True, use_container_width=True)
    st.subheader("조직별 월 실적")
    st.dataframe(q("SELECT org_name 조직, applied_count 청약, new_count 성립, withdrawn_count 철회, net_count 순건수, converted_premium 환산P, commission_amount 수수료 FROM v_org_performance WHERE period_type='month' AND period_key=? ORDER BY org_name", (ym,)), hide_index=True, use_container_width=True)
    st.subheader("대사 차이 유형별")
    st.dataframe(q("SELECT i.code 보험사, d.diff_type 유형, d.status 상태, COUNT(*) 건수, SUM(d.diff_amount) 차이합 FROM reconciliation_diff d JOIN insurer i ON i.insurer_id=d.insurer_id JOIN calc_run r ON r.run_id=d.run_id AND r.status='done' WHERE d.period_ym=? GROUP BY 1,2,3", (ym,)), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- 데이터 수집
elif page == "데이터 수집":
    st.title("데이터 수집")
    tab1, tab2, tab3 = st.tabs(["적재 배치 이력", "본부 계약 업로드", "시상 공문 PDF"])
    with tab1:
        st.dataframe(q("SELECT b.batch_id, i.code 보험사, o.name 업로드조직, b.data_type 유형, b.period_ym 대상월, b.version 버전, b.file_name 파일, substr(b.file_hash,1,12) 해시, b.received_via 경로, b.row_count 건수, b.status 상태 FROM import_batch b LEFT JOIN insurer i ON i.insurer_id=b.insurer_id LEFT JOIN org_unit o ON o.org_id=b.upload_org_id ORDER BY b.batch_id DESC"), hide_index=True, use_container_width=True)
    with tab2:
        st.caption("본부 담당자가 템플릿(templates/11_본부계약업로드.csv) 형식으로 올린다. 청약은 예정 업적, 철회는 제외·차감된다.")
        up = st.file_uploader("CSV 업로드 (미리보기만, 적재는 하지 않음)", type=["csv"])
        if up:
            d = pd.read_csv(up, encoding="utf-8-sig", dtype=str)
            st.dataframe(d, hide_index=True, use_container_width=True)
            st.info(f"{len(d)}행 · 상태별: {d['status'].value_counts().to_dict()}")
        st.subheader("현재 청약(예정 업적) / 철회 가능 건")
        st.dataframe(q("SELECT a.internal_code 사번, a.name 설계사, i.code 보험사, c.application_no 청약번호, c.application_date 청약일, c.status 상태, c.premium_monthly 월납P FROM contract c JOIN agent a ON a.agent_id=c.agent_id JOIN insurer i ON i.insurer_id=c.insurer_id WHERE c.status='applied' ORDER BY c.application_date DESC"), hide_index=True, use_container_width=True)
    with tab3:
        st.dataframe(q("SELECT d.doc_id, COALESCE(i.code,'내부') 출처, d.period_ym 대상월, d.title 제목, d.file_path 파일, d.extraction_method 추출방식, d.extraction_status 상태, d.reviewed_by 검토자 FROM incentive_source_doc d LEFT JOIN insurer i ON i.insurer_id=d.insurer_id ORDER BY d.period_ym DESC"), hide_index=True, use_container_width=True)
        st.caption("상태 extracted = 추출만 됨, 검토 화면에서 승인해야 규칙이 active 가 된다.")

# ---------------------------------------------------------------- 마스터
elif page == "마스터":
    st.title("마스터")
    t = st.tabs(["설계사", "상품", "수수료율 규칙", "환수 규칙", "시상 규칙"])
    with t[0]:
        st.dataframe(q("""SELECT a.internal_code 사번, a.name 이름, a.hire_date 위촉일, a.status 상태, o.name 현재소속, l.name 직급
                          FROM agent a LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_to IS NULL LEFT JOIN org_unit o ON o.org_id=h.org_id
                          LEFT JOIN agent_career_history ch ON ch.agent_id=a.agent_id AND ch.effective_to IS NULL LEFT JOIN career_level l ON l.level_id=ch.level_id ORDER BY a.internal_code"""), hide_index=True, use_container_width=True)
    with t[1]:
        st.dataframe(q("SELECT i.code 보험사, p.product_code 코드, p.product_name 상품명, p.product_category 상품군, p.conversion_rate 환산율, p.effective_from 적용시작 FROM product p JOIN insurer i ON i.insurer_id=p.insurer_id"), hide_index=True, use_container_width=True)
    with t[2]:
        st.dataframe(q("SELECT r.rule_id, i.code 보험사, p.product_code 상품, r.product_category 상품군, r.commission_type 유형, r.installment_from 회차from, r.installment_to 회차to, r.basis 기준, r.rate 요율, r.effective_from 적용시작, r.effective_to 적용종료, r.priority 우선순위, r.note 비고 FROM commission_rate_rule r JOIN insurer i ON i.insurer_id=r.insurer_id LEFT JOIN product p ON p.product_id=r.product_id ORDER BY i.code, r.priority, r.commission_type"), hide_index=True, use_container_width=True)
    with t[3]:
        st.dataframe(q("SELECT i.code 보험사, c.elapsed_from 경과from, c.elapsed_to 경과to, c.clawback_rate 환수율 FROM clawback_rule c JOIN insurer i ON i.insurer_id=c.insurer_id"), hide_index=True, use_container_width=True)
    with t[4]:
        rules = q("SELECT r.rule_id, r.name 규칙명, COALESCE(i.code,'내부') 출처, r.period_type 기간, r.metric 지표, r.scope 범위, r.scope_value 범위값, r.clawback_months 환수감시월, r.effective_from 적용시작, r.effective_to 적용종료, r.status 상태, r.version 버전 FROM incentive_rule r LEFT JOIN insurer i ON i.insurer_id=r.insurer_id")
        st.dataframe(rules, hide_index=True, use_container_width=True)
        rid = st.selectbox("규칙 상세", rules["rule_id"].tolist(), format_func=lambda x: rules.set_index("rule_id").loc[x, "규칙명"])
        c1, c2 = st.columns(2)
        c1.dataframe(q("SELECT tier_no 구간, min_value 이상, max_value 미만, reward_type 보상방식, reward_value 보상값 FROM incentive_tier WHERE rule_id=?", (rid,)), hide_index=True)
        c2.dataframe(q("SELECT condition_type 조건, operator 연산, value 값 FROM incentive_condition WHERE rule_id=?", (rid,)), hide_index=True)

# ---------------------------------------------------------------- 업적
elif page == "업적":
    st.title("업적")
    ptype = st.radio("기간 유형", ["month", "quarter", "half", "year"], horizontal=True, format_func=lambda x: {"month": "월", "quarter": "분기", "half": "반기", "year": "연"}[x])
    key = period_key(ptype, ym)
    st.caption(f"기간 키: {key} (분기·반기·연은 기준 월까지 누적)")
    d = q("""SELECT a.internal_code 사번, a.name 설계사, o.name 소속, s.applied_count 청약, s.new_count 성립, s.withdrawn_count 철회, s.net_count 순건수,
             s.monthly_premium 월납P, s.converted_premium 환산P, s.commission_amount 수수료, s.clawback_amount 환수
             FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
             JOIN agent a ON a.agent_id=s.agent_id LEFT JOIN org_unit o ON o.org_id=s.org_id
             WHERE s.period_type=? AND s.period_key=? ORDER BY s.converted_premium DESC""", (ym, ptype, key))
    st.dataframe(d, hide_index=True, use_container_width=True)
    st.subheader("계약 원장 검색")
    kw = st.text_input("증권번호 / 청약번호 / 설계사명")
    if kw:
        st.dataframe(q("""SELECT c.contract_id, i.code 보험사, c.policy_no 증권번호, c.application_no 청약번호, a.name 설계사, p.product_code 상품, c.contract_date 성립일, c.perf_ym 귀속월,
                          c.premium_monthly 월납P, c.converted_premium 환산P, c.status 상태, c.status_date 상태일, c.withdrawal_date 철회일
                          FROM contract c JOIN insurer i ON i.insurer_id=c.insurer_id JOIN agent a ON a.agent_id=c.agent_id LEFT JOIN product p ON p.product_id=c.product_id
                          WHERE c.policy_no LIKE ? OR c.application_no LIKE ? OR a.name LIKE ? LIMIT 200""", (f"%{kw}%",) * 3), hide_index=True, use_container_width=True)
    st.subheader("유지율 (13회차)")
    st.dataframe(q("SELECT a.internal_code 사번, a.name 설계사, p.rate 유지율, p.numerator 유지, p.denominator 대상 FROM persistency p JOIN agent a ON a.agent_id=p.agent_id WHERE p.period_ym=? AND p.kind='p13' ORDER BY p.rate DESC", (ym,)), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- 수수료
elif page == "수수료":
    st.title("수수료")
    if st.button("이 달 수수료 재계산"):
        rid, n = commission.run(con, ym, "ui")
        st.success(f"run {rid}: {n}라인")
    st.subheader("설계사별 명세")
    st.dataframe(q("SELECT internal_code 사번, name 설계사, paid 지급, clawback 환수, net 순액 FROM v_agent_monthly_commission WHERE period_ym=? ORDER BY net DESC", (ym,)), hide_index=True, use_container_width=True)
    code = st.selectbox("설계사 상세", q("SELECT internal_code FROM agent ORDER BY 1")["internal_code"])
    st.dataframe(q("""SELECT i.code 보험사, k.policy_no 증권번호, c.commission_type 유형, c.installment_no 회차, c.basis_amount 기준액, c.rate 요율, c.amount 금액, c.rule_id 규칙
                      FROM commission_calc c JOIN calc_run r ON r.run_id=c.run_id AND r.status='done' JOIN contract k ON k.contract_id=c.contract_id
                      JOIN insurer i ON i.insurer_id=k.insurer_id JOIN agent a ON a.agent_id=c.agent_id WHERE c.period_ym=? AND a.internal_code=? ORDER BY c.commission_type, k.policy_no""", (ym, code)), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- 구간시상
elif page == "구간시상":
    st.title("구간시상")
    if st.button("이 달 시상 재계산"):
        rid, n, cb = incentive.run(con, ym, "ui")
        st.success(f"run {rid}: {n}건, 환수 {cb}건")
    st.dataframe(q("""SELECT a.internal_code 사번, a.name 설계사, o.name 소속, r.name 규칙, ir.metric_value 지표값, ir.qualified 자격, ir.tier_no 구간, ir.reward_amount 시상액, ir.disqualify_reason 미자격사유
                      FROM incentive_result ir JOIN calc_run cr ON cr.run_id=ir.run_id AND cr.status='done' JOIN agent a ON a.agent_id=ir.agent_id
                      JOIN incentive_rule r ON r.rule_id=ir.rule_id LEFT JOIN org_unit o ON o.org_id=ir.org_id WHERE ir.period_ym=? ORDER BY ir.reward_amount DESC, ir.metric_value DESC""", (ym,)), hide_index=True, use_container_width=True)
    st.subheader("시상 환수")
    st.dataframe(q("""SELECT ic.period_ym 귀속월, a.name 설계사, r.name 규칙, ir.period_ym 시상월, k.policy_no 증권번호, ic.amount 환수액, ic.reason 사유
                      FROM incentive_clawback ic JOIN incentive_result ir ON ir.result_id=ic.result_id JOIN agent a ON a.agent_id=ir.agent_id
                      JOIN incentive_rule r ON r.rule_id=ir.rule_id JOIN contract k ON k.contract_id=ic.contract_id ORDER BY ic.period_ym DESC"""), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- 점검
elif page == "점검(대사)":
    st.title("점검(대사)")
    if st.button("대사 재실행"):
        rid, n = reconcile.run(con, ym, "ui")
        st.success(f"run {rid}: 차이 {n}건")
    st.dataframe(q("SELECT * FROM v_reconciliation_summary WHERE period_ym=?", (ym,)), hide_index=True, use_container_width=True)
    f = st.multiselect("상태", ["open", "confirmed", "adjusted", "ignored"], default=["open"])
    d = q(f"""SELECT d.diff_id, i.code 보험사, d.policy_no 증권번호, d.commission_type 유형, d.installment_no 회차, d.statement_amount 보험사금액, d.internal_amount 내부금액, d.diff_amount 차이, d.diff_type 차이유형, d.status 상태, d.assignee 담당, d.note 메모
              FROM reconciliation_diff d JOIN insurer i ON i.insurer_id=d.insurer_id JOIN calc_run r ON r.run_id=d.run_id AND r.status='done'
              WHERE d.period_ym=? AND d.status IN ({','.join('?' * len(f)) or "''"}) ORDER BY ABS(d.diff_amount) DESC""", (ym, *f))
    st.dataframe(d, hide_index=True, use_container_width=True)
    with st.form("처리"):
        st.subheader("차이 처리")
        did = st.selectbox("diff_id", d["diff_id"].tolist() if not d.empty else [])
        new = st.selectbox("상태", ["confirmed", "adjusted", "ignored", "open"])
        who = st.text_input("담당자")
        note = st.text_area("메모 (근거: 공문/메일 경로)")
        if st.form_submit_button("저장") and did:
            con.execute("UPDATE reconciliation_diff SET status=?, assignee=?, note=?, resolved_at=CASE WHEN ?='open' THEN NULL ELSE ? END WHERE diff_id=?", (new, who, note, new, db.now(), did))
            con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)", (db.now(), who or "ui", "diff_update", f"diff:{did}", json.dumps({"status": new, "note": note}, ensure_ascii=False)))
            con.commit()
            st.success("저장됨")
            st.rerun()

# ---------------------------------------------------------------- 커리어패스
elif page == "커리어패스":
    st.title("커리어패스")
    st.subheader("승격 후보 (최근 완료 분기 기준)")
    st.dataframe(career.promotion_candidates(con, ym), hide_index=True, use_container_width=True)
    st.subheader("목표 진척")
    st.dataframe(career.goal_progress(con, ym), hide_index=True, use_container_width=True)
    st.subheader("승격 기준")
    st.dataframe(q("SELECT f.name 현재직급, t.name 목표직급, c.metric 지표, c.period_type 기간, c.operator 연산, c.value 기준값, c.effective_from 적용시작 FROM career_promotion_criteria c JOIN career_level f ON f.level_id=c.from_level_id JOIN career_level t ON t.level_id=c.to_level_id"), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- 인증
elif page == "인증 관리":
    st.title("인증 관리 (MDRT · 우수인증설계사)")
    year = int(ym[:4])
    if st.button("진척 재계산"):
        rid, n = certification.run(con, year, ym, "ui")
        st.success(f"run {rid}: {n}행")
    prog = st.selectbox("프로그램", q("SELECT code FROM certification_program")["code"])
    st.dataframe(q("SELECT year 연도, criteria_group 기준그룹, metric 지표, operator 연산, value 기준값, note 비고 FROM certification_criteria c JOIN certification_program p ON p.program_id=c.program_id WHERE p.code=? AND year=?", (prog, year)), hide_index=True, use_container_width=True)
    d = q("""SELECT a.internal_code 사번, a.name 설계사, s.progress_rate 진척률, s.achieved 달성, s.projected_achieved 연말예상, s.metric_json 상세
             FROM agent_certification_status s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' JOIN agent a ON a.agent_id=s.agent_id JOIN certification_program p ON p.program_id=s.program_id
             WHERE p.code=? AND s.year=? ORDER BY s.progress_rate DESC""", (prog, year))
    st.dataframe(d, hide_index=True, use_container_width=True, column_config={"진척률": st.column_config.ProgressColumn(min_value=0, max_value=1)})

# ---------------------------------------------------------------- 마감
elif page == "마감":
    st.title("월 마감")
    cp = q("SELECT * FROM close_period WHERE period_ym=?", (ym,))
    st.write("상태:", cp.iloc[0]["status"] if not cp.empty else "open")
    blockers = []
    if q("SELECT COUNT(*) FROM quality_issue i JOIN import_batch b ON b.batch_id=i.batch_id WHERE b.period_ym=? AND i.severity='block' AND i.resolved=0", (ym,)).iloc[0, 0]:
        blockers.append("차단 등급 품질 이슈가 남아 있음")
    if q("SELECT COUNT(*) FROM reconciliation_diff d JOIN calc_run r ON r.run_id=d.run_id AND r.status='done' WHERE d.period_ym=? AND d.status='open'", (ym,)).iloc[0, 0]:
        blockers.append("미처리 대사 차이가 남아 있음")
    for b in blockers:
        st.warning(b)
    if st.button("마감 실행", disabled=bool(blockers) or (not cp.empty and cp.iloc[0]["status"] == "closed")):
        con.execute("INSERT OR REPLACE INTO close_period(period_ym, status, closed_at, closed_by) VALUES(?,?,?,?)", (ym, "closed", db.now(), "ui"))
        con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), "ui", "close", ym))
        con.commit()
        st.success(f"{ym} 마감 완료")
        st.rerun()
    st.subheader("감사 로그")
    st.dataframe(q("SELECT at 시각, actor 행위자, action 행위, target 대상, after_json 내용 FROM audit_log ORDER BY id DESC LIMIT 50"), hide_index=True, use_container_width=True)
