"""参数推荐持久化 Schema。"""

DDL = """
-- 参数推荐记录
CREATE TABLE IF NOT EXISTS backtest_recommendations (
    rec_id TEXT PRIMARY KEY,
    source_run_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'applied', 'rolled_back', 'rejected')),
    overfitting_ratio DOUBLE PRECISION,
    consistency_rate DOUBLE PRECISION,
    oos_sharpe DOUBLE PRECISION,
    oos_win_rate DOUBLE PRECISION,
    oos_total_trades INTEGER,
    changes JSONB NOT NULL,
    rationale TEXT,
    approved_at TIMESTAMPTZ,
    applied_at TIMESTAMPTZ,
    rolled_back_at TIMESTAMPTZ,
    backup_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_bt_rec_status
    ON backtest_recommendations(status);
CREATE INDEX IF NOT EXISTS idx_bt_rec_created
    ON backtest_recommendations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bt_rec_source
    ON backtest_recommendations(source_run_id);
"""

INSERT_RECOMMENDATION_SQL = """
INSERT INTO backtest_recommendations (
    rec_id, source_run_id, created_at, status,
    overfitting_ratio, consistency_rate, oos_sharpe, oos_win_rate, oos_total_trades,
    changes, rationale
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (rec_id) DO NOTHING
"""

UPDATE_RECOMMENDATION_STATUS_SQL = """
UPDATE backtest_recommendations
SET status = %s,
    approved_at = %s,
    applied_at = %s,
    rolled_back_at = %s,
    backup_path = %s
WHERE rec_id = %s
"""

FETCH_RECOMMENDATION_SQL = """
SELECT rec_id, source_run_id, created_at, status,
       overfitting_ratio, consistency_rate, oos_sharpe, oos_win_rate, oos_total_trades,
       changes, rationale,
       approved_at, applied_at, rolled_back_at, backup_path
FROM backtest_recommendations
WHERE rec_id = %s
"""

LIST_RECOMMENDATIONS_SQL = """
SELECT rec_id, source_run_id, created_at, status,
       overfitting_ratio, consistency_rate, oos_sharpe, oos_win_rate, oos_total_trades,
       changes, rationale,
       approved_at, applied_at, rolled_back_at, backup_path
FROM backtest_recommendations
{where_clause}
ORDER BY created_at DESC
LIMIT %s OFFSET %s
"""
