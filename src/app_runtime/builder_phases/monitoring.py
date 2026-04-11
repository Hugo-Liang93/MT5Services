"""Monitoring phase builders."""

from __future__ import annotations

from typing import Any

from src.app_runtime.container import AppContainer
from src.config import get_runtime_data_path
from src.monitoring import get_health_monitor, get_monitoring_manager


def build_monitoring_layer(
    container: AppContainer,
    *,
    ingest_settings: Any,
    economic_settings: Any,
) -> None:
    """Build health monitoring and runtime alerts."""
    container.health_monitor = get_health_monitor(
        get_runtime_data_path("health_monitor.db")
    )
    container.health_monitor.configure_alerts(
        data_latency_warning=max(1.0, ingest_settings.max_allowed_delay / 2.0),
        data_latency_critical=max(1.0, ingest_settings.max_allowed_delay),
        intrabar_drop_rate_1m_warning=ingest_settings.intrabar_drop_rate_1m_warning,
        intrabar_drop_rate_1m_critical=ingest_settings.intrabar_drop_rate_1m_critical,
        intrabar_queue_age_p95_ms_warning=ingest_settings.intrabar_queue_age_p95_ms_warning,
        intrabar_queue_age_p95_ms_critical=ingest_settings.intrabar_queue_age_p95_ms_critical,
        intrabar_to_decision_latency_p95_ms_warning=(
            ingest_settings.intrabar_to_decision_latency_p95_ms_warning
        ),
        intrabar_to_decision_latency_p95_ms_critical=(
            ingest_settings.intrabar_to_decision_latency_p95_ms_critical
        ),
    )
    container.health_monitor.alerts["economic_calendar_staleness"] = {
        "warning": max(1.0, economic_settings.stale_after_seconds / 2.0),
        "critical": max(1.0, economic_settings.stale_after_seconds),
    }
    container.health_monitor.alerts["economic_provider_failures"] = {
        "warning": 1.0,
        "critical": float(max(2, economic_settings.request_retries)),
    }
    monitoring_interval = max(
        1,
        int(min(ingest_settings.health_check_interval, ingest_settings.queue_monitor_interval)),
    )
    container.monitoring_manager = get_monitoring_manager(
        container.health_monitor,
        check_interval=monitoring_interval,
    )
    container.health_monitor.cleanup_old_data(days_to_keep=30)
    container.indicator_manager.cleanup_old_events(days_to_keep=7)
