from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.signals.service import SignalModule
from src.trading.execution.sizing import (
    compute_trade_params,
    extract_atr_from_indicators,
)

from .services import TradingCommandService, TradingQueryService


class SignalTradePreparationError(Exception):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.details = details or {}


@dataclass(frozen=True)
class PreparedSignalTrade:
    trade_request: dict[str, Any]
    execution_metadata: dict[str, Any]


class SignalTradeCommandService:
    """从确认信号生成交易命令的应用服务。"""

    def __init__(
        self,
        *,
        signal_service: SignalModule,
        command_service: TradingCommandService,
        query_service: TradingQueryService,
    ) -> None:
        self._signal_service = signal_service
        self._command_service = command_service
        self._query_service = query_service

    def prepare_trade(
        self,
        *,
        signal_id: str,
        volume_override: float | None = None,
    ) -> PreparedSignalTrade:
        signal_row = self._find_signal(signal_id)
        direction = str(signal_row.get("direction") or "").strip().lower()
        if direction not in {"buy", "sell"}:
            raise SignalTradePreparationError(
                "validation",
                f"Signal direction '{direction}' is not executable (buy/sell required)",
                details={"signal_id": signal_id, "direction": direction},
            )

        indicators = signal_row.get("indicators_snapshot") or {}
        atr = extract_atr_from_indicators(indicators)
        if atr is None or atr <= 0:
            raise SignalTradePreparationError(
                "data",
                "Cannot compute trade params: ATR not found in signal snapshot",
                details={"signal_id": signal_id},
            )

        balance = self._resolve_account_balance()
        if balance <= 0:
            raise SignalTradePreparationError(
                "account",
                "Cannot compute position size: account balance unavailable",
                details={"signal_id": signal_id},
            )

        entry_price = self._resolve_entry_price(indicators)
        if entry_price is None or entry_price <= 0:
            raise SignalTradePreparationError(
                "data",
                "Cannot estimate entry price from signal snapshot",
                details={"signal_id": signal_id},
            )

        params = compute_trade_params(
            action=direction,
            current_price=entry_price,
            atr_value=atr,
            account_balance=balance,
        )
        volume = (
            volume_override
            if volume_override is not None
            else params.position_size
        )
        trade_request = {
            "symbol": signal_row.get("symbol"),
            "volume": volume,
            "side": direction,
            "order_kind": "market",
            "sl": params.stop_loss,
            "tp": params.take_profit,
            "comment": f"agent:{signal_row.get('strategy')}:{direction}:{signal_id[:8]}",
            "request_id": signal_id,
            "metadata": {
                "entry_origin": "auto",
                "regime": (
                    signal_row.get("metadata", {}).get("regime")
                    if isinstance(signal_row.get("metadata"), dict)
                    else None
                ),
                "signal": {
                    "signal_id": signal_id,
                    "strategy": signal_row.get("strategy"),
                    "timeframe": signal_row.get("timeframe"),
                    "confidence": signal_row.get("confidence"),
                    "direction": direction,
                },
            },
        }
        execution_metadata = {
            "operation": "trade_from_signal",
            "signal_id": signal_id,
            "direction": direction,
            "volume": volume,
            "sl": params.stop_loss,
            "tp": params.take_profit,
            "risk_reward_ratio": params.risk_reward_ratio,
            "account_alias": self._command_service.active_account_alias,
        }
        return PreparedSignalTrade(
            trade_request=trade_request,
            execution_metadata=execution_metadata,
        )

    def execute_prepared(self, prepared: PreparedSignalTrade) -> Any:
        return self._command_service.dispatch_operation("trade", prepared.trade_request)

    def _find_signal(self, signal_id: str) -> dict[str, Any]:
        rows = self._signal_service.recent_signals(scope="confirmed", limit=500)
        signal_row = next(
            (row for row in rows if row.get("signal_id") == signal_id),
            None,
        )
        if signal_row is None:
            raise SignalTradePreparationError(
                "not_found",
                f"Signal not found: {signal_id}",
                details={"signal_id": signal_id},
            )
        return signal_row

    def _resolve_account_balance(self) -> float:
        try:
            account = self._query_service.account_info() or {}
            equity = (
                account.get("equity")
                if isinstance(account, dict)
                else getattr(account, "equity", None)
            )
            balance_value = (
                account.get("balance")
                if isinstance(account, dict)
                else getattr(account, "balance", None)
            )
            return float(equity or balance_value or 0)
        except Exception:
            return 0.0

    @staticmethod
    def _resolve_entry_price(indicators: dict[str, Any]) -> Optional[float]:
        for ind_name in ("bollinger20", "sma20", "close", "price"):
            payload = indicators.get(ind_name)
            if not isinstance(payload, dict):
                continue
            for field in ("close", "value", "last", "bb_mid", "sma"):
                value = payload.get(field)
                if value is None:
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None
