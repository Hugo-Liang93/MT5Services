from __future__ import annotations

from typing import Any, Callable, Optional

from src.trading.broker.comment_codec import looks_like_system_trade_comment
from src.trading.ports import TradingQueryPort


class TradingStateAlerts:
    """独立的交易状态告警评估器。"""

    def __init__(
        self,
        *,
        state_store: Any,
        trading_module: TradingQueryPort,
        account_alias_getter: Optional[Callable[[], str]] = None,
    ) -> None:
        self._state_store = state_store
        self._trading = trading_module
        self._account_alias_getter = account_alias_getter or (lambda: "")

    def summary(self, *, pending_limit: int = 50, position_limit: int = 50) -> dict[str, Any]:
        active_pending = self._safe_pending_states(
            statuses=["placed", "orphan"],
            limit=pending_limit,
        )
        lifecycle_missing = self._safe_pending_states(
            statuses=["missing"],
            limit=pending_limit,
        )
        open_positions = self._safe_position_states(
            statuses=["open"],
            limit=position_limit,
        )
        live_orders = self._safe_live_orders()
        live_positions = self._safe_live_positions()

        active_status_counts = self._state_counts(active_pending)
        missing_count = len(lifecycle_missing)
        orphan_count = int(active_status_counts.get("orphan", 0))
        persisted_active_count = len(active_pending)
        persisted_open_count = len(open_positions)
        live_order_count = len(live_orders)
        live_position_count = len(live_positions)
        unmanaged_positions = self._unmanaged_live_positions(
            live_positions=live_positions,
            managed_rows=open_positions,
        )
        unmanaged_tickets = [
            int(item.get("ticket"))
            for item in unmanaged_positions
            if item.get("ticket") is not None
        ]

        alerts: list[dict[str, Any]] = []
        if missing_count > 0:
            alerts.append(
                self._alert(
                    code="pending_missing",
                    severity="critical",
                    message=f"存在 {missing_count} 笔 unresolved missing 挂单状态",
                    details={"missing_count": missing_count},
                )
            )
        if orphan_count > 0:
            alerts.append(
                self._alert(
                    code="pending_orphan",
                    severity="warning",
                    message=f"存在 {orphan_count} 笔 orphan 挂单",
                    details={"orphan_count": orphan_count},
                )
            )
        if persisted_active_count != live_order_count:
            alerts.append(
                self._alert(
                    code="pending_active_mismatch",
                    severity="warning",
                    message="活跃挂单状态与 MT5 实时挂单数量不一致",
                    details={
                        "persisted_active_count": persisted_active_count,
                        "live_order_count": live_order_count,
                    },
                )
            )
        if unmanaged_positions:
            alerts.append(
                self._alert(
                    code="unmanaged_live_positions",
                    severity="warning",
                    message=f"存在 {len(unmanaged_positions)} 笔未纳管实时持仓",
                    details={
                        "managed_open_count": persisted_open_count,
                        "live_position_count": live_position_count,
                        "unmanaged_tickets": unmanaged_tickets,
                    },
                )
            )

        overall_status = "healthy"
        if any(alert["severity"] == "critical" for alert in alerts):
            overall_status = "critical"
        elif alerts:
            overall_status = "warning"

        return {
            "status": overall_status,
            "account_alias": self._account_alias_getter(),
            "alerts": alerts,
            "summary": [
                {
                    "code": alert["code"],
                    "status": "failed",
                    "severity": alert["severity"],
                    "message": alert["message"],
                }
                for alert in alerts
            ],
            "observed": {
                "active_pending_count": persisted_active_count,
                "active_pending_status_counts": active_status_counts,
                "missing_pending_count": missing_count,
                "open_position_count": persisted_open_count,
                "live_order_count": live_order_count,
                "live_position_count": live_position_count,
                "unmanaged_live_position_count": len(unmanaged_positions),
                "unmanaged_live_position_tickets": unmanaged_tickets,
            },
        }

    def monitoring_summary(self, *, hours: int = 24) -> dict[str, Any]:
        del hours
        return self.summary()

    def _safe_pending_states(self, *, statuses: list[str], limit: int) -> list[dict[str, Any]]:
        try:
            return list(
                self._state_store.list_pending_order_states(
                    statuses=statuses,
                    limit=limit,
                )
            )
        except Exception:
            return []

    def _safe_position_states(self, *, statuses: list[str], limit: int) -> list[dict[str, Any]]:
        try:
            return list(
                self._state_store.list_position_runtime_states(
                    statuses=statuses,
                    limit=limit,
                )
            )
        except Exception:
            return []

    def _safe_live_orders(self) -> list[Any]:
        try:
            return list(self._trading.get_orders())
        except Exception:
            return []

    def _safe_live_positions(self) -> list[Any]:
        try:
            return list(self._trading.get_positions())
        except Exception:
            return []

    def _unmanaged_live_positions(
        self,
        *,
        live_positions: list[Any],
        managed_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        managed_tickets = {
            int(ticket)
            for ticket in (
                row.get("position_ticket")
                for row in managed_rows
            )
            if ticket is not None
        }
        resolver = getattr(self._trading, "resolve_position_context", None)
        unmanaged: list[dict[str, Any]] = []
        for position in live_positions:
            ticket = self._attr(position, "ticket")
            if ticket is None:
                continue
            if int(ticket) in managed_tickets:
                continue
            comment = str(self._attr(position, "comment") or "").strip()
            magic = self._attr(position, "magic")
            context = None
            if callable(resolver):
                try:
                    context = resolver(ticket=int(ticket), comment=comment, limit=200)
                except Exception:
                    context = None
            if not comment and int(magic or 0) == 0:
                reason = "manual_position"
            elif context is None and not looks_like_system_trade_comment(comment):
                reason = "unsupported_comment"
            else:
                reason = "missing_context"
            unmanaged.append({"ticket": int(ticket), "reason": reason})
        return unmanaged

    @staticmethod
    def _attr(item: Any, key: str) -> Any:
        if isinstance(item, dict):
            return item.get(key)
        return getattr(item, key, None)

    @staticmethod
    def _state_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    @staticmethod
    def _alert(*, code: str, severity: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": code,
            "severity": severity,
            "message": message,
            "details": details,
        }
