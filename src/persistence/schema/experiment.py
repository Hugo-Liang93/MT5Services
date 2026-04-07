"""实验追踪 Schema。"""

DDL = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'research'
        CHECK (status IN ('research', 'backtest', 'paper_trading', 'live', 'abandoned')),
    symbol TEXT,
    timeframe TEXT,
    -- 各阶段引用（随阶段推进填充）
    mining_run_id TEXT,
    backtest_run_ids TEXT[] DEFAULT '{}',
    recommendation_id TEXT,
    paper_session_id TEXT,
    -- 各阶段关键指标快照
    backtest_sharpe DOUBLE PRECISION,
    backtest_win_rate DOUBLE PRECISION,
    paper_sharpe DOUBLE PRECISION,
    paper_win_rate DOUBLE PRECISION,
    validation_passed BOOLEAN,
    notes TEXT
);
"""

INSERT_EXPERIMENT_SQL = """
INSERT INTO experiments (experiment_id, status, symbol, timeframe)
VALUES (%s, %s, %s, %s)
ON CONFLICT (experiment_id) DO NOTHING;
"""

UPDATE_STATUS_SQL = """
UPDATE experiments SET status = %s, updated_at = NOW()
WHERE experiment_id = %s;
"""

ADVANCE_TO_BACKTEST_SQL = """
UPDATE experiments
SET status = 'backtest',
    backtest_run_ids = array_append(backtest_run_ids, %s),
    updated_at = NOW()
WHERE experiment_id = %s;
"""

ADVANCE_TO_PAPER_SQL = """
UPDATE experiments
SET status = 'paper_trading',
    paper_session_id = %s,
    recommendation_id = %s,
    updated_at = NOW()
WHERE experiment_id = %s;
"""

RECORD_BACKTEST_METRICS_SQL = """
UPDATE experiments
SET backtest_sharpe = %s, backtest_win_rate = %s, updated_at = NOW()
WHERE experiment_id = %s;
"""

RECORD_VALIDATION_SQL = """
UPDATE experiments
SET paper_sharpe = %s,
    paper_win_rate = %s,
    validation_passed = %s,
    updated_at = NOW()
WHERE experiment_id = %s;
"""

FETCH_EXPERIMENT_SQL = """
SELECT experiment_id, created_at, updated_at, status,
       symbol, timeframe,
       mining_run_id, backtest_run_ids, recommendation_id, paper_session_id,
       backtest_sharpe, backtest_win_rate,
       paper_sharpe, paper_win_rate, validation_passed,
       notes
FROM experiments
WHERE experiment_id = %s;
"""

LIST_EXPERIMENTS_SQL = """
SELECT experiment_id, created_at, updated_at, status,
       symbol, timeframe,
       mining_run_id, backtest_run_ids, recommendation_id, paper_session_id,
       backtest_sharpe, backtest_win_rate,
       paper_sharpe, paper_win_rate, validation_passed,
       notes
FROM experiments
{where_clause}
ORDER BY updated_at DESC
LIMIT %s OFFSET %s;
"""

MARK_ABANDONED_SQL = """
UPDATE experiments SET status = 'abandoned', updated_at = NOW()
WHERE experiment_id = %s;
"""
