"""回测用经济日历 TradeGuard 提供者。

按 bar 时间查询风险窗口，复用实盘 trade_guard 的分级逻辑。
事件加载器已迁移至 src.calendar.economic_loader（P4 解耦，2026-04-22）——
本模块只保留 backtesting 专属的 TradeGuard 实现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.calendar.economic_loader import SimpleEvent

logger = logging.getLogger(__name__)


@dataclass
class _SimpleSettings:
    """回测用的简化 settings，替代 EconomicCalendarService.settings。"""

    pre_event_buffer_minutes: int = 30
    post_event_buffer_minutes: int = 30
    trade_guard_block_importance_min: int = 3
    trade_guard_warn_pre_buffer_minutes: int = 15
    trade_guard_warn_post_buffer_minutes: int = 10
    trade_guard_relevance_filter_enabled: bool = False
    gold_impact_keywords: str = ""
    gold_impact_categories: str = ""
    high_importance_threshold: int = 3
    # MarketImpact 相关（回测中不使用，保持默认）
    market_impact_high_spike_threshold: float = 3.0
    market_impact_med_spike_threshold: float = 2.0
    market_impact_high_spike_buffer_minutes: int = 60
    market_impact_med_spike_buffer_minutes: int = 45


class BacktestTradeGuardProvider:
    """回测用 TradeGuard 提供者。

    在回测开始时从 DB 预加载回测期间（±buffer）的所有经济事件，
    然后每个 bar 评估时从内存中过滤。

    实现 TradeGuardProvider Protocol（src/signals/execution/filters.py:32）。
    """

    def __init__(
        self,
        events: List[SimpleEvent],
        settings: Optional[_SimpleSettings] = None,
    ) -> None:
        self._events = sorted(events, key=lambda e: e.scheduled_at)
        self.settings = settings or _SimpleSettings()
        self.market_impact_analyzer = None  # 回测不使用 MarketImpact
        logger.info(
            "BacktestTradeGuardProvider: loaded %d economic events", len(events)
        )

    def get_events(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
        **kwargs: Any,
    ) -> List[SimpleEvent]:
        """内存过滤，模拟 EconomicCalendarService.get_events()。"""
        result = []
        for ev in self._events:
            if start_time and ev.scheduled_at < start_time:
                continue
            if end_time and ev.scheduled_at > end_time:
                continue
            if countries and ev.country not in countries:
                continue
            if currencies and ev.currency not in currencies:
                continue
            if statuses and ev.status not in statuses:
                continue
            if importance_min is not None and (ev.importance or 0) < importance_min:
                continue
            result.append(ev)
            if len(result) >= limit:
                break
        return result

    def get_trade_guard(self, **kwargs: Any) -> Dict[str, Any]:
        """TradeGuardProvider Protocol 实现。"""
        from src.calendar import get_trade_guard

        return get_trade_guard(self, **kwargs)

    def _ensure_worker_running(self) -> None:
        """兼容 trade_guard.get_trade_guard() 的调用。回测不需要后台线程。"""
        pass


__all__ = [
    "BacktestTradeGuardProvider",
    "_SimpleSettings",
]
