from __future__ import annotations

from types import SimpleNamespace

from src.monitoring.health_check import MonitoringManager


def test_monitoring_manager_runs_retention_for_health_and_events() -> None:
    calls = []
    health_monitor = SimpleNamespace(cleanup_old_data=lambda days: calls.append(("health", days)))
    indicator_manager = SimpleNamespace(cleanup_old_events=lambda days: calls.append(("events", days)))

    manager = MonitoringManager(health_monitor, check_interval=5)
    manager.retention_interval_seconds = 0
    manager._monitored_components["indicator_calculation"] = {"obj": indicator_manager, "methods": []}

    manager._run_retention_if_due()

    assert ("health", 30) in calls
    assert ("events", 7) in calls
