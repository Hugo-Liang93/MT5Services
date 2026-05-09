DDL = """
CREATE TABLE IF NOT EXISTS ticks (
    time        timestamptz      NOT NULL,
    symbol      text             NOT NULL,
    price       double precision NOT NULL CHECK (price > 0),
    bid         double precision CHECK (bid IS NULL OR bid > 0),
    ask         double precision CHECK (ask IS NULL OR ask > 0),
    last        double precision CHECK (last IS NULL OR last > 0),
    volume      double precision CHECK (volume >= 0),
    time_msc    bigint           CHECK (time_msc >= 0),
    flags       integer,
    PRIMARY KEY (time, symbol)
);
SELECT create_hypertable('ticks', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_time     ON ticks(symbol, time DESC);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_time_msc ON ticks(symbol, time_msc DESC);
"""

MIGRATION_SQL = """
ALTER TABLE ticks ADD COLUMN IF NOT EXISTS bid DOUBLE PRECISION;
ALTER TABLE ticks ADD COLUMN IF NOT EXISTS ask DOUBLE PRECISION;
ALTER TABLE ticks ADD COLUMN IF NOT EXISTS last DOUBLE PRECISION;
ALTER TABLE ticks ADD COLUMN IF NOT EXISTS flags INTEGER;
UPDATE ticks
SET last = price
WHERE bid IS NULL
  AND ask IS NULL
  AND last IS NULL
  AND price IS NOT NULL;
"""

INSERT_SQL = (
    "INSERT INTO ticks (symbol, price, bid, ask, last, volume, time, time_msc, flags) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING"
)
