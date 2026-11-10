# 업로드 템플릿

## 엑셀 원본 양식 (실제 적재기가 읽는 형태)

원본 엑셀은 `samples/excel/` 에 실제 예시가 있다. 열 이름이 다르면 `config/mapping/*.yaml` 만 고치면 된다.

| 예시 파일 | 내용 | 매핑 | 적재 대상 |
|-----------|------|------|-----------|
| `samples/excel/202610_서울본부_계약현황.xlsx` | 본부 계약현황. 사번·성명·원수사·상품명·구분·보험료·인정보험료·건수와 청약번호·상태·날짜 | `internal_org_contract.yaml` | contract, contract_status_history |
| `samples/excel/202610_유지율현황.xlsx` | 원수사별 13회차·25회차 대상/유지 건수 | `internal_persistency.yaml` | persistency |
| `samples/excel/202610_SAM_수수료정산서.xlsx` 등 4종 | 원수사 정산서 (원수사마다 양식이 다름) | `{원수사코드}_statement.yaml` | commission_statement_line |
| `samples/pdf/*.pdf` | 시상 공문 (구간표, 대상 구분, 조건, 환수) | PDF 추출기 | incentive_source_doc, incentive_rule 초안 |

## 마스터 입력용 CSV (UTF-8 BOM, Excel 에서 바로 열림)

| 파일 | 용도 | 적재 대상 테이블 |
|------|------|------------------|
| 01_설계사마스터 | 설계사·관리자 명부 + 소속 | agent, agent_org_history |
| 02_설계사_보험사코드매핑 | 원수사별 설계사 코드 | agent_insurer_code |
| 03_상품마스터 | 상품, 구분, 인정률 | product |
| 04_수수료율규칙 | 원수사 × 상품(구분) × 유형 × 회차 요율 | commission_rate_rule |
| 05_구간시상규칙 | PDF 추출이 안 될 때 수기 입력. 기본/추가시상, 대상 구분, 구간, 조건 | incentive_rule, incentive_rule_category, incentive_tier, incentive_condition |
| 06_계약현황_표준 / 07_수수료정산서_표준 / 08_유지현황_표준 / 09_유지율_표준 | 매핑을 거친 뒤의 표준 형태 (참고용) | contract, commission_statement_line, persistency |
| 12_외부지표 | 불완전판매·민원·교육 이수 | agent_external_metric |
| 13_커리어패스 | 직급 체계와 승격 기준 | career_level, career_promotion_criteria |
| 14_인증기준 | MDRT, 협회·원수사 인증, 임원제도, 영업우수·명예의전당 연도별 기준 | certification_program, certification_criteria |
| 15_조직_관리자 | 본부장·지점장 | org_manager |

구분 코드: whole_life(종신), health(건강), savings(저축), annuity(연금), property(재물), auto(자동차), general(일반)

주의
- 사번·설계사 코드·증권번호는 Excel 이 숫자로 바꿔 앞자리 0 을 지우지 않도록 텍스트 서식으로 둔다.
- 요율은 `8.0` = 800% (월납보험료 대비 배수), `0.3` = 30%.
- 금액은 원 단위 정수, 환수는 음수.
- 인증·임원·어워드 기준 값(템플릿의 0)은 매년 공식 공고·사내 규정 값으로 채운다.
