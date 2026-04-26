DDL = """
CREATE TABLE IF NOT EXISTS trade_control_state (
    account_key TEXT NOT NULL PRIMARY KEY,
    account_alias TEXT NOT NULL,
    auto_entry_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    close_only_mode BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ,
    reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
ALTER TABLE trade_control_state
    ADD COLUMN IF NOT EXISTS account_key TEXT;
CREATE INDEX IF NOT EXISTS idx_trade_control_state_alias
    ON trade_control_state (account_alias);
"""

# §0v P3 迁移：把 PK 从 account_alias 升级到 account_key（globally unique
# env:server:login）。旧 schema PK=account_alias + UNIQUE INDEX=account_key
# → 同 account_key 不同 alias（别名重命名/规范化）写入时 unique 索引先冲突，
# 但 ON CONFLICT (account_alias) 接不住 → UPSERT 直接抛 UNIQUE violation。
# 步骤：
#   1) backfill account_key（NULL/空串 → account_alias）
#   2) ALTER COLUMN ... SET NOT NULL（PK 列不允许 NULL）
#   3) drop 旧的 unique index（PK 自带 unique 约束，索引冗余且与 PK 冲突）
#   4) 仅在当前 PK 不是 account_key 时 drop+rebuild（DO 块保证幂等）
MIGRATION_SQL = """
UPDATE trade_control_state
SET account_key = account_alias
WHERE account_key IS NULL OR account_key = '';

ALTER TABLE trade_control_state
    ALTER COLUMN account_key SET NOT NULL;

DROP INDEX IF EXISTS idx_trade_control_state_account_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'trade_control_state'
          AND c.contype = 'p'
          AND pg_get_constraintdef(c.oid) ILIKE '%(account_key)'
    ) THEN
        ALTER TABLE trade_control_state
            DROP CONSTRAINT IF EXISTS trade_control_state_pkey;
        ALTER TABLE trade_control_state
            ADD CONSTRAINT trade_control_state_pkey
            PRIMARY KEY (account_key);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_trade_control_state_alias
    ON trade_control_state (account_alias);
"""

UPSERT_SQL = """
INSERT INTO trade_control_state (
    account_alias, auto_entry_enabled, close_only_mode,
    updated_at, reason, metadata, account_key
) VALUES (
    %s, %s, %s,
    %s, %s, %s, %s
)
ON CONFLICT (account_key) DO UPDATE SET
    account_alias = EXCLUDED.account_alias,
    auto_entry_enabled = EXCLUDED.auto_entry_enabled,
    close_only_mode = EXCLUDED.close_only_mode,
    updated_at = EXCLUDED.updated_at,
    reason = EXCLUDED.reason,
    metadata = EXCLUDED.metadata
"""
