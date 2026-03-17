from __future__ import annotations

from pathlib import Path

from src.monitoring.health_check import HealthMonitor


def test_health_report_skips_non_finite_metrics(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("market_data", "data_latency", float("inf"))
    report = monitor.generate_report(hours=1)

    assert report["summary"]["total_metrics"] == 0
    assert report["overall_status"] == "healthy"


def test_health_report_treats_cache_hit_rate_as_advisory(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("indicator_calculation", "cache_hit_rate", 0.2)
    report = monitor.generate_report(hours=1)

    metric = report["components"]["indicator_calculation"]["cache_hit_rate"]
    assert metric["status"] == "critical"
    assert metric["overall_impact"] == "advisory"
    assert report["overall_status"] == "warning"


def test_health_report_uses_blocking_metrics_for_critical_status(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("market_data", "data_latency", 999.0)
    report = monitor.generate_report(hours=1)

    metric = report["components"]["market_data"]["data_latency"]
    assert metric["status"] == "critical"
    assert metric["overall_impact"] == "blocking"
    assert report["overall_status"] == "critical"
