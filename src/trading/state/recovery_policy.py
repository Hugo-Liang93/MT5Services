from __future__ import annotations

from typing import Any, Literal

from src.trading.ports import PendingOrderCancellationPort


OrphanRecoveryAction = Literal["record_only", "cancel"]
MissingRecoveryAction = Literal["mark_missing", "ignore"]


class TradingStateRecoveryPolicy:
    """挂单恢复处置策略。"""

    def __init__(
        self,
        state_store: Any,
        *,
        orphan_action: OrphanRecoveryAction = "record_only",
        missing_action: MissingRecoveryAction = "mark_missing",
    ) -> None:
        self._store = state_store
        self._orphan_action = orphan_action
        self._missing_action = missing_action

    def handle_missing_pending_order(
        self,
        info: dict[str, Any],
        *,
        state: dict[str, Any] | None = None,
    ) -> str:
        if self._missing_action == "ignore":
            return "ignored_missing"
        self._store.mark_pending_order_missing(
            info,
            reason=str((state or {}).get("reason") or "startup_missing"),
        )
        return "missing"

    def handle_orphan_pending_order(
        self,
        order_row: Any,
        *,
        trading_module: PendingOrderCancellationPort,
    ) -> str:
        self._store.mark_pending_order_orphan(order_row)
        if self._orphan_action != "cancel":
            return "orphan"

        ticket = self._ticket(order_row)
        if ticket <= 0:
            return "orphan"

        try:
            result = trading_module.cancel_orders_by_tickets([ticket])
        except Exception:
            return "orphan"

        if not self._ticket_was_cancelled(result, ticket):
            return "orphan"

        self._store.mark_pending_order_cancelled(
            self._pending_info_from_order_row(order_row),
            reason="startup_orphan_cancelled",
        )
        return "orphan_cancelled"

    @classmethod
    def _pending_info_from_order_row(cls, order_row: Any) -> dict[str, Any]:
        return {
            "ticket": cls._ticket(order_row),
            "signal_id": None,
            "expires_at": None,
            "direction": cls._direction_from_order(order_row),
            "symbol": str(cls._row_value(order_row, "symbol", "") or ""),
            "strategy": "",
            "timeframe": "",
            "confidence": None,
            "regime": None,
            "comment": str(cls._row_value(order_row, "comment", "") or ""),
            "params": None,
            "entry_low": None,
            "entry_high": None,
            "trigger_price": cls._float_or_none(cls._row_value(order_row, "price_open", None)),
            "entry_price_requested": cls._float_or_none(cls._row_value(order_row, "price_open", None)),
            "stop_loss": cls._float_or_none(cls._row_value(order_row, "sl", None)),
            "take_profit": cls._float_or_none(cls._row_value(order_row, "tp", None)),
            "volume": cls._float_or_none(cls._row_value(order_row, "volume_current", None))
            or cls._float_or_none(cls._row_value(order_row, "volume_initial", None)),
            "created_at": cls._row_value(order_row, "time_setup", None),
            "metadata": {},
        }

    @staticmethod
    def _ticket(order_row: Any) -> int:
        try:
            return int(TradingStateRecoveryPolicy._row_value(order_row, "ticket", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _row_value(row: Any, field: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(field, default)
        return getattr(row, field, default)

    @staticmethod
    def _ticket_was_cancelled(result: Any, ticket: int) -> bool:
        if isinstance(result, dict):
            cancelled = {
                int(item)
                for item in (result.get("canceled") or result.get("cancelled") or [])
                if item is not None
            }
            failed = {int(item) for item in (result.get("failed") or []) if item is not None}
            if ticket in cancelled:
                return True
            if failed:
                return ticket not in failed
        return bool(result)

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _direction_from_order(order_row: Any) -> str:
        order_type = TradingStateRecoveryPolicy._row_value(order_row, "type", None)
        if order_type in {0, 2, 4, 6}:
            return "buy"
        if order_type in {1, 3, 5, 7}:
            return "sell"
        return ""
