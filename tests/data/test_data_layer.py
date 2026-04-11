from __future__ import annotations

from datetime import datetime, timedelta, timezone

import src.persistence.db as db_module
from src.config import DBSettings
from src.persistence.db import TimescaleWriter
from src.persistence.validator import DataValidator


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_settings() -> DBSettings:
    return DBSettings(
        pg_host="localhost",
        pg_port=5432,
        pg_user="postgres",
        pg_password="postgres",
        pg_database="mt5",
        pg_schema="public",
    )


class _FakePool:
    def __init__(self, min_conn: int, max_conn: int, **kwargs) -> None:
        self.min_conn = min_conn
        self.max_conn = max_conn
        self.kwargs = kwargs
        self.closed = False
        self._used = 0
        self._rused = 0

    def closeall(self) -> None:
        self.closed = True


def test_validator_accepts_valid_market_rows() -> None:
    valid_tick = ("EURUSD", 1.12345, 1000.0, _now_iso())
    valid_quote = ("EURUSD", 1.1234, 1.1235, 1.12345, 1000.0, _now_iso())
    valid_ohlc = ("EURUSD", "M1", 1.1234, 1.1240, 1.1230, 1.1238, 1000.0, _now_iso(), {})
    valid_intrabar = (
        "EURUSD",
        "M1",
        1.1234,
        1.1240,
        1.1230,
        1.1238,
        1000.0,
        _now_iso(),
        _now_iso(),
    )

    assert DataValidator.validate_tick(*valid_tick) == (True, "")
    assert DataValidator.validate_quote(*valid_quote) == (True, "")
    assert DataValidator.validate_ohlc(*valid_ohlc) == (True, "")
    assert DataValidator.validate_intrabar(*valid_intrabar) == (True, "")


def test_validator_rejects_invalid_market_rows() -> None:
    future_time = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    tick_valid, tick_msg = DataValidator.validate_tick("EURUSD", -1.0, 1000.0, _now_iso())
    future_valid, future_msg = DataValidator.validate_tick("EURUSD", 1.12345, 1000.0, future_time)
    quote_valid, quote_msg = DataValidator.validate_quote("EURUSD", 1.1236, 1.1235, 1.12345, 1000.0, _now_iso())
    ohlc_valid, ohlc_msg = DataValidator.validate_ohlc(
        "EURUSD",
        "M1",
        1.1234,
        1.1220,
        1.1230,
        1.1238,
        1000.0,
        _now_iso(),
        {},
    )
    intrabar_valid, intrabar_msg = DataValidator.validate_intrabar(
        "EURUSD",
        "M1",
        1.1234,
        1.1240,
        1.1230,
        1.1238,
        1000.0,
        _now_iso(),
        (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    )

    assert tick_valid is False
    assert "Invalid price" in tick_msg
    assert future_valid is False
    assert "Future time detected" in future_msg
    assert quote_valid is False
    assert "Bid > Ask" in quote_msg
    assert ohlc_valid is False
    assert "Price relationship invalid" in ohlc_msg
    assert intrabar_valid is False
    assert "Recorded time before bar time" in intrabar_msg


def test_filtering_keeps_only_valid_rows() -> None:
    now_iso = _now_iso()

    ticks = [
        ("EURUSD", 1.12345, 1000.0, now_iso),
        ("EURUSD", -1.12345, 1000.0, now_iso),
        ("USDJPY", 150.123, 2000.0, now_iso),
        ("BROKEN",),
    ]
    quotes = [
        ("EURUSD", 1.1234, 1.1235, 1.12345, 1000.0, now_iso),
        ("EURUSD", 1.1236, 1.1235, 1.12345, 1000.0, now_iso),
        ("USDJPY", 150.123, 150.124, 150.1235, 2000.0, now_iso),
    ]
    ohlc_rows = [
        ("EURUSD", "M1", 1.1234, 1.1240, 1.1230, 1.1238, 1000.0, now_iso, {}),
        ("EURUSD", "M1", 1.1234, 1.1220, 1.1230, 1.1238, 1000.0, now_iso, {}),
    ]

    assert DataValidator.filter_valid_ticks(ticks) == [
        ("EURUSD", 1.12345, 1000.0, now_iso),
        ("USDJPY", 150.123, 2000.0, now_iso),
    ]
    assert DataValidator.filter_valid_quotes(quotes) == [
        ("EURUSD", 1.1234, 1.1235, 1.12345, 1000.0, now_iso),
        ("USDJPY", 150.123, 150.124, 150.1235, 2000.0, now_iso),
    ]
    assert DataValidator.filter_valid_ohlc(ohlc_rows) == [
        ("EURUSD", "M1", 1.1234, 1.1240, 1.1230, 1.1238, 1000.0, now_iso, {}),
    ]


def test_timescale_writer_initializes_connection_pool_from_settings(monkeypatch) -> None:
    created: dict[str, _FakePool] = {}

    def _fake_pool_factory(min_conn: int, max_conn: int, **kwargs) -> _FakePool:
        pool = _FakePool(min_conn, max_conn, **kwargs)
        created["pool"] = pool
        return pool

    monkeypatch.setattr(db_module, "SimpleConnectionPool", _fake_pool_factory)

    writer = TimescaleWriter(_db_settings(), min_conn=2, max_conn=4)
    stats = writer.get_pool_stats()

    assert created["pool"].min_conn == 2
    assert created["pool"].max_conn == 4
    assert created["pool"].kwargs["host"] == "localhost"
    assert created["pool"].kwargs["port"] == 5432
    assert created["pool"].kwargs["dbname"] == "mt5"
    assert stats["status"] == "healthy"
    assert stats["min_connections"] == 2
    assert stats["max_connections"] == 4

    writer.close()

    assert created["pool"].closed is True


def test_timescale_writer_pool_stats_report_uninitialized_pool() -> None:
    writer = TimescaleWriter.__new__(TimescaleWriter)
    writer._pool = None

    assert writer.get_pool_stats() == {"status": "pool_not_initialized"}
