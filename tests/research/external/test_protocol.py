"""ExternalDataSource Protocol — abstraction for any daily-OHLCV data source.

Implementations: yfinance (Phase 1) → stooq / fred / polygon / databento (future).
Registry pattern keeps `backfill.py` CLI source-agnostic; new sources need only
register a factory.
"""

from __future__ import annotations

from datetime import date
from typing import List

import pytest

from src.research.external.protocol import (
    DailyBar,
    ExternalDataSource,
    UnknownSourceError,
    get_source,
    list_registered_sources,
    register_source,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    """Snapshot _REGISTRY before each test, restore after — so tests can register
    dummy sources without polluting the next test's view."""
    from src.research.external import protocol as _proto

    saved = dict(_proto._REGISTRY)
    yield
    _proto._REGISTRY.clear()
    _proto._REGISTRY.update(saved)


def test_daily_bar_is_frozen_dataclass() -> None:
    bar = DailyBar(
        symbol="GC=F",
        date=date(2026, 4, 1),
        open=2300.0,
        high=2310.0,
        low=2295.0,
        close=2305.0,
        volume=250000.0,
    )
    with pytest.raises((AttributeError, TypeError)):
        bar.symbol = "X"  # type: ignore


def test_register_and_get_source_round_trip() -> None:
    class DummySource:
        name = "dummy_test_source"

        def fetch_daily(self, symbol, *, start, end):
            return []

        def supports_symbol(self, symbol):
            return True

    register_source("dummy_test_source", lambda: DummySource())
    src = get_source("dummy_test_source")
    assert isinstance(src, DummySource)
    assert "dummy_test_source" in list_registered_sources()


def test_register_source_rejects_non_callable_factory() -> None:
    class DummyInstance:
        name = "dummy"

        def fetch_daily(self, symbol, *, start, end):
            return []

        def supports_symbol(self, symbol):
            return True

    with pytest.raises(TypeError) as exc:
        register_source("oops", DummyInstance())  # type: ignore[arg-type]
    assert "callable" in str(exc.value).lower()


def test_get_source_unknown_raises() -> None:
    with pytest.raises(UnknownSourceError) as exc:
        get_source("nonexistent_source_xyz")
    assert "nonexistent_source_xyz" in str(exc.value)
    assert "registered:" in str(exc.value).lower()


def test_external_data_source_is_runtime_checkable() -> None:
    class GoodImpl:
        name = "good"

        def fetch_daily(self, symbol, *, start, end):
            return []

        def supports_symbol(self, symbol):
            return True

    class BadImpl:
        name = "bad"
        # missing fetch_daily

    assert isinstance(GoodImpl(), ExternalDataSource)
    assert not isinstance(BadImpl(), ExternalDataSource)
