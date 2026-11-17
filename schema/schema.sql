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

-- ---------------------------------------------------------------------------
-- 시상안 원장: 보험사별 시상안(엑셀)을 원문 그대로 한 줄씩 보존하고 해석 결과를 곁에 둔다.
-- 해석은 초안이며, 승인된 줄만 commission_rate_rule(promo) / incentive_rule 로 넘긴다.
-- ---------------------------------------------------------------------------
CREATE TABLE incentive_plan_doc (
    plan_doc_id     INTEGER PRIMARY KEY,
    period_ym       TEXT NOT NULL,
    title           TEXT,
    file_name       TEXT NOT NULL,
    file_hash       TEXT NOT NULL UNIQUE,
    source_kind     TEXT NOT NULL,                -- xlsx / drive_extract
    received_at     TEXT NOT NULL,
    loaded_at       TEXT,
    loaded_by       TEXT,
    row_count       INTEGER,
    skipped_sheets  TEXT,                         -- 적재하지 않은 시트와 사유(JSON)
    status          TEXT NOT NULL DEFAULT 'loaded', -- loaded / reviewed / superseded
    note            TEXT
);

CREATE TABLE incentive_plan_line (
    line_id         INTEGER PRIMARY KEY,
    plan_doc_id     INTEGER NOT NULL REFERENCES incentive_plan_doc(plan_doc_id),
    sheet           TEXT NOT NULL,
    row_no          INTEGER NOT NULL,
    sector          TEXT,                         -- life / nonlife
    insurer_raw     TEXT,                         -- 원문 (병합 셀이면 빈칸)
    insurer_name    TEXT NOT NULL,                -- 병합 셀을 채운 보험사명
    insurer_id      INTEGER REFERENCES insurer(insurer_id),   -- 마스터와 이름이 맞을 때만
    recipient_raw   TEXT,
    recipient_level TEXT NOT NULL,                -- ga / hq(총괄) / branch(지사) / fc / note(비고) / unknown
    category_raw    TEXT,
    period_raw      TEXT,
    period_from     TEXT,                         -- YYYY-MM-DD (일 단위)
    period_to       TEXT,
    criteria_raw    TEXT,
    reward_raw      TEXT,
    pay_timing      TEXT NOT NULL DEFAULT 'unspecified', -- next_month(익월) / inst13(13회차) / next_month+inst13 / year1 / year2 / mixed / unspecified
    line_kind       TEXT NOT NULL,                -- rate_promo / activity / consecutive / tier / note / unknown
    parse_status    TEXT NOT NULL,                -- auto(자동 해석) / manual(사람 확인 필요) / note(비고)
    parse_note      TEXT,
    review_status   TEXT NOT NULL DEFAULT 'draft', -- draft / approved / rejected
    reviewed_by     TEXT,
    reviewed_at     TEXT,
    linked_rate_rule_id      INTEGER REFERENCES commission_rate_rule(rule_id),
    linked_incentive_rule_id INTEGER REFERENCES incentive_rule(rule_id)
);
CREATE INDEX idx_plan_line_doc ON incentive_plan_line(plan_doc_id, insurer_name, recipient_level);

-- 정률 시책의 지급 조각: 기준 보험료 × 지급률, 지급 시점
CREATE TABLE incentive_plan_component (
    component_id    INTEGER PRIMARY KEY,
    line_id         INTEGER NOT NULL REFERENCES incentive_plan_line(line_id),
    seq             INTEGER NOT NULL,
    option_label    TEXT,                         -- 조건이 여럿일 때의 번호(1, 2, ①)
    basis           TEXT NOT NULL,                -- initial_premium(월초P) / monthly_premium / paid_premium / base_premium / premium / unspecified
    rate            NUMERIC NOT NULL,
    pay_timing      TEXT NOT NULL,                -- next_month / inst13 / next_month+inst13 / year1 / year2 / unspecified
    pay_condition   TEXT
);

