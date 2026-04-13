from __future__ import annotations

from datetime import datetime, timezone

from src.persistence.repositories.execution_intent_repo import ExecutionIntentRepository


class _DummyWriter:
    def __init__(self) -> None:
        self.batch_sql = None
        self.batch_rows = None

    def _json(self, payload):
        return {"wrapped": payload}

    def _batch(self, sql, rows, page_size: int = 200) -> None:
        self.batch_sql = sql
        self.batch_rows = list(rows)


def test_write_execution_intents_preserves_timeframe_and_payload_columns() -> None:
    writer = _DummyWriter()
    repo = ExecutionIntentRepository(writer)
    row = (
        datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc),
        "intent-1",
        "sig-1:live:broker-live:1002",
        "sig-1",
        "live:broker-live:1002",
        "live_exec_a",
        "trend_alpha",
        "XAUUSD",
        "M5",
        {
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "trend_alpha",
            "direction": "buy",
        },
        "pending",
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        {"published_by_instance_id": "main-live-main"},
    )

    repo.write_execution_intents([row])

    assert writer.batch_rows is not None
    assert len(writer.batch_rows) == 1
    persisted = writer.batch_rows[0]
    assert persisted[8] == "M5"
    assert persisted[9] == {"wrapped": row[9]}
    assert persisted[10] == "pending"
    assert persisted[20] == {"wrapped": {"published_by_instance_id": "main-live-main"}}
