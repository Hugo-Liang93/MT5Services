"""execution_intents 表 DDL。

main 实例把可执行 confirmed 信号展开成按账户投递的 intent，
executor/main-consumer 只 claim 属于自己账户的 intent 并负责执行。
"""

DDL = """
CREATE TABLE IF NOT EXISTS execution_intents (
    created_at             timestamptz NOT NULL,
    intent_id              text NOT NULL,
    intent_key             text NOT NULL,
    signal_id              text NOT NULL,
    target_account_key     text NOT NULL,
    target_account_alias   text NOT NULL,
    strategy               text NOT NULL,
    symbol                 text NOT NULL,
    timeframe              text NOT NULL,
    payload                jsonb NOT NULL,
    -- §0dm P1：'dispatched' 状态表示已 atomic CAS claimed → dispatched，
    -- broker dispatch 已开始或已发出，禁止 reclaim 重试（防止真实重复下单）。
    -- 过期 dispatched 由人工 reconcile 走 dead_lettered，不自动重置 pending。
    status                 text NOT NULL
                           CHECK (
                               status IN (
                                   'pending',
                                   'claimed',
                                   'dispatched',
                                   'completed',
                                   'failed',
                                   'skipped',
                                   'dead_lettered'
                               )
                           ),
    attempt_count          integer NOT NULL DEFAULT 0,
    claimed_by_instance_id text,
    claimed_by_run_id      text,
    claimed_at             timestamptz,
    lease_expires_at       timestamptz,
    last_heartbeat_at      timestamptz,
    completed_at           timestamptz,
    dead_lettered_at       timestamptz,
    last_error_code        text,
    decision_metadata      jsonb,
    PRIMARY KEY (created_at, intent_id)
);
SELECT create_hypertable('execution_intents', 'created_at',
                          if_not_exists => TRUE, migrate_data => TRUE);
ALTER TABLE execution_intents ADD COLUMN IF NOT EXISTS intent_key TEXT;
ALTER TABLE execution_intents ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;
ALTER TABLE execution_intents ADD COLUMN IF NOT EXISTS claimed_by_run_id TEXT;
ALTER TABLE execution_intents ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE execution_intents ADD COLUMN IF NOT EXISTS last_heartbeat_at timestamptz;
ALTER TABLE execution_intents ADD COLUMN IF NOT EXISTS dead_lettered_at timestamptz;
ALTER TABLE execution_intents ADD COLUMN IF NOT EXISTS last_error_code TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_intents_id
ON execution_intents (intent_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_intents_key
ON execution_intents (intent_key, created_at);
CREATE INDEX IF NOT EXISTS idx_execution_intents_target_status
ON execution_intents (target_account_key, status, created_at DESC);
"""

MIGRATION_SQL = """
DROP INDEX IF EXISTS idx_execution_intents_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_intents_key
ON execution_intents (intent_key, created_at);

-- §0dm P1: status CHECK 升级支持 'dispatched'（at-most-once dispatch）。
-- Postgres CHECK 不能直接 ALTER 增加值——必须 DROP + ADD，旧约束名 schema 命中。
ALTER TABLE execution_intents DROP CONSTRAINT IF EXISTS execution_intents_status_check;
ALTER TABLE execution_intents ADD CONSTRAINT execution_intents_status_check
    CHECK (status IN (
        'pending', 'claimed', 'dispatched',
        'completed', 'failed', 'skipped', 'dead_lettered'
    ));
"""

INSERT_SQL = """
INSERT INTO execution_intents (
    created_at,
    intent_id,
    intent_key,
    signal_id,
    target_account_key,
    target_account_alias,
    strategy,
    symbol,
    timeframe,
    payload,
    status,
    attempt_count,
    claimed_by_instance_id,
    claimed_by_run_id,
    claimed_at,
    lease_expires_at,
    last_heartbeat_at,
    completed_at,
    dead_lettered_at,
    last_error_code,
    decision_metadata
)
VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (intent_id, created_at) DO UPDATE SET
    intent_key = EXCLUDED.intent_key,
    signal_id = EXCLUDED.signal_id,
    payload = EXCLUDED.payload,
    status = EXCLUDED.status,
    attempt_count = EXCLUDED.attempt_count,
    claimed_by_instance_id = EXCLUDED.claimed_by_instance_id,
    claimed_by_run_id = EXCLUDED.claimed_by_run_id,
    claimed_at = EXCLUDED.claimed_at,
    lease_expires_at = EXCLUDED.lease_expires_at,
    last_heartbeat_at = EXCLUDED.last_heartbeat_at,
    completed_at = EXCLUDED.completed_at,
    dead_lettered_at = EXCLUDED.dead_lettered_at,
    last_error_code = EXCLUDED.last_error_code,
    decision_metadata = EXCLUDED.decision_metadata
"""
