"""Streamlit 화면.  실행:  streamlit run app/ui.py"""
import json
import pathlib
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402
from app.engine import career, certification, commission, incentive, payments, performance, persist, ranking, reconcile  # noqa: E402
from app.loader import excel_loader, pdf_incentive  # noqa: E402
from app.periods import period_key, ym_add  # noqa: E402

st.set_page_config(page_title="GA 수수료·시상·업적관리", layout="wide")
UPLOAD_DIR = db.ROOT / "data" / "raw" / "upload"
PTYPE_KO = {"month": "월", "quarter": "분기", "half": "반기", "year": "연"}
CAT_KO = {"mdrt": "MDRT", "association_cert": "협회 인증", "insurer_cert": "원수사 인증", "executive_fc": "설계사 임원제도",
          "executive_manager": "관리자 임원제도", "award": "영업우수 · 명예의전당"}


@st.cache_resource
def get_con():
    return db.connect()


con = get_con()


def q(sql, params=()):
    return db.df(con, sql, params)


def show(d, **kw):
    st.dataframe(d, hide_index=True, width="stretch", **kw)


def months():
    return [r["period_ym"] for r in con.execute("SELECT DISTINCT period_ym FROM calc_run ORDER BY 1")]


def won(v):
    return f"{int(v):,}" if pd.notna(v) else "-"


def save_upload(up):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / up.name
    path.write_bytes(up.getvalue())
    return path


def is_closed(ym):
    r = con.execute("SELECT status FROM close_period WHERE period_ym=?", (ym,)).fetchone()
    return bool(r and r["status"] == "closed")


st.sidebar.title("GA 정산관리")
page = st.sidebar.radio("메뉴", ["대시보드", "데이터 수집", "조직", "마스터", "업적", "수수료", "구간시상", "랭킹 · 명예의전당", "점검(대사)",
                                "커리어패스", "인증 · 임원제도", "마감"])
ym = st.sidebar.selectbox("기준 월", months()[::-1] if months() else ["202610"])

# ================================================================ 대시보드
if page == "대시보드":
    st.title(f"{ym} 마감 현황")
    c1, c2, c3, c4 = st.columns(4)
    stmt = q("SELECT COUNT(DISTINCT insurer_id) n FROM import_batch WHERE data_type='statement' AND period_ym=? AND status='active'", (ym,)).iloc[0, 0]
    c1.metric("정산서 수신 원수사", f"{stmt} / {q('SELECT COUNT(*) FROM insurer WHERE active=1').iloc[0, 0]}")
    comm = q("SELECT COALESCE(SUM(amount),0) v FROM commission_calc c JOIN calc_run r ON r.run_id=c.run_id AND r.status='done' WHERE c.period_ym=?", (ym,)).iloc[0, 0]
    c2.metric("내부 수수료 순액", won(comm))
    opn = q("SELECT COUNT(*) n, COALESCE(SUM(ABS(diff_amount)),0) a FROM reconciliation_diff d JOIN calc_run r ON r.run_id=d.run_id AND r.status='done' WHERE d.period_ym=? AND d.status='open'", (ym,)).iloc[0]
    c3.metric("미처리 대사 차이", f"{int(opn['n'])}건", f"{won(opn['a'])}원", delta_color="off")
    inc = q("SELECT COALESCE(SUM(reward_amount),0) v FROM incentive_result i JOIN calc_run r ON r.run_id=i.run_id AND r.status='done' WHERE i.period_ym=?", (ym,)).iloc[0, 0]
    c4.metric("시상 총액 (기본+추가)", won(inc))
    qi = q("SELECT q.severity 등급, COUNT(*) 건수 FROM quality_issue q JOIN import_batch b ON b.batch_id=q.batch_id WHERE b.period_ym=? AND q.resolved=0 GROUP BY 1", (ym,))
    if not qi.empty:
        st.warning("미해결 품질 이슈: " + ", ".join(f"{r['등급']} {r['건수']}건" for _, r in qi.iterrows()) + " (데이터 수집 → 품질검증)")
    st.subheader("단계별 진행")
    show(q("SELECT run_type 단계, MAX(finished_at) 완료시각, note 비고 FROM calc_run WHERE period_ym=? AND status='done' GROUP BY run_type", (ym,)))
    st.subheader("지점별 월 실적")
    show(q("""SELECT h.name 본부, v.org_name 지점, v.applied_count 청약, v.new_count 성립, v.withdrawn_count 철회, v.net_count 순건수, v.converted_premium 인정보험료, v.commission_amount 수수료
              FROM v_org_performance v JOIN org_unit o ON o.org_id=v.org_id LEFT JOIN org_unit h ON h.org_id=o.parent_id
              WHERE v.period_type='month' AND v.period_key=? ORDER BY 1,2""", (ym,)))
    st.subheader("대사 차이 유형별")
    show(q("SELECT i.code 원수사, d.diff_type 유형, d.status 상태, COUNT(*) 건수, SUM(d.diff_amount) 차이합 FROM reconciliation_diff d JOIN insurer i ON i.insurer_id=d.insurer_id JOIN calc_run r ON r.run_id=d.run_id AND r.status='done' WHERE d.period_ym=? GROUP BY 1,2,3", (ym,)))

