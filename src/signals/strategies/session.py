from __future__ import annotations

from typing import Any

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import _resolve_indicator_value


def _market_structure(context: SignalContext) -> dict[str, Any]:
    payload = context.metadata.get("market_structure")
    return payload if isinstance(payload, dict) else {}


class SessionMomentumBias:
    """Bias momentum entries by active trading session."""

    name = "session_momentum"
    category = "session"
    required_indicators = ("atr14", "supertrend14")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 0.90,
        RegimeType.RANGING: 0.50,
        RegimeType.BREAKOUT: 0.80,
        RegimeType.UNCERTAIN: 0.60,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        atr, atr_name = _resolve_indicator_value(
            context.indicators, (("atr14", "atr"), ("atr", "atr"))
        )
        direction, st_name = _resolve_indicator_value(
            context.indicators,
            (
                ("supertrend14", "direction"),
                ("supertrend", "direction"),
            ),
        )
        used = [name for name in (atr_name, st_name) if name]
        if atr is None or direction is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["atr14", "supertrend14"],
            )

        sessions = context.metadata.get("session_buckets")
        session = sessions[0] if isinstance(sessions, list) and sessions else "unknown"
        close_price = context.metadata.get("close_price")
        try:
            close_value = float(close_price) if close_price is not None else None
        except (TypeError, ValueError):
            close_value = None
        atr_pct = (atr / close_value) if close_value and close_value > 0 else 0.0

        structure = _market_structure(context)
        structure_bias = str(structure.get("structure_bias") or "neutral")
        breakout_state = str(structure.get("breakout_state") or "none")
        first_pullback_state = str(structure.get("first_pullback_state") or "none")
        sweep_confirmation_state = str(
            structure.get("sweep_confirmation_state") or "none"
        )

        if session == "asia":
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.15,
                reason=f"asia_session_low_momentum:atr_pct={atr_pct:.5f}",
                used_indicators=used or ["atr14", "supertrend14"],
                metadata={
                    "session": session,
                    "atr": atr,
                    "atr_pct": atr_pct,
                    "structure_bias": structure_bias,
                },
            )

        if direction > 0:
            action = "buy"
            aligned = (
                breakout_state.startswith("above_")
                or first_pullback_state.startswith("bullish_")
                or sweep_confirmation_state.startswith("bullish_")
                or structure_bias in {"bullish_breakout", "bullish_pullback", "expansion"}
            )
        elif direction < 0:
            action = "sell"
            aligned = (
                breakout_state.startswith("below_")
                or first_pullback_state.startswith("bearish_")
                or sweep_confirmation_state.startswith("bearish_")
                or structure_bias in {"bearish_breakout", "bearish_pullback", "expansion"}
            )
        else:
            action = "hold"
            aligned = False

        if action == "hold":
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.1,
                reason="flat_supertrend_direction",
                used_indicators=used or ["atr14", "supertrend14"],
            )

        min_atr_pct = 0.00045 if session == "london" else 0.00035
        if atr_pct < min_atr_pct:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.15,
                reason=f"atr_too_low_for_session:{atr_pct:.5f}<{min_atr_pct:.5f}",
                used_indicators=used or ["atr14", "supertrend14"],
                metadata={"session": session, "atr_pct": atr_pct},
            )

        confidence = 0.48 + min(atr_pct * 120, 0.18)
        if session == "london":
            confidence += 0.06
        elif session == "new_york":
            confidence += 0.08
        if aligned:
            confidence += 0.12

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=min(confidence, 0.92),
            reason=f"session_bias:{session},structure={structure_bias},atr_pct={atr_pct:.5f}",
            used_indicators=used or ["atr14", "supertrend14"],
            metadata={
                "session": session,
                "atr": atr,
                "atr_pct": atr_pct,
                "structure_bias": structure_bias,
                "breakout_state": breakout_state,
                "first_pullback_state": first_pullback_state,
                "sweep_confirmation_state": sweep_confirmation_state,
            },
        )
