CREATE TABLE IF NOT EXISTS bank_decision (
    id VARCHAR(36) PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    amount DECIMAL(19, 2) NOT NULL CHECK (amount >= 0),
    time_bucket INTEGER NOT NULL CHECK (time_bucket >= 0 AND time_bucket <= 21 AND MOD(time_bucket, 3) = 0),
    fund_type VARCHAR(10) NOT NULL,
    channel VARCHAR(10) NOT NULL,
    risk_score DOUBLE PRECISION NOT NULL CHECK (risk_score >= 0 AND risk_score <= 1),
    alert BOOLEAN NOT NULL,
    threshold DOUBLE PRECISION NOT NULL CHECK (threshold >= 0 AND threshold <= 1),
    model_version VARCHAR(64) NOT NULL,
    request_key VARCHAR(64),
    request_hash VARCHAR(64)
);

ALTER TABLE bank_decision ADD COLUMN IF NOT EXISTS request_key VARCHAR(64);
ALTER TABLE bank_decision ADD COLUMN IF NOT EXISTS request_hash VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_bank_decision_alert_created
    ON bank_decision (alert, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bank_decision_created_id
    ON bank_decision (created_at DESC, id DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_decision_request_key
    ON bank_decision (request_key);

-- Contextual decisions keep the transaction used for scoring, while the
-- existing four-field decision table remains compatible with older clients.
CREATE TABLE IF NOT EXISTS bank_contextual_event (
    decision_id VARCHAR(36) PRIMARY KEY REFERENCES bank_decision(id),
    transaction_date DATE NOT NULL,
    source_account VARCHAR(64) NOT NULL,
    destination_account VARCHAR(64) NOT NULL,
    source_institution VARCHAR(64) NOT NULL,
    destination_institution VARCHAR(64) NOT NULL,
    history_count INTEGER NOT NULL CHECK (history_count >= 0),
    history_window_start DATE NOT NULL,
    history_status VARCHAR(32) NOT NULL,
    context_status VARCHAR(32) NOT NULL,
    evidence_type VARCHAR(80) NOT NULL,
    feature_schema_version VARCHAR(64) NOT NULL,
    prior_summary CLOB NOT NULL,
    observed_context CLOB NOT NULL,
    feature_snapshot CLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bank_contextual_source_date
    ON bank_contextual_event (source_account, transaction_date, decision_id);
CREATE INDEX IF NOT EXISTS idx_bank_contextual_source_institution_date
    ON bank_contextual_event (source_institution, source_account, transaction_date, decision_id);

-- Candidate v2 graph ledger is isolated from the v1 prior-day contract.
CREATE TABLE IF NOT EXISTS bank_graph_event (
    decision_id VARCHAR(36) PRIMARY KEY REFERENCES bank_decision(id),
    transaction_date DATE NOT NULL,
    transaction_hour INTEGER NOT NULL CHECK (transaction_hour >= 0 AND transaction_hour <= 21 AND MOD(transaction_hour, 3) = 0),
    source_account VARCHAR(64) NOT NULL,
    destination_account VARCHAR(64) NOT NULL,
    source_institution VARCHAR(64) NOT NULL,
    destination_institution VARCHAR(64) NOT NULL,
    history_count INTEGER NOT NULL CHECK (history_count >= 0),
    history_window_start DATE NOT NULL,
    history_status VARCHAR(32) NOT NULL,
    context_status VARCHAR(32) NOT NULL,
    evidence_type VARCHAR(80) NOT NULL,
    feature_schema_version VARCHAR(64) NOT NULL,
    prior_summary CLOB NOT NULL,
    observed_context CLOB NOT NULL,
    graph_context CLOB NOT NULL,
    model_explanation CLOB,
    support_assessment CLOB,
    feature_snapshot CLOB NOT NULL
);
ALTER TABLE bank_graph_event ADD COLUMN IF NOT EXISTS model_explanation CLOB;
ALTER TABLE bank_graph_event ADD COLUMN IF NOT EXISTS support_assessment CLOB;
CREATE INDEX IF NOT EXISTS idx_bank_graph_source_date_hour
    ON bank_graph_event (source_account, transaction_date, transaction_hour, decision_id);
CREATE INDEX IF NOT EXISTS idx_bank_graph_destination_date_hour
    ON bank_graph_event (destination_account, transaction_date, transaction_hour, decision_id);
CREATE INDEX IF NOT EXISTS idx_bank_graph_date_hour
    ON bank_graph_event (transaction_date DESC, transaction_hour DESC);

CREATE TABLE IF NOT EXISTS bank_closed_window_review (
    transaction_date DATE NOT NULL,
    transaction_hour INTEGER NOT NULL CHECK (transaction_hour >= 0 AND transaction_hour <= 21 AND MOD(transaction_hour, 3) = 0),
    reviewed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    observed_rows INTEGER NOT NULL CHECK (observed_rows > 0),
    model_version VARCHAR(64) NOT NULL,
    review_json CLOB NOT NULL,
    PRIMARY KEY (transaction_date, transaction_hour)
);
