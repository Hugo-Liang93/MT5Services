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


# ── P9 Phase 1.5: Admission writeback fields ──────────────────────
# 这些字段由 executor admission 链路异步回填（actionability/guard_*/priority），
# INSERT 时全为 NULL，UPDATE_ADMISSION_SQL 单独负责写入。历史记录天然 NULL，前端
# 按 NULLS LAST 排序自动降级。
MIGRATION_SQL = """
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS actionability text;
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS guard_reason_code text;
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS guard_category text;
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS priority double precision;
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS rank_source text;

CREATE INDEX IF NOT EXISTS idx_signal_events_priority
ON signal_events (priority DESC NULLS LAST, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_signal_events_actionability
ON signal_events (actionability, generated_at DESC)
WHERE actionability IS NOT NULL;
"""


# UPDATE 单条 signal 的 admission 结果。priority 由 (confidence × 系数) 算出，
# 在调用方计算后传入；guard_category 也由调用方按 reason_category() 推导。
UPDATE_ADMISSION_SQL = """
UPDATE signal_events
SET actionability = %s,
    guard_reason_code = %s,
    guard_category = %s,
    priority = %s,
    rank_source = %s
WHERE signal_id = %s
"""
