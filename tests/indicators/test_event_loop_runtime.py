from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from src.indicators.runtime.event_loops import run_event_loop
from src.utils.event_store import ClaimedEvent


def test_run_event_loop_claims_events_with_claim_next_events(monkeypatch) -> None:
    stop_event = threading.Event()
    processed: list[tuple[list[ClaimedEvent], bool]] = []
    claimed_events = [
        ClaimedEvent(
            event_id=1,
            symbol="XAUUSD",
            timeframe="M1",
            bar_time=datetime(2026, 4, 11, 0, 0, tzinfo=timezone.utc),
        )
    ]

    class _EventStore:
        def __init__(self) -> None:
            self.claim_calls: list[int] = []

        def claim_next_events(self, limit: int = 1):
            self.claim_calls.append(limit)
            stop_event.set()
            return claimed_events

    event_store = _EventStore()
    manager = SimpleNamespace(
        config=SimpleNamespace(pipeline=SimpleNamespace(poll_interval=1.0)),
        state=SimpleNamespace(stop_event=stop_event, results={}, last_reconcile_at=None),
        event_store=event_store,
        cleanup_old_events=lambda days_to_keep: None,
    )

    monkeypatch.setattr(
        "src.indicators.runtime.event_loops.process_closed_bar_events_batch",
        lambda mgr, events, durable_event=False: processed.append((events, durable_event)),
    )
    monkeypatch.setattr(
        "src.indicators.runtime.event_loops.has_reconcile_ready_targets",
        lambda mgr: False,
    )

    run_event_loop(manager)

    assert event_store.claim_calls == [32]
    assert processed == [(claimed_events, True)]
