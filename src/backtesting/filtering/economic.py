"""回测用经济日历 TradeGuard 提供者。

从 DB 预加载回测时间段的经济事件到内存，
按 bar 时间查询风险窗口，复用实盘 trade_guard 的分级逻辑。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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


@dataclass
class _SimpleEvent:
    """回测用的简化经济事件，兼容 trade_guard.build_window()。"""

    event_uid: str
    event_name: str
    source: str
    country: str
    currency: str
    importance: int
    scheduled_at: datetime
    scheduled_at_local: Optional[datetime] = None
    scheduled_at_release: Optional[datetime] = None
    status: str = "released"
    session_bucket: str = ""
    category: str = ""


class BacktestTradeGuardProvider:
    """回测用 TradeGuard 提供者。

    在回测开始时从 DB 预加载回测期间（±buffer）的所有经济事件，
    然后每个 bar 评估时从内存中过滤。

    实现 TradeGuardProvider Protocol（src/signals/execution/filters.py:32）。
    """

    def __init__(
        self,
        events: List[_SimpleEvent],
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
    ) -> List[_SimpleEvent]:
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
        from src.calendar.economic_calendar.trade_guard import get_trade_guard

        return get_trade_guard(self, **kwargs)

    def _ensure_worker_running(self) -> None:
        """兼容 trade_guard.get_trade_guard() 的调用。回测不需要后台线程。"""
        pass


def load_backtest_economic_events(
    economic_repo: Any,
    start_time: datetime,
    end_time: datetime,
    buffer_hours: int = 6,
    currencies: Optional[List[str]] = None,
    importance_min: int = 2,
) -> List[_SimpleEvent]:
    """从 DB 加载回测期间的经济事件。

    Args:
        economic_repo: EconomicCalendarRepository 实例
        start_time: 回测开始时间
        end_time: 回测结束时间
        buffer_hours: 前后扩展小时数（捕获边界事件）
        currencies: 货币过滤（如 ["USD"]）
        importance_min: 最低重要性

    Returns:
        _SimpleEvent 列表
    """
    buffered_start = start_time - timedelta(hours=buffer_hours)
    buffered_end = end_time + timedelta(hours=buffer_hours)

    rows = economic_repo.fetch_economic_calendar(
        start_time=buffered_start,
        end_time=buffered_end,
        limit=5000,
        currencies=currencies,
        importance_min=importance_min,
    )

    events: List[_SimpleEvent] = []
    for row in rows:
        scheduled_at = row[0]
        if scheduled_at is None:
            continue
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)

        events.append(
            _SimpleEvent(
                event_uid=str(row[1] or ""),
                source=str(row[2] or ""),
                event_name=str(row[4] or ""),
                country=str(row[5] or ""),
                category=str(row[6] or ""),
                currency=str(row[7] or ""),
                importance=int(row[13] or 0),
                scheduled_at=scheduled_at,
                status=str(row[26] or "released"),
                session_bucket=str(row[22] or ""),
            )
        )

    logger.info(
        "Loaded %d economic events for backtest [%s ~ %s] (currencies=%s, imp>=%d)",
        len(events),
        buffered_start.strftime("%Y-%m-%d"),
        buffered_end.strftime("%Y-%m-%d"),
        currencies,
        importance_min,
    )
    return events
