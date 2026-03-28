from __future__ import annotations

import threading

from src.persistence.storage_writer import StorageWriter


class _FakeThread:
    def __init__(self) -> None:
        self.join_calls: list[float] = []
        self._alive = True

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout if timeout is not None else -1.0)
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


def test_stop_joins_writer_before_force_flush() -> None:
    writer = StorageWriter.__new__(StorageWriter)
    writer._stop = threading.Event()
    writer._thread = _FakeThread()
    writer._lock = threading.RLock()
    writer._channels = {"ticks": {"pending": [], "queue": type("Q", (), {"empty": lambda self: True})()}}

    calls: list[str] = []

    def fake_flush(name, ch, force=False):
        calls.append(f"flush:{name}:{force}")

    writer._flush_if_due = fake_flush  # type: ignore[assignment]

    writer.stop(timeout=1.5)

    assert writer._thread is None
    assert calls == ["flush:ticks:True"]