# ================================================================ 데이터 수집
elif page == "데이터 수집":
    st.title("데이터 수집")
    t = st.tabs(["본부 계약 엑셀 업로드", "추출 데이터", "품질검증", "시상 공문 PDF", "원수사 정산서 업로드", "적재 배치 이력"])
    with t[0]:
        st.caption("본부 계약현황 엑셀에서 사번 · 성명 · 원수사 · 상품 · 구분 · 보험료 · 인정보험료 · 건수를 뽑아 계약 원장에 반영한다. "
                   "청약은 예정 업적, 성립만 확정 업적, 철회는 제외·차감된다. 매핑: config/mapping/internal_org_contract.yaml")
        hqs = q("SELECT org_id, name FROM org_unit WHERE level='hq'")
        hq = st.selectbox("업로드 본부", hqs["org_id"].tolist(), format_func=lambda x: hqs.set_index("org_id").loc[x, "name"])
        up = st.file_uploader(f"{ym} 계약현황 엑셀 (.xlsx)", type=["xlsx"], key="org")
        if up and st.button("적재 실행", disabled=is_closed(ym)):
            try:
                s = excel_loader.load_org_contract(con, save_upload(up), ym, int(hq), by="ui")
                st.success(f"batch {s['batch_id']}: {s['rows']}행 → 신규 {s['inserted']}, 상태변경 {s['updated']}, 제외 {s['skipped']} (차단 {s['block']}, 경고 {s['warn']})")
                st.info("적재 후에는 수수료·구간시상 메뉴에서 재계산을 실행하면 반영된다.")
                payments.accrue(con, ym)
                performance.run(con, ym, "ui")
            except excel_loader.DuplicateFile as e:
                st.error(str(e))
        st.subheader("현재 청약 중 (예정 업적)")
        show(q("SELECT 사번, 성명, 원수사, 상품, 구분, 보험료, 인정보험료, 건수, 청약번호 FROM v_contract_extract WHERE 상태='applied' ORDER BY 사번"))
    with t[1]:
        st.caption(f"{ym} 귀속 확정 업적 (성립 건, 철회 제외). 엑셀에서 뽑아낸 8개 항목이다.")
        ext = q("""SELECT 사번, 성명, 원수사, 상품, 구분, CASE 시상대상구분 WHEN 1 THEN '대상' ELSE '-' END 시상대상, 보험료, 인정보험료, 건수
                   FROM v_contract_extract WHERE 귀속월=? AND 상태 NOT IN ('applied','declined','withdrawn') ORDER BY 사번, 원수사""", (ym,))
        show(ext)
        st.download_button("CSV 내려받기", ext.to_csv(index=False).encode("utf-8-sig"), f"{ym}_업적추출.csv", "text/csv")
        st.subheader("구분별 합계")
        show(ext.groupby(["구분", "시상대상"], as_index=False)[["건수", "보험료", "인정보험료"]].sum().sort_values("인정보험료", ascending=False))
    with t[2]:
        qi = q("""SELECT q.id, b.file_name 파일, q.row_no 행, q.severity 등급, q.check_code 검사, q.message 내용, q.resolved 해결
                  FROM quality_issue q JOIN import_batch b ON b.batch_id=q.batch_id WHERE b.period_ym=? ORDER BY q.resolved, q.severity, q.id""", (ym,))
        show(qi)
        opts = qi[qi["해결"] == 0]["id"].tolist()
        sel = st.multiselect("해결 처리할 이슈 id", opts)
        if st.button("해결 처리", disabled=not sel):
            con.executemany("UPDATE quality_issue SET resolved=1 WHERE id=?", [(i,) for i in sel])
            con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), "ui", "quality_resolve", json.dumps(sel)))
            con.commit()
            st.rerun()
    with t[3]:
        st.caption("시상 공문 PDF 에서 구간표·대상 구분·조건을 추출해 초안을 만든다. 승인 전에는 계산에 쓰이지 않는다.")
        up = st.file_uploader("시상 공문 PDF", type=["pdf"], key="pdf")
        if up and st.button("추출 실행"):
            try:
                doc_id, parsed = pdf_incentive.save_draft(con, save_upload(up), "ui")
                st.success(f"doc {doc_id}: 규칙 초안 {len(parsed['sections'])}개 생성")
            except ValueError as e:
                st.error(str(e))
        docs = q("""SELECT d.doc_id, COALESCE(i.name,'내부') 발신, d.period_ym 적용시작, d.title 제목, d.extraction_status 상태, d.reviewed_by 검토자, d.file_path 파일
                    FROM incentive_source_doc d LEFT JOIN insurer i ON i.insurer_id=d.insurer_id ORDER BY d.extraction_status='reviewed', d.doc_id DESC""")
        show(docs)
        if not docs.empty:
            did = st.selectbox("공문 선택", docs["doc_id"].tolist(), format_func=lambda x: f"{x} · {docs.set_index('doc_id').loc[x, '제목']} ({docs.set_index('doc_id').loc[x, '상태']})")
            doc = con.execute("SELECT * FROM incentive_source_doc WHERE doc_id=?", (did,)).fetchone()
            parsed = json.loads(doc["note"]) if doc["note"] else {}
            for i, r in enumerate(con.execute("SELECT * FROM incentive_rule WHERE source_doc_id=? ORDER BY rule_id", (did,)).fetchall()):
                sec = parsed.get("sections", [{}] * 99)[i] if i < len(parsed.get("sections", [])) else {}
                cats = q("SELECT c.name FROM incentive_rule_category rc JOIN product_category c ON c.code=rc.product_category WHERE rc.rule_id=?", (r["rule_id"],))["name"].tolist()
                st.markdown(f"**{'기본시상' if r['rule_kind'] == 'base' else '추가시상'} · {r['name']}** ({r['status']})  \n"
                            f"대상 구분: {', '.join(cats) or '전 구분'} · 지표: {r['metric']} ({PTYPE_KO[r['period_type']]}) · 적용: {r['effective_from']}~{r['effective_to'] or ''} · 환수 감시: {r['clawback_months']}개월")
                a, b = st.columns(2)
                a.dataframe(q("SELECT tier_no 구간, min_value 이상, max_value 미만, reward_type 방식, reward_value 값 FROM incentive_tier WHERE rule_id=?", (r["rule_id"],)), hide_index=True)
                b.dataframe(q("SELECT condition_type 조건, operator 연산, value 값 FROM incentive_condition WHERE rule_id=?", (r["rule_id"],)), hide_index=True)
                for w in sec.get("warnings", []):
                    st.caption(f"⚠ {w}")
            if doc["extraction_status"] == "extracted":
                who = st.text_input("검토자", key="rev")
                a, b = st.columns(2)
                if a.button("승인 (규칙 활성화)", disabled=not who):
                    pdf_incentive.approve(con, int(did), who)
                    st.rerun()
                if b.button("반려", disabled=not who):
                    pdf_incentive.reject(con, int(did), who)
                    st.rerun()
    with t[4]:
        ins = q("SELECT code, name FROM insurer")
        code = st.selectbox("원수사", ins["code"].tolist(), format_func=lambda x: ins.set_index("code").loc[x, "name"])
        st.caption(f"매핑: config/mapping/{code}_statement.yaml · 같은 월 재수신은 새 버전으로 쌓이고 이전 배치는 superseded 가 된다.")
        up = st.file_uploader(f"{ym} 수수료 정산서 (.xlsx)", type=["xlsx"], key="stmt")
        if up and st.button("정산서 적재", disabled=is_closed(ym)):
            try:
                s = excel_loader.load_statement(con, save_upload(up), code, ym, by="ui")
                st.success(f"batch {s['batch_id']}: {s['rows']}행 (차단 {s['block']}, 경고 {s['warn']}). 점검(대사) 메뉴에서 대사를 재실행하면 반영된다.")
            except excel_loader.DuplicateFile as e:
                st.error(str(e))
    with t[5]:
        show(q("""SELECT b.batch_id, COALESCE(i.code, o.name, '내부') 출처, b.data_type 유형, b.period_ym 대상월, b.version 버전, b.file_name 파일,
                  substr(b.file_hash,1,10) 해시, b.mapping_version 매핑, b.row_count 행수, b.status 상태, b.loaded_by 적재자
                  FROM import_batch b LEFT JOIN insurer i ON i.insurer_id=b.insurer_id LEFT JOIN org_unit o ON o.org_id=b.upload_org_id ORDER BY b.batch_id DESC"""))

