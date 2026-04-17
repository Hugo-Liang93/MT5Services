"""Tests for TradingStateAlerts polling adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

import pytest

from src.config.models.notifications import NotificationConfig
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.hooks import make_trading_state_poll_job
from src.notifications.persistence.outbox import OutboxStore
from src.notifications.templates.loader import TemplateRegistry
from src.notifications.transport.base import NotificationTransport, TransportResult

TEMPLATES = (
    Path(__file__).resolve().parents[2] / "config" / "notifications" / "templates"
)


@dataclass
class FakeTransport(NotificationTransport):
    sent: List[tuple[str, str]] = field(default_factory=list)

    def send(self, *, chat_id: str, text: str) -> TransportResult:
        self.sent.append((chat_id, text))
        return TransportResult(ok=True)


class FakeTradingStateAlerts:
    def __init__(self, alerts: list[dict]) -> None:
        self.alerts = alerts
        self.call_count = 0

    def summary(self, **_: Any) -> dict:
        self.call_count += 1
        return {"alerts": list(self.alerts)}


def _dispatch_fixture(tmp_path: Path) -> tuple[FakeTransport, NotificationDispatcher]:
    transport = FakeTransport()
    config = NotificationConfig(
        runtime={"enabled": True},
        events={
            "pending_missing": "critical",
            "unmanaged_position": "critical",
            "trading_state_warning": "warning",
        },
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
    current = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)]
    dispatcher = NotificationDispatcher(
        config=config,
        templates=templates,
        transport=transport,
        outbox=outbox,
        default_chat_id="99",
        clock=lambda: current[0],
    )
    return transport, dispatcher


class TestTradingStatePoll:
    def test_pending_missing_dispatched(self, tmp_path: Path):
        transport, dispatcher = _dispatch_fixture(tmp_path)
        alerts = [
            {
                "code": "pending_missing",
                "severity": "critical",
                "message": "x missing",
                "details": {"missing_count": 3},
            }
        ]
        poll = make_trading_state_poll_job(
            trading_state_alerts=FakeTradingStateAlerts(alerts),
            dispatcher=dispatcher,
            instance="live-main",
        )
        poll()
        dispatcher.drain_once()
        assert len(transport.sent) == 1
        assert (
            "pending_missing" in transport.sent[0][1]
            or "挂单丢失" in transport.sent[0][1]
        )

    def test_unmanaged_tickets_dispatched_with_tickets(self, tmp_path: Path):
        transport, dispatcher = _dispatch_fixture(tmp_path)
        alerts = [
            {
                "code": "unmanaged_live_positions",
                "severity": "warning",  # severity from summary, not from map
                "message": "2 unmanaged",
                "details": {"unmanaged_tickets": [42, 99]},
            }
        ]
        poll = make_trading_state_poll_job(
            trading_state_alerts=FakeTradingStateAlerts(alerts),
            dispatcher=dispatcher,
            instance="live-main",
        )
        poll()
        dispatcher.drain_once()
        assert len(transport.sent) == 1
        text = transport.sent[0][1]
        assert "42,99" in text

    def test_orphan_routed_to_generic_warn(self, tmp_path: Path):
        transport, dispatcher = _dispatch_fixture(tmp_path)
        alerts = [
            {
                "code": "pending_orphan",
                "severity": "warning",
                "message": "5 orphans",
                "details": {"orphan_count": 5},
            }
        ]
        poll = make_trading_state_poll_job(
            trading_state_alerts=FakeTradingStateAlerts(alerts),
            dispatcher=dispatcher,
            instance="live-main",
        )
        poll()
        dispatcher.drain_once()
        assert len(transport.sent) == 1
        assert "pending_orphan" in transport.sent[0][1]

    def test_unknown_code_ignored(self, tmp_path: Path):
        transport, dispatcher = _dispatch_fixture(tmp_path)
        alerts = [
            {
                "code": "unknown_new_alert",
                "severity": "warning",
                "message": "mystery",
                "details": {},
            }
        ]
        poll = make_trading_state_poll_job(
            trading_state_alerts=FakeTradingStateAlerts(alerts),
            dispatcher=dispatcher,
            instance="live-main",
        )
        poll()
        dispatcher.drain_once()
        assert transport.sent == []

    def test_dedup_collapses_repeated_poll(self, tmp_path: Path):
        transport, dispatcher = _dispatch_fixture(tmp_path)
        alerts = [
            {
                "code": "pending_missing",
                "severity": "critical",
                "message": "ongoing",
                "details": {"missing_count": 1},
            }
        ]
        poll = make_trading_state_poll_job(
            trading_state_alerts=FakeTradingStateAlerts(alerts),
            dispatcher=dispatcher,
            instance="live-main",
        )
        poll()
        poll()
        poll()
        dispatcher.drain_once()
        # TTL (60s) in mock clock — the three calls fire at same timestamp, so
        # only one survives dedup.
        assert len(transport.sent) == 1

    def test_summary_exception_does_not_raise(self, tmp_path: Path):
        _, dispatcher = _dispatch_fixture(tmp_path)

        class Broken:
            def summary(self, **_):
                raise RuntimeError("broken")

        poll = make_trading_state_poll_job(
            trading_state_alerts=Broken(),
            dispatcher=dispatcher,
            instance="live-main",
        )
        # Must not raise — scheduler depends on isolation.
        poll()

    def test_missing_summary_method_noops(self, tmp_path: Path):
        _, dispatcher = _dispatch_fixture(tmp_path)

        class HasNoSummary:
            pass

        poll = make_trading_state_poll_job(
            trading_state_alerts=HasNoSummary(),
            dispatcher=dispatcher,
            instance="live-main",
        )
        poll()  # no exception, no send
