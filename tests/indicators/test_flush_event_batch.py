"""Tests for the extracted event batch persistence helpers."""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

from src.indicators.manager import UnifiedIndicatorManager
from src.indicators.runtime import event_io
from src.indicators.runtime import event_loops


def _make_manager_stub() -> UnifiedIndicatorManager:
    """Create a bare manager via __new__ with only flush-related attrs."""
    manager = object.__new__(UnifiedIndicatorManager)
    manager.state = SimpleNamespace(
        event_write_queue=queue.Queue(maxsize=100),
        stop_event=threading.Event(),
    )
    manager.event_store = SimpleNamespace(
        publish_events_batch=lambda batch: None,
    )
    return manager


def test_flush_event_batch_drains_up_to_64() -> None:
    manager = _make_manager_stub()
    published = []
    manager.event_store.publish_events_batch = lambda batch: published.extend(batch)

    # Put 70 items
    for i in range(70):
        manager.state.event_write_queue.put(("XAUUSD", "M5", i))

    # Flush with first item — should drain up to 64 total (1 first + 63 more)
    first = manager.state.event_write_queue.get()
    event_io.flush_event_batch(manager, first, "test")

    # 1 (first) + min(63, 69 remaining) = 64
    assert len(published) == 64
    # 70 - 64 = 6 remaining
    assert manager.state.event_write_queue.qsize() == 6


def test_flush_event_batch_handles_small_queue() -> None:
    manager = _make_manager_stub()
    published = []
    manager.event_store.publish_events_batch = lambda batch: published.extend(batch)

    manager.state.event_write_queue.put(("XAUUSD", "M5", 1))
    manager.state.event_write_queue.put(("XAUUSD", "M5", 2))

    first = manager.state.event_write_queue.get()
    event_io.flush_event_batch(manager, first, "test")

    assert len(published) == 2
    assert manager.state.event_write_queue.qsize() == 0


def test_flush_event_batch_survives_write_error(caplog) -> None:
    manager = _make_manager_stub()
    manager.event_store.publish_events_batch = lambda batch: (_ for _ in ()).throw(
        RuntimeError("db write failed")
    )

    manager.state.event_write_queue.put(("XAUUSD", "M5", 1))
    first = manager.state.event_write_queue.get()

    # Should not raise
    event_io.flush_event_batch(manager, first, "test")

    assert manager.state.event_write_queue.qsize() == 0


def test_event_writer_loop_drains_on_shutdown() -> None:
    manager = _make_manager_stub()
    import threading
    published = []
    manager.event_store.publish_events_batch = lambda batch: published.extend(batch)

    # Pre-fill queue
    for i in range(5):
        manager.state.event_write_queue.put(("XAUUSD", "M5", i))

    # Set stop immediately so the loop exits after draining.
    manager.state.stop_event.set()
    event_loops.run_event_writer_loop(manager)

    assert len(published) == 5
    assert manager.state.event_write_queue.qsize() == 0
