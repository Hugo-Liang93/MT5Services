"""IntrabarTradeGuard: bar 内去重 + confirmed 协调。

职责：
  1. traded_this_bar: 同一父 TF bar 内，同一策略同一方向只允许入场一次。
  2. confirmed 协调: 父 TF bar 收盘时查询是否有 intrabar 仓位，
     协助 TradeExecutor 决定跳过/平仓/正常流程。

线程安全（RLock，因为 intrabar 和 confirmed 路径可能并发访问）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from ..reasons import REASON_TRADED_THIS_BAR

logger = logging.getLogger(__name__)


class IntrabarTradeGuard:
    """Intrabar 交易的去重保护和 confirmed 协调。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # key = (symbol, parent_tf, strategy, direction, parent_bar_time)
        self._traded: set[tuple[str, str, str, str, datetime]] = set()
        # key = (symbol, parent_tf, strategy, parent_bar_time) → direction
        self._positions: dict[tuple[str, str, str, datetime], str] = {}
        # 统计
        self._trades_recorded: int = 0
        self._blocks_traded_this_bar: int = 0
        self._confirmed_validations: int = 0

    def can_trade(
        self,
        symbol: str,
        parent_tf: str,
        strategy: str,
        direction: str,
        parent_bar_time: datetime,
    ) -> tuple[bool, str]:
        """检查是否允许 intrabar 交易。返回 (allowed, reason)。"""
        with self._lock:
            key = (symbol, parent_tf, strategy, direction, parent_bar_time)
            if key in self._traded:
                self._blocks_traded_this_bar += 1
                return False, REASON_TRADED_THIS_BAR
        return True, ""

    def record_trade(
        self,
        symbol: str,
        parent_tf: str,
        strategy: str,
        direction: str,
        parent_bar_time: datetime,
    ) -> None:
        """交易执行后记录。"""
        with self._lock:
            trade_key = (symbol, parent_tf, strategy, direction, parent_bar_time)
            self._traded.add(trade_key)
            pos_key = (symbol, parent_tf, strategy, parent_bar_time)
            self._positions[pos_key] = direction
            self._trades_recorded += 1
            logger.info(
                "IntrabarTradeGuard: recorded trade %s/%s/%s %s (bar=%s)",
                symbol,
                parent_tf,
                strategy,
                direction,
                parent_bar_time.isoformat() if parent_bar_time else "?",
            )

    def has_intrabar_position(
        self,
        symbol: str,
        parent_tf: str,
        strategy: str,
        parent_bar_time: datetime | None,
    ) -> tuple[bool, str | None]:
        """confirmed 链路查询：是否有 intrabar 仓位。

        Returns:
            (exists, direction) — direction 为 "buy"/"sell" 或 None。
        """
        if parent_bar_time is None:
            return False, None
        with self._lock:
            pos_key = (symbol, parent_tf, strategy, parent_bar_time)
            direction = self._positions.get(pos_key)
            if direction is not None:
                self._confirmed_validations += 1
                return True, direction
        return False, None

    def on_parent_bar_close(
        self,
        symbol: str,
        parent_tf: str,
        bar_time: datetime,
    ) -> None:
        """清理已过期 bar 的状态。"""
        with self._lock:
            self._traded = {
                key
                for key in self._traded
                if not (key[0] == symbol and key[1] == parent_tf and key[4] == bar_time)
            }
            to_remove_pos = [
                key
                for key in self._positions
                if key[0] == symbol and key[1] == parent_tf and key[3] == bar_time
            ]
            for key in to_remove_pos:
                del self._positions[key]

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "trades_recorded": self._trades_recorded,
                "blocks_traded_this_bar": self._blocks_traded_this_bar,
                "confirmed_validations": self._confirmed_validations,
                "active_traded_keys": len(self._traded),
                "active_positions": len(self._positions),
            }