# ================================================================ 조직
elif page == "조직":
    st.title("조직 구성")
    rows = []
    for hq in con.execute("SELECT * FROM org_unit WHERE level='hq' ORDER BY org_id").fetchall():
        units = [hq] + con.execute("SELECT * FROM org_unit WHERE parent_id=? ORDER BY org_id", (hq["org_id"],)).fetchall()
        for u in units:
            mgr = con.execute("""SELECT a.internal_code, a.name FROM org_manager m JOIN agent a ON a.agent_id=m.agent_id
                                 WHERE m.org_id=? AND m.effective_from<=? AND (m.effective_to IS NULL OR m.effective_to>=?)""", (u["org_id"], ym, ym)).fetchone()
            members = certification.org_members(con, u["org_id"], ym)
            marks = ",".join("?" * len(members)) or "NULL"
            perf = con.execute(f"""SELECT COALESCE(SUM(s.net_count),0) n, COALESCE(SUM(s.converted_premium),0) rp, SUM(CASE WHEN s.new_count>0 THEN 1 ELSE 0 END) act
                                   FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                                   WHERE s.period_type='month' AND s.period_key=? AND s.agent_id IN ({marks})""", [ym, ym] + members).fetchone()
            rows.append({"본부": hq["name"], "조직": "(본부 전체)" if u["level"] == "hq" else u["name"], "관리자 직책": "본부장" if u["level"] == "hq" else "지점장",
                         "관리자": f"{mgr['name']} ({mgr['internal_code']})" if mgr else "-", "설계사 수": len(members), "월 순건수": perf["n"], "월 인정보험료": perf["rp"],
                         "가동률": round((perf["act"] or 0) / len(members), 2) if members else 0,
                         "13회차 유지율": persist.rate(con, members, "p13", ym)[0], "25회차 유지율": persist.rate(con, members, "p25", ym)[0], "_org": u["org_id"]})
    tree = pd.DataFrame(rows)
    show(tree.drop(columns="_org"), column_config={"가동률": st.column_config.ProgressColumn(min_value=0, max_value=1),
                                                    "13회차 유지율": st.column_config.NumberColumn(format="%.3f"), "25회차 유지율": st.column_config.NumberColumn(format="%.3f")})
    st.caption("가동률 = 그 달 성립 건이 1건 이상인 설계사 / 소속 설계사. 본부 행은 산하 지점 전체 합계다.")
    st.subheader("소속 설계사")
    sel = st.selectbox("조직", tree["_org"].tolist(), format_func=lambda x: " · ".join(tree.set_index("_org").loc[x, ["본부", "조직"]]))
    members = certification.org_members(con, int(sel), ym)
    marks = ",".join("?" * len(members)) or "NULL"
    show(q(f"""SELECT a.internal_code 사번, a.name 성명, o.name 지점, l.name 직급, a.hire_date 위촉일, s.new_count 성립, s.withdrawn_count 철회, s.converted_premium 인정보험료, s.commission_amount 수수료
               FROM agent a LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_from<=? AND (h.effective_to IS NULL OR h.effective_to>=?)
               LEFT JOIN org_unit o ON o.org_id=h.org_id
               LEFT JOIN agent_career_history ch ON ch.agent_id=a.agent_id AND ch.effective_to IS NULL LEFT JOIN career_level l ON l.level_id=ch.level_id
               LEFT JOIN (SELECT s.* FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=? WHERE s.period_type='month' AND s.period_key=?) s ON s.agent_id=a.agent_id
               WHERE a.agent_id IN ({marks}) ORDER BY o.name, a.internal_code""", [ym, ym, ym, ym] + members))
    st.subheader("소속 변경 이력")
    show(q("""SELECT a.internal_code 사번, a.name 성명, o.name 조직, h.effective_from 시작, h.effective_to 종료 FROM agent_org_history h
              JOIN agent a ON a.agent_id=h.agent_id JOIN org_unit o ON o.org_id=h.org_id
              WHERE a.agent_id IN (SELECT agent_id FROM agent_org_history GROUP BY agent_id HAVING COUNT(*)>1) ORDER BY 1, 4"""))

