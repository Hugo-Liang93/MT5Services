DDL = """
CREATE TABLE IF NOT EXISTS runtime_task_status (
    component text NOT NULL,
    task_name text NOT NULL,
    updated_at timestamptz NOT NULL,
    state text NOT NULL,
    started_at timestamptz,
    completed_at timestamptz,
    next_run_at timestamptz,
    duration_ms integer,
    success_count bigint NOT NULL DEFAULT 0,
    failure_count bigint NOT NULL DEFAULT 0,
    consecutive_failures integer NOT NULL DEFAULT 0,
    last_error text,
    details jsonb,
    PRIMARY KEY (component, task_name)
);
CREATE INDEX IF NOT EXISTS runtime_task_status_updated_idx
ON runtime_task_status (updated_at DESC, component);
"""

UPSERT_SQL = """
INSERT INTO runtime_task_status (
    component,
    task_name,
    updated_at,
    state,
    started_at,
    completed_at,
    next_run_at,
    duration_ms,
    success_count,
    failure_count,
    consecutive_failures,
    last_error,
    details
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (component, task_name) DO UPDATE SET
    updated_at = EXCLUDED.updated_at,
    state = EXCLUDED.state,
    started_at = EXCLUDED.started_at,
    completed_at = EXCLUDED.completed_at,
    next_run_at = EXCLUDED.next_run_at,
    duration_ms = EXCLUDED.duration_ms,
    success_count = EXCLUDED.success_count,
    failure_count = EXCLUDED.failure_count,
    consecutive_failures = EXCLUDED.consecutive_failures,
    last_error = EXCLUDED.last_error,
    details = EXCLUDED.details
"""
