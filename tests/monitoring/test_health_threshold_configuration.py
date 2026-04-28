from __future__ import annotations

from pathlib import Path

from src.app_runtime.builder_phases.monitoring import (
    _economic_calendar_staleness_thresholds,
)
from src.config import get_ingest_config, get_runtime_ingest_settings
from src.config.models.runtime import EconomicConfig
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

    assert (
        runtime_settings.intrabar_drop_rate_1m_warning
        == ingest.intrabar_drop_rate_1m_warning
    )
    assert (
        runtime_settings.intrabar_drop_rate_1m_critical
        == ingest.intrabar_drop_rate_1m_critical
    )
    assert (
        runtime_settings.intrabar_queue_age_p95_ms_warning
        == ingest.intrabar_queue_age_p95_ms_warning
    )
    assert (
        runtime_settings.intrabar_queue_age_p95_ms_critical
        == ingest.intrabar_queue_age_p95_ms_critical
    )
    assert (
        runtime_settings.intrabar_to_decision_latency_p95_ms_warning
        == ingest.intrabar_to_decision_latency_p95_ms_warning
    )
    assert (
        runtime_settings.intrabar_to_decision_latency_p95_ms_critical
        == ingest.intrabar_to_decision_latency_p95_ms_critical
    )


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
    monitor.record_metric(
        "indicator_calculation", "intrabar_to_decision_latency_p95_ms", 50.0
    )

    report = monitor.generate_report(hours=1)
    assert (
        report["components"]["indicator_calculation"]["intrabar_drop_rate_1m"]["status"]
        == "warning"
    )
    assert (
        report["components"]["indicator_calculation"]["intrabar_queue_age_p95_ms"][
            "status"
        ]
        == "critical"
    )
    assert (
        report["components"]["indicator_calculation"][
            "intrabar_to_decision_latency_p95_ms"
        ]["status"]
        == "critical"
    )


def test_economic_calendar_staleness_thresholds_follow_release_watch_idle_gap() -> None:
    """release_watch_idle 主导时 warning 不超过 stale_after（buffer 被夹紧）。"""
    settings = EconomicConfig(
        stale_after_seconds=1800.0,
        calendar_sync_interval_seconds=21600.0,
        near_term_refresh_interval_seconds=0.0,
        release_watch_interval_seconds=120.0,
        release_watch_idle_interval_seconds=1800.0,
    )

    thresholds = _economic_calendar_staleness_thresholds(settings)

    # buffered_gap = 1800 * 1.1 + 30 = 2010, 但 stale_after=1800 夹紧
    assert thresholds == {"warning": 1800.0, "critical": 1800.0}


def test_economic_calendar_staleness_thresholds_follow_fastest_enabled_refresh_path() -> (
    None
):
    """near_term refresh 较快时 warning = buffered_gap（不再裸 expected_gap）。

    历史 demo-main 频繁报 staleness 微超阈值（current=1804s vs threshold=1800s）即
    "warning = expected_gap" 没有调度+抖动 buffer 的根因。新公式 warning =
    min(stale_after, expected_gap * 1.1 + 30)，让正常 fetch gap 不再触发误警。
    """
    settings = EconomicConfig(
        stale_after_seconds=1800.0,
        calendar_sync_interval_seconds=21600.0,
        near_term_refresh_interval_seconds=900.0,
        release_watch_interval_seconds=120.0,
        release_watch_idle_interval_seconds=1800.0,
    )

    thresholds = _economic_calendar_staleness_thresholds(settings)

    # expected_gap = min(21600, 900, 1800) = 900
    # buffered_gap = 900 * 1.1 + 30 = 1020
    # warning = min(stale_after=1800, 1020) = 1020
    assert thresholds["critical"] == 1800.0
    assert abs(thresholds["warning"] - 1020.0) < 1e-6
