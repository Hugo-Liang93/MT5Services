"""Tests for src/studio/event_buffer.py — thread-safe ring buffer."""
from __future__ import annotations

import threading

from src.studio.event_buffer import EventBuffer


def test_append_and_recent() -> None:
    buf = EventBuffer(max_size=5)
    for i in range(3):
        buf.append({"i": i})
    assert len(buf) == 3
    assert buf.recent() == [{"i": 0}, {"i": 1}, {"i": 2}]


def test_ring_evicts_oldest() -> None:
    buf = EventBuffer(max_size=3)
    for i in range(5):
        buf.append({"i": i})
    assert len(buf) == 3
    assert buf.recent() == [{"i": 2}, {"i": 3}, {"i": 4}]


def test_recent_limit() -> None:
    buf = EventBuffer(max_size=10)
    for i in range(10):
        buf.append({"i": i})
    result = buf.recent(limit=3)
    assert result == [{"i": 7}, {"i": 8}, {"i": 9}]


def test_recent_limit_exceeds_size() -> None:
    buf = EventBuffer(max_size=5)
    buf.append({"i": 0})
    assert buf.recent(limit=100) == [{"i": 0}]


def test_empty_buffer() -> None:
    buf = EventBuffer()
    assert len(buf) == 0
    assert buf.recent() == []


def test_thread_safety() -> None:
    """Concurrent appends should not corrupt the buffer."""
    buf = EventBuffer(max_size=200)
    errors: list[str] = []

    def writer(start: int) -> None:
        try:
            for i in range(100):
                buf.append({"thread": start, "i": i})
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(buf) == 200  # 4 threads × 100 = 400, capped at 200
