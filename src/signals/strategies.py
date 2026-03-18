from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Protocol

from .models import SignalContext, SignalDecision


class SignalStrategy(Protocol):
    name: str
    required_indicators: tuple[str, ...]
    # Scopes this strategy wants to receive snapshots for.
    # "confirmed" = bar-close snapshots only (all indicators available).
    # "intrabar"  = live partial-bar snapshots (intrabar_eligible indicators only).
    # Defaults to both if not declared on a concrete class.
    preferred_scopes: tuple[str, ...]

    def evaluate(self, context: SignalContext) -> SignalDecision:
        ...


def _resolve_indicator_value(
    indicators: Dict[str, Dict[str, Any]],
    candidates: Iterable[tuple[str, str]],
) -> tuple[float | None, str | None]:
    for indicator_name, field_name in candidates:
        payload = indicators.get(indicator_name)
        if not isinstance(payload, dict):
            continue
        value = payload.get(field_name)
        if value is None:
            continue
        try:
            return float(value), indicator_name
        except (TypeError, ValueError):
            continue
    return None, None


class SmaTrendStrategy:
    """Simple trend signal based on fast/slow SMA relation.

    Uses bar-close snapshots only.  SMA/EMA crossovers are meaningful only
    when a bar has *closed* — intrabar MA values oscillate continuously as
    the live price moves, generating excessive false crossover noise.
    """

    name = "sma_trend"
    required_indicators = ("sma20", "ema50")
    preferred_scopes = ("confirmed",)

    def evaluate(self, context: SignalContext) -> SignalDecision:
        fast, fast_name = _resolve_indicator_value(
            context.indicators,
            (
                ("sma_fast", "value"),
                ("sma_fast", "sma"),
                ("sma20", "sma"),
                ("sma20", "value"),
            ),
        )
        slow, slow_name = _resolve_indicator_value(
            context.indicators,
            (
                ("sma_slow", "value"),
                ("sma_slow", "sma"),
                ("ema50", "ema"),
                ("ema50", "value"),
            ),
        )
        used_indicators = [name for name in (fast_name, slow_name) if name]
        if fast is None or slow is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used_indicators or ["sma20", "ema50"],
            )

        spread = fast - slow
        if spread > 0:
            action = "buy"
        elif spread < 0:
            action = "sell"
        else:
            action = "hold"

        relative_spread = spread / slow if slow else 0.0
        confidence = min(abs(relative_spread) * 100, 1.0)
        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"sma_spread={spread:.6f},relative={relative_spread:.6f}",
            used_indicators=used_indicators or ["sma20", "ema50"],
            metadata={
                "spread": spread,
                "relative_spread": relative_spread,
                "fast_indicator": fast_name or "sma20",
                "slow_indicator": slow_name or "ema50",
            },
        )


class RsiReversionStrategy:
    """Mean reversion signal based on RSI overbought/oversold zones.

    Receives both intrabar and confirmed snapshots.  RSI extreme readings
    (≤30 oversold, ≥70 overbought) are meaningful in real time — the deepest
    extreme often occurs mid-bar before price reverts.  Bar-close confirmation
    verifies the reading was sustained through candle close.
    """

    name = "rsi_reversion"
    required_indicators = ("rsi14",)
    preferred_scopes = ("intrabar", "confirmed")

    def evaluate(self, context: SignalContext) -> SignalDecision:
        rsi_value, rsi_name = _resolve_indicator_value(
            context.indicators,
            (
                ("rsi", "value"),
                ("rsi", "rsi"),
                ("rsi14", "rsi"),
                ("rsi14", "value"),
            ),
        )
        if rsi_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:rsi",
                used_indicators=[rsi_name] if rsi_name else ["rsi14"],
            )

        rsi = rsi_value
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
            used_indicators=[rsi_name] if rsi_name else ["rsi14"],
            metadata={"rsi": rsi, "rsi_indicator": rsi_name or "rsi14"},
        )


