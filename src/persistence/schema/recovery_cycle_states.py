"""Recovery / martingale cycle runtime state."""

DDL = """
CREATE TABLE IF NOT EXISTS recovery_cycle_states (
    account_alias TEXT NOT NULL,
    account_key TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('buy', 'sell')),
    strategy TEXT NOT NULL DEFAULT '',
    timeframe TEXT NOT NULL DEFAULT '',
    source_signal_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('open', 'closed', 'blocked')),
    status_reason TEXT,
    base_volume DOUBLE PRECISION NOT NULL,
    total_volume DOUBLE PRECISION NOT NULL,
    step_count INTEGER NOT NULL,
    average_entry_price DOUBLE PRECISION NOT NULL,
    last_entry_price DOUBLE PRECISION NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    last_step_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    close_price DOUBLE PRECISION,
    realized_pnl DOUBLE PRECISION,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_key, cycle_id)
);

CREATE INDEX IF NOT EXISTS idx_recovery_cycle_states_account_status
ON recovery_cycle_states (account_key, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_recovery_cycle_states_account_symbol_status
ON recovery_cycle_states (account_key, symbol, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_recovery_cycle_states_source_signal
ON recovery_cycle_states (source_signal_id, updated_at DESC)
WHERE source_signal_id IS NOT NULL;
"""

UPSERT_SQL = """
INSERT INTO recovery_cycle_states (
    account_alias, account_key, cycle_id, symbol, direction,
    strategy, timeframe, source_signal_id, status, status_reason,
    base_volume, total_volume, step_count, average_entry_price,
    last_entry_price, started_at, last_step_at, closed_at,
    close_price, realized_pnl, metadata, updated_at
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s
)
ON CONFLICT (account_key, cycle_id) DO UPDATE SET
    account_alias = EXCLUDED.account_alias,
    symbol = EXCLUDED.symbol,
    direction = EXCLUDED.direction,
    strategy = EXCLUDED.strategy,
    timeframe = EXCLUDED.timeframe,
    source_signal_id = EXCLUDED.source_signal_id,
    status = EXCLUDED.status,
    status_reason = EXCLUDED.status_reason,
    base_volume = EXCLUDED.base_volume,
    total_volume = EXCLUDED.total_volume,
    step_count = EXCLUDED.step_count,
    average_entry_price = EXCLUDED.average_entry_price,
    last_entry_price = EXCLUDED.last_entry_price,
    started_at = EXCLUDED.started_at,
    last_step_at = EXCLUDED.last_step_at,
    closed_at = EXCLUDED.closed_at,
    close_price = EXCLUDED.close_price,
    realized_pnl = EXCLUDED.realized_pnl,
    metadata = EXCLUDED.metadata,
    updated_at = EXCLUDED.updated_at
"""