# ================================================================ 마스터
elif page == "마스터":
    st.title("마스터")
    t = st.tabs(["원수사 · 상품 · 구분", "설계사", "수수료율 규칙", "환수 규칙", "시상 규칙"])
    with t[0]:
        a, b = st.columns([1, 2])
        a.dataframe(q("SELECT name 구분, CASE incentive_eligible WHEN 1 THEN '대상' ELSE '-' END 시상대상기본 FROM product_category ORDER BY sort_order"), hide_index=True)
        b.dataframe(q("""SELECT i.name 원수사, CASE i.sector WHEN 'life' THEN '생명' ELSE '손해' END 업권, p.product_code 코드, p.product_name 상품명, c.name 구분, p.conversion_rate 인정률
                         FROM product p JOIN insurer i ON i.insurer_id=p.insurer_id JOIN product_category c ON c.code=p.product_category ORDER BY i.insurer_id, c.sort_order"""), hide_index=True)
    with t[1]:
        show(q("""SELECT a.internal_code 사번, a.name 성명, CASE a.job_type WHEN 'fc' THEN '설계사' ELSE '관리자' END 구분, a.hire_date 위촉일, o.name 현재소속, l.name 직급
                  FROM agent a LEFT JOIN agent_org_history h ON h.agent_id=a.agent_id AND h.effective_to IS NULL LEFT JOIN org_unit o ON o.org_id=h.org_id
                  LEFT JOIN agent_career_history ch ON ch.agent_id=a.agent_id AND ch.effective_to IS NULL LEFT JOIN career_level l ON l.level_id=ch.level_id ORDER BY a.job_type DESC, a.internal_code"""))
    with t[2]:
        show(q("""SELECT r.rule_id, i.name 원수사, p.product_code 상품, c.name 구분, r.commission_type 유형, r.installment_from 회차from, r.installment_to 회차to, r.rate 요율,
                  r.effective_from 적용시작, r.priority 우선순위, r.note 비고 FROM commission_rate_rule r JOIN insurer i ON i.insurer_id=r.insurer_id
                  LEFT JOIN product p ON p.product_id=r.product_id LEFT JOIN product_category c ON c.code=r.product_category ORDER BY i.insurer_id, r.priority, c.sort_order, r.installment_from"""))
    with t[3]:
        show(q("SELECT i.name 원수사, c.elapsed_from 경과from, c.elapsed_to 경과to, c.clawback_rate 환수율 FROM clawback_rule c JOIN insurer i ON i.insurer_id=c.insurer_id"))
    with t[4]:
        show(q("""SELECT r.rule_id, r.name 규칙명, CASE r.rule_kind WHEN 'base' THEN '기본시상' ELSE '추가시상' END 종류, COALESCE(i.name,'내부') 출처,
                  (SELECT GROUP_CONCAT(c.name, ', ') FROM incentive_rule_category rc JOIN product_category c ON c.code=rc.product_category WHERE rc.rule_id=r.rule_id) 대상구분,
                  r.period_type 기간, r.metric 지표, r.clawback_months 환수감시월, r.effective_from 적용시작, r.effective_to 적용종료, r.status 상태, d.title 근거공문
                  FROM incentive_rule r LEFT JOIN insurer i ON i.insurer_id=r.insurer_id LEFT JOIN incentive_source_doc d ON d.doc_id=r.source_doc_id ORDER BY r.rule_id"""))

