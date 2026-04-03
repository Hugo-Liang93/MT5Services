from __future__ import annotations

from typing import Any, Optional, Protocol


class TradingQueryPort(Protocol):
    def get_positions(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
    ) -> Any: ...

    def get_orders(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
    ) -> Any: ...


class TradeControlStatePort(Protocol):
    def apply_trade_control_state(self, state: dict[str, Any]) -> dict[str, Any]: ...


class PendingOrderCancellationPort(Protocol):
    def cancel_orders_by_tickets(self, tickets: list[int]) -> Any: ...


class PositionManagementPort(TradingQueryPort, Protocol):
    def close_all_positions(self, **kwargs: Any) -> Any: ...

    def modify_positions(self, **kwargs: Any) -> Any: ...

    def account_info(self) -> Any: ...

    def get_position_close_details(
        self,
        ticket: int,
        *,
        symbol: Optional[str] = None,
        lookback_days: int = 7,
    ) -> Optional[dict[str, Any]]: ...


class RecoveryTradingPort(
    TradingQueryPort,
    PendingOrderCancellationPort,
    TradeControlStatePort,
    Protocol,
):
    pass


class TradeDispatchPort(Protocol):
    def dispatch_operation(
        self,
        operation: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> Any: ...


class ExecutorTradingPort(TradingQueryPort, TradeDispatchPort, Protocol):
    pass
