"""
性能指标收集器
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import time
import threading
import logging
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


@dataclass
class IndicatorMetrics:
    """单个指标计算指标"""
    
    name: str
    compute_time: float  # 计算耗时（秒）
    cache_hit: bool  # 是否命中缓存
    data_points: int  # 数据点数量
    timestamp: datetime  # 计算时间
    success: bool = True  # 是否成功
    error_msg: Optional[str] = None  # 错误信息
    symbol: Optional[str] = None  # 交易品种
    timeframe: Optional[str] = None  # 时间框架
    incremental: bool = False  # 是否使用增量计算


@dataclass
class AggregatedMetrics:
    """聚合指标"""
    
    name: str
    total_computations: int = 0
    successful_computations: int = 0
    failed_computations: int = 0
    total_compute_time: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0
    incremental_computations: int = 0
    full_computations: int = 0
    avg_data_points: float = 0.0
    
    # 时间窗口统计
    last_hour_computations: int = 0
    last_hour_avg_time: float = 0.0
    last_hour_cache_hit_rate: float = 0.0
    
    def update(self, metrics: IndicatorMetrics) -> None:
        """更新聚合指标"""
        self.total_computations += 1
        
        if metrics.success:
            self.successful_computations += 1
            self.total_compute_time += metrics.compute_time
            self.avg_data_points = (
                (self.avg_data_points * (self.successful_computations - 1) + metrics.data_points)
                / self.successful_computations
            )
        else:
            self.failed_computations += 1
        
        if metrics.cache_hit:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        
        if metrics.incremental:
            self.incremental_computations += 1
        else:
            self.full_computations += 1
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        total = self.total_computations
        successful = self.successful_computations
        
        avg_compute_time = (
            self.total_compute_time / successful if successful > 0 else 0
        )
        
        cache_hit_rate = (
            self.cache_hits / total * 100 if total > 0 else 0
        )
        
        incremental_rate = (
            self.incremental_computations / total * 100 if total > 0 else 0
        )
        
        success_rate = (
            self.successful_computations / total * 100 if total > 0 else 0
        )
        
        return {
            "name": self.name,
            "total_computations": total,
            "successful_computations": successful,
            "failed_computations": self.failed_computations,
            "success_rate": f"{success_rate:.2f}%",
            "avg_compute_time_ms": avg_compute_time * 1000,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": f"{cache_hit_rate:.2f}%",
            "incremental_computations": self.incremental_computations,
            "full_computations": self.full_computations,
            "incremental_rate": f"{incremental_rate:.2f}%",
            "avg_data_points": self.avg_data_points,
            "last_hour_computations": self.last_hour_computations,
            "last_hour_avg_time_ms": self.last_hour_avg_time * 1000,
            "last_hour_cache_hit_rate": f"{self.last_hour_cache_hit_rate:.2f}%"
        }


class MetricsCollector:
    """
    性能指标收集器
    
    功能：
    1. 收集指标计算性能数据
    2. 提供实时统计信息
    3. 支持时间窗口分析
    4. 生成性能报告
    """
    
    def __init__(self, max_history: int = 10000, time_window_hours: int = 24):
        """
        初始化指标收集器
        
        Args:
            max_history: 最大历史记录数
            time_window_hours: 时间窗口小时数
        """
        self.max_history = max_history
        self.time_window_hours = time_window_hours
        
        # 存储所有指标记录
        self.metrics_history: deque[IndicatorMetrics] = deque(maxlen=max_history)
        
        # 聚合指标
        self.aggregated_metrics: Dict[str, AggregatedMetrics] = {}
        
        # 线程安全
        self.lock = threading.RLock()
        
        # 初始化时间
        self.start_time = datetime.now()
        
        logger.info(f"MetricsCollector initialized: max_history={max_history}, time_window={time_window_hours}h")
    
    def record_computation(self, metrics: IndicatorMetrics) -> None:
        """
        记录指标计算
        
        Args:
            metrics: 指标计算指标
        """
        with self.lock:
            # 添加到历史记录
            self.metrics_history.append(metrics)
            
            # 更新聚合指标
            if metrics.name not in self.aggregated_metrics:
                self.aggregated_metrics[metrics.name] = AggregatedMetrics(name=metrics.name)
            
            self.aggregated_metrics[metrics.name].update(metrics)
            
            # 更新时间窗口统计
            self._update_time_window_stats()
            
            # 记录日志
            log_level = logging.DEBUG if metrics.success else logging.ERROR
            logger.log(
                log_level,
                f"Indicator computation: {metrics.name}, "
                f"time={metrics.compute_time*1000:.2f}ms, "
                f"cache_hit={metrics.cache_hit}, "
                f"incremental={metrics.incremental}, "
                f"success={metrics.success}"
            )
    
    def _update_time_window_stats(self) -> None:
        """更新时间窗口统计"""
        cutoff_time = datetime.now() - timedelta(hours=1)
        
        # 按指标名称分组
        hour_metrics: Dict[str, List[IndicatorMetrics]] = defaultdict(list)
        
        for metrics in self.metrics_history:
            if metrics.timestamp >= cutoff_time:
                hour_metrics[metrics.name].append(metrics)
        
        # 更新每个指标的最近一小时统计
        for name, metrics_list in hour_metrics.items():
            if name in self.aggregated_metrics:
                agg = self.aggregated_metrics[name]
                
                agg.last_hour_computations = len(metrics_list)
                
                # 计算平均耗时
                successful_metrics = [m for m in metrics_list if m.success]
                if successful_metrics:
                    total_time = sum(m.compute_time for m in successful_metrics)
                    agg.last_hour_avg_time = total_time / len(successful_metrics)
                else:
                    agg.last_hour_avg_time = 0
                
                # 计算缓存命中率
                cache_hits = sum(1 for m in metrics_list if m.cache_hit)
                agg.last_hour_cache_hit_rate = (
                    cache_hits / len(metrics_list) * 100 if metrics_list else 0
                )
    
    def get_metrics_for_indicator(self, name: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取指定指标的详细指标
        
        Args:
            name: 指标名称
            limit: 返回的最大记录数
            
        Returns:
            指标记录列表
        """
        with self.lock:
            result = []
            count = 0
            
            # 从最新开始遍历
            for metrics in reversed(self.metrics_history):
                if metrics.name == name:
                    result.append({
                        "timestamp": metrics.timestamp.isoformat(),
                        "compute_time_ms": metrics.compute_time * 1000,
                        "cache_hit": metrics.cache_hit,
                        "data_points": metrics.data_points,
                        "success": metrics.success,
                        "error_msg": metrics.error_msg,
                        "symbol": metrics.symbol,
                        "timeframe": metrics.timeframe,
                        "incremental": metrics.incremental
                    })
                    count += 1
                    if count >= limit:
                        break
            
            return result
    
    def get_aggregated_report(self) -> Dict[str, Any]:
        """
        获取聚合报告
        
        Returns:
            包含所有指标聚合信息的字典
        """
        with self.lock:
            report = {
                "system": {
                    "start_time": self.start_time.isoformat(),
                    "uptime_hours": (datetime.now() - self.start_time).total_seconds() / 3600,
                    "total_computations": len(self.metrics_history),
                    "unique_indicators": len(self.aggregated_metrics),
                    "max_history": self.max_history,
                    "time_window_hours": self.time_window_hours
                },
                "indicators": {}
            }
            
            # 添加每个指标的聚合信息
            for name, agg in self.aggregated_metrics.items():
                report["indicators"][name] = agg.to_dict()
            
            # 计算系统级统计
            if self.metrics_history:
                successful_metrics = [m for m in self.metrics_history if m.success]
                total_successful = len(successful_metrics)
                
                if total_successful > 0:
                    total_time = sum(m.compute_time for m in successful_metrics)
                    report["system"]["avg_compute_time_ms"] = total_time / total_successful * 1000
                else:
                    report["system"]["avg_compute_time_ms"] = 0
                
                cache_hits = sum(1 for m in self.metrics_history if m.cache_hit)
                report["system"]["cache_hit_rate"] = (
                    f"{cache_hits / len(self.metrics_history) * 100:.2f}%"
                )
                
                incremental_count = sum(1 for m in self.metrics_history if m.incremental)
                report["system"]["incremental_rate"] = (
                    f"{incremental_count / len(self.metrics_history) * 100:.2f}%"
                )
            
            return report
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """
        获取性能摘要
        
        Returns:
            性能摘要信息
        """
        report = self.get_aggregated_report()
        
        # 提取关键指标
        summary = {
            "system": report["system"],
            "top_indicators": [],
            "slowest_indicators": [],
            "most_used_indicators": []
        }
        
        # 找出使用最多的指标
        indicator_stats = []
        for name, stats in report["indicators"].items():
            indicator_stats.append({
                "name": name,
                "total_computations": stats["total_computations"],
                "avg_compute_time_ms": stats["avg_compute_time_ms"],
                "cache_hit_rate": stats["cache_hit_rate"]
            })
        
        # 按使用次数排序
        indicator_stats.sort(key=lambda x: x["total_computations"], reverse=True)
        summary["most_used_indicators"] = indicator_stats[:5]
        
        # 按平均耗时排序
        indicator_stats.sort(key=lambda x: x["avg_compute_time_ms"], reverse=True)
        summary["slowest_indicators"] = [
            {k: v for k, v in item.items() if k != "avg_compute_time_ms"}
            for item in indicator_stats[:5]
        ]
        
        # 综合评分（使用次数/耗时）
        for item in indicator_stats:
            if item["avg_compute_time_ms"] > 0:
                item["score"] = item["total_computations"] / item["avg_compute_time_ms"]
            else:
                item["score"] = 0
        
        indicator_stats.sort(key=lambda x: x["score"], reverse=True)
        summary["top_indicators"] = indicator_stats[:5]
        
        return summary
    
    def clear_history(self) -> None:
        """清空历史记录"""
        with self.lock:
            old_size = len(self.metrics_history)
            self.metrics_history.clear()
            self.aggregated_metrics.clear()
            logger.info(f"Cleared metrics history: {old_size} records removed")
    
    def export_to_json(self) -> Dict[str, Any]:
        """
        导出为JSON格式
        
        Returns:
            JSON格式的数据
        """
        with self.lock:
            return {
                "metadata": {
                    "export_time": datetime.now().isoformat(),
                    "total_records": len(self.metrics_history),
                    "time_window_hours": self.time_window_hours
                },
                "aggregated_report": self.get_aggregated_report(),
                "recent_metrics": [
                    {
                        "timestamp": m.timestamp.isoformat(),
                        "name": m.name,
                        "compute_time_ms": m.compute_time * 1000,
                        "cache_hit": m.cache_hit,
                        "success": m.success
                    }
                    for m in list(self.metrics_history)[-100:]  # 最近100条
                ]
            }


