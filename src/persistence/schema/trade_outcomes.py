"""trade_outcomes 表 DDL — 记录实际交易的盈亏结果。

与 signal_outcomes 的区别：
- signal_outcomes：信号质量评估（N bars 后理论盈亏，所有信号，供 Calibrator 统计）
- trade_outcomes：实际交易结果（真实成交价，仅已执行交易，供 PerformanceTracker 反馈）
"""

DDL = """
CREATE TABLE IF NOT EXISTS trade_outcomes (
    recorded_at   timestamptz NOT NULL,
    signal_id     text NOT NULL,
    account_key   text,
    account_alias text,
    intent_id     text,
    symbol        text NOT NULL,
    timeframe     text NOT NULL,
    strategy      text NOT NULL,
    direction     text NOT NULL
                  CHECK (direction IN ('buy', 'sell')),
    confidence    double precision NOT NULL,
    fill_price    double precision,
    close_price   double precision,
    price_change  double precision,
    won           boolean,
    regime        text,
    metadata      jsonb,
    PRIMARY KEY (recorded_at, signal_id)
);
SELECT create_hypertable('trade_outcomes', 'recorded_at',
                          if_not_exists => TRUE, migrate_data => TRUE);
ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS account_key text;
ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS account_alias text;
ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS intent_id text;
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_outcomes_upsert
ON trade_outcomes (signal_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_symbol
ON trade_outcomes (symbol, timeframe, strategy, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_won
ON trade_outcomes (won, strategy, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_recent
ON trade_outcomes (recorded_at DESC) WHERE won IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_account
ON trade_outcomes (account_key, recorded_at DESC);
"""

INSERT_SQL = """
INSERT INTO trade_outcomes (
    recorded_at,
    signal_id,
    account_key,
    account_alias,
    intent_id,
    symbol,
    timeframe,
    strategy,
    direction,
    confidence,
    fill_price,
    close_price,
    price_change,
    won,
    regime,
    metadata
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (signal_id, recorded_at) DO UPDATE SET
    close_price   = EXCLUDED.close_price,
    price_change  = EXCLUDED.price_change,
    won           = EXCLUDED.won,
    metadata      = EXCLUDED.metadata
"""
