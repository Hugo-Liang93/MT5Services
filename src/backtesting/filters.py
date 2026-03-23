"""回测过滤器模拟：在历史回放中复现实盘 SignalFilterChain 的行为。

核心差异：
- 实盘用 datetime.now(UTC)，回测用 bar.time（历史时间）
- Spread 使用可配置的模拟值（历史 tick spread 数据可选注入）
- 经济事件过滤可选（需要历史日历数据）
- VolatilitySpikeFilter 直接复用（基于当前 bar 指标）

设计原则：
- 复用实盘 filter 的相同判断逻辑（不重新实现）
- 仅适配时间来源（bar.time 替代 now()）
- 过滤统计独立收集，不影响主循环性能
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.signals.contracts import normalize_session_name
from src.signals.execution.filters import (
    SessionFilter,
    SessionTransitionFilter,
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
            (self.total_bars_evaluated - self.total_bars_rejected)
            / self.total_bars_evaluated
        )

    def record_rejection(
        self, bar_time: datetime, filter_name: str, reason: str
    ) -> None:
        self.total_bars_rejected += 1
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

    def record_pass(self) -> None:
        pass  # total_bars_evaluated 在外部递增

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
    """回测过滤器配置。"""

    # 总开关
    enabled: bool = True
    # 时段过滤
    session_filter_enabled: bool = True
    allowed_sessions: str = "london,newyork"
    # 时段切换冷却
    session_transition_enabled: bool = True
    session_transition_cooldown_minutes: int = 15
    # 波动率异常
    volatility_filter_enabled: bool = True
    volatility_spike_multiplier: float = 2.5
    # 点差过滤（回测中使用模拟点差）
    spread_filter_enabled: bool = False
    max_spread_points: float = 50.0
    session_spread_limits: Dict[str, float] = field(default_factory=dict)
    # 模拟点差值（按时段，用于没有真实 spread 数据时）
    simulated_spread_by_session: Dict[str, float] = field(
        default_factory=lambda: {
            "asia": 30.0,
            "london": 25.0,
            "newyork": 20.0,
            "off_hours": 45.0,
        }
    )


class BacktestFilterSimulator:
    """回测过滤器模拟器。

    复用实盘 SignalFilterChain 的各个子过滤器，
    但使用历史 bar.time 替代 datetime.now() 进行判断。

    用法：
        simulator = BacktestFilterSimulator(config)
        for bar in bars:
            allowed, reason = simulator.should_evaluate(
                symbol, bar.time, indicators
            )
            if not allowed:
                continue  # 跳过此 bar 的策略评估
    """

    def __init__(self, config: BacktestFilterConfig) -> None:
        self._config = config
        self._stats = BacktestFilterStats()

        # 构建子过滤器（复用实盘组件）
        self._session_filter: Optional[SessionFilter] = None
        self._session_transition_filter: Optional[SessionTransitionFilter] = None
        self._volatility_filter: Optional[VolatilitySpikeFilter] = None
        self._spread_filter: Optional[SpreadFilter] = None

        if not config.enabled:
            return

        if config.session_filter_enabled:
            sessions = tuple(
                normalize_session_name(s.strip())
                for s in config.allowed_sessions.split(",")
                if s.strip()
            )
            self._session_filter = SessionFilter(allowed_sessions=sessions)

        if config.session_transition_enabled:
            self._session_transition_filter = SessionTransitionFilter(
                cooldown_minutes=config.session_transition_cooldown_minutes,
            )

        if config.volatility_filter_enabled and config.volatility_spike_multiplier > 0:
            self._volatility_filter = VolatilitySpikeFilter(
                spike_multiplier=config.volatility_spike_multiplier,
            )

        if config.spread_filter_enabled:
            self._spread_filter = SpreadFilter(
                max_spread_points=config.max_spread_points,
                session_max_spread_points={
                    normalize_session_name(k): v
                    for k, v in config.session_spread_limits.items()
                },
            )

    @property
    def stats(self) -> BacktestFilterStats:
        return self._stats

    def should_evaluate(
        self,
        symbol: str,
        bar_time: datetime,
        indicators: Optional[Dict[str, Dict[str, Any]]] = None,
        spread_points: Optional[float] = None,
    ) -> tuple[bool, str]:
        """判断在历史 bar_time 时刻是否应评估策略。

        Args:
            symbol: 交易品种
            bar_time: 历史 K 线时间（替代 datetime.now()）
            indicators: 当前 bar 的指标计算结果
            spread_points: 历史点差（可选，无则使用模拟值）

        Returns:
            (allowed, reason) — reason 为空表示通过
        """
        self._stats.total_bars_evaluated += 1

        if not self._config.enabled:
            return True, ""

        # 1. 时段过滤
        if self._session_filter is not None:
            if not self._session_filter.is_active_session(bar_time):
                sessions = self._session_filter.current_sessions(bar_time)
                reason = f"outside_allowed_sessions:{','.join(sessions)}"
                self._stats.record_rejection(bar_time, "session", reason)
                return False, reason

        # 2. 时段切换冷却
        if self._session_transition_filter is not None:
            if not self._session_transition_filter.is_safe(bar_time):
                transition = self._session_transition_filter.active_transition(
                    bar_time
                )
                reason = f"session_transition_cooldown:{transition}"
                self._stats.record_rejection(
                    bar_time, "session_transition", reason
                )
                return False, reason

        # 3. 点差过滤
        if self._spread_filter is not None:
            actual_spread = spread_points
            if actual_spread is None:
                # 使用模拟点差
                actual_spread = self._get_simulated_spread(bar_time)

            sessions = (
                self._session_filter.current_sessions(bar_time)
                if self._session_filter
                else None
            )
            if not self._spread_filter.is_spread_acceptable(
                actual_spread, sessions=sessions
            ):
                threshold = self._spread_filter.threshold_for_sessions(sessions)
                reason = f"spread_too_wide:{actual_spread:.1f}>{threshold:.1f}"
                self._stats.record_rejection(bar_time, "spread", reason)
                return False, reason

        # 4. 波动率异常
        if self._volatility_filter is not None and indicators:
            if not self._volatility_filter.is_volatility_acceptable(indicators):
                reason = "volatility_spike"
                self._stats.record_rejection(bar_time, "volatility", reason)
                return False, reason

        return True, ""

    def _get_simulated_spread(self, bar_time: datetime) -> float:
        """根据 bar 时间推断模拟点差。"""
        if self._session_filter is not None:
            sessions = self._session_filter.current_sessions(bar_time)
            for session in sessions:
                if session in self._config.simulated_spread_by_session:
                    return self._config.simulated_spread_by_session[session]
        return self._config.max_spread_points * 0.5  # 默认返回阈值一半
