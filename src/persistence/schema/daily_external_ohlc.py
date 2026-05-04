"""daily_external_ohlc table DDL.

Stores daily OHLCV for non-broker external symbols used by research feature
providers (CME GC futures, DXY, 10Y yield, S&P 500). Composite PK
(symbol, date) keeps the schema generic for any new symbol added later.

Resolution: 1 day. Joined to intraday bars in feature providers via
``bar.time.date()`` lookup.

No CHECK constraints on OHLCV columns: external symbols span asset classes
with very different value ranges (e.g. ^TNX yields can be negative, indices
have no natural bound). Per-source validation lives in the data ingestor,
not in the schema.
"""

DDL = """
CREATE TABLE IF NOT EXISTS daily_external_ohlc (
    symbol  text NOT NULL,
    date    date NOT NULL,
    open    double precision,
    high    double precision,
    low     double precision,
    close   double precision,
    volume  double precision,
    PRIMARY KEY (symbol, date)
);
SELECT create_hypertable('daily_external_ohlc', 'date',
                          if_not_exists => TRUE, migrate_data => TRUE);
CREATE INDEX IF NOT EXISTS idx_daily_external_ohlc_symbol_date
ON daily_external_ohlc (symbol, date DESC);
"""

INSERT_SQL = """
INSERT INTO daily_external_ohlc (symbol, date, open, high, low, close, volume)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, date) DO UPDATE SET
    open   = EXCLUDED.open,
    high   = EXCLUDED.high,
    low    = EXCLUDED.low,
    close  = EXCLUDED.close,
    volume = EXCLUDED.volume
"""
