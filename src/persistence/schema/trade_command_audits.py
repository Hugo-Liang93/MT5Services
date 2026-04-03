DDL = """
CREATE TABLE IF NOT EXISTS trade_command_audits (
    recorded_at timestamptz NOT NULL,
    operation_id text PRIMARY KEY,
    account_alias text NOT NULL,
    command_type text NOT NULL,
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
CREATE INDEX IF NOT EXISTS trade_command_audits_recorded_idx
ON trade_command_audits (recorded_at DESC, account_alias, command_type);
CREATE INDEX IF NOT EXISTS trade_command_audits_status_idx
ON trade_command_audits (status, recorded_at DESC);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_name = 'trade_operations'
    ) THEN
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
        SELECT
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
        FROM trade_operations
        WHERE operation_type IN (
            'execute_trade',
            'precheck_trade',
            'execute_trade_batch',
            'close_position',
            'close_all_positions',
            'close_positions_by_tickets',
            'cancel_orders',
            'cancel_orders_by_tickets',
            'modify_orders',
            'modify_positions'
        )
        ON CONFLICT (operation_id) DO NOTHING;
    END IF;
END $$;
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
ON CONFLICT (operation_id) DO UPDATE SET
    recorded_at = EXCLUDED.recorded_at,
    account_alias = EXCLUDED.account_alias,
    command_type = EXCLUDED.command_type,
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
