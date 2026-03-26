"""Studio protocol constants and builder helpers.

This module defines the agent metadata registry and provides pure helper
functions for constructing ``StudioAgent`` / ``StudioEvent`` dicts that
conform to the Anteater frontend protocol (``types/protocol.ts``).

No business-module imports — all data arrives as primitives.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

from src.utils.timezone import utc_now

# ── Agent metadata registry ────────────────────────────────────
# Static information per agent role. Matches the frontend's
# ``config/employees.ts`` — keep in sync when roles change.

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
        "name": "实时分析员",
        "module": "UnifiedIndicatorManager(intrabar)",
        "zone": "analysis",
    },
    "filter_guard": {
        "name": "过滤员",
        "module": "SignalFilterChain",
        "zone": "filter",
    },
    "regime_guard": {
        "name": "研判员",
        "module": "RegimeDetector+AffinityGate",
        "zone": "regime",
    },
    "strategist": {
        "name": "策略师",
        "module": "SignalModule(confirmed)",
        "zone": "strategy",
    },
    "live_strategist": {
        "name": "实时策略员",
        "module": "SignalModule(intrabar)",
        "zone": "strategy",
    },
    "voter": {
        "name": "投票主席",
        "module": "VotingEngine",
        "zone": "decision",
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
        "name": "仓管员",
        "module": "PositionManager",
        "zone": "support",
    },
    "accountant": {
        "name": "会计",
        "module": "TradingModule",
        "zone": "support",
    },
    "calendar_reporter": {
        "name": "日历员",
        "module": "EconomicCalendarService",
        "zone": "support",
    },
    "inspector": {
        "name": "巡检员",
        "module": "MonitoringManager",
        "zone": "support",
    },
}


# ── Builder helpers ─────────────────────────────────────────────


def build_agent(
    agent_id: str,
    status: str,
    task: str,
    *,
    metrics: Optional[dict[str, Any]] = None,
    alert_level: str = "none",
    symbol: Optional[str] = None,
) -> dict[str, Any]:
    """Construct a StudioAgent dict conforming to the frontend protocol."""
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
    """Construct a StudioEvent dict conforming to the frontend protocol."""
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
