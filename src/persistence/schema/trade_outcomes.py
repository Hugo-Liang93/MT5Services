"""trade_outcomes 表 DDL — 记录实际交易的盈亏结果。

与 signal_outcomes 的区别：
- signal_outcomes：信号质量评估（N bars 后理论盈亏，所有信号，供 Calibrator 统计）
- trade_outcomes：实际交易结果（真实成交价，仅已执行交易，供 PerformanceTracker 反馈）
"""

DDL = """
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
    PRIMARY KEY (signal_id)
);
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
ON CONFLICT (signal_id) DO UPDATE SET
    close_price   = EXCLUDED.close_price,
    price_change  = EXCLUDED.price_change,
    won           = EXCLUDED.won,
    metadata      = EXCLUDED.metadata
"""
