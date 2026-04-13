from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from src.indicators.query_services.runtime import publish_snapshot
from src.monitoring.pipeline import PipelineEventBus


class _ManagerStub:
    def __init__(self) -> None:
        self.state = SimpleNamespace(
            pipeline_event_bus=None,
            snapshot_listeners=[],
            snapshot_listeners_lock=threading.Lock(),
        )
        self.current_trace_id: str | None = None

    def get_current_trace_id(self) -> str | None:
        return self.current_trace_id

    def set_current_trace_id(self, trace_id: str | None) -> None:
        self.current_trace_id = trace_id

    def get_pipeline_event_bus(self):
        return self.state.pipeline_event_bus


def test_publish_snapshot_allocates_fallback_trace_for_listener_and_pipeline() -> None:
    manager = _ManagerStub()
    bus = PipelineEventBus()
    received = []
    observed_trace_ids = []
    bus.add_listener(received.append)
    manager.state.pipeline_event_bus = bus

    def _listener(symbol, timeframe, bar_time, indicators, scope):
        observed_trace_ids.append(manager.get_current_trace_id())

    manager.state.snapshot_listeners.append(_listener)

    publish_snapshot(
        manager,
        "XAUUSD",
        "M5",
        datetime(2026, 4, 13, 13, 15, tzinfo=timezone.utc),
        {"atr14": {"atr": 3.2}},
        scope="confirmed",
    )

    assert len(received) == 1
    assert received[0].type == "snapshot_published"
    assert received[0].trace_id
    assert observed_trace_ids == [received[0].trace_id]
    assert manager.get_current_trace_id() is None
