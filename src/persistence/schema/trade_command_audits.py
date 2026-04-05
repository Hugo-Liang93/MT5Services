"""trade_command_audits 表 DDL — 交易命令审计日志。

记录所有通过 TradingCommandService 执行的交易命令（下单、平仓、改单等），
包含请求/响应载荷、耗时、错误信息，用于合规审计和问题排查。

TimescaleDB hypertable，分区键 recorded_at。
"""

DDL = """
CREATE TABLE IF NOT EXISTS trade_command_audits (
    recorded_at      timestamptz NOT NULL,
    operation_id     text NOT NULL,
    account_alias    text NOT NULL,
    command_type     text NOT NULL,
    status           text NOT NULL
                     CHECK (status IN ('success', 'failed', 'timeout', 'error')),
    symbol           text,
    side             text,
    order_kind       text,
    volume           double precision,
    ticket           bigint,
    order_id         bigint,
    deal_id          bigint,
    magic            bigint,
    duration_ms      integer,
    error_message    text,
    request_payload  jsonb,
    response_payload jsonb,
    PRIMARY KEY (recorded_at, operation_id)
);
SELECT create_hypertable('trade_command_audits', 'recorded_at',
                          if_not_exists => TRUE, migrate_data => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_audits_operation_id
ON trade_command_audits (operation_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_trade_audits_recorded
ON trade_command_audits (recorded_at DESC, account_alias, command_type);
CREATE INDEX IF NOT EXISTS idx_trade_audits_status
ON trade_command_audits (status, recorded_at DESC);
"""

INSERT_SQL = """
INSERT INTO trade_command_audits (
    recorded_at,
    operation_id,
    account_alias,
    command_type,
    status,
    symbol,
    side,
    order_kind,
    volume,
    ticket,
    order_id,
    deal_id,
    magic,
    duration_ms,
    error_message,
    request_payload,
    response_payload
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (operation_id, recorded_at) DO UPDATE SET
    account_alias    = EXCLUDED.account_alias,
    command_type     = EXCLUDED.command_type,
    status           = EXCLUDED.status,
    symbol           = EXCLUDED.symbol,
    side             = EXCLUDED.side,
    order_kind       = EXCLUDED.order_kind,
    volume           = EXCLUDED.volume,
    ticket           = EXCLUDED.ticket,
    order_id         = EXCLUDED.order_id,
    deal_id          = EXCLUDED.deal_id,
    magic            = EXCLUDED.magic,
    duration_ms      = EXCLUDED.duration_ms,
    error_message    = EXCLUDED.error_message,
    request_payload  = EXCLUDED.request_payload,
    response_payload = EXCLUDED.response_payload
"""
