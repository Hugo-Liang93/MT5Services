from __future__ import annotations

import queue
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.signals.models import SignalDecision
from src.signals.orchestration.runtime import SignalRuntime, SignalTarget
from src.signals.orchestration.runtime_processing import SignalScopeError


class _SnapshotSource:
    def add_snapshot_listener(self, _listener):
        pass

    def remove_snapshot_listener(self, _listener):
        pass

    def get_current_trace_id(self):
        return None

    def get_indicator(self, _symbol, _timeframe, _indicator_name):
        return None

    def get_performance_stats(self):
        return {}

    def get_current_spread(self, _symbol):
        return 0.0

    def get_symbol_point(self, _symbol):
        return 0.0001


class _Service:
    def strategy_capability_catalog(self):
        return [
            {
                "name": "tick_probe",
                "valid_scopes": ["tick_derived"],
                "needed_indicators": [],
                "needs_intrabar": False,
                "needs_htf": False,
                "regime_affinity": {},
                "htf_requirements": {},
                "market_data_requirements": ["tick"],
            }
        ]

    def evaluate(
        self,
        *,
        symbol,
        timeframe,
        strategy,
        indicators,
        metadata,
        persist=False,
        htf_indicators=None,
    ):
        return SignalDecision(
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            direction="buy",
            confidence=0.82,
            reason="tick probe",
            timestamp=metadata["snapshot_time"],
            metadata={"raw_confidence": 0.82},
        )

    def persist_decision(self, decision, *, indicators, metadata):
        return SimpleNamespace(signal_id="sig-tick-runtime-1")

    def get_strategy(self, _strategy):
        return SimpleNamespace(category="tick_probe")


def _runtime() -> SignalRuntime:
    return SignalRuntime(
        service=_Service(),
        snapshot_source=_SnapshotSource(),
        targets=[SignalTarget("EURUSD", "M1", "tick_probe")],
    )


def _event(scope: str = "tick_derived") -> tuple:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return (
        scope,
        "EURUSD",
        "M1",
        {},
        {
            "snapshot_time": now,
            "bar_time": now,
        },
    )


def test_tick_derived_enqueue_uses_dedicated_queue() -> None:
    runtime = _runtime()

    runtime._enqueue(_event())

    assert runtime._tick_derived_events.qsize() == 1
    assert runtime._intrabar_events.qsize() == 0
    assert runtime._confirmed_events.qsize() == 0


def test_tick_derived_overflow_updates_tick_specific_drop_stats() -> None:
    runtime = _runtime()
    runtime._tick_derived_events = queue.Queue(maxsize=1)

    runtime._enqueue(_event())
    runtime._enqueue(_event())

    assert runtime._tick_derived_events.qsize() == 1
    assert runtime._dropped_events == 1
    assert runtime._dropped_tick_derived == 1
    assert runtime._dropped_intrabar == 0


def test_unknown_scope_raises_structured_error() -> None:
    runtime = _runtime()

    with pytest.raises(SignalScopeError):
        runtime._enqueue(_event("unknown"))


def test_runtime_status_includes_tick_derived_queue_stats() -> None:
    runtime = _runtime()
    runtime._enqueue(_event())

    status = runtime.status()

    assert status["tick_derived_queue_size"] == 1
    assert status["tick_derived_queue_capacity"] == 8192
    assert status["dropped_tick_derived"] == 0
    assert "tick_derived" in status["filter_by_scope"]


def test_tick_derived_actionable_decision_publishes_signal_event() -> None:
    runtime = _runtime()
    events = []
    runtime.add_signal_listener(events.append)

    runtime._enqueue(_event())
    processed = runtime.process_next_event(timeout=0.01)

    assert processed is True
    assert len(events) == 1
    event = events[0]
    assert event.scope == "tick_derived"
    assert event.signal_state == "tick_derived_buy"
    assert event.signal_id == "sig-tick-runtime-1"
    assert event.metadata["signal_state"] == "tick_derived_buy"
