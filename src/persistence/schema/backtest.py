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
    duration_ms INTEGER,
    filter_stats JSONB
);

-- 回测交易记录
CREATE TABLE IF NOT EXISTS backtest_trades (
    id SERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_time TIMESTAMPTZ NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    exit_time TIMESTAMPTZ NOT NULL,
    exit_price DOUBLE PRECISION NOT NULL,
    stop_loss DOUBLE PRECISION,
    take_profit DOUBLE PRECISION,
    position_size DOUBLE PRECISION,
    pnl DOUBLE PRECISION NOT NULL,
    pnl_pct DOUBLE PRECISION,
    bars_held INTEGER,
    regime TEXT,
    confidence DOUBLE PRECISION,
    exit_reason TEXT,
    slippage_cost DOUBLE PRECISION DEFAULT 0.0,
    commission_cost DOUBLE PRECISION DEFAULT 0.0
);

-- 回测信号评估记录（对应实盘 signal_outcomes 表）
CREATE TABLE IF NOT EXISTS backtest_signal_evaluations (
    id SERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    bar_time TIMESTAMPTZ NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    regime TEXT,
    price_at_signal DOUBLE PRECISION NOT NULL,
    price_after_n_bars DOUBLE PRECISION,
    bars_to_evaluate INTEGER DEFAULT 5,
    won BOOLEAN,
    pnl_pct DOUBLE PRECISION,
    filtered BOOLEAN NOT NULL DEFAULT FALSE,
    filter_reason TEXT,
    incomplete BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id);
CREATE INDEX IF NOT EXISTS idx_bt_runs_created ON backtest_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bt_evals_run ON backtest_signal_evaluations(run_id);
CREATE INDEX IF NOT EXISTS idx_bt_evals_strategy ON backtest_signal_evaluations(run_id, strategy);
CREATE INDEX IF NOT EXISTS idx_bt_evals_filtered ON backtest_signal_evaluations(run_id, filtered);
"""

INSERT_RUN_SQL = """
INSERT INTO backtest_runs (run_id, created_at, config, param_set, metrics,
                           metrics_by_regime, metrics_by_strategy, equity_curve,
                           status, duration_ms, filter_stats)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (run_id) DO NOTHING
"""

INSERT_TRADE_SQL = """
INSERT INTO backtest_trades (run_id, strategy, direction, entry_time, entry_price,
                             exit_time, exit_price, stop_loss, take_profit,
                             position_size, pnl, pnl_pct, bars_held, regime,
                             confidence, exit_reason, slippage_cost, commission_cost)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

INSERT_EVALUATION_SQL = """
INSERT INTO backtest_signal_evaluations (run_id, bar_time, strategy, direction,
                                         confidence, regime, price_at_signal,
                                         price_after_n_bars, bars_to_evaluate,
                                         won, pnl_pct, filtered, filter_reason,
                                         incomplete)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
