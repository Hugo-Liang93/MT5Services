DDL = """
CREATE TABLE IF NOT EXISTS position_runtime_states (
    account_alias TEXT NOT NULL,
    account_key TEXT NOT NULL,
    position_ticket BIGINT NOT NULL,
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_key, position_ticket)
);
ALTER TABLE position_runtime_states
    ADD COLUMN IF NOT EXISTS account_key TEXT;

CREATE INDEX IF NOT EXISTS idx_pos_states_account_status
    ON position_runtime_states (account_alias, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pos_states_account_key
    ON position_runtime_states (account_key, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pos_states_signal
    ON position_runtime_states (signal_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pos_states_symbol_status
    ON position_runtime_states (symbol, status);
"""

# §0u P1 迁移：把单列 PK (position_ticket) 升级为复合 PK
# (account_key, position_ticket)。多账户拓扑下不同账户可能持有同一 MT5 ticket，
# 旧 PK 让 upsert 跨账户互相覆盖 → cockpit / trace / risk 投影全被污染。
# 步骤：
#   1) backfill account_key（NULL/空串 → account_alias）
#   2) ALTER COLUMN ... SET NOT NULL（PK 列不允许 NULL）
#   3) 仅在当前 PK 不是复合形式时 drop+rebuild（DO 块保证幂等）
MIGRATION_SQL = """
UPDATE position_runtime_states
SET account_key = account_alias
WHERE account_key IS NULL OR account_key = '';

ALTER TABLE position_runtime_states
    ALTER COLUMN account_key SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'position_runtime_states'
          AND c.contype = 'p'
          AND pg_get_constraintdef(c.oid) ILIKE '%(account_key, position_ticket)'
    ) THEN
        ALTER TABLE position_runtime_states
            DROP CONSTRAINT IF EXISTS position_runtime_states_pkey;
        ALTER TABLE position_runtime_states
            ADD CONSTRAINT position_runtime_states_pkey
            PRIMARY KEY (account_key, position_ticket);
    END IF;
END $$;
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
    metadata, updated_at, account_key
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s
)
ON CONFLICT (account_key, position_ticket) DO UPDATE SET
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
