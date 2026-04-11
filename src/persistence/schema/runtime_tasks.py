"""运行时任务状态持久化 Schema。"""

from src.monitoring.runtime_task_status import (
    RUNTIME_TASK_STATUS_CHECK_CONSTRAINT,
    runtime_task_states_sql_literals,
)

_STATE_SQL = runtime_task_states_sql_literals()

DDL = f"""
CREATE TABLE IF NOT EXISTS runtime_task_status (
    component TEXT NOT NULL,
    task_name TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL
        CONSTRAINT {RUNTIME_TASK_STATUS_CHECK_CONSTRAINT}
        CHECK (state IN ({_STATE_SQL})),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    duration_ms INTEGER,
    success_count BIGINT NOT NULL DEFAULT 0,
    failure_count BIGINT NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    details JSONB,
    PRIMARY KEY (component, task_name)
);

CREATE INDEX IF NOT EXISTS idx_runtime_tasks_updated
    ON runtime_task_status(updated_at DESC, component);
"""

MIGRATION_SQL = f"""
DO $$
DECLARE
    constraint_name text;
BEGIN
    IF to_regclass('runtime_task_status') IS NOT NULL THEN
        FOR constraint_name IN
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'runtime_task_status'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) ILIKE '%state%'
        LOOP
            EXECUTE format(
                'ALTER TABLE runtime_task_status DROP CONSTRAINT %I',
                constraint_name
            );
        END LOOP;

        ALTER TABLE runtime_task_status
            ADD CONSTRAINT {RUNTIME_TASK_STATUS_CHECK_CONSTRAINT}
            CHECK (state IN ({_STATE_SQL}));
    END IF;
END $$;
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
