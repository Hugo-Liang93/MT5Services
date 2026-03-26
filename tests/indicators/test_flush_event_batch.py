"""Tests for UnifiedIndicatorManager._flush_event_batch."""
from __future__ import annotations

import queue
from types import SimpleNamespace

from src.indicators.manager import UnifiedIndicatorManager


def _make_manager_stub() -> UnifiedIndicatorManager:
    """Create a bare manager via __new__ with only flush-related attrs."""
    manager = object.__new__(UnifiedIndicatorManager)
    manager._event_write_queue = queue.Queue(maxsize=100)
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
        manager._event_write_queue.put(("XAUUSD", "M5", i))

    # Flush with first item — should drain up to 64 total (1 first + 63 more)
    first = manager._event_write_queue.get()
    manager._flush_event_batch(first, "test")

    # 1 (first) + min(63, 69 remaining) = 64
    assert len(published) == 64
    # 70 - 64 = 6 remaining
    assert manager._event_write_queue.qsize() == 6


def test_flush_event_batch_handles_small_queue() -> None:
    manager = _make_manager_stub()
    published = []
    manager.event_store.publish_events_batch = lambda batch: published.extend(batch)

    manager._event_write_queue.put(("XAUUSD", "M5", 1))
    manager._event_write_queue.put(("XAUUSD", "M5", 2))

    first = manager._event_write_queue.get()
    manager._flush_event_batch(first, "test")

    assert len(published) == 2
    assert manager._event_write_queue.qsize() == 0


def test_flush_event_batch_survives_write_error(caplog) -> None:
    manager = _make_manager_stub()
    manager.event_store.publish_events_batch = lambda batch: (_ for _ in ()).throw(
        RuntimeError("db write failed")
    )

    manager._event_write_queue.put(("XAUUSD", "M5", 1))
    first = manager._event_write_queue.get()

    # Should not raise
    manager._flush_event_batch(first, "test")

    assert manager._event_write_queue.qsize() == 0


def test_event_writer_loop_drains_on_shutdown() -> None:
    manager = _make_manager_stub()
    import threading
    manager._stop = threading.Event()
    published = []
    manager.event_store.publish_events_batch = lambda batch: published.extend(batch)

    # Pre-fill queue
    for i in range(5):
        manager._event_write_queue.put(("XAUUSD", "M5", i))

    # Set stop immediately so the loop exits after draining
    manager._stop.set()
    manager._event_writer_loop()

    assert len(published) == 5
    assert manager._event_write_queue.qsize() == 0
