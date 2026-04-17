"""Observability additions: status() carries last_successful_send_at + dlq_count."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest

from src.config.models.notifications import NotificationConfig
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.events import NotificationEvent, Severity
from src.notifications.persistence.outbox import OutboxStore
from src.notifications.templates.loader import TemplateRegistry
from src.notifications.transport.base import NotificationTransport, TransportResult

TEMPLATES = (
    Path(__file__).resolve().parents[2] / "config" / "notifications" / "templates"
)


@dataclass
class FakeTransport(NotificationTransport):
    results: List[TransportResult] = field(default_factory=list)

    def send(self, *, chat_id: str, text: str) -> TransportResult:
        if self.results:
            return self.results.pop(0)
        return TransportResult(ok=True)


def _make(tmp_path: Path, transport: NotificationTransport):
    cfg = NotificationConfig(
        runtime={
            "enabled": True,
            "max_retry_attempts": 1,
        },  # DLQ after 1 retryable fail
        events={"execution_failed": "critical"},
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
        chats={"default_chat_id": "99"},
        bot_token="fake",
    )
    templates = TemplateRegistry.from_directory(TEMPLATES, strict=True)
    outbox = OutboxStore(tmp_path / "outbox.db")
    clock = [datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)]
    return (
        NotificationDispatcher(
            config=cfg,
            templates=templates,
            transport=transport,
            outbox=outbox,
            default_chat_id="99",
            clock=lambda: clock[0],
        ),
        clock,
    )


def _event(dedup_parts=("X",)) -> NotificationEvent:
    return NotificationEvent.build(
        event_type="execution_failed",
        severity=Severity.CRITICAL,
        template_key="critical_execution_failed",
        source="pipeline_bus",
        instance="live-main",
        payload={
            "strategy": "trend_h1",
            "symbol": "XAUUSD",
            "direction": "long",
            "reason": "r",
            "instance": "live-main",
            "trace_id": "t",
        },
        dedup_parts=dedup_parts,
    )


class TestLastSuccessfulSend:
    def test_none_before_any_send(self, tmp_path: Path):
        disp, _ = _make(tmp_path, FakeTransport())
        assert disp.status()["last_successful_send_at"] is None

    def test_populated_after_ok_send(self, tmp_path: Path):
        disp, clock = _make(tmp_path, FakeTransport())
        disp.submit(_event())
        disp.drain_once()
        ts = disp.status()["last_successful_send_at"]
        assert ts is not None
        # ISO 8601 UTC
        assert ts.startswith("2026-04-18T12:00:00")

    def test_not_advanced_on_failed_send(self, tmp_path: Path):
        transport = FakeTransport(
            results=[TransportResult(ok=False, retryable=False, error="bad")]
        )
        disp, _ = _make(tmp_path, transport)
        disp.submit(_event())
        disp.drain_once()
        assert disp.status()["last_successful_send_at"] is None
        # But DLQ count reflects the failure.
        assert disp.status()["dlq_count"] == 1


class TestDlqCount:
    def test_zero_initially(self, tmp_path: Path):
        disp, _ = _make(tmp_path, FakeTransport())
        assert disp.status()["dlq_count"] == 0
        assert disp.status()["outbox_pending"] == 0

    def test_pending_reflected(self, tmp_path: Path):
        disp, _ = _make(tmp_path, FakeTransport())
        disp.submit(_event())
        # Before draining — 1 pending
        assert disp.status()["outbox_pending"] == 1
        assert disp.status()["dlq_count"] == 0

    def test_dlq_reflected_after_terminal(self, tmp_path: Path):
        transport = FakeTransport(
            results=[TransportResult(ok=False, retryable=False, error="401")]
        )
        disp, _ = _make(tmp_path, transport)
        disp.submit(_event())
        disp.drain_once()
        st = disp.status()
        assert st["dlq_count"] == 1
        assert st["outbox_pending"] == 0
