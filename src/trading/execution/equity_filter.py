"""Equity Curve Filter — 权益曲线过滤器。

当账户权益低于 N 周期移动平均线时暂停开仓，防止在持续回撤期继续加仓。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class EquityCurveFilterConfig:
    enabled: bool = False
    ma_period: int = 20
    min_samples: int = 5


class EquityCurveFilter:
    """基于权益 MA 的开仓过滤器。"""

    def __init__(
        self,
        balance_getter: Callable[[], Optional[float]],
        config: Optional[EquityCurveFilterConfig] = None,
    ) -> None:
        self._balance_getter = balance_getter
        self.config = config or EquityCurveFilterConfig()
        self._equity_history: deque[float] = deque(maxlen=max(self.config.ma_period, 5))
        self._lock = threading.Lock()
        self._paused = False

    def record_equity(self) -> None:
        """采样当前权益到历史序列。应在每次交易评估周期调用。"""
        try:
            equity = self._balance_getter()
            if equity is not None and equity > 0:
                with self._lock:
                    self._equity_history.append(equity)
        except Exception:
            pass

    def should_block(self) -> bool:
        """如果权益低于 MA 则返回 True（应阻止开仓）。"""
        if not self.config.enabled:
            return False
        with self._lock:
            history = list(self._equity_history)
        if len(history) < self.config.min_samples:
            return False
        ma = sum(history) / len(history)
        current = history[-1]
        self._paused = current < ma
        return self._paused

    def status(self) -> dict:
        with self._lock:
            history = list(self._equity_history)
        if not history:
            return {
                "enabled": self.config.enabled,
                "paused": False,
                "samples": 0,
                "ma_period": self.config.ma_period,
                "current_equity": None,
                "equity_ma": None,
            }
        ma = sum(history) / len(history) if len(history) >= self.config.min_samples else None
        return {
            "enabled": self.config.enabled,
            "paused": self._paused,
            "samples": len(history),
            "ma_period": self.config.ma_period,
            "current_equity": history[-1],
            "equity_ma": round(ma, 2) if ma is not None else None,
        }
