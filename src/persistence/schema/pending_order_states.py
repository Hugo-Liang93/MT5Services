DDL = """
CREATE TABLE IF NOT EXISTS pending_order_states (
    account_alias TEXT NOT NULL,
    account_key TEXT NOT NULL,
    order_ticket BIGINT NOT NULL,
    signal_id TEXT,
    request_id TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL
        CHECK (direction IN ('buy', 'sell')),
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
    status TEXT NOT NULL
        CHECK (status IN ('placed', 'filled', 'expired', 'cancelled', 'missing', 'orphan')),
    status_reason TEXT,
    last_seen_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_key, order_ticket)
);
ALTER TABLE pending_order_states
    ADD COLUMN IF NOT EXISTS account_key TEXT;

CREATE INDEX IF NOT EXISTS idx_pending_orders_account_status
    ON pending_order_states (account_alias, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pending_orders_account_key
    ON pending_order_states (account_key, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pending_orders_signal
    ON pending_order_states (signal_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pending_orders_expires
    ON pending_order_states (status, expires_at);

CREATE INDEX IF NOT EXISTS idx_pending_orders_symbol_status
    ON pending_order_states (symbol, status);
"""

# §0u P1 迁移：把单列 PK (order_ticket) 升级为复合 PK (account_key, order_ticket)。
# 多账户拓扑下不同账户可能持有同一 MT5 ticket，旧 PK 让 upsert 跨账户互相覆盖
# → cockpit / trace / risk 投影全被污染。
# 步骤：
#   1) backfill account_key（NULL/空串 → account_alias）
#   2) ALTER COLUMN ... SET NOT NULL（PK 列不允许 NULL）
#   3) 仅在当前 PK 不是复合形式时 drop+rebuild（DO 块保证幂等）
MIGRATION_SQL = """
UPDATE pending_order_states
SET account_key = account_alias
WHERE account_key IS NULL OR account_key = '';

ALTER TABLE pending_order_states
    ALTER COLUMN account_key SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'pending_order_states'
          AND c.contype = 'p'
          AND pg_get_constraintdef(c.oid) ILIKE '%(account_key, order_ticket)'
    ) THEN
        ALTER TABLE pending_order_states
            DROP CONSTRAINT IF EXISTS pending_order_states_pkey;
        ALTER TABLE pending_order_states
            ADD CONSTRAINT pending_order_states_pkey
            PRIMARY KEY (account_key, order_ticket);
    END IF;
END $$;
"""

UPSERT_SQL = """
INSERT INTO pending_order_states (
    account_alias, order_ticket, signal_id, request_id, symbol, direction,
    strategy, timeframe, category, order_kind, comment,
    entry_low, entry_high, trigger_price, entry_price_requested,
    stop_loss, take_profit, volume, atr_at_entry, confidence, regime,
    created_at, expires_at, filled_at, cancelled_at,
    position_ticket, deal_id, fill_price,
    status, status_reason, last_seen_at, metadata, updated_at, account_key
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s, %s, %s
)
ON CONFLICT (account_key, order_ticket) DO UPDATE SET
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
