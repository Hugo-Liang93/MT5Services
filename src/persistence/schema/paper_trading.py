"""Paper Trading 独立数据表 Schema。

三张独立表，与实盘 trade_outcomes / signal_outcomes 完全分离。
"""

DDL = """
-- Paper Trading Sessions
CREATE TABLE IF NOT EXISTS paper_trading_sessions (
    session_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    stopped_at TIMESTAMPTZ,
    initial_balance DOUBLE PRECISION NOT NULL,
    final_balance DOUBLE PRECISION,
    config_snapshot JSONB NOT NULL DEFAULT '{}',
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    total_pnl DOUBLE PRECISION DEFAULT 0.0,
    max_drawdown_pct DOUBLE PRECISION DEFAULT 0.0,
    sharpe_ratio DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_paper_sessions_started
    ON paper_trading_sessions(started_at DESC);

-- Paper Trade Outcomes（已平仓交易记录）
CREATE TABLE IF NOT EXISTS paper_trade_outcomes (
    trade_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES paper_trading_sessions(session_id) ON DELETE CASCADE,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL
        CHECK (direction IN ('buy', 'sell')),
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    entry_time TIMESTAMPTZ NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    exit_time TIMESTAMPTZ,
    exit_price DOUBLE PRECISION,
    stop_loss DOUBLE PRECISION,
    take_profit DOUBLE PRECISION,
    position_size DOUBLE PRECISION,
    confidence DOUBLE PRECISION,
    regime TEXT,
    signal_id TEXT,
    pnl DOUBLE PRECISION,
    pnl_pct DOUBLE PRECISION,
    exit_reason TEXT,
    bars_held INTEGER DEFAULT 0,
    slippage_cost DOUBLE PRECISION DEFAULT 0.0,
    commission_cost DOUBLE PRECISION DEFAULT 0.0,
    max_favorable_excursion DOUBLE PRECISION DEFAULT 0.0,
    max_adverse_excursion DOUBLE PRECISION DEFAULT 0.0,
    breakeven_activated BOOLEAN DEFAULT FALSE,
    trailing_activated BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_session
    ON paper_trade_outcomes(session_id);
CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy
    ON paper_trade_outcomes(strategy, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol
    ON paper_trade_outcomes(symbol, entry_time DESC);
"""

UPSERT_SESSION_SQL = """
INSERT INTO paper_trading_sessions (
    session_id, started_at, stopped_at, initial_balance, final_balance,
    config_snapshot, total_trades, winning_trades, losing_trades,
    total_pnl, max_drawdown_pct, sharpe_ratio
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (session_id) DO UPDATE SET
    stopped_at = EXCLUDED.stopped_at,
    final_balance = EXCLUDED.final_balance,
    total_trades = EXCLUDED.total_trades,
    winning_trades = EXCLUDED.winning_trades,
    losing_trades = EXCLUDED.losing_trades,
    total_pnl = EXCLUDED.total_pnl,
    max_drawdown_pct = EXCLUDED.max_drawdown_pct,
    sharpe_ratio = EXCLUDED.sharpe_ratio
"""

INSERT_TRADE_SQL = """
INSERT INTO paper_trade_outcomes (
    trade_id, session_id, strategy, direction, symbol, timeframe,
    entry_time, entry_price, exit_time, exit_price,
    stop_loss, take_profit, position_size, confidence, regime, signal_id,
    pnl, pnl_pct, exit_reason, bars_held,
    slippage_cost, commission_cost,
    max_favorable_excursion, max_adverse_excursion,
    breakeven_activated, trailing_activated
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trade_id) DO UPDATE SET
    exit_time = EXCLUDED.exit_time,
    exit_price = EXCLUDED.exit_price,
    stop_loss = EXCLUDED.stop_loss,
    take_profit = EXCLUDED.take_profit,
    pnl = EXCLUDED.pnl,
    pnl_pct = EXCLUDED.pnl_pct,
    exit_reason = EXCLUDED.exit_reason,
    bars_held = EXCLUDED.bars_held,
    slippage_cost = EXCLUDED.slippage_cost,
    commission_cost = EXCLUDED.commission_cost,
    max_favorable_excursion = EXCLUDED.max_favorable_excursion,
    max_adverse_excursion = EXCLUDED.max_adverse_excursion,
    breakeven_activated = EXCLUDED.breakeven_activated,
    trailing_activated = EXCLUDED.trailing_activated
"""