# ================================================================ 업적
elif page == "업적":
    st.title("업적")
    t = st.tabs(["기간별 실적", "구분별 집계", "계약 원장", "유지율"])
    with t[0]:
        ptype = st.radio("기간 유형", list(PTYPE_KO), horizontal=True, format_func=PTYPE_KO.get)
        key = period_key(ptype, ym)
        st.caption(f"기간 키: {key} (분기·반기·연은 기준 월까지 누적). 청약은 예정 업적으로만 표시되고 인정보험료에 들어가지 않는다.")
        show(q("""SELECT a.internal_code 사번, a.name 성명, h.name 본부, o.name 지점, s.applied_count 청약, s.new_count 성립, s.withdrawn_count 철회, s.net_count 순건수,
                  s.monthly_premium 보험료, s.converted_premium 인정보험료, s.commission_amount 수수료, s.clawback_amount 환수
                  FROM agent_performance_summary s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=?
                  JOIN agent a ON a.agent_id=s.agent_id LEFT JOIN org_unit o ON o.org_id=s.org_id LEFT JOIN org_unit h ON h.org_id=o.parent_id
                  WHERE s.period_type=? AND s.period_key=? ORDER BY s.converted_premium DESC""", (ym, ptype, key)))
    with t[1]:
        st.caption(f"{ym} 귀속 성립 건 (철회 제외). 종신·건강만 시상 대상 구분이다.")
        d = q("""SELECT 사번, 성명, 구분, SUM(건수) 건수, SUM(인정보험료) 인정보험료 FROM v_contract_extract
                 WHERE 귀속월=? AND 상태 NOT IN ('applied','declined','withdrawn') GROUP BY 1,2,3""", (ym,))
        if not d.empty:
            order = q("SELECT name FROM product_category ORDER BY sort_order")["name"].tolist()
            pv = d.pivot_table(index=["사번", "성명"], columns="구분", values="인정보험료", aggfunc="sum", fill_value=0)
            pv = pv[[c for c in order if c in pv.columns] + [c for c in pv.columns if c not in order]]
            pv["시상대상(종신+건강)"] = pv.get("종신", 0) + pv.get("건강", 0)
            pv["전체"] = pv.drop(columns="시상대상(종신+건강)").sum(axis=1)
            show(pv.reset_index().sort_values("시상대상(종신+건강)", ascending=False))
    with t[2]:
        kw = st.text_input("증권번호 / 청약번호 / 성명 / 사번")
        if kw:
            show(q("""SELECT 귀속월, 사번, 성명, 원수사, 상품, 구분, 보험료, 인정보험료, 건수, 상태, 청약번호, 증권번호 FROM v_contract_extract
                      WHERE 증권번호 LIKE ? OR 청약번호 LIKE ? OR 성명 LIKE ? OR 사번 LIKE ? LIMIT 300""", (f"%{kw}%",) * 4))
    with t[3]:
        kind = st.radio("회차", ["p13", "p25"], horizontal=True, format_func=lambda x: "13회차" if x == "p13" else "25회차")
        d = q("""SELECT a.internal_code 사번, a.name 성명, i.name 원수사, i.sector, p.numerator 유지, p.denominator 대상 FROM persistency p
                 JOIN agent a ON a.agent_id=p.agent_id JOIN insurer i ON i.insurer_id=p.insurer_id WHERE p.period_ym=? AND p.kind=?""", (ym, kind))
        if not d.empty:
            def ratio(g):
                return round(g["유지"].sum() / g["대상"].sum(), 3) if g["대상"].sum() else None
            base = d.groupby(["사번", "성명"]).apply(ratio, include_groups=False).rename("전체")
            life = d[d["sector"] == "life"].groupby(["사번", "성명"]).apply(ratio, include_groups=False).rename("생명")
            non = d[d["sector"] == "nonlife"].groupby(["사번", "성명"]).apply(ratio, include_groups=False).rename("손해")
            by_ins = d.assign(율=(d["유지"] / d["대상"]).round(3)).pivot_table(index=["사번", "성명"], columns="원수사", values="율")
            show(pd.concat([base, life, non, by_ins], axis=1).reset_index().sort_values("전체", ascending=False))
        st.caption("원수사별 유지·대상 건수를 합산해 전체·생명·손해 유지율을 구한다. 시상 조건과 인증 판정에 같은 값이 쓰인다.")

# ================================================================ 수수료
elif page == "수수료":
    st.title("수수료")
    if st.button("이 달 수수료 재계산", disabled=is_closed(ym)):
        rid, n = commission.run(con, ym, "ui")
        st.success(f"run {rid}: {n}라인")
    st.subheader("설계사별 명세")
    show(q("SELECT internal_code 사번, name 성명, paid 지급, clawback 환수, net 순액 FROM v_agent_monthly_commission WHERE period_ym=? ORDER BY net DESC", (ym,)))
    code = st.selectbox("설계사 상세", q("SELECT internal_code FROM agent WHERE job_type='fc' ORDER BY 1")["internal_code"])
    show(q("""SELECT i.name 원수사, k.policy_no 증권번호, pc.name 구분, c.commission_type 유형, c.installment_no 회차, c.basis_amount 기준액, c.rate 요율, c.amount 금액, c.rule_id 규칙
              FROM commission_calc c JOIN calc_run r ON r.run_id=c.run_id AND r.status='done' JOIN contract k ON k.contract_id=c.contract_id
              JOIN insurer i ON i.insurer_id=k.insurer_id JOIN agent a ON a.agent_id=c.agent_id LEFT JOIN product p ON p.product_id=k.product_id
              LEFT JOIN product_category pc ON pc.code=p.product_category WHERE c.period_ym=? AND a.internal_code=? ORDER BY c.commission_type, k.policy_no""", (ym, code)))

