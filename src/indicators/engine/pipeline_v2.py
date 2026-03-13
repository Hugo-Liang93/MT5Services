"""
优化计算流水线 v2

集成：
1. 依赖关系管理
2. 并行计算
3. 智能缓存
4. 增量计算
5. 性能监控
"""

from __future__ import annotations

from typing import Dict, List, Any, Optional, Callable, Set, Tuple
import time
import logging
from dataclasses import dataclass

# 使用绝对导入避免相对导入问题
from src.indicators_unified.engine.dependency_manager import DependencyManager, get_global_dependency_manager
from src.indicators_unified.engine.parallel_executor import ParallelExecutor, get_global_executor
from src.indicators_unified.cache.smart_cache import SmartCache, get_global_cache
from src.indicators_unified.cache.incremental import IncrementalIndicator, IndicatorState
from src.indicators_unified.monitoring.metrics_collector import record_indicator_computation

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    """流水线配置"""
    enable_parallel: bool = True
    max_workers: int = 4
    enable_cache: bool = True
    cache_ttl: float = 300.0
    enable_incremental: bool = True
    max_retries: int = 2
    retry_delay: float = 0.1
    enable_monitoring: bool = True


@dataclass
class ComputationContext:
    """计算上下文"""
    symbol: str
    timeframe: str
    bars: List[Any]
    config: PipelineConfig
    results: Dict[str, Any]
    dependencies: Dict[str, Set[str]]
    start_time: float


