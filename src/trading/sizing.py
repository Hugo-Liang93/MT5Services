"""ATR-based trade sizing and SL/TP computation for XAUUSD."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


TIMEFRAME_SL_TP: dict[str, dict[str, float]] = {
    "M1": {"sl_atr_mult": 1.0, "tp_atr_mult": 2.0},
    "M5": {"sl_atr_mult": 1.2, "tp_atr_mult": 2.5},
    "M15": {"sl_atr_mult": 1.3, "tp_atr_mult": 2.8},
    "H1": {"sl_atr_mult": 1.5, "tp_atr_mult": 3.0},
    "D1": {"sl_atr_mult": 2.0, "tp_atr_mult": 4.0},
}

# 时间框架差异化风险百分比（乘数）：M1 更保守，H1 可更激进
# 实际 risk_pct = base_risk_percent × multiplier
TIMEFRAME_RISK_MULTIPLIER: dict[str, float] = {
    "M1": 0.50,   # M1 噪声大，半仓
    "M5": 0.75,   # M5 适中偏保守
    "M15": 1.00,  # M15 基准
    "H1": 1.20,   # H1 信号更可靠，可稍放大
    "D1": 1.50,   # D1 信号最稳定，允许更大仓位
}


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
    timeframe: Optional[str] = None,
    risk_percent: float = 1.0,
    sl_atr_multiplier: float = 1.5,
    tp_atr_multiplier: float = 3.0,
    min_volume: float = 0.01,
    max_volume: float = 1.0,
    contract_size: float = 100.0,
    timeframe_risk_overrides: Optional[Dict[str, float]] = None,
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

    resolved_sl_multiplier, resolved_tp_multiplier = resolve_timeframe_sl_tp(
        timeframe,
        default_sl=sl_atr_multiplier,
        default_tp=tp_atr_multiplier,
    )
    sl_distance = atr_value * resolved_sl_multiplier
    tp_distance = atr_value * resolved_tp_multiplier

    if action == "buy":
        stop_loss = current_price - sl_distance
        take_profit = current_price + tp_distance
    elif action == "sell":
        stop_loss = current_price + sl_distance
        take_profit = current_price - tp_distance
    else:
        raise ValueError(f"action must be 'buy' or 'sell', got: {action}")

    effective_risk_pct = risk_percent * resolve_timeframe_risk_multiplier(
        timeframe, overrides=timeframe_risk_overrides,
    )
    risk_amount = account_balance * (effective_risk_pct / 100.0)
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


def resolve_timeframe_risk_multiplier(
    timeframe: Optional[str],
    *,
    overrides: Optional[Dict[str, float]] = None,
) -> float:
    """Return the risk-percent multiplier for a given timeframe (default 1.0).

    If *overrides* is provided (from signal.ini ``[timeframe_risk]``), it takes
    precedence over the built-in ``TIMEFRAME_RISK_MULTIPLIER`` defaults.
    """
    if not timeframe:
        return 1.0
    tf = str(timeframe).strip().upper()
    if overrides and tf in overrides:
        return overrides[tf]
    return TIMEFRAME_RISK_MULTIPLIER.get(tf, 1.0)


def resolve_timeframe_sl_tp(
    timeframe: Optional[str],
    *,
    default_sl: float = 1.5,
    default_tp: float = 3.0,
) -> tuple[float, float]:
    if not timeframe:
        return default_sl, default_tp
    profile = TIMEFRAME_SL_TP.get(str(timeframe).strip().upper())
    if not profile:
        return default_sl, default_tp
    return (
        float(profile.get("sl_atr_mult", default_sl)),
        float(profile.get("tp_atr_mult", default_tp)),
    )
