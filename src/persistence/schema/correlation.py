"""策略相关性分析结果持久化 Schema（P11 Phase 3）。

表：`backtest_correlation_analyses` —— 每次 `POST /backtest/results/{run_id}/correlation-analysis`
触发的计算结果都会生成一条新记录（按 analysis_id 主键，允许同一 backtest_run
多次分析历史）。

字段设计：
- `analysis_id`：UUID hex 主键（与现有 run_id / rec_id 生成风格一致）
- `backtest_run_id`：关联 backtest_runs.run_id（必填 —— 总对应某次回测）
- `correlation_threshold / penalty_weight`：调用参数快照
- `pairs / high_correlation_pairs / strategy_weights`：完整结果 JSONB
- 冗余 `high_correlation_count` 便于列表快速排序和前端展示

不直接把 FK 设到 backtest_runs —— 允许 backtest_runs 删除后 correlation 记录
独立保留（作为历史研究足迹）。如需联动删除可通过后台任务完成。
"""

DDL = """
CREATE TABLE IF NOT EXISTS backtest_correlation_analyses (
    analysis_id TEXT PRIMARY KEY,
    backtest_run_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    correlation_threshold DOUBLE PRECISION NOT NULL,
    penalty_weight DOUBLE PRECISION NOT NULL,
    total_bars_analyzed INTEGER NOT NULL,
    strategies_analyzed INTEGER NOT NULL,
    high_correlation_count INTEGER NOT NULL DEFAULT 0,
    pairs JSONB NOT NULL,
    high_correlation_pairs JSONB,
    strategy_weights JSONB,
    summary JSONB
);

CREATE INDEX IF NOT EXISTS idx_correlation_backtest_run
    ON backtest_correlation_analyses(backtest_run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_correlation_created
    ON backtest_correlation_analyses(created_at DESC);
"""


INSERT_SQL = """
INSERT INTO backtest_correlation_analyses (
    analysis_id, backtest_run_id, created_at,
    correlation_threshold, penalty_weight,
    total_bars_analyzed, strategies_analyzed, high_correlation_count,
    pairs, high_correlation_pairs, strategy_weights, summary
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (analysis_id) DO NOTHING
"""


FETCH_BY_ID_SQL = """
SELECT analysis_id, backtest_run_id, created_at,
       correlation_threshold, penalty_weight,
       total_bars_analyzed, strategies_analyzed, high_correlation_count,
       pairs, high_correlation_pairs, strategy_weights, summary
FROM backtest_correlation_analyses
WHERE analysis_id = %s
"""


FETCH_LATEST_BY_RUN_SQL = """
SELECT analysis_id, backtest_run_id, created_at,
       correlation_threshold, penalty_weight,
       total_bars_analyzed, strategies_analyzed, high_correlation_count,
       pairs, high_correlation_pairs, strategy_weights, summary
FROM backtest_correlation_analyses
WHERE backtest_run_id = %s
ORDER BY created_at DESC
LIMIT 1
"""


LIST_BY_RUN_SQL = """
SELECT analysis_id, backtest_run_id, created_at,
       correlation_threshold, penalty_weight,
       total_bars_analyzed, strategies_analyzed, high_correlation_count,
       pairs, high_correlation_pairs, strategy_weights, summary
FROM backtest_correlation_analyses
WHERE backtest_run_id = %s
ORDER BY created_at DESC
LIMIT %s OFFSET %s
"""
