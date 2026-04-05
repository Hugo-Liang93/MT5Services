"""signal_events / signal_preview_events 表 DDL — 信号事件时序存储。

两表结构相同，均为 TimescaleDB hypertable（分区键 generated_at）。
- signal_events：confirmed scope 的 K 线收盘信号
- signal_preview_events：intrabar scope 的盘中预览信号
"""

DDL = """
CREATE TABLE IF NOT EXISTS signal_events (
    generated_at       timestamptz NOT NULL,
    signal_id          text NOT NULL,
    symbol             text NOT NULL,
    timeframe          text NOT NULL,
    strategy           text NOT NULL,
    direction          text NOT NULL
                       CHECK (direction IN ('buy', 'sell', 'hold')),
    confidence         double precision NOT NULL,
    reason             text,
    used_indicators    jsonb,
    indicators_snapshot jsonb,
    metadata           jsonb,
    PRIMARY KEY (generated_at, signal_id)
);
SELECT create_hypertable('signal_events', 'generated_at',
                          if_not_exists => TRUE, migrate_data => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_events_signal_id
ON signal_events (signal_id, generated_at);
CREATE INDEX IF NOT EXISTS idx_signal_events_time
ON signal_events (generated_at DESC, symbol, timeframe, strategy);
CREATE INDEX IF NOT EXISTS idx_signal_events_direction
ON signal_events (direction, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_events_trace_id
ON signal_events USING GIN ((metadata->'signal_trace_id'))
WHERE metadata->'signal_trace_id' IS NOT NULL;

CREATE TABLE IF NOT EXISTS signal_preview_events (
    generated_at       timestamptz NOT NULL,
    signal_id          text NOT NULL,
    symbol             text NOT NULL,
    timeframe          text NOT NULL,
    strategy           text NOT NULL,
    direction          text NOT NULL
                       CHECK (direction IN ('buy', 'sell', 'hold')),
    confidence         double precision NOT NULL,
    reason             text,
    used_indicators    jsonb,
    indicators_snapshot jsonb,
    metadata           jsonb,
    PRIMARY KEY (generated_at, signal_id)
);
SELECT create_hypertable('signal_preview_events', 'generated_at',
                          if_not_exists => TRUE, migrate_data => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_preview_signal_id
ON signal_preview_events (signal_id, generated_at);
CREATE INDEX IF NOT EXISTS idx_signal_preview_time
ON signal_preview_events (generated_at DESC, symbol, timeframe, strategy);
CREATE INDEX IF NOT EXISTS idx_signal_preview_direction
ON signal_preview_events (direction, generated_at DESC);
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
ON CONFLICT (signal_id, generated_at) DO UPDATE SET
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
ON CONFLICT (signal_id, generated_at) DO UPDATE SET
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
