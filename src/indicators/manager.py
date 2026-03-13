"""
统一指标管理器

配置驱动的指标管理，提供简单易用的统一接口
"""

from __future__ import annotations

import logging
import threading
import time
import importlib
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable, Union
from dataclasses import dataclass

from src.core.market_service import MarketDataService
from src.clients.mt5_market import OHLC

from .config import (
    UnifiedIndicatorConfig, IndicatorConfig, PipelineConfig,
    ComputeMode, CacheStrategy, get_global_config_manager, get_config
)
from .engine.pipeline_v2 import OptimizedPipeline, get_global_pipeline
from .engine.dependency_manager import get_global_dependency_manager
from .cache.smart_cache import get_global_cache
from .monitoring.metrics_collector import get_global_collector, record_indicator_computation

logger = logging.getLogger(__name__)


@dataclass
class IndicatorResult:
    """指标计算结果"""
    name: str                    # 指标名称
    value: Dict[str, Any]       # 指标值
    symbol: str                 # 交易品种
    timeframe: str              # 时间框架
    timestamp: datetime         # 计算时间
    bar_time: Optional[datetime] = None  # 对应的K线时间
    cache_hit: bool = False     # 是否来自缓存
    incremental: bool = False   # 是否使用增量计算
    compute_time_ms: float = 0.0  # 计算耗时（毫秒）
    success: bool = True        # 是否成功
    error: Optional[str] = None  # 错误信息


