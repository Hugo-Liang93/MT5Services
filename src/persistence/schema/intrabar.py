DDL = """
CREATE TABLE IF NOT EXISTS ohlc_intrabar (
    recorded_at timestamptz      NOT NULL,
    time        timestamptz      NOT NULL,
    symbol      text             NOT NULL,
    timeframe   text             NOT NULL,
    open        double precision CHECK (open  > 0),
    high        double precision CHECK (high  > 0),
    low         double precision CHECK (low   > 0),
    close       double precision CHECK (close > 0),
    volume      double precision CHECK (volume >= 0),
    PRIMARY KEY (recorded_at, symbol, timeframe),
    CHECK (high >= low),
    CHECK (high >= open  AND high >= close),
    CHECK (low  <= open  AND low  <= close)
);
SELECT create_hypertable('ohlc_intrabar', 'recorded_at', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ohlc_intrabar_symbol_tf_time ON ohlc_intrabar(symbol, timeframe, time DESC);
"""

INSERT_SQL = """
INSERT INTO ohlc_intrabar (symbol, timeframe, open, high, low, close, volume, time, recorded_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT DO NOTHING
"""
