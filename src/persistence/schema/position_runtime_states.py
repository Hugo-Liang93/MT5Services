DDL = """
CREATE TABLE IF NOT EXISTS position_runtime_states (
    account_alias TEXT NOT NULL,
    position_ticket BIGINT PRIMARY KEY,
    signal_id TEXT,
    order_ticket BIGINT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL
        CHECK (direction IN ('buy', 'sell')),
    timeframe TEXT,
    strategy TEXT,
    comment TEXT,
    entry_price DOUBLE PRECISION,
    initial_stop_loss DOUBLE PRECISION,
    initial_take_profit DOUBLE PRECISION,
    current_stop_loss DOUBLE PRECISION,
    current_take_profit DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    atr_at_entry DOUBLE PRECISION,
    confidence DOUBLE PRECISION,
    regime TEXT,
    opened_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    last_managed_at TIMESTAMPTZ,
    highest_price DOUBLE PRECISION,
    lowest_price DOUBLE PRECISION,
    current_price DOUBLE PRECISION,
    breakeven_applied BOOLEAN NOT NULL DEFAULT FALSE,
    trailing_active BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL
        CHECK (status IN ('open', 'closed')),
    closed_at TIMESTAMPTZ,
    close_source TEXT,
    close_price DOUBLE PRECISION,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pos_states_account_status
    ON position_runtime_states (account_alias, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pos_states_signal
    ON position_runtime_states (signal_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pos_states_symbol_status
    ON position_runtime_states (symbol, status);
"""

UPSERT_SQL = """
INSERT INTO position_runtime_states (
    account_alias, position_ticket, signal_id, order_ticket, symbol, direction,
    timeframe, strategy, comment, entry_price,
    initial_stop_loss, initial_take_profit, current_stop_loss, current_take_profit,
    volume, atr_at_entry, confidence, regime,
    opened_at, last_seen_at, last_managed_at,
    highest_price, lowest_price, current_price,
    breakeven_applied, trailing_active,
    status, closed_at, close_source, close_price,
    metadata, updated_at
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s,
    %s, %s
)
ON CONFLICT (position_ticket) DO UPDATE SET
    account_alias = EXCLUDED.account_alias,
    signal_id = EXCLUDED.signal_id,
    order_ticket = EXCLUDED.order_ticket,
    symbol = EXCLUDED.symbol,
    direction = EXCLUDED.direction,
    timeframe = EXCLUDED.timeframe,
    strategy = EXCLUDED.strategy,
    comment = EXCLUDED.comment,
    entry_price = EXCLUDED.entry_price,
    initial_stop_loss = EXCLUDED.initial_stop_loss,
    initial_take_profit = EXCLUDED.initial_take_profit,
    current_stop_loss = EXCLUDED.current_stop_loss,
    current_take_profit = EXCLUDED.current_take_profit,
    volume = EXCLUDED.volume,
    atr_at_entry = EXCLUDED.atr_at_entry,
    confidence = EXCLUDED.confidence,
    regime = EXCLUDED.regime,
    opened_at = EXCLUDED.opened_at,
    last_seen_at = EXCLUDED.last_seen_at,
    last_managed_at = EXCLUDED.last_managed_at,
    highest_price = EXCLUDED.highest_price,
    lowest_price = EXCLUDED.lowest_price,
    current_price = EXCLUDED.current_price,
    breakeven_applied = EXCLUDED.breakeven_applied,
    trailing_active = EXCLUDED.trailing_active,
    status = EXCLUDED.status,
    closed_at = EXCLUDED.closed_at,
    close_source = EXCLUDED.close_source,
    close_price = EXCLUDED.close_price,
    metadata = EXCLUDED.metadata,
    updated_at = EXCLUDED.updated_at
"""
