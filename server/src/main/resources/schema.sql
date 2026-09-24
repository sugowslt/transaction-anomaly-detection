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