# ================================================================ 구간시상
elif page == "구간시상":
    st.title("구간시상")
    st.subheader(f"{ym} 구분별 시상 · 추가시상 확인")
    rules = con.execute("""SELECT r.*, COALESCE(i.name,'내부') src FROM incentive_rule r LEFT JOIN insurer i ON i.insurer_id=r.insurer_id
                           WHERE r.status='active' AND r.effective_from<=? AND (r.effective_to IS NULL OR r.effective_to>=?)""", (ym, ym)).fetchall()
    cats = con.execute("SELECT * FROM product_category ORDER BY sort_order").fetchall()
    matrix = []
    for c in cats:
        base, extra = [], []
        for r in rules:
            inc = [x["product_category"] for x in con.execute("SELECT product_category FROM incentive_rule_category WHERE rule_id=?", (r["rule_id"],))]
            if not inc or c["code"] in inc:
                (base if r["rule_kind"] == "base" else extra).append(f"{r['name']} ({r['src']}, {PTYPE_KO[r['period_type']]})")
        matrix.append({"구분": c["name"], "기본시상": "있음" if base else "해당 없음", "추가시상": "있음" if extra else "해당 없음",
                       "기본시상 규칙": " / ".join(base), "추가시상 규칙": " / ".join(extra)})
    show(pd.DataFrame(matrix))
    st.caption("승인된(active) 규칙 가운데 기준 월에 유효한 것만 본다. 대상 구분은 시상 공문 PDF 의 '대상 구분' 에서 추출된다.")
    if st.button("이 달 시상 재계산", disabled=is_closed(ym)):
        rid, n, cb = incentive.run(con, ym, "ui")
        st.success(f"run {rid}: {n}건, 환수 {cb}건")
    st.subheader("시상 결과")
    only = st.checkbox("자격자만 보기", value=True)
    show(q(f"""SELECT a.internal_code 사번, a.name 성명, o.name 지점, CASE r.rule_kind WHEN 'base' THEN '기본' ELSE '추가' END 종류, r.name 규칙, ir.metric_value 지표값,
               ir.tier_no 구간, ir.reward_amount 시상액, ir.disqualify_reason 미자격사유
               FROM incentive_result ir JOIN calc_run cr ON cr.run_id=ir.run_id AND cr.status='done' JOIN agent a ON a.agent_id=ir.agent_id
               JOIN incentive_rule r ON r.rule_id=ir.rule_id LEFT JOIN org_unit o ON o.org_id=ir.org_id
               WHERE ir.period_ym=? {'AND ir.qualified=1' if only else ''} ORDER BY ir.reward_amount DESC, ir.metric_value DESC""", (ym,)))
    st.subheader("설계사별 합계 (기본 + 추가)")
    show(q("""SELECT a.internal_code 사번, a.name 성명, SUM(CASE WHEN r.rule_kind='base' THEN ir.reward_amount ELSE 0 END) 기본시상,
              SUM(CASE WHEN r.rule_kind='extra' THEN ir.reward_amount ELSE 0 END) 추가시상, SUM(ir.reward_amount) 합계
              FROM incentive_result ir JOIN calc_run cr ON cr.run_id=ir.run_id AND cr.status='done' JOIN agent a ON a.agent_id=ir.agent_id
              JOIN incentive_rule r ON r.rule_id=ir.rule_id WHERE ir.period_ym=? GROUP BY 1,2 HAVING SUM(ir.reward_amount)>0 ORDER BY 5 DESC""", (ym,)))
    st.subheader("시상 환수")
    show(q("""SELECT ic.period_ym 귀속월, a.name 성명, r.name 규칙, ir.period_ym 시상월, k.policy_no 증권번호, ic.amount 환수액, ic.reason 사유
              FROM incentive_clawback ic JOIN incentive_result ir ON ir.result_id=ic.result_id JOIN agent a ON a.agent_id=ir.agent_id
              JOIN incentive_rule r ON r.rule_id=ir.rule_id JOIN contract k ON k.contract_id=ic.contract_id ORDER BY ic.period_ym DESC"""))

