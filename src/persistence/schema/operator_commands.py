"""operator_commands 表 DDL。

账户级控制动作统一先落控制平面，再由目标账户实例本地 claim 并执行。
"""

DDL = """
CREATE TABLE IF NOT EXISTS operator_commands (
    created_at             timestamptz NOT NULL,
    command_id             text NOT NULL,
    command_type           text NOT NULL,
    target_account_key     text NOT NULL,
    target_account_alias   text NOT NULL,
    status                 text NOT NULL
                           CHECK (
                               status IN (
                                   'pending',
                                   'claimed',
                                   'completed',
                                   'failed',
                                   'dead_lettered',
                                   'cancelled'
                               )
                           ),
    action_id              text,
    actor                  text,
    reason                 text,
    idempotency_key        text,
    request_context        jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload                jsonb NOT NULL DEFAULT '{}'::jsonb,
    claimed_by_instance_id text,
    claimed_by_run_id      text,
    claimed_at             timestamptz,
    lease_expires_at       timestamptz,
    last_heartbeat_at      timestamptz,
    attempt_count          integer NOT NULL DEFAULT 0,
    last_error_code        text,
    completed_at           timestamptz,
    dead_lettered_at       timestamptz,
    response_payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    audit_id               text,
    PRIMARY KEY (created_at, command_id)
);
SELECT create_hypertable('operator_commands', 'created_at',
                          if_not_exists => TRUE, migrate_data => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_operator_commands_id
ON operator_commands (command_id, created_at);
CREATE INDEX IF NOT EXISTS idx_operator_commands_target_status
ON operator_commands (target_account_key, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operator_commands_action_id
ON operator_commands (action_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_operator_commands_idempotency
ON operator_commands (target_account_key, command_type, idempotency_key)
WHERE idempotency_key IS NOT NULL;
"""

INSERT_SQL = """
INSERT INTO operator_commands (
    created_at,
    command_id,
    command_type,
    target_account_key,
    target_account_alias,
    status,
    action_id,
    actor,
    reason,
    idempotency_key,
    request_context,
    payload,
    claimed_by_instance_id,
    claimed_by_run_id,
    claimed_at,
    lease_expires_at,
    last_heartbeat_at,
    attempt_count,
    last_error_code,
    completed_at,
    response_payload,
    audit_id
)
VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (command_id, created_at) DO UPDATE SET
    status = EXCLUDED.status,
    claimed_by_instance_id = EXCLUDED.claimed_by_instance_id,
    claimed_by_run_id = EXCLUDED.claimed_by_run_id,
    claimed_at = EXCLUDED.claimed_at,
    lease_expires_at = EXCLUDED.lease_expires_at,
    last_heartbeat_at = EXCLUDED.last_heartbeat_at,
    attempt_count = EXCLUDED.attempt_count,
    last_error_code = EXCLUDED.last_error_code,
    completed_at = EXCLUDED.completed_at,
    response_payload = EXCLUDED.response_payload,
    audit_id = EXCLUDED.audit_id
"""
