DDL = """
CREATE TABLE IF NOT EXISTS ohlc_intrabar (
    recorded_at timestamptz NOT NULL,
    time timestamptz NOT NULL,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    PRIMARY KEY (recorded_at, symbol, timeframe)
);
SELECT create_hypertable('ohlc_intrabar', 'recorded_at', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ohlc_intrabar_symbol_time_idx ON ohlc_intrabar(symbol, timeframe, time DESC);
"""

INSERT_SQL = """
INSERT INTO ohlc_intrabar (symbol, timeframe, open, high, low, close, volume, time, recorded_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT DO NOTHING
"""
