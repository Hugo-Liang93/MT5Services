"""Monitoring phase builders."""

from __future__ import annotations

from typing import Any

from src.app_runtime.container import AppContainer
from src.config import get_runtime_data_path
from src.monitoring import get_health_monitor, get_monitoring_manager


def _economic_calendar_staleness_thresholds(
    economic_settings: Any,
) -> dict[str, float]:
    """Derive staleness thresholds from the fastest enabled economic job.

    Economic calendar ``_last_refresh_at`` is advanced by any successful job
    (calendar_sync / near_term_sync / release_watch). The alert threshold should
    therefore follow the shortest enabled refresh path's *maximum* expected gap,
    otherwise idle release_watch periods will emit perpetual warning noise while
    the service itself still reports ``health_state=ok``.
    """

    stale_after = max(1.0, float(economic_settings.stale_after_seconds))
    enabled_refresh_gaps: list[float] = []

    calendar_sync_interval = float(
        getattr(economic_settings, "calendar_sync_interval_seconds", 0.0) or 0.0
    )
    if calendar_sync_interval > 0:
        enabled_refresh_gaps.append(calendar_sync_interval)

    near_term_interval = float(
        getattr(economic_settings, "near_term_refresh_interval_seconds", 0.0) or 0.0
    )
    if near_term_interval > 0:
        enabled_refresh_gaps.append(near_term_interval)

    release_watch_interval = float(
        getattr(economic_settings, "release_watch_idle_interval_seconds", 0.0) or 0.0
    )
    if release_watch_interval <= 0:
        release_watch_interval = float(
            getattr(economic_settings, "release_watch_interval_seconds", 0.0) or 0.0
        )
    if release_watch_interval > 0:
        enabled_refresh_gaps.append(release_watch_interval)

    expected_refresh_gap = (
        min(enabled_refresh_gaps) if enabled_refresh_gaps else stale_after
    )
    warning = max(1.0, min(stale_after, expected_refresh_gap))
    critical = stale_after
    return {"warning": warning, "critical": critical}


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
    container.health_monitor.alerts["economic_calendar_staleness"] = (
        _economic_calendar_staleness_thresholds(economic_settings)
    )
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
    if container.indicator_manager is not None:
        container.indicator_manager.cleanup_old_events(days_to_keep=7)