# 全局指标收集器实例
_global_collector: Optional[MetricsCollector] = None


def get_global_collector(max_history: int = 10000, time_window_hours: int = 24) -> MetricsCollector:
    """
    获取全局指标收集器实例（单例模式）
    
    Args:
        max_history: 最大历史记录数
        time_window_hours: 时间窗口小时数
        
    Returns:
        全局指标收集器实例
    """
    global _global_collector
    if _global_collector is None:
        _global_collector = MetricsCollector(
            max_history=max_history,
            time_window_hours=time_window_hours
        )
    return _global_collector


def record_indicator_computation(
    name: str,
    compute_time: float,
    cache_hit: bool,
    data_points: int,
    success: bool = True,
    error_msg: Optional[str] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    incremental: bool = False
) -> None:
    """
    记录指标计算（便捷函数）
    
    Args:
        name: 指标名称
        compute_time: 计算耗时（秒）
        cache_hit: 是否命中缓存
        data_points: 数据点数量
        success: 是否成功
        error_msg: 错误信息
        symbol: 交易品种
        timeframe: 时间框架
        incremental: 是否使用增量计算
    """
    collector = get_global_collector()
    
    metrics = IndicatorMetrics(
        name=name,
        compute_time=compute_time,
        cache_hit=cache_hit,
        data_points=data_points,
        timestamp=datetime.now(),
        success=success,
        error_msg=error_msg,
        symbol=symbol,
        timeframe=timeframe,
        incremental=incremental
    )
    
    collector.record_computation(metrics)


def get_performance_report() -> Dict[str, Any]:
    """
    获取性能报告（便捷函数）
    
    Returns:
        性能报告
    """
    collector = get_global_collector()
    return collector.get_aggregated_report()