class BollingerBreakoutStrategy:
    """Mean reversion signal based on Bollinger Band breakout.

    Price touching lower band -> buy (expect reversion to mean).
    Price touching upper band -> sell (expect reversion to mean).
    Band width (squeeze) is used to boost confidence on breakouts after compression.

    Receives both intrabar and confirmed snapshots.  Bollinger Bands are
    computed from historical closes so the band levels are stable intrabar;
    only the live close price varies.  Price touching the bands is a real-time
    event — waiting for bar close often means the price has already recovered
    to the middle band, losing the entry edge.
    """

    name = "bollinger_breakout"
    required_indicators = ("boll20",)
    preferred_scopes = ("intrabar", "confirmed")

    def evaluate(self, context: SignalContext) -> SignalDecision:
        upper, upper_name = _resolve_indicator_value(
            context.indicators,
            (
                ("boll20", "bb_upper"),
                ("bollinger20", "bb_upper"),
                ("bollinger", "bb_upper"),
            ),
        )
        lower, lower_name = _resolve_indicator_value(
            context.indicators,
            (
                ("boll20", "bb_lower"),
                ("bollinger20", "bb_lower"),
                ("bollinger", "bb_lower"),
            ),
        )
        mid, _ = _resolve_indicator_value(
            context.indicators,
            (
                ("boll20", "bb_mid"),
                ("bollinger20", "bb_mid"),
                ("bollinger", "bb_mid"),
            ),
        )
        close_value = self._get_close(context)

        used = [n for n in (upper_name, lower_name) if n]
        if upper is None or lower is None or mid is None or close_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["boll20"],
            )

        band_width = (upper - lower) / mid if mid else 0.0
        squeeze_bonus = max(0.0, 0.3 - band_width * 10) if band_width < 0.03 else 0.0

        if close_value <= lower:
            action = "buy"
            penetration = (lower - close_value) / (upper - lower) if (upper - lower) else 0
            confidence = min(0.5 + penetration + squeeze_bonus, 1.0)
        elif close_value >= upper:
            action = "sell"
            penetration = (close_value - upper) / (upper - lower) if (upper - lower) else 0
            confidence = min(0.5 + penetration + squeeze_bonus, 1.0)
        else:
            action = "hold"
            confidence = 0.1

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"bb_close={close_value:.2f},upper={upper:.2f},lower={lower:.2f}",
            used_indicators=used or ["boll20"],
            metadata={
                "close": close_value,
                "bb_upper": upper,
                "bb_lower": lower,
                "bb_mid": mid,
                "band_width": band_width,
                "squeeze_bonus": squeeze_bonus,
            },
        )

    @staticmethod
    def _get_close(context: SignalContext) -> Optional[float]:
        """Try to extract close price from indicators metadata."""
        for indicator_name in ("boll20", "bollinger20", "bollinger", "close", "price"):
            payload = context.indicators.get(indicator_name)
            if isinstance(payload, dict):
                for field in ("close", "value", "last"):
                    val = payload.get(field)
                    if val is not None:
                        try:
                            return float(val)
                        except (TypeError, ValueError):
                            continue
        close_val = context.metadata.get("close") or context.metadata.get("last_close")
        if close_val is not None:
            try:
                return float(close_val)
            except (TypeError, ValueError):
                pass
        return None


class MultiTimeframeConfirmStrategy:
    """Confirms signals when direction aligns across timeframes.

    Uses existing indicator snapshots to check if the same directional signal
    exists on a higher timeframe. Only produces signals when lower TF and
    higher TF agree on direction.

    Uses bar-close snapshots only.  The higher-timeframe direction is read from
    confirmed signal state (RuntimeSignalState.confirmed_state), which is only
    updated after a higher-TF bar closes.  Evaluating on intrabar snapshots
    yields htf_direction=None almost always, producing only wasteful hold decisions.
    """

    name = "mtf_confirm"
    required_indicators = ("sma20", "ema50")
    preferred_scopes = ("confirmed",)

    def __init__(
        self,
        *,
        state_reader: Optional[Any] = None,
    ):
        self._state_reader = state_reader

    def evaluate(self, context: SignalContext) -> SignalDecision:
        fast, fast_name = _resolve_indicator_value(
            context.indicators,
            (("sma_fast", "value"), ("sma_fast", "sma"), ("sma20", "sma"), ("sma20", "value")),
        )
        slow, slow_name = _resolve_indicator_value(
            context.indicators,
            (("sma_slow", "value"), ("sma_slow", "sma"), ("ema50", "ema"), ("ema50", "value")),
        )
        used = [n for n in (fast_name, slow_name) if n]
        if fast is None or slow is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["sma20", "ema50"],
            )

        local_direction = "buy" if fast > slow else ("sell" if fast < slow else "hold")
        htf_direction = self._get_htf_direction(context)

        if local_direction == "hold" or htf_direction is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.1,
                reason=f"local={local_direction},htf={htf_direction or 'unknown'}",
                used_indicators=used or ["sma20", "ema50"],
                metadata={"local_direction": local_direction, "htf_direction": htf_direction},
            )

        if local_direction == htf_direction:
            relative_spread = (fast - slow) / slow if slow else 0
            confidence = min(abs(relative_spread) * 150, 1.0)
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action=local_direction,
                confidence=max(confidence, 0.6),
                reason=f"mtf_aligned:{local_direction},htf={htf_direction}",
                used_indicators=used or ["sma20", "ema50"],
                metadata={
                    "local_direction": local_direction,
                    "htf_direction": htf_direction,
                    "relative_spread": relative_spread,
                },
            )

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action="hold",
            confidence=0.15,
            reason=f"mtf_conflict:local={local_direction},htf={htf_direction}",
            used_indicators=used or ["sma20", "ema50"],
            metadata={"local_direction": local_direction, "htf_direction": htf_direction},
        )

    def _get_htf_direction(self, context: SignalContext) -> Optional[str]:
        """Read higher timeframe direction from state reader or metadata."""
        htf = context.metadata.get("htf_direction")
        if htf in ("buy", "sell", "hold"):
            return htf
        if self._state_reader is None:
            return None
        try:
            states = self._state_reader
            htf_key = context.metadata.get("htf_key")
            if htf_key and isinstance(states, dict):
                state = states.get(htf_key)
                if state:
                    confirmed = getattr(state, "confirmed_state", "idle")
                    if "buy" in confirmed:
                        return "buy"
                    if "sell" in confirmed:
                        return "sell"
        except Exception:
            pass
        return None
