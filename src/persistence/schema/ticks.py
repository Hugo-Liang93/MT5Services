DDL = """
CREATE TABLE IF NOT EXISTS ticks (
    time timestamptz NOT NULL,
    symbol text NOT NULL,
    price double precision NOT NULL,
    volume double precision,
    time_msc bigint,
    PRIMARY KEY (time, symbol)
);
ALTER TABLE ticks ADD COLUMN IF NOT EXISTS time_msc bigint;
UPDATE ticks
SET time_msc = FLOOR(EXTRACT(EPOCH FROM time) * 1000)::bigint
WHERE time_msc IS NULL;
SELECT create_hypertable('ticks', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ticks_symbol_time_idx ON ticks(symbol, time DESC);
CREATE INDEX IF NOT EXISTS ticks_symbol_time_msc_idx ON ticks(symbol, time_msc DESC);
"""

INSERT_SQL = (
    "INSERT INTO ticks (symbol, price, volume, time, time_msc) "
    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING"
)
