"""Tests for TradeExecutor async execution mechanics.

Validates the non-blocking queue-based architecture:
- on_signal_event is non-blocking
- flush waits for completion
- shutdown stops the worker
- queue overflow drops events without blocking
- worker survives exceptions
- worker thread is lazily started
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.signals.models import SignalEvent
from src.trading.execution_gate import ExecutionGate, ExecutionGateConfig
from src.trading.signal_executor import ExecutorConfig, TradeExecutor


# ---------------------------------------------------------------------------
# Helpers (adapted from test_signal_executor.py)
# ---------------------------------------------------------------------------

class DummyTradingModule:
    def __init__(self, *, delay: float = 0.0, error: Exception | None = None):
        self.calls: list[tuple] = []
        self._delay = delay
        self._error = error

    def dispatch_operation(self, operation: str, payload: dict) -> dict:
        if self._delay > 0:
            time.sleep(self._delay)
        if self._error is not None:
            raise self._error
        self.calls.append((operation, payload))
        return {"ticket": 1, "payload": payload}

    def account_info(self) -> dict:
        return {"equity": 10000.0}

    def get_positions(self, symbol: str | None = None) -> list:
        return []


def _build_event(
    *,
    action: str = "buy",
    confidence: float = 0.9,
    signal_id: str = "sig_test",
) -> SignalEvent:
    return SignalEvent(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="sma_trend",
        action=action,
        confidence=confidence,
        signal_state=f"confirmed_{action}",
        scope="confirmed",
        indicators={
            "atr14": {"atr": 2.0},
            "sma20": {"sma": 3000.0},
        },
        metadata={
            "previous_state": "armed_buy",
            "spread_points": 20.0,
            "spread_price": 0.20,
            "symbol_point": 0.01,
            "close_price": 3000.0,
        },
        generated_at=datetime.now(timezone.utc),
        signal_id=signal_id,
        reason="test",
    )


def _make_executor(
    module: DummyTradingModule | None = None,
    *,
    enabled: bool = True,
    maxsize: int = 64,
) -> TradeExecutor:
    """Create a TradeExecutor with sane test defaults."""
    mod = module or DummyTradingModule()
    executor = TradeExecutor(
        trading_module=mod,
        config=ExecutorConfig(
            enabled=enabled,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_spread_to_stop_ratio=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )
    # Override queue size for tests that need a small queue
    if maxsize != 64:
        executor._exec_queue = queue.Queue(maxsize=maxsize)
    return executor


# ===========================================================================
# Tests
# ===========================================================================

def test_on_signal_event_is_non_blocking() -> None:
    """on_signal_event must return in < 50ms even when worker is slow."""
    module = DummyTradingModule(delay=1.0)  # worker takes 1 second per event
    executor = _make_executor(module)

    event = _build_event()
    t0 = time.perf_counter()
    executor.on_signal_event(event)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert elapsed_ms < 50.0, f"on_signal_event took {elapsed_ms:.1f}ms, expected <50ms"
    executor.shutdown()


def test_flush_waits_for_completion() -> None:
    """flush() blocks until all queued events have been processed."""
    module = DummyTradingModule(delay=0.05)
    executor = _make_executor(module)

    # Enqueue 3 events
    for i in range(3):
        executor.on_signal_event(_build_event(signal_id=f"sig_{i}"))

    # flush should block until all 3 are processed
    executor.flush(timeout=10.0)

    assert len(module.calls) == 3
    executor.shutdown()


def test_shutdown_stops_worker() -> None:
    """After shutdown(), the worker thread must exit."""
    executor = _make_executor()

    # Trigger lazy worker start
    executor.on_signal_event(_build_event())
    executor.flush()

    thread = executor._exec_thread
    assert thread is not None
    assert thread.is_alive()

    executor.shutdown()

    # Thread should have joined within the shutdown timeout
    assert not thread.is_alive()


def test_queue_overflow_drops_event() -> None:
    """When queue is full, on_signal_event retries once (3s) then drops."""
    module = DummyTradingModule(delay=5.0)  # very slow worker
    executor = _make_executor(module, maxsize=1)

    # First event goes into the queue
    executor.on_signal_event(_build_event(signal_id="sig_0"))
    # Give worker a moment to pick up the first event (so queue is drained)
    time.sleep(0.1)

    # Now queue is empty but worker is busy with sig_0.
    # Fill the queue with one more event.
    executor.on_signal_event(_build_event(signal_id="sig_1"))

    # Third event: queue full → backpressure retry (3s timeout) → then drop.
    t0 = time.perf_counter()
    executor.on_signal_event(_build_event(signal_id="sig_2"))
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Backpressure retry takes ~3s, allow up to 4000ms for CI jitter
    assert elapsed_ms < 4000.0, f"on_signal_event blocked for {elapsed_ms:.1f}ms"
    assert executor._execution_quality["queue_overflows"] == 1
    executor.shutdown()


def test_worker_handles_exception() -> None:
    """Worker thread survives exceptions in _handle_confirmed and processes next event."""
    module = DummyTradingModule()
    executor = _make_executor(module)

    # Temporarily break _handle_confirmed to raise on first call
    call_count = {"n": 0}
    original_handle = executor._handle_confirmed

    def flaky_handle(event: SignalEvent):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure")
        return original_handle(event)

    executor._handle_confirmed = flaky_handle  # type: ignore[assignment]

    # Send two events: first will fail, second should succeed
    executor.on_signal_event(_build_event(signal_id="sig_fail"))
    executor.on_signal_event(_build_event(signal_id="sig_ok"))
    executor.flush(timeout=5.0)

    # Worker survived the exception and processed the second event
    assert call_count["n"] == 2
    assert len(module.calls) == 1  # only second event succeeded
    executor.shutdown()


def test_lazy_worker_start() -> None:
    """Worker thread is NOT started at construction; only on first on_signal_event."""
    executor = _make_executor()

    # At construction, no thread should exist
    assert executor._exec_thread is None

    # Send an intrabar event (filtered out by on_signal_event, should not start worker)
    intrabar_event = SignalEvent(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="rsi_reversion",
        action="buy",
        confidence=0.8,
        signal_state="preview_buy",
        scope="intrabar",  # not "confirmed" => early return
        indicators={},
        metadata={},
        generated_at=datetime.now(timezone.utc),
        signal_id="sig_intra",
        reason="test",
    )
    executor.on_signal_event(intrabar_event)

    # Intrabar events are filtered before worker start
    assert executor._exec_thread is None

    # Now send a confirmed event — this should start the worker
    executor.on_signal_event(_build_event())
    assert executor._exec_thread is not None
    assert executor._exec_thread.is_alive()

    executor.flush()
    executor.shutdown()
