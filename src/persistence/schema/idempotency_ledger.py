"""§0dm P2 #4 + #5：execution_intents / operator_commands 幂等键 ledger 表。

execution_intents / operator_commands 是 hypertable，timescaledb 限制 unique
约束必须包含 chunk 列（created_at），所以无法直接对 (intent_key) /
(target_account_key, command_type, idempotency_key) 加跨时间唯一约束。

工程化方案：用普通 (非-hypertable) ledger 表存幂等键，由 publisher / service
端在写入主表 **之前** 先 INSERT ON CONFLICT DO NOTHING 到 ledger——成功才允许
插主表，失败说明重复，跳过插入。这把"幂等性"集中到 ledger 单一约束源，
原 hypertable 的非唯一索引仅用于查询加速。

ledger 设计：
- execution_intent_idempotency: 主键 (intent_key)，记录首次成功发布的 intent_id
- operator_command_idempotency: 主键 (target_account_key, command_type,
  idempotency_key)，记录首次成功提交的 command_id
"""

DDL = """
CREATE TABLE IF NOT EXISTS execution_intent_idempotency (
    intent_key   text NOT NULL PRIMARY KEY,
    intent_id    text NOT NULL,
    created_at   timestamptz NOT NULL,
    signal_id    text NOT NULL,
    target_account_key text NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_command_idempotency (
    target_account_key text NOT NULL,
    command_type       text NOT NULL,
    idempotency_key    text NOT NULL,
    command_id         text NOT NULL,
    created_at         timestamptz NOT NULL,
    PRIMARY KEY (target_account_key, command_type, idempotency_key)
);
"""

INSERT_INTENT_KEY_SQL = """
INSERT INTO execution_intent_idempotency (
    intent_key, intent_id, created_at, signal_id, target_account_key
)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (intent_key) DO NOTHING
"""

INSERT_COMMAND_KEY_SQL = """
INSERT INTO operator_command_idempotency (
    target_account_key, command_type, idempotency_key, command_id, created_at
)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (target_account_key, command_type, idempotency_key) DO NOTHING
"""
