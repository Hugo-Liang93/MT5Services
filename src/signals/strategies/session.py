from __future__ import annotations

from typing import Any

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import _resolve_indicator_value, get_tf_param


def _market_structure(context: SignalContext) -> dict[str, Any]:
    payload = context.metadata.get("market_structure")
    return payload if isinstance(payload, dict) else {}


class AsianRangeBreakout:
    """亚盘区间突破策略 — XAUUSD 日内经典模式。

    原理：XAUUSD 亚盘（UTC 0:00-7:00）通常形成窄幅区间，
    伦敦开盘后方向性突破概率约 65-70%。

    信号逻辑：
    - 仅在 london 时段评估（亚盘形成区间，伦敦开盘突破）
    - close 突破亚盘 high → buy
    - close 突破亚盘 low → sell
    - 区间太窄（< min_range_atr × ATR）或太宽（> max_range_atr × ATR）→ hold
    """

    name = "asian_range_breakout"
    category = "session"
    required_indicators = ("atr14",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 0.70,
        RegimeType.RANGING: 0.20,
        RegimeType.BREAKOUT: 1.00,
        RegimeType.UNCERTAIN: 0.50,
    }

    def __init__(
        self,
        *,
        min_range_atr: float = 0.3,
        max_range_atr: float = 2.0,
    ) -> None:
        self._min_range_atr = min_range_atr
        self._max_range_atr = max_range_atr

    def evaluate(self, context: SignalContext) -> SignalDecision:
        atr, atr_name = _resolve_indicator_value(
            context.indicators, (("atr14", "atr"), ("atr", "atr"))
        )
        used = [atr_name] if atr_name else ["atr14"]

        if atr is None or atr <= 0:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_atr",
                used_indicators=used,
            )

        # 仅在 london 时段评估
        sessions = context.metadata.get("session_buckets")
        session = sessions[0] if isinstance(sessions, list) and sessions else "unknown"
        if session != "london":
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason=f"not_london_session:{session}",
                used_indicators=used,
            )

        # 从 market_structure 获取亚盘区间
        structure = _market_structure(context)
        asia_high = structure.get("asia_range_high")
        asia_low = structure.get("asia_range_low")

        if asia_high is None or asia_low is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="no_asia_range_data",
                used_indicators=used,
            )

        try:
            asia_high_val = float(asia_high)
            asia_low_val = float(asia_low)
        except (TypeError, ValueError):
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="invalid_asia_range_data",
                used_indicators=used,
            )

        asia_range = asia_high_val - asia_low_val
        if asia_range <= 0:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="zero_asia_range",
                used_indicators=used,
            )

        # 区间宽度过滤（ATR 倍数）
        range_atr_ratio = asia_range / atr
        tf = context.timeframe
        min_atr = get_tf_param(self, "min_range_atr", tf, self._min_range_atr)
        max_atr = get_tf_param(self, "max_range_atr", tf, self._max_range_atr)
        if range_atr_ratio < min_atr:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.1,
                reason=f"asia_range_too_narrow:{range_atr_ratio:.2f}<{min_atr}",
                used_indicators=used,
                metadata={"asia_range": asia_range, "range_atr_ratio": range_atr_ratio},
            )
        if range_atr_ratio > max_atr:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.1,
                reason=f"asia_range_too_wide:{range_atr_ratio:.2f}>{max_atr}",
                used_indicators=used,
                metadata={"asia_range": asia_range, "range_atr_ratio": range_atr_ratio},
            )

        close_price = context.metadata.get("close_price")
        try:
            close_val = float(close_price) if close_price is not None else None
        except (TypeError, ValueError):
            close_val = None

        if close_val is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="no_close_price",
                used_indicators=used,
            )

        # 突破判断
        if close_val > asia_high_val:
            action = "buy"
            penetration = (close_val - asia_high_val) / atr
        elif close_val < asia_low_val:
            action = "sell"
            penetration = (asia_low_val - close_val) / atr
        else:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.15,
                reason="price_within_asia_range",
                used_indicators=used,
                metadata={
                    "close": close_val,
                    "asia_high": asia_high_val,
                    "asia_low": asia_low_val,
                },
            )

        # 置信度：基础 0.55 + 穿透深度加成 + 区间适中加成
        confidence = 0.55 + min(penetration * 0.15, 0.20)
        # 区间适中（0.8-1.5 ATR）加成
        if 0.8 <= range_atr_ratio <= 1.5:
            confidence += 0.08

        # 市场结构对齐加成
        structure_bias = str(structure.get("structure_bias") or "neutral")
        if action == "buy" and structure_bias in {"bullish_breakout", "expansion"}:
            confidence += 0.10
        elif action == "sell" and structure_bias in {"bearish_breakout", "expansion"}:
            confidence += 0.10

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=min(confidence, 0.92),
            reason=f"asia_breakout:{action},penetration={penetration:.3f},range_atr={range_atr_ratio:.2f}",
            used_indicators=used,
            metadata={
                "asia_high": asia_high_val,
                "asia_low": asia_low_val,
                "asia_range": asia_range,
                "range_atr_ratio": range_atr_ratio,
                "penetration": penetration,
                "close": close_val,
                "structure_bias": structure_bias,
            },
        )


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

    def __init__(
        self,
        *,
        london_min_atr_pct: float = 0.00045,
        other_min_atr_pct: float = 0.00035,
    ) -> None:
        self._london_min_atr_pct = london_min_atr_pct
        self._other_min_atr_pct = other_min_atr_pct

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
                direction="hold",
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
                direction="hold",
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
                direction="hold",
                confidence=0.1,
                reason="flat_supertrend_direction",
                used_indicators=used or ["atr14", "supertrend14"],
            )

        tf = context.timeframe
        london_min = get_tf_param(self, "london_min_atr_pct", tf, self._london_min_atr_pct)
        other_min = get_tf_param(self, "other_min_atr_pct", tf, self._other_min_atr_pct)
        min_atr_pct = london_min if session == "london" else other_min
        if atr_pct < min_atr_pct:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
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
            direction=action,
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
