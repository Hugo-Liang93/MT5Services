from __future__ import annotations

from datetime import date
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .module import TradingModule


class TradingCommandService:
    """交易命令入口。

    面向 API/应用层的写操作边界，只暴露会改变系统状态或触发交易动作的能力。
    """

    def __init__(self, module: TradingModule) -> None:
        self._module = module

    @property
    def active_account_alias(self) -> str:
        return self._module.active_account_alias

    def dispatch_operation(self, operation: str, payload: Optional[dict[str, Any]] = None) -> Any:
        return self._module.dispatch_operation(operation, payload)

    def update_trade_control(
        self,
        *,
        auto_entry_enabled: Optional[bool] = None,
        close_only_mode: Optional[bool] = None,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
        action_id: Optional[str] = None,
        audit_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        request_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._module.update_trade_control(
            auto_entry_enabled=auto_entry_enabled,
            close_only_mode=close_only_mode,
            reason=reason,
            actor=actor,
            action_id=action_id,
            audit_id=audit_id,
            idempotency_key=idempotency_key,
            request_context=request_context,
        )

    def apply_trade_control_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._module.apply_trade_control_state(state)

    def record_operator_action(
        self,
        *,
        command_type: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        status: str = "success",
        error_message: Optional[str] = None,
        operation_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._module.record_operator_action(
            command_type=command_type,
            request_payload=request_payload,
            response_payload=response_payload,
            status=status,
            error_message=error_message,
            operation_id=operation_id,
            symbol=symbol,
        )

    def find_operator_action_replay(
        self,
        *,
        command_type: str,
        idempotency_key: Optional[str],
        request_payload: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        return self._module.find_operator_action_replay(
            command_type=command_type,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )

    def execute_trade(self, **kwargs: Any) -> Any:
        return self._module.execute_trade(**kwargs)

    def precheck_trade(self, **kwargs: Any) -> Any:
        return self._module.precheck_trade(**kwargs)

    def execute_trade_batch(self, trades: list[dict], stop_on_error: bool = False) -> dict[str, Any]:
        return self._module.execute_trade_batch(trades=trades, stop_on_error=stop_on_error)

    def close_position(self, **kwargs: Any) -> Any:
        return self._module.close_position(**kwargs)

    def close_all_positions(self, **kwargs: Any) -> Any:
        return self._module.close_all_positions(**kwargs)

    def close_positions_by_tickets(
        self,
        tickets: list[int],
        deviation: int = 20,
        comment: str = "close_batch",
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
        action_id: Optional[str] = None,
        audit_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        request_context: Optional[dict[str, Any]] = None,
    ) -> Any:
        return self._module.close_positions_by_tickets(
            tickets=tickets,
            deviation=deviation,
            comment=comment,
            actor=actor,
            reason=reason,
            action_id=action_id,
            audit_id=audit_id,
            idempotency_key=idempotency_key,
            request_context=request_context,
        )

    def cancel_orders(self, **kwargs: Any) -> Any:
        return self._module.cancel_orders(**kwargs)

    def cancel_orders_by_tickets(
        self,
        tickets: list[int],
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
        action_id: Optional[str] = None,
        audit_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        request_context: Optional[dict[str, Any]] = None,
    ) -> Any:
        return self._module.cancel_orders_by_tickets(
            tickets,
            actor=actor,
            reason=reason,
            action_id=action_id,
            audit_id=audit_id,
            idempotency_key=idempotency_key,
            request_context=request_context,
        )

    def estimate_margin(self, **kwargs: Any) -> Any:
        return self._module.estimate_margin(**kwargs)

    def modify_orders(self, **kwargs: Any) -> Any:
        return self._module.modify_orders(**kwargs)

    def modify_positions(self, **kwargs: Any) -> Any:
        return self._module.modify_positions(**kwargs)


class TradingQueryService:
    """交易查询入口。

    面向 API/read model 的只读边界，只暴露查询、摘要、状态投影与只读恢复辅助能力。
    """

    def __init__(self, module: TradingModule) -> None:
        self._module = module

    @property
    def active_account_alias(self) -> str:
        return self._module.active_account_alias

    def trade_control_status(self) -> dict[str, Any]:
        return self._module.trade_control_status()

    def daily_trade_summary(self, summary_date: Optional[date] = None) -> dict[str, Any]:
        return self._module.daily_trade_summary(summary_date=summary_date)

    def entry_to_order_status(
        self,
        symbol: Optional[str] = None,
        volume: float = 0.1,
        side: str = "buy",
        order_kind: str = "market",
    ) -> dict[str, Any]:
        return self._module.entry_to_order_status(
            symbol=symbol,
            volume=volume,
            side=side,
            order_kind=order_kind,
        )

    def list_accounts(self) -> list[dict]:
        return self._module.list_accounts()

    def health(self) -> dict[str, Any]:
        return self._module.health()

    def account_info(self) -> Any:
        return self._module.account_info()

    def positions(self, symbol: Optional[str] = None) -> Any:
        return self._module.positions(symbol)

    def orders(self, symbol: Optional[str] = None) -> Any:
        return self._module.orders(symbol)

    def get_positions(self, symbol: Optional[str] = None, magic: Optional[int] = None) -> Any:
        return self._module.get_positions(symbol, magic)

    def get_orders(self, symbol: Optional[str] = None, magic: Optional[int] = None) -> Any:
        return self._module.get_orders(symbol, magic)

    def get_position_close_details(
        self,
        ticket: int,
        *,
        symbol: Optional[str] = None,
        lookback_days: int = 7,
    ) -> Optional[dict[str, Any]]:
        return self._module.get_position_close_details(
            ticket=ticket,
            symbol=symbol,
            lookback_days=lookback_days,
        )

    def resolve_position_context(
        self,
        *,
        ticket: int,
        comment: Optional[str] = None,
        limit: int = 500,
    ) -> Optional[dict[str, Any]]:
        return self._module.resolve_position_context(
            ticket=ticket,
            comment=comment,
            limit=limit,
        )

    def recent_command_audits(
        self,
        *,
        command_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        return self._module.recent_command_audits(
            command_type=command_type,
            status=status,
            limit=limit,
        )

    def command_audit_page(
        self,
        *,
        command_type: Optional[str] = None,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        signal_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        actor: Optional[str] = None,
        from_time: Optional[Any] = None,
        to_time: Optional[Any] = None,
        page: int = 1,
        page_size: int = 100,
        sort: str = "recorded_at_desc",
    ) -> dict[str, Any]:
        return self._module.command_audit_page(
            command_type=command_type,
            status=status,
            symbol=symbol,
            signal_id=signal_id,
            trace_id=trace_id,
            actor=actor,
            from_time=from_time,
            to_time=to_time,
            page=page,
            page_size=page_size,
            sort=sort,
        )

    def monitoring_summary(self, *, hours: int = 24) -> dict[str, Any]:
        return self._module.monitoring_summary(hours=hours)
