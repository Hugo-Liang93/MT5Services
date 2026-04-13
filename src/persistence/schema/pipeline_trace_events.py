"""pipeline_trace_events 表 DDL — Pipeline 链路追踪事件。

记录从 bar_closed 到 execution_submitted 的完整 pipeline 链路事件，
用于延迟分析和问题排查。

TimescaleDB hypertable，分区键 recorded_at。
"""

DDL = """
CREATE TABLE IF NOT EXISTS pipeline_trace_events (
    recorded_at TIMESTAMPTZ NOT NULL,
    id          BIGSERIAL NOT NULL,
    trace_id    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    scope       TEXT NOT NULL
                CHECK (scope IN ('confirmed', 'intrabar')),
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    instance_id TEXT,
    instance_role TEXT,
    account_key TEXT,
    signal_id TEXT,
    intent_id TEXT,
    command_id TEXT,
    action_id TEXT,
    PRIMARY KEY (recorded_at, id)
);
SELECT create_hypertable('pipeline_trace_events', 'recorded_at',
                          if_not_exists => TRUE, migrate_data => TRUE);
ALTER TABLE pipeline_trace_events ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE pipeline_trace_events ADD COLUMN IF NOT EXISTS instance_role TEXT;
ALTER TABLE pipeline_trace_events ADD COLUMN IF NOT EXISTS account_key TEXT;
ALTER TABLE pipeline_trace_events ADD COLUMN IF NOT EXISTS signal_id TEXT;
ALTER TABLE pipeline_trace_events ADD COLUMN IF NOT EXISTS intent_id TEXT;
ALTER TABLE pipeline_trace_events ADD COLUMN IF NOT EXISTS command_id TEXT;
ALTER TABLE pipeline_trace_events ADD COLUMN IF NOT EXISTS action_id TEXT;
CREATE INDEX IF NOT EXISTS idx_pipeline_trace_trace
ON pipeline_trace_events (trace_id, recorded_at ASC);
CREATE INDEX IF NOT EXISTS idx_pipeline_trace_symbol_tf
ON pipeline_trace_events (symbol, timeframe, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_trace_type
ON pipeline_trace_events (event_type, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_trace_instance
ON pipeline_trace_events (instance_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_trace_signal
ON pipeline_trace_events (signal_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_trace_command
ON pipeline_trace_events (command_id, recorded_at DESC);
"""

INSERT_SQL = """
INSERT INTO pipeline_trace_events (
    trace_id,
    symbol,
    timeframe,
    scope,
    event_type,
    recorded_at,
    payload,
    instance_id,
    instance_role,
    account_key,
    signal_id,
    intent_id,
    command_id,
    action_id
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
