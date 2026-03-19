"""
轻量级健康监控系统
- 监控数据延迟、指标新鲜度、系统状态
- 基于SQLite存储监控数据
- 提供健康报告和告警
"""

import sqlite3
import json
import time
import threading
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple
import logging

from src.config import get_shared_symbols, get_shared_timeframes
from src.utils.common import timeframe_seconds

logger = logging.getLogger(__name__)


def _is_finite_metric_value(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _metric_overall_impact(metric_name: str) -> str:
    if metric_name in {
        "data_latency",
        "indicator_freshness",
        "queue_depth",
        "economic_calendar_staleness",
        "economic_provider_failures",
    }:
        return "blocking"
    return "advisory"


class HealthMonitor:
    """轻量级健康监控"""
    
    def __init__(self, db_path: str = "health_monitor.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()
        
        # 内存中保存最近数据（用于快速访问）
        self.metrics: Dict[str, List[Dict]] = {}
        
        # 告警配置
        self.alerts = {
            "data_latency": {
                "warning": 10.0,  # 10秒警告
                "critical": 30.0  # 30秒严重
            },
            "indicator_freshness": {
                "warning": 60.0,  # 1分钟警告
                "critical": 300.0  # 5分钟严重
            },
            "queue_depth": {
                "warning": 1000,
                "critical": 5000
            },
            "cache_hit_rate": {
                "warning": 0.7,  # 70%警告
                "critical": 0.5  # 50%严重
            }
        }
        
        # 活动告警
        self.active_alerts: Dict[str, Dict] = {}
        
        logger.info(f"HealthMonitor initialized with database: {db_path}")

    def configure_alerts(
        self,
        *,
        data_latency_warning: Optional[float] = None,
        data_latency_critical: Optional[float] = None,
    ) -> None:
        if data_latency_warning is not None:
            self.alerts["data_latency"]["warning"] = float(data_latency_warning)
        if data_latency_critical is not None:
            self.alerts["data_latency"]["critical"] = float(data_latency_critical)
    
    def _init_db(self):
        """初始化数据库表结构"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 创建监控指标表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS health_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    component TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    details TEXT,
                    alert_level TEXT
                )
            """)
            
            # 创建索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_component_time 
                ON health_metrics(component, timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_metric_time 
                ON health_metrics(metric_name, timestamp)
            """)
            
            # 创建告警历史表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    component TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    alert_level TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    threshold REAL NOT NULL,
                    message TEXT,
                    resolved_at TEXT,
                    resolved_by TEXT
                )
            """)
            
            # 创建系统状态表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_status (
                    timestamp TEXT PRIMARY KEY,
                    overall_status TEXT NOT NULL,
                    components_status TEXT NOT NULL,
                    metrics_summary TEXT NOT NULL
                )
            """)
            
            conn.commit()
            conn.close()
    
    def record_metric(
        self, 
        component: str, 
        metric_name: str, 
        value: float, 
        details: Dict = None,
        check_alert: bool = True
    ):
        """
        记录监控指标
        
        Args:
            component: 组件名称（如 "data_ingestion", "indicator_calculation"）
            metric_name: 指标名称（如 "data_latency", "cache_hit_rate"）
            value: 指标值
            details: 详细信息
            check_alert: 是否检查告警
        """
        timestamp = self._utc_now().isoformat()
        if not _is_finite_metric_value(value):
            logger.debug("Skipping non-finite metric: %s.%s=%r", component, metric_name, value)
            return
        metric_value = float(value)
        
        # 确定告警级别
        alert_level = self._check_alert_level(component, metric_name, metric_value) if check_alert else None
        
        # 内存中保存最近数据
        key = f"{component}.{metric_name}"
        if key not in self.metrics:
            self.metrics[key] = []
        
        metric_data = {
            "timestamp": timestamp,
            "value": metric_value,
            "details": details,
            "alert_level": alert_level
        }
        
        self.metrics[key].append(metric_data)
        
        # 保留最近100个点
        if len(self.metrics[key]) > 100:
            self.metrics[key] = self.metrics[key][-100:]
        
        # 存储到数据库
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO health_metrics 
                (timestamp, component, metric_name, metric_value, details, alert_level)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                timestamp,
                component,
                metric_name,
                metric_value,
                json.dumps(details) if details else None,
                alert_level
            ))
            
            # 如果触发了告警，记录到告警历史
            if alert_level and alert_level in ["warning", "critical"]:
                self._record_alert(
                    cursor, timestamp, component, metric_name, 
                    alert_level, metric_value, details
                )
            
            conn.commit()
            conn.close()
        
        logger.debug(f"Recorded metric: {component}.{metric_name} = {metric_value}, alert={alert_level}")

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    
    def _check_alert_level(self, component: str, metric_name: str, value: float) -> Optional[str]:
        """检查指标值是否触发告警"""
        if metric_name not in self.alerts:
            return None
        
        thresholds = self.alerts[metric_name]
        
        # 对于延迟类指标，值越大越差
        if metric_name in ["data_latency", "indicator_freshness", "economic_calendar_staleness", "economic_provider_failures"]:
            if value >= thresholds["critical"]:
                return "critical"
            elif value >= thresholds["warning"]:
                return "warning"
        
        # 对于命中率类指标，值越小越差
        elif metric_name in ["cache_hit_rate"]:
            if value <= thresholds["critical"]:
                return "critical"
            elif value <= thresholds["warning"]:
                return "warning"
        
        # 对于队列深度类指标，值越大越差
        elif metric_name in ["queue_depth"]:
            if value >= thresholds["critical"]:
                return "critical"
            elif value >= thresholds["warning"]:
                return "warning"
        
        return None
    
    def _record_alert(
        self, cursor, timestamp: str, component: str, metric_name: str,
        alert_level: str, value: float, details: Dict = None
    ):
        """记录告警到历史表"""
        alert_key = f"{component}.{metric_name}"
        threshold = self.alerts[metric_name][alert_level]
        
        # 检查是否已有未解决的相同告警
        cursor.execute("""
            SELECT id FROM alert_history 
            WHERE component = ? AND metric_name = ? AND alert_level = ? AND resolved_at IS NULL
            ORDER BY timestamp DESC LIMIT 1
        """, (component, metric_name, alert_level))
        
        existing = cursor.fetchone()
        
        if existing:
            # 更新现有告警的时间戳（避免重复告警）
            cursor.execute("""
                UPDATE alert_history 
                SET timestamp = ?, metric_value = ?
                WHERE id = ?
            """, (timestamp, value, existing[0]))
        else:
            # 插入新告警
            message = self._generate_alert_message(component, metric_name, alert_level, value, threshold)
            
            cursor.execute("""
                INSERT INTO alert_history 
                (timestamp, component, metric_name, alert_level, metric_value, threshold, message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp,
                component,
                metric_name,
                alert_level,
                value,
                threshold,
                message
            ))
            
            # 添加到活动告警
            self.active_alerts[alert_key] = {
                "timestamp": timestamp,
                "component": component,
                "metric_name": metric_name,
                "alert_level": alert_level,
                "value": value,
                "threshold": threshold,
                "message": message
            }
            
            logger.warning(f"New alert: {message}")
    
    def _generate_alert_message(
        self, component: str, metric_name: str, alert_level: str, 
        value: float, threshold: float
    ) -> str:
        """生成告警消息"""
        if metric_name == "data_latency":
            return f"{component}: 数据延迟{alert_level}告警 - 当前延迟{value:.1f}秒，阈值{threshold}秒"
        elif metric_name == "indicator_freshness":
            return f"{component}: 指标新鲜度{alert_level}告警 - 当前延迟{value:.1f}秒，阈值{threshold}秒"
        elif metric_name == "queue_depth":
            return f"{component}: 队列深度{alert_level}告警 - 当前深度{value}，阈值{threshold}"
        elif metric_name == "cache_hit_rate":
            return f"{component}: 缓存命中率{alert_level}告警 - 当前命中率{value:.1%}，阈值{threshold:.1%}"
        else:
            return f"{component}.{metric_name}: {alert_level}告警 - 当前值{value}，阈值{threshold}"
    
    def resolve_alert(self, component: str, metric_name: str, resolved_by: str = "system"):
        """解决告警"""
        alert_key = f"{component}.{metric_name}"
        
        if alert_key not in self.active_alerts:
            return False
        
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE alert_history 
                SET resolved_at = ?, resolved_by = ?
                WHERE component = ? AND metric_name = ? AND resolved_at IS NULL
            """, (
                self._utc_now().isoformat(),
                resolved_by,
                component,
                metric_name
            ))
            
            conn.commit()
            conn.close()
        
        # 从活动告警中移除
        del self.active_alerts[alert_key]
        logger.info(f"Resolved alert: {component}.{metric_name}")
        
        return True
    
    def check_data_latency(self, component: str, service, symbol: str, timeframe: str) -> float:
        """
        检查数据延迟
        
        Args:
            service: MarketDataService实例
            symbol: 交易品种
            timeframe: 时间框架
            
        Returns:
            延迟秒数
        """
        try:
            latest_bar = service.get_latest_ohlc(symbol, timeframe)
            if not latest_bar:
                latency = float('inf')
            else:
                bar_open_time = self._as_utc(latest_bar.time)
                interval_seconds = max(1, timeframe_seconds(timeframe))
                next_expected_close = bar_open_time + timedelta(seconds=interval_seconds * 2)
                latency = max(0.0, (self._utc_now() - next_expected_close).total_seconds())
            
            self.record_metric(
                component,
                "data_latency",
                latency,
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "latest_bar_time": latest_bar.time.isoformat() if latest_bar else None,
                    "interval_seconds": interval_seconds if latest_bar else None,
                    "next_expected_close": next_expected_close.isoformat() if latest_bar else None,
                }
            )
            
            return latency
            
        except Exception as e:
            logger.error(f"Failed to check data latency for {symbol}/{timeframe}: {e}")
            return float('inf')
    
    def check_indicator_freshness(self, component: str, worker, symbol: str, timeframe: str) -> float:
        """
        检查指标新鲜度
        
        Args:
            worker: IndicatorWorker实例
            symbol: 交易品种
            timeframe: 时间框架
            
        Returns:
            新鲜度秒数（最后计算时间到现在的时间差）
        """
        try:
            snapshot = worker.get_snapshot(symbol, timeframe)
            if not snapshot:
                freshness = float('inf')
            else:
                # 获取快照时间
                if hasattr(snapshot, 'timestamp'):
                    snapshot_time = snapshot.timestamp
                elif hasattr(snapshot, 'bar_time'):
                    snapshot_time = snapshot.bar_time
                else:
                    snapshot_time = self._utc_now() - timedelta(days=1)
                
                freshness = (self._utc_now() - self._as_utc(snapshot_time)).total_seconds()
            
            self.record_metric(
                component,
                "indicator_freshness",
                freshness,
                {"symbol": symbol, "timeframe": timeframe}
            )
            
            return freshness
            
        except Exception as e:
            logger.error(f"Failed to check indicator freshness for {symbol}/{timeframe}: {e}")
            return float('inf')
    
    def check_queue_stats(self, component: str, ingestor) -> Dict[str, Any]:
        """
        检查队列统计
        
        Args:
            ingestor: BackgroundIngestor实例
            
        Returns:
            队列统计信息
        """
        try:
            stats = ingestor.queue_stats()
            
            # 记录各个队列的深度
            for queue_name, queue_info in stats.get("queues", {}).items():
                depth = queue_info.get("size", 0) + queue_info.get("pending", 0)
                self.record_metric(
                    component,
                    "queue_depth",
                    depth,
                    {"queue_name": queue_name, "stats": queue_info}
                )
            
            return stats
            
        except Exception as e:
            logger.error(f"Failed to check queue stats: {e}")
            return {}
    
    def check_cache_stats(self, component: str, worker) -> Dict[str, Any]:
        """
        检查缓存统计
        
        Args:
            worker: IndicatorWorker实例
            
        Returns:
            缓存统计信息
        """
        try:
            stats = worker.get_performance_stats() if hasattr(worker, 'get_performance_stats') else worker.stats()
            
            # 记录缓存命中率
            cache_hits = stats.get("cache_hits", 0)
            cache_misses = stats.get("cache_misses", 0)
            total = cache_hits + cache_misses
            
            if total > 0:
                hit_rate = cache_hits / total
                self.record_metric(
                    component,
                    "cache_hit_rate",
                    hit_rate,
                    {"hits": cache_hits, "misses": cache_misses, "total": total}
                )
            
            return stats
            
        except Exception as e:
            logger.error(f"Failed to check cache stats: {e}")
            return {}

    def check_economic_calendar(self, component: str, service) -> Dict[str, Any]:
        try:
            stats = service.stats()
            last_refresh_at = stats.get("last_refresh_at")
            if last_refresh_at:
                refresh_time = self._as_utc(datetime.fromisoformat(last_refresh_at))
                stale_seconds = (self._utc_now() - refresh_time).total_seconds()
            else:
                stale_seconds = float("inf")

            provider_status = stats.get("provider_status") or {}
            if isinstance(provider_status, dict):
                provider_failures = float(
                    sum(int((provider_status.get(name) or {}).get("consecutive_failures", 0)) for name in provider_status)
                )
            else:
                provider_failures = 0.0

            self.record_metric(
                component,
                "economic_calendar_staleness",
                stale_seconds,
                {"stats": stats},
            )
            self.record_metric(
                component,
                "economic_provider_failures",
                provider_failures,
                {"provider_status": provider_status},
            )
            return stats
        except Exception as e:
            logger.error(f"Failed to check economic calendar health: {e}")
            return {}
    
    def _generate_report_legacy(self, hours: int = 24) -> Dict[str, Any]:
        """
        生成健康报告
        
        Args:
            hours: 报告时间范围（小时）
            
        Returns:
            健康报告字典
        """
        cutoff = self._utc_now() - timedelta(hours=hours)
        
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 获取各组件最新状态
            cursor.execute("""
                SELECT component, metric_name, metric_value, timestamp
                FROM health_metrics
                WHERE timestamp > ?
                ORDER BY component, metric_name, timestamp
            """, (cutoff.isoformat(),))
            
            report = {
                "timestamp": self._utc_now().isoformat(),
                "time_range_hours": hours,
                "overall_status": "healthy",
                "components": {},
                "active_alerts": list(self.active_alerts.values()),
                "summary": {
                    "total_metrics": 0,
                    "warning_count": 0,
                    "critical_count": 0,
                    "advisory_warning_count": 0,
                    "advisory_critical_count": 0
                }
            }
            
            for row in cursor.fetchall():
                component, metric_name, avg_value, min_value, max_value, sample_count, last_updated = row
                
                if component not in report["components"]:
                    report["components"][component] = {}
                
                # 确定状态
                alert_level = self._check_alert_level(component, metric_name, avg_value)
                status = "healthy"
                if alert_level == "warning":
                    status = "warning"
                    report["summary"]["warning_count"] += 1
                elif alert_level == "critical":
                    status = "critical"
                    report["summary"]["critical_count"] += 1
                
                report["components"][component][metric_name] = {
                    "average": avg_value,
                    "min": min_value,
                    "max": max_value,
                    "samples": sample_count,
                    "last_updated": last_updated,
                    "status": status,
                    "alert_level": alert_level
                }
                
                report["summary"]["total_metrics"] += 1
            
            # 确定整体状态
            if report["summary"]["critical_count"] > 0:
                report["overall_status"] = "critical"
            elif report["summary"]["warning_count"] > 0:
                report["overall_status"] = "warning"
            
            # 获取最近告警
            cursor.execute("""
                SELECT timestamp, component, metric_name, alert_level, metric_value, threshold, message
                FROM alert_history
                WHERE timestamp > ?
                ORDER BY timestamp DESC
                LIMIT 20
            """, (cutoff.isoformat(),))
            
            report["recent_alerts"] = []
            for row in cursor.fetchall():
                timestamp, component, metric_name, alert_level, metric_value, threshold, message = row
                report["recent_alerts"].append({
                    "timestamp": timestamp,
                    "component": component,
                    "metric_name": metric_name,
                    "alert_level": alert_level,
                    "metric_value": metric_value,
                    "threshold": threshold,
                    "message": message
                })
            
            # 保存系统状态
            cursor.execute("""
                INSERT OR REPLACE INTO system_status 
                (timestamp, overall_status, components_status, metrics_summary)
                VALUES (?, ?, ?, ?)
            """, (
                self._utc_now().isoformat(),
                report["overall_status"],
                json.dumps(report["components"]),
                json.dumps(report["summary"])
            ))
            
            conn.commit()
            conn.close()
        
        return report
    
    def generate_report(self, hours: int = 24) -> Dict[str, Any]:
        """Generate a health report using the latest valid samples per metric."""
        cutoff = self._utc_now() - timedelta(hours=hours)

        with self._lock:
            conn = sqlite3.connect(self.db_path)
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
                "timestamp": self._utc_now().isoformat(),
                "time_range_hours": hours,
                "overall_status": "healthy",
                "components": {},
                "active_alerts": list(self.active_alerts.values()),
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
                if not _is_finite_metric_value(metric_value):
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
                min_value = min(values)
                max_value = max(values)
                sample_count = len(values)
                last_updated = metric_data["last_updated"]

                if component not in report["components"]:
                    report["components"][component] = {}

                alert_level = self._check_alert_level(component, metric_name, latest_value)
                status = "healthy"
                impact = _metric_overall_impact(metric_name)
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
                    "min": min_value,
                    "max": max_value,
                    "samples": sample_count,
                    "last_updated": last_updated,
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

            cursor.execute(
                """
                SELECT timestamp, component, metric_name, alert_level, metric_value, threshold, message
                FROM alert_history
                WHERE timestamp > ?
                ORDER BY timestamp DESC
                LIMIT 20
                """,
                (cutoff.isoformat(),),
            )

            report["recent_alerts"] = []
            for row in cursor.fetchall():
                timestamp, component, metric_name, alert_level, metric_value, threshold, message = row
                report["recent_alerts"].append(
                    {
                        "timestamp": timestamp,
                        "component": component,
                        "metric_name": metric_name,
                        "alert_level": alert_level,
                        "metric_value": metric_value,
                        "threshold": threshold,
                        "message": message,
                    }
                )

            cursor.execute(
                """
                INSERT OR REPLACE INTO system_status
                (timestamp, overall_status, components_status, metrics_summary)
                VALUES (?, ?, ?, ?)
                """,
                (
                    self._utc_now().isoformat(),
                    report["overall_status"],
                    json.dumps(report["components"]),
                    json.dumps(report["summary"]),
                ),
            )

            conn.commit()
            conn.close()

        return report

    def get_recent_metrics(self, component: str, metric_name: str, limit: int = 100) -> List[Dict]:
        """
        获取最近的指标数据
        
        Args:
            component: 组件名称
            metric_name: 指标名称
            limit: 返回数据点数量
            
        Returns:
            指标数据列表
        """
        key = f"{component}.{metric_name}"
        
        # 先从内存获取
        if key in self.metrics:
            return self.metrics[key][-limit:]
        
        # 从数据库获取
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT timestamp, metric_value, details, alert_level
                FROM health_metrics
                WHERE component = ? AND metric_name = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (component, metric_name, limit))
            
            metrics = []
            for timestamp, metric_value, details_json, alert_level in cursor.fetchall():
                details = json.loads(details_json) if details_json else None
                metrics.append({
                    "timestamp": timestamp,
                    "value": metric_value,
                    "details": details,
                    "alert_level": alert_level
                })
            
            conn.close()
        
        return metrics
    
    def cleanup_old_data(self, days_to_keep: int = 30):
        """
        清理旧数据
        
        Args:
            days_to_keep: 保留天数
        """
        cutoff = self._utc_now() - timedelta(days=days_to_keep)
        
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 清理旧指标数据
            cursor.execute("""
                DELETE FROM health_metrics 
                WHERE timestamp < ?
            """, (cutoff.isoformat(),))
            
            metrics_deleted = cursor.rowcount
            
            # 清理旧告警历史（只保留已解决的）
            cursor.execute("""
                DELETE FROM alert_history 
                WHERE timestamp < ? AND resolved_at IS NOT NULL
            """, (cutoff.isoformat(),))
            
            alerts_deleted = cursor.rowcount
            
            # 清理旧系统状态
            cursor.execute("""
                DELETE FROM system_status 
                WHERE timestamp < ?
            """, (cutoff.isoformat(),))
            
            status_deleted = cursor.rowcount
            
            conn.commit()
            conn.close()
        
        logger.info(f"Cleaned up health monitor data: {metrics_deleted} metrics, "
                   f"{alerts_deleted} alerts, {status_deleted} status records")
    
    def get_system_status(self) -> Dict[str, Any]:
        """
        获取最新系统状态
        
        Returns:
            系统状态信息
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT timestamp, overall_status, components_status, metrics_summary
                FROM system_status
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                timestamp, overall_status, components_status_json, metrics_summary_json = row
                return {
                    "timestamp": timestamp,
                    "overall_status": overall_status,
                    "components_status": json.loads(components_status_json),
                    "metrics_summary": json.loads(metrics_summary_json),
                    "active_alerts": list(self.active_alerts.values())
                }
            else:
                return {
                    "timestamp": self._utc_now().isoformat(),
                    "overall_status": "unknown",
                    "components_status": {},
                    "metrics_summary": {},
                    "active_alerts": []
                }



# 单例实例
_health_monitor_instance = None


def get_health_monitor(db_path: str = "health_monitor.db") -> HealthMonitor:
    """获取健康监控单例"""
    global _health_monitor_instance
    if _health_monitor_instance is None:
        _health_monitor_instance = HealthMonitor(db_path)
    return _health_monitor_instance
