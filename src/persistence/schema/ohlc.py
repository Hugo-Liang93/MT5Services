DDL = """
CREATE TABLE IF NOT EXISTS ohlc (
    time timestamptz NOT NULL,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    indicators jsonb,
    PRIMARY KEY (time, symbol, timeframe)
);
SELECT create_hypertable('ohlc', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ohlc_symbol_time_idx ON ohlc(symbol, timeframe, time DESC);
"""

INSERT_SQL = """
INSERT INTO ohlc (symbol, timeframe, open, high, low, close, volume, time, indicators)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT DO NOTHING
"""

UPSERT_SQL = """
INSERT INTO ohlc (symbol, timeframe, open, high, low, close, volume, time, indicators)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (time, symbol, timeframe) DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    indicators = COALESCE(ohlc.indicators, '{}'::jsonb) || COALESCE(EXCLUDED.indicators, '{}'::jsonb)
"""
