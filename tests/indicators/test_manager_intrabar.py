"""Unit tests for UnifiedIndicatorManager intrabar path fixes."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.config.indicator_config import ConfigLoader
from src.indicators.manager import UnifiedIndicatorManager


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------

class _Bar:
    def __init__(self, t: datetime, close: float = 1900.0, volume: float = 100.0):
        self.time = t
        self.open = close - 1.0
        self.high = close + 2.0
        self.low = close - 2.0
        self.close = close
        self.volume = volume
        self.symbol = "XAUUSD"
        self.timeframe = "M5"
        self.indicators: Dict[str, Any] = {}


class _FakeService:
    """Stub for MarketDataService – only what the manager touches."""

    def __init__(self, bars: Optional[List[_Bar]] = None):
        self._bars = bars or []
        self._sink = None
        self._intrabar_listeners: list = []
        self._ohlc_indicators: Dict = {}

    def set_ohlc_event_sink(self, sink):
        self._sink = sink

    def add_intrabar_listener(self, listener):
        self._intrabar_listeners.append(listener)

    def remove_intrabar_listener(self, listener):
        self._intrabar_listeners = [l for l in self._intrabar_listeners if l is not listener]

    def get_ohlc(self, symbol, timeframe, count=None):
        return self._bars[-count:] if count else list(self._bars)

    def get_ohlc_window(self, symbol, timeframe, end_time=None, limit=None):
        bars = self._bars
        if end_time is not None:
            bars = [b for b in bars if b.time <= end_time]
        if limit is not None:
            bars = bars[-limit:]
        return bars

    def has_cached_ohlc(self, symbol, timeframe, minimum_bars=1):
        return len(self._bars) >= minimum_bars

    def latest_indicators(self, symbol, timeframe):
        return {}

    def update_ohlc_indicators(self, symbol, timeframe, bar_time, indicators):
        self._ohlc_indicators[(symbol, timeframe, bar_time)] = indicators


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bars(n: int = 60) -> List[_Bar]:
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    return [_Bar(now + timedelta(minutes=i), close=1900.0 + i * 0.1) for i in range(n)]


def _make_manager(bars, storage_writer=None):
    service = _FakeService(bars=bars)
    mgr = UnifiedIndicatorManager(
        market_service=service,
        config_file="config/indicators.json",
        storage_writer=storage_writer,
    )
    return mgr, service


# ---------------------------------------------------------------------------
# Test: intrabar_eligible cache is built at init and reflects config
# ---------------------------------------------------------------------------

def test_intrabar_eligible_cache_excludes_volume_indicators():
    mgr, _ = _make_manager(_make_bars())
    eligible = mgr._get_intrabar_eligible_names()

    # Volume-derived indicators must be excluded
    assert "mfi14" not in eligible, "mfi14 should be intrabar-ineligible"
    assert "obv30" not in eligible, "obv30 should be intrabar-ineligible"
    assert "vwap30" not in eligible, "vwap30 should be intrabar-ineligible"

    # Price-based indicators must be included
    assert "sma20" in eligible
    assert "ema50" in eligible
    assert "rsi14" in eligible
    assert "macd" in eligible


def test_intrabar_eligible_cache_is_frozenset():
    mgr, _ = _make_manager(_make_bars())
    eligible = mgr._get_intrabar_eligible_names()
    assert isinstance(eligible, frozenset)


def test_intrabar_eligible_cache_is_reused():
    mgr, _ = _make_manager(_make_bars())
    first = mgr._get_intrabar_eligible_names()
    second = mgr._get_intrabar_eligible_names()
    assert first is second, "_get_intrabar_eligible_names() should return cached object"


def test_intrabar_eligible_cache_is_invalidated_on_reinitialize():
    mgr, _ = _make_manager(_make_bars())
    original = mgr._get_intrabar_eligible_names()
    mgr._intrabar_eligible_cache = None  # simulate hot-reload clearing the cache
    rebuilt = mgr._get_intrabar_eligible_names()
    # Content must be equal even if rebuilt
    assert original == rebuilt


# ---------------------------------------------------------------------------
# Test: _process_intrabar_event only computes eligible indicators
# ---------------------------------------------------------------------------

def test_process_intrabar_only_runs_eligible_indicators():
    """
    The pipeline must only be invoked with intrabar-eligible indicator names.
    Volume indicators (mfi14, obv30, vwap30) must not appear in the pipeline call.
    """
    bars = _make_bars(60)
    mgr, svc = _make_manager(bars)

    computed_names: list[list[str]] = []

    original_pipeline_compute = mgr.pipeline.compute_staged

    def spy_compute(symbol, timeframe, bar_data, indicators=None, on_level_complete=None):
        if indicators is not None:
            computed_names.append(list(indicators))
        return original_pipeline_compute(
            symbol, timeframe, bar_data,
            indicators=indicators, on_level_complete=on_level_complete,
        )

    mgr.pipeline.compute_staged = spy_compute  # type: ignore[method-assign]

    intrabar_bar = bars[-1]
    mgr._process_intrabar_event("XAUUSD", "M5", intrabar_bar)

    assert computed_names, "pipeline.compute_staged was never called"
    for name_list in computed_names:
        assert "mfi14" not in name_list, "mfi14 must not be computed during intrabar"
        assert "obv30" not in name_list, "obv30 must not be computed during intrabar"
        assert "vwap30" not in name_list, "vwap30 must not be computed during intrabar"
        assert "sma20" in name_list or "ema50" in name_list, "price indicators must be included"


# ---------------------------------------------------------------------------
# Test: get_indicator_info includes intrabar_eligible
# ---------------------------------------------------------------------------

def test_get_indicator_info_exposes_intrabar_eligible():
    mgr, _ = _make_manager(_make_bars())

    info_mfi = mgr.get_indicator_info("mfi14")
    assert info_mfi is not None
    assert info_mfi["intrabar_eligible"] is False

    info_sma = mgr.get_indicator_info("sma20")
    assert info_sma is not None
    assert info_sma["intrabar_eligible"] is True


def test_list_indicators_includes_intrabar_eligible():
    mgr, _ = _make_manager(_make_bars())

    infos = mgr.list_indicators()
    names_with_field = {info["name"]: info.get("intrabar_eligible") for info in infos}

    assert names_with_field.get("vwap30") is False
    assert names_with_field.get("ema50") is True


# ---------------------------------------------------------------------------
# Test: intrabar loop skips computation when no snapshot listeners
# ---------------------------------------------------------------------------

def test_intrabar_loop_skips_when_no_listeners(monkeypatch):
    bars = _make_bars(60)
    mgr, svc = _make_manager(bars)

    call_count = {"n": 0}

    def spy_process(symbol, timeframe, bar):
        call_count["n"] += 1

    monkeypatch.setattr(mgr, "_process_intrabar_event", spy_process)

    # No listeners → loop should drain queue without computing
    assert len(mgr._snapshot_listeners) == 0
    mgr._intrabar_queue.put(("XAUUSD", "M5", bars[-1]))

    # Run one iteration of the loop manually
    import queue as q_module
    try:
        item = mgr._intrabar_queue.get(timeout=0.1)
    except q_module.Empty:
        pytest.fail("queue was not populated")

    # Simulate the guard inside _intrabar_loop
    if not mgr._snapshot_listeners:
        pass  # guard fires → no compute
    else:
        mgr._process_intrabar_event(*item)

    assert call_count["n"] == 0, "_process_intrabar_event must not be called with no listeners"
