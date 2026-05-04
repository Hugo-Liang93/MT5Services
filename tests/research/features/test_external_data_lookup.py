"""external_data_lookup — shared factory for FeatureProviders to query
daily_external_ohlc by (symbol, field, date), with per-instance caching.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.research.features.external_data_lookup import (
    make_daily_field_lookup,
    SUPPORTED_FIELDS,
)


def test_supported_fields_match_schema() -> None:
    # daily_external_ohlc columns minus PK (symbol, date)
    assert SUPPORTED_FIELDS == frozenset({"open", "high", "low", "close", "volume"})


def test_make_daily_field_lookup_rejects_unknown_field() -> None:
    with pytest.raises(ValueError) as exc:
        make_daily_field_lookup("GC=F", field="bid")
    assert "bid" in str(exc.value)
    assert "supported:" in str(exc.value).lower()


def test_lookup_caches_per_date() -> None:
    """Two calls with same date hit DB only once."""
    fake_writer = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.fetchone.return_value = (250000.0,)
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    fake_writer.connection.return_value.__enter__.return_value = fake_conn

    lookup = make_daily_field_lookup(
        "GC=F", field="volume", _writer_factory=lambda: fake_writer
    )

    v1 = lookup(date(2026, 4, 1))
    v2 = lookup(date(2026, 4, 1))  # same date
    assert v1 == 250000.0
    assert v2 == 250000.0
    # DB called only once despite two lookups
    assert fake_cursor.execute.call_count == 1


def test_lookup_returns_none_when_row_missing() -> None:
    fake_writer = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.fetchone.return_value = None  # no row
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    fake_writer.connection.return_value.__enter__.return_value = fake_conn

    lookup = make_daily_field_lookup(
        "GC=F", field="volume", _writer_factory=lambda: fake_writer
    )

    assert lookup(date(2026, 4, 1)) is None


def test_writer_factory_deferred_until_first_lookup() -> None:
    """make_daily_field_lookup must NOT call _writer_factory at construction —
    TimescaleWriter eagerly opens a connection pool, so building it for a
    provider that never gets queried (e.g. backtest filtered out the symbol)
    is wasteful."""
    factory_calls = {"count": 0}

    def tracking_factory() -> MagicMock:
        factory_calls["count"] += 1
        fake_writer = MagicMock()
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = (1.0,)
        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
        fake_writer.connection.return_value.__enter__.return_value = fake_conn
        return fake_writer

    lookup = make_daily_field_lookup(
        "GC=F", field="volume", _writer_factory=tracking_factory
    )
    assert factory_calls["count"] == 0  # not called yet

    lookup(date(2026, 4, 1))
    assert factory_calls["count"] == 1  # called on first lookup

    lookup(date(2026, 4, 2))
    assert factory_calls["count"] == 1  # writer reused — not called again


def test_lookup_independent_caches_per_factory_call() -> None:
    """Two calls to make_daily_field_lookup return independent caches."""
    fake_writer = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.fetchone.return_value = (100.0,)
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    fake_writer.connection.return_value.__enter__.return_value = fake_conn

    lookup_a = make_daily_field_lookup(
        "GC=F", field="volume", _writer_factory=lambda: fake_writer
    )
    lookup_b = make_daily_field_lookup(
        "DX-Y.NYB", field="close", _writer_factory=lambda: fake_writer
    )

    lookup_a(date(2026, 4, 1))
    lookup_b(date(2026, 4, 1))
    # Different (symbol, field) → 2 separate DB queries
    assert fake_cursor.execute.call_count == 2
