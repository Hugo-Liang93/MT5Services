DDL = """
CREATE TABLE IF NOT EXISTS quotes (
    time   timestamptz      NOT NULL,
    symbol text             NOT NULL,
    bid    double precision CHECK (bid > 0),
    ask    double precision CHECK (ask > 0),
    last   double precision CHECK (last > 0),
    volume double precision CHECK (volume >= 0),
    PRIMARY KEY (time, symbol),
    CHECK (ask >= bid)
);
SELECT create_hypertable('quotes', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_quotes_symbol_time ON quotes(symbol, time DESC);
"""

INSERT_SQL = "INSERT INTO quotes (symbol, bid, ask, last, volume, time) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING"
