"""trade_outcomes 表 DDL — 记录实际交易的盈亏结果。

与 signal_outcomes 的区别：
- signal_outcomes：信号质量评估（N bars 后理论盈亏，所有信号，供 Calibrator 统计）
- trade_outcomes：实际交易结果（真实成交价，仅已执行交易，供 PerformanceTracker 反馈）
"""

DDL = """
-- Migration: 旧表 PK 为 (signal_id)，不含分区键 recorded_at，需重建为 hypertable
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_constraint co ON co.conrelid = c.oid
        WHERE c.relname = 'trade_outcomes' AND co.conname = 'trade_outcomes_pkey'
    ) AND NOT EXISTS (
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'trade_outcomes'
    ) THEN
        DROP TABLE trade_outcomes;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS trade_outcomes (
    recorded_at   timestamptz NOT NULL,
    signal_id     text NOT NULL,
    symbol        text NOT NULL,
    timeframe     text NOT NULL,
    strategy      text NOT NULL,
    direction     text NOT NULL,
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
CREATE UNIQUE INDEX IF NOT EXISTS trade_outcomes_upsert_idx
ON trade_outcomes (signal_id, recorded_at);
CREATE INDEX IF NOT EXISTS trade_outcomes_symbol_idx
ON trade_outcomes (symbol, timeframe, strategy, recorded_at DESC);
CREATE INDEX IF NOT EXISTS trade_outcomes_won_idx
ON trade_outcomes (won, strategy, recorded_at DESC);
"""

INSERT_SQL = """
INSERT INTO trade_outcomes (
    recorded_at,
    signal_id,
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
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (signal_id, recorded_at) DO UPDATE SET
    close_price   = EXCLUDED.close_price,
    price_change  = EXCLUDED.price_change,
    won           = EXCLUDED.won,
    metadata      = EXCLUDED.metadata
"""
