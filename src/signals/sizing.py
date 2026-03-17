"""ATR-based trade sizing and SL/TP computation for XAUUSD."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class TradeParameters:
    """Computed trade parameters based on ATR and risk management."""

    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    risk_reward_ratio: float
    atr_value: float
    sl_distance: float
    tp_distance: float


def compute_trade_params(
    action: str,
    current_price: float,
    atr_value: float,
    account_balance: float,
    *,
    risk_percent: float = 1.0,
    sl_atr_multiplier: float = 1.5,
    tp_atr_multiplier: float = 3.0,
    min_volume: float = 0.01,
    max_volume: float = 1.0,
    contract_size: float = 100.0,
) -> TradeParameters:
    """Compute SL, TP, and position size for a trade based on ATR.

    Args:
        action: "buy" or "sell"
        current_price: Current market price
        atr_value: Current ATR value (same unit as price)
        account_balance: Account equity/balance
        risk_percent: % of account to risk per trade (1.0 = 1%)
        sl_atr_multiplier: ATR multiplier for stop loss distance
        tp_atr_multiplier: ATR multiplier for take profit distance
        min_volume: Minimum lot size
        max_volume: Maximum lot size
        contract_size: Contract size (100 oz for XAUUSD standard lot)

    Returns:
        TradeParameters with computed SL, TP, and lot size
    """
    if atr_value <= 0:
        raise ValueError("ATR value must be positive")
    if account_balance <= 0:
        raise ValueError("Account balance must be positive")

    sl_distance = atr_value * sl_atr_multiplier
    tp_distance = atr_value * tp_atr_multiplier

    if action == "buy":
        stop_loss = current_price - sl_distance
        take_profit = current_price + tp_distance
    elif action == "sell":
        stop_loss = current_price + sl_distance
        take_profit = current_price - tp_distance
    else:
        raise ValueError(f"action must be 'buy' or 'sell', got: {action}")

    risk_amount = account_balance * (risk_percent / 100.0)
    risk_per_lot = sl_distance * contract_size
    if risk_per_lot > 0:
        raw_volume = risk_amount / risk_per_lot
    else:
        raw_volume = min_volume

    position_size = max(min_volume, min(max_volume, round(raw_volume, 2)))
    risk_reward = tp_distance / sl_distance if sl_distance > 0 else 0.0

    return TradeParameters(
        entry_price=current_price,
        stop_loss=round(stop_loss, 2),
        take_profit=round(take_profit, 2),
        position_size=position_size,
        risk_reward_ratio=round(risk_reward, 2),
        atr_value=atr_value,
        sl_distance=round(sl_distance, 2),
        tp_distance=round(tp_distance, 2),
    )


def extract_atr_from_indicators(
    indicators: Dict[str, Dict[str, Any]],
) -> Optional[float]:
    """Try to extract ATR value from an indicators snapshot."""
    for indicator_name in ("atr14", "atr", "atr20"):
        payload = indicators.get(indicator_name)
        if not isinstance(payload, dict):
            continue
        for field in ("atr", "value"):
            val = payload.get(field)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
    return None
