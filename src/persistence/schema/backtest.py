"""回测结果持久化 Schema。"""

DDL = """
-- 回测运行记录
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    config JSONB NOT NULL,
    param_set JSONB NOT NULL,
    metrics JSONB NOT NULL,
    metrics_by_regime JSONB,
    metrics_by_strategy JSONB,
    equity_curve JSONB,
    status TEXT NOT NULL DEFAULT 'completed',
    duration_ms INTEGER
);

-- 回测交易记录
CREATE TABLE IF NOT EXISTS backtest_trades (
    id SERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    strategy TEXT NOT NULL,
    action TEXT NOT NULL,
    entry_time TIMESTAMPTZ NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    exit_time TIMESTAMPTZ NOT NULL,
    exit_price DOUBLE PRECISION NOT NULL,
    stop_loss DOUBLE PRECISION,
    take_profit DOUBLE PRECISION,
    position_size DOUBLE PRECISION,
    pnl DOUBLE PRECISION NOT NULL,
    bars_held INTEGER,
    regime TEXT,
    confidence DOUBLE PRECISION,
    exit_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id);
CREATE INDEX IF NOT EXISTS idx_bt_runs_created ON backtest_runs(created_at DESC);
"""

INSERT_RUN_SQL = """
INSERT INTO backtest_runs (run_id, created_at, config, param_set, metrics,
                           metrics_by_regime, metrics_by_strategy, equity_curve,
                           status, duration_ms)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (run_id) DO NOTHING
"""

INSERT_TRADE_SQL = """
INSERT INTO backtest_trades (run_id, strategy, action, entry_time, entry_price,
                             exit_time, exit_price, stop_loss, take_profit,
                             position_size, pnl, bars_held, regime, confidence,
                             exit_reason)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
