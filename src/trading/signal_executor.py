"""TradeExecutor: auto-executes trades in response to confirmed signal events.

Lives in the trading module, not the signal module, to maintain clean separation:
- Signal module  → generates and publishes SignalEvent (knows nothing about trading)
- TradeExecutor  → subscribes via SignalRuntime.add_signal_listener(), executes trades

Usage in deps.py:
    executor = TradeExecutor(trading_module=_c.trade_module, config=cfg)
    signal_runtime.add_signal_listener(executor.on_signal_event)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.signals.models import SignalEvent
from src.signals.position_manager import PositionManager
from src.signals.sizing import TradeParameters, compute_trade_params, extract_atr_from_indicators

logger = logging.getLogger(__name__)


@dataclass
class ExecutorConfig:
    enabled: bool = False
    min_confidence: float = 0.7
    require_armed: bool = True
    risk_percent: float = 1.0
    sl_atr_multiplier: float = 1.5
    tp_atr_multiplier: float = 3.0
    min_volume: float = 0.01
    max_volume: float = 1.0
    contract_size: float = 100.0
    default_volume: float = 0.01


class TradeExecutor:
    """Subscribes to SignalRuntime events and auto-executes confirmed trades."""

    def __init__(
        self,
        trading_module: Any,
        config: Optional[ExecutorConfig] = None,
        account_balance_getter: Optional[Any] = None,
        position_manager: Optional[PositionManager] = None,
    ):
        self._trading = trading_module
        self.config = config or ExecutorConfig()
        self._account_balance_getter = account_balance_getter
        self._position_manager = position_manager
        self._execution_count = 0
        self._last_execution_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._execution_log: list[dict] = []

    # ------------------------------------------------------------------
    # Public listener interface
    # ------------------------------------------------------------------

    def on_signal_event(self, event: SignalEvent) -> None:
        """Called by SignalRuntime for every signal state transition.

        Only acts on scope=confirmed transitions with a buy/sell action.
        """
        if event.scope != "confirmed":
            return
        if "confirmed" not in event.signal_state:
            return
        self._handle_confirmed(event)

    # ------------------------------------------------------------------
    # Internal execution logic
    # ------------------------------------------------------------------

    def _handle_confirmed(self, event: SignalEvent) -> Optional[Dict[str, Any]]:
        if not self.config.enabled:
            return None

        if event.action not in ("buy", "sell"):
            return None

        if event.confidence < self.config.min_confidence:
            logger.debug(
                "TradeExecutor: skipping %s/%s - confidence %.2f < %.2f",
                event.symbol, event.strategy,
                event.confidence, self.config.min_confidence,
            )
            return None

        if self.config.require_armed:
            previous_state = event.metadata.get("previous_state", "")
            if "armed" not in previous_state:
                logger.debug(
                    "TradeExecutor: skipping %s - require_armed but previous_state=%s",
                    event.symbol, previous_state,
                )
                return None

        trade_params = self._compute_params(event)
        if trade_params is None:
            logger.warning(
                "TradeExecutor: cannot compute trade params for %s (missing ATR?)",
                event.symbol,
            )
            return None

        return self._execute(event, trade_params)

    def _compute_params(self, event: SignalEvent) -> Optional[TradeParameters]:
        atr = extract_atr_from_indicators(event.indicators)
        if atr is None or atr <= 0:
            return None

        balance = self._get_account_balance()
        if balance is None or balance <= 0:
            return None

        close_price = self._estimate_price(event.indicators)
        if close_price is None or close_price <= 0:
            return None

        return compute_trade_params(
            action=event.action,
            current_price=close_price,
            atr_value=atr,
            account_balance=balance,
            risk_percent=self.config.risk_percent,
            sl_atr_multiplier=self.config.sl_atr_multiplier,
            tp_atr_multiplier=self.config.tp_atr_multiplier,
            min_volume=self.config.min_volume,
            max_volume=self.config.max_volume,
            contract_size=self.config.contract_size,
        )

    def _get_account_balance(self) -> Optional[float]:
        if self._account_balance_getter is not None:
            try:
                return float(self._account_balance_getter())
            except Exception:
                pass
        try:
            info = self._trading.account_info()
            if isinstance(info, dict):
                return float(info.get("equity") or info.get("balance") or 0)
            return float(getattr(info, "equity", None) or getattr(info, "balance", None) or 0)
        except Exception:
            return None

    @staticmethod
    def _estimate_price(indicators: Dict[str, Dict[str, Any]]) -> Optional[float]:
        for name in ("bollinger20", "sma20", "close", "price"):
            payload = indicators.get(name)
            if isinstance(payload, dict):
                for fld in ("close", "value", "last", "bb_mid", "sma"):
                    val = payload.get(fld)
                    if val is not None:
                        try:
                            return float(val)
                        except (TypeError, ValueError):
                            continue
        return None

    def _execute(self, event: SignalEvent, params: TradeParameters) -> Optional[Dict[str, Any]]:
        payload = {
            "symbol": event.symbol,
            "volume": params.position_size,
            "side": event.action,
            "order_kind": "market",
            "sl": params.stop_loss,
            "tp": params.take_profit,
            "comment": f"auto:{event.strategy}:{event.action}",
        }

        try:
            result = self._trading.dispatch_operation("trade", payload)
            self._execution_count += 1
            self._last_execution_at = datetime.now(timezone.utc)
            self._last_error = None
            log_entry = {
                "at": self._last_execution_at.isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "action": event.action,
                "strategy": event.strategy,
                "confidence": event.confidence,
                "params": {
                    "volume": params.position_size,
                    "sl": params.stop_loss,
                    "tp": params.take_profit,
                    "rr": params.risk_reward_ratio,
                },
                "success": True,
            }
            self._execution_log.append(log_entry)
            if len(self._execution_log) > 100:
                self._execution_log = self._execution_log[-50:]
            logger.info(
                "TradeExecutor: executed %s %s vol=%.2f sl=%.2f tp=%.2f rr=%.2f (signal=%s)",
                event.action, event.symbol,
                params.position_size, params.stop_loss, params.take_profit,
                params.risk_reward_ratio, event.signal_id,
            )
            if self._position_manager is not None and isinstance(result, dict):
                ticket = result.get("ticket") or result.get("order")
                if ticket:
                    try:
                        self._position_manager.track_position(
                            ticket=int(ticket),
                            signal_id=event.signal_id,
                            symbol=event.symbol,
                            action=event.action,
                            params=params,
                        )
                    except Exception as pm_exc:
                        logger.warning(
                            "TradeExecutor: failed to register position ticket=%s: %s",
                            ticket, pm_exc,
                        )
            return result
        except Exception as exc:
            self._last_error = str(exc)
            self._execution_log.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "action": event.action,
                "strategy": event.strategy,
                "success": False,
                "error": str(exc),
            })
            if len(self._execution_log) > 100:
                self._execution_log = self._execution_log[-50:]
            logger.exception(
                "TradeExecutor: failed to execute %s %s: %s", event.action, event.symbol, exc,
            )
            return None

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "execution_count": self._execution_count,
            "last_execution_at": self._last_execution_at.isoformat() if self._last_execution_at else None,
            "last_error": self._last_error,
            "config": {
                "min_confidence": self.config.min_confidence,
                "require_armed": self.config.require_armed,
                "risk_percent": self.config.risk_percent,
                "sl_atr_multiplier": self.config.sl_atr_multiplier,
                "tp_atr_multiplier": self.config.tp_atr_multiplier,
            },
            "recent_executions": self._execution_log[-10:],
        }
