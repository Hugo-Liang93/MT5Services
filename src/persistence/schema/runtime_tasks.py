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
    instance_id TEXT NOT NULL DEFAULT 'legacy',
    instance_role TEXT,
    account_key TEXT,
    account_alias TEXT,
    PRIMARY KEY (instance_id, component, task_name)
);

CREATE INDEX IF NOT EXISTS idx_runtime_tasks_updated
    ON runtime_task_status(updated_at DESC, component);
CREATE INDEX IF NOT EXISTS idx_runtime_tasks_instance
    ON runtime_task_status(instance_id, updated_at DESC);
"""

MIGRATION_SQL = f"""
DO $$
DECLARE
    constraint_name text;
BEGIN
    IF to_regclass('runtime_task_status') IS NOT NULL THEN
        ALTER TABLE runtime_task_status ADD COLUMN IF NOT EXISTS instance_id TEXT;
        ALTER TABLE runtime_task_status ADD COLUMN IF NOT EXISTS instance_role TEXT;
        ALTER TABLE runtime_task_status ADD COLUMN IF NOT EXISTS account_key TEXT;
        ALTER TABLE runtime_task_status ADD COLUMN IF NOT EXISTS account_alias TEXT;
        UPDATE runtime_task_status
        SET instance_id = 'legacy'
        WHERE instance_id IS NULL OR btrim(instance_id) = '';
        ALTER TABLE runtime_task_status
            ALTER COLUMN instance_id SET DEFAULT 'legacy';
        ALTER TABLE runtime_task_status
            ALTER COLUMN instance_id SET NOT NULL;
        ALTER TABLE runtime_task_status
            DROP CONSTRAINT IF EXISTS runtime_task_status_pkey;
        ALTER TABLE runtime_task_status
            ADD PRIMARY KEY (instance_id, component, task_name);
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
    details,
    instance_id,
    instance_role,
    account_key,
    account_alias
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (instance_id, component, task_name) DO UPDATE SET
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
    details = EXCLUDED.details,
    instance_role = EXCLUDED.instance_role,
    account_key = EXCLUDED.account_key,
    account_alias = EXCLUDED.account_alias
"""
