DDL = """
CREATE TABLE IF NOT EXISTS signal_events (
    generated_at timestamptz NOT NULL,
    signal_id text PRIMARY KEY,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    strategy text NOT NULL,
    action text NOT NULL,
    confidence double precision NOT NULL,
    reason text,
    used_indicators jsonb,
    indicators_snapshot jsonb,
    metadata jsonb
);
CREATE INDEX IF NOT EXISTS signal_events_time_idx
ON signal_events (generated_at DESC, symbol, timeframe, strategy);
CREATE INDEX IF NOT EXISTS signal_events_action_idx
ON signal_events (action, generated_at DESC);
"""

INSERT_SQL = """
INSERT INTO signal_events (
    generated_at,
    signal_id,
    symbol,
    timeframe,
    strategy,
    action,
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
    action = EXCLUDED.action,
    confidence = EXCLUDED.confidence,
    reason = EXCLUDED.reason,
    used_indicators = EXCLUDED.used_indicators,
    indicators_snapshot = EXCLUDED.indicators_snapshot,
    metadata = EXCLUDED.metadata
"""
