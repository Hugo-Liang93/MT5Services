DDL = """
CREATE TABLE IF NOT EXISTS quotes (
    time timestamptz NOT NULL,
    symbol text NOT NULL,
    bid double precision,
    ask double precision,
    last double precision,
    volume double precision,
    PRIMARY KEY (time, symbol)
);
SELECT create_hypertable('quotes', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS quotes_symbol_time_idx ON quotes(symbol, time DESC);
"""

INSERT_SQL = "INSERT INTO quotes (symbol, bid, ask, last, volume, time) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING"
