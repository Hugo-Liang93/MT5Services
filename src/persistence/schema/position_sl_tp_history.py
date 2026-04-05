DDL = """
CREATE TABLE IF NOT EXISTS position_sl_tp_history (
    id BIGSERIAL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_alias TEXT NOT NULL,
    position_ticket BIGINT NOT NULL,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    action_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    old_stop_loss DOUBLE PRECISION,
    new_stop_loss DOUBLE PRECISION,
    old_take_profit DOUBLE PRECISION,
    new_take_profit DOUBLE PRECISION,
    current_price DOUBLE PRECISION,
    highest_price DOUBLE PRECISION,
    lowest_price DOUBLE PRECISION,
    atr_at_entry DOUBLE PRECISION,
    success BOOLEAN NOT NULL,
    retcode INTEGER,
    broker_comment TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, recorded_at)
);

SELECT create_hypertable('position_sl_tp_history', 'recorded_at',
    if_not_exists => TRUE, migrate_data => TRUE);

CREATE INDEX IF NOT EXISTS idx_sl_tp_history_ticket
    ON position_sl_tp_history (position_ticket, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_sl_tp_history_symbol
    ON position_sl_tp_history (symbol, recorded_at DESC);
"""

INSERT_SQL = """
INSERT INTO position_sl_tp_history (
    recorded_at, account_alias, position_ticket, signal_id, symbol,
    action_type, reason,
    old_stop_loss, new_stop_loss, old_take_profit, new_take_profit,
    current_price, highest_price, lowest_price, atr_at_entry,
    success, retcode, broker_comment, metadata
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s
)
"""
