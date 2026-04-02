DDL = """
CREATE TABLE IF NOT EXISTS pending_order_states (
    account_alias TEXT NOT NULL,
    order_ticket BIGINT PRIMARY KEY,
    signal_id TEXT,
    request_id TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT,
    timeframe TEXT,
    category TEXT,
    order_kind TEXT,
    comment TEXT,
    entry_low DOUBLE PRECISION,
    entry_high DOUBLE PRECISION,
    trigger_price DOUBLE PRECISION,
    entry_price_requested DOUBLE PRECISION,
    stop_loss DOUBLE PRECISION,
    take_profit DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    atr_at_entry DOUBLE PRECISION,
    confidence DOUBLE PRECISION,
    regime TEXT,
    created_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    filled_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    position_ticket BIGINT,
    deal_id BIGINT,
    fill_price DOUBLE PRECISION,
    status TEXT NOT NULL,
    status_reason TEXT,
    last_seen_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS pending_order_states_account_status_idx
ON pending_order_states (account_alias, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS pending_order_states_signal_idx
ON pending_order_states (signal_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS pending_order_states_expires_idx
ON pending_order_states (status, expires_at);
"""

UPSERT_SQL = """
INSERT INTO pending_order_states (
    account_alias, order_ticket, signal_id, request_id, symbol, direction,
    strategy, timeframe, category, order_kind, comment,
    entry_low, entry_high, trigger_price, entry_price_requested,
    stop_loss, take_profit, volume, atr_at_entry, confidence, regime,
    created_at, expires_at, filled_at, cancelled_at,
    position_ticket, deal_id, fill_price,
    status, status_reason, last_seen_at, metadata, updated_at
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s, %s
)
ON CONFLICT (order_ticket) DO UPDATE SET
    account_alias = EXCLUDED.account_alias,
    signal_id = EXCLUDED.signal_id,
    request_id = EXCLUDED.request_id,
    symbol = EXCLUDED.symbol,
    direction = EXCLUDED.direction,
    strategy = EXCLUDED.strategy,
    timeframe = EXCLUDED.timeframe,
    category = EXCLUDED.category,
    order_kind = EXCLUDED.order_kind,
    comment = EXCLUDED.comment,
    entry_low = EXCLUDED.entry_low,
    entry_high = EXCLUDED.entry_high,
    trigger_price = EXCLUDED.trigger_price,
    entry_price_requested = EXCLUDED.entry_price_requested,
    stop_loss = EXCLUDED.stop_loss,
    take_profit = EXCLUDED.take_profit,
    volume = EXCLUDED.volume,
    atr_at_entry = EXCLUDED.atr_at_entry,
    confidence = EXCLUDED.confidence,
    regime = EXCLUDED.regime,
    created_at = EXCLUDED.created_at,
    expires_at = EXCLUDED.expires_at,
    filled_at = EXCLUDED.filled_at,
    cancelled_at = EXCLUDED.cancelled_at,
    position_ticket = EXCLUDED.position_ticket,
    deal_id = EXCLUDED.deal_id,
    fill_price = EXCLUDED.fill_price,
    status = EXCLUDED.status,
    status_reason = EXCLUDED.status_reason,
    last_seen_at = EXCLUDED.last_seen_at,
    metadata = EXCLUDED.metadata,
    updated_at = EXCLUDED.updated_at
"""
