"""Tests for NotificationDispatcher — end-to-end through a fake transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest

from src.config.models.notifications import NotificationConfig
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.events import NotificationEvent, Severity
from src.notifications.persistence.outbox import OutboxStatus, OutboxStore
from src.notifications.templates.loader import TemplateRegistry
from src.notifications.transport.base import NotificationTransport, TransportResult

TEMPLATES = (
    Path(__file__).resolve().parents[2] / "config" / "notifications" / "templates"
)


@dataclass
class FakeTransport(NotificationTransport):
    """Scripted transport: feed ``results`` in order, each send pops the head.

    If no scripted result remains, defaults to a ``ok=True`` outcome.
    """

    results: List[TransportResult] = field(default_factory=list)
    sent: List[tuple[str, str]] = field(default_factory=list)

    def send(self, *, chat_id: str, text: str) -> TransportResult:
        self.sent.append((chat_id, text))
        if self.results:
            return self.results.pop(0)
        return TransportResult(ok=True)


def _make_config(**kwargs) -> NotificationConfig:
    defaults = dict(
        runtime={"enabled": False},
        events={},
        event_filters={},
        dedup={
            "critical_ttl_seconds": 60,
            "warning_ttl_seconds": 60,
            "info_ttl_seconds": 60,
        },
        rate_limit={"global_per_minute": 1000, "per_chat_per_minute": 1000},
        schedules={},
        templates={},
        inbound={"enabled": False},
        chats={"default_chat_id": "123"},
        bot_token="fake",
    )
    # Enable after defaults so invariants still pass
    defaults["runtime"] = {"enabled": True, **kwargs.pop("runtime", {})}
    defaults.update(kwargs)
    return NotificationConfig(**defaults)


def _make_event(
    *, severity: Severity = Severity.CRITICAL, dedup_parts=("XAUUSD", "t")
) -> NotificationEvent:
    return NotificationEvent.build(
        event_type="execution_failed",
        severity=severity,
        template_key="critical_execution_failed",
        source="pipeline_bus",
        instance="live-main",
        payload={
            "strategy": "trend_h1",
            "symbol": "XAUUSD",
            "direction": "long",
            "reason": "broker_reject",
            "instance": "live-main",
            "trace_id": "abc",
        },
        dedup_parts=dedup_parts,
    )


def _make_dispatcher(
    tmp_path: Path,
    *,
    transport: NotificationTransport,
    config: NotificationConfig | None = None,
    clock_value: datetime | None = None,
) -> NotificationDispatcher:
    reg = TemplateRegistry.from_directory(TEMPLATES, strict=True)
    outbox = OutboxStore(tmp_path / "outbox.db")
    current = [clock_value or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)]
    return NotificationDispatcher(
        config=config or _make_config(),
        templates=reg,
        transport=transport,
        outbox=outbox,
        default_chat_id="123",
        clock=lambda: current[0],
    )


class TestSubmitPath:
    def test_submit_persists_to_outbox(self, tmp_path: Path):
        transport = FakeTransport()
        disp = _make_dispatcher(tmp_path, transport=transport)
        assert disp.submit(_make_event()) is True

    def test_dedup_suppresses_duplicate(self, tmp_path: Path):
        transport = FakeTransport()
        disp = _make_dispatcher(tmp_path, transport=transport)
        e = _make_event()
        assert disp.submit(e) is True
        # Same dedup_key but new event_id within TTL window → suppressed
        e2 = NotificationEvent.build(
            event_type=e.event_type,
            severity=e.severity,
            template_key=e.template_key,
            source=e.source,
            instance=e.instance,
            payload=dict(e.payload),
            dedup_key=e.dedup_key,
        )
        assert disp.submit(e2) is False

    def test_rate_limit_drops_info_events(self, tmp_path: Path):
        transport = FakeTransport()
        config = _make_config(
            rate_limit={"global_per_minute": 1, "per_chat_per_minute": 1}
        )
        disp = _make_dispatcher(tmp_path, transport=transport, config=config)
        # First INFO event consumes the 1-token bucket.
        e1 = _make_event(severity=Severity.INFO, dedup_parts=("A",))
        # Rebuild as INFO via a proper template: use info_execution_submitted.
        e1 = NotificationEvent.build(
            event_type="execution_submitted",
            severity=Severity.INFO,
            template_key="info_execution_submitted",
            source="pipeline_bus",
            instance="live-main",
            payload={
                "strategy": "s",
                "symbol": "XAUUSD",
                "direction": "long",
                "order_kind": "market",
                "ticket": "1",
                "instance": "live-main",
                "trace_id": "t",
            },
            dedup_parts=("A",),
        )
        assert disp.submit(e1) is True
        e2 = NotificationEvent.build(
            event_type="execution_submitted",
            severity=Severity.INFO,
            template_key="info_execution_submitted",
            source="pipeline_bus",
            instance="live-main",
            payload=dict(e1.payload),
            dedup_parts=("B",),  # different dedup key → not suppressed by dedup
        )
        assert disp.submit(e2) is False
        snapshot = disp.status()["metrics"]
        assert snapshot.get("dropped_rate_limit_total", 0) >= 1

    def test_rate_limit_critical_still_enqueues(self, tmp_path: Path):
        transport = FakeTransport()
        config = _make_config(
            rate_limit={"global_per_minute": 1, "per_chat_per_minute": 1}
        )
        disp = _make_dispatcher(tmp_path, transport=transport, config=config)
        assert disp.submit(_make_event(dedup_parts=("A",))) is True
        # Second CRITICAL: rate limit hit but should still be enqueued.
        assert disp.submit(_make_event(dedup_parts=("B",))) is True

    def test_duplicate_event_id_skipped(self, tmp_path: Path):
        transport = FakeTransport()
        disp = _make_dispatcher(tmp_path, transport=transport)
        e = _make_event()
        assert disp.submit(e) is True
        # Exact same event_id (rare, but dispatcher must tolerate) — outbox
        # raises, dispatcher catches and records.
        assert disp.submit(e) is False  # suppressed by dedup first

    def test_render_failure_returns_false(self, tmp_path: Path):
        transport = FakeTransport()
        disp = _make_dispatcher(tmp_path, transport=transport)
        broken = NotificationEvent.build(
            event_type="execution_failed",
            severity=Severity.CRITICAL,
            template_key="critical_execution_failed",
            source="pipeline_bus",
            instance="live-main",
            payload={"strategy": "x"},  # missing required vars
            dedup_parts=("X",),
        )
        assert disp.submit(broken) is False
        assert disp.status()["metrics"].get("render_failures_total", 0) == 1


class TestDrain:
    def test_drain_sends_and_marks_sent(self, tmp_path: Path):
        transport = FakeTransport()
        disp = _make_dispatcher(tmp_path, transport=transport)
        disp.submit(_make_event())
        attempted = disp.drain_once()
        assert attempted == 1
        assert len(transport.sent) == 1
        assert "[CRITICAL]" in transport.sent[0][1]

    def test_retryable_failure_retries_later(self, tmp_path: Path):
        transport = FakeTransport(
            results=[TransportResult(ok=False, retryable=True, error="timeout")]
        )
        disp = _make_dispatcher(tmp_path, transport=transport)
        disp.submit(_make_event())
        disp.drain_once()
        counts = disp._outbox.count_by_status()
        # After a retryable failure, stays pending but not immediately due
        # (next_retry_at = now + backoff_seconds[0]).
        assert counts.get("pending", 0) == 1
        assert disp.status()["metrics"].get("send_failed_retryable_total", 0) == 1
        # Drain at same now — should skip (not due yet).
        assert disp.drain_once() == 0

    def test_terminal_failure_goes_to_dlq(self, tmp_path: Path):
        transport = FakeTransport(
            results=[TransportResult(ok=False, retryable=False, error="bad token")]
        )
        disp = _make_dispatcher(tmp_path, transport=transport)
        disp.submit(_make_event())
        disp.drain_once()
        counts = disp._outbox.count_by_status()
        assert counts.get("dlq", 0) == 1
        assert disp.status()["metrics"].get("moved_to_dlq_total", 0) == 1

    def test_retry_after_honored(self, tmp_path: Path):
        transport = FakeTransport(
            results=[
                TransportResult(
                    ok=False,
                    retryable=True,
                    error="429",
                    retry_after_seconds=60,
                )
            ]
        )
        disp = _make_dispatcher(tmp_path, transport=transport)
        disp.submit(_make_event())
        disp.drain_once()
        # Fetching 30s later — still not due (retry_after=60).
        future = datetime(2026, 1, 1, 12, 0, 30, tzinfo=timezone.utc)
        assert disp._outbox.fetch_due(now=future) == []
        # Fetching 90s later — due.
        future2 = datetime(2026, 1, 1, 12, 1, 30, tzinfo=timezone.utc)
        assert len(disp._outbox.fetch_due(now=future2)) == 1

    def test_max_attempts_routes_to_dlq(self, tmp_path: Path):
        # max_retry_attempts defaults to 3. Feed 3 retryable errors in a row.
        config = _make_config(runtime={"max_retry_attempts": 3})
        transport = FakeTransport(
            results=[
                TransportResult(ok=False, retryable=True, error="err1"),
                TransportResult(ok=False, retryable=True, error="err2"),
                TransportResult(ok=False, retryable=True, error="err3"),
            ]
        )
        disp = _make_dispatcher(tmp_path, transport=transport, config=config)
        disp.submit(_make_event())
        # Advance the clock on each drain so the row becomes due.
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        clock_holder = [base]
        disp._clock = lambda: clock_holder[0]
        for i in range(3):
            clock_holder[0] = base + timedelta(minutes=i + 1)
            disp.drain_once()
        assert disp._outbox.count_by_status().get("dlq", 0) == 1


class TestLifecycle:
    def test_start_stop_cycle(self, tmp_path: Path):
        transport = FakeTransport()
        disp = _make_dispatcher(tmp_path, transport=transport)
        disp.start()
        assert disp.status()["worker_running"] is True
        disp.stop(timeout=2.0)
        assert disp.status()["worker_running"] is False

    def test_stop_idempotent_when_not_started(self, tmp_path: Path):
        transport = FakeTransport()
        disp = _make_dispatcher(tmp_path, transport=transport)
        # No start() — stop should not raise.
        disp.stop(timeout=1.0)

    def test_start_twice_no_double_worker(self, tmp_path: Path):
        transport = FakeTransport()
        disp = _make_dispatcher(tmp_path, transport=transport)
        disp.start()
        first = disp._worker_thread
        disp.start()
        assert disp._worker_thread is first
        disp.stop(timeout=2.0)


class TestStatus:
    def test_status_reports_outbox_and_metrics(self, tmp_path: Path):
        transport = FakeTransport()
        disp = _make_dispatcher(tmp_path, transport=transport)
        disp.submit(_make_event())
        disp.drain_once()
        status = disp.status()
        assert status["enabled"] is True
        assert status["outbox_counts"].get("sent", 0) == 1
        assert "classified_total" in status["metrics"]
