"""trade_frequency_reservations 表 DDL — 交易频率 quota reservation。

该表记录下单前已经占用但尚未完成审计落库的交易频率额度，避免并发请求
在 trade_command_audits 写入前同时通过 max_trades_per_day/hour。
"""

DDL = """
CREATE TABLE IF NOT EXISTS trade_frequency_reservations (
    reservation_id  text PRIMARY KEY,
    account_key     text NOT NULL,
    account_alias   text,
    reserved_at     timestamptz NOT NULL,
    expires_at      timestamptz NOT NULL,
    status          text NOT NULL
                    CHECK (status IN ('active', 'committed', 'released', 'expired')),
    finalized_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_frequency_reservations_account_status
ON trade_frequency_reservations (account_key, status, reserved_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_frequency_reservations_expiry
ON trade_frequency_reservations (expires_at);
"""
