from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..ports import ExposureCloseoutPort


@dataclass
class CloseoutActionResult:
    requested_tickets: list[int] = field(default_factory=list)
    completed_tickets: list[int] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": list(self.requested_tickets),
            "completed": list(self.completed_tickets),
            "failed": list(self.failed),
            "error": self.error,
        }


@dataclass
class ExposureCloseoutResult:
    positions: CloseoutActionResult
    orders: CloseoutActionResult
    remaining_position_tickets: list[int]
    remaining_order_tickets: list[int]

    @property
    def completed(self) -> bool:
        return (
            not self.remaining_position_tickets
            and not self.remaining_order_tickets
            and self.positions.error is None
            and self.orders.error is None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "positions": self.positions.as_dict(),
            "orders": self.orders.as_dict(),
            "remaining_positions": list(self.remaining_position_tickets),
            "remaining_orders": list(self.remaining_order_tickets),
            "completed": self.completed,
        }


class ExposureCloseoutService:
    """集中处理风险收口：平掉全部持仓并撤销全部挂单。"""

    def __init__(self, trading: ExposureCloseoutPort):
        self._trading = trading

    def execute(self, *, comment: str) -> ExposureCloseoutResult:
        initial_positions, position_query_error = self._safe_get_positions()
        initial_orders, order_query_error = self._safe_get_orders()
        position_result = CloseoutActionResult(
            requested_tickets=self._extract_tickets(initial_positions)
        )
        order_result = CloseoutActionResult(
            requested_tickets=self._extract_tickets(initial_orders)
        )
        if position_query_error is not None:
            position_result.error = position_query_error
        if order_query_error is not None:
            order_result.error = order_query_error

        if position_result.requested_tickets and position_result.error is None:
            try:
                raw_result = self._trading.close_all_positions(comment=comment)
                position_result = self._merge_position_result(position_result, raw_result)
            except Exception as exc:
                position_result.error = str(exc)

        if order_result.requested_tickets and order_result.error is None:
            try:
                raw_result = self._trading.cancel_orders_by_tickets(order_result.requested_tickets)
                order_result = self._merge_order_result(order_result, raw_result)
            except Exception as exc:
                order_result.error = str(exc)

        remaining_positions_raw, remaining_positions_error = self._safe_get_positions()
        remaining_orders_raw, remaining_orders_error = self._safe_get_orders()
        if remaining_positions_error is not None:
            position_result.error = remaining_positions_error
        if remaining_orders_error is not None:
            order_result.error = remaining_orders_error
        remaining_positions = self._extract_tickets(remaining_positions_raw)
        remaining_orders = self._extract_tickets(remaining_orders_raw)
        return ExposureCloseoutResult(
            positions=position_result,
            orders=order_result,
            remaining_position_tickets=remaining_positions,
            remaining_order_tickets=remaining_orders,
        )

    @staticmethod
    def _extract_tickets(items: list[Any]) -> list[int]:
        tickets: list[int] = []
        for item in items:
            ticket = getattr(item, "ticket", None)
            if ticket is None and isinstance(item, dict):
                ticket = item.get("ticket")
            try:
                numeric = int(ticket or 0)
            except (TypeError, ValueError):
                numeric = 0
            if numeric > 0:
                tickets.append(numeric)
        return tickets

    @staticmethod
    def _extract_failed(raw_result: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_result, dict):
            return []
        failed = raw_result.get("failed", [])
        if not isinstance(failed, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in failed:
            if isinstance(item, dict):
                normalized.append(dict(item))
            else:
                normalized.append({"error": str(item)})
        return normalized

    @staticmethod
    def _extract_int_list(values: Any) -> list[int]:
        if not isinstance(values, list):
            return []
        extracted: list[int] = []
        for value in values:
            try:
                ticket = int(value)
            except (TypeError, ValueError):
                continue
            if ticket > 0:
                extracted.append(ticket)
        return extracted

    def _merge_position_result(
        self,
        result: CloseoutActionResult,
        raw_result: Any,
    ) -> CloseoutActionResult:
        if isinstance(raw_result, dict):
            result.completed_tickets = self._extract_int_list(raw_result.get("closed"))
            result.failed = self._extract_failed(raw_result)
        return result

    def _merge_order_result(
        self,
        result: CloseoutActionResult,
        raw_result: Any,
    ) -> CloseoutActionResult:
        if isinstance(raw_result, dict):
            result.completed_tickets = self._extract_int_list(raw_result.get("canceled"))
            result.failed = self._extract_failed(raw_result)
        return result

    def _safe_get_positions(self) -> tuple[list[Any], str | None]:
        try:
            return list(self._trading.get_positions() or []), None
        except Exception as exc:
            return [], str(exc)

    def _safe_get_orders(self) -> tuple[list[Any], str | None]:
        try:
            return list(self._trading.get_orders() or []), None
        except Exception as exc:
            return [], str(exc)


class ExposureCloseoutController:
    """统一的风险收口命令入口，供 EOD 与人工/API 复用。"""

    def __init__(self, service: ExposureCloseoutService):
        self._service = service
        self._lock = threading.Lock()
        self._last_status: dict[str, Any] = {
            "status": "idle",
            "last_reason": None,
            "last_comment": None,
            "last_requested_at": None,
            "last_completed_at": None,
            "result": None,
        }

    def execute(self, *, reason: str, comment: str) -> dict[str, Any]:
        requested_at = datetime.now(timezone.utc)
        result = self._service.execute(comment=comment).as_dict()
        status = {
            "status": "completed" if result.get("completed") else "incomplete",
            "last_reason": reason,
            "last_comment": comment,
            "last_requested_at": requested_at.isoformat(),
            "last_completed_at": requested_at.isoformat() if result.get("completed") else None,
            "result": result,
        }
        with self._lock:
            self._last_status = status
        return dict(status)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_status)
