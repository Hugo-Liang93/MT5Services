"""ATR-based trade sizing and SL/TP computation for XAUUSD."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.signals.evaluation.regime import RegimeType

TIMEFRAME_SL_TP: dict[str, dict[str, float]] = {
    "M5": {"sl_atr_mult": 1.8, "tp_atr_mult": 2.5},
    "M15": {"sl_atr_mult": 1.8, "tp_atr_mult": 2.5},
    "M30": {"sl_atr_mult": 2.0, "tp_atr_mult": 2.5},
    "H1": {"sl_atr_mult": 2.0, "tp_atr_mult": 2.5},
    "H4": {"sl_atr_mult": 2.2, "tp_atr_mult": 3.0},
    "D1": {"sl_atr_mult": 2.5, "tp_atr_mult": 3.5},
}

# 时间框架差异化风险百分比（乘数）
# 实际 risk_pct = base_risk_percent × multiplier
TIMEFRAME_RISK_MULTIPLIER: dict[str, float] = {
    "M5": 0.50,   # M5 噪声大，SL 已加宽，仓位需缩小
    "M15": 0.75,  # M15 中等
    "M30": 0.90,  # M30 确认层
    "H1": 1.00,   # H1 基准
    "H4": 1.20,   # H4 过滤层，信号稳定
    "D1": 1.50,   # D1 大势层，允许更大仓位
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


@dataclass(frozen=True)
class RegimeSizing:
    tp_trending: float = 1.20
    tp_ranging: float = 0.80
    tp_breakout: float = 1.10
    tp_uncertain: float = 1.00
    sl_trending: float = 1.00
    sl_ranging: float = 0.90
    sl_breakout: float = 1.10
    sl_uncertain: float = 1.00


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
    regime: Optional[str] = None,
    regime_sizing: Optional[RegimeSizing] = None,
    digits: int = 2,
    volume_step: float = 0.01,
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
    regime_sl_multiplier, regime_tp_multiplier = resolve_regime_sl_tp_multiplier(
        regime,
        regime_sizing=regime_sizing,
    )
    resolved_sl_multiplier *= regime_sl_multiplier
    resolved_tp_multiplier *= regime_tp_multiplier
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

    if stop_loss <= 0:
        raise ValueError(
            f"Computed stop_loss={stop_loss:.2f} is non-positive "
            f"(price={current_price}, sl_distance={sl_distance})"
        )
    if take_profit <= 0:
        raise ValueError(
            f"Computed take_profit={take_profit:.2f} is non-positive "
            f"(price={current_price}, tp_distance={tp_distance})"
        )

    effective_risk_pct = risk_percent * resolve_timeframe_risk_multiplier(
        timeframe, overrides=timeframe_risk_overrides,
    )
    risk_amount = account_balance * (effective_risk_pct / 100.0)
    risk_per_lot = sl_distance * contract_size
    if risk_per_lot > 0:
        raw_volume = risk_amount / risk_per_lot
    else:
        raw_volume = min_volume

    # 对齐 volume 到品种的 volume_step（避免 MT5 拒绝非法手数）
    if volume_step > 0:
        aligned = round(raw_volume / volume_step) * volume_step
    else:
        aligned = round(raw_volume, 2)
    position_size = max(min_volume, min(max_volume, round(aligned, 8)))
    risk_reward = tp_distance / sl_distance if sl_distance > 0 else 0.0

    return TradeParameters(
        entry_price=round(current_price, digits),
        stop_loss=round(stop_loss, digits),
        take_profit=round(take_profit, digits),
        position_size=position_size,
        risk_reward_ratio=round(risk_reward, 2),
        atr_value=atr_value,
        sl_distance=round(sl_distance, digits),
        tp_distance=round(tp_distance, digits),
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


def resolve_regime_sl_tp_multiplier(
    regime: Optional[str],
    *,
    regime_sizing: Optional[RegimeSizing] = None,
) -> tuple[float, float]:
    """根据行情 Regime 返回 SL/TP 缩放倍数（默认 1.0, 1.0）。"""
    if not regime:
        return 1.0, 1.0
    sizing = regime_sizing or RegimeSizing()
    key = str(regime).strip().lower()
    if key.startswith("regimetype."):
        key = key.split(".", 1)[1]
    if key == RegimeType.TRENDING.value:
        return float(sizing.sl_trending), float(sizing.tp_trending)
    if key == RegimeType.RANGING.value:
        return float(sizing.sl_ranging), float(sizing.tp_ranging)
    if key == RegimeType.BREAKOUT.value:
        return float(sizing.sl_breakout), float(sizing.tp_breakout)
    if key == RegimeType.UNCERTAIN.value:
        return float(sizing.sl_uncertain), float(sizing.tp_uncertain)
    return 1.0, 1.0
