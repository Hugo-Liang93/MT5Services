"""IntrabarTradeCoordinator: 用 bar 计数追踪信号稳定性，判定何时可盘中入场。

独立负责 intrabar 交易的稳定性判定——当连续 N 根子 TF bar 同方向且
confidence 达标时，发布 intrabar_armed 信号触发盘中入场。

纯状态操作，无 IO。线程安全（RLock）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class IntrabarTradingPolicy:
    """Intrabar 交易的稳定性判定策略。

    Attributes:
        min_stable_bars: 连续同方向的触发 TF bar 数（默认 3）。
        min_confidence: 盘中入场最低置信度（高于 confirmed 阈值）。
        enabled_strategies: 允许 intrabar 交易的策略白名单。
        strategy_overrides: 按策略覆盖 {strategy: {"min_stable_bars": N, "min_confidence": X}}。
    """

    min_stable_bars: int = 3
    min_confidence: float = 0.75
    enabled_strategies: frozenset[str] = field(default_factory=frozenset)
    strategy_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get_min_stable_bars(self, strategy: str) -> int:
        override = self.strategy_overrides.get(strategy, {})
        return int(override.get("min_stable_bars", self.min_stable_bars))

    def get_min_confidence(self, strategy: str) -> float:
        override = self.strategy_overrides.get(strategy, {})
        return float(override.get("min_confidence", self.min_confidence))


@dataclass
class _StabilityState:
    direction: str = "hold"
    stable_count: int = 0
    parent_bar_time: datetime | None = None
    last_confidence: float = 0.0
    armed_emitted: bool = False


class IntrabarTradeCoordinator:
    """Bar-count 稳定性追踪器。

    每次策略评估后调用 update()，返回 signal_state（"intrabar_armed_buy/sell"）或 None。
    """

    def __init__(self, policy: IntrabarTradingPolicy) -> None:
        self._policy = policy
        # key = (symbol, parent_tf, strategy)
        self._states: dict[tuple[str, str, str], _StabilityState] = {}
        self._lock = threading.RLock()
        # 统计
        self._updates_total: int = 0
        self._armed_total: int = 0

    @property
    def policy(self) -> IntrabarTradingPolicy:
        return self._policy

    def update(
        self,
        symbol: str,
        parent_tf: str,
        strategy: str,
        direction: str,
        confidence: float,
        parent_bar_time: datetime,
    ) -> str | None:
        """每次策略评估后调用。返回 signal_state 或 None。

        - direction 与上次相同 → stable_count += 1
        - direction 改变 → 重置 stable_count = 1
        - parent_bar_time 改变 → 重置（新 bar 开始）
        - stable_count >= min_stable_bars AND confidence >= min_confidence
          AND 策略在白名单中 AND 本 bar 未 emit 过
          → 返回 "intrabar_armed_buy" / "intrabar_armed_sell"
        """
        if strategy not in self._policy.enabled_strategies:
            return None
        if direction not in ("buy", "sell"):
            return None

        self._updates_total += 1
        key = (symbol, parent_tf, strategy)
        min_bars = self._policy.get_min_stable_bars(strategy)
        min_conf = self._policy.get_min_confidence(strategy)

        with self._lock:
            state = self._states.get(key)

            if state is None:
                state = _StabilityState(
                    direction=direction,
                    stable_count=1,
                    parent_bar_time=parent_bar_time,
                    last_confidence=confidence,
                )
                self._states[key] = state
                return None

            # 新 bar 开始 → 重置
            if state.parent_bar_time != parent_bar_time:
                state.direction = direction
                state.stable_count = 1
                state.parent_bar_time = parent_bar_time
                state.last_confidence = confidence
                state.armed_emitted = False
                return None

            # 同 bar，方向相同 → 累加
            if state.direction == direction:
                state.stable_count += 1
                state.last_confidence = confidence
            else:
                # 方向切换 → 重置计数
                state.direction = direction
                state.stable_count = 1
                state.last_confidence = confidence
                state.armed_emitted = False
                return None

            # 判定是否达标
            if (
                state.stable_count >= min_bars
                and confidence >= min_conf
                and not state.armed_emitted
            ):
                state.armed_emitted = True
                self._armed_total += 1
                signal_state = f"intrabar_armed_{direction}"
                logger.info(
                    "IntrabarTradeCoordinator: %s/%s/%s → %s "
                    "(stable_bars=%d, confidence=%.3f)",
                    symbol,
                    parent_tf,
                    strategy,
                    signal_state,
                    state.stable_count,
                    confidence,
                )
                return signal_state

        return None

    def on_parent_bar_close(
        self,
        symbol: str,
        parent_tf: str,
        bar_time: datetime,
    ) -> None:
        """父 TF bar 收盘，清理该 bar 的所有稳定性状态。"""
        with self._lock:
            to_remove = [
                key
                for key, state in self._states.items()
                if key[0] == symbol
                and key[1] == parent_tf
                and state.parent_bar_time == bar_time
            ]
            for key in to_remove:
                del self._states[key]

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = {
                f"{k[0]}/{k[1]}/{k[2]}": {
                    "direction": s.direction,
                    "stable_count": s.stable_count,
                    "parent_bar_time": (
                        s.parent_bar_time.isoformat() if s.parent_bar_time else None
                    ),
                    "last_confidence": round(s.last_confidence, 4),
                    "armed_emitted": s.armed_emitted,
                }
                for k, s in self._states.items()
            }
        return {
            "updates_total": self._updates_total,
            "armed_total": self._armed_total,
            "active_states": active,
            "policy": {
                "min_stable_bars": self._policy.min_stable_bars,
                "min_confidence": self._policy.min_confidence,
                "enabled_strategies": sorted(self._policy.enabled_strategies),
            },
        }
