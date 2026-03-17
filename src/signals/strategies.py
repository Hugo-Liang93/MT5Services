from __future__ import annotations

from typing import Protocol

from .models import SignalContext, SignalDecision


class SignalStrategy(Protocol):
    name: str

    def evaluate(self, context: SignalContext) -> SignalDecision:
        ...


class SmaTrendStrategy:
    """Simple trend signal based on fast/slow SMA relation."""

    name = "sma_trend"

    def evaluate(self, context: SignalContext) -> SignalDecision:
        fast = context.indicators.get("sma_fast", {}).get("value")
        slow = context.indicators.get("sma_slow", {}).get("value")
        if fast is None or slow is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=["sma_fast", "sma_slow"],
            )

        spread = float(fast) - float(slow)
        if spread > 0:
            action = "buy"
        elif spread < 0:
            action = "sell"
        else:
            action = "hold"

        confidence = min(abs(spread), 1.0)
        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"sma_spread={spread:.6f}",
            used_indicators=["sma_fast", "sma_slow"],
            metadata={"spread": spread},
        )


class RsiReversionStrategy:
    """Mean reversion signal based on RSI overbought/oversold zones."""

    name = "rsi_reversion"

    def evaluate(self, context: SignalContext) -> SignalDecision:
        rsi_value = context.indicators.get("rsi", {}).get("value")
        if rsi_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:rsi",
                used_indicators=["rsi"],
            )

        rsi = float(rsi_value)
        if rsi <= 30:
            action = "buy"
            confidence = min((30 - rsi) / 30 + 0.4, 1.0)
        elif rsi >= 70:
            action = "sell"
            confidence = min((rsi - 70) / 30 + 0.4, 1.0)
        else:
            action = "hold"
            confidence = 0.2

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"rsi={rsi:.2f}",
            used_indicators=["rsi"],
            metadata={"rsi": rsi},
        )
