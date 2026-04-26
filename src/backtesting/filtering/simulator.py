"""回测过滤器：直接复用实盘 SignalFilterChain，仅适配时间来源和统计收集。

核心设计：
- 回测直接使用 SignalFilterChain.should_evaluate()（同一份代码）
- 唯一差异：utc_now 传入 bar.time（历史时间）而非 datetime.now()
- SignalFilterChain 本身已支持 utc_now 参数，无需任何适配
- BacktestFilterSimulator 只负责：
  1. 构建 SignalFilterChain 实例（按回测配置）
  2. 为每次调用注入 bar.time 和模拟点差
  3. 收集过滤统计（实盘不需要这层统计）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.calendar import EconomicDecayService
from src.calendar.policy import EconomicEventWindow, SignalEconomicPolicy
from src.signals.contracts import normalize_session_name
from src.signals.execution.filters import (
    EconomicEventFilter,
    SessionFilter,
    SessionTransitionFilter,
    SignalFilterChain,
    SpreadFilter,
    VolatilitySpikeFilter,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterRejection:
    """单次过滤拒绝记录。"""

    bar_time: datetime
    filter_name: str
    reason: str


@dataclass
class BacktestFilterStats:
    """回测过滤统计。"""

    total_bars_evaluated: int = 0
    total_bars_rejected: int = 0
    rejections_by_filter: Dict[str, int] = field(default_factory=dict)
    rejection_details: List[FilterRejection] = field(default_factory=list)
    # 限制详细记录数量（避免内存爆炸）
    max_detail_records: int = 1000

    @property
    def pass_rate(self) -> float:
        if self.total_bars_evaluated == 0:
            return 0.0
        return (
            self.total_bars_evaluated - self.total_bars_rejected
        ) / self.total_bars_evaluated

    def record_rejection(self, bar_time: datetime, reason: str) -> None:
        self.total_bars_rejected += 1
        # 从 reason 提取过滤器名称（格式：filter_type:details）
        filter_name = reason.split(":")[0] if ":" in reason else reason
        self.rejections_by_filter[filter_name] = (
            self.rejections_by_filter.get(filter_name, 0) + 1
        )
        if len(self.rejection_details) < self.max_detail_records:
            self.rejection_details.append(
                FilterRejection(
                    bar_time=bar_time,
                    filter_name=filter_name,
                    reason=reason,
                )
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_bars_evaluated": self.total_bars_evaluated,
            "total_bars_rejected": self.total_bars_rejected,
            "pass_rate": round(self.pass_rate, 4),
            "rejections_by_filter": dict(self.rejections_by_filter),
            "rejection_sample": [
                {
                    "bar_time": r.bar_time.isoformat(),
                    "filter_name": r.filter_name,
                    "reason": r.reason,
                }
                for r in self.rejection_details[:100]  # API 只返回前 100 条
            ],
        }


@dataclass
class BacktestFilterConfig:
    """回测过滤器配置。

    参数含义与 signal.ini 中的过滤器配置完全一致。
    """

    # 总开关
    enabled: bool = True
    # 时段过滤（对应 signal.ini [signal].allowed_sessions）
    session_filter_enabled: bool = True
    allowed_sessions: str = "london,new_york"
    # 时段切换冷却（对应 signal.ini [signal].session_transition_cooldown_minutes）
    session_transition_enabled: bool = True
    session_transition_cooldown_minutes: int = 15
    # 波动率异常（对应 signal.ini [signal].volatility_atr_spike_multiplier）
    volatility_filter_enabled: bool = True
    volatility_spike_multiplier: float = 2.5
    # 点差过滤（回测中默认禁用，因为没有真实 spread 数据）
    spread_filter_enabled: bool = False
    max_spread_points: float = 50.0
    session_spread_limits: Dict[str, float] = field(default_factory=dict)
    # 经济事件过滤（需要注入 TradeGuardProvider，回测中默认禁用）
    economic_filter_enabled: bool = False
    economic_provider: Optional[Any] = None
    economic_lookahead_minutes: int = 30
    economic_lookback_minutes: int = 15
    economic_importance_min: int = 3
    # 模拟点差值（按时段，用于没有真实 spread 数据时）
    simulated_spread_by_session: Dict[str, float] = field(
        default_factory=lambda: {
            "asia": 30.0,
            "london": 25.0,
            "new_york": 20.0,
            "off_hours": 45.0,
        }
    )


class BacktestFilterSimulator:
    """回测过滤器模拟器。

    **直接复用实盘 SignalFilterChain**——同一份过滤判断代码。
    回测层只负责：
    1. 按 BacktestFilterConfig 构建 SignalFilterChain 实例
    2. 每次调用时注入 bar.time（替代 datetime.now()）和模拟点差
    3. 收集过滤统计（实盘中不需要这层聚合）

    实盘 SignalRuntime._evaluate_strategies() 调用路径：
        self.filter_chain.should_evaluate(symbol, spread_points=..., utc_now=event_time, ...)

    回测调用路径（完全相同的 should_evaluate）：
        self._filter_chain.should_evaluate(symbol, spread_points=..., utc_now=bar.time, ...)
    """

    def __init__(self, config: BacktestFilterConfig) -> None:
        self._config = config
        self._stats = BacktestFilterStats()

        # 直接构建实盘 SignalFilterChain（同一个类）
        self._filter_chain: Optional[SignalFilterChain] = None

        if not config.enabled:
            return

        self._filter_chain = SignalFilterChain(
            session_filter=(
                SessionFilter(
                    allowed_sessions=tuple(
                        normalize_session_name(s.strip())
                        for s in config.allowed_sessions.split(",")
                        if s.strip()
                    )
                )
                if config.session_filter_enabled
                else None
            ),
            session_transition_filter=(
                SessionTransitionFilter(
                    cooldown_minutes=config.session_transition_cooldown_minutes,
                )
                if config.session_transition_enabled
                else None
            ),
            spread_filter=(
                SpreadFilter(
                    max_spread_points=config.max_spread_points,
                    session_max_spread_points={
                        normalize_session_name(k): v
                        for k, v in config.session_spread_limits.items()
                    },
                )
                if config.spread_filter_enabled
                else None
            ),
            economic_filter=(
                EconomicEventFilter(
                    service=EconomicDecayService(
                        provider=config.economic_provider,
                        policy=SignalEconomicPolicy(
                            enabled=True,
                            filter_window=EconomicEventWindow(
                                lookahead_minutes=config.economic_lookahead_minutes,
                                lookback_minutes=config.economic_lookback_minutes,
                                importance_min=config.economic_importance_min,
                            ),
                            query_window=EconomicEventWindow(
                                lookahead_minutes=config.economic_lookahead_minutes,
                                lookback_minutes=config.economic_lookback_minutes,
                                importance_min=config.economic_importance_min,
                            ),
                            hard_block_pre_minutes=config.economic_lookahead_minutes,
                            hard_block_post_minutes=config.economic_lookback_minutes,
                            decay_pre_minutes=0,
                            decay_post_minutes=0,
                        ),
                    ),
                )
                if config.economic_filter_enabled and config.economic_provider
                else None
            ),
            volatility_filter=(
                VolatilitySpikeFilter(
                    spike_multiplier=config.volatility_spike_multiplier,
                )
                if config.volatility_filter_enabled
                and config.volatility_spike_multiplier > 0
                else None
            ),
        )

    @property
    def stats(self) -> BacktestFilterStats:
        return self._stats

    @property
    def filter_chain(self) -> Optional[SignalFilterChain]:
        """暴露内部的实盘 SignalFilterChain 实例（用于测试/调试）。"""
        return self._filter_chain

    def should_evaluate(
        self,
        symbol: str,
        bar_time: datetime,
        indicators: Optional[Dict[str, Dict[str, Any]]] = None,
        spread_points: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """判断在历史 bar_time 时刻是否应评估策略。

        内部直接调用 SignalFilterChain.should_evaluate()，
        传入 utc_now=bar_time 实现历史时间注入。

        Args:
            symbol: 交易品种
            bar_time: 历史 K 线时间（注入到 SignalFilterChain 的 utc_now 参数）
            indicators: 当前 bar 的指标计算结果（用于波动率过滤）
            spread_points: 历史点差（可选，无则使用模拟值）

        Returns:
            (allowed, reason) — reason 为空表示通过
        """
        self._stats.total_bars_evaluated += 1

        if self._filter_chain is None:
            return True, ""

        # 解析模拟点差
        actual_spread = spread_points
        if actual_spread is None and self._config.spread_filter_enabled:
            actual_spread = self._get_simulated_spread(bar_time)

        # 解析当前 session（与实盘 SignalRuntime 相同的逻辑）
        active_sessions = None
        if self._filter_chain.session_filter is not None:
            active_sessions = self._filter_chain.session_filter.current_sessions(
                bar_time
            )

        # 直接调用实盘 SignalFilterChain.should_evaluate()
        allowed, reason = self._filter_chain.should_evaluate(
            symbol,
            spread_points=actual_spread or 0.0,
            utc_now=bar_time,
            active_sessions=active_sessions,
            indicators=indicators,
        )

        if not allowed:
            self._stats.record_rejection(bar_time, reason)

        return allowed, reason

    def _get_simulated_spread(self, bar_time: datetime) -> float:
        """根据 bar 时间推断模拟点差。"""
        if self._filter_chain and self._filter_chain.session_filter:
            sessions = self._filter_chain.session_filter.current_sessions(bar_time)
            for session in sessions:
                if session in self._config.simulated_spread_by_session:
                    return self._config.simulated_spread_by_session[session]
        return self._config.max_spread_points * 0.5
