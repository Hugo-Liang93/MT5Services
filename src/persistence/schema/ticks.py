DDL = """
CREATE TABLE IF NOT EXISTS ticks (
    time        timestamptz      NOT NULL,
    symbol      text             NOT NULL,
    price       double precision NOT NULL CHECK (price > 0),
    volume      double precision CHECK (volume >= 0),
    time_msc    bigint           CHECK (time_msc >= 0),
    PRIMARY KEY (time, symbol)
);
SELECT create_hypertable('ticks', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_time     ON ticks(symbol, time DESC);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_time_msc ON ticks(symbol, time_msc DESC);
"""

INSERT_SQL = (
    "INSERT INTO ticks (symbol, price, volume, time, time_msc) "
    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING"
)
