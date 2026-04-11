from __future__ import annotations

from pathlib import Path

from src.config import get_ingest_config, get_runtime_ingest_settings
from src.monitoring.health import HealthMonitor


def test_ingest_thresholds_exposed_in_runtime_config() -> None:
    ingest = get_ingest_config()
    runtime_settings = get_runtime_ingest_settings()

    assert ingest.intrabar_drop_rate_1m_warning == 1.0
    assert ingest.intrabar_drop_rate_1m_critical == 5.0
    assert ingest.intrabar_queue_age_p95_ms_warning == 2500.0
    assert ingest.intrabar_queue_age_p95_ms_critical == 5000.0
    assert ingest.intrabar_to_decision_latency_p95_ms_warning == 3500.0
    assert ingest.intrabar_to_decision_latency_p95_ms_critical == 7000.0

    assert runtime_settings.intrabar_drop_rate_1m_warning == ingest.intrabar_drop_rate_1m_warning
    assert runtime_settings.intrabar_drop_rate_1m_critical == ingest.intrabar_drop_rate_1m_critical
    assert runtime_settings.intrabar_queue_age_p95_ms_warning == ingest.intrabar_queue_age_p95_ms_warning
    assert runtime_settings.intrabar_queue_age_p95_ms_critical == ingest.intrabar_queue_age_p95_ms_critical
    assert runtime_settings.intrabar_to_decision_latency_p95_ms_warning == ingest.intrabar_to_decision_latency_p95_ms_warning
    assert runtime_settings.intrabar_to_decision_latency_p95_ms_critical == ingest.intrabar_to_decision_latency_p95_ms_critical


def test_health_monitor_configure_intrabar_thresholds(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.configure_alerts(
        intrabar_drop_rate_1m_warning=1.0,
        intrabar_drop_rate_1m_critical=2.0,
        intrabar_queue_age_p95_ms_warning=10.0,
        intrabar_queue_age_p95_ms_critical=20.0,
        intrabar_to_decision_latency_p95_ms_warning=30.0,
        intrabar_to_decision_latency_p95_ms_critical=40.0,
    )

    assert monitor.alerts["intrabar_drop_rate_1m"]["warning"] == 1.0
    assert monitor.alerts["intrabar_drop_rate_1m"]["critical"] == 2.0
    assert monitor.alerts["intrabar_queue_age_p95_ms"]["warning"] == 10.0
    assert monitor.alerts["intrabar_queue_age_p95_ms"]["critical"] == 20.0
    assert monitor.alerts["intrabar_to_decision_latency_p95_ms"]["warning"] == 30.0
    assert monitor.alerts["intrabar_to_decision_latency_p95_ms"]["critical"] == 40.0


def test_intrabar_metrics_respect_threshold_order(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.configure_alerts(
        intrabar_drop_rate_1m_warning=1.0,
        intrabar_drop_rate_1m_critical=2.0,
        intrabar_queue_age_p95_ms_warning=250.0,
        intrabar_queue_age_p95_ms_critical=300.0,
        intrabar_to_decision_latency_p95_ms_warning=35.0,
        intrabar_to_decision_latency_p95_ms_critical=40.0,
    )
    monitor.record_metric("indicator_calculation", "intrabar_drop_rate_1m", 1.2)
    monitor.record_metric("indicator_calculation", "intrabar_queue_age_p95_ms", 350.0)
    monitor.record_metric("indicator_calculation", "intrabar_to_decision_latency_p95_ms", 50.0)

    report = monitor.generate_report(hours=1)
    assert report["components"]["indicator_calculation"]["intrabar_drop_rate_1m"]["status"] == "warning"
    assert report["components"]["indicator_calculation"]["intrabar_queue_age_p95_ms"]["status"] == "critical"
    assert report["components"]["indicator_calculation"]["intrabar_to_decision_latency_p95_ms"]["status"] == "critical"
