DDL = """
CREATE TABLE IF NOT EXISTS indicators (
    symbol text NOT NULL,
    timeframe text NOT NULL,
    indicator text NOT NULL,
    value double precision,
    bar_time timestamptz NOT NULL,
    computed_at timestamptz NOT NULL,
    PRIMARY KEY (symbol, timeframe, indicator, bar_time)
);
SELECT create_hypertable('indicators', 'bar_time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS indicators_symbol_tf_time_idx ON indicators(symbol, timeframe, bar_time DESC);
"""

UPSERT_SQL = """
INSERT INTO indicators (symbol, timeframe, indicator, value, bar_time, computed_at)
VALUES (%s,%s,%s,%s,%s,%s)
ON CONFLICT (symbol, timeframe, indicator, bar_time) DO UPDATE SET
    value = EXCLUDED.value,
    computed_at = EXCLUDED.computed_at
"""
