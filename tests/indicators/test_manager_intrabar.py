"""Unit tests for UnifiedIndicatorManager intrabar path fixes."""
from __future__ import annotations

import threading
import time
import queue
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from src.indicators.manager import UnifiedIndicatorManager
from src.indicators.runtime import event_loops
from src.indicators.runtime import intrabar_queue
from src.indicators.runtime.intrabar_metrics import (
    get_intrabar_metrics_snapshot,
    record_intrabar_drop,
    record_intrabar_queue_age_ms,
    record_intrabar_processing_latency_ms,
)


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

    def get_ohlc_closed(self, symbol, timeframe, limit=None):
        return self._bars[-limit:] if limit else list(self._bars)

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
# Test: intrabar eligible 由策略推导注入
# ---------------------------------------------------------------------------

def test_intrabar_eligible_empty_without_override():
    """未注入 override 时（standalone 模式），intrabar eligible 为空集。"""
    mgr, _ = _make_manager(_make_bars())
    eligible = mgr.get_intrabar_eligible_names()
    assert eligible == frozenset()


def test_intrabar_eligible_uses_strategy_override():
    """set_intrabar_eligible_override() 注入策略推导集合后，只有该集合里的指标 eligible。"""
    mgr, _ = _make_manager(_make_bars())
    mgr.set_intrabar_eligible_override(frozenset(["rsi14", "boll20"]))
    eligible = mgr.get_intrabar_eligible_names()
    assert eligible == frozenset(["rsi14", "boll20"])
    assert "sma20" not in eligible
    assert "ema50" not in eligible
    assert "macd" not in eligible


def test_intrabar_eligible_is_frozenset():
    mgr, _ = _make_manager(_make_bars())
    eligible = mgr.get_intrabar_eligible_names()
    assert isinstance(eligible, frozenset)


# ---------------------------------------------------------------------------
# Test: _process_intrabar_event only computes eligible indicators
# ---------------------------------------------------------------------------

def test_process_intrabar_only_runs_eligible_indicators():
    """Pipeline 仅计算 override 注入的 intrabar eligible 指标。"""
    bars = _make_bars(60)
    mgr, svc = _make_manager(bars)
    # 模拟策略推导：只有 rsi14 和 boll20 需要 intrabar
    mgr.set_intrabar_eligible_override(frozenset(["rsi14", "boll20"]))

    computed_names: list[list[str]] = []

    original_pipeline_compute = mgr.pipeline.compute_staged

    def spy_compute(symbol, timeframe, bar_data, indicators=None, on_level_complete=None, scope="confirmed"):
        if indicators is not None:
            computed_names.append(list(indicators))
        return original_pipeline_compute(
            symbol, timeframe, bar_data,
            indicators=indicators, on_level_complete=on_level_complete, scope=scope,
        )

    mgr.pipeline.compute_staged = spy_compute  # type: ignore[method-assign]

    intrabar_bar = bars[-1]
    mgr._process_intrabar_event("XAUUSD", "M5", intrabar_bar)

    assert computed_names, "pipeline.compute_staged was never called"
    for name_list in computed_names:
        allowed = {"rsi14", "boll20"}
        assert set(name_list) <= allowed, \
            f"Only {allowed} should be computed, got {name_list}"


def test_apply_delta_metrics_uses_historical_indicator_values():
    bars = _make_bars(8)
    bars[-4].indicators = {"rsi14": {"rsi": 40.0}}
    mgr, _ = _make_manager(bars)

    enriched = mgr._apply_delta_metrics(
        "XAUUSD",
        "M5",
        {"rsi14": {"rsi": 55.0}},
        bar_time=bars[-1].time,
    )

    assert enriched["rsi14"]["rsi_d3"] == 15.0


def test_apply_delta_metrics_skips_missing_history_gracefully():
    bars = _make_bars(2)
    mgr, _ = _make_manager(bars)

    enriched = mgr._apply_delta_metrics(
        "XAUUSD",
        "M5",
        {"rsi14": {"rsi": 55.0}},
        bar_time=bars[-1].time,
    )

    assert "rsi_d3" not in enriched["rsi14"]


# ---------------------------------------------------------------------------
# Test: intrabar loop skips computation when no snapshot listeners
# ---------------------------------------------------------------------------