# ================================================================ 랭킹
elif page == "랭킹 · 명예의전당":
    st.title("랭킹 · 명예의전당")
    prev = ym_add(ym, -1)
    st.info(f"기준 월 {ym} 은 집계 중이다. 전체 설계사 공유는 확정된 전월({prev}) 순위로 한다.")
    a, b, c = st.columns(3)
    target = a.radio("대상 월", [prev, ym], horizontal=True, format_func=lambda x: f"{x} (전월·공유)" if x == prev else f"{x} (당월·집계 중)")
    metric = b.radio("지표", ["recognized_premium", "net_count"], horizontal=True, format_func=lambda x: "인정보험료" if x == "recognized_premium" else "건수")
    scope = c.radio("범위", ["all", "eligible"], horizontal=True, format_func=lambda x: "전 구분" if x == "all" else "시상 대상(종신·건강)")
    rk = q("""SELECT s.rank_no 순위, a.internal_code 사번, a.name 성명, h.name 본부, o.name 지점, s.value 값 FROM ranking_snapshot s
              JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=? JOIN agent a ON a.agent_id=s.agent_id
              LEFT JOIN org_unit o ON o.org_id=s.org_id LEFT JOIN org_unit h ON h.org_id=o.parent_id
              WHERE s.period_type='month' AND s.period_key=? AND s.metric=? AND s.scope=? ORDER BY s.rank_no, a.internal_code""", (target, target, metric, scope))
    if not rk.empty:
        top = rk.iloc[0]
        st.metric(f"{target} 1위", f"{top['성명']} ({top['지점']})", f"{top['값']:,.0f}", delta_color="off")
        show(rk)
        st.download_button("공유용 CSV 내려받기", rk.to_csv(index=False).encode("utf-8-sig"), f"{target}_랭킹_{metric}_{scope}.csv", "text/csv", disabled=(target != prev))
    st.subheader(f"{ym[:4]}년 누적 순위 (영업우수설계사 판정 기준)")
    show(q("""SELECT s.rank_no 순위, a.internal_code 사번, a.name 성명, o.name 지점, s.value 누적인정보험료 FROM ranking_snapshot s
              JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=? JOIN agent a ON a.agent_id=s.agent_id LEFT JOIN org_unit o ON o.org_id=s.org_id
              WHERE s.period_type='year' AND s.metric='recognized_premium' AND s.scope='all' ORDER BY s.rank_no LIMIT 10""", (ym,)))
    st.subheader("명예의전당 · 영업우수설계사 수상 이력")
    show(q("""SELECT a.internal_code 사번, a.name 성명, p.name 프로그램, GROUP_CONCAT(w.year, ', ') 수상연도, COUNT(*) 누적횟수
              FROM agent_certification_award w JOIN agent a ON a.agent_id=w.agent_id JOIN certification_program p ON p.program_id=w.program_id
              GROUP BY a.agent_id, p.program_id ORDER BY 5 DESC, 1"""))
    st.caption("명예의전당 판정은 과거 수상 횟수에 올해 영업우수설계사 달성 여부를 더해 계산한다. 인증 · 임원제도 메뉴에서 진척을 본다.")

# ================================================================ 점검
elif page == "점검(대사)":
    st.title("점검(대사)")
    if st.button("대사 재실행", disabled=is_closed(ym)):
        try:
            rid, n = reconcile.run(con, ym, "ui")
            st.success(f"run {rid}: 차이 {n}건")
        except RuntimeError as e:
            st.error(str(e))
    show(q("SELECT period_ym 월, insurer_code 원수사, diff_count 차이건수, open_count 미처리, open_amount 미처리금액 FROM v_reconciliation_summary WHERE period_ym=?", (ym,)))
    f = st.multiselect("상태", ["open", "confirmed", "adjusted", "ignored"], default=["open"])
    d = q(f"""SELECT d.diff_id, i.code 원수사, d.policy_no 증권번호, d.commission_type 유형, d.installment_no 회차, d.statement_amount 원수사금액, d.internal_amount 내부금액,
              d.diff_amount 차이, d.diff_type 차이유형, d.status 상태, d.assignee 담당, d.note 메모
              FROM reconciliation_diff d JOIN insurer i ON i.insurer_id=d.insurer_id JOIN calc_run r ON r.run_id=d.run_id AND r.status='done'
              WHERE d.period_ym=? AND d.status IN ({','.join('?' * len(f)) or "''"}) ORDER BY ABS(d.diff_amount) DESC""", (ym, *f))
    show(d)
    with st.form("처리"):
        st.subheader("차이 처리")
        ids = st.multiselect("diff_id (여러 건 선택 가능)", d["diff_id"].tolist() if not d.empty else [])
        new = st.selectbox("상태", ["confirmed", "adjusted", "ignored", "open"])
        who = st.text_input("담당자")
        note = st.text_area("메모 (근거: 공문/메일 경로)")
        if st.form_submit_button("저장") and ids:
            for did in ids:
                con.execute("UPDATE reconciliation_diff SET status=?, assignee=?, note=?, resolved_at=CASE WHEN ?='open' THEN NULL ELSE ? END WHERE diff_id=?", (new, who, note, new, db.now(), did))
                con.execute("INSERT INTO audit_log(at, actor, action, target, after_json) VALUES(?,?,?,?,?)", (db.now(), who or "ui", "diff_update", f"diff:{did}", json.dumps({"status": new, "note": note}, ensure_ascii=False)))
            con.commit()
            st.rerun()

# ================================================================ 커리어패스
elif page == "커리어패스":
    st.title("커리어패스")
    st.subheader("승격 후보 (최근 완료 분기 기준)")
    show(career.promotion_candidates(con, ym))
    st.subheader("목표 진척")
    show(career.goal_progress(con, ym), column_config={"진척률": st.column_config.ProgressColumn(min_value=0, max_value=1)})
    st.subheader("승격 기준")
    show(q("SELECT f.name 현재직급, t.name 목표직급, c.metric 지표, c.period_type 기간, c.operator 연산, c.value 기준값, c.effective_from 적용시작 FROM career_promotion_criteria c JOIN career_level f ON f.level_id=c.from_level_id JOIN career_level t ON t.level_id=c.to_level_id"))

