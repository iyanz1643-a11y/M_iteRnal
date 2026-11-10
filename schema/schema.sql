-- =====================================================================
-- 보험 GA 수수료·구간시상·업적관리 표준 스키마
-- SQLite / PostgreSQL 호환 (타입은 양쪽에서 동작하는 범위로 제한)
-- 원칙: Raw/Staging 불변, Core append-only, Calc는 run 단위 재생성
-- =====================================================================

-- ---------- 마스터 ----------
CREATE TABLE insurer (
    insurer_id      INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,         -- 예: SAM, HAN
    name            TEXT NOT NULL,
    sector          TEXT NOT NULL DEFAULT 'life', -- life(생명보험) / nonlife(손해보험)
    active          INTEGER NOT NULL DEFAULT 1
);

-- 상품 구분 마스터 (종신/건강/저축/연금/재물/자동차/일반)
CREATE TABLE product_category (
    code                TEXT PRIMARY KEY,         -- whole_life / health / savings / annuity / property / auto / general
    name                TEXT NOT NULL UNIQUE,     -- 종신 / 건강 / ...
    incentive_eligible  INTEGER NOT NULL DEFAULT 0, -- 시상 대상 기본값 (규칙별 대상 구분이 우선)
    sort_order          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE product (
    product_id          INTEGER PRIMARY KEY,
    insurer_id          INTEGER NOT NULL REFERENCES insurer(insurer_id),
    product_code        TEXT NOT NULL,
    product_name        TEXT NOT NULL,
    product_category    TEXT NOT NULL REFERENCES product_category(code),
    conversion_rate     NUMERIC NOT NULL DEFAULT 1.0,  -- 환산율
    effective_from      TEXT NOT NULL,            -- YYYYMM
    effective_to        TEXT,                     -- NULL = 현재 유효
    UNIQUE (insurer_id, product_code, effective_from)
);

CREATE TABLE org_unit (
    org_id      INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES org_unit(org_id),
    name        TEXT NOT NULL,
    level       TEXT NOT NULL,                    -- hq(본부) / branch(지점) / team(팀)
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE agent (
    agent_id        INTEGER PRIMARY KEY,
    internal_code   TEXT NOT NULL UNIQUE,         -- 내부 사번
    name            TEXT NOT NULL,
    hire_date       TEXT,                         -- 위촉일 YYYY-MM-DD
    termination_date TEXT,                        -- 해촉일
    status          TEXT NOT NULL DEFAULT 'active', -- active / terminated / suspended
    job_type        TEXT NOT NULL DEFAULT 'fc'    -- fc(설계사) / manager(관리자: 지점장·본부장)
);

-- 조직 관리자 (본부장, 지점장) 이력
CREATE TABLE org_manager (
    id              INTEGER PRIMARY KEY,
    org_id          INTEGER NOT NULL REFERENCES org_unit(org_id),
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    role            TEXT NOT NULL,                -- hq_head(본부장) / branch_head(지점장)
    effective_from  TEXT NOT NULL,                -- YYYYMM
    effective_to    TEXT
);

CREATE TABLE agent_insurer_code (
    id                  INTEGER PRIMARY KEY,
    agent_id            INTEGER NOT NULL REFERENCES agent(agent_id),
    insurer_id          INTEGER NOT NULL REFERENCES insurer(insurer_id),
    insurer_agent_code  TEXT NOT NULL,
    effective_from      TEXT NOT NULL,            -- YYYYMM
    effective_to        TEXT,
    UNIQUE (insurer_id, insurer_agent_code, effective_from)
);

CREATE TABLE agent_org_history (
    id              INTEGER PRIMARY KEY,
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    org_id          INTEGER NOT NULL REFERENCES org_unit(org_id),
    effective_from  TEXT NOT NULL,                -- YYYYMM
    effective_to    TEXT
);

CREATE TABLE commission_rate_rule (
    rule_id             INTEGER PRIMARY KEY,
    insurer_id          INTEGER NOT NULL REFERENCES insurer(insurer_id),
    product_id          INTEGER REFERENCES product(product_id),   -- NULL이면 product_category 기준
    product_category    TEXT,
    commission_type     TEXT NOT NULL,            -- initial / renewal / persistency / promo
    installment_from    INTEGER NOT NULL,
    installment_to      INTEGER,                  -- NULL = 이후 전부
    basis               TEXT NOT NULL,            -- monthly_premium / converted_premium / annual_premium
    rate                NUMERIC NOT NULL,
    effective_from      TEXT NOT NULL,            -- YYYYMM
    effective_to        TEXT,
    priority            INTEGER NOT NULL DEFAULT 100, -- 낮을수록 우선
    note                TEXT,
    CHECK (product_id IS NOT NULL OR product_category IS NOT NULL)
);

CREATE TABLE clawback_rule (
    rule_id             INTEGER PRIMARY KEY,
    insurer_id          INTEGER NOT NULL REFERENCES insurer(insurer_id),
    product_category    TEXT,
    elapsed_from        INTEGER NOT NULL,         -- 경과 회차 시작
    elapsed_to          INTEGER,                  -- 경과 회차 끝 (NULL = 이후)
    clawback_rate       NUMERIC NOT NULL,         -- 환수 비율 (1.0 = 전액)
    effective_from      TEXT NOT NULL,
    effective_to        TEXT
);

-- 시상 공문 PDF (보험사 시책 / 내부 시상 안내)
CREATE TABLE incentive_source_doc (
    doc_id              INTEGER PRIMARY KEY,
    insurer_id          INTEGER REFERENCES insurer(insurer_id),   -- NULL = 내부 시상
    period_ym           TEXT NOT NULL,
    title               TEXT,
    file_path           TEXT NOT NULL,            -- raw/ 하위 상대경로
    file_hash           TEXT NOT NULL UNIQUE,
    received_at         TEXT NOT NULL,
    extraction_method   TEXT,                     -- text / ocr / manual
    extraction_status   TEXT NOT NULL DEFAULT 'pending', -- pending / extracted / reviewed / rejected
    reviewed_by         TEXT,
    reviewed_at         TEXT,
    note                TEXT
);

CREATE TABLE incentive_rule (
    rule_id         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    insurer_id      INTEGER REFERENCES insurer(insurer_id),   -- NULL = 내부 시상
    source_doc_id   INTEGER REFERENCES incentive_source_doc(doc_id),
    version         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'draft', -- draft / active / retired
    rule_kind       TEXT NOT NULL DEFAULT 'base', -- base(기본시상) / extra(추가시상)
    period_type     TEXT NOT NULL,                -- month / quarter / half / year
    metric          TEXT NOT NULL,                -- converted_premium / count / monthly_premium / annual_premium
    scope           TEXT NOT NULL DEFAULT 'all',  -- all / insurer / product_category
    scope_value     TEXT,
    clawback_months INTEGER NOT NULL DEFAULT 0,   -- 시상 환수 감시 기간 (0 = 없음)
    effective_from  TEXT NOT NULL,                -- 월별 시책이면 from = to = 대상 월
    effective_to    TEXT,
    approved_by     TEXT,
    approved_at     TEXT
);

-- 규칙별 시상 대상 상품 구분 (행이 없으면 전 구분 대상)
CREATE TABLE incentive_rule_category (
    rule_id             INTEGER NOT NULL REFERENCES incentive_rule(rule_id),
    product_category    TEXT NOT NULL REFERENCES product_category(code),
    PRIMARY KEY (rule_id, product_category)
);

CREATE TABLE incentive_tier (
    id          INTEGER PRIMARY KEY,
    rule_id     INTEGER NOT NULL REFERENCES incentive_rule(rule_id),
    tier_no     INTEGER NOT NULL,
    min_value   NUMERIC NOT NULL,                 -- 이상
    max_value   NUMERIC,                          -- 미만, NULL = 무한
    reward_type TEXT NOT NULL,                    -- rate / fixed
    reward_value NUMERIC NOT NULL,
    UNIQUE (rule_id, tier_no)
);

CREATE TABLE incentive_condition (
    id              INTEGER PRIMARY KEY,
    rule_id         INTEGER NOT NULL REFERENCES incentive_rule(rule_id),
    condition_type  TEXT NOT NULL,                -- persistency_13 / persistency_25 / min_count / tenure_months / exclude_product_category
    operator        TEXT NOT NULL,                -- >= / <= / = / in / not_in
    value           TEXT NOT NULL
);

-- ---------- 수집 ----------
CREATE TABLE import_batch (
    batch_id        INTEGER PRIMARY KEY,
    insurer_id      INTEGER REFERENCES insurer(insurer_id),   -- 내부 파일이면 NULL
    data_type       TEXT NOT NULL,                -- statement / newbiz / inforce / payment / clawback / agentcode / internal / org_contract / external_metric
    upload_org_id   INTEGER REFERENCES org_unit(org_id),      -- 본부 단위 업로드 시 업로드 조직
    period_ym       TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    file_name       TEXT NOT NULL,
    file_path       TEXT NOT NULL,                -- raw/ 하위 상대경로
    file_hash       TEXT NOT NULL UNIQUE,         -- SHA-256
    received_via    TEXT,                         -- portal / email / manual
    received_at     TEXT NOT NULL,
    loaded_at       TEXT,
    loaded_by       TEXT,
    mapping_version TEXT,                         -- 사용한 매핑 파일 버전
    row_count       INTEGER,
    status          TEXT NOT NULL DEFAULT 'received', -- received / loaded / failed / superseded / active
    note            TEXT
);

CREATE TABLE raw_row (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES import_batch(batch_id),
    sheet_name  TEXT,
    row_no      INTEGER NOT NULL,
    raw_json    TEXT NOT NULL                     -- 원본 셀 값 그대로 (문자열)
);

CREATE TABLE quality_issue (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES import_batch(batch_id),
    row_no      INTEGER,
    severity    TEXT NOT NULL,                    -- block / warn
    check_code  TEXT NOT NULL,                    -- missing_column / blank_policy_no / duplicate_key / total_mismatch / unmapped_agent / unmapped_product / bad_date / negative_amount / volume_jump
    message     TEXT NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0
);

-- ---------- Core: 업적 ----------
CREATE TABLE contract (
    contract_id         INTEGER PRIMARY KEY,
    insurer_id          INTEGER NOT NULL REFERENCES insurer(insurer_id),
    policy_no           TEXT,                     -- 정규화된 증권번호 (청약 단계는 NULL)
    product_id          INTEGER REFERENCES product(product_id),
    product_code_raw    TEXT,                     -- 마스터에 없을 때 보존
    agent_id            INTEGER REFERENCES agent(agent_id),
    insurer_agent_code  TEXT,
    policyholder_name   TEXT,
    application_no      TEXT,                     -- 청약번호 (성립 전 식별 키)
    application_date    TEXT,                     -- 청약일
    contract_date       TEXT,                     -- 성립일
    perf_date           TEXT,                     -- 업적 기준일 (내부 정책)
    perf_ym             TEXT,                     -- 업적 귀속 월
    payment_mode        TEXT,                     -- monthly / annual / single / quarterly / semiannual
    payment_term        INTEGER,                  -- 납입 기간(년)
    premium             NUMERIC,                  -- 원 보험료
    premium_monthly     NUMERIC,                  -- 월납 환산
    converted_premium   NUMERIC,                  -- 인정보험료(환산보험료)
    policy_count        NUMERIC NOT NULL DEFAULT 1, -- 건수 (공동모집 0.5 등)
    status              TEXT NOT NULL DEFAULT 'applied', -- applied / inforce / withdrawn / declined / lapsed / cancelled / matured
    status_date         TEXT,
    withdrawal_date     TEXT,                     -- 청약철회일
    source_batch_id     INTEGER REFERENCES import_batch(batch_id),
    created_at          TEXT NOT NULL,
    UNIQUE (insurer_id, policy_no),
    UNIQUE (insurer_id, application_no)
);

CREATE TABLE contract_status_history (
    id          INTEGER PRIMARY KEY,
    contract_id INTEGER NOT NULL REFERENCES contract(contract_id),
    status      TEXT NOT NULL,
    status_date TEXT NOT NULL,
    reason      TEXT,
    batch_id    INTEGER REFERENCES import_batch(batch_id),
    created_at  TEXT NOT NULL
);

CREATE TABLE premium_payment (
    id              INTEGER PRIMARY KEY,
    contract_id     INTEGER NOT NULL REFERENCES contract(contract_id),
    period_ym       TEXT NOT NULL,
    installment_no  INTEGER NOT NULL,
    amount          NUMERIC NOT NULL,
    paid_date       TEXT,
    batch_id        INTEGER REFERENCES import_batch(batch_id),
    UNIQUE (contract_id, installment_no, batch_id)
);

CREATE TABLE persistency (
    id          INTEGER PRIMARY KEY,
    agent_id    INTEGER NOT NULL REFERENCES agent(agent_id),
    insurer_id  INTEGER NOT NULL REFERENCES insurer(insurer_id),  -- 원수사별 (전체·생보·손보는 합산해 계산)
    period_ym   TEXT NOT NULL,
    kind        TEXT NOT NULL,                    -- p13 / p25
    numerator   INTEGER NOT NULL,                 -- 유지 건수
    denominator INTEGER NOT NULL,                 -- 대상 건수
    rate        NUMERIC NOT NULL,
    batch_id    INTEGER REFERENCES import_batch(batch_id),
    UNIQUE (agent_id, insurer_id, period_ym, kind)
);

-- ---------- Core: 보험사 정산서 ----------
CREATE TABLE commission_statement_line (
    line_id             INTEGER PRIMARY KEY,
    batch_id            INTEGER NOT NULL REFERENCES import_batch(batch_id),
    insurer_id          INTEGER NOT NULL REFERENCES insurer(insurer_id),
    period_ym           TEXT NOT NULL,
    policy_no           TEXT NOT NULL,
    insurer_agent_code  TEXT,
    agent_id            INTEGER REFERENCES agent(agent_id),   -- 매핑 결과
    product_code        TEXT,
    commission_type     TEXT NOT NULL,            -- initial / renewal / persistency / promo / clawback / other
    installment_no      INTEGER,
    basis_amount        NUMERIC,
    rate                NUMERIC,
    amount              NUMERIC NOT NULL,         -- 환수는 음수
    pay_date            TEXT,
    raw_ref             TEXT                      -- 시트/행
);

-- ---------- 계산 ----------
CREATE TABLE calc_run (
    run_id          INTEGER PRIMARY KEY,
    run_type        TEXT NOT NULL,                -- commission / incentive / reconciliation
    period_ym       TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    run_by          TEXT,
    rule_snapshot   TEXT,                         -- 사용한 규칙 ID 목록 JSON
    status          TEXT NOT NULL DEFAULT 'running', -- running / done / failed / superseded
    note            TEXT
);

CREATE TABLE commission_calc (
    calc_id         INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES calc_run(run_id),
    period_ym       TEXT NOT NULL,
    contract_id     INTEGER NOT NULL REFERENCES contract(contract_id),
    agent_id        INTEGER REFERENCES agent(agent_id),
    commission_type TEXT NOT NULL,
    installment_no  INTEGER,
    basis_amount    NUMERIC NOT NULL,
    rate            NUMERIC NOT NULL,
    amount          NUMERIC NOT NULL,
    rule_id         INTEGER REFERENCES commission_rate_rule(rule_id),
    clawback_rule_id INTEGER REFERENCES clawback_rule(rule_id)
);

CREATE TABLE incentive_result (
    result_id           INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES calc_run(run_id),
    period_ym           TEXT NOT NULL,
    agent_id            INTEGER NOT NULL REFERENCES agent(agent_id),
    org_id              INTEGER REFERENCES org_unit(org_id),   -- 기준일 소속
    rule_id             INTEGER NOT NULL REFERENCES incentive_rule(rule_id),
    metric_value        NUMERIC NOT NULL,
    qualified           INTEGER NOT NULL,         -- 1 / 0
    disqualify_reason   TEXT,
    tier_no             INTEGER,
    reward_amount       NUMERIC NOT NULL DEFAULT 0,
    detail_json         TEXT                      -- 대상 계약 ID 목록, 조건별 측정값
);

CREATE TABLE incentive_clawback (
    id              INTEGER PRIMARY KEY,
    result_id       INTEGER NOT NULL REFERENCES incentive_result(result_id),
    contract_id     INTEGER NOT NULL REFERENCES contract(contract_id),
    period_ym       TEXT NOT NULL,                -- 환수 귀속 월
    amount          NUMERIC NOT NULL,
    reason          TEXT
);

-- ---------- 점검·마감 ----------
CREATE TABLE reconciliation_diff (
    diff_id             INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES calc_run(run_id),
    period_ym           TEXT NOT NULL,
    insurer_id          INTEGER NOT NULL REFERENCES insurer(insurer_id),
    policy_no           TEXT NOT NULL,
    commission_type     TEXT,
    installment_no      INTEGER,
    statement_line_id   INTEGER REFERENCES commission_statement_line(line_id),
    calc_id             INTEGER REFERENCES commission_calc(calc_id),
    statement_amount    NUMERIC,
    internal_amount     NUMERIC,
    diff_amount         NUMERIC NOT NULL,
    diff_type           TEXT NOT NULL,            -- rate / conversion / status_mismatch / missing_contract / insurer_only / internal_only / clawback / rounding
    status              TEXT NOT NULL DEFAULT 'open', -- open / confirmed / adjusted / ignored
    assignee            TEXT,
    note                TEXT,
    resolved_at         TEXT
);

CREATE TABLE adjustment (
    adj_id              INTEGER PRIMARY KEY,
    period_ym           TEXT NOT NULL,            -- 반영 월
    adjusts_period_ym   TEXT,                     -- 원래 월
    agent_id            INTEGER REFERENCES agent(agent_id),
    contract_id         INTEGER REFERENCES contract(contract_id),
    diff_id             INTEGER REFERENCES reconciliation_diff(diff_id),
    adj_type            TEXT NOT NULL,            -- commission / incentive / clawback / other
    amount              NUMERIC NOT NULL,
    reason              TEXT NOT NULL,
    evidence_path       TEXT,                     -- 공문/메일 파일 경로
    created_by          TEXT NOT NULL,
    approved_by         TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE close_period (
    period_ym   TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'open',     -- open / closed
    closed_at   TEXT,
    closed_by   TEXT,
    snapshot_path TEXT,
    snapshot_hash TEXT
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,                    -- master_update / close / adjustment / run / login ...
    target      TEXT,
    before_json TEXT,
    after_json  TEXT
);

-- ---------- 기간별 업적 집계 ----------
CREATE TABLE agent_performance_summary (
    id                  INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES calc_run(run_id),
    agent_id            INTEGER NOT NULL REFERENCES agent(agent_id),
    org_id              INTEGER REFERENCES org_unit(org_id),   -- 기간 기준일 소속
    period_type         TEXT NOT NULL,            -- month / quarter / half / year
    period_key          TEXT NOT NULL,            -- 202610 / 2026Q4 / 2026H2 / 2026
    applied_count       INTEGER NOT NULL DEFAULT 0, -- 청약 (예정 업적)
    new_count           INTEGER NOT NULL DEFAULT 0, -- 성립
    withdrawn_count     INTEGER NOT NULL DEFAULT 0, -- 철회
    net_count           INTEGER NOT NULL DEFAULT 0,
    monthly_premium     NUMERIC NOT NULL DEFAULT 0,
    converted_premium   NUMERIC NOT NULL DEFAULT 0,
    commission_amount   NUMERIC NOT NULL DEFAULT 0,
    clawback_amount     NUMERIC NOT NULL DEFAULT 0,
    UNIQUE (run_id, agent_id, period_type, period_key)
);

-- ---------- 외부 지표 (불완전판매, 민원, 교육 이수 등) ----------
CREATE TABLE agent_external_metric (
    id          INTEGER PRIMARY KEY,
    agent_id    INTEGER NOT NULL REFERENCES agent(agent_id),
    period_ym   TEXT NOT NULL,
    metric      TEXT NOT NULL,                    -- mis_selling_count / complaint_count / training_completed / ...
    value       NUMERIC NOT NULL,
    source      TEXT,                             -- 보험사 / 협회 / 내부
    batch_id    INTEGER REFERENCES import_batch(batch_id),
    UNIQUE (agent_id, period_ym, metric)
);

-- ---------- 커리어패스 ----------
CREATE TABLE career_level (
    level_id    INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,             -- FC / SFC / SM / BM / DH
    name        TEXT NOT NULL,
    rank_order  INTEGER NOT NULL,                 -- 낮을수록 하위
    description TEXT
);

CREATE TABLE career_promotion_criteria (
    id              INTEGER PRIMARY KEY,
    from_level_id   INTEGER NOT NULL REFERENCES career_level(level_id),
    to_level_id     INTEGER NOT NULL REFERENCES career_level(level_id),
    metric          TEXT NOT NULL,                -- converted_premium / new_count / persistency_13 / tenure_months / team_size / ...
    period_type     TEXT NOT NULL,                -- month / quarter / half / year / rolling_12m
    operator        TEXT NOT NULL,                -- >= / <= / =
    value           NUMERIC NOT NULL,
    effective_from  TEXT NOT NULL,
    effective_to    TEXT
);

CREATE TABLE agent_career_history (
    id              INTEGER PRIMARY KEY,
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    level_id        INTEGER NOT NULL REFERENCES career_level(level_id),
    effective_from  TEXT NOT NULL,                -- YYYYMM
    effective_to    TEXT,
    reason          TEXT,                         -- promotion / demotion / initial
    approved_by     TEXT
);

CREATE TABLE agent_goal (
    id              INTEGER PRIMARY KEY,
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    period_type     TEXT NOT NULL,                -- month / quarter / half / year
    period_key      TEXT NOT NULL,
    metric          TEXT NOT NULL,
    target_value    NUMERIC NOT NULL,
    set_by          TEXT NOT NULL,                -- self / manager / hq
    set_at          TEXT NOT NULL,
    note            TEXT,
    UNIQUE (agent_id, period_type, period_key, metric)
);

-- ---------- 인증 (MDRT, 우수인증설계사 등) ----------
CREATE TABLE certification_program (
    program_id      INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,         -- MDRT / COT / TOT / EXCELLENT_FC_LIFE / EXCELLENT_FC_NONLIFE / INTERNAL_*
    name            TEXT NOT NULL,
    issuer          TEXT,                         -- MDRT / 생명보험협회 / 손해보험협회 / 원수사 / 자사
    category        TEXT NOT NULL DEFAULT 'association_cert', -- mdrt / association_cert(협회 인증) / insurer_cert(원수사 인증) / executive_fc(설계사 임원제도) / executive_manager(관리자 임원제도) / award(영업우수·명예의전당)
    target          TEXT NOT NULL DEFAULT 'agent', -- agent(설계사) / manager(관리자)
    target_role     TEXT,                         -- 관리자 대상일 때 branch_head(지점장) / hq_head(본부장)
    sector          TEXT NOT NULL DEFAULT 'all',  -- all / life / nonlife (지표 집계 범위)
    insurer_id      INTEGER REFERENCES insurer(insurer_id),   -- 원수사 인증이면 해당 원수사
    sort_order      INTEGER NOT NULL DEFAULT 0,
    basis_period    TEXT NOT NULL DEFAULT 'calendar_year', -- calendar_year / rolling_12m
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE certification_criteria (
    id              INTEGER PRIMARY KEY,
    program_id      INTEGER NOT NULL REFERENCES certification_program(program_id),
    year            INTEGER NOT NULL,             -- 매년 공식 공고 값 입력
    metric          TEXT NOT NULL,                -- annual_commission / annual_first_premium / annual_recognized_premium / persistency_13 / persistency_25 / tenure_months / mis_selling_count / rank_recognized_premium / award_count / org_recognized_premium / org_persistency_13 / org_headcount / org_active_ratio
    metric_param    TEXT,                         -- award_count 의 대상 프로그램 코드 등
    operator        TEXT NOT NULL,
    value           NUMERIC NOT NULL,
    criteria_group  TEXT NOT NULL DEFAULT 'all',  -- 그룹 내 AND, 그룹 간 OR (예: 수수료 기준 OR 보험료 기준)
    evidence_path   TEXT,                         -- 공고문 경로
    note            TEXT
);

CREATE TABLE agent_certification_status (
    id                  INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES calc_run(run_id),
    agent_id            INTEGER NOT NULL REFERENCES agent(agent_id),
    org_id              INTEGER REFERENCES org_unit(org_id),   -- 관리자 대상이면 평가 조직
    program_id          INTEGER NOT NULL REFERENCES certification_program(program_id),
    year                INTEGER NOT NULL,
    as_of_ym            TEXT NOT NULL,
    metric_json         TEXT NOT NULL,            -- 지표별 현재값 / 기준값 / 충족 여부
    progress_rate       NUMERIC NOT NULL,         -- 0.0 ~ 1.0 (가장 가까운 그룹 기준)
    achieved            INTEGER NOT NULL DEFAULT 0,
    projected_achieved  INTEGER,                  -- 현재 추세 유지 시 연말 달성 예상
    projected_ym        TEXT,                     -- 달성 예상 월
    UNIQUE (run_id, agent_id, program_id, year)
);

CREATE TABLE agent_certification_award (
    id              INTEGER PRIMARY KEY,
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    program_id      INTEGER NOT NULL REFERENCES certification_program(program_id),
    year            INTEGER NOT NULL,
    awarded_at      TEXT,
    certificate_no  TEXT,
    evidence_path   TEXT,
    UNIQUE (agent_id, program_id, year)
);

-- ---------- 랭킹 (전체 설계사 순위, 전월분 공유) ----------
CREATE TABLE ranking_snapshot (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES calc_run(run_id),
    period_type     TEXT NOT NULL,                -- month / year(누적)
    period_key      TEXT NOT NULL,
    metric          TEXT NOT NULL,                -- recognized_premium / net_count
    scope           TEXT NOT NULL DEFAULT 'all',  -- all / eligible(시상 대상 구분만)
    rank_no         INTEGER NOT NULL,
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    org_id          INTEGER REFERENCES org_unit(org_id),
    value           NUMERIC NOT NULL,
    UNIQUE (run_id, period_type, period_key, metric, scope, agent_id)
);

-- ---------- 인덱스 ----------
CREATE INDEX idx_contract_agent_ym ON contract(agent_id, perf_ym);
CREATE INDEX idx_contract_status ON contract(status);
CREATE INDEX idx_stmt_match ON commission_statement_line(insurer_id, period_ym, policy_no, commission_type, installment_no);
CREATE INDEX idx_calc_match ON commission_calc(period_ym, contract_id, commission_type, installment_no);
CREATE INDEX idx_diff_status ON reconciliation_diff(period_ym, status);
CREATE INDEX idx_rate_rule_lookup ON commission_rate_rule(insurer_id, commission_type, effective_from);
CREATE INDEX idx_agent_code_lookup ON agent_insurer_code(insurer_id, insurer_agent_code);
CREATE INDEX idx_contract_app ON contract(insurer_id, application_no);
CREATE INDEX idx_perf_summary ON agent_performance_summary(period_type, period_key, agent_id);
CREATE INDEX idx_cert_status ON agent_certification_status(program_id, year, agent_id);
CREATE INDEX idx_persistency ON persistency(agent_id, period_ym, kind);
CREATE INDEX idx_ranking ON ranking_snapshot(period_type, period_key, metric, scope, rank_no);

-- ---------- Mart 뷰 ----------
-- 설계사별 월 수수료 합계 (최신 완료 run 기준)
CREATE VIEW v_agent_monthly_commission AS
SELECT c.period_ym, c.agent_id, a.internal_code, a.name,
       SUM(CASE WHEN c.commission_type <> 'clawback' THEN c.amount ELSE 0 END) AS paid,
       SUM(CASE WHEN c.commission_type =  'clawback' THEN c.amount ELSE 0 END) AS clawback,
       SUM(c.amount) AS net
FROM commission_calc c
JOIN calc_run r ON r.run_id = c.run_id AND r.status = 'done'
JOIN agent a ON a.agent_id = c.agent_id
GROUP BY c.period_ym, c.agent_id, a.internal_code, a.name;

-- 보험사별 월 대사 요약
CREATE VIEW v_reconciliation_summary AS
SELECT d.period_ym, i.code AS insurer_code,
       COUNT(*) AS diff_count,
       SUM(CASE WHEN d.status = 'open' THEN 1 ELSE 0 END) AS open_count,
       SUM(CASE WHEN d.status = 'open' THEN ABS(d.diff_amount) ELSE 0 END) AS open_amount
FROM reconciliation_diff d
JOIN insurer i ON i.insurer_id = d.insurer_id
GROUP BY d.period_ym, i.code;

-- 조직별 기간 실적 (최신 완료 run 기준)
CREATE VIEW v_org_performance AS
SELECT s.period_type, s.period_key, s.org_id, o.name AS org_name,
       SUM(s.applied_count) AS applied_count, SUM(s.new_count) AS new_count,
       SUM(s.withdrawn_count) AS withdrawn_count, SUM(s.net_count) AS net_count,
       SUM(s.converted_premium) AS converted_premium, SUM(s.commission_amount) AS commission_amount
FROM agent_performance_summary s
JOIN calc_run r ON r.run_id = s.run_id AND r.status = 'done'
LEFT JOIN org_unit o ON o.org_id = s.org_id
GROUP BY s.period_type, s.period_key, s.org_id, o.name;

-- 업적 추출 데이터: 사번 / 성명 / 원수사 / 상품 / 구분 / 보험료 / 인정보험료 / 건수
CREATE VIEW v_contract_extract AS
SELECT c.contract_id, c.perf_ym AS 귀속월, a.internal_code AS 사번, a.name AS 성명, i.name AS 원수사, i.sector AS 업권,
       COALESCE(p.product_name, c.product_code_raw) AS 상품, COALESCE(pc.name, '미분류') AS 구분, COALESCE(pc.incentive_eligible, 0) AS 시상대상구분,
       c.premium_monthly AS 보험료, c.converted_premium AS 인정보험료, c.policy_count AS 건수,
       c.status AS 상태, c.application_no AS 청약번호, c.policy_no AS 증권번호
FROM contract c
JOIN agent a ON a.agent_id = c.agent_id
JOIN insurer i ON i.insurer_id = c.insurer_id
LEFT JOIN product p ON p.product_id = c.product_id
LEFT JOIN product_category pc ON pc.code = p.product_category;
