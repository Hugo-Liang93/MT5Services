"""
增量计算基类
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
import time
import logging

# 使用类型提示，避免直接导入
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.clients.mt5_market import OHLC

# 定义简化的OHLC类用于测试
class SimpleOHLC:
    """简化的OHLC类，避免依赖MT5"""
    def __init__(self, time, open, high, low, close, tick_volume=0, spread=0, real_volume=0):
        self.time = time
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.tick_volume = tick_volume
        self.spread = spread
        self.real_volume = real_volume

# 在非类型检查时使用简化类
OHLC = SimpleOHLC

logger = logging.getLogger(__name__)


@dataclass
class IndicatorState:
    """
    指标计算状态
    
    用于支持增量计算，保存中间计算结果
    """
    
    value: Any  # 当前指标值
    timestamp: float  # 状态更新时间戳
    data_points: int  # 使用的数据点数量
    metadata: Dict[str, Any]  # 额外元数据
    last_bar_time: Optional[float] = None  # 最后处理的K线时间
    last_values: Optional[List[float]] = None  # 最近的值序列（用于增量计算）
    intermediate_results: Optional[Dict[str, Any]] = None  # 中间计算结果
    
    def is_valid(self, current_time: float, max_age: float = 3600) -> bool:
        """
        检查状态是否有效
        
        Args:
            current_time: 当前时间戳
            max_age: 最大有效年龄（秒）
            
        Returns:
            是否有效
        """
        return current_time - self.timestamp <= max_age
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "value": self.value,
            "timestamp": self.timestamp,
            "data_points": self.data_points,
            "metadata": self.metadata,
            "last_bar_time": self.last_bar_time,
            "has_last_values": self.last_values is not None,
            "has_intermediate_results": self.intermediate_results is not None
        }


class IncrementalIndicator(ABC):
    """
    支持增量计算的指标基类
    
    特性：
    1. 支持完整计算和增量计算两种模式
    2. 状态管理：保存中间计算结果
    3. 自动检测是否需要完整计算
    4. 性能监控集成
    """
    
    def __init__(self, name: str, params: Dict[str, Any]):
        """
        初始化增量指标
        
        Args:
            name: 指标名称
            params: 指标参数
        """
        self.name = name
        self.params = params
        self.state_store: Dict[str, IndicatorState] = {}
        self.supports_incremental = True
        self.min_data_points = params.get("min_bars", 20)  # 最小数据点要求
        
        logger.debug(f"IncrementalIndicator initialized: {name}, params={params}")
    
    def get_state_key(self, symbol: str, timeframe: str, scope: str = "confirmed") -> str:
        """
        生成状态键

        scope 区分 confirmed（bar 收盘）与 intrabar（盘中轮询），使两条路径
        各自维护独立的 last_bar_time，避免 intrabar 提前更新时间戳导致
        confirmed 路径永远无法命中增量分支。
        """
        params_hash = hash(str(sorted(self.params.items())))
        return f"{symbol}_{timeframe}_{self.name}_{params_hash}_{scope}"

    def compute(
        self,
        bars: List[OHLC],
        symbol: str,
        timeframe: str,
        use_incremental: bool = True,
        force_full: bool = False,
        scope: str = "confirmed",
    ) -> Dict[str, float]:
        """
        计算指标
        
        Args:
            bars: K线数据列表
            symbol: 交易品种
            timeframe: 时间框架
            use_incremental: 是否尝试增量计算
            force_full: 是否强制完整计算
            
        Returns:
            指标计算结果
        """
        start_time = time.time()
        
        try:
            # 检查数据是否足够
            if len(bars) < self.min_data_points:
                logger.warning(
                    f"Insufficient data for {self.name}: "
                    f"got {len(bars)}, need {self.min_data_points}"
                )
                return {}
            
            # 获取当前状态
            state_key = self.get_state_key(symbol, timeframe, scope)
            current_state = self.state_store.get(state_key)
            
            # 决定计算模式
            should_use_incremental = (
                use_incremental and
                self.supports_incremental and
                not force_full and
                current_state is not None and
                current_state.is_valid(time.time()) and
                self._can_use_incremental(bars, current_state)
            )
            
            if should_use_incremental:
                # 增量计算
                result = self._compute_incremental(bars, current_state)
                incremental = True
                cache_hit = False  # 增量计算不算缓存命中
                logger.debug(f"Incremental computation: {self.name} for {symbol}/{timeframe}")
            else:
                # 完整计算
                result = self._compute_full(bars)
                incremental = False
                cache_hit = False
                logger.debug(f"Full computation: {self.name} for {symbol}/{timeframe}")
            
            # 更新状态
            if result:
                new_state = self._create_new_state(bars, result)
                self.state_store[state_key] = new_state
            
            # 计算耗时
            compute_time = time.time() - start_time
            
            # 记录性能指标（通过监控模块）
            try:
                from ..monitoring.metrics_collector import record_indicator_computation
                record_indicator_computation(
                    name=self.name,
                    compute_time=compute_time,
                    cache_hit=cache_hit,
                    data_points=len(bars),
                    success=True,
                    symbol=symbol,
                    timeframe=timeframe,
                    incremental=incremental
                )
            except ImportError:
                # 监控模块可能还未导入，记录简单日志
                logger.debug(
                    f"Indicator {self.name}: "
                    f"time={compute_time*1000:.2f}ms, "
                    f"incremental={incremental}, "
                    f"data_points={len(bars)}"
                )
            
            return result
            
        except Exception as e:
            # 计算失败
            compute_time = time.time() - start_time
            error_msg = str(e)
            
            logger.error(f"Indicator computation failed: {self.name}, error: {error_msg}")
            
            # 记录错误指标
            try:
                from ..monitoring.metrics_collector import record_indicator_computation
                record_indicator_computation(
                    name=self.name,
                    compute_time=compute_time,
                    cache_hit=False,
                    data_points=len(bars),
                    success=False,
                    error_msg=error_msg,
                    symbol=symbol,
                    timeframe=timeframe,
                    incremental=False
                )
            except ImportError:
                pass
            
            # 返回空结果，不抛出异常
            return {}
    
    def _can_use_incremental(self, bars: List[OHLC], state: IndicatorState) -> bool:
        """
        检查是否可以使用增量计算
        
        Args:
            bars: 新的K线数据
            state: 当前状态
            
        Returns:
            是否可以使用增量计算
        """
        if not bars:
            return False
        
        # 检查是否有最后处理的K线时间
        if state.last_bar_time is None:
            return False
        
        # 获取最新K线时间
        latest_bar = bars[-1]
        latest_time = latest_bar.time.timestamp()
        
        # 如果最新K线时间早于或等于最后处理时间，说明没有新数据
        if latest_time <= state.last_bar_time:
            logger.debug(f"No new data for incremental computation: {self.name}")
            return False
        
        # 检查数据连续性（这里可以扩展更复杂的检查）
        # 例如：检查时间间隔是否一致，是否有缺失数据等
        
        return True
    
    @abstractmethod
    def _compute_full(self, bars: List[OHLC]) -> Dict[str, float]:
        """
        完整计算（子类必须实现）
        
        Args:
            bars: K线数据列表
            
        Returns:
            指标计算结果
        """
        pass
    
    def _compute_incremental(self, bars: List[OHLC], state: IndicatorState) -> Dict[str, float]:
        """
        增量计算（子类可以重写）
        
        默认实现回退到完整计算
        
        Args:
            bars: 新的K线数据
            state: 当前状态
            
        Returns:
            指标计算结果
        """
        logger.debug(f"Falling back to full computation for incremental: {self.name}")
        return self._compute_full(bars)
    
    def _create_new_state(self, bars: List[OHLC], result: Dict[str, float]) -> IndicatorState:
        """
        创建新的状态
        
        Args:
            bars: 使用的K线数据
            result: 计算结果
            
        Returns:
            新的状态对象
        """
        if not bars:
            latest_time = None
        else:
            latest_time = bars[-1].time.timestamp()
        
        # 提取主要值
        main_value = None
        if result:
            # 取第一个值作为主要值
            main_value = next(iter(result.values()))
        
        return IndicatorState(
            value=main_value,
            timestamp=time.time(),
            data_points=len(bars),
            metadata={
                "indicator": self.name,
                "params": self.params,
                "result_keys": list(result.keys())
            },
            last_bar_time=latest_time
        )
    
    def get_state(self, symbol: str, timeframe: str, scope: str = "confirmed") -> Optional[IndicatorState]:
        """
        获取计算状态

        Args:
            symbol: 交易品种
            timeframe: 时间框架
            scope: "confirmed" 或 "intrabar"

        Returns:
            状态对象，如果不存在则返回None
        """
        state_key = self.get_state_key(symbol, timeframe, scope)
        return self.state_store.get(state_key)

    def update_state(self, symbol: str, timeframe: str, state: IndicatorState, scope: str = "confirmed") -> None:
        """
        更新计算状态

        Args:
            symbol: 交易品种
            timeframe: 时间框架
            state: 新的状态对象
            scope: "confirmed" 或 "intrabar"
        """
        state_key = self.get_state_key(symbol, timeframe, scope)
        self.state_store[state_key] = state
        logger.debug(f"State updated: {self.name} for {symbol}/{timeframe}")
    
    def clear_state(self, symbol: Optional[str] = None, timeframe: Optional[str] = None) -> int:
        """
        清除状态
        
        Args:
            symbol: 交易品种（可选）
            timeframe: 时间框架（可选）
            
        Returns:
            清除的状态数量
        """
        if symbol is None and timeframe is None:
            # 清除所有状态
            count = len(self.state_store)
            self.state_store.clear()
            logger.info(f"Cleared all states for {self.name}: {count} states")
            return count
        
        # 清除特定品种/时间框架的状态
        keys_to_remove = []
        
        for key in self.state_store.keys():
            key_parts = key.split('_')
            if len(key_parts) >= 4:
                key_symbol = key_parts[0]
                key_timeframe = key_parts[1]
                
                if (symbol is None or key_symbol == symbol) and \
                   (timeframe is None or key_timeframe == timeframe):
                    keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del self.state_store[key]
        
        logger.info(
            f"Cleared states for {self.name}: "
            f"symbol={symbol or 'all'}, "
            f"timeframe={timeframe or 'all'}, "
            f"count={len(keys_to_remove)}"
        )
        
        return len(keys_to_remove)
    
    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有状态
        
        Returns:
            状态字典
        """
        return {
            key: state.to_dict()
            for key, state in self.state_store.items()
        }
    
    def get_info(self) -> Dict[str, Any]:
        """
        获取指标信息
        
        Returns:
            指标信息字典
        """
        return {
            "name": self.name,
            "params": self.params,
            "supports_incremental": self.supports_incremental,
            "min_data_points": self.min_data_points,
            "state_count": len(self.state_store)
        }


class SimpleIndicator(IncrementalIndicator):
    """
    简单指标基类（不支持增量计算）
    
    用于那些不适合或不需要增量计算的指标
    """
    
    def __init__(self, name: str, params: Dict[str, Any]):
        super().__init__(name, params)
        self.supports_incremental = False
    
    def _can_use_incremental(self, bars: List[OHLC], state: IndicatorState) -> bool:
        """简单指标不支持增量计算"""
        return False
    
    def _compute_incremental(self, bars: List[OHLC], state: IndicatorState) -> Dict[str, float]:
        """简单指标不支持增量计算"""
        raise NotImplementedError(f"{self.name} does not support incremental computation")
