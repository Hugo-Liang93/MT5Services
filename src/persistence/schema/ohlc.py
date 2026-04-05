DDL = """
CREATE TABLE IF NOT EXISTS ohlc (
    time       timestamptz      NOT NULL,
    symbol     text             NOT NULL,
    timeframe  text             NOT NULL,
    open       double precision CHECK (open  > 0),
    high       double precision CHECK (high  > 0),
    low        double precision CHECK (low   > 0),
    close      double precision CHECK (close > 0),
    volume     double precision CHECK (volume >= 0),
    indicators jsonb,
    PRIMARY KEY (time, symbol, timeframe),
    CHECK (high >= low),
    CHECK (high >= open  AND high >= close),
    CHECK (low  <= open  AND low  <= close)
);
SELECT create_hypertable('ohlc', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ohlc_symbol_tf_time ON ohlc(symbol, timeframe, time DESC);
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
