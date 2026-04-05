DDL = """
CREATE TABLE IF NOT EXISTS signal_events (
    generated_at timestamptz NOT NULL,
    signal_id text PRIMARY KEY,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    strategy text NOT NULL,
    direction text NOT NULL,
    confidence double precision NOT NULL,
    reason text,
    used_indicators jsonb,
    indicators_snapshot jsonb,
    metadata jsonb
);
CREATE INDEX IF NOT EXISTS signal_events_time_idx
ON signal_events (generated_at DESC, symbol, timeframe, strategy);
CREATE INDEX IF NOT EXISTS signal_events_direction_idx
ON signal_events (direction, generated_at DESC);
CREATE TABLE IF NOT EXISTS signal_preview_events (
    generated_at timestamptz NOT NULL,
    signal_id text PRIMARY KEY,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    strategy text NOT NULL,
    direction text NOT NULL,
    confidence double precision NOT NULL,
    reason text,
    used_indicators jsonb,
    indicators_snapshot jsonb,
    metadata jsonb
);
CREATE INDEX IF NOT EXISTS signal_preview_events_time_idx
ON signal_preview_events (generated_at DESC, symbol, timeframe, strategy);
CREATE INDEX IF NOT EXISTS signal_preview_events_direction_idx
ON signal_preview_events (direction, generated_at DESC);
CREATE INDEX IF NOT EXISTS signal_events_trace_id_idx
ON signal_events USING GIN ((metadata->'signal_trace_id'))
WHERE metadata->'signal_trace_id' IS NOT NULL;
"""

INSERT_SQL = """
INSERT INTO signal_events (
    generated_at,
    signal_id,
    symbol,
    timeframe,
    strategy,
    direction,
    confidence,
    reason,
    used_indicators,
    indicators_snapshot,
    metadata
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (signal_id) DO UPDATE SET
    generated_at = EXCLUDED.generated_at,
    symbol = EXCLUDED.symbol,
    timeframe = EXCLUDED.timeframe,
    strategy = EXCLUDED.strategy,
    direction = EXCLUDED.direction,
    confidence = EXCLUDED.confidence,
    reason = EXCLUDED.reason,
    used_indicators = EXCLUDED.used_indicators,
    indicators_snapshot = EXCLUDED.indicators_snapshot,
    metadata = EXCLUDED.metadata
"""

PREVIEW_INSERT_SQL = """
INSERT INTO signal_preview_events (
    generated_at,
    signal_id,
    symbol,
    timeframe,
    strategy,
    direction,
    confidence,
    reason,
    used_indicators,
    indicators_snapshot,
    metadata
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (signal_id) DO UPDATE SET
    generated_at = EXCLUDED.generated_at,
    symbol = EXCLUDED.symbol,
    timeframe = EXCLUDED.timeframe,
    strategy = EXCLUDED.strategy,
    direction = EXCLUDED.direction,
    confidence = EXCLUDED.confidence,
    reason = EXCLUDED.reason,
    used_indicators = EXCLUDED.used_indicators,
    indicators_snapshot = EXCLUDED.indicators_snapshot,
    metadata = EXCLUDED.metadata
"""
