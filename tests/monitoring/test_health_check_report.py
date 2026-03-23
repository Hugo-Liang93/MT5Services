from __future__ import annotations

from pathlib import Path

from src.monitoring.health_monitor import HealthMonitor
from src.monitoring.manager import MonitoringManager


def test_health_report_skips_non_finite_metrics(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("market_data", "data_latency", float("inf"))
    report = monitor.generate_report(hours=1)

    assert report["summary"]["total_metrics"] == 0
    assert report["overall_status"] == "healthy"


def test_cache_hit_rate_no_longer_triggers_alert(tmp_path: Path) -> None:
    # cache_hit_rate 阈值已设为 0（仅作信息指标），即使值为 0 也不触发告警。
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("indicator_calculation", "cache_hit_rate", 0.0)
    report = monitor.generate_report(hours=1)

    metric = report["components"]["indicator_calculation"]["cache_hit_rate"]
    assert metric["status"] == "healthy"


def test_indicator_compute_p99_triggers_warning(tmp_path: Path) -> None:
    # indicator_compute_p99_ms 是替代 cache_hit_rate 的新告警指标。
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("indicator_calculation", "indicator_compute_p99_ms", 800.0)
    report = monitor.generate_report(hours=1)

    metric = report["components"]["indicator_calculation"]["indicator_compute_p99_ms"]
    assert metric["status"] == "warning"


def test_health_report_uses_blocking_metrics_for_critical_status(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("market_data", "data_latency", 999.0)
    report = monitor.generate_report(hours=1)

    metric = report["components"]["market_data"]["data_latency"]
    assert metric["status"] == "critical"
    assert metric["overall_impact"] == "blocking"
    assert report["overall_status"] == "critical"


def test_monitoring_manager_lists_registered_components(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))
    manager = MonitoringManager(monitor, check_interval=5)
    manager.register_component("signals", object(), ["status"])
    manager.register_component("market_data", object(), ["data_latency"])

    rows = manager.list_registered_components()

    assert [item["name"] for item in rows] == ["market_data", "signals"]
    assert rows[0]["methods"] == ["data_latency"]
    assert rows[1]["methods"] == ["status"]
