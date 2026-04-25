"""Studio protocol constants and builder helpers."""

from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

from src.utils.timezone import utc_now

AGENT_META: dict[str, dict[str, str]] = {
    "collector": {
        "name": "采集员",
        "module": "BackgroundIngestor",
        "zone": "collection",
    },
    "analyst": {
        "name": "分析师",
        "module": "UnifiedIndicatorManager(confirmed)",
        "zone": "analysis",
    },
    "live_analyst": {
        "name": "盘中分析员",
        "module": "UnifiedIndicatorManager(intrabar)",
        "zone": "analysis",
    },
    "filter_guard": {
        "name": "过滤员",
        "module": "SignalFilterChain",
        "zone": "filter",
    },
    "regime_guard": {
        "name": "状态守卫",
        "module": "RegimeDetector+AffinityGate",
        "zone": "regime",
    },
    "strategist": {
        "name": "策略师",
        "module": "SignalModule(confirmed)",
        "zone": "strategy",
    },
    "live_strategist": {
        "name": "盘中策略员",
        "module": "SignalModule(intrabar)",
        "zone": "strategy",
    },
    "risk_officer": {
        "name": "风控官",
        "module": "PreTradeRiskService",
        "zone": "decision",
    },
    "trader": {
        "name": "交易员",
        "module": "TradeExecutor",
        "zone": "decision",
    },
    "position_manager": {
        "name": "仓位管理员",
        "module": "PositionManager",
        "zone": "support",
    },
    "accountant": {
        "name": "会计模块",
        "module": "TradingModule",
        "zone": "support",
    },
    "calendar_reporter": {
        "name": "日历模块",
        "module": "EconomicCalendarService",
        "zone": "support",
    },
    "backtester": {
        "name": "回测模块",
        "module": "BacktestingWorkbench",
        "zone": "support",
    },
    "inspector": {
        "name": "巡检模块",
        "module": "MonitoringManager",
        "zone": "support",
    },
}


def build_agent(
    agent_id: str,
    status: str,
    task: str,
    *,
    metrics: Optional[dict[str, Any]] = None,
    alert_level: str = "none",
    symbol: Optional[str] = None,
) -> dict[str, Any]:
    meta = AGENT_META.get(agent_id, {"name": agent_id, "module": "", "zone": ""})
    result: dict[str, Any] = {
        "id": agent_id,
        "name": meta["name"],
        "module": meta["module"],
        "zone": meta["zone"],
        "status": status,
        "task": task,
        "alertLevel": alert_level,
        "updatedAt": utc_now().isoformat(),
    }
    if symbol is not None:
        result["symbol"] = symbol
    if metrics is not None:
        result["metrics"] = metrics
    return result


def build_event(
    event_type: str,
    source: str,
    message: str,
    *,
    level: str = "info",
    target: Optional[str] = None,
    symbol: Optional[str] = None,
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "eventId": event_id or uuid4().hex[:12],
        "type": event_type,
        "source": source,
        "level": level,
        "message": message,
        "createdAt": utc_now().isoformat(),
    }
    if target is not None:
        result["target"] = target
    if symbol is not None:
        result["symbol"] = symbol
    return result
