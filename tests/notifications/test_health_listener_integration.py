"""Integration test: real HealthMonitor's set_alert_listener actually fires."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.monitoring.health.monitor import HealthMonitor


def _warm_up(monitor: HealthMonitor) -> None:
    # Startup grace (60s) suppresses the first alert on certain metrics.
    # Use a metric NOT in the grace set (e.g. data_latency / circuit_breaker_open).
    monitor._started_at_monotonic -= 120.0


class TestSetAlertListener:
    def test_listener_receives_warning(self, tmp_path: Path):
        monitor = HealthMonitor(db_path=str(tmp_path / "health.db"))
        _warm_up(monitor)
        received: list[dict] = []
        monitor.set_alert_listener(lambda alert: received.append(alert))
        # data_latency warning threshold default 10s.
        monitor.record_metric("data", "data_latency", 15.0)
        assert len(received) == 1
        alert = received[0]
        assert alert["alert_level"] == "warning"
        assert alert["component"] == "data"
        assert alert["metric_name"] == "data_latency"

    def test_listener_receives_critical(self, tmp_path: Path):
        monitor = HealthMonitor(db_path=str(tmp_path / "health.db"))
        _warm_up(monitor)
        received: list[dict] = []
        monitor.set_alert_listener(lambda alert: received.append(alert))
        monitor.record_metric("data", "data_latency", 60.0)  # > critical 30
        assert len(received) == 1
        assert received[0]["alert_level"] == "critical"

    def test_listener_not_called_when_ok(self, tmp_path: Path):
        monitor = HealthMonitor(db_path=str(tmp_path / "health.db"))
        _warm_up(monitor)
        received: list[dict] = []
        monitor.set_alert_listener(lambda alert: received.append(alert))
        monitor.record_metric("data", "data_latency", 2.0)  # below warning
        assert received == []

    def test_listener_exception_does_not_break_monitor(self, tmp_path: Path):
        monitor = HealthMonitor(db_path=str(tmp_path / "health.db"))
        _warm_up(monitor)

        def broken(alert):
            raise RuntimeError("boom")

        monitor.set_alert_listener(broken)
        # Must not raise — HealthMonitor swallows listener exceptions.
        monitor.record_metric("data", "data_latency", 60.0)
        # active_alerts still populated despite listener failure
        assert len(monitor.active_alerts) == 1

    def test_setting_none_clears_listener(self, tmp_path: Path):
        monitor = HealthMonitor(db_path=str(tmp_path / "health.db"))
        _warm_up(monitor)
        received: list[dict] = []
        monitor.set_alert_listener(lambda alert: received.append(alert))
        monitor.set_alert_listener(None)
        monitor.record_metric("data", "data_latency", 60.0)
        assert received == []
