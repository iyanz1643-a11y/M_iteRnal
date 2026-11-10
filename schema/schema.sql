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
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE product (
    product_id          INTEGER PRIMARY KEY,
    insurer_id          INTEGER NOT NULL REFERENCES insurer(insurer_id),
    product_code        TEXT NOT NULL,
    product_name        TEXT NOT NULL,
    product_category    TEXT NOT NULL,            -- life / health / longterm_pc / annuity / other
    conversion_rate     NUMERIC NOT NULL DEFAULT 1.0,  -- 환산율
    effective_from      TEXT NOT NULL,            -- YYYYMM
    effective_to        TEXT,                     -- NULL = 현재 유효
    UNIQUE (insurer_id, product_code, effective_from)
);

CREATE TABLE org_unit (
    org_id      INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES org_unit(org_id),
    name        TEXT NOT NULL,
    level       TEXT NOT NULL,                    -- hq / branch / office / team
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE agent (
    agent_id        INTEGER PRIMARY KEY,
    internal_code   TEXT NOT NULL UNIQUE,         -- 내부 사번
    name            TEXT NOT NULL,
    hire_date       TEXT,                         -- 위촉일 YYYY-MM-DD
    termination_date TEXT,                        -- 해촉일
    status          TEXT NOT NULL DEFAULT 'active' -- active / terminated / suspended
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

CREATE TABLE incentive_rule (
    rule_id         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    period_type     TEXT NOT NULL,                -- month / quarter
    metric          TEXT NOT NULL,                -- converted_premium / count / monthly_premium
    scope           TEXT NOT NULL DEFAULT 'all',  -- all / insurer / product_category
    scope_value     TEXT,
    clawback_months INTEGER NOT NULL DEFAULT 0,   -- 시상 환수 감시 기간 (0 = 없음)
    effective_from  TEXT NOT NULL,
    effective_to    TEXT,
    active          INTEGER NOT NULL DEFAULT 1
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
    data_type       TEXT NOT NULL,                -- statement / newbiz / inforce / payment / clawback / agentcode / internal
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
    policy_no           TEXT NOT NULL,            -- 정규화된 증권번호
    product_id          INTEGER REFERENCES product(product_id),
    product_code_raw    TEXT,                     -- 마스터에 없을 때 보존
    agent_id            INTEGER REFERENCES agent(agent_id),
    insurer_agent_code  TEXT,
    policyholder_name   TEXT,
    contract_date       TEXT,                     -- 성립일
    perf_date           TEXT,                     -- 업적 기준일 (내부 정책)
    perf_ym             TEXT,                     -- 업적 귀속 월
    payment_mode        TEXT,                     -- monthly / annual / single / quarterly / semiannual
    payment_term        INTEGER,                  -- 납입 기간(년)
    premium             NUMERIC,                  -- 원 보험료
    premium_monthly     NUMERIC,                  -- 월납 환산
    converted_premium   NUMERIC,                  -- 환산보험료
    status              TEXT NOT NULL DEFAULT 'inforce', -- inforce / lapsed / cancelled / withdrawn / matured
    status_date         TEXT,
    source_batch_id     INTEGER REFERENCES import_batch(batch_id),
    created_at          TEXT NOT NULL,
    UNIQUE (insurer_id, policy_no)
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
    period_ym   TEXT NOT NULL,
    kind        TEXT NOT NULL,                    -- p13 / p25
    numerator   INTEGER NOT NULL,
    denominator INTEGER NOT NULL,
    rate        NUMERIC NOT NULL,
    batch_id    INTEGER REFERENCES import_batch(batch_id),
    UNIQUE (agent_id, period_ym, kind)
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

-- ---------- 인덱스 ----------
CREATE INDEX idx_contract_agent_ym ON contract(agent_id, perf_ym);
CREATE INDEX idx_contract_status ON contract(status);
CREATE INDEX idx_stmt_match ON commission_statement_line(insurer_id, period_ym, policy_no, commission_type, installment_no);
CREATE INDEX idx_calc_match ON commission_calc(period_ym, contract_id, commission_type, installment_no);
CREATE INDEX idx_diff_status ON reconciliation_diff(period_ym, status);
CREATE INDEX idx_rate_rule_lookup ON commission_rate_rule(insurer_id, commission_type, effective_from);
CREATE INDEX idx_agent_code_lookup ON agent_insurer_code(insurer_id, insurer_agent_code);

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
