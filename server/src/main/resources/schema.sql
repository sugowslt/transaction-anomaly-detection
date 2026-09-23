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
    model_version VARCHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bank_decision_alert_created
    ON bank_decision (alert, created_at DESC);
