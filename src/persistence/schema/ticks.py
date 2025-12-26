DDL = """
CREATE TABLE IF NOT EXISTS ticks (
    time timestamptz NOT NULL,
    symbol text NOT NULL,
    price double precision NOT NULL,
    volume double precision,
    PRIMARY KEY (time, symbol)
);
SELECT create_hypertable('ticks', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ticks_symbol_time_idx ON ticks(symbol, time DESC);
"""

INSERT_SQL = "INSERT INTO ticks (symbol, price, volume, time) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING"