# ================================================================ 인증 · 임원제도
elif page == "인증 · 임원제도":
    st.title("인증 · 임원제도 · 어워드")
    year = int(ym[:4])
    if st.button("진척 재계산"):
        ranking.run(con, ym, "ui")
        rid, n = certification.run(con, year, ym, "ui")
        st.success(f"run {rid}: {n}행")
    st.subheader("프로그램 현황")
    show(q("""SELECT p.category, p.name 프로그램, p.issuer 주관, CASE p.target WHEN 'manager' THEN '관리자' ELSE '설계사' END 대상,
              CASE p.sector WHEN 'life' THEN '생명' WHEN 'nonlife' THEN '손해' ELSE '전체' END 업권, COUNT(s.id) 평가인원, SUM(s.achieved) 달성, SUM(s.projected_achieved) 연말예상
              FROM certification_program p LEFT JOIN agent_certification_status s ON s.program_id=p.program_id AND s.year=?
                   AND s.run_id=(SELECT MAX(run_id) FROM calc_run WHERE run_type='certification' AND status='done' AND period_ym=?)
              GROUP BY p.program_id ORDER BY p.sort_order""", (year, ym)).assign(category=lambda d: d["category"].map(CAT_KO)).rename(columns={"category": "분류"}))
    progs = q("SELECT code, name, category FROM certification_program ORDER BY sort_order")
    cat = st.radio("분류", list(CAT_KO), horizontal=True, format_func=CAT_KO.get)
    sub = progs[progs["category"] == cat]
    prog = st.selectbox("프로그램", sub["code"].tolist(), format_func=lambda x: sub.set_index("code").loc[x, "name"])
    st.caption("기준 값은 샘플이다. 실제 운영 시 공식 공고·사내 규정 값으로 매년 입력한다. 같은 기준그룹 안은 모두 충족(AND), 그룹이 여럿이면 하나만 충족(OR).")
    show(q("""SELECT c.criteria_group 기준그룹, c.metric 지표, c.metric_param 대상, c.operator 연산, c.value 기준값 FROM certification_criteria c
              JOIN certification_program p ON p.program_id=c.program_id WHERE p.code=? AND c.year=?""", (prog, year)))
    d = q("""SELECT a.internal_code 사번, a.name 성명, o.name 평가조직, s.progress_rate 진척률, s.achieved 달성, s.projected_achieved 연말예상, s.metric_json
             FROM agent_certification_status s JOIN calc_run r ON r.run_id=s.run_id AND r.status='done' AND r.period_ym=? JOIN agent a ON a.agent_id=s.agent_id
             JOIN certification_program p ON p.program_id=s.program_id LEFT JOIN org_unit o ON o.org_id=s.org_id
             WHERE p.code=? AND s.year=? ORDER BY s.achieved DESC, s.progress_rate DESC""", (ym, prog, year))
    if not d.empty:
        detail = d["metric_json"].apply(lambda j: {k: f"{v['실적']:,.3g}".rstrip("0").rstrip(".") + (" ✓" if v["충족"] else " ✗") for k, v in json.loads(j).items()})
        out = pd.concat([d.drop(columns="metric_json"), pd.DataFrame(detail.tolist())], axis=1)
        out["달성"] = out["달성"].map({1: "달성", 0: "-"})
        out["연말예상"] = out["연말예상"].map({1: "달성 예상", 0: "-"})
        if out["평가조직"].isna().all():
            out = out.drop(columns="평가조직")
        show(out, column_config={"진척률": st.column_config.ProgressColumn(min_value=0, max_value=1)})

# ================================================================ 마감
elif page == "마감":
    st.title("월 마감")
    closed = is_closed(ym)
    st.write("상태:", "closed (마감됨)" if closed else "open")
    blockers = []
    n = q("SELECT COUNT(*) FROM quality_issue i JOIN import_batch b ON b.batch_id=i.batch_id WHERE b.period_ym=? AND i.severity='block' AND i.resolved=0", (ym,)).iloc[0, 0]
    if n:
        blockers.append(f"차단 등급 품질 이슈 {n}건 미해결 (데이터 수집 → 품질검증)")
    n = q("SELECT COUNT(*) FROM reconciliation_diff d JOIN calc_run r ON r.run_id=d.run_id AND r.status='done' WHERE d.period_ym=? AND d.status='open'", (ym,)).iloc[0, 0]
    if n:
        blockers.append(f"미처리 대사 차이 {n}건 (점검 메뉴)")
    n = q("SELECT COUNT(*) FROM incentive_source_doc WHERE period_ym=? AND extraction_status='extracted'", (ym,)).iloc[0, 0]
    if n:
        blockers.append(f"검토 대기 중인 시상 공문 {n}건 (데이터 수집 → 시상 공문 PDF)")
    for b in blockers:
        st.warning(b)
    if st.button("마감 실행", disabled=bool(blockers) or closed):
        con.execute("INSERT OR REPLACE INTO close_period(period_ym, status, closed_at, closed_by) VALUES(?,?,?,?)", (ym, "closed", db.now(), "ui"))
        con.execute("INSERT INTO audit_log(at, actor, action, target) VALUES(?,?,?,?)", (db.now(), "ui", "close", ym))
        con.commit()
        st.rerun()
    st.subheader("감사 로그")
    show(q("SELECT at 시각, actor 행위자, action 행위, target 대상, after_json 내용 FROM audit_log ORDER BY id DESC LIMIT 50"))