-- ---------------------------------------------------------------------------
-- 계정: 로그인 단위. 역할은 여기 적지 않고 조직장 지정(org_manager)과 is_main_admin 에서 그때그때 정한다.
-- ---------------------------------------------------------------------------
CREATE TABLE user_account (
    user_id         INTEGER PRIMARY KEY,
    login_id        TEXT NOT NULL UNIQUE,         -- 설계사는 사번
    password_hash   TEXT NOT NULL,                -- pbkdf2$반복수$salt$hash
    agent_id        INTEGER UNIQUE REFERENCES agent(agent_id),   -- 메인 관리자 전용 계정이면 NULL
    display_name    TEXT NOT NULL,
    is_main_admin   INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active', -- active / suspended
    must_change_password INTEGER NOT NULL DEFAULT 1,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    last_login_at   TEXT,
    created_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 프로모션: 설계사에게 게시하는 시상. 유형이 달라도 기간 창, 구간표, 결합 규칙, 보상, 조건의 조합으로 담는다.
-- 달성 현황은 계약 원장에서 그때그때 계산한다(예상). 확정은 보험사 정산을 따른다.
-- ---------------------------------------------------------------------------
CREATE TABLE promotion (
    promo_id        INTEGER PRIMARY KEY,
    period_ym       TEXT NOT NULL,                -- 귀속 월 (목록과 보관의 기준)
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,                -- rate(정률 시책) / weekly(주차) / linked(연계) / monthly(월 구간) / travel(여행) / activity(가동) / membership
    recipient_level TEXT NOT NULL DEFAULT 'fc',   -- fc 만 설계사에게 보인다. ga / hq / branch 는 메인 관리자만
    insurer_id      INTEGER REFERENCES insurer(insurer_id),   -- NULL 이면 전 보험사 또는 마스터에 없는 보험사
    insurer_name    TEXT,                         -- 표시용
    sector          TEXT,                         -- life / nonlife / NULL(전체): 대상 계약의 업권
    categories      TEXT,                         -- 대상 구분 코드를 쉼표로. NULL 이면 전 구분
    target_note     TEXT,                         -- 대상 상품의 원문
    metric          TEXT NOT NULL DEFAULT 'premium_monthly', -- premium_monthly / converted_premium / policy_count
    date_basis      TEXT NOT NULL DEFAULT 'contract_date',   -- contract_date / application_date
    per_contract_cap NUMERIC,                     -- 건당 인정 한도
    combine_rule    TEXT NOT NULL DEFAULT 'single', -- single(창 하나) / all(창마다 달성, 낮은 구간 적용) / sum(창을 합산)
    pay_timing      TEXT,                         -- next_month / inst13 / next_month+inst13 / ...
    caution         TEXT,                         -- 유의사항, 지급 조건의 원문
    source_line_id  INTEGER REFERENCES incentive_plan_line(line_id),
    status          TEXT NOT NULL DEFAULT 'draft', -- draft / published / withdrawn
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    published_by    TEXT,
    published_at    TEXT
);
CREATE INDEX idx_promotion_ym ON promotion(period_ym, status);

CREATE TABLE promotion_window (
    window_id       INTEGER PRIMARY KEY,
    promo_id        INTEGER NOT NULL REFERENCES promotion(promo_id),
    seq             INTEGER NOT NULL,
    label           TEXT NOT NULL,                -- 1주차, 7월 4주 ...
    date_from       TEXT NOT NULL,                -- YYYY-MM-DD
    date_to         TEXT NOT NULL
);

CREATE TABLE promotion_tier (
    tier_id         INTEGER PRIMARY KEY,
    promo_id        INTEGER NOT NULL REFERENCES promotion(promo_id),
    tier_no         INTEGER NOT NULL,             -- 1 이 가장 낮은 구간
    min_value       NUMERIC NOT NULL,             -- 이 값 이상이면 이 구간
    reward_kind     TEXT NOT NULL,                -- fixed(정액) / rate(실적의 비율) / travel(여행) / goods(물품)
    reward_value    NUMERIC,                      -- 정액이면 원, 비율이면 0.5 = 50%
    reward_label    TEXT                          -- 여행지, 물품 이름, 현금 대체 조건
);

-- 정률 시책의 지급 조각. option_label 이 있으면 조건별 지급률이라 예상액을 내지 않는다.
CREATE TABLE promotion_rate (
    rate_id         INTEGER PRIMARY KEY,
    promo_id        INTEGER NOT NULL REFERENCES promotion(promo_id),
    seq             INTEGER NOT NULL,
    option_label    TEXT,
    basis           TEXT NOT NULL,
    rate            NUMERIC NOT NULL,
    pay_timing      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 조직 부문 시상: 조직이 스스로 정해 매달 주는 시상. 부문 하나는 지표, 조건, 범위, 선발 인원의 조합이다.
-- 순위는 계약 원장에서 그때그때 계산하고, 메인 관리자가 확정하면 그 달의 수상자를 따로 남긴다.
-- ---------------------------------------------------------------------------
CREATE TABLE org_award_division (
    division_id     INTEGER PRIMARY KEY,
    org_id          INTEGER NOT NULL REFERENCES org_unit(org_id),   -- 부문을 연 조직. 그 조직과 아래의 설계사가 대상이다
    name            TEXT NOT NULL,
    metric          TEXT NOT NULL,                -- converted_premium / premium_monthly / policy_count
    sector          TEXT,                         -- life / nonlife / NULL(전체)
    categories      TEXT,                         -- 대상 구분 코드를 쉼표로. NULL 이면 전 구분
    top_n           INTEGER NOT NULL DEFAULT 3,   -- 선발 인원
    min_value       NUMERIC,                      -- 이 값에 못 미치면 순위에 넣지 않는다
    effective_from  TEXT NOT NULL,                -- 적용 시작 월 YYYYMM
    effective_to    TEXT,
    note            TEXT,
    status          TEXT NOT NULL DEFAULT 'active', -- active / inactive
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_by      TEXT,
    updated_at      TEXT
);

CREATE TABLE org_award_reward (
    id              INTEGER PRIMARY KEY,
    division_id     INTEGER NOT NULL REFERENCES org_award_division(division_id),
    rank_no         INTEGER NOT NULL,
    reward_label    TEXT NOT NULL,
    UNIQUE (division_id, rank_no)
);

CREATE TABLE org_award_result (
    result_id       INTEGER PRIMARY KEY,
    division_id     INTEGER NOT NULL REFERENCES org_award_division(division_id),
    period_ym       TEXT NOT NULL,
    rank_no         INTEGER NOT NULL,
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    value           NUMERIC NOT NULL,
    reward_label    TEXT,
    confirmed_by    TEXT NOT NULL,
    confirmed_at    TEXT NOT NULL,
    UNIQUE (division_id, period_ym, agent_id)
);

-- ---------------------------------------------------------------------------
-- 실제 계약현황(이터널, 56열) 적재에 필요한 칸. 기존 CREATE 문은 그대로 두고 덧붙이기만 한다.
-- 매핑은 config/mapping/eternal_contract.yaml, 적재기는 app/loader/eternal_contract.py 다.
-- ---------------------------------------------------------------------------
-- 원수사: 적재기가 마스터에 없는 이름을 만나 임시로 만든 줄은 origin='loader' 로 표시한다. 확인 뒤 'master' 로 바꾼다.
ALTER TABLE insurer ADD COLUMN origin TEXT NOT NULL DEFAULT 'master';

-- 조직 경로. 센터와 팀은 이름이 겹치므로 이름 하나가 아니라 위에서부터의 경로로 식별한다.
ALTER TABLE contract ADD COLUMN org_head_office TEXT;      -- 본사
ALTER TABLE contract ADD COLUMN org_hq TEXT;               -- 본부
ALTER TABLE contract ADD COLUMN org_branch TEXT;           -- 지점
ALTER TABLE contract ADD COLUMN org_center TEXT;           -- 센터. 빈 행이 있다
ALTER TABLE contract ADD COLUMN org_team TEXT;             -- 팀. 센터가 있을 때만 있다
ALTER TABLE contract ADD COLUMN org_path TEXT;             -- 빈 칸을 뺀 경로를 ' > ' 로 이은 글. 묶기와 표시용
-- 계약의 분류
ALTER TABLE contract ADD COLUMN sector TEXT;               -- 계약종류. 생보 life / 손보 nonlife
ALTER TABLE contract ADD COLUMN product_type_raw TEXT;     -- 상품종류 원문
ALTER TABLE contract ADD COLUMN product_category TEXT;     -- 분류표로 옮긴 일곱 구분 코드. 표에 없으면 NULL
ALTER TABLE contract ADD COLUMN insurer_product_code TEXT; -- 상품코드
ALTER TABLE contract ADD COLUMN ca_code TEXT;              -- CA코드
ALTER TABLE contract ADD COLUMN payment_term_name TEXT;    -- 납입기간명 원문
ALTER TABLE contract ADD COLUMN coverage_start TEXT;       -- 시작일자
ALTER TABLE contract ADD COLUMN coverage_end TEXT;         -- 종료일자
-- 상태와 수금
ALTER TABLE contract ADD COLUMN status_raw TEXT;           -- 계약상태 원문
ALTER TABLE contract ADD COLUMN status_changed_date TEXT;  -- 최종상태변경일 원값. 다음 파일과 비교해 이력을 쌓는다
ALTER TABLE contract ADD COLUMN paid_installments INTEGER; -- 납입회차
ALTER TABLE contract ADD COLUMN last_paid_ym TEXT;         -- 최종납입년월 YYYYMM
ALTER TABLE contract ADD COLUMN last_collected_date TEXT;  -- 최종수금일
ALTER TABLE contract ADD COLUMN receipt_date TEXT;         -- 영수일자
ALTER TABLE contract ADD COLUMN auto_debit_day TEXT;       -- 자동이체일 원문
ALTER TABLE contract ADD COLUMN collected_premium NUMERIC; -- 수금보험료
-- 사람. 모집과 수금이 다를 수 있다. 계약자와 피보험자 이름은 D-05 제안대로 저장하고 화면 목록에서 가린다
ALTER TABLE contract ADD COLUMN collector_code TEXT;       -- 수금사원번호
ALTER TABLE contract ADD COLUMN collector_agent_id INTEGER REFERENCES agent(agent_id);
ALTER TABLE contract ADD COLUMN insured_name TEXT;         -- 피보험자
-- 성적. 1차년에서 4차년 성적은 뜻이 확인될 때까지 보관만 하고 계산에 쓰지 않는다 (D-03)
ALTER TABLE contract ADD COLUMN perf_y1 NUMERIC;
ALTER TABLE contract ADD COLUMN perf_y2 NUMERIC;
ALTER TABLE contract ADD COLUMN perf_y3 NUMERIC;
ALTER TABLE contract ADD COLUMN perf_y4 NUMERIC;
ALTER TABLE contract ADD COLUMN manual_perf NUMERIC;       -- 수기성적. 보관만 한다
-- 준수. Y 는 1, N 은 0, 빈 칸은 NULL
ALTER TABLE contract ADD COLUMN self_family INTEGER;       -- 본인·가족계약
ALTER TABLE contract ADD COLUMN external_transfer INTEGER; -- 외부이관여부
ALTER TABLE contract ADD COLUMN excel_registered INTEGER;  -- 엑셀등록여부
ALTER TABLE contract ADD COLUMN confirm_personal INTEGER;  -- 확인서징구(개인)
ALTER TABLE contract ADD COLUMN confirm_compare INTEGER;   -- 확인서징구(비교)
ALTER TABLE contract ADD COLUMN confirm_fcpa INTEGER;      -- 확인서징구(금소법)
ALTER TABLE contract ADD COLUMN confirm_whole_life INTEGER; -- 확인서징구(종신)
ALTER TABLE contract ADD COLUMN mobile_application TEXT;   -- 모바일청약 원문
-- 적재 추적
ALTER TABLE contract ADD COLUMN last_batch_id INTEGER REFERENCES import_batch(batch_id); -- 마지막으로 이 줄을 실어 온 배치
ALTER TABLE contract ADD COLUMN source_row_no INTEGER;     -- 그 배치의 행 번호

CREATE INDEX idx_contract_org ON contract(org_hq, org_branch, org_center, org_team);
CREATE INDEX idx_contract_last_batch ON contract(last_batch_id);

-- ---------------------------------------------------------------------------
-- 조직 계층 여섯 단계 (D-01): GA, 지사·본부, 지점, 센터, 팀, 설계사. 기존 CREATE 문은 그대로 두고 덧붙이기만 한다.
-- 계층의 수와 이름, 조직장 역할은 config/org_levels.yaml 이 원본이다. 동기화는 app/org/sync.py 다.
-- org_unit.level 값: ga(GA, 하나뿐인 뿌리) / hq(지사·본부) / branch(지점) / center(센터) / team(팀)
-- org_manager.role 값: hq_head / branch_head / center_head(센터장) / team_head(팀장)
-- ---------------------------------------------------------------------------
-- 조직은 이름이 아니라 위에서부터의 경로로 식별한다. 센터와 팀은 사람 이름으로 붙어 이름이 겹친다.
-- 뿌리는 'ga', 그 아래는 'hq:이름 > branch:이름 > center:이름' 처럼 계층 코드를 붙여 잇는다. 중간 계층이 비어도 된다.
ALTER TABLE org_unit ADD COLUMN path_key TEXT;
CREATE UNIQUE INDEX idx_org_unit_path ON org_unit(path_key);

-- 조직장 지정의 출처. 사원현황 동기화는 자기가 만든 지정(staff)만 닫고, 화면에서 지정한 것(manual)은 건드리지 않는다.
ALTER TABLE org_manager ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';   -- manual / staff
ALTER TABLE org_manager ADD COLUMN created_by TEXT;
ALTER TABLE org_manager ADD COLUMN created_at TEXT;

-- 기간제 대리. 조직장이 자리를 비우면 메인 관리자가 날짜로 기간을 정해 지정한다. 그 조직과 아래를 기간 동안만 본다.
-- 계산 엔진이 org_manager.role 로 관리자 제도를 따지므로 대리는 그 표에 넣지 않는다.
CREATE TABLE org_deputy (
    id          INTEGER PRIMARY KEY,
    org_id      INTEGER NOT NULL REFERENCES org_unit(org_id),
    agent_id    INTEGER NOT NULL REFERENCES agent(agent_id),
    date_from   TEXT NOT NULL,                    -- YYYY-MM-DD
    date_to     TEXT NOT NULL,                    -- YYYY-MM-DD, 그날까지
    reason      TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    ended_at    TEXT,                             -- 기간 전에 끝낸 때
    ended_by    TEXT
);
CREATE INDEX idx_org_deputy_agent ON org_deputy(agent_id, date_from, date_to);
CREATE INDEX idx_org_manager_agent ON org_manager(agent_id, effective_from);
CREATE INDEX idx_agent_org_history_agent ON agent_org_history(agent_id, effective_to);
CREATE INDEX idx_agent_org_history_org ON agent_org_history(org_id, effective_from);

-- ---------------------------------------------------------------------------
-- 구분과 업권은 계약에 붙은 값을 먼저 읽는다. 실제 계약현황은 상품 마스터와 잇지 않고 계약에 구분과 업권을 직접 싣기 때문이다.
-- 엔진은 app/engine/classify.py 의 식을 쓴다. 이 뷰도 같은 식이다. 기존 CREATE 문은 그대로 두고 여기서 다시 만든다.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_contract_extract;
CREATE VIEW v_contract_extract AS
SELECT c.contract_id, c.perf_ym AS 귀속월, a.internal_code AS 사번, a.name AS 성명, i.name AS 원수사,
       COALESCE(c.sector, i.sector) AS 업권,
       COALESCE(p.product_name, c.product_code_raw) AS 상품, COALESCE(pc.name, '미분류') AS 구분, COALESCE(pc.incentive_eligible, 0) AS 시상대상구분,
       c.premium_monthly AS 보험료, c.converted_premium AS 인정보험료, c.policy_count AS 건수,
       c.status AS 상태, c.application_no AS 청약번호, c.policy_no AS 증권번호
FROM contract c
JOIN agent a ON a.agent_id = c.agent_id
JOIN insurer i ON i.insurer_id = c.insurer_id
LEFT JOIN product p ON p.product_id = c.product_id
LEFT JOIN product_category pc ON pc.code = COALESCE(c.product_category, p.product_category);

-- 조직과 그 아래 전부의 짝. ancestor_id 의 아래(자기 포함)에 org_id 가 있다. depth 는 몇 단계 아래인가다.
-- 여섯 단계 조직에서 본부와 지점 집계가 센터와 팀까지 내려가게 하려고 둔다. 순환을 막으려고 열 단계에서 멈춘다.
DROP VIEW IF EXISTS v_org_closure;
CREATE VIEW v_org_closure AS
WITH RECURSIVE t(ancestor_id, org_id, depth) AS (
    SELECT org_id, org_id, 0 FROM org_unit
    UNION ALL
    SELECT t.ancestor_id, o.org_id, t.depth + 1 FROM t JOIN org_unit o ON o.parent_id = t.org_id WHERE t.depth < 10
)
SELECT ancestor_id, org_id, depth FROM t;

-- 조직별 기간 실적, 아래 조직 전부 포함. as_of_ym 은 업적 run 의 기준 월이다.
-- 분기, 반기, 연은 run 마다 누적이 다르므로 as_of_ym 으로 하나를 골라 읽는다. 월은 as_of_ym 과 period_key 가 같다.
-- 소속은 run 이 남긴 기간 기준일 소속(agent_performance_summary.org_id)이다. app/engine/performance.py 의 org_rollup 과 같은 값이다.
DROP VIEW IF EXISTS v_org_performance_rollup;
CREATE VIEW v_org_performance_rollup AS
SELECT r.period_ym AS as_of_ym, s.period_type, s.period_key, t.ancestor_id AS org_id, o.name AS org_name, o.level AS org_level,
       COUNT(*) AS agent_count,
       SUM(s.applied_count) AS applied_count, SUM(s.new_count) AS new_count,
       SUM(s.withdrawn_count) AS withdrawn_count, SUM(s.net_count) AS net_count,
       SUM(s.monthly_premium) AS monthly_premium, SUM(s.converted_premium) AS converted_premium,
       SUM(s.commission_amount) AS commission_amount, SUM(s.clawback_amount) AS clawback_amount
FROM agent_performance_summary s
JOIN calc_run r ON r.run_id = s.run_id AND r.status = 'done' AND r.run_type = 'performance'
JOIN v_org_closure t ON t.org_id = s.org_id
JOIN org_unit o ON o.org_id = t.ancestor_id
GROUP BY r.period_ym, s.period_type, s.period_key, t.ancestor_id, o.name, o.level;

-- ---------------------------------------------------------------------------
-- 운영 연결 (app/connect). 연결 상태, 폴더 감시 목록, 설계사와 명함 주소를 잇는 표.
-- 앱이 오래된 DB 를 열 때도 이 묶음만 다시 돌릴 수 있게 모두 IF NOT EXISTS 로 쓴다. app/connect/tables.py 가 이 묶음을 읽는다.
-- 사람 이름, 연락처, 금액, 키는 넣지 않는다.
-- [connect:begin]
-- ---------------------------------------------------------------------------
-- 연결마다 한 줄. 메인 관리자 화면의 마지막 성공 시각과 실패 사유가 여기서 나온다.
CREATE TABLE IF NOT EXISTS connection_state (
    name                 TEXT PRIMARY KEY,            -- upload, gdrive, folder:경로, card_site.status, card_site.admin
    last_status          TEXT,                        -- ok / failed / not_configured
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    last_failure_at      TEXT,
    last_failure_reason  TEXT NOT NULL DEFAULT '',    -- 300자까지. 설정 안 됨 사유도 여기 둔다
    consecutive_failures INTEGER NOT NULL DEFAULT 0,  -- 성공하면 0 으로 돌아간다
    detail_json          TEXT,                        -- 결과 건수 같은 요약
    updated_at           TEXT NOT NULL,
    migrated_from        TEXT                         -- data/state/connections.json 에서 옮긴 줄이면 그 파일 이름
);

-- 폴더 감시가 이미 끝낸 파일. 크기와 바뀐 시각이 같고 끝난 상태면 다시 읽지 않는다.
CREATE TABLE IF NOT EXISTS connection_watch (
    name        TEXT NOT NULL,                        -- 감시 이름. connection_state.name 과 같다
    rel_path    TEXT NOT NULL,                        -- 감시 폴더 아래 상대 경로
    file_size   INTEGER,
    file_mtime  REAL,
    file_hash   TEXT,                                 -- sha256
    status      TEXT,                                 -- loaded / duplicate / failed / no_handler / skipped
    seen_at     TEXT,
    PRIMARY KEY (name, rel_path)
);

-- 설계사와 명함 주소. 메인 관리자가 확인한 것만 들어온다. 이름 대조 제안은 표에 넣지 않고 확인 화면에만 보인다.
-- 끊을 때는 지우지 않고 unlinked_at 을 채운다. 이어진 줄은 주소마다 하나, 설계사마다 하나다.
CREATE TABLE IF NOT EXISTS card_link (
    id               INTEGER PRIMARY KEY,
    agent_id         INTEGER NOT NULL REFERENCES agent(agent_id),
    slug             TEXT NOT NULL,                   -- 명함 주소. 영문 소문자, 숫자, 하이픈 2자에서 30자
    linked_via       TEXT NOT NULL DEFAULT 'manual',  -- manual(직접 입력) / suggest(이름 대조 제안을 확인)
    linked_by        TEXT NOT NULL,                   -- 이은 메인 관리자의 login_id
    linked_at        TEXT NOT NULL,
    last_status      TEXT,                            -- 마지막으로 확인한 상태: pending / approved / rejected / removed / none
    last_live        INTEGER,                         -- 마지막으로 확인했을 때 공개 중이면 1
    last_live_at     TEXT,                            -- 공개본이 승인된 시각. 명함 사이트의 값
    last_checked_at  TEXT,
    unlinked_by      TEXT,
    unlinked_at      TEXT,
    unlink_reason    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_card_link_slug_active ON card_link(slug) WHERE unlinked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_card_link_agent_active ON card_link(agent_id) WHERE unlinked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_card_link_agent ON card_link(agent_id, linked_at);
-- [connect:end]

-- ---------------------------------------------------------------------------
-- 명함 승인 권한. D-44 를 고친 11월 14일 결정: 조직장이 되었다고 저절로 생기지 않고 메인 관리자가 골라서 켠다.
-- 대상은 지금 조직장이거나 대리인 관리자 계정이다. 효력은 켠 시각이 지금 맡은 자리의 가장 늦은 시작보다 앞서지 않을 때만 있다.
-- 조직장에서 빠지거나, 대리 기간이 끝나거나, 다른 조직으로 옮기거나, 자리를 하나 더 맡으면 켜진 줄을 revoked_by='system:left' 로 끄고 새 승인 요청을 거친다.
-- 끌 때는 지우지 않고 revoked_at 을 채운다. 켜진 줄은 계정마다 하나다. app/org/card_grant.py 가 이 묶음을 읽는다.
-- [card_grant:begin]
CREATE TABLE IF NOT EXISTS card_approver (
    id          INTEGER PRIMARY KEY,
    login_id    TEXT NOT NULL,                        -- user_account.login_id. 관리자는 사번
    agent_id    INTEGER REFERENCES agent(agent_id),   -- 켤 때의 계정에 이어진 설계사
    granted_by  TEXT NOT NULL,                        -- 켠 메인 관리자의 login_id
    granted_at  TEXT NOT NULL,
    revoked_by  TEXT,                                 -- 끈 메인 관리자의 login_id
    revoked_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_card_approver_active ON card_approver(login_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_card_approver_login ON card_approver(login_id, granted_at);
-- [card_grant:end]

-- 명함 승인 권한 요청. 11월 14일 대표님 결정: 승인 요청을 만들어 메인 관리자가 승인해야 권한이 켜진다.
-- 조직장이나 대리가 되었는데 효력 있는 권한이 없으면 요청이 저절로 생긴다. 관리자 본인도 요청한다.
-- 예전에 켜 둔 줄은 되살아나지 않는다. 조직장이나 대리에서 빠지면 켜진 줄을 revoked_by='system:left' 로 끈다.
-- 지우지 않고 상태만 바꾼다. 같은 사람의 pending 은 하나다. app/org/card_grant.py 가 이 묶음을 읽는다.
-- [card_request:begin]
CREATE TABLE IF NOT EXISTS card_approver_request (
    id            INTEGER PRIMARY KEY,
    login_id      TEXT NOT NULL,                      -- 대상 계정. 관리자는 사번
    agent_id      INTEGER REFERENCES agent(agent_id), -- 요청 때의 계정에 이어진 설계사
    reason        TEXT NOT NULL CHECK (reason IN ('new_head', 'reappointed', 'self')),
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    requested_at  TEXT NOT NULL,
    requested_by  TEXT NOT NULL,                      -- 본인 login_id, 저절로 생기면 system:appointed
    decided_by    TEXT,                               -- 처리한 메인 관리자의 login_id, 빠져서 거두면 system:left
    decided_at    TEXT,
    grant_id      INTEGER REFERENCES card_approver(id) -- 승인으로 켜진 줄
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_card_request_pending ON card_approver_request(login_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_card_request_login ON card_approver_request(login_id, requested_at);
CREATE INDEX IF NOT EXISTS idx_card_request_status ON card_approver_request(status, requested_at);
-- [card_request:end]

-- ---------------------------------------------------------------------------
-- 교육 (파트 E). 과정, 차수, 수료, 명함 수료 코드 발급 기록. 계산은 app/engine/education.py 다.
-- 수료의 원본은 수료자 명부다. 과정 목록과 명부 양식을 묻는 D-09 가 열려 있어 칸 구성은 샘플 안의 가정이다.
-- 진척도, 기한, 미수료, 기한 임박은 표에 남기지 않고 이 표들에서 그때그때 계산한다. 그래서 언제든 다시 만들 수 있다.
-- 수료 줄은 지우지 않는다. 잘못 들어간 줄과 다시 올린 명부의 앞 배치는 voided_at 을 채워 뺀다.
-- 사람 이름, 연락처, 금액, 수료 코드 원문은 넣지 않는다.
-- 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 모두 IF NOT EXISTS 로 쓴다. app/engine/education.py 의 ensure 가 이 묶음을 읽는다.
-- [education:begin]
-- ---------------------------------------------------------------------------
-- 과정. 대상은 역할, 직급, 위촉일, 과정을 연 조직을 모두 만족하는 사람이다.
CREATE TABLE IF NOT EXISTS edu_course (
    course_id         INTEGER PRIMARY KEY,
    code              TEXT NOT NULL UNIQUE,           -- 앱 안 과정 코드. 명부의 과정 칸과 맞춘다
    name              TEXT NOT NULL,
    kind              TEXT NOT NULL,                  -- newbie(신인) / refresher(보수) / product(상품) / compliance(준법)
    required          INTEGER NOT NULL DEFAULT 0,     -- 1 이면 필수. 진척도는 필수 과정만 센다
    target_role       TEXT NOT NULL DEFAULT 'fc',     -- fc(설계사) / manager(관리자) / all. agent.job_type 과 맞춘다
    target_levels     TEXT,                           -- 직급 코드를 쉼표로 잇는다. career_level.code. 비면 모든 직급
    target_hired_from TEXT,                           -- YYYY-MM-DD. 이날 이후 위촉한 사람만 대상. 비면 위촉일을 따지지 않는다
    owner_org_id      INTEGER REFERENCES org_unit(org_id),  -- 과정을 연 조직. 그 조직과 아래가 대상. 비면 전체
    deadline_rule     TEXT NOT NULL DEFAULT 'none',   -- none / hire_days(위촉 뒤 N일) / fixed(정한 날) / yearly(매년 MM-DD)
    deadline_days     INTEGER,                        -- hire_days 의 N
    deadline_date     TEXT,                           -- fixed 의 YYYY-MM-DD
    deadline_md       TEXT,                           -- yearly 의 MM-DD. 그해 1월 1일부터 12월 31일까지의 수료가 그해 몫이다
    card_course_code  TEXT,                           -- 명함 사이트 EDU_COURSES 의 키. 지금은 ABC 하나다. 비면 수료 코드가 없다
    status            TEXT NOT NULL DEFAULT 'active', -- active / closed. 닫은 과정은 진척도에서 빠지고 수료 기록은 남는다
    note              TEXT,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_by        TEXT,
    updated_at        TEXT
);

-- 차수. 날짜와 장소 또는 온라인. 닫지 않고 status 로 취소를 표시한다.
CREATE TABLE IF NOT EXISTS edu_session (
    session_id  INTEGER PRIMARY KEY,
    course_id   INTEGER NOT NULL REFERENCES edu_course(course_id),
    session_no  INTEGER NOT NULL,                     -- 과정 안의 차수 번호. 1부터
    date_from   TEXT NOT NULL,                        -- YYYY-MM-DD
    date_to     TEXT NOT NULL,                        -- YYYY-MM-DD. 하루짜리면 date_from 과 같다
    online      INTEGER NOT NULL DEFAULT 0,           -- 1 이면 온라인
    place       TEXT,                                 -- 장소. 온라인이면 비워도 된다
    capacity    INTEGER,                              -- 정원. 비면 정하지 않음
    status      TEXT NOT NULL DEFAULT 'planned',      -- planned / held / cancelled
    note        TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (course_id, session_no)
);

-- 수료. 명부 한 줄 또는 관리자의 확인 처리 한 번이 한 줄이다.
-- 같은 수료는 두 번 넣지 않는다. 같은 사람, 같은 과정, 같은 주기(cycle_key)의 살아 있는 줄은 하나다.
-- cycle_key 는 매년 기한인 과정이면 수료한 해 YYYY, 그 밖은 once 다. 넣을 때 엔진이 정한다.
CREATE TABLE IF NOT EXISTS edu_completion (
    completion_id INTEGER PRIMARY KEY,
    agent_id      INTEGER NOT NULL REFERENCES agent(agent_id),
    course_id     INTEGER NOT NULL REFERENCES edu_course(course_id),
    session_id    INTEGER REFERENCES edu_session(session_id),   -- 명부에 차수가 없으면 비운다
    completed_on  TEXT NOT NULL,                      -- 수료일 YYYY-MM-DD
    cycle_key     TEXT NOT NULL,                      -- once / YYYY
    source        TEXT NOT NULL,                      -- roster(수료자 명부) / manual(관리자 확인 처리) / sample(샘플)
    batch_id      INTEGER REFERENCES import_batch(batch_id),    -- 원본 명부 배치. import_batch.data_type 은 edu_roster
    row_no        INTEGER,                            -- 명부의 줄 번호. 같은 배치를 다시 만들 때 쓴다
    recorded_by   TEXT NOT NULL,
    recorded_at   TEXT NOT NULL,
    voided_at     TEXT,                               -- 뺀 때. 지우지 않는다
    voided_by     TEXT,
    void_reason   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edu_completion_live ON edu_completion(agent_id, course_id, cycle_key) WHERE voided_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_edu_completion_course ON edu_completion(course_id, session_id);
CREATE INDEX IF NOT EXISTS idx_edu_completion_batch ON edu_completion(batch_id);

-- 명함 수료 코드 발급 기록. 코드 원문은 저장하지 않고 명함 사이트의 일련번호와 코드의 SHA-256 만 둔다.
-- 명함 사이트는 코드의 SHA-256 앞 20자로 코드를 찾는다. 발급과 내리기는 메인 관리자만 한다(D-44).
-- 사이트에 실제로 보내는 일은 D-28 이 정해지고 CARD_SITE_WRITE 를 켠 뒤에 한다. 그 전에는 requested 에 머문다.
-- 살아 있는 발급(requested, issued)은 사람과 명함 과정마다 하나다. 명함은 과정마다 수료 하나만 보여 주기 때문이다.
CREATE TABLE IF NOT EXISTS edu_card_code (
    issue_id         INTEGER PRIMARY KEY,
    completion_id    INTEGER NOT NULL REFERENCES edu_completion(completion_id),
    agent_id         INTEGER NOT NULL REFERENCES agent(agent_id),
    card_course_code TEXT NOT NULL,                   -- 명함 사이트 EDU_COURSES 의 키
    completed_ym     TEXT NOT NULL,                   -- 명함에 찍히는 수료월 YYYY-MM
    status           TEXT NOT NULL DEFAULT 'requested', -- requested / issued / failed / revoked
    serial           TEXT,                            -- 명함 사이트의 일련번호. 예: ABC2610-002
    code_sha256      TEXT,                            -- 코드 원문(공백 없애고 대문자)의 SHA-256 16진수
    requested_by     TEXT NOT NULL,
    requested_at     TEXT NOT NULL,
    issued_at        TEXT,
    fail_reason      TEXT,
    revoked_by       TEXT,
    revoked_at       TEXT,
    revoke_reason    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edu_card_code_live ON edu_card_code(agent_id, card_course_code) WHERE status IN ('requested', 'issued');
CREATE UNIQUE INDEX IF NOT EXISTS idx_edu_card_code_serial ON edu_card_code(serial) WHERE serial IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_edu_card_code_hash ON edu_card_code(code_sha256) WHERE code_sha256 IS NOT NULL;
-- [education:end]

-- ---------------------------------------------------------------------------
-- 알림함의 읽음 표시 (파트 F). D-10 제안대로 앱 안 알림만 한다.
-- 알림 자체는 표에 남기지 않는다. 알림함을 열 때 프로모션, 달성 현황, 교육, 명함 승인 대기의 원본에서 그때그때 만든다.
-- 이 표에는 누가 어느 알림을 읽었는지만 둔다. note_key 는 종류와 대상 번호와 날짜로 만든 글이고 사람 이름과 금액을 넣지 않는다.
-- 지우지 않는다. 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 IF NOT EXISTS 로 쓴다. app/web/routes/notify.py 의 ensure 가 이 묶음을 읽는다.
-- [notifications:begin]
CREATE TABLE IF NOT EXISTS notification_read (
    user_id    INTEGER NOT NULL REFERENCES user_account(user_id),
    note_key   TEXT NOT NULL,                         -- 예: promo_due:12:2026-10-31, edu_due:3:2026-11-06, card_grant:7
    read_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, note_key)
);
-- [notifications:end]

-- ---------------------------------------------------------------------------
-- 공지와 자료실 (파트 F, /board). 글과 첨부 파일의 정보만 둔다. 파일 자체는 저장소 밖 data/board/<sha256> 에 둔다.
-- 폴더는 APP_BOARD_DIR 로 바꾼다. DB 에는 파일 이름, 크기, 해시, 꼴만 남기고 내용은 넣지 않는다.
-- 글은 메인 관리자만 올리고 고치고 내린다. docs/09 2.4 에 관리자의 공지 올리기가 없어서다.
-- 대상은 조직과 역할을 함께 고른다. 조직이 비면 모든 조직, 역할이 비면 모든 역할이다. 조직은 그 아래까지 포함한다.
-- 시상안 원문은 수령 계층의 몫이 담겨 있을 수 있어 올릴 때 fc_visible 을 고른다(D-18). 0 이면 메인 관리자만 본다.
-- 지우지 않는다. 내리면 withdrawn 으로 두고, 고치면서 뺀 첨부는 removed_at 을 채운다. app/web/board.py 의 ensure 가 이 묶음을 읽는다.
-- [board:begin]
CREATE TABLE IF NOT EXISTS board_post (
    post_id        INTEGER PRIMARY KEY,
    category       TEXT NOT NULL,                     -- notice(공지) / plan(시상안 원문) / lesson(교안) / form(양식)
    title          TEXT NOT NULL,
    body           TEXT,
    target_org_id  INTEGER REFERENCES org_unit(org_id),   -- NULL 이면 모든 조직. 아니면 그 조직과 아래
    target_role    TEXT,                              -- NULL 이면 모든 역할. manager / agent
    fc_visible     INTEGER,                           -- 시상안 원문만. 1 설계사에게 보여도 되는 FC 몫만 / 0 메인 관리자만. 다른 분류는 NULL
    status         TEXT NOT NULL DEFAULT 'posted',    -- posted / withdrawn
    posted_by      TEXT NOT NULL,
    posted_at      TEXT NOT NULL,
    updated_by     TEXT,
    updated_at     TEXT,
    withdrawn_by   TEXT,
    withdrawn_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_board_post_status ON board_post(status, posted_at);

CREATE TABLE IF NOT EXISTS board_file (
    file_id        INTEGER PRIMARY KEY,
    post_id        INTEGER NOT NULL REFERENCES board_post(post_id),
    name           TEXT NOT NULL,                     -- 올린 파일 이름. 경로를 떼고 위험한 글자를 바꾼 것
    size_bytes     INTEGER NOT NULL,
    sha256         TEXT NOT NULL,                     -- 내용의 SHA-256 16진수. 저장 파일 이름이기도 하다
    kind           TEXT NOT NULL,                     -- pdf / image / excel / hwp / word
    uploaded_by    TEXT NOT NULL,
    uploaded_at    TEXT NOT NULL,
    removed_by     TEXT,                              -- 고치면서 뺀 때. 파일과 줄은 지우지 않는다
    removed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_board_file_post ON board_file(post_id);
-- [board:end]

-- ---------------------------------------------------------------------------
-- 프로모션 원안 칸과 확정 결과 (파트 D, D-21 은 열린 결정이라 제안대로 둔 가정). 기존 CREATE 문은 그대로 두고 덧붙이기만 한다.
-- 원안: 보험사 원안의 보상. 지금 칸(reward_value, rate)은 적립금을 뺀 배포안으로 본다. 원안 칸은 메인 관리자가 넣은 것만 있고
-- 비면 원문 확인이다. 설계사 화면은 배포안만, 메인 관리자 화면은 둘 다 보여 준다. 금액 단위는 reward_value, rate 와 같다.
-- 확정: 끝난 프로모션의 사람별 결과를 메인 관리자가 확정해 남긴다. 확정 전에는 계약 원장에서 그때그때 계산한 마감 전 예상이다.
-- 확정 한 번이 promotion_confirm 한 줄이고, 그때의 재직자 모두의 결과가 promotion_result 에 남는다. 지우지 않는다.
-- 확정을 무르는 일은 아직 없다. 생기면 voided_at 을 채우고 새 확정을 만든다.
-- 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 쓴다. app/engine/promotion.py 의 ensure 가 이 묶음을 읽고, 이미 있는 칸은 건너뛴다.
-- [promotion_plan:begin]
ALTER TABLE promotion_tier ADD COLUMN original_value NUMERIC;   -- 원안 보상. 정액이면 원, 비율이면 0.5 = 50%
ALTER TABLE promotion_rate ADD COLUMN original_rate NUMERIC;    -- 원안 지급률

CREATE TABLE IF NOT EXISTS promotion_confirm (
    confirm_id      INTEGER PRIMARY KEY,
    promo_id        INTEGER NOT NULL REFERENCES promotion(promo_id),
    ref_date        TEXT NOT NULL,                    -- 계산 기준일 YYYY-MM-DD. 프로모션 종료일 뒤다
    date_basis      TEXT NOT NULL,                    -- 확정할 때의 실적 기준일 칸
    basis_note      TEXT,                             -- 확정할 때 화면에 보인 실적 기준 설명
    n_rows          INTEGER NOT NULL,
    confirmed_by    TEXT NOT NULL,                    -- 메인 관리자의 login_id
    confirmed_at    TEXT NOT NULL,
    voided_by       TEXT,
    voided_at       TEXT,
    void_reason     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_promotion_confirm_live ON promotion_confirm(promo_id) WHERE voided_at IS NULL;

CREATE TABLE IF NOT EXISTS promotion_result (
    result_id       INTEGER PRIMARY KEY,
    confirm_id      INTEGER NOT NULL REFERENCES promotion_confirm(confirm_id),
    promo_id        INTEGER NOT NULL REFERENCES promotion(promo_id),
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    value           NUMERIC NOT NULL,                 -- 실적. 프로모션의 지표 단위
    tier_no         INTEGER,                          -- 구간. 없으면 NULL
    reward          TEXT,                             -- 보상 글. 여행지, 물품, 금액 글
    amount          NUMERIC,                          -- 배포안 금액. 여행과 물품은 NULL
    original_amount NUMERIC,                          -- 원안 금액. 원안 칸이 없으면 NULL
    outcome         TEXT NOT NULL,                    -- achieved / missed
    UNIQUE (confirm_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_promotion_result_promo ON promotion_result(promo_id, agent_id);
-- [promotion_plan:end]

-- ---------------------------------------------------------------------------
-- 초대 링크 (앱 소개와 초대 랜딩, docs/10 6.1). 메인 관리자가 초기 비밀번호를 쓰는 계정에 한 번 쓰는 링크를 만든다.
-- 토큰 원문은 저장하지 않고 SHA-256 16진수만 둔다. 유효기간은 app/web/invite.py 의 TTL_DAYS 이고, 쓰면 used_at 이 찬다.
-- 같은 계정에 새 링크를 만들면 앞의 줄에 revoked_at 이 찬다. 줄은 지우지 않는다.
-- 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 쓴다. app/web/invite.py 의 ensure 가 이 묶음을 읽는다.
-- [invite:begin]
CREATE TABLE IF NOT EXISTS user_invite (
    invite_id      INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES user_account(user_id),
    token_hash     TEXT NOT NULL UNIQUE,              -- 토큰의 SHA-256 16진수. 원문은 만든 화면에 한 번만 보인다
    created_by     TEXT NOT NULL,                     -- 만든 메인 관리자의 login_id
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,                     -- 이 시각이 지나면 쓸 수 없다
    used_at        TEXT,                              -- 첫 비밀번호를 정한 때. 차면 다시 쓸 수 없다
    revoked_at     TEXT                               -- 새 링크를 만들었거나 다른 링크를 써서 거둔 때
);
CREATE INDEX IF NOT EXISTS idx_user_invite_user ON user_invite(user_id, used_at, revoked_at);
-- [invite:end]

-- ---------- 수수료·점검: 대사 차이 처리 기록 (G) ----------
-- 웹의 대사 화면이 차이마다 남기는 처리 상태다. checked(확인함) / asked(보험사 문의) / resolved(해결). 처리 전은 줄이 없고 미처리로 본다.
-- 한 번 처리가 한 줄이다. 지우지 않고 지금 상태는 마지막 줄이다. 대사를 다시 돌리면 diff_id 가 새로 생기므로
-- (period_ym, insurer_id, policy_no, commission_type, installment_no) 로 묶어 새 차이에 이어 붙인다. diff_id 와 run_id 는 처리할 때의 것이다.
-- diff_amount 는 처리할 때의 차이 금액이다. 지금 차이와 다르면 처리 뒤에 금액이 바뀐 것이다. app/engine/settle.py 가 쓴다.
-- 기존 도구의 reconciliation_diff.status 는 건드리지 않는다.
-- [settle:begin]
CREATE TABLE IF NOT EXISTS recon_diff_action (
    action_id       INTEGER PRIMARY KEY,
    period_ym       TEXT NOT NULL,
    insurer_id      INTEGER NOT NULL REFERENCES insurer(insurer_id),
    policy_no       TEXT NOT NULL,
    commission_type TEXT,
    installment_no  INTEGER,
    diff_id         INTEGER NOT NULL REFERENCES reconciliation_diff(diff_id),
    run_id          INTEGER NOT NULL REFERENCES calc_run(run_id),
    status          TEXT NOT NULL CHECK (status IN ('checked','asked','resolved')),
    diff_amount     NUMERIC NOT NULL,
    note            TEXT NOT NULL,
    acted_by        TEXT NOT NULL,                    -- 메인 관리자의 login_id
    acted_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recon_diff_action_key ON recon_diff_action(period_ym, insurer_id, policy_no, commission_type, installment_no);
-- [settle:end]

-- ---------------------------------------------------------------------------
-- 명함 배지 판정 (S3 명함 연동 3차). app/connect/card_badges.py 가 쓴다. 다시 만들 수 있는 계산 결과다.
-- 근거는 인증 확정 기록 agent_certification_award, 인증 계산 agent_certification_status, 조직 부문 수상 확정 org_award_result 다.
-- 근거가 없어진 줄은 지우지 않고 gone_at 을 채운다. 사람 이름, 실적 값, 시상 내용, 금액은 넣지 않는다.
-- 배지는 퇴사 전까지 유지한다. D-55. 그래서 valid_until 은 비어 있고 status 는 valid 나 expected 다. 배지 종류는 config/card_badges.yaml 이다.
-- [card_badge:begin]
CREATE TABLE IF NOT EXISTS card_badge (
    id               INTEGER PRIMARY KEY,
    agent_id         INTEGER NOT NULL REFERENCES agent(agent_id),
    badge_key        TEXT NOT NULL,                   -- cert:우수인증설계사:2025 / award:SALES_EXCELLENT:2025 / org_award:12:202610
    kind             TEXT NOT NULL,                   -- cert(명함 자격 칩) / award(인증 프로그램의 수상) / org_award(조직 부문 수상)
    badge            TEXT NOT NULL,                   -- 명함 사이트의 자격 이름, 또는 프로그램 이름이나 부문 이름
    card_field       TEXT NOT NULL,                   -- certs(자격 칩과 연도 칸) / etc(그 밖의 자격·수상 칸)
    year             INTEGER,                         -- 연도 칸에 넣을 달성 연도
    period_ym        TEXT,                            -- 조직 부문 수상의 수상월 YYYYMM
    label            TEXT NOT NULL,                   -- 명함에 보이는 글
    basis            TEXT NOT NULL,                   -- award(인증 확정) / calc(인증 계산 달성, 확정 전) / org_award(부문 수상 확정)
    source_ref       TEXT,                            -- 프로그램 코드나 부문 번호
    valid_from       TEXT,                            -- YYYY-MM-DD
    valid_until      TEXT,                            -- YYYY-MM-DD. 이날까지 유효
    status           TEXT NOT NULL,                   -- valid / expiring / expired / expected
    first_judged_at  TEXT NOT NULL,
    judged_at        TEXT NOT NULL,
    gone_at          TEXT,                            -- 근거가 없어진 때
    UNIQUE (agent_id, badge_key)
);
CREATE INDEX IF NOT EXISTS idx_card_badge_until ON card_badge(valid_until);
-- [card_badge:end]

-- ---------------------------------------------------------------------------
-- 명함 배지와 명예의전당 항목의 본인 공개 설정. D-77. app/connect/card_badges.py 가 쓴다.
-- 항목은 자격이 생긴 배지 하나이고 열쇠는 card_badge.badge_key 와 같은 꼴이다. 인증, 수상, 조직 부문 수상 같은 기록이 모두 여기에 든다.
-- 줄이 없으면 비공개다. 본인만 바꾼다. 메인 관리자도 남의 것을 바꾸지 못한다. 바꾼 일은 audit_log 의 card_publish 에 남는다.
-- 퇴사하면 모두 비공개로 돌린다. 줄은 지우지 않는다. 사람 이름, 실적 값, 시상 내용, 금액은 넣지 않는다.
-- [card_publish:begin]
CREATE TABLE IF NOT EXISTS card_publish (
    agent_id    INTEGER NOT NULL REFERENCES agent(agent_id),
    item_key    TEXT NOT NULL,                        -- cert:MDRT:2026 / award:SALES_EXCELLENT:2025 / org_award:12:202610
    published   INTEGER NOT NULL DEFAULT 0,           -- 1 공개 / 0 비공개
    changed_by  TEXT NOT NULL,                        -- 바꾼 계정의 login_id. 퇴사로 돌린 것은 system
    changed_at  TEXT NOT NULL,
    PRIMARY KEY (agent_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_card_publish_on ON card_publish(published, agent_id);
-- [card_publish:end]

-- ---------------------------------------------------------------------------
-- 조직별 설정 (파트 H, /org/settings). 항목의 목록과 기본값, 꼴은 app/org/settings.py 의 ITEMS 가 원본이다.
-- 값은 GA 에서 아래로 상속하고 아래 조직이 덮어쓴다. 값마다 시작 달 from_ym 이 있어 지난달 계산은 그때 값을 쓴다.
-- 지우지 않는다. 바꿀 때마다 org_setting 에 줄을 더하고, value 가 NULL 인 줄은 그 달부터 위 조직의 값을 따르게 돌린 것이다.
-- 막기는 메인 관리자만 한다. 막은 조직의 값이 아래 전부에 가고 아래에서는 값을 둘 수 없다. 풀면 to_ym 만 채운다.
-- 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 IF NOT EXISTS 로 쓴다. app/org/settings.py 의 ensure 가 이 묶음을 읽는다.
-- [org_settings:begin]
CREATE TABLE IF NOT EXISTS org_setting (
    id          INTEGER PRIMARY KEY,
    org_id      INTEGER NOT NULL REFERENCES org_unit(org_id),
    key         TEXT NOT NULL,                        -- 예: notify.tier_near, award.default_top_n
    value       TEXT,                                 -- JSON. NULL 이면 위 조직의 값을 따른다
    from_ym     TEXT NOT NULL,                        -- 적용 시작 달 YYYYMM
    set_by      TEXT NOT NULL,                        -- login_id
    set_at      TEXT NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_setting_find ON org_setting(org_id, key, from_ym);

CREATE TABLE IF NOT EXISTS org_setting_lock (
    lock_id     INTEGER PRIMARY KEY,
    org_id      INTEGER NOT NULL REFERENCES org_unit(org_id),
    key         TEXT NOT NULL,
    from_ym     TEXT NOT NULL,                        -- 막기 시작 달 YYYYMM
    to_ym       TEXT,                                 -- 막기 끝 달. 비면 아직 막혀 있다
    locked_by   TEXT NOT NULL,
    locked_at   TEXT NOT NULL,
    unlocked_by TEXT,
    unlocked_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_org_setting_lock_live ON org_setting_lock(org_id, key) WHERE to_ym IS NULL;
-- [org_settings:end]

-- ---------------------------------------------------------------------------
-- 운영 기반 (파트 K, platform). 세션 버전, 뒤에서 하는 작업, 스키마 버전. app/ops 가 이 묶음을 쓴다.
-- 세션 버전: 로그아웃, 비밀번호 바꾸기, 계정 중지와 재설정 때 1 씩 올린다. 쿠키에 적힌 값과 다르면 guard 가 접속을 끊는다.
-- 작업: 화면과 명령줄은 줄을 넣고 상태만 본다. 워커 python -m app.worker 가 하나씩 꺼내 처리한다.
-- 작업의 params 와 result 에는 사람 이름, 연락처, 금액을 넣지 않는다. 건수와 번호와 귀속월만 둔다. error 에는 예외 종류만 둔다.
-- 지우지 않는다. 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 쓴다. app/ops/schema.py 의 ensure 가 읽고, 이미 있는 칸은 건너뛴다.
-- [platform:begin]
ALTER TABLE user_account ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS job (
    job_id          INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,                    -- perf_run, rank_run, ingest, connect_watch, gdrive, notify_sweep
    params          TEXT NOT NULL DEFAULT '{}',       -- JSON. 귀속월, 저장 위치 키, 종류
    dedupe_key      TEXT,                             -- 같은 키의 대기 중인 작업은 하나만 둔다
    status          TEXT NOT NULL DEFAULT 'queued',   -- queued / running / done / failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    run_after       TEXT NOT NULL,                    -- 이 시각 뒤에 꺼낸다. 실패하면 뒤로 미룬다
    requested_by    TEXT,                             -- login_id 또는 system, worker
    parent_id       INTEGER REFERENCES job(job_id),   -- 적재 뒤에 넣은 업적 run 처럼 앞 작업이 넣은 작업
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    lease_until     TEXT,                             -- 처리 중인 워커가 이 시각까지 잡는다. 지나면 다시 대기로 돌린다
    worker          TEXT,
    result          TEXT,                             -- JSON. 건수와 번호만
    error           TEXT                              -- 예외 종류만. 내용은 남기지 않는다
);
CREATE INDEX IF NOT EXISTS idx_job_queue ON job(status, run_after, job_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_dedupe ON job(dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS schema_migration (
    version         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    applied_at      TEXT NOT NULL
);
INSERT INTO schema_migration(version, name, applied_at) VALUES (1, 'schema.sql 기준', '2026-11-15 00:00:00') ON CONFLICT (version) DO NOTHING;
-- [platform:end]

-- ---------------------------------------------------------------------------
-- 프로모션 확정 기능 (파트 D, S3). app/engine/promotion.py 가 쓴다. 기존 CREATE 문은 그대로 두고 덧붙이기만 한다.
-- 창별 구간표: promotion_tier.window_seq 가 비면 공통 구간표, 숫자면 그 창의 구간표다. 기존 프로모션은 모두 공통이라 그대로 돈다.
--   창별 구간표는 창마다 달성하는 결합 규칙(all)에서만 쓴다. 보상은 공통 구간표에서, 없으면 마지막 창의 구간표에서 읽는다. 가정이다.
-- 13회차 조건: promotion.inst13_required 가 1 이거나 지급 시점에 inst13 이 들면 13회차 유지 판정을 한다.
-- 확정 순위: promotion_result.rank_no 는 확정할 때의 재직자 전체에서 실적 큰 순이다. 같은 값은 같은 순위이고 실적이 0 이하면 비운다.
-- 확정할 때 센 계약: promotion_result_contract. 13회차 판정은 이 계약들로 한다. 지우지 않는다.
-- 13회차 확정: promotion_inst13 한 줄이 한 사람의 지급 또는 환수 판정이다. 판정 전인 사람은 줄이 없고 다음에 다시 확정한다.
--   (confirm_id, agent_id) 가 하나라 한 번 남긴 판정은 바뀌지 않는다. 결과 확정을 무르면 그 확정에 달린 판정도 함께 보이지 않는다.
-- 확정 무르기(D-50 제안대로 둔 가정): promotion_confirm 의 voided_at, voided_by, void_reason 을 채운다. 줄은 지우지 않는다.
-- 확정 규칙: promotion_confirm.rule_json 은 확정할 때의 창, 구간표, 지급률이다. 13회차 판정이 이 규칙으로 다시 계산한다.
-- 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 쓴다. app/engine/promotion.py 의 ensure 가 이 묶음을 읽고, 이미 있는 칸은 건너뛴다.
-- [promotion_confirm:begin]
ALTER TABLE promotion_tier ADD COLUMN window_seq INTEGER;                     -- 비면 공통 구간표. 숫자면 그 창(promotion_window.seq)의 구간표
ALTER TABLE promotion ADD COLUMN inst13_required INTEGER NOT NULL DEFAULT 0;  -- 1 이면 13회차 유지 계약만 인정
ALTER TABLE promotion_result ADD COLUMN rank_no INTEGER;                      -- 확정할 때의 순위. 실적이 0 이하면 NULL
ALTER TABLE promotion_confirm ADD COLUMN rule_json TEXT;                      -- 확정할 때의 창, 구간표, 지급률

CREATE TABLE IF NOT EXISTS promotion_result_contract (
    id              INTEGER PRIMARY KEY,
    confirm_id      INTEGER NOT NULL REFERENCES promotion_confirm(confirm_id),
    promo_id        INTEGER NOT NULL REFERENCES promotion(promo_id),
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    contract_id     INTEGER NOT NULL REFERENCES contract(contract_id),
    window_seq      INTEGER NOT NULL,                 -- 그 계약을 센 창
    value           NUMERIC NOT NULL                  -- 그 창에서 센 값. 건당 인정 한도를 적용한 뒤다
);
CREATE INDEX IF NOT EXISTS idx_promotion_result_contract ON promotion_result_contract(confirm_id, agent_id);

CREATE TABLE IF NOT EXISTS promotion_inst13 (
    inst13_id       INTEGER PRIMARY KEY,
    confirm_id      INTEGER NOT NULL REFERENCES promotion_confirm(confirm_id),
    promo_id        INTEGER NOT NULL REFERENCES promotion(promo_id),
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    ref_date        TEXT NOT NULL,                    -- 판정 기준일 YYYY-MM-DD
    decision        TEXT NOT NULL CHECK (decision IN ('pay','clawback')),  -- 지급 / 환수
    value           NUMERIC NOT NULL,                 -- 결과 확정 때의 실적
    value_kept      NUMERIC NOT NULL,                 -- 실효 계약을 뺀 실적
    tier_no         INTEGER,
    tier_no_kept    INTEGER,
    amount          NUMERIC,                          -- 결과 확정 때의 배포안 금액
    amount_kept     NUMERIC,                          -- 실효 계약을 뺀 배포안 금액
    original_amount NUMERIC,
    original_amount_kept NUMERIC,
    outcome_kept    TEXT NOT NULL,                    -- achieved / missed
    n_kept          INTEGER NOT NULL,
    n_lapsed        INTEGER NOT NULL,
    judged_by       TEXT NOT NULL,                    -- 메인 관리자의 login_id
    judged_at       TEXT NOT NULL,
    UNIQUE (confirm_id, agent_id)
);

CREATE TABLE IF NOT EXISTS promotion_inst13_contract (
    id              INTEGER PRIMARY KEY,
    inst13_id       INTEGER NOT NULL REFERENCES promotion_inst13(inst13_id),
    contract_id     INTEGER NOT NULL REFERENCES contract(contract_id),
    window_seq      INTEGER NOT NULL,
    value           NUMERIC NOT NULL,
    due_date        TEXT,                             -- 13회차 도래일
    judgement       TEXT NOT NULL,                    -- kept / lapsed
    reason          TEXT NOT NULL,                    -- paid / lapsed / single
    paid_ym         TEXT                              -- 판정에 쓴 마지막 납입 달
);
CREATE INDEX IF NOT EXISTS idx_promotion_inst13_contract ON promotion_inst13_contract(inst13_id);
-- [promotion_confirm:end]

-- ---------------------------------------------------------------------------
-- 제도 (파트 I, S4). 기관별 기준 버전, 커리어 회차와 승인, 영업우수 선발. 계산은 app/engine/standards.py, career.py, certification.py 다.
-- 기준 버전은 판정할 때 쓴 기준의 사본이다. 같은 기관의 같은 해 기준이 고쳐지면 판이 하나 늘고, 앞의 판정은 앞의 판을 가리킨다.
-- 승인과 선발은 메인 관리자만 한다. 지우지 않는다. 무르면 voided_at, voided_by, void_reason 을 채운다. 무르기 여부는 열린 결정이라 가정이다.
-- 사람 이름과 금액은 넣지 않는다. 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 IF NOT EXISTS 로 쓴다. standards.ensure 가 이 묶음을 읽는다.
-- [program_s4:begin]
CREATE TABLE IF NOT EXISTS standard_version (
    version_id      INTEGER PRIMARY KEY,
    scope           TEXT NOT NULL,                    -- cert(인증, 임원제도, 영업우수) / career(커리어패스)
    std_key         TEXT NOT NULL,                    -- cert 는 프로그램 코드, career 는 'FC>SFC' 처럼 단계 코드 짝
    issuer          TEXT,                             -- 주관 기관. 커리어 기준은 기관 칸이 없어 자사로 본다
    year            INTEGER NOT NULL,                 -- 기준의 해. 커리어는 회차 기준월의 해
    version_no      INTEGER NOT NULL,                 -- 같은 scope, std_key, year 안에서 1 부터
    content_hash    TEXT NOT NULL,                    -- 기준 줄을 정렬한 JSON 의 SHA-256 앞 16자
    criteria_json   TEXT NOT NULL,                    -- 판정 때의 기준 줄 사본
    recorded_by     TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    UNIQUE (scope, std_key, year, content_hash)
);

CREATE TABLE IF NOT EXISTS standard_use (
    id              INTEGER PRIMARY KEY,
    version_id      INTEGER NOT NULL REFERENCES standard_version(version_id),
    used_in         TEXT NOT NULL,                    -- cert_run(인증 계산 run) / career_round(커리어 회차)
    ref_id          INTEGER NOT NULL,                 -- run_id 또는 round_id
    used_at         TEXT NOT NULL,
    UNIQUE (used_in, ref_id, version_id)
);
CREATE INDEX IF NOT EXISTS idx_standard_use_ref ON standard_use(used_in, ref_id);

CREATE TABLE IF NOT EXISTS career_round (
    round_id        INTEGER PRIMARY KEY,
    round_key       TEXT NOT NULL,                    -- 판정에 쓴 최근 끝난 분기. 2026Q3
    as_of_ym        TEXT NOT NULL,                    -- 회차를 연 기준월
    n_members       INTEGER NOT NULL,
    n_eligible      INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',     -- open / replaced(같은 분기의 회차를 다시 열어 바뀐 것)
    opened_by       TEXT NOT NULL,                    -- 메인 관리자의 login_id
    opened_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS career_round_member (
    id              INTEGER PRIMARY KEY,
    round_id        INTEGER NOT NULL REFERENCES career_round(round_id),
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    from_level_id   INTEGER NOT NULL REFERENCES career_level(level_id),
    to_level_id     INTEGER NOT NULL REFERENCES career_level(level_id),
    n_met           INTEGER NOT NULL,
    n_total         INTEGER NOT NULL,
    eligible        INTEGER NOT NULL,                 -- 기준을 모두 채운 후보
    detail_json     TEXT NOT NULL,                    -- 지표별 실적, 기준, 충족
    version_id      INTEGER REFERENCES standard_version(version_id),
    UNIQUE (round_id, agent_id)
);

CREATE TABLE IF NOT EXISTS career_decision (
    decision_id     INTEGER PRIMARY KEY,
    round_id        INTEGER NOT NULL REFERENCES career_round(round_id),
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    from_level_id   INTEGER NOT NULL REFERENCES career_level(level_id),
    to_level_id     INTEGER NOT NULL REFERENCES career_level(level_id),
    decision        TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
    effective_ym    TEXT,                             -- 승인이면 새 단계가 시작하는 달
    note            TEXT,
    history_id      INTEGER REFERENCES agent_career_history(id),   -- 승인으로 넣은 단계 이력
    prev_history_id INTEGER REFERENCES agent_career_history(id),   -- 승인으로 닫은 앞 단계 이력
    prev_effective_to TEXT,                           -- 닫기 전 앞 이력의 effective_to. 무를 때 되돌린다
    version_id      INTEGER REFERENCES standard_version(version_id),
    decided_by      TEXT NOT NULL,
    decided_at      TEXT NOT NULL,
    voided_by       TEXT,
    voided_at       TEXT,
    void_reason     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_career_decision_live ON career_decision(round_id, agent_id) WHERE voided_at IS NULL;

CREATE TABLE IF NOT EXISTS excellence_selection (
    selection_id    INTEGER PRIMARY KEY,
    program_id      INTEGER NOT NULL REFERENCES certification_program(program_id),
    year            INTEGER NOT NULL,
    agent_id        INTEGER NOT NULL REFERENCES agent(agent_id),
    status_run_id   INTEGER REFERENCES calc_run(run_id),           -- 선발 때 본 인증 계산 run
    version_id      INTEGER REFERENCES standard_version(version_id),
    note            TEXT,
    selected_by     TEXT NOT NULL,
    selected_at     TEXT NOT NULL,
    voided_by       TEXT,
    voided_at       TEXT,
    void_reason     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_excellence_selection_live ON excellence_selection(program_id, year, agent_id) WHERE voided_at IS NULL;
-- [program_s4:end]

-- ---------------------------------------------------------------------------
-- 영업 바탕과 팀 코칭 (파트 J, S4, web-screen). app/sales 가 쓴다. 기존 CREATE 문은 그대로 두고 덧붙이기만 한다.
-- D-12 제안대로 둔 가정: DB 와 고객의 이름과 연락처는 저장하되 화면 목록에서는 가린다. 원문은 담당 설계사와 직속 관리자만 보고,
--   메인 관리자는 사유를 적은 뒤 본다. 원문을 본 일은 audit_log 에 sales_contact_view 로 남긴다.
-- D-13 제안대로 둔 가정: 상담 이력의 원본은 이 묶음의 sales_activity 다. 노션은 읽기용으로 남기고 이번에는 잇지 않는다.
-- D-24 제안대로 둔 가정: 해촉 설계사의 고객은 직속 관리자에게 넘기고 handover_status 를 pending 으로 둔다.
--   메인 관리자가 준법 확인을 남기기 전까지 받은 관리자에게도 연락처는 가려진다. 진행 중인 DB 는 회수해 조직 풀로 돌린다.
-- 가이드 자료는 새 표 없이 공지와 자료실 board_post 의 분류 guide, script, needs 로 둔다. /sales/guide 가 그 셋을 모아 보인다.
-- D-29 제안대로 둔 가정: 상담 신청 랜딩은 이름, 연락처, 지역, 상담 희망 시간과 동의 시각만 받는다. 접속 IP 는 저장하지 않는다.
-- 주민번호, 계좌번호, 소득은 칸이 없다. 지우지 않는다. 회수와 넘기기는 sales_assign_log 에 남기고 줄은 그대로 둔다.
-- 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 쓴다. app/sales/core.py 의 ensure 가 이 묶음을 읽는다. 묶음 안에는 ; 가 든 글자를 두지 않는다.
-- [sales:begin]
CREATE TABLE IF NOT EXISTS sales_lead (
    lead_id          INTEGER PRIMARY KEY,
    source           TEXT NOT NULL,                   -- landing(상담 신청) / self(직접 등록) / company(회사 DB) / referral(소개) / event(행사)
    name             TEXT NOT NULL,
    phone            TEXT NOT NULL,                   -- 숫자와 줄표만. 화면 목록에서는 가운데를 가린다
    region           TEXT NOT NULL,                   -- 시도 이름. app/sales/core.py 의 REGIONS
    preferred_time   TEXT,                            -- 상담 희망 시간. app/sales/core.py 의 TIMES
    memo             TEXT,
    consent_at       TEXT,                            -- 수집과 이용 동의 시각
    consent_version  TEXT,                            -- 동의 문구의 판. 준법 확인 전 문구는 draft 로 시작한다
    status           TEXT NOT NULL DEFAULT 'new',     -- new(미배정) / assigned(배정) / working(진행 중) / won(체결) / lost(종료)
    agent_id         INTEGER REFERENCES agent(agent_id),       -- 담당. 미배정이면 NULL
    pool_org_id      INTEGER REFERENCES org_unit(org_id),      -- 미배정일 때 배정할 조직. NULL 이면 메인 관리자의 본부 풀
    assigned_at      TEXT,
    assigned_by      TEXT,
    last_activity_at TEXT,
    reclaim_count    INTEGER NOT NULL DEFAULT 0,
    closed_at        TEXT,
    close_reason     TEXT,
    customer_id      INTEGER,
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_lead_agent ON sales_lead(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_sales_lead_pool ON sales_lead(status, pool_org_id);

CREATE TABLE IF NOT EXISTS sales_assign_log (
    id               INTEGER PRIMARY KEY,
    lead_id          INTEGER NOT NULL REFERENCES sales_lead(lead_id),
    action           TEXT NOT NULL,                   -- assign / auto / reclaim / pool / handover
    from_agent_id    INTEGER,
    to_agent_id      INTEGER,
    pool_org_id      INTEGER,
    reason           TEXT,
    by_login         TEXT NOT NULL,
    at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_assign_log_lead ON sales_assign_log(lead_id);

CREATE TABLE IF NOT EXISTS sales_activity (
    activity_id      INTEGER PRIMARY KEY,
    lead_id          INTEGER NOT NULL REFERENCES sales_lead(lead_id),
    agent_id         INTEGER NOT NULL REFERENCES agent(agent_id),
    kind             TEXT NOT NULL CHECK (kind IN ('call','consult','proposal','application')),   -- 통화 / 상담 / 제안 / 청약
    done_on          TEXT NOT NULL,                   -- YYYY-MM-DD
    result           TEXT,                            -- 짧은 결과. 고객의 건강, 소득 같은 민감한 내용은 적지 않는다
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_activity_agent ON sales_activity(agent_id, done_on);
CREATE INDEX IF NOT EXISTS idx_sales_activity_lead ON sales_activity(lead_id);

CREATE TABLE IF NOT EXISTS sales_region_pref (
    agent_id         INTEGER NOT NULL REFERENCES agent(agent_id),
    region           TEXT NOT NULL,
    set_by           TEXT NOT NULL,
    set_at           TEXT NOT NULL,
    PRIMARY KEY (agent_id, region)
);

CREATE TABLE IF NOT EXISTS sales_customer (
    customer_id      INTEGER PRIMARY KEY,
    lead_id          INTEGER REFERENCES sales_lead(lead_id),
    agent_id         INTEGER NOT NULL REFERENCES agent(agent_id),   -- 지금 담당
    name             TEXT NOT NULL,
    phone            TEXT NOT NULL,
    region           TEXT,
    birth_md         TEXT,                            -- 생일 MM-DD. 연도는 받지 않는다
    anniv_md         TEXT,                            -- 다른 기념일 MM-DD
    anniv_label      TEXT,
    next_action      TEXT,                            -- 다음 할 일
    next_on          TEXT,                            -- 다음 할 일의 날짜 YYYY-MM-DD
    handed_from_agent_id INTEGER REFERENCES agent(agent_id),       -- 해촉자에게서 넘겨받았으면 그 설계사
    handover_status  TEXT,                            -- NULL / pending(준법 확인 대기) / confirmed(준법 확인)
    handover_at      TEXT,
    handover_by      TEXT,
    compliance_by    TEXT,
    compliance_at    TEXT,
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_customer_agent ON sales_customer(agent_id);

CREATE TABLE IF NOT EXISTS sales_customer_contract (
    customer_id      INTEGER NOT NULL REFERENCES sales_customer(customer_id),
    contract_id      INTEGER NOT NULL UNIQUE REFERENCES contract(contract_id),
    linked_by        TEXT NOT NULL,
    linked_at        TEXT NOT NULL,
    PRIMARY KEY (customer_id, contract_id)
);

CREATE TABLE IF NOT EXISTS team_coaching (
    coaching_id      INTEGER PRIMARY KEY,
    agent_id         INTEGER NOT NULL REFERENCES agent(agent_id),   -- 면담한 팀원
    manager_agent_id INTEGER NOT NULL REFERENCES agent(agent_id),   -- 면담한 관리자
    met_on           TEXT NOT NULL,                   -- YYYY-MM-DD
    promises         TEXT,                            -- 약속 사항
    next_on          TEXT,                            -- 다음 면담 날짜
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_coaching_agent ON team_coaching(agent_id, met_on);
-- [sales:end]

-- ---------------------------------------------------------------------------
-- 앱 안 AI (파트 K, ai-features, S4). app/ai 가 쓴다. 설계는 docs/13 1.4 와 2.4 다.
-- ai_budget: 기능별 월 한도. D-26 제안대로 둔 가정이다. 바꿀 때마다 줄을 더하고 마지막 줄을 쓴다. 줄이 없으면 AI_MONTHLY_LIMIT_USD 를 쓴다.
-- ai_call: API 호출 한 번이 한 줄이다. 쓴 토큰과 비용 추정, 상태만 남긴다. 키, 프롬프트 본문, 응답 본문은 남기지 않는다.
--   status 는 ok, error, budget 이다. budget 은 한도에 닿아 부르지 않은 것이다. 이번 달 쓴 비용은 ym 의 cost_usd 합이다.
-- ai_output: 해석 보조와 묻고 답하기의 한 번 실행. 입력 해시, 지침 버전, 가린 뒤의 컨텍스트, 출력, 자동 점검 결과, 사람의 결재와 고친 문안.
-- ai_review: 7나이츠 심사 한 건. required 는 고객에게 나가는 글이라 통과가 필수인지다(D-15 가정). 처음 글, 마지막 글, 사람이 확정한 글을 둔다.
-- ai_review_opinion: 회전마다 다빈치의 형상화(verdict shape)와 자리별 판정, 근거 인용, 고친 문안. adopted 는 다음 회전에 받은 문안이다.
-- 이름, 연락처, 주민번호는 가린 뒤에만 들어간다. 지우지 않는다. 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 쓴다. app/ai/store.py 의 ensure 가 읽는다.
-- [ai:begin]
CREATE TABLE IF NOT EXISTS ai_budget (
    budget_id         INTEGER PRIMARY KEY,
    feature           TEXT NOT NULL,                  -- plan_interpret, plan_qa, review, report, cardnews, monthly_report
    monthly_limit_usd NUMERIC NOT NULL,
    set_by            TEXT NOT NULL,
    set_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_budget_feature ON ai_budget(feature, budget_id);

CREATE TABLE IF NOT EXISTS ai_call (
    call_id         INTEGER PRIMARY KEY,
    feature         TEXT NOT NULL,
    ym              TEXT NOT NULL,                    -- 한도를 세는 달 YYYYMM
    called_at       TEXT NOT NULL,
    login_id        TEXT,
    model           TEXT,
    guide_version   TEXT,
    input_hash      TEXT,
    status          TEXT NOT NULL,                    -- ok / error / budget
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd        NUMERIC NOT NULL DEFAULT 0,       -- 가격표로 낸 추정
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT                              -- 실패 종류만. timeout, connect, http_429 같은 것
);
CREATE INDEX IF NOT EXISTS idx_ai_call_month ON ai_call(feature, ym);

CREATE TABLE IF NOT EXISTS ai_output (
    output_id       INTEGER PRIMARY KEY,
    feature         TEXT NOT NULL,
    login_id        TEXT,
    created_at      TEXT NOT NULL,
    input_hash      TEXT,
    guide_version   TEXT,
    context_json    TEXT,                             -- 가린 뒤의 근거 줄, 계산값, 질문
    output_text     TEXT,
    parsed_json     TEXT,                             -- 해석 보조의 구조화된 초안
    status          TEXT NOT NULL,                    -- draft, blocked, answered, no_evidence, off, budget, error
    problems_json   TEXT,                             -- 자동 점검에서 찾은 것
    call_id         INTEGER REFERENCES ai_call(call_id),
    ref_kind        TEXT,                             -- plan_line 같은 대상의 종류
    ref_id          INTEGER,
    decision        TEXT,                             -- approved / rejected. 비면 결재 전
    decided_by      TEXT,
    decided_at      TEXT,
    edited_text     TEXT,                             -- 사람이 고친 문안
    decision_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_output_ref ON ai_output(feature, ref_kind, ref_id);

CREATE TABLE IF NOT EXISTS ai_review (
    review_id       INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,                    -- cardnews, report_form, notice, standard_text, landing, report, monthly_report
    audience        TEXT NOT NULL,                    -- customer / agent / internal
    title           TEXT,
    requested_by    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    guide_version   TEXT NOT NULL,
    panel_version   TEXT NOT NULL,
    forbidden_version TEXT NOT NULL,
    required        INTEGER NOT NULL DEFAULT 0,       -- 1 이면 통과해야 승인한다
    draft_text      TEXT NOT NULL,
    current_text    TEXT NOT NULL,                    -- 마지막 회전의 글
    status          TEXT NOT NULL,                    -- running, passed, stopped, approved, rejected
    stop_reason     TEXT,                             -- rounds, reject, number, off, budget, error
    rounds          INTEGER NOT NULL DEFAULT 0,
    parent_id       INTEGER REFERENCES ai_review(review_id),
    decided_by      TEXT,
    decided_at      TEXT,
    final_text      TEXT,                             -- 사람이 승인한 글
    human_edited    INTEGER NOT NULL DEFAULT 0,
    decision_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_review_status ON ai_review(status, review_id);

CREATE TABLE IF NOT EXISTS ai_review_opinion (
    opinion_id      INTEGER PRIMARY KEY,
    review_id       INTEGER NOT NULL REFERENCES ai_review(review_id),
    round_no        INTEGER NOT NULL,
    seat_key        TEXT NOT NULL,                    -- davinci, themis, seat2 같은 자리
    seat_name       TEXT NOT NULL,
    verdict         TEXT NOT NULL,                    -- shape, pass, revise, reject, error
    quote           TEXT,
    quote_ok        INTEGER NOT NULL DEFAULT 1,       -- 인용이 심사한 글에 있는지
    reason          TEXT,                             -- 판정 이유. shape 이면 형상화 글
    revised_text    TEXT,
    adopted         INTEGER NOT NULL DEFAULT 0,
    call_id         INTEGER REFERENCES ai_call(call_id),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_review_opinion ON ai_review_opinion(review_id, round_no);
-- [ai:end]

-- 사람별 기간 실적 집계 (파트 B, data-loader). 인별실적 파일에서 온다. 적재기는 app/loader/person_perf.py, 매핑은 config/mapping/eternal_person_perf.yaml 이다.
-- 계약 단위 원장(contract)과 섞지 않는다. 업적 집계 규칙(성립한 달에 더하고 철회한 달에 뺀다)은 이 표를 읽지 않는다.
-- agent_period_perf: 한 사람의 한 기간(start_ym 에서 end_ym) 한 줄. 사원명은 저장하지 않는다. 칸 이름이 _count 면 건수, _amt 면 원 단위 금액이다.
--   org_ 칸은 파일에 적힌 소속의 원문이다. 조직 트리와 잇지 않는다. agent_id 는 적재 때 사원번호로 찾은 설계사이고 없으면 비어 있다.
--   perf_tier 는 유실적 구분이다. elite_50, active_10, some, none 이고 파일의 묶음 값으로만 정한다.
-- agent_period_perf_insurer: 보험사 묶음의 건수와 월납보험료. insurer_name 은 파일의 원문이고 insurer_id 는 마스터에서 찾은 것이다.
-- 같은 기간의 파일을 다시 넣으면 이전 배치는 import_batch.status 가 superseded 가 된다. 읽을 때는 v_agent_period_perf 를 쓴다.
-- [person_perf:begin]
CREATE TABLE IF NOT EXISTS agent_period_perf (
    perf_id         INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES import_batch(batch_id),
    employee_no     TEXT NOT NULL,
    agent_id        INTEGER REFERENCES agent(agent_id),
    start_ym        TEXT NOT NULL,
    end_ym          TEXT NOT NULL,
    row_no          INTEGER,
    position        TEXT,
    org_general     TEXT,
    org_hq          TEXT,
    org_branch      TEXT,
    org_direct_branch TEXT,
    org_team        TEXT,
    perf_tier       TEXT NOT NULL,
    total_count     INTEGER NOT NULL DEFAULT 0,
    total_monthly_premium_amt REAL NOT NULL DEFAULT 0,
    total_premium_per_policy_amt REAL NOT NULL DEFAULT 0,
    total_insurer_perf_amt REAL NOT NULL DEFAULT 0,
    total_global_perf_amt REAL NOT NULL DEFAULT 0,
    active_small_count INTEGER,
    active_small_monthly_premium_amt REAL,
    active_10_count INTEGER NOT NULL DEFAULT 0,
    active_10_monthly_premium_amt REAL NOT NULL DEFAULT 0,
    elite_50_count  INTEGER NOT NULL DEFAULT 0,
    elite_50_premium_amt REAL NOT NULL DEFAULT 0,
    single_count    INTEGER NOT NULL DEFAULT 0,
    single_premium_amt REAL NOT NULL DEFAULT 0,
    single_insurer_perf_amt REAL NOT NULL DEFAULT 0,
    auto_count      INTEGER NOT NULL DEFAULT 0,
    auto_premium_amt REAL NOT NULL DEFAULT 0,
    auto_global_perf_amt REAL NOT NULL DEFAULT 0,
    general_count   INTEGER NOT NULL DEFAULT 0,
    general_premium_amt REAL NOT NULL DEFAULT 0,
    general_global_perf_amt REAL NOT NULL DEFAULT 0,
    UNIQUE (batch_id, employee_no)
);
CREATE INDEX IF NOT EXISTS idx_agent_period_perf_period ON agent_period_perf(start_ym, end_ym, agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_period_perf_emp ON agent_period_perf(employee_no);

CREATE TABLE IF NOT EXISTS agent_period_perf_insurer (
    perf_id         INTEGER NOT NULL REFERENCES agent_period_perf(perf_id),
    insurer_name    TEXT NOT NULL,
    insurer_id      INTEGER REFERENCES insurer(insurer_id),
    policy_count    INTEGER NOT NULL DEFAULT 0,
    monthly_premium_amt REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (perf_id, insurer_name)
);

CREATE VIEW IF NOT EXISTS v_agent_period_perf AS
SELECT p.* FROM agent_period_perf p JOIN import_batch b ON b.batch_id = p.batch_id AND b.status = 'active';
-- [person_perf:end]

-- ---------------------------------------------------------------------------
-- 프로모션 조건 확장 (파트 D, 11월 17일). app/engine/promotion.py 가 쓴다. 기존 CREATE 문은 그대로 두고 덧붙이기만 한다.
-- 칸이 비면 예전 동작 그대로다. 규칙은 app/engine/promotion.py 머리말의 '조건 확장' 이다.
-- count_basis: issued(성립 기준) 또는 application(청약 기준). 청약 기준은 철회, 반송, 해지를 뺀다 (D-85).
-- boost_json: 가중 인정 {factor, month_days 또는 ranges, cap, cap_scope, applies_to}. 한도의 범위는 D-84 가정이다.
-- product_include, product_exclude: 상품 이름 낱말의 JSON 목록. 이름이 빈 계약은 세지 않고 따로 센다 (D-86).
-- exclude_self: 1 이면 본인·가족계약(contract.self_family) 을 뺀다 (D-87). drop_statuses: 세지 않을 상태를 쉼표로 (D-92).
-- parent_promo_id, target_levels, gate_min_achievers, gate_org_path: 다른 프로모션 달성자에게 붙는 시책과 그 대상 직급, 조직 달성 인원 문턱 (D-88, D-89).
-- retention_min, retention_ref, retention_basis: 13회차 유지율 문턱. 표시만 한다 (D-90).
-- promotion_result_contract.value_each: 창 조건에 쓴 값. 가중 인정을 조건 1에만 쓰면 value 와 다르다. 비면 value 와 같다.
-- combine_rule 에 both(조건 1과 조건 2 모두), either(둘 가운데 하나)를 더한다. 칸은 그대로다.
-- 오래된 DB 에서도 이 묶음만 다시 돌릴 수 있게 쓴다. app/engine/promotion.py 의 ensure 가 이 묶음을 읽고, 이미 있는 칸은 건너뛴다.
-- [promotion_rules:begin]
ALTER TABLE promotion ADD COLUMN count_basis TEXT;                  -- issued / application. 비면 issued
ALTER TABLE promotion ADD COLUMN boost_json TEXT;                   -- 가중 인정
ALTER TABLE promotion ADD COLUMN product_include TEXT;              -- 대상 상품 이름 낱말 JSON 목록
ALTER TABLE promotion ADD COLUMN product_exclude TEXT;              -- 제외 상품 이름 낱말 JSON 목록
ALTER TABLE promotion ADD COLUMN exclude_self INTEGER NOT NULL DEFAULT 0;
ALTER TABLE promotion ADD COLUMN drop_statuses TEXT;                -- 세지 않을 상태를 쉼표로
ALTER TABLE promotion ADD COLUMN parent_promo_id INTEGER REFERENCES promotion(promo_id);
ALTER TABLE promotion ADD COLUMN target_levels TEXT;                -- 대상 커리어 직급 코드를 쉼표로
ALTER TABLE promotion ADD COLUMN gate_min_achievers INTEGER;        -- 조직 달성 인원 문턱
ALTER TABLE promotion ADD COLUMN gate_org_path TEXT;                -- 문턱을 셀 조직의 path_key. 비면 맨 위 조직
ALTER TABLE promotion ADD COLUMN retention_min NUMERIC;             -- 13회차 유지율 문턱. 0.95 = 95%
ALTER TABLE promotion ADD COLUMN retention_ref TEXT;                -- 유지율 판정 기준일 YYYY-MM-DD
ALTER TABLE promotion ADD COLUMN retention_basis TEXT;              -- count / amount. 비면 count
ALTER TABLE promotion_result_contract ADD COLUMN value_each NUMERIC;
-- [promotion_rules:end]
