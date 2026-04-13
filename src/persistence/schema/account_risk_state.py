DDL = """
CREATE TABLE IF NOT EXISTS account_risk_state (
    account_key TEXT PRIMARY KEY,
    account_alias TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    instance_role TEXT NOT NULL,
    runtime_mode TEXT,
    auto_entry_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    close_only_mode BOOLEAN NOT NULL DEFAULT FALSE,
    circuit_open BOOLEAN NOT NULL DEFAULT FALSE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_risk_block TEXT,
    margin_level DOUBLE PRECISION,
    margin_guard_state TEXT,
    should_block_new_trades BOOLEAN NOT NULL DEFAULT FALSE,
    should_tighten_stops BOOLEAN NOT NULL DEFAULT FALSE,
    should_emergency_close BOOLEAN NOT NULL DEFAULT FALSE,
    open_positions_count INTEGER NOT NULL DEFAULT 0,
    pending_orders_count INTEGER NOT NULL DEFAULT 0,
    quote_stale BOOLEAN NOT NULL DEFAULT FALSE,
    indicator_degraded BOOLEAN NOT NULL DEFAULT FALSE,
    db_degraded BOOLEAN NOT NULL DEFAULT FALSE,
    active_risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_risk_state_updated
    ON account_risk_state (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_risk_state_alias
    ON account_risk_state (account_alias);
CREATE INDEX IF NOT EXISTS idx_account_risk_state_instance
    ON account_risk_state (instance_id, updated_at DESC);
"""

UPSERT_SQL = """
INSERT INTO account_risk_state (
    account_key,
    account_alias,
    instance_id,
    instance_role,
    runtime_mode,
    auto_entry_enabled,
    close_only_mode,
    circuit_open,
    consecutive_failures,
    last_risk_block,
    margin_level,
    margin_guard_state,
    should_block_new_trades,
    should_tighten_stops,
    should_emergency_close,
    open_positions_count,
    pending_orders_count,
    quote_stale,
    indicator_degraded,
    db_degraded,
    active_risk_flags,
    metadata,
    updated_at
)
VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (account_key) DO UPDATE SET
    account_alias = EXCLUDED.account_alias,
    instance_id = EXCLUDED.instance_id,
    instance_role = EXCLUDED.instance_role,
    runtime_mode = EXCLUDED.runtime_mode,
    auto_entry_enabled = EXCLUDED.auto_entry_enabled,
    close_only_mode = EXCLUDED.close_only_mode,
    circuit_open = EXCLUDED.circuit_open,
    consecutive_failures = EXCLUDED.consecutive_failures,
    last_risk_block = EXCLUDED.last_risk_block,
    margin_level = EXCLUDED.margin_level,
    margin_guard_state = EXCLUDED.margin_guard_state,
    should_block_new_trades = EXCLUDED.should_block_new_trades,
    should_tighten_stops = EXCLUDED.should_tighten_stops,
    should_emergency_close = EXCLUDED.should_emergency_close,
    open_positions_count = EXCLUDED.open_positions_count,
    pending_orders_count = EXCLUDED.pending_orders_count,
    quote_stale = EXCLUDED.quote_stale,
    indicator_degraded = EXCLUDED.indicator_degraded,
    db_degraded = EXCLUDED.db_degraded,
    active_risk_flags = EXCLUDED.active_risk_flags,
    metadata = EXCLUDED.metadata,
    updated_at = EXCLUDED.updated_at
"""
