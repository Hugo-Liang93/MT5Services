"""经济事件按时间窗口加载器。

从 TimescaleDB `economic_calendar` 表读取给定时间窗口内的事件，
转成轻量 `SimpleEvent` dataclass 供下游（backtesting TradeGuard、
research mining 派生特征）使用。

这是 calendar 领域对外的数据端口之一，不依赖 research 或 backtesting；
两个消费方都从本模块正向导入。

历史：原位于 `src/backtesting/filtering/economic.py`，2026-04-22 P4 解耦
（research↔backtesting 反向依赖）迁移至 calendar 域；原名
`load_backtest_economic_events` 改名为 `load_economic_events_window`，
因不再是 backtest 专属加载能力。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SimpleEvent:
    """经济事件的轻量 value object，兼容 trade_guard.build_window()。

    下划线前缀已去除（不再是模块私有），供 calendar 域外的消费方导入。
    字段命名与 `economic_calendar.EventSummary` 保持一致。
    """

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


def load_economic_events_window(
    economic_repo: Any,
    start_time: datetime,
    end_time: datetime,
    buffer_hours: int = 6,
    currencies: Optional[List[str]] = None,
    importance_min: int = 2,
) -> List[SimpleEvent]:
    """从 DB 加载给定时间窗口内的经济事件（含前后缓冲）。

    Args:
        economic_repo: EconomicCalendarRepository 实例
        start_time: 窗口起始时间
        end_time: 窗口结束时间
        buffer_hours: 前后扩展小时数（捕获边界事件）
        currencies: 货币过滤（如 ["USD"]）
        importance_min: 最低重要性（0-3）

    Returns:
        SimpleEvent 列表，按 scheduled_at 升序。
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

    events: List[SimpleEvent] = []
    for row in rows:
        scheduled_at = row[0]
        if scheduled_at is None:
            continue
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)

        events.append(
            SimpleEvent(
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
        "Loaded %d economic events for window [%s ~ %s] (currencies=%s, imp>=%d)",
        len(events),
        buffered_start.strftime("%Y-%m-%d"),
        buffered_end.strftime("%Y-%m-%d"),
        currencies,
        importance_min,
    )
    return events


__all__ = ["SimpleEvent", "load_economic_events_window"]
