from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .sizing import TradeParameters, compute_trade_params, extract_atr_from_indicators

if TYPE_CHECKING:
    from src.signals.models import SignalEvent
    from .executor import TradeExecutor

logger = logging.getLogger(__name__)


def get_contract_size(executor: "TradeExecutor", symbol: str) -> float:
    size_map = executor.config.contract_size_map
    return size_map.get(symbol, size_map.get("default", 100.0))


def compute_params(
    executor: "TradeExecutor", event: "SignalEvent"
) -> TradeParameters | None:
    atr = extract_atr_from_indicators(event.indicators)
    if atr is None or atr <= 0:
        return None

    balance = get_account_balance(executor)
    if balance is None or balance <= 0:
        return None

    close_price: float | None = None
    raw_close = event.metadata.get("close_price")
    if raw_close is not None:
        try:
            close_price = float(raw_close)
        except (TypeError, ValueError):
            close_price = None
    if close_price is None or close_price <= 0:
        close_price = estimate_price(event.indicators)
    if close_price is None or close_price <= 0:
        return None

    contract_size = get_contract_size(executor, event.symbol)

    return compute_trade_params(
        action=event.direction,
        current_price=close_price,
        atr_value=atr,
        account_balance=balance,
        timeframe=event.timeframe,
        risk_percent=executor.config.risk_percent,
        sl_atr_multiplier=executor.config.sl_atr_multiplier,
        tp_atr_multiplier=executor.config.tp_atr_multiplier,
        min_volume=executor.config.min_volume,
        max_volume=executor.config.max_volume,
        contract_size=contract_size,
        timeframe_risk_overrides=executor.config.timeframe_risk_multipliers or None,
        regime=str(event.metadata.get("regime") or event.metadata.get("_regime") or ""),
        regime_sizing=executor.config.regime_sizing,
    )


def get_account_balance(executor: "TradeExecutor") -> float | None:
    if executor._account_balance_getter is not None:
        try:
            return float(executor._account_balance_getter())
        except (TypeError, ValueError, AttributeError):
            logger.debug("account_balance_getter failed", exc_info=True)
    try:
        info = executor._trading.account_info()
        if isinstance(info, dict):
            return float(info.get("equity") or info.get("balance") or 0)
        return float(
            getattr(info, "equity", None) or getattr(info, "balance", None) or 0
        )
    except (TypeError, ValueError, AttributeError) as exc:
        logger.debug("Failed to get account balance: %s", exc)
        return None


def tf_to_seconds(tf: str) -> int:
    tf = tf.upper().strip()
    mapping = {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
    }
    return mapping.get(tf, 0)


def estimate_price(indicators: dict[str, dict[str, Any]]) -> float | None:
    for name in ("bollinger20", "sma20", "close", "price"):
        payload = indicators.get(name)
        if isinstance(payload, dict):
            for field in ("close", "value", "last", "bb_mid", "sma"):
                value = payload.get(field)
                if value is not None:
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        continue
    return None


def estimate_cost_metrics(
    executor: "TradeExecutor",
    event: "SignalEvent",
    params: TradeParameters,
) -> dict[str, float | None]:
    raw_spread = event.metadata.get("spread_points")
    try:
        spread_points = float(raw_spread) if raw_spread is not None else None
    except (TypeError, ValueError):
        spread_points = None

    raw_symbol_point = event.metadata.get("symbol_point")
    try:
        symbol_point = (
            float(raw_symbol_point) if raw_symbol_point is not None else None
        )
    except (TypeError, ValueError):
        symbol_point = None
    if symbol_point is not None and symbol_point <= 0:
        symbol_point = None

    raw_spread_price = event.metadata.get("spread_price")
    try:
        spread_price = (
            float(raw_spread_price) if raw_spread_price is not None else None
        )
    except (TypeError, ValueError):
        spread_price = None
    if spread_price is None and spread_points is not None and symbol_point is not None:
        spread_price = spread_points * symbol_point
    if spread_points is None and spread_price is not None and symbol_point is not None:
        spread_points = spread_price / symbol_point

    raw_close = event.metadata.get("close_price")
    try:
        close_price = float(raw_close) if raw_close is not None else None
    except (TypeError, ValueError):
        close_price = None
    if close_price is None or close_price <= 0:
        close_price = estimate_price(event.indicators)

    stop_distance = None
    reward_distance = None
    if close_price is not None and close_price > 0:
        stop_distance = abs(close_price - params.stop_loss)
        reward_distance = abs(params.take_profit - close_price)

    stop_distance_points = None
    reward_distance_points = None
    if symbol_point is not None and symbol_point > 0:
        if stop_distance is not None:
            stop_distance_points = stop_distance / symbol_point
        if reward_distance is not None:
            reward_distance_points = reward_distance / symbol_point

    spread_to_stop_ratio = None
    if spread_points is not None:
        if stop_distance_points is not None and stop_distance_points > 0:
            spread_to_stop_ratio = round(spread_points / stop_distance_points, 4)
        elif spread_price is not None and stop_distance and stop_distance > 0:
            spread_to_stop_ratio = round(spread_price / stop_distance, 4)

    reward_to_cost_ratio = None
    if spread_points is not None and spread_points > 0:
        if reward_distance_points is not None:
            reward_to_cost_ratio = round(reward_distance_points / spread_points, 4)
        elif spread_price is not None and spread_price > 0 and reward_distance is not None:
            reward_to_cost_ratio = round(reward_distance / spread_price, 4)

    return {
        "estimated_cost_points": spread_points,
        "estimated_cost_price": (
            round(spread_price, 6) if spread_price is not None else None
        ),
        "symbol_point": symbol_point,
        "stop_distance": round(stop_distance, 4) if stop_distance is not None else None,
        "stop_distance_points": (
            round(stop_distance_points, 2)
            if stop_distance_points is not None
            else None
        ),
        "reward_distance": round(reward_distance, 4) if reward_distance is not None else None,
        "reward_distance_points": (
            round(reward_distance_points, 2)
            if reward_distance_points is not None
            else None
        ),
        "spread_to_stop_ratio": spread_to_stop_ratio,
        "reward_to_cost_ratio": reward_to_cost_ratio,
    }
