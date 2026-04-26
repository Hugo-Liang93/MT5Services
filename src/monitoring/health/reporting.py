"""Health reporting functions — reads from in-memory MetricsStore.

v2: All reads come from the ring-buffer in ``MetricsStore``.
    SQLite is only used for ``alert_history`` (via ``MetricsStore.load_recent_alerts``).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Tuple

from .common import is_finite_metric_value, metric_overall_impact

logger = logging.getLogger(__name__)


def generate_report(monitor: Any, hours: int = 24) -> Dict[str, Any]:
    """Generate a health report from the in-memory ring buffer."""
    now = monitor._utc_now()
    cutoff = now - timedelta(hours=hours)

    report: Dict[str, Any] = {
        "timestamp": now.isoformat(),
        "time_range_hours": hours,
        "overall_status": "healthy",
        "components": {},
        "active_alerts": list(monitor.active_alerts.values()),
        "summary": {
            "total_metrics": 0,
            "warning_count": 0,
            "critical_count": 0,
            "advisory_warning_count": 0,
            "advisory_critical_count": 0,
        },
    }

    # 从环形缓冲读取窗口内的指标数据
    window_data = monitor._store.iter_window(hours=hours, now=now)

    # 按 (component, metric_name) 分组聚合
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for component, metric_name, metric_value, timestamp in window_data:
        if not is_finite_metric_value(metric_value):
            continue
        key = (component, metric_name)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {"values": [], "latest": float(metric_value), "last_updated": timestamp}
            grouped[key] = bucket
        bucket["values"].append(float(metric_value))
        bucket["latest"] = float(metric_value)
        bucket["last_updated"] = timestamp

    for (component, metric_name), metric_data in grouped.items():
        values = metric_data["values"]
        latest_value = metric_data["latest"]
        avg_value = sum(values) / len(values)
        report["components"].setdefault(component, {})

        alert_level = monitor._check_alert_level(component, metric_name, latest_value)
        status = "healthy"
        impact = metric_overall_impact(metric_name)
        if alert_level == "warning":
            status = "warning"
            if impact == "blocking":
                report["summary"]["warning_count"] += 1
            else:
                report["summary"]["advisory_warning_count"] += 1
        elif alert_level == "critical":
            status = "critical"
            if impact == "blocking":
                report["summary"]["critical_count"] += 1
            else:
                report["summary"]["advisory_critical_count"] += 1

        report["components"][component][metric_name] = {
            "latest": latest_value,
            "average": avg_value,
            "min": min(values),
            "max": max(values),
            "samples": len(values),
            "last_updated": metric_data["last_updated"],
            "status": status,
            "alert_level": alert_level,
            "overall_impact": impact,
        }
        report["summary"]["total_metrics"] += 1

    if report["summary"]["critical_count"] > 0:
        report["overall_status"] = "critical"
    elif report["summary"]["warning_count"] > 0:
        report["overall_status"] = "warning"
    elif (
        report["summary"]["advisory_critical_count"] > 0
        or report["summary"]["advisory_warning_count"] > 0
    ):
        report["overall_status"] = "warning"

    # §0t P2：active_alerts 必须参与 overall_status 评级。
    # 旧实现只看本次窗口聚合的 metric counts；过去触发但仍未消除的 alert
    # 会被静默忽略，导致只要本次窗口没新指标进 critical/warning，整体就被报 healthy。
    active_alerts_list = report.get("active_alerts") or []
    has_active_critical = any(
        str((alert.get("severity") or alert.get("alert_level") or "")).lower() == "critical"
        for alert in active_alerts_list
    )
    has_active_warning = any(
        str((alert.get("severity") or alert.get("alert_level") or "")).lower() == "warning"
        for alert in active_alerts_list
    )
    if has_active_critical and report["overall_status"] != "critical":
        report["overall_status"] = "critical"
    elif has_active_warning and report["overall_status"] == "healthy":
        report["overall_status"] = "warning"

    # 告警历史从 SQLite 读取（数据极少）
    report["recent_alerts"] = monitor._store.load_recent_alerts(cutoff.isoformat())

    # 系统状态快照写入内存
    monitor._store.set_system_status(
        {
            "timestamp": now.isoformat(),
            "overall_status": report["overall_status"],
            "components_status": report["components"],
            "metrics_summary": report["summary"],
        }
    )

    return report


def get_recent_metrics(
    monitor: Any,
    component: str,
    metric_name: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return recent metric samples — prefer in-memory fast cache, fall back to ring buffer."""
    key = f"{component}.{metric_name}"
    if key in monitor.metrics:
        return monitor.metrics[key][-limit:]
    # Fallback: read from ring buffer
    return monitor._store.get_recent(component, metric_name, limit=limit)


def get_system_status(monitor: Any) -> Dict[str, Any]:
    """Return the latest system status snapshot."""
    status = monitor._store.get_system_status()
    if status:
        result = dict(status)
        result["active_alerts"] = list(monitor.active_alerts.values())
        return result
    return {
        "timestamp": monitor._utc_now().isoformat(),
        "overall_status": "unknown",
        "components_status": {},
        "metrics_summary": {},
        "active_alerts": [],
    }
