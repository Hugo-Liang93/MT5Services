"""daily_external_ohlc table schema — daily OHLCV for non-broker external symbols
(CME GC, DXY, ^TNX, ^GSPC). Composite PK (symbol, date). TimescaleDB hypertable
on date column; symbol is text to keep schema generic for future symbols.
"""
from src.persistence.schema.daily_external_ohlc import DDL, INSERT_SQL


def test_ddl_creates_hypertable_with_composite_pk() -> None:
    assert "CREATE TABLE IF NOT EXISTS daily_external_ohlc" in DDL
    assert "PRIMARY KEY (symbol, date)" in DDL
    assert "create_hypertable" in DDL
    assert "'date'" in DDL  # hypertable time column


def test_insert_sql_has_seven_placeholders() -> None:
    # symbol, date, open, high, low, close, volume
    assert INSERT_SQL.count("%s") == 7
    assert "ON CONFLICT (symbol, date) DO UPDATE" in INSERT_SQL


def test_indices_for_query_patterns() -> None:
    # Need symbol + date DESC for "latest N days for symbol X" pattern
    assert "idx_daily_external_ohlc_symbol_date" in DDL
