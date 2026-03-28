from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from typing import Any, Dict, List, Tuple

from .health_common import is_finite_metric_value, metric_overall_impact

logger = logging.getLogger(__name__)


def generate_report(monitor, hours: int = 24) -> Dict[str, Any]:
    cutoff = monitor._utc_now() - timedelta(hours=hours)
    monitor._flush_buffer()

    with monitor._lock:
        conn = monitor._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT component, metric_name, metric_value, timestamp
            FROM health_metrics
            WHERE timestamp > ?
            ORDER BY component, metric_name, timestamp
            """,
            (cutoff.isoformat(),),
        )

        report = {
            "timestamp": monitor._utc_now().isoformat(),
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

        grouped_metrics: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for component, metric_name, metric_value, timestamp in cursor.fetchall():
            if not is_finite_metric_value(metric_value):
                continue
            key = (component, metric_name)
            bucket = grouped_metrics.setdefault(
                key,
                {"values": [], "latest": float(metric_value), "last_updated": timestamp},
            )
            bucket["values"].append(float(metric_value))
            bucket["latest"] = float(metric_value)
            bucket["last_updated"] = timestamp

        for (component, metric_name), metric_data in grouped_metrics.items():
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

        report["recent_alerts"] = _load_recent_alerts(cursor, cutoff.isoformat())
        _persist_system_status(cursor, monitor, report)
        conn.commit()

    return report


def get_recent_metrics(
    monitor,
    component: str,
    metric_name: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    key = f"{component}.{metric_name}"
    if key in monitor.metrics:
        return monitor.metrics[key][-limit:]

    with monitor._lock:
        conn = monitor._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT timestamp, metric_value, details, alert_level
            FROM health_metrics
            WHERE component = ? AND metric_name = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (component, metric_name, limit),
        )

        metrics = []
        for timestamp, metric_value, details_json, alert_level in cursor.fetchall():
            metrics.append(
                {
                    "timestamp": timestamp,
                    "value": metric_value,
                    "details": json.loads(details_json) if details_json else None,
                    "alert_level": alert_level,
                }
            )
    return metrics


def cleanup_old_data(monitor, days_to_keep: int = 30) -> None:
    cutoff = monitor._utc_now() - timedelta(days=days_to_keep)
    monitor._flush_buffer()

    with monitor._lock:
        conn = monitor._get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM health_metrics WHERE timestamp < ?", (cutoff.isoformat(),))
        metrics_deleted = cursor.rowcount
        cursor.execute(
            "DELETE FROM alert_history WHERE timestamp < ? AND resolved_at IS NOT NULL",
            (cutoff.isoformat(),),
        )
        alerts_deleted = cursor.rowcount
        cursor.execute("DELETE FROM system_status WHERE timestamp < ?", (cutoff.isoformat(),))
        status_deleted = cursor.rowcount
        conn.commit()

    logger.info(
        "Cleaned up health monitor data: %s metrics, %s alerts, %s status records",
        metrics_deleted,
        alerts_deleted,
        status_deleted,
    )


def get_system_status(monitor) -> Dict[str, Any]:
    with monitor._lock:
        conn = monitor._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT timestamp, overall_status, components_status, metrics_summary
            FROM system_status
            ORDER BY timestamp DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()

    if row:
        timestamp, overall_status, components_status_json, metrics_summary_json = row
        return {
            "timestamp": timestamp,
            "overall_status": overall_status,
            "components_status": json.loads(components_status_json),
            "metrics_summary": json.loads(metrics_summary_json),
            "active_alerts": list(monitor.active_alerts.values()),
        }
    return {
        "timestamp": monitor._utc_now().isoformat(),
        "overall_status": "unknown",
        "components_status": {},
        "metrics_summary": {},
        "active_alerts": [],
    }


def _load_recent_alerts(cursor, cutoff: str) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT timestamp, component, metric_name, alert_level, metric_value, threshold, message
        FROM alert_history
        WHERE timestamp > ?
        ORDER BY timestamp DESC
        LIMIT 20
        """,
        (cutoff,),
    )
    return [
        {
            "timestamp": timestamp,
            "component": component,
            "metric_name": metric_name,
            "alert_level": alert_level,
            "metric_value": metric_value,
            "threshold": threshold,
            "message": message,
        }
        for timestamp, component, metric_name, alert_level, metric_value, threshold, message
        in cursor.fetchall()
    ]


def _persist_system_status(cursor, monitor, report: Dict[str, Any]) -> None:
    cursor.execute(
        """
        INSERT OR REPLACE INTO system_status
        (timestamp, overall_status, components_status, metrics_summary)
        VALUES (?, ?, ?, ?)
        """,
        (
            monitor._utc_now().isoformat(),
            report["overall_status"],
            json.dumps(report["components"]),
            json.dumps(report["summary"]),
        ),
    )