class OptimizedPipeline:
    """
    优化计算流水线
    
    集成所有优化功能：
    1. 依赖关系管理
    2. 并行计算
    3. 智能缓存
    4. 增量计算
    5. 性能监控
    """
    
    def __init__(self, config: Optional[PipelineConfig] = None):
        """
        初始化优化流水线
        
        Args:
            config: 流水线配置
        """
        self.config = config or PipelineConfig()
        
        # 初始化组件
        self.dependency_manager = get_global_dependency_manager()
        self.cache = get_global_cache(maxsize=1000, ttl=self.config.cache_ttl)
        
        # 并行执行器（按需创建）
        self._executor: Optional[ParallelExecutor] = None
        
        # 增量计算指标注册表
        self.incremental_indicators: Dict[str, IncrementalIndicator] = {}
        
        # 性能统计
        self.computation_stats = {
            "total_computations": 0,
            "parallel_computations": 0,
            "cached_computations": 0,
            "incremental_computations": 0,
            "failed_computations": 0
        }
        
        logger.info(f"OptimizedPipeline initialized with config: {self.config}")
    
    @property
    def executor(self) -> ParallelExecutor:
        """获取并行执行器（懒加载）"""
        if self._executor is None and self.config.enable_parallel:
            self._executor = get_global_executor(
                max_workers=self.config.max_workers,
                max_retries=self.config.max_retries,
                retry_delay=self.config.retry_delay,
                enable_cache=self.config.enable_cache,
                cache_ttl=self.config.cache_ttl
            )
        return self._executor
    
    def register_indicator(
        self,
        name: str,
        func: Callable,
        params: Dict[str, Any],
        dependencies: Optional[List[str]] = None,
        incremental_class: Optional[type] = None
    ) -> None:
        """
        注册指标
        
        Args:
            name: 指标名称
            func: 指标计算函数
            params: 指标参数
            dependencies: 依赖的指标列表
            incremental_class: 增量计算类（如果支持）
        """
        # 注册到依赖管理器
        self.dependency_manager.add_indicator(name, func, params, dependencies)
        
        # 如果支持增量计算，创建增量计算实例
        if incremental_class is not None and issubclass(incremental_class, IncrementalIndicator):
            incremental_instance = incremental_class(name, params)
            self.incremental_indicators[name] = incremental_instance
            logger.debug(f"Incremental indicator registered: {name}")
        
        logger.info(f"Indicator registered: {name} with {len(dependencies or [])} dependencies")
    
    def _generate_cache_key(
        self,
        indicator: str,
        symbol: str,
        timeframe: str,
        bars_hash: int
    ) -> str:
        """
        生成缓存键
        
        Args:
            indicator: 指标名称
            symbol: 交易品种
            timeframe: 时间框架
            bars_hash: K线数据哈希
            
        Returns:
            缓存键
        """
        return f"{indicator}_{symbol}_{timeframe}_{bars_hash}"
    
    def _compute_indicator(
        self,
        indicator: str,
        context: ComputationContext
    ) -> Any:
        """
        计算单个指标
        
        Args:
            indicator: 指标名称
            context: 计算上下文
            
        Returns:
            计算结果
        """
        start_time = time.time()
        cache_hit = False
        incremental = False
        success = True
        error_msg = None
        
        try:
            # 生成缓存键
            bars_hash = hash(str(context.bars))
            cache_key = self._generate_cache_key(
                indicator, context.symbol, context.timeframe, bars_hash
            )
            
            # 检查缓存
            if self.config.enable_cache:
                cached_result = self.cache.get(cache_key)
                if cached_result is not None:
                    cache_hit = True
                    result = cached_result
                    logger.debug(f"Cache hit for {indicator}")
                else:
                    cache_hit = False
                    
                    # 获取指标函数
                    func = self.dependency_manager.indicator_funcs.get(indicator)
                    if func is None:
                        raise ValueError(f"Indicator function not found: {indicator}")
                    
                    # 检查是否支持增量计算
                    if (self.config.enable_incremental and 
                        indicator in self.incremental_indicators):
                        
                        # 使用增量计算
                        incremental_indicator = self.incremental_indicators[indicator]
                        result = incremental_indicator.compute(
                            context.bars,
                            context.symbol,
                            context.timeframe,
                            use_incremental=True
                        )
                        incremental = True
                        self.computation_stats["incremental_computations"] += 1
                        
                    else:
                        # 使用普通计算
                        # 准备依赖结果
                        deps = context.dependencies.get(indicator, set())
                        dep_results = {}
                        for dep in deps:
                            if dep in context.results:
                                dep_results[dep] = context.results[dep]
                        
                        # 计算指标
                        result = func(context.bars, **dep_results)
                    
                    # 缓存结果
                    if self.config.enable_cache and result is not None:
                        self.cache.set(cache_key, result)
            
            # 更新统计
            compute_time = time.time() - start_time
            
            if cache_hit:
                self.computation_stats["cached_computations"] += 1
            
            # 记录性能指标
            if self.config.enable_monitoring:
                record_indicator_computation(
                    name=indicator,
                    compute_time=compute_time,
                    cache_hit=cache_hit,
                    data_points=len(context.bars),
                    success=success,
                    error_msg=error_msg,
                    symbol=context.symbol,
                    timeframe=context.timeframe,
                    incremental=incremental
                )
            
            return result
            
        except Exception as e:
            # 计算失败
            compute_time = time.time() - start_time
            error_msg = str(e)
            success = False
            
            self.computation_stats["failed_computations"] += 1
            
            logger.error(f"Indicator computation failed: {indicator}, error: {error_msg}")
            
            # 记录错误指标
            if self.config.enable_monitoring:
                record_indicator_computation(
                    name=indicator,
                    compute_time=compute_time,
                    cache_hit=cache_hit,
                    data_points=len(context.bars),
                    success=success,
                    error_msg=error_msg,
                    symbol=context.symbol,
                    timeframe=context.timeframe,
                    incremental=incremental
                )
            
            # 返回None表示失败
            return None
    
    def _compute_parallel_group(
        self,
        indicators: List[str],
        context: ComputationContext
    ) -> Dict[str, Any]:
        """
        并行计算一组指标
        
        Args:
            indicators: 指标列表
            context: 计算上下文
            
        Returns:
            计算结果字典
        """
        if not self.config.enable_parallel or len(indicators) <= 1:
            # 串行计算
            results = {}
            for indicator in indicators:
                results[indicator] = self._compute_indicator(indicator, context)
            return results
        
        # 并行计算
        tasks = []
        task_indicator_map = {}
        
        for indicator in indicators:
            # 为每个指标创建任务
            task = (
                self._compute_indicator,  # 函数
                (indicator, context),     # 参数
                {}                        # 关键字参数
            )
            tasks.append(task)
            
            # 生成任务ID
            bars_hash = hash(str(context.bars))
            task_id = f"{indicator}_{context.symbol}_{context.timeframe}_{bars_hash}"
            task_indicator_map[task_id] = indicator
        
        # 执行并行任务
        parallel_results = self.executor.execute_parallel(
            tasks,
            use_cache=self.config.enable_cache
        )
        
        # 提取结果
        results = {}
        for task_id, task_result in parallel_results.items():
            if task_result and task_result.success:
                indicator = task_indicator_map.get(task_id)
                if indicator:
                    results[indicator] = task_result.result
        
        self.computation_stats["parallel_computations"] += len(indicators)
        
        return results
    
    def compute(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        indicators: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        计算指标
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            bars: K线数据
            indicators: 需要计算的指标列表，如果为None则计算所有
            
        Returns:
            计算结果字典
        """
        start_time = time.time()
        
        try:
            # 确定要计算的指标
            if indicators is None:
                indicators = list(self.dependency_manager.indicator_funcs.keys())
            
            # 获取执行顺序（按依赖层级分组）
            execution_groups = self.dependency_manager.get_parallelizable_groups(indicators)
            
            # 创建计算上下文
            context = ComputationContext(
                symbol=symbol,
                timeframe=timeframe,
                bars=bars,
                config=self.config,
                results={},
                dependencies={ind: self.dependency_manager.get_dependencies(ind) 
                            for ind in indicators},
                start_time=start_time
            )
            
            # 按层级计算
            for level_indicators in execution_groups:
                # 计算当前层级
                level_results = self._compute_parallel_group(level_indicators, context)
                
                # 更新结果
                context.results.update(level_results)
                
                # 更新统计
                self.computation_stats["total_computations"] += len(level_indicators)
            
            # 计算总耗时
            total_time = time.time() - start_time
            
            logger.info(
                f"Pipeline computation completed: "
                f"{len(indicators)} indicators, "
                f"{len(execution_groups)} levels, "
                f"{total_time*1000:.2f}ms"
            )
            
            return context.results
            
        except Exception as e:
            logger.error(f"Pipeline computation failed: {e}")
            
            # 返回空结果
            return {}
    
    def compute_single(
        self,
        indicator: str,
        symbol: str,
        timeframe: str,
        bars: List[Any]
    ) -> Any:
        """
        计算单个指标
        
        Args:
            indicator: 指标名称
            symbol: 交易品种
            timeframe: 时间框架
            bars: K线数据
            
        Returns:
            计算结果
        """
        return self.compute(symbol, timeframe, bars, [indicator]).get(indicator)
    
    def get_execution_plan(
        self,
        indicators: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        获取执行计划
        
        Args:
            indicators: 需要计算的指标列表
            
        Returns:
            执行计划信息
        """
        if indicators is None:
            indicators = list(self.dependency_manager.indicator_funcs.keys())
        
        execution_groups = self.dependency_manager.get_parallelizable_groups(indicators)
        
        plan = {
            "total_indicators": len(indicators),
            "levels": len(execution_groups),
            "execution_groups": execution_groups,
            "dependency_graph": self.dependency_manager.visualize("mermaid"),
            "indicators": []
        }
        
        for indicator in indicators:
            info = self.dependency_manager.get_indicator_info(indicator)
            if info:
                plan["indicators"].append(info)
        
        return plan
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计信息字典
        """
        stats = self.computation_stats.copy()
        
        # 添加缓存统计
        cache_stats = self.cache.get_stats()
        stats["cache"] = cache_stats
        
        # 添加并行执行器统计
        if self._executor is not None:
            executor_stats = self._executor.get_stats()
            stats["executor"] = executor_stats
        
        # 计算成功率
        total = stats["total_computations"]
        if total > 0:
            failed = stats["failed_computations"]
            stats["success_rate"] = (total - failed) / total * 100
        else:
            stats["success_rate"] = 0
        
        # 计算优化效果
        if total > 0:
            cached = stats["cached_computations"]
            incremental = stats["incremental_computations"]
            parallel = stats["parallel_computations"]
            
            stats["cache_optimization_rate"] = cached / total * 100
            stats["incremental_optimization_rate"] = incremental / total * 100
            stats["parallel_optimization_rate"] = parallel / total * 100
        
        return stats
    
    def clear_cache(self) -> int:
        """
        清空缓存
        
        Returns:
            清除的缓存项数量
        """
        return self.cache.clear()
    
    def clear_state(self, symbol: Optional[str] = None, timeframe: Optional[str] = None) -> int:
        """
        清除增量计算状态
        
        Args:
            symbol: 交易品种
            timeframe: 时间框架
            
        Returns:
            清除的状态数量
        """
        total = 0
        for indicator in self.incremental_indicators.values():
            count = indicator.clear_state(symbol, timeframe)
            total += count
        
        logger.info(f"Cleared {total} incremental states")
        return total
    
    def shutdown(self) -> None:
        """关闭流水线"""
        if self._executor is not None:
            self._executor.shutdown()
            self._executor = None
        
        logger.info("OptimizedPipeline shutdown")


# 全局优化流水线实例
_global_pipeline: Optional[OptimizedPipeline] = None


def get_global_pipeline(config: Optional[PipelineConfig] = None) -> OptimizedPipeline:
    """
    获取全局优化流水线实例（单例模式）
    
    Args:
        config: 流水线配置
        
    Returns:
        全局优化流水线实例
    """
    global _global_pipeline
    if _global_pipeline is None:
        _global_pipeline = OptimizedPipeline(config)
    return _global_pipeline


def shutdown_global_pipeline() -> None:
    """关闭全局优化流水线"""
    global _global_pipeline
    if _global_pipeline is not None:
        _global_pipeline.shutdown()
        _global_pipeline = None