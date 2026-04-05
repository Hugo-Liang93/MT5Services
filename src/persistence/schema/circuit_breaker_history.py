"""熔断器状态变更历史表。

记录交易熔断器的每次触发/重置事件，用于排查和审计。
"""

DDL = """
CREATE TABLE IF NOT EXISTS circuit_breaker_history (
    recorded_at TIMESTAMPTZ NOT NULL,
    account_alias TEXT NOT NULL,
    breaker_type TEXT NOT NULL
        CHECK (breaker_type IN ('executor', 'pnl', 'frequency', 'margin')),
    event TEXT NOT NULL
        CHECK (event IN ('tripped', 'reset', 'auto_reset')),
    consecutive_failures INTEGER DEFAULT 0,
    reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (recorded_at, account_alias, breaker_type)
);

SELECT create_hypertable('circuit_breaker_history', 'recorded_at',
       if_not_exists => TRUE, migrate_data => TRUE);

CREATE INDEX IF NOT EXISTS idx_cb_history_account
    ON circuit_breaker_history(account_alias, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_cb_history_type_event
    ON circuit_breaker_history(breaker_type, event, recorded_at DESC);
"""

INSERT_SQL = """
INSERT INTO circuit_breaker_history (
    recorded_at, account_alias, breaker_type, event,
    consecutive_failures, reason, metadata
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""
