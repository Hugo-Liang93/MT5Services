from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from src.monitoring.pipeline import PipelineEvent, PipelineEventBus
from src.monitoring.pipeline import PipelineTraceRecorder


class _DBWriter:
    def __init__(self) -> None:
        self.rows = []

    def write_pipeline_trace_events(self, rows, page_size: int = 200) -> None:
        self.rows.extend(list(rows))


class _FakeThread:
    def __init__(self, *, alive_after_join: bool = False) -> None:
        self._alive = True
        self._alive_after_join = alive_after_join

    def join(self, timeout: float | None = None) -> None:
        self._alive = self._alive_after_join

    def is_alive(self) -> bool:
        return self._alive


def test_pipeline_trace_recorder_persists_bus_events() -> None:
    bus = PipelineEventBus()
    db_writer = _DBWriter()
    recorder = PipelineTraceRecorder(
        pipeline_bus=bus,
        db_writer=db_writer,
        batch_size=1,
        flush_interval_seconds=0.0,
    )

    recorder.start()
    try:
        bus.emit(
            PipelineEvent(
                type="bar_closed",
                trace_id="trace-1",
                symbol="XAUUSD",
                timeframe="M15",
                scope="confirmed",
                ts=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc).isoformat(),
                payload={"bar_time": "2026-01-01T08:00:00+00:00"},
            )
        )
        recorder.stop()
    finally:
        if recorder.is_running():
            recorder.stop()

    assert len(db_writer.rows) == 1
    row = db_writer.rows[0]
    assert row[0] == "trace-1"
    assert row[1] == "XAUUSD"
    assert row[4] == "bar_closed"
    assert row[6]["bar_time"] == "2026-01-01T08:00:00+00:00"


def test_pipeline_trace_recorder_stop_keeps_thread_reference_when_join_times_out() -> None:
    removed = []
    flushed = []

    class _Bus:
        def remove_listener(self, listener) -> None:
            removed.append(listener)

    recorder = PipelineTraceRecorder.__new__(PipelineTraceRecorder)
    recorder._pipeline_bus = _Bus()
    recorder._db_writer = _DBWriter()
    recorder._batch_size = 1
    recorder._flush_interval_seconds = 0.1
    recorder._queue = None
    recorder._pending = []
    recorder._stop = threading.Event()
    recorder._thread = _FakeThread(alive_after_join=True)
    recorder._listener_attached = True
    recorder._dropped_events = 0
    recorder._flush = lambda force=False: flushed.append(force)  # type: ignore[assignment]

    recorder.stop()

    assert recorder._thread is not None
    assert recorder.is_running() is True
    assert removed
    assert flushed == []


def test_pipeline_trace_recorder_start_fails_when_bus_rejects_listener() -> None:
    recorder = PipelineTraceRecorder(
        pipeline_bus=PipelineEventBus(max_listeners=0),
        db_writer=_DBWriter(),
    )

    with pytest.raises(RuntimeError):
        recorder.start()

    assert recorder.is_running() is False


# ── §0cc P2 回归：单次写库失败不能永久停录 ──


import time as _time


class _FlakyDBWriter:
    def __init__(self, fail_first_n: int = 1) -> None:
        self.rows: list[tuple] = []
        self._fail_remaining = fail_first_n
        self.calls = 0

    def write_pipeline_trace_events(self, rows, page_size: int = 200) -> None:
        self.calls += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise RuntimeError("disk full")
        self.rows.extend(list(rows))


def test_pipeline_trace_recorder_survives_transient_write_failure() -> None:
    """P2 §0cc 回归：旧 _run() 无顶层 try/except，_flush() 抛异常 → 后台线程
    崩溃后 pipeline bus 仍发事件但永远不再持久化（trace 失明）。
    必须把 _flush 异常隔离，让 recorder 在瞬时 DB/磁盘故障下持续运行。
    """
    bus = PipelineEventBus()
    db = _FlakyDBWriter(fail_first_n=2)
    recorder = PipelineTraceRecorder(
        pipeline_bus=bus,
        db_writer=db,
        batch_size=1,
        flush_interval_seconds=0.0,
    )
    recorder.start()
    try:
        # 发 5 条事件；前两次 _flush 应抛异常但线程必须存活
        for i in range(5):
            bus.emit(
                PipelineEvent(
                    type="bar_closed",
                    trace_id=f"trace-{i}",
                    symbol="XAUUSD",
                    timeframe="M15",
                    scope="confirmed",
                    ts=datetime(2026, 1, 1, 8, i, tzinfo=timezone.utc).isoformat(),
                    payload={"i": i},
                )
            )
            _time.sleep(0.05)
        # 等线程消化
        for _ in range(20):
            _time.sleep(0.05)
            if len(db.rows) >= 1:
                break
        assert recorder.is_running(), (
            f"瞬时写库失败不能打死 recorder 线程；db.calls={db.calls!r}"
        )
        # 至少恢复后写入 1 条
        assert len(db.rows) >= 1, (
            f"恢复后必须能写入；db.rows={len(db.rows)} db.calls={db.calls}"
        )
    finally:
        recorder.stop()
