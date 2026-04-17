"""Tests for NotificationModule — pipeline/health wiring + lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List

import pytest

from src.config.models.notifications import NotificationConfig
from src.monitoring.pipeline.event_bus import PipelineEvent, PipelineEventBus
from src.monitoring.pipeline.events import PIPELINE_EXECUTION_FAILED
from src.notifications.classifier import PipelineEventClassifier
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.module import NotificationModule
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


class FakeHealthMonitor:
    """Minimal stand-in for HealthMonitor exposing set_alert_listener."""

    def __init__(self) -> None:
        self._listener: Callable | None = None
        self.listener_calls: int = 0

    def set_alert_listener(self, listener: Callable | None) -> None:
        self._listener = listener

    def fire_alert(self, **payload: Any) -> None:
        """Simulate HealthMonitor invoking the registered listener."""
        if self._listener is not None:
            self._listener(payload)
            self.listener_calls += 1


def _make_config(**kwargs: Any) -> NotificationConfig:
    defaults = dict(
        runtime={"enabled": True},
        events={
            "execution_failed": "critical",
            "health_degraded": "warning",
            "circuit_open": "critical",
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
        chats={"default_chat_id": "123"},
        bot_token="fake-token",
    )
    defaults.update(kwargs)
    return NotificationConfig(**defaults)


def _build_module(
    tmp_path: Path,
    *,
    transport: FakeTransport,
    config: NotificationConfig | None = None,
    pipeline_bus: PipelineEventBus | None = None,
    health_monitor: Any = None,
) -> NotificationModule:
    cfg = config or _make_config()
    templates = TemplateRegistry.from_directory(TEMPLATES, strict=True)
    outbox = OutboxStore(tmp_path / "outbox.db")
    classifier = PipelineEventClassifier(config=cfg, instance="live-main")
    current = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)]
    dispatcher = NotificationDispatcher(
        config=cfg,
        templates=templates,
        transport=transport,
        outbox=outbox,
        default_chat_id="123",
        clock=lambda: current[0],
    )
    return NotificationModule(
        config=cfg,
        instance="live-main",
        dispatcher=dispatcher,
        classifier=classifier,
        templates=templates,
        transport=transport,
        outbox=outbox,
        pipeline_event_bus=pipeline_bus,
        health_monitor=health_monitor,
    )


class TestLifecycle:
    def test_start_stop(self, tmp_path: Path):
        transport = FakeTransport()
        module = _build_module(tmp_path, transport=transport)
        module.start()
        assert module.status()["started"] is True
        module.stop()
        assert module.status()["started"] is False

    def test_disabled_skips_start(self, tmp_path: Path):
        transport = FakeTransport()
        cfg = _make_config(runtime={"enabled": False})
        module = _build_module(tmp_path, transport=transport, config=cfg)
        module.start()
        assert module.status()["started"] is False

    def test_double_start_idempotent(self, tmp_path: Path):
        transport = FakeTransport()
        module = _build_module(tmp_path, transport=transport)
        module.start()
        module.start()
        assert module.status()["started"] is True
        module.stop()


class TestPipelineWiring:
    def test_bus_event_triggers_send_after_drain(self, tmp_path: Path):
        transport = FakeTransport()
        bus = PipelineEventBus()
        module = _build_module(tmp_path, transport=transport, pipeline_bus=bus)
        module.start()
        try:
            bus.emit(
                PipelineEvent(
                    type=PIPELINE_EXECUTION_FAILED,
                    trace_id="t1",
                    symbol="XAUUSD",
                    timeframe="H1",
                    scope="confirmed",
                    ts=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                    payload={
                        "strategy": "trend_h1",
                        "direction": "long",
                        "reason": "broker_reject",
                    },
                )
            )
            # Submit path is synchronous — outbox row exists immediately.
            # Then drain through the worker (which we don't want to race with
            # in tests; use dispatcher.drain_once directly).
            module._dispatcher.drain_once()
            assert len(transport.sent) == 1
            chat_id, text = transport.sent[0]
            assert chat_id == "123"
            # Escaped form — "trend_h1" renders as "trend\_h1" under Markdown safety.
            assert "XAUUSD" in text and "trend\\_h1" in text
        finally:
            module.stop()

    def test_listener_unregisters_on_stop(self, tmp_path: Path):
        transport = FakeTransport()
        bus = PipelineEventBus()
        module = _build_module(tmp_path, transport=transport, pipeline_bus=bus)
        module.start()
        module.stop()
        # After stop, emitting should not crash and should not route to dispatch.
        bus.emit(
            PipelineEvent(
                type=PIPELINE_EXECUTION_FAILED,
                trace_id="t2",
                symbol="XAUUSD",
                timeframe="H1",
                scope="confirmed",
                ts=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                payload={"strategy": "x", "direction": "long", "reason": "r"},
            )
        )
        # outbox has not been torn down; but nothing should have been enqueued
        # by a live listener (module.stop() unsubscribed).
        module._dispatcher.drain_once()
        assert transport.sent == []


class TestHealthWiring:
    def test_health_alert_to_notification(self, tmp_path: Path):
        transport = FakeTransport()
        hm = FakeHealthMonitor()
        module = _build_module(tmp_path, transport=transport, health_monitor=hm)
        module.start()
        try:
            hm.fire_alert(
                timestamp="2026-01-01T12:00:00Z",
                component="data",
                metric_name="data_latency",
                alert_level="critical",
                value=45.0,
                threshold=30.0,
                message="too stale",
            )
            module._dispatcher.drain_once()
            assert len(transport.sent) == 1
            # "data.data_latency" renders as "data.data\_latency" post-escape
            assert "data.data\\_latency" in transport.sent[0][1]
        finally:
            module.stop()

    def test_health_info_level_ignored(self, tmp_path: Path):
        transport = FakeTransport()
        hm = FakeHealthMonitor()
        module = _build_module(tmp_path, transport=transport, health_monitor=hm)
        module.start()
        try:
            hm.fire_alert(
                timestamp="ts",
                component="c",
                metric_name="m",
                alert_level="ok",  # below warning threshold → ignored
                value=1.0,
                threshold=2.0,
                message="fine",
            )
            module._dispatcher.drain_once()
            assert transport.sent == []
        finally:
            module.stop()


class TestToggle:
    def test_toggle_off_stops_worker(self, tmp_path: Path):
        transport = FakeTransport()
        module = _build_module(tmp_path, transport=transport)
        module.start()
        assert module.status()["started"] is True
        module.set_enabled(False)
        assert module.status()["started"] is False
        assert module.status()["enabled"] is False

    def test_toggle_on_starts_worker(self, tmp_path: Path):
        transport = FakeTransport()
        cfg = _make_config(runtime={"enabled": False})
        module = _build_module(tmp_path, transport=transport, config=cfg)
        module.start()  # no-op since disabled
        assert module.status()["started"] is False
        module.set_enabled(True)
        assert module.status()["started"] is True
        module.stop()


class TestStatus:
    def test_status_shape(self, tmp_path: Path):
        transport = FakeTransport()
        bus = PipelineEventBus()
        hm = FakeHealthMonitor()
        module = _build_module(
            tmp_path, transport=transport, pipeline_bus=bus, health_monitor=hm
        )
        module.start()
        try:
            status = module.status()
            assert status["enabled"] is True
            assert status["started"] is True
            assert status["pipeline_listener_registered"] is True
            assert status["health_listener_registered"] is True
            assert status["scheduler_running"] is True
            # outbox_purge always registered; daily_report registered when
            # daily_report_utc is set (not in the default test config).
            assert "outbox_purge" in status["scheduler_jobs"]
            assert "dispatcher" in status
        finally:
            module.stop()


class FakeTradingStateAlerts:
    def __init__(self, alerts: list[dict] | None = None) -> None:
        self.alerts = alerts or []
        self.call_count = 0

    def summary(self, **_: Any) -> dict:
        self.call_count += 1
        return {"alerts": list(self.alerts)}


class TestSchedulerIntegration:
    def test_trading_state_job_registered_when_alerts_provided(self, tmp_path: Path):
        transport = FakeTransport()
        tsa = FakeTradingStateAlerts()
        cfg = _make_config(
            events={
                "execution_failed": "critical",
                "pending_missing": "critical",
                "unmanaged_position": "critical",
                "trading_state_warning": "warning",
            }
        )
        templates = TemplateRegistry.from_directory(TEMPLATES, strict=True)
        outbox = OutboxStore(tmp_path / "outbox.db")
        classifier = PipelineEventClassifier(config=cfg, instance="live-main")
        current = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)]
        dispatcher = NotificationDispatcher(
            config=cfg,
            templates=templates,
            transport=transport,
            outbox=outbox,
            default_chat_id="123",
            clock=lambda: current[0],
        )
        module = NotificationModule(
            config=cfg,
            instance="live-main",
            dispatcher=dispatcher,
            classifier=classifier,
            templates=templates,
            transport=transport,
            outbox=outbox,
            trading_state_alerts=tsa,
        )
        module.start()
        try:
            jobs = module.status()["scheduler_jobs"]
            assert "trading_state_poll" in jobs
            assert "outbox_purge" in jobs
        finally:
            module.stop()

    def test_daily_report_job_registered_when_schedule_set(self, tmp_path: Path):
        transport = FakeTransport()
        cfg = _make_config(schedules={"daily_report_utc": "21:00"})
        module = _build_module(tmp_path, transport=transport, config=cfg)
        module.start()
        try:
            jobs = module.status()["scheduler_jobs"]
            assert "daily_report" in jobs
        finally:
            module.stop()

    def test_no_trading_state_job_when_alerts_absent(self, tmp_path: Path):
        transport = FakeTransport()
        module = _build_module(tmp_path, transport=transport)
        module.start()
        try:
            jobs = module.status()["scheduler_jobs"]
            assert "trading_state_poll" not in jobs
            assert "outbox_purge" in jobs
        finally:
            module.stop()
