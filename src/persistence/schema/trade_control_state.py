DDL = """
CREATE TABLE IF NOT EXISTS trade_control_state (
    account_alias TEXT PRIMARY KEY,
    account_key TEXT,
    auto_entry_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    close_only_mode BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ,
    reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
ALTER TABLE trade_control_state
    ADD COLUMN IF NOT EXISTS account_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_control_state_account_key
    ON trade_control_state (account_key);
"""

UPSERT_SQL = """
INSERT INTO trade_control_state (
    account_alias, auto_entry_enabled, close_only_mode,
    updated_at, reason, metadata, account_key
) VALUES (
    %s, %s, %s,
    %s, %s, %s, %s
)
ON CONFLICT (account_alias) DO UPDATE SET
    auto_entry_enabled = EXCLUDED.auto_entry_enabled,
    close_only_mode = EXCLUDED.close_only_mode,
    updated_at = EXCLUDED.updated_at,
    reason = EXCLUDED.reason,
    metadata = EXCLUDED.metadata,
    account_key = EXCLUDED.account_key
"""
