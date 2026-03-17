DDL = """
CREATE TABLE IF NOT EXISTS trade_operations (
    recorded_at timestamptz NOT NULL,
    operation_id text PRIMARY KEY,
    account_alias text NOT NULL,
    operation_type text NOT NULL,
    status text NOT NULL,
    symbol text,
    side text,
    order_kind text,
    volume double precision,
    ticket bigint,
    order_id bigint,
    deal_id bigint,
    magic bigint,
    duration_ms integer,
    error_message text,
    request_payload jsonb,
    response_payload jsonb
);
CREATE INDEX IF NOT EXISTS trade_operations_recorded_idx
ON trade_operations (recorded_at DESC, account_alias, operation_type);
CREATE INDEX IF NOT EXISTS trade_operations_status_idx
ON trade_operations (status, recorded_at DESC);
"""

INSERT_SQL = """
INSERT INTO trade_operations (
    recorded_at,
    operation_id,
    account_alias,
    operation_type,
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
ON CONFLICT (operation_id) DO UPDATE SET
    recorded_at = EXCLUDED.recorded_at,
    account_alias = EXCLUDED.account_alias,
    operation_type = EXCLUDED.operation_type,
    status = EXCLUDED.status,
    symbol = EXCLUDED.symbol,
    side = EXCLUDED.side,
    order_kind = EXCLUDED.order_kind,
    volume = EXCLUDED.volume,
    ticket = EXCLUDED.ticket,
    order_id = EXCLUDED.order_id,
    deal_id = EXCLUDED.deal_id,
    magic = EXCLUDED.magic,
    duration_ms = EXCLUDED.duration_ms,
    error_message = EXCLUDED.error_message,
    request_payload = EXCLUDED.request_payload,
    response_payload = EXCLUDED.response_payload
"""
