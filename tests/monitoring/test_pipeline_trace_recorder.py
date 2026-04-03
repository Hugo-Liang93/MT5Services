from __future__ import annotations

from datetime import datetime, timezone

from src.monitoring.pipeline_event_bus import PipelineEvent, PipelineEventBus
from src.monitoring.pipeline_trace_recorder import PipelineTraceRecorder


class _DBWriter:
    def __init__(self) -> None:
        self.rows = []

    def write_pipeline_trace_events(self, rows, page_size: int = 200) -> None:
        self.rows.extend(list(rows))


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
