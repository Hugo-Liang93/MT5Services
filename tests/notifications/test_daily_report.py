"""Tests for DailyReportGenerator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.notifications.daily_report import DailyReportGenerator
from src.notifications.events import Severity
from src.notifications.templates.loader import TemplateRegistry

TEMPLATES = (
    Path(__file__).resolve().parents[2] / "config" / "notifications" / "templates"
)


class FakeReadModel:
    """Minimal RuntimeReadModel stand-in — each method returns a shaped dict."""

    def __init__(
        self,
        *,
        health_status: str = "ok",
        active_alerts: list | None = None,
        executor_enabled: bool = True,
        circuit_open: bool = False,
        signals_running: bool = True,
        open_positions: int = 0,
        pending_entries: int = 0,
        mode: str = "full",
    ) -> None:
        self._health_status = health_status
        self._active_alerts = active_alerts or []
        self._executor_enabled = executor_enabled
        self._circuit_open = circuit_open
        self._signals_running = signals_running
        self._open_positions = open_positions
        self._pending_entries = pending_entries
        self._mode = mode

    def health_report(self) -> dict[str, Any]:
        return {"status": self._health_status, "active_alerts": self._active_alerts}

    def trade_executor_summary(self) -> dict[str, Any]:
        return {
            "enabled": self._executor_enabled,
            "circuit_open": self._circuit_open,
        }

    def tracked_positions_payload(self) -> dict[str, Any]:
        return {
            "count": self._open_positions,
            "manager": {"pending_entries_count": self._pending_entries},
        }

    def signal_runtime_summary(self) -> dict[str, Any]:
        return {"running": self._signals_running}

    def runtime_mode_summary(self) -> dict[str, Any]:
        return {"current_mode": self._mode}


FIXED_TS = datetime(2026, 4, 18, 21, 0, 0, tzinfo=timezone.utc)


class TestBuildEvent:
    def test_basic_build(self):
        rrm = FakeReadModel(
            health_status="ok",
            active_alerts=[],
            executor_enabled=True,
            circuit_open=False,
            open_positions=3,
            pending_entries=1,
            mode="full",
        )
        gen = DailyReportGenerator(
            runtime_read_model=rrm, instance="live-main", clock=lambda: FIXED_TS
        )
        event = gen.build_event()
        assert event.event_type == "daily_report"
        assert event.severity is Severity.INFO
        assert event.template_key == "info_daily_report"
        assert event.source == "scheduler"
        assert event.instance == "live-main"
        assert event.payload["report_date_utc"] == "2026-04-18"
        assert event.payload["health_status"] == "ok"
        assert event.payload["executor_enabled"] == "是"
        assert event.payload["circuit_open"] == "否"
        assert event.payload["open_positions"] == "3"
        assert event.payload["pending_entries"] == "1"
        assert event.payload["read_model_ok"] is True

    def test_dedup_per_date(self):
        rrm = FakeReadModel()
        gen = DailyReportGenerator(
            runtime_read_model=rrm, instance="live-main", clock=lambda: FIXED_TS
        )
        event = gen.build_event()
        # Same day fires would collapse via dedup.
        assert event.dedup_key == "daily_report:2026-04-18"

    def test_active_alerts_count_from_list(self):
        rrm = FakeReadModel(health_status="warning", active_alerts=[{"x": 1}, {"x": 2}])
        gen = DailyReportGenerator(
            runtime_read_model=rrm, instance="live-main", clock=lambda: FIXED_TS
        )
        event = gen.build_event()
        assert event.payload["active_alerts_count"] == "2"
        assert event.payload["health_status"] == "warning"

    def test_none_readmodel_still_builds_event(self):
        gen = DailyReportGenerator(
            runtime_read_model=None, instance="live-main", clock=lambda: FIXED_TS
        )
        event = gen.build_event()
        # Graceful degradation — payload filled with defaults + read_model_ok=False
        assert event.payload["read_model_ok"] is False
        assert event.payload["health_status"] == "-"
        assert event.payload["executor_enabled"] == "-"

    def test_readmodel_method_raising_is_handled(self):
        class RaisingReadModel:
            def health_report(self):
                raise RuntimeError("boom")

            def trade_executor_summary(self):
                return {"enabled": True}

            def tracked_positions_payload(self):
                return {"count": 0}

            def signal_runtime_summary(self):
                return {}

            def runtime_mode_summary(self):
                return {}

        gen = DailyReportGenerator(
            runtime_read_model=RaisingReadModel(),
            instance="live-main",
            clock=lambda: FIXED_TS,
        )
        # Must not raise — scheduler calls this inside its own try/except, but
        # we want the generator to be internally robust too.
        event = gen.build_event()
        # health_report raised → defaults in
        assert event.payload["health_status"] == "-"
        # executor_summary worked → reflected
        assert event.payload["executor_enabled"] == "是"


class TestTemplateRendering:
    """Round-trip: generator output → real template → readable message."""

    def test_full_render(self):
        rrm = FakeReadModel(
            active_alerts=[{"name": "queue"}],
            open_positions=5,
            circuit_open=True,
        )
        gen = DailyReportGenerator(
            runtime_read_model=rrm, instance="live-main", clock=lambda: FIXED_TS
        )
        event = gen.build_event()
        registry = TemplateRegistry.from_directory(TEMPLATES, strict=True)
        message = registry.render_event(event)
        assert "[DAILY]" in message
        assert "2026-04-18" in message
        assert "21:00 UTC" in message
        assert "live-main" in message
        assert "熔断=是" in message  # circuit_open=True
        # Markdown escape active for dynamic values; "ok" has no meta-chars.
        assert "ok" in message
