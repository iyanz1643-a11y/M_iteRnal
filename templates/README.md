# 업로드 템플릿

모든 파일은 UTF-8 (BOM 포함) CSV라 Excel에서 바로 열린다. 첫 행은 헤더, 둘째 행은 예시 값이다.

| 파일 | 용도 | 적재 대상 테이블 |
|------|------|------------------|
| 01_설계사마스터 | 설계사 명부 + 초기 소속 | agent, agent_org_history |
| 02_설계사_보험사코드매핑 | 보험사별 설계사 코드 | agent_insurer_code |
| 03_상품마스터 | 상품, 환산율 | product |
| 04_수수료율규칙 | 보험사 × 상품(군) × 유형 × 회차 요율 | commission_rate_rule |
| 05_구간시상규칙 | 규칙·구간·조건을 한 파일에 (행 유형이 섞임) | incentive_rule, incentive_tier, incentive_condition |
| 06_계약현황_표준 | 보험사 신계약/계약 현황을 표준화한 형태 | contract, contract_status_history |
| 07_수수료정산서_표준 | 보험사 정산서를 표준화한 형태 | commission_statement_line |
| 08_유지현황_표준 | 상태 변경 | contract_status_history |
| 09_유지율_표준 | 보험사 제공 유지율 (내부 산출과 비교용) | persistency |
| 10_시상구간_추출결과 | 시상 공문 PDF에서 추출(또는 수기 입력)한 구간·조건, 검토 후 승인 | incentive_source_doc, incentive_rule, incentive_tier, incentive_condition |
| 11_본부계약업로드 | 본부 담당자가 올리는 청약·성립·철회 현황 | contract, contract_status_history |
| 12_외부지표 | 불완전판매·민원·교육 이수 등 외부 지표 | agent_external_metric |
| 13_커리어패스 | 직급 체계와 승격 기준 (행 유형이 섞임) | career_level, career_promotion_criteria |
| 14_인증기준 | MDRT·우수인증설계사 연도별 기준 (값은 공식 공고로 매년 교체) | certification_program, certification_criteria |

06~08은 보험사 원본을 직접 올리는 게 아니라 **매핑을 거친 결과**의 모양이다.
보험사 원본은 `raw/`에 그대로 두고, 매핑 파일(`config/mapping/*.yaml`)로 이 형태를 만든다.

주의
- 코드류(설계사 코드, 증권번호)는 Excel이 숫자로 바꿔 앞자리 0을 지우므로 텍스트 서식으로 열어야 한다.
- 요율은 `8.5` = 850% (월납보험료 대비 배수), `0.3` = 30% 로 소수 표기한다.
- 금액은 원 단위 정수, 환수는 음수.
