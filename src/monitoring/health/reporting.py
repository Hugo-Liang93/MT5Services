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


def _escalate_status_for_active_alerts(
    base_status: str,
    active_alerts: List[Dict[str, Any]],
) -> str:
    """§0t/§0v P2：把 active_alerts 严重度拉进 overall_status 评级。

    旧实现只看本次窗口聚合的 metric counts；过去触发但仍未消除的 alert 会被
    静默忽略 → 只要本次窗口没新指标进 critical/warning，整体就被报 healthy。
    本 helper 既被 generate_report 调用，也被 get_system_status 调用，避免
    "缓存 snapshot + active_alerts 拼接" 出现 healthy + active critical 矛盾态。
    """
    has_critical = any(
        str((a.get("severity") or a.get("alert_level") or "")).lower() == "critical"
        for a in active_alerts
    )
    has_warning = any(
        str((a.get("severity") or a.get("alert_level") or "")).lower() == "warning"
        for a in active_alerts
    )
    current = str(base_status or "").lower()
    if has_critical and current != "critical":
        return "critical"
    if has_warning and current == "healthy":
        return "warning"
    return base_status


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

    # §0t P2：active_alerts 必须参与 overall_status 评级（详见 _escalate_status_for_active_alerts）
    report["overall_status"] = _escalate_status_for_active_alerts(
        report["overall_status"],
        report.get("active_alerts") or [],
    )

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
        active = list(monitor.active_alerts.values())
        result["active_alerts"] = active
        # §0v P2：缓存 snapshot 与 active_alerts 之间可能出现 stale —— critical
        # alert 在两次 generate_report 之间触发时，若直接返回缓存的 healthy snapshot
        # 就会出现 overall_status='healthy' + active_alerts 含 critical 的矛盾。
        # 用与 generate_report 同款 escalation helper 重新评级。
        result["overall_status"] = _escalate_status_for_active_alerts(
            str(result.get("overall_status") or "unknown"),
            active,
        )
        return result
    return {
        "timestamp": monitor._utc_now().isoformat(),
        "overall_status": "unknown",
        "components_status": {},
        "metrics_summary": {},
        "active_alerts": [],
    }
