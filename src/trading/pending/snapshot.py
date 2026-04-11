"""PendingEntryManager 的只读状态快照服务。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class PendingEntrySnapshotService:
    """负责 pending/m5 运行态的结构化只读视图构建。"""

    def __init__(
        self,
        *,
        lock: Any,
        pending: dict[str, Any],
        mt5_orders: dict[str, Any],
        stats: dict[str, Any],
    ) -> None:
        self._lock = lock
        self._pending = pending
        self._mt5_orders = mt5_orders
        self._stats = stats

    def status(self) -> dict[str, Any]:
        """返回状态快照（线程安全）。"""
        with self._lock:
            entries = [
                {
                    "signal_id": e.signal_event.signal_id,
                    "symbol": e.signal_event.symbol,
                    "direction": e.signal_event.direction,
                    "strategy": e.signal_event.strategy,
                    "zone": [e.entry_low, e.entry_high],
                    "reference_price": e.reference_price,
                    "zone_mode": e.zone_mode,
                    "checks_count": e.checks_count,
                    "best_price_seen": e.best_price_seen,
                    "remaining_seconds": max(
                        0,
                        (e.expires_at - datetime.now(timezone.utc)).total_seconds(),
                    ),
                }
                for e in self._pending.values()
                if e.status == "pending"
            ]
            stats_copy = dict(self._stats)

        filled = stats_copy.get("total_filled", 0)
        submitted = stats_copy.get("total_submitted", 0)
        return {
            "active_count": len(entries),
            "entries": entries,
            "stats": {
                **stats_copy,
                "fill_rate": round(filled / submitted, 3) if submitted > 0 else None,
                "avg_price_improvement": (
                    round(
                        float(stats_copy.get("total_price_improvement", 0.0)) / filled, 4
                    )
                    if filled > 0
                    else None
                ),
            },
        }

    def active_execution_contexts(self) -> list[dict[str, Any]]:
        """返回统一的执行上下文快照（pending + MT5 挂单）。"""
        with self._lock:
            pending_entries = [
                {
                    "signal_id": e.signal_event.signal_id,
                    "symbol": e.signal_event.symbol,
                    "timeframe": e.signal_event.timeframe,
                    "strategy": e.signal_event.strategy,
                    "direction": e.signal_event.direction,
                    "source": "pending_entry",
                    "status": e.status,
                }
                for e in self._pending.values()
                if e.status == "pending"
            ]
            mt5_entries = [
                {
                    "signal_id": str(info.get("signal_id") or ""),
                    "symbol": str(info.get("symbol") or ""),
                    "timeframe": str(info.get("timeframe") or ""),
                    "strategy": str(info.get("strategy") or ""),
                    "direction": str(info.get("direction") or ""),
                    "source": "mt5_order",
                    "status": "pending",
                    "ticket": int(info.get("ticket") or 0),
                }
                for info in self._mt5_orders.values()
            ]
        return pending_entries + mt5_entries
