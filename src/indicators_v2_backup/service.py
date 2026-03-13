"""
优化指标服务适配器

将优化后的指标模块包装成与现有EnhancedIndicatorWorker兼容的接口
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass

from src.core.market_service import MarketDataService
from src.clients.mt5_market import OHLC
from src.indicators.types import IndicatorTask
from src.config import IndicatorSettings

from .engine.pipeline_v2 import OptimizedPipeline, PipelineConfig, get_global_pipeline
from .engine.dependency_manager import get_global_dependency_manager
from .monitoring.metrics_collector import get_global_collector, record_indicator_computation

logger = logging.getLogger(__name__)


@dataclass
class OptimizedIndicatorSnapshot:
    """优化指标快照"""
    symbol: str
    timeframe: str
    data: Dict[str, Any]
    timestamp: datetime
    bar_time: Optional[datetime] = None
    cache_hit: bool = False
    incremental: bool = False
    compute_time_ms: float = 0.0


class OptimizedIndicatorService:
    """
    优化指标服务
    
    提供与EnhancedIndicatorWorker兼容的接口，但内部使用优化计算引擎
    """
    
    def __init__(
        self,
        service: MarketDataService,
        symbols: List[str],
        timeframes: List[str],
        tasks: List[IndicatorTask],
        indicator_settings: Optional[IndicatorSettings] = None,
        pipeline_config: Optional[PipelineConfig] = None
    ):
        self.service = service
        self.symbols = symbols
        self.timeframes = timeframes
        self.tasks = tasks
        self.settings = indicator_settings or IndicatorSettings()
        
        # 优化流水线配置
        self.pipeline_config = pipeline_config or PipelineConfig(
            enable_parallel=True,
            max_workers=4,
            enable_cache=True,
            cache_ttl=300.0,
            enable_incremental=True,
            max_retries=2,
            enable_monitoring=True
        )
        
        # 初始化优化流水线
        self.pipeline = get_global_pipeline(self.pipeline_config)
        
        # 指标快照缓存
        self._snapshots: Dict[str, OptimizedIndicatorSnapshot] = {}
        self._snapshot_lock = threading.RLock()
        
        # 线程控制
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        
        # 性能统计
        self._stats = {
            "total_computations": 0,
            "successful_computations": 0,
            "failed_computations": 0,
            "cache_hits": 0,
            "incremental_computations": 0,
            "total_compute_time_ms": 0.0
        }
        
        # 注册指标任务到依赖管理器
        self._register_tasks()
        
        logger.info(f"OptimizedIndicatorService initialized with {len(tasks)} tasks")
    
    def _register_tasks(self) -> None:
        """注册指标任务到依赖管理器"""
        dependency_manager = get_global_dependency_manager()
        
        for task in self.tasks:
            try:
                # 动态导入指标函数
                module_path, func_name = task.func_path.rsplit('.', 1)
                module = __import__(module_path, fromlist=[func_name])
                func = getattr(module, func_name)
                
                # 注册到依赖管理器
                dependency_manager.add_indicator(
                    name=task.name,
                    func=func,
                    params=task.params,
                    dependencies=[]  # 暂时不处理依赖，后续可以扩展
                )
                
                logger.debug(f"Registered indicator: {task.name}")
                
            except Exception as e:
                logger.error(f"Failed to register indicator {task.name}: {e}")
    
    def start(self) -> None:
        """启动指标计算服务"""
        if self._thread and self._thread.is_alive():
            return
        
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="OptimizedIndicatorService",
            daemon=True
        )
        self._thread.start()
        logger.info("OptimizedIndicatorService started")
    
    def stop(self) -> None:
        """停止指标计算服务"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("OptimizedIndicatorService stopped")
    
    def _poll_loop(self) -> None:
        """轮询计算循环"""
        poll_interval = self.settings.poll_seconds
        
        while not self._stop.is_set():
            try:
                self._compute_all_indicators()
            except Exception as e:
                logger.error(f"Error in indicator computation loop: {e}")
            
            # 等待下一次轮询
            time.sleep(poll_interval)
    
    def _compute_all_indicators(self) -> None:
        """计算所有配置的指标"""
        for symbol in self.symbols:
            for timeframe in self.timeframes:
                try:
                    self._compute_indicators_for_symbol(symbol, timeframe)
                except Exception as e:
                    logger.error(f"Error computing indicators for {symbol}/{timeframe}: {e}")
    
    def _compute_indicators_for_symbol(self, symbol: str, timeframe: str) -> None:
        """为特定品种和时间框架计算指标"""
        # 获取K线数据
        lookback = self._get_max_lookback()
        bars = self.service.get_ohlc(symbol, timeframe, count=lookback)
        
        if not bars or len(bars) < 2:
            logger.debug(f"Insufficient data for {symbol}/{timeframe}: {len(bars) if bars else 0} bars")
            return
        
        # 获取需要计算的指标列表
        indicator_names = [task.name for task in self.tasks]
        
        # 使用优化流水线计算
        start_time = time.time()
        results = self.pipeline.compute(symbol, timeframe, bars, indicator_names)
        compute_time = (time.time() - start_time) * 1000  # 转换为毫秒
        
        # 更新快照
        with self._snapshot_lock:
            for indicator_name, result in results.items():
                if result is not None:
                    snapshot_key = f"{symbol}_{timeframe}_{indicator_name}"
                    snapshot = OptimizedIndicatorSnapshot(
                        symbol=symbol,
                        timeframe=timeframe,
                        data=result,
                        timestamp=datetime.now(),
                        bar_time=bars[-1].time if bars else None,
                        cache_hit=False,  # 流水线内部处理缓存
                        incremental=False,  # 流水线内部处理增量
                        compute_time_ms=compute_time
                    )
                    self._snapshots[snapshot_key] = snapshot
        
        # 更新统计
        with self._snapshot_lock:
            self._stats["total_computations"] += len(indicator_names)
            self._stats["successful_computations"] += len(results)
            self._stats["failed_computations"] += (len(indicator_names) - len(results))
            self._stats["total_compute_time_ms"] += compute_time
    
    def _get_max_lookback(self) -> int:
        """获取最大回溯周期"""
        max_lookback = 0
        for task in self.tasks:
            params = task.params
            # 查找min_bars或period参数
            min_bars = params.get('min_bars', 0)
            period = params.get('period', 0)
            max_lookback = max(max_lookback, min_bars, period)
        
        # 默认至少100根K线
        return max(max_lookback, 100)
    
    def get_indicator(
        self,
        symbol: str,
        timeframe: str,
        indicator_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        获取单个指标值
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            indicator_name: 指标名称
            
        Returns:
            指标值字典，如果不存在则返回None
        """
        snapshot_key = f"{symbol}_{timeframe}_{indicator_name}"
        
        with self._snapshot_lock:
            snapshot = self._snapshots.get(snapshot_key)
        
        if snapshot:
            return snapshot.data
        return None
    
    def get_all_indicators(
        self,
        symbol: str,
        timeframe: str
    ) -> Dict[str, Dict[str, Any]]:
        """
        获取指定品种和时间框架的所有指标
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            
        Returns:
            指标名称到值的映射
        """
        result = {}
        prefix = f"{symbol}_{timeframe}_"
        
        with self._snapshot_lock:
            for key, snapshot in self._snapshots.items():
                if key.startswith(prefix):
                    indicator_name = key[len(prefix):]
                    result[indicator_name] = snapshot.data
        
        return result
    
    def compute_on_demand(
        self,
        symbol: str,
        timeframe: str,
        indicator_names: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """
        按需计算指标（实时计算，不依赖缓存）
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            indicator_names: 指标名称列表，如果为None则计算所有
            
        Returns:
            计算结果字典
        """
        # 获取K线数据
        lookback = self._get_max_lookback()
        bars = self.service.get_ohlc(symbol, timeframe, count=lookback)
        
        if not bars or len(bars) < 2:
            logger.warning(f"Insufficient data for on-demand computation: {symbol}/{timeframe}")
            return {}
        
        # 确定要计算的指标
        if indicator_names is None:
            indicator_names = [task.name for task in self.tasks]
        
        # 使用流水线计算
        results = self.pipeline.compute(symbol, timeframe, bars, indicator_names)
        
        # 更新快照
        with self._snapshot_lock:
            for indicator_name, result in results.items():
                if result is not None:
                    snapshot_key = f"{symbol}_{timeframe}_{indicator_name}"
                    snapshot = OptimizedIndicatorSnapshot(
                        symbol=symbol,
                        timeframe=timeframe,
                        data=result,
                        timestamp=datetime.now(),
                        bar_time=bars[-1].time if bars else None,
                        cache_hit=False,
                        incremental=False,
                        compute_time_ms=0.0  # 流水线内部会记录
                    )
                    self._snapshots[snapshot_key] = snapshot
        
        return results
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """获取性能统计"""
        with self._snapshot_lock:
            stats = self._stats.copy()
            
            # 计算成功率
            total = stats["total_computations"]
            if total > 0:
                stats["success_rate"] = (stats["successful_computations"] / total) * 100
                stats["average_compute_time_ms"] = stats["total_compute_time_ms"] / total
            else:
                stats["success_rate"] = 0.0
                stats["average_compute_time_ms"] = 0.0
            
            # 获取流水线统计
            pipeline_stats = self.pipeline.get_stats()
            stats["pipeline"] = pipeline_stats
            
            # 获取快照数量
            stats["snapshot_count"] = len(self._snapshots)
            
            return stats
    
    def get_snapshot_freshness(self) -> Dict[str, datetime]:
        """获取快照新鲜度（最后更新时间）"""
        freshness = {}
        
        with self._snapshot_lock:
            for key, snapshot in self._snapshots.items():
                freshness[key] = snapshot.timestamp
        
        return freshness
    
    def clear_cache(self) -> int:
        """清空缓存"""
        cache_count = self.pipeline.clear_cache()
        logger.info(f"Cleared {cache_count} cache entries")
        return cache_count
    
    def shutdown(self) -> None:
        """关闭服务"""
        self.stop()
        self.pipeline.shutdown()


# 全局优化指标服务实例
_global_optimized_service: Optional[OptimizedIndicatorService] = None


def get_global_optimized_service(
    service: Optional[MarketDataService] = None,
    symbols: Optional[List[str]] = None,
    timeframes: Optional[List[str]] = None,
    tasks: Optional[List[IndicatorTask]] = None,
    config: Optional[PipelineConfig] = None
) -> OptimizedIndicatorService:
    """
    获取全局优化指标服务实例（单例模式）
    
    Args:
        service: 市场数据服务
        symbols: 交易品种列表
        timeframes: 时间框架列表
        tasks: 指标任务列表
        config: 流水线配置
        
    Returns:
        优化指标服务实例
    """
    global _global_optimized_service
    
    if _global_optimized_service is None:
        if service is None or symbols is None or timeframes is None or tasks is None:
            raise ValueError("首次调用需要提供所有必要参数")
        
        _global_optimized_service = OptimizedIndicatorService(
            service=service,
            symbols=symbols,
            timeframes=timeframes,
            tasks=tasks,
            pipeline_config=config
        )
    
    return _global_optimized_service


def shutdown_global_optimized_service() -> None:
    """关闭全局优化指标服务"""
    global _global_optimized_service
    if _global_optimized_service is not None:
        _global_optimized_service.shutdown()
        _global_optimized_service = None