class UnifiedIndicatorManager:
    """
    统一指标管理器
    
    提供配置驱动的统一接口，简化指标模块的使用和管理
    """
    
    def __init__(
        self,
        market_service: MarketDataService,
        config: Optional[UnifiedIndicatorConfig] = None,
        config_file: Optional[str] = None
    ):
        """
        初始化统一指标管理器
        
        Args:
            market_service: 市场数据服务
            config: 配置对象，如果为None则从配置文件加载
            config_file: 配置文件路径
        """
        self.market_service = market_service
        
        # 配置管理
        self.config_manager = get_global_config_manager(config_file)
        self.config = config or self.config_manager.get_config()
        
        # 组件初始化
        self._init_components()
        
        # 指标函数缓存
        self._indicator_funcs: Dict[str, Callable] = {}
        
        # 结果缓存
        self._results: Dict[str, IndicatorResult] = {}
        self._results_lock = threading.RLock()
        
        # 线程控制
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reload_thread: Optional[threading.Thread] = None
        
        # 注册指标
        self._register_indicators()
        
        logger.info(f"UnifiedIndicatorManager initialized with {len(self.config.indicators)} indicators")
    
    def _init_components(self) -> None:
        """初始化组件"""
        # 创建流水线配置
        pipeline_config = self.config.pipeline
        
        # 初始化流水线
        self.pipeline = get_global_pipeline(pipeline_config)
        
        # 获取依赖管理器
        self.dependency_manager = get_global_dependency_manager()
        
        # 获取缓存
        self.cache = get_global_cache(
            maxsize=pipeline_config.cache_maxsize,
            ttl=pipeline_config.cache_ttl
        )
        
        # 获取监控收集器
        self.metrics_collector = get_global_collector()
    
    def _register_indicators(self) -> None:
        """注册所有指标到依赖管理器"""
        for indicator_config in self.config.indicators:
            if not indicator_config.enabled:
                logger.debug(f"Skipping disabled indicator: {indicator_config.name}")
                continue
            
            try:
                # 加载指标函数
                func = self._load_indicator_func(indicator_config)
                
                # 注册到依赖管理器
                self.dependency_manager.add_indicator(
                    name=indicator_config.name,
                    func=func,
                    params=indicator_config.params,
                    dependencies=indicator_config.dependencies
                )
                
                # 缓存函数
                self._indicator_funcs[indicator_config.name] = func
                
                logger.debug(f"Registered indicator: {indicator_config.name}")
                
            except Exception as e:
                logger.error(f"Failed to register indicator {indicator_config.name}: {e}")
    
    def _load_indicator_func(self, config: IndicatorConfig) -> Callable:
        """加载指标函数"""
        # 检查缓存
        if config.name in self._indicator_funcs:
            return self._indicator_funcs[config.name]
        
        # 动态导入
        module_path, func_name = config.func_path.rsplit('.', 1)
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        
        return func
    
    def start(self) -> None:
        """启动指标管理器"""
        if self._thread and self._thread.is_alive():
            return
        
        self._stop.clear()
        
        # 启动主计算线程
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="UnifiedIndicatorManager",
            daemon=True
        )
        self._thread.start()
        
        # 启动配置热重载线程
        if self.config.hot_reload:
            self._reload_thread = threading.Thread(
                target=self._reload_loop,
                name="ConfigReloader",
                daemon=True
            )
            self._reload_thread.start()
        
        logger.info("UnifiedIndicatorManager started")
    
    def stop(self) -> None:
        """停止指标管理器"""
        self._stop.set()
        
        if self._thread:
            self._thread.join(timeout=5.0)
        
        if self._reload_thread:
            self._reload_thread.join(timeout=2.0)
        
        logger.info("UnifiedIndicatorManager stopped")
    
    def _poll_loop(self) -> None:
        """轮询计算循环"""
        poll_interval = self.config.pipeline.poll_interval
        
        while not self._stop.is_set():
            try:
                self._compute_all()
            except Exception as e:
                logger.error(f"Error in computation loop: {e}")
            
            # 等待下一次轮询
            time.sleep(poll_interval)
    
    def _reload_loop(self) -> None:
        """配置热重载循环"""
        reload_interval = self.config.reload_interval
        
        while not self._stop.is_set():
            try:
                if self.config_manager.reload():
                    # 配置已更新，重新初始化
                    self.config = self.config_manager.get_config()
                    self._reinitialize()
            except Exception as e:
                logger.error(f"Error in config reload loop: {e}")
            
            # 等待下一次检查
            time.sleep(reload_interval)
    
    def _reinitialize(self) -> None:
        """重新初始化（配置热重载后调用）"""
        logger.info("Reinitializing after config reload")
        
        # 停止当前计算
        if self._thread and self._thread.is_alive():
            self._stop.set()
            self._thread.join(timeout=2.0)
        
        # 重新初始化组件
        self._init_components()
        
        # 清空函数缓存
        self._indicator_funcs.clear()
        
        # 重新注册指标
        self._register_indicators()
        
        # 重启计算线程
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="UnifiedIndicatorManager",
            daemon=True
        )
        self._thread.start()
        
        logger.info("Reinitialization complete")
    
    def _compute_all(self) -> None:
        """计算所有配置的指标"""
        for symbol in self.config.symbols:
            for timeframe in self.config.timeframes:
                try:
                    self._compute_for_symbol(symbol, timeframe)
                except Exception as e:
                    logger.error(f"Error computing for {symbol}/{timeframe}: {e}")
    
    def _compute_for_symbol(self, symbol: str, timeframe: str) -> None:
        """为特定品种和时间框架计算指标"""
        # 获取K线数据
        lookback = self._get_max_lookback()
        bars = self.market_service.get_ohlc(symbol, timeframe, count=lookback)
        
        if not bars or len(bars) < 2:
            logger.debug(f"Insufficient data for {symbol}/{timeframe}")
            return
        
        # 获取需要计算的指标
        indicator_names = [
            config.name for config in self.config.indicators 
            if config.enabled
        ]
        
        # 使用流水线计算
        start_time = time.time()
        results = self.pipeline.compute(symbol, timeframe, bars, indicator_names)
        compute_time = (time.time() - start_time) * 1000
        
        # 保存结果
        with self._results_lock:
            for name, value in results.items():
                if value is not None:
                    result_key = f"{symbol}_{timeframe}_{name}"
                    result = IndicatorResult(
                        name=name,
                        value=value,
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=datetime.now(),
                        bar_time=bars[-1].time if bars else None,
                        cache_hit=False,  # 流水线内部处理
                        incremental=False,  # 流水线内部处理
                        compute_time_ms=compute_time,
                        success=True
                    )
                    self._results[result_key] = result
    
    def _get_max_lookback(self) -> int:
        """获取最大回溯周期"""
        max_lookback = 0
        for config in self.config.indicators:
            if not config.enabled:
                continue
            
            params = config.params
            min_bars = params.get('min_bars', 0)
            period = params.get('period', 0)
            max_lookback = max(max_lookback, min_bars, period)
        
        # 默认至少100根K线
        return max(max_lookback, 100)
    
    # ========== 公共API接口 ==========
    
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
        result_key = f"{symbol}_{timeframe}_{indicator_name}"
        
        with self._results_lock:
            result = self._results.get(result_key)
        
        if result:
            return result.value
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
        results = {}
        prefix = f"{symbol}_{timeframe}_"
        
        with self._results_lock:
            for key, result in self._results.items():
                if key.startswith(prefix):
                    indicator_name = key[len(prefix):]
                    results[indicator_name] = result.value
        
        return results
    
    def compute(
        self,
        symbol: str,
        timeframe: str,
        indicator_names: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """
        按需计算指标（实时计算）
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            indicator_names: 指标名称列表，如果为None则计算所有启用的指标
            
        Returns:
            计算结果字典
        """
        # 获取K线数据
        lookback = self._get_max_lookback()
        bars = self.market_service.get_ohlc(symbol, timeframe, count=lookback)
        
        if not bars or len(bars) < 2:
            logger.warning(f"Insufficient data for computation: {symbol}/{timeframe}")
            return {}
        
        # 确定要计算的指标
        if indicator_names is None:
            indicator_names = [
                config.name for config in self.config.indicators 
                if config.enabled
            ]
        
        # 使用流水线计算
        results = self.pipeline.compute(symbol, timeframe, bars, indicator_names)
        
        # 保存结果
        with self._results_lock:
            for name, value in results.items():
                if value is not None:
                    result_key = f"{symbol}_{timeframe}_{name}"
                    result = IndicatorResult(
                        name=name,
                        value=value,
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=datetime.now(),
                        bar_time=bars[-1].time if bars else None,
                        cache_hit=False,
                        incremental=False,
                        compute_time_ms=0.0,
                        success=True
                    )
                    self._results[result_key] = result
        
        return results
    
    def get_indicator_info(self, name: str) -> Optional[Dict[str, Any]]:
        """
        获取指标信息
        
        Args:
            name: 指标名称
            
        Returns:
            指标信息字典
        """
        config = self.config_manager.get_indicator(name)
        if config is None:
            return None
        
        # 获取依赖信息
        dependencies = self.dependency_manager.get_dependencies(name)
        dependents = self.dependency_manager.get_dependents(name)
        
        return {
            "name": config.name,
            "func_path": config.func_path,
            "params": config.params,
            "dependencies": list(dependencies),
            "dependents": list(dependents),
            "compute_mode": config.compute_mode.value,
            "enabled": config.enabled,
            "description": config.description,
            "tags": config.tags
        }
    
    def list_indicators(self) -> List[Dict[str, Any]]:
        """
        列出所有指标信息
        
        Returns:
            指标信息列表
        """
        indicators = []
        for config in self.config.indicators:
            info = self.get_indicator_info(config.name)
            if info:
                indicators.append(info)
        
        return indicators
    
    def add_indicator(self, config: IndicatorConfig) -> bool:
        """
        添加新指标
        
        Args:
            config: 指标配置
            
        Returns:
            是否添加成功
        """
        try:
            # 添加到配置管理器
            self.config_manager.add_indicator(config)
            
            # 重新加载配置
            self.config = self.config_manager.get_config()
            
            # 注册新指标
            func = self._load_indicator_func(config)
            self.dependency_manager.add_indicator(
                name=config.name,
                func=func,
                params=config.params,
                dependencies=config.dependencies
            )
            self._indicator_funcs[config.name] = func
            
            logger.info(f"Added new indicator: {config.name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add indicator {config.name}: {e}")
            return False
    
    def update_indicator(self, name: str, **kwargs) -> bool:
        """
        更新指标配置
        
        Args:
            name: 指标名称
            **kwargs: 要更新的字段
            
        Returns:
            是否更新成功
        """
        config = self.config_manager.get_indicator(name)
        if config is None:
            logger.error(f"Indicator not found: {name}")
            return False
        
        try:
            # 更新配置
            for key, value in kwargs.items():
                if hasattr(config, key):
                    setattr(config, key, value)
            
            # 保存配置
            self.config_manager.add_indicator(config)
            
            # 重新加载配置
            self.config = self.config_manager.get_config()
            
            # 重新注册指标
            func = self._load_indicator_func(config)
            self.dependency_manager.add_indicator(
                name=config.name,
                func=func,
                params=config.params,
                dependencies=config.dependencies
            )
            self._indicator_funcs[config.name] = func
            
            logger.info(f"Updated indicator: {name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update indicator {name}: {e}")
            return False
    
    def remove_indicator(self, name: str) -> bool:
        """
        移除指标
        
        Args:
            name: 指标名称
            
        Returns:
            是否移除成功
        """
        try:
            # 从配置管理器移除
            success = self.config_manager.remove_indicator(name)
            if not success:
                return False
            
            # 重新加载配置
            self.config = self.config_manager.get_config()
            
            # 从依赖管理器移除
            self.dependency_manager.remove_indicator(name)
            
            # 从函数缓存移除
            if name in self._indicator_funcs:
                del self._indicator_funcs[name]
            
            # 从结果缓存移除
            with self._results_lock:
                keys_to_remove = [
                    key for key in self._results.keys() 
                    if key.endswith(f"_{name}")
                ]
                for key in keys_to_remove:
                    del self._results[key]
            
            logger.info(f"Removed indicator: {name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to remove indicator {name}: {e}")
            return False
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """获取性能统计"""
        # 获取流水线统计
        pipeline_stats = self.pipeline.get_stats()
        
        # 获取结果统计
        with self._results_lock:
            result_stats = {
                "total_results": len(self._results),
                "symbols": len(set(r.symbol for r in self._results.values())),
                "timeframes": len(set(r.timeframe for r in self._results.values())),
                "indicators": len(set(r.name for r in self._results.values()))
            }
        
        # 获取配置统计
        config_stats = {
            "total_indicators": len(self.config.indicators),
            "enabled_indicators": len([c for c in self.config.indicators if c.enabled]),
            "symbols": len(self.config.symbols),
            "timeframes": len(self.config.timeframes),
            "hot_reload": self.config.hot_reload,
            "auto_start": self.config.auto_start
        }
        
        # 合并统计
        stats = {
            "pipeline": pipeline_stats,
            "results": result_stats,
            "config": config_stats,
            "timestamp": datetime.now().isoformat()
        }
        
        return stats
    
    def clear_cache(self) -> int:
        """清空缓存"""
        cache_count = self.pipeline.clear_cache()
        
        # 清空结果缓存
        with self._results_lock:
            self._results.clear()
        
        logger.info(f"Cleared {cache_count} cache entries and all results")
        return cache_count
    
    def get_dependency_graph(self, format: str = "mermaid") -> str:
        """
        获取依赖关系图
        
        Args:
            format: 图形格式（mermaid 或 dot）
            
        Returns:
            依赖关系图
        """
        return self.dependency_manager.visualize(format)
    
    def shutdown(self) -> None:
        """关闭管理器"""
        self.stop()
        self.pipeline.shutdown()


# 全局统一指标管理器实例
_global_unified_manager: Optional[UnifiedIndicatorManager] = None


def get_global_unified_manager(
    market_service: Optional[MarketDataService] = None,
    config_file: Optional[str] = None
) -> UnifiedIndicatorManager:
    """
    获取全局统一指标管理器实例（单例模式）
    
    Args:
        market_service: 市场数据服务
        config_file: 配置文件路径
        
    Returns:
        统一指标管理器实例
    """
    global _global_unified_manager
    
    if _global_unified_manager is None:
        if market_service is None:
            raise ValueError("首次调用需要提供 market_service 参数")
        
        _global_unified_manager = UnifiedIndicatorManager(
            market_service=market_service,
            config_file=config_file
        )
        
        # 自动启动
        config = _global_unified_manager.config
        if config.auto_start:
            _global_unified_manager.start()
    
    return _global_unified_manager


def shutdown_global_unified_manager() -> None:
    """关闭全局统一指标管理器"""
    global _global_unified_manager
    if _global_unified_manager is not None:
        _global_unified_manager.shutdown()
        _global_unified_manager = None
