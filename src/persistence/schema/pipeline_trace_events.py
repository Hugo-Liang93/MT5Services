DDL = """
CREATE TABLE IF NOT EXISTS pipeline_trace_events (
    id BIGSERIAL PRIMARY KEY,
    trace_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    scope TEXT NOT NULL,
    event_type TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS pipeline_trace_events_trace_idx
ON pipeline_trace_events (trace_id, recorded_at ASC);
CREATE INDEX IF NOT EXISTS pipeline_trace_events_symbol_tf_idx
ON pipeline_trace_events (symbol, timeframe, recorded_at DESC);
CREATE INDEX IF NOT EXISTS pipeline_trace_events_type_idx
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
