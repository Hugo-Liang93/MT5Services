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
    PRIMARY KEY (recorded_at, id)
);
SELECT create_hypertable('pipeline_trace_events', 'recorded_at',
                          if_not_exists => TRUE, migrate_data => TRUE);
CREATE INDEX IF NOT EXISTS idx_pipeline_trace_trace
ON pipeline_trace_events (trace_id, recorded_at ASC);
CREATE INDEX IF NOT EXISTS idx_pipeline_trace_symbol_tf
ON pipeline_trace_events (symbol, timeframe, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_trace_type
ON pipeline_trace_events (event_type, recorded_at DESC);
"""

INSERT_SQL = """
INSERT INTO pipeline_trace_events (
    trace_id,
    symbol,
    timeframe,
    scope,
    event_type,
    recorded_at,
    payload
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""