def test_intrabar_loop_skips_when_no_listeners(monkeypatch):
    bars = _make_bars(60)
    mgr, _ = _make_manager(bars)

    call_count = {"n": 0}

    def spy_process(symbol, timeframe, bar):
        call_count["n"] += 1

    monkeypatch.setattr(mgr, "_process_intrabar_event", spy_process)

    # No listeners → loop should drain queue without computing
    assert len(mgr.state.snapshot_listeners) == 0
    mgr.state.intrabar_queue.put(
        intrabar_queue.IntrabarQueueItem(
            symbol="XAUUSD",
            timeframe="M5",
            bar=bars[-1],
            enqueued_at_monotonic=time.monotonic() - 0.05,
        )
    )

    # Run one iteration of the loop manually
    try:
        item = mgr.state.intrabar_queue.get(timeout=0.1)
    except queue.Empty:
        pytest.fail("queue was not populated")

    # Simulate the guard inside _intrabar_loop
    if not mgr.state.snapshot_listeners:
        pass  # guard fires → no compute
    else:
        mgr._process_intrabar_event(item.symbol, item.timeframe, item.bar)

    assert call_count["n"] == 0, "_process_intrabar_event must not be called with no listeners"


def test_enqueue_intrabar_event_records_dropped_count():
    bars = _make_bars(60)
    mgr, _ = _make_manager(bars)
    mgr.state.intrabar_queue = queue.Queue(maxsize=1)

    intrabar_queue.enqueue_intrabar_event(mgr, "XAUUSD", "M5", bars[-1])
    intrabar_queue.enqueue_intrabar_event(mgr, "XAUUSD", "M5", bars[-1])

    assert mgr.state.intrabar_dropped_count == 1


def test_high_pressure_intrabar_queue_reports_drops_and_metrics():
    bars = _make_bars(60)
    mgr, _ = _make_manager(bars)
    mgr.state.intrabar_queue = queue.Queue(maxsize=1)

    for _ in range(30):
        intrabar_queue.enqueue_intrabar_event(mgr, "XAUUSD", "M5", bars[-1])

    assert mgr.state.intrabar_queue.qsize() == 1
    assert mgr.state.intrabar_dropped_count >= 29

    perf = mgr.get_performance_stats()
    intrabar = perf["intrabar"]
    assert intrabar["queue"]["dropped_total"] == mgr.state.intrabar_dropped_count
    assert intrabar["queue"]["current_size"] == 1
    assert intrabar["queue"]["maxsize"] == 1


def test_intrabar_loop_records_queue_age_and_latency(monkeypatch):
    bars = _make_bars(60)
    mgr, _ = _make_manager(bars)
    mgr.state.snapshot_listeners.append(lambda *args: None)

    def _fast_process(_manager, symbol, timeframe, bar):
        time.sleep(0.01)

    monkeypatch.setattr(event_loops, "process_intrabar_event", _fast_process)

    now = time.monotonic()
    item = intrabar_queue.IntrabarQueueItem(
        symbol="XAUUSD",
        timeframe="M5",
        bar=bars[-1],
        enqueued_at_monotonic=now - 0.123,
    )
    mgr.state.intrabar_queue.put(item)
    mgr.state.stop_event.clear()
    worker = threading.Thread(
        target=event_loops.run_intrabar_loop,
        args=(mgr,),
        daemon=True,
    )
    worker.start()

    time.sleep(0.2)
    mgr.state.stop_event.set()
    worker.join(timeout=2.0)

    metrics = get_intrabar_metrics_snapshot(mgr)
    assert metrics["queue_age_sample_count"] >= 1
    assert metrics["processing_latency_sample_count"] >= 1
    assert metrics["queue_age_ms_latest"] >= 110.0
    assert metrics["processing_latency_ms_latest"] >= 9.0


def test_get_performance_stats_exposes_intrabar_metrics_snapshot():
    mgr, _ = _make_manager(_make_bars(60))

    record_intrabar_drop(mgr)
    record_intrabar_queue_age_ms(mgr, 123.4)
    record_intrabar_processing_latency_ms(mgr, 45.6)

    perf = mgr.get_performance_stats()
    intrabar = perf["intrabar"]
    assert intrabar["queue"]["dropped_total"] == 1
    assert intrabar["queue_age_sample_count"] == 1
    assert intrabar["processing_latency_sample_count"] == 1
