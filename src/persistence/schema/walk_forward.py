"""Walk-Forward 验证结果持久化 Schema（P11 Phase 2）。

两张表：
- `backtest_walk_forward_runs`：单次 WF 任务元数据（聚合指标 + 状态）
- `backtest_walk_forward_windows`：每个 split 的 IS/OOS 分段结果

关键关系：
- `backtest_run_id` 外键指向 `backtest_runs.run_id`（可为空：独立 WF 任务时）
- `wf_run_id` 是 WF 自身 ID（execute_walk_forward 生成），不与 backtest_runs 主键冲突
- windows 表 ON DELETE CASCADE 跟随 runs 表清理

字段设计说明：
- `aggregate_metrics` JSONB 保留完整 BacktestMetrics（未来加字段不改表）
- 同时冗余 `oos_sharpe / oos_win_rate / oos_total_trades` 常用排序字段以避免 JSONB 解析
- 每个 window 同时存 `is_metrics / oos_metrics` JSONB + `oos_pnl / oos_win_rate` 冗余字段
"""

DDL = """
-- Walk-Forward 任务记录
CREATE TABLE IF NOT EXISTS backtest_walk_forward_runs (
    wf_run_id TEXT PRIMARY KEY,
    backtest_run_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    config JSONB NOT NULL,
    aggregate_metrics JSONB,
    overfitting_ratio DOUBLE PRECISION,
    consistency_rate DOUBLE PRECISION,
    oos_sharpe DOUBLE PRECISION,
    oos_win_rate DOUBLE PRECISION,
    oos_total_trades INTEGER,
    oos_total_pnl DOUBLE PRECISION,
    n_splits INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed'
        CHECK (status IN ('running', 'completed', 'failed')),
    duration_ms INTEGER,
    error TEXT,
    experiment_id TEXT
);

-- Walk-Forward 每窗口结果
CREATE TABLE IF NOT EXISTS backtest_walk_forward_windows (
    id BIGSERIAL PRIMARY KEY,
    wf_run_id TEXT NOT NULL
        REFERENCES backtest_walk_forward_runs(wf_run_id) ON DELETE CASCADE,
    split_index INTEGER NOT NULL,
    window_label TEXT NOT NULL,
    train_start TIMESTAMPTZ,
    train_end TIMESTAMPTZ,
    test_start TIMESTAMPTZ,
    test_end TIMESTAMPTZ,
    best_params JSONB,
    is_metrics JSONB,
    oos_metrics JSONB,
    is_pnl DOUBLE PRECISION,
    oos_pnl DOUBLE PRECISION,
    is_sharpe DOUBLE PRECISION,
    oos_sharpe DOUBLE PRECISION,
    is_win_rate DOUBLE PRECISION,
    oos_win_rate DOUBLE PRECISION,
    oos_max_drawdown DOUBLE PRECISION,
    oos_trade_count INTEGER,
    UNIQUE (wf_run_id, split_index)
);

CREATE INDEX IF NOT EXISTS idx_wf_runs_backtest
    ON backtest_walk_forward_runs(backtest_run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wf_runs_created
    ON backtest_walk_forward_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wf_runs_experiment
    ON backtest_walk_forward_runs(experiment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wf_windows_run
    ON backtest_walk_forward_windows(wf_run_id, split_index);
"""


INSERT_RUN_SQL = """
INSERT INTO backtest_walk_forward_runs (
    wf_run_id, backtest_run_id, created_at, completed_at, config,
    aggregate_metrics, overfitting_ratio, consistency_rate,
    oos_sharpe, oos_win_rate, oos_total_trades, oos_total_pnl,
    n_splits, status, duration_ms, error, experiment_id
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (wf_run_id) DO UPDATE SET
    backtest_run_id = EXCLUDED.backtest_run_id,
    completed_at = EXCLUDED.completed_at,
    aggregate_metrics = EXCLUDED.aggregate_metrics,
    overfitting_ratio = EXCLUDED.overfitting_ratio,
    consistency_rate = EXCLUDED.consistency_rate,
    oos_sharpe = EXCLUDED.oos_sharpe,
    oos_win_rate = EXCLUDED.oos_win_rate,
    oos_total_trades = EXCLUDED.oos_total_trades,
    oos_total_pnl = EXCLUDED.oos_total_pnl,
    n_splits = EXCLUDED.n_splits,
    status = EXCLUDED.status,
    duration_ms = EXCLUDED.duration_ms,
    error = EXCLUDED.error,
    experiment_id = EXCLUDED.experiment_id
"""


INSERT_WINDOW_SQL = """
INSERT INTO backtest_walk_forward_windows (
    wf_run_id, split_index, window_label, train_start, train_end,
    test_start, test_end, best_params, is_metrics, oos_metrics,
    is_pnl, oos_pnl, is_sharpe, oos_sharpe,
    is_win_rate, oos_win_rate, oos_max_drawdown, oos_trade_count
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (wf_run_id, split_index) DO UPDATE SET
    window_label = EXCLUDED.window_label,
    train_start = EXCLUDED.train_start,
    train_end = EXCLUDED.train_end,
    test_start = EXCLUDED.test_start,
    test_end = EXCLUDED.test_end,
    best_params = EXCLUDED.best_params,
    is_metrics = EXCLUDED.is_metrics,
    oos_metrics = EXCLUDED.oos_metrics,
    is_pnl = EXCLUDED.is_pnl,
    oos_pnl = EXCLUDED.oos_pnl,
    is_sharpe = EXCLUDED.is_sharpe,
    oos_sharpe = EXCLUDED.oos_sharpe,
    is_win_rate = EXCLUDED.is_win_rate,
    oos_win_rate = EXCLUDED.oos_win_rate,
    oos_max_drawdown = EXCLUDED.oos_max_drawdown,
    oos_trade_count = EXCLUDED.oos_trade_count
"""


FETCH_RUN_SQL = """
SELECT wf_run_id, backtest_run_id, created_at, completed_at, config,
       aggregate_metrics, overfitting_ratio, consistency_rate,
       oos_sharpe, oos_win_rate, oos_total_trades, oos_total_pnl,
       n_splits, status, duration_ms, error, experiment_id
FROM backtest_walk_forward_runs
WHERE wf_run_id = %s
"""


FETCH_LATEST_BY_BACKTEST_RUN_SQL = """
SELECT wf_run_id, backtest_run_id, created_at, completed_at, config,
       aggregate_metrics, overfitting_ratio, consistency_rate,
       oos_sharpe, oos_win_rate, oos_total_trades, oos_total_pnl,
       n_splits, status, duration_ms, error, experiment_id
FROM backtest_walk_forward_runs
WHERE backtest_run_id = %s
ORDER BY created_at DESC
LIMIT 1
"""


FETCH_WINDOWS_SQL = """
SELECT split_index, window_label, train_start, train_end,
       test_start, test_end, best_params, is_metrics, oos_metrics,
       is_pnl, oos_pnl, is_sharpe, oos_sharpe,
       is_win_rate, oos_win_rate, oos_max_drawdown, oos_trade_count
FROM backtest_walk_forward_windows
WHERE wf_run_id = %s
ORDER BY split_index ASC
"""


LIST_RUNS_SQL = """
SELECT wf_run_id, backtest_run_id, created_at, completed_at,
       overfitting_ratio, consistency_rate,
       oos_sharpe, oos_win_rate, oos_total_trades, oos_total_pnl,
       n_splits, status, duration_ms, experiment_id
FROM backtest_walk_forward_runs
{where_clause}
ORDER BY created_at DESC
LIMIT %s OFFSET %s
"""
