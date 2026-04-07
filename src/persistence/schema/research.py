"""Research 挖掘结果持久化 Schema。"""

DDL = """
CREATE TABLE IF NOT EXISTS research_mining_runs (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    n_bars INTEGER,
    status TEXT NOT NULL DEFAULT 'completed'
        CHECK (status IN ('running', 'completed', 'failed')),
    data_summary JSONB,
    top_findings JSONB,
    full_result JSONB
);
"""

INSERT_MINING_RUN_SQL = """
INSERT INTO research_mining_runs
    (run_id, experiment_id, symbol, timeframe, start_time, end_time,
     n_bars, status, data_summary, top_findings, full_result)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (run_id) DO UPDATE SET
    status = EXCLUDED.status,
    data_summary = EXCLUDED.data_summary,
    top_findings = EXCLUDED.top_findings,
    full_result = EXCLUDED.full_result;
"""

FETCH_MINING_RUN_SQL = """
SELECT run_id, experiment_id, created_at, symbol, timeframe,
       start_time, end_time, n_bars, status,
       data_summary, top_findings, full_result
FROM research_mining_runs
WHERE run_id = %s;
"""

LIST_MINING_RUNS_SQL = """
SELECT run_id, experiment_id, created_at, symbol, timeframe,
       start_time, end_time, n_bars, status,
       data_summary, top_findings
FROM research_mining_runs
ORDER BY created_at DESC
LIMIT %s OFFSET %s;
"""
