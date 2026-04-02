from __future__ import annotations

from typing import Any, Callable, Optional


class TradingStateAlerts:
    """独立的交易状态告警评估器。"""

    def __init__(
        self,
        *,
        state_store: Any,
        trading_module: Any,
        account_alias_getter: Optional[Callable[[], str]] = None,
    ) -> None:
        self._state_store = state_store
        self._trading_module = trading_module
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
        if persisted_open_count != live_position_count:
            alerts.append(
                self._alert(
                    code="position_open_mismatch",
                    severity="critical",
                    message="持仓状态与 MT5 实时持仓数量不一致",
                    details={
                        "persisted_open_count": persisted_open_count,
                        "live_position_count": live_position_count,
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
        getter = getattr(self._trading_module, "get_orders", None)
        if not callable(getter):
            return []
        try:
            return list(getter())
        except Exception:
            return []

    def _safe_live_positions(self) -> list[Any]:
        getter = getattr(self._trading_module, "get_positions", None)
        if not callable(getter):
            return []
        try:
            return list(getter())
        except Exception:
            return []

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
