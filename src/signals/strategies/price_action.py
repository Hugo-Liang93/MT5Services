from __future__ import annotations

from typing import Any, Optional

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import _resolve_indicator_value


def _recent_bars(context: SignalContext) -> list[Any]:
    payload = context.metadata.get("recent_bars")
    return list(payload) if isinstance(payload, list) else []


def _bar_value(bar: Any, field: str) -> Optional[float]:
    value = getattr(bar, field, None)
    if value is None and isinstance(bar, dict):
        value = bar.get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ohlc(bar: Any) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    return (
        _bar_value(bar, "open"),
        _bar_value(bar, "high"),
        _bar_value(bar, "low"),
        _bar_value(bar, "close"),
    )


class PriceActionReversal:
    """Detect simple price-action reversal patterns normalized by ATR."""

    name = "price_action_reversal"
    required_indicators = ("atr14",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 0.30,
        RegimeType.RANGING: 0.90,
        RegimeType.BREAKOUT: 0.60,
        RegimeType.UNCERTAIN: 0.70,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        atr, atr_name = _resolve_indicator_value(
            context.indicators, (("atr14", "atr"), ("atr", "atr"))
        )
        bars = _recent_bars(context)
        used = [atr_name] if atr_name else ["atr14"]

        if atr is None or atr <= 0 or not bars:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_price_action_context",
                used_indicators=used,
            )

        candidates: list[tuple[str, float, str, dict[str, Any]]] = []
        current = bars[-1]
        current_open, current_high, current_low, current_close = _ohlc(current)
        if None in {current_open, current_high, current_low, current_close}:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_bar_ohlc",
                used_indicators=used,
            )

        body = abs(current_close - current_open)
        upper_wick = max(current_high - max(current_open, current_close), 0.0)
        lower_wick = max(min(current_open, current_close) - current_low, 0.0)

        if lower_wick >= max(body * 2.0, atr * 0.6):
            candidates.append(
                (
                    "buy",
                    min(0.52 + min(lower_wick / atr, 2.0) * 0.15, 0.9),
                    "bullish_pin_bar",
                    {"body": body, "lower_wick": lower_wick, "upper_wick": upper_wick},
                )
            )
        if upper_wick >= max(body * 2.0, atr * 0.6):
            candidates.append(
                (
                    "sell",
                    min(0.52 + min(upper_wick / atr, 2.0) * 0.15, 0.9),
                    "bearish_pin_bar",
                    {"body": body, "lower_wick": lower_wick, "upper_wick": upper_wick},
                )
            )

        if len(bars) >= 2:
            prev_open, _prev_high, _prev_low, prev_close = _ohlc(bars[-2])
            if None not in {prev_open, prev_close}:
                if (
                    prev_close < prev_open
                    and current_close > current_open
                    and current_open <= prev_close
                    and current_close >= prev_open
                ):
                    candidates.append(
                        (
                            "buy",
                            0.72,
                            "bullish_engulfing",
                            {"prev_open": prev_open, "prev_close": prev_close},
                        )
                    )
                if (
                    prev_close > prev_open
                    and current_close < current_open
                    and current_open >= prev_close
                    and current_close <= prev_open
                ):
                    candidates.append(
                        (
                            "sell",
                            0.72,
                            "bearish_engulfing",
                            {"prev_open": prev_open, "prev_close": prev_close},
                        )
                    )

        if len(bars) >= 3:
            _mother_open, mother_high, mother_low, _mother_close = _ohlc(bars[-3])
            _prev_open, prev_high, prev_low, _prev_close = _ohlc(bars[-2])
            if None not in {mother_high, mother_low, prev_high, prev_low}:
                inside_bar = prev_high <= mother_high and prev_low >= mother_low
                if inside_bar and current_close > mother_high:
                    candidates.append(
                        (
                            "buy",
                            0.6,
                            "inside_bar_breakout_up",
                            {"mother_high": mother_high, "mother_low": mother_low},
                        )
                    )
                if inside_bar and current_close < mother_low:
                    candidates.append(
                        (
                            "sell",
                            0.6,
                            "inside_bar_breakout_down",
                            {"mother_high": mother_high, "mother_low": mother_low},
                        )
                    )

        if not candidates:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.1,
                reason="no_price_action_pattern",
                used_indicators=used,
                metadata={"atr": atr},
            )

        action, confidence, pattern, extra = max(candidates, key=lambda item: item[1])
        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=pattern,
            used_indicators=used,
            metadata={
                "atr": atr,
                "pattern": pattern,
                "open": current_open,
                "high": current_high,
                "low": current_low,
                "close": current_close,
                **extra,
            },
        )
