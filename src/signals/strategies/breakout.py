"""突破策略

包含基于波动率压缩/通道突破的策略，适合 BREAKOUT 行情。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import _resolve_indicator_value

logger = logging.getLogger(__name__)


def _market_structure(context: SignalContext) -> dict[str, Any]:
    payload = context.metadata.get("market_structure")
    return payload if isinstance(payload, dict) else {}


def _recent_bars(context: SignalContext) -> list[Any]:
    payload = context.metadata.get("recent_bars")
    return list(payload) if isinstance(payload, list) else []


def _bar_price(bar: Any, field: str) -> Optional[float]:
    value = getattr(bar, field, None)
    if value is None and isinstance(bar, dict):
        value = bar.get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    category = "breakout"
    required_indicators = ("boll20",)
    preferred_scopes = ("intrabar", "confirmed")

    regime_affinity = {
        RegimeType.TRENDING:  0.30,  # 趋势中价格走布林带边缘属正常，触带≠反转
        RegimeType.RANGING:   0.85,  # 震荡市触及上下轨是典型均值回归机会
        RegimeType.BREAKOUT:  1.00,  # 价格突破布林带 = 波动率扩张主要信号
        RegimeType.UNCERTAIN: 0.60,
    }

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
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["boll20"],
            )

        band_width = (upper - lower) / mid if mid else 0.0
        squeeze_bonus = max(0.0, 0.3 - band_width * 10) if band_width < 0.03 else 0.0

        # Band position: 价格在通道中的相对位置 (0=下轨, 1=上轨)
        band_range = upper - lower
        band_position = (
            (close_value - lower) / band_range if band_range > 0 else 0.5
        )
        # BB 宽度过窄（挤压中）→ 不适合反转交易，应等待释放
        bb_too_narrow = band_width < 0.003

        if close_value <= lower:
            action = "buy"
            penetration = (lower - close_value) / band_range if band_range > 0 else 0
            confidence = min(0.5 + penetration + squeeze_bonus, 1.0)
            # 极端穿透但 BB 过窄 → 可能是挤压突破而非反转
            if bb_too_narrow:
                confidence *= 0.70
        elif close_value >= upper:
            action = "sell"
            penetration = (close_value - upper) / band_range if band_range > 0 else 0
            confidence = min(0.5 + penetration + squeeze_bonus, 1.0)
            if bb_too_narrow:
                confidence *= 0.70
        else:
            action = "hold"
            confidence = 0.1

        structure = _market_structure(context)
        reclaim_state = str(structure.get("reclaim_state") or "none")
        sweep_confirmation_state = str(
            structure.get("sweep_confirmation_state") or "none"
        )
        structure_bias = str(structure.get("structure_bias") or "neutral")
        structure_note = "none"
        if action == "buy":
            if sweep_confirmation_state.startswith("bullish_"):
                confidence = min(confidence + 0.22, 1.0)
                structure_note = sweep_confirmation_state
            elif reclaim_state.startswith("bullish_"):
                confidence = min(confidence + 0.15, 1.0)
                structure_note = reclaim_state
            elif structure_bias in {"bearish_breakout", "bearish_sweep_confirmed"}:
                confidence = max(confidence - 0.20, 0.0)
                structure_note = structure_bias
        elif action == "sell":
            if sweep_confirmation_state.startswith("bearish_"):
                confidence = min(confidence + 0.22, 1.0)
                structure_note = sweep_confirmation_state
            elif reclaim_state.startswith("bearish_"):
                confidence = min(confidence + 0.15, 1.0)
                structure_note = reclaim_state
            elif structure_bias in {"bullish_breakout", "bullish_sweep_confirmed"}:
                confidence = max(confidence - 0.20, 0.0)
                structure_note = structure_bias

        # HTF: if H1 BB is also squeezed, breakout signal is stronger
        htf_bonus = 0.0
        htf = context.htf_indicators.get("H1", {})
        htf_bb = htf.get("boll20", {})
        htf_upper = htf_bb.get("bb_upper")
        htf_lower = htf_bb.get("bb_lower")
        htf_mid = htf_bb.get("bb_mid")
        if htf_upper is not None and htf_lower is not None and htf_mid and htf_mid > 0:
            htf_bw = (htf_upper - htf_lower) / htf_mid
            if htf_bw < 0.01:
                # H1 also squeezed → amplifies breakout expectation
                htf_bonus = 0.06
        confidence = min(confidence + htf_bonus, 1.0)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=(
                f"bb_close={close_value:.2f},upper={upper:.2f},lower={lower:.2f},"
                f"structure={structure_note}"
            ),
            used_indicators=used or ["boll20"],
            metadata={
                "close": close_value,
                "bb_upper": upper,
                "bb_lower": lower,
                "bb_mid": mid,
                "band_width": band_width,
                "squeeze_bonus": squeeze_bonus,
                "structure_bias": structure_bias,
                "reclaim_state": reclaim_state,
                "sweep_confirmation_state": sweep_confirmation_state,
                "htf_bonus": htf_bonus,
            },
        )

    @staticmethod
    def _get_close(context: SignalContext) -> Optional[float]:
        for indicator_name in ("boll20", "bollinger20", "bollinger"):
            payload = context.indicators.get(indicator_name)
            if isinstance(payload, dict):
                val = payload.get("close")
                if val is not None:
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        continue
        return None


class KeltnerBollingerSqueezeStrategy:
    """Keltner-Bollinger 挤压突破策略（John Carter Squeeze 变体）。

    核心逻辑：
    - 挤压（Squeeze）= 布林带完全在肯特纳通道内部（波动率极度压缩）
    - 突破（Breakout）= close 突破布林带上/下轨
    - 挤压后的突破具有更高的爆发动能，置信度加成

    支持 intrabar：带宽是历史 close 计算的，实时稳定，
    close 突破带宽是实时事件，intrabar 可以比 bar 收盘提前发现。
    """

    name = "keltner_bb_squeeze"
    category = "breakout"
    required_indicators = ("boll20", "keltner20")
    preferred_scopes = ("intrabar", "confirmed")

    regime_affinity = {
        RegimeType.TRENDING:  0.35,  # 趋势中 Squeeze 出现频率低，信号意义有限
        RegimeType.RANGING:   0.55,  # 震荡末期常出现 Squeeze，可提前布局
        RegimeType.BREAKOUT:  1.00,  # Squeeze 释放正是这个策略的核心场景
        RegimeType.UNCERTAIN: 0.65,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        bb_upper, bb_name = _resolve_indicator_value(
            context.indicators, (("boll20", "bb_upper"),)
        )
        bb_lower, _ = _resolve_indicator_value(
            context.indicators, (("boll20", "bb_lower"),)
        )
        bb_mid, _ = _resolve_indicator_value(
            context.indicators, (("boll20", "bb_mid"),)
        )
        bb_close, _ = _resolve_indicator_value(
            context.indicators, (("boll20", "close"),)
        )
        kc_upper, kc_name = _resolve_indicator_value(
            context.indicators, (("keltner20", "kc_upper"),)
        )
        kc_lower, _ = _resolve_indicator_value(
            context.indicators, (("keltner20", "kc_lower"),)
        )
        used = [n for n in (bb_name, kc_name) if n]

        if any(v is None for v in (bb_upper, bb_lower, bb_mid, bb_close, kc_upper, kc_lower)):
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["boll20", "keltner20"],
            )

        close = bb_close
        band_range = bb_upper - bb_lower  # type: ignore[operator]
        kc_range = kc_upper - kc_lower  # type: ignore[operator]
        is_squeeze = bb_upper < kc_upper and bb_lower > kc_lower  # type: ignore[operator]
        # Squeeze 紧度量化：bb_width / kc_width，越小越紧
        squeeze_tightness = band_range / kc_range if kc_range > 0 else 1.0
        if is_squeeze:
            if squeeze_tightness < 0.5:
                squeeze_bonus = 0.30  # 极度压缩
            elif squeeze_tightness < 0.7:
                squeeze_bonus = 0.20  # 中度压缩
            else:
                squeeze_bonus = 0.10  # 轻度压缩
        else:
            squeeze_bonus = 0.0

        if close >= bb_upper:  # type: ignore[operator]
            action = "buy"
            excess = (close - bb_upper) / band_range if band_range > 0 else 0.0  # type: ignore[operator]
            confidence = min(0.50 + min(excess, 0.30) + squeeze_bonus, 1.0)
        elif close <= bb_lower:  # type: ignore[operator]
            action = "sell"
            excess = (bb_lower - close) / band_range if band_range > 0 else 0.0  # type: ignore[operator]
            confidence = min(0.50 + min(excess, 0.30) + squeeze_bonus, 1.0)
        else:
            action = "hold"
            confidence = 0.15 if is_squeeze else 0.1

        # HTF: H1 level squeeze confirmation amplifies breakout expectation
        htf_bonus = 0.0
        htf = context.htf_indicators.get("H1", {})
        htf_bb = htf.get("boll20", {})
        htf_kc = htf.get("keltner20", {})
        htf_bb_u = htf_bb.get("bb_upper")
        htf_bb_l = htf_bb.get("bb_lower")
        htf_kc_u = htf_kc.get("kc_upper")
        htf_kc_l = htf_kc.get("kc_lower")
        if all(v is not None for v in (htf_bb_u, htf_bb_l, htf_kc_u, htf_kc_l)):
            htf_squeeze = htf_bb_u < htf_kc_u and htf_bb_l > htf_kc_l
            if htf_squeeze and is_squeeze:
                htf_bonus = 0.08  # Both TFs squeezed → strong breakout setup
        confidence = min(confidence + htf_bonus, 1.0)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=(
                f"close={close:.2f},bb_upper={bb_upper:.2f},bb_lower={bb_lower:.2f},"
                f"squeeze={is_squeeze}"
            ),
            used_indicators=used or ["boll20", "keltner20"],
            metadata={
                "close": close,
                "bb_upper": bb_upper,
                "bb_lower": bb_lower,
                "kc_upper": kc_upper,
                "kc_lower": kc_lower,
                "is_squeeze": is_squeeze,
                "squeeze_bonus": squeeze_bonus,
                "htf_bonus": htf_bonus,
            },
        )


class DonchianBreakoutStrategy:
    """唐奇安通道突破策略（海龟交易法则变体）。

    核心逻辑：
    - close 突破 N 周期最高价 → 买入（趋势延续）
    - close 跌破 N 周期最低价 → 卖出
    - ADX 过滤：ADX < adx_min 时不交易（避免震荡市虚假突破）

    仅在 bar 收盘时评估：价格在 bar 内触及前高/前低不算有效突破，
    需要 bar 收盘价确认。
    """

    name = "donchian_breakout"
    category = "breakout"
    required_indicators = ("donchian20", "adx14")
    preferred_scopes = ("confirmed",)

    regime_affinity = {
        RegimeType.TRENDING:  0.90,  # 趋势延续期创新高/低是高概率信号
        RegimeType.RANGING:   0.15,  # 震荡市假突破极多，即便有 ADX 过滤也危险
        RegimeType.BREAKOUT:  1.00,  # 通道突破就是为这个 Regime 而生
        RegimeType.UNCERTAIN: 0.45,
    }

    def __init__(self, *, adx_min: float = 23.0) -> None:
        self._adx_min = adx_min

    def evaluate(self, context: SignalContext) -> SignalDecision:
        d_upper, d_name = _resolve_indicator_value(
            context.indicators, (("donchian20", "donchian_upper"),)
        )
        d_lower, _ = _resolve_indicator_value(
            context.indicators, (("donchian20", "donchian_lower"),)
        )
        d_close, _ = _resolve_indicator_value(
            context.indicators, (("donchian20", "close"),)
        )
        adx_val, adx_name = _resolve_indicator_value(
            context.indicators, (("adx14", "adx"), ("adx", "adx"))
        )
        plus_di, _ = _resolve_indicator_value(
            context.indicators, (("adx14", "plus_di"),)
        )
        minus_di, _ = _resolve_indicator_value(
            context.indicators, (("adx14", "minus_di"),)
        )
        used = [n for n in (d_name, adx_name) if n]

        if d_upper is None or d_lower is None or d_close is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicator:donchian20",
                used_indicators=used or ["donchian20"],
            )

        adx = adx_val if adx_val is not None else 0.0

        if adx < self._adx_min:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.1,
                reason=f"adx_too_low:{adx:.1f}<{self._adx_min}",
                used_indicators=used or ["donchian20", "adx14"],
                metadata={"adx": adx, "donchian_upper": d_upper, "donchian_lower": d_lower},
            )

        channel_width = d_upper - d_lower

        if d_close >= d_upper:
            action = "buy"
            di_bonus = 0.1 if (plus_di is not None and minus_di is not None and plus_di > minus_di) else 0.0
            adx_conf = min((adx - self._adx_min) / 30.0 + 0.55, 0.85)
            confidence = min(adx_conf + di_bonus, 1.0)
        elif d_close <= d_lower:
            action = "sell"
            di_bonus = 0.1 if (plus_di is not None and minus_di is not None and minus_di > plus_di) else 0.0
            adx_conf = min((adx - self._adx_min) / 30.0 + 0.55, 0.85)
            confidence = min(adx_conf + di_bonus, 1.0)
        else:
            action = "hold"
            confidence = 0.1

        structure = _market_structure(context)
        breakout_state = str(structure.get("breakout_state") or "none")
        reclaim_state = str(structure.get("reclaim_state") or "none")
        sweep_confirmation_state = str(
            structure.get("sweep_confirmation_state") or "none"
        )
        first_pullback_state = str(structure.get("first_pullback_state") or "none")
        structure_note = breakout_state if breakout_state != "none" else reclaim_state
        if structure_note == "none":
            structure_note = sweep_confirmation_state
        if structure_note == "none":
            structure_note = first_pullback_state
        if action == "buy":
            if reclaim_state.startswith("bearish_") or sweep_confirmation_state.startswith(
                "bearish_"
            ):
                failed_state = (
                    sweep_confirmation_state
                    if sweep_confirmation_state.startswith("bearish_")
                    else reclaim_state
                )
                return SignalDecision(
                    strategy=self.name,
                    symbol=context.symbol,
                    timeframe=context.timeframe,
                    direction="hold",
                    confidence=0.15,
                    reason=f"failed_structure_breakout:{failed_state}",
                    used_indicators=used or ["donchian20", "adx14"],
                    metadata={
                        "close": d_close,
                        "donchian_upper": d_upper,
                        "donchian_lower": d_lower,
                        "channel_width": channel_width,
                        "adx": adx,
                        "plus_di": plus_di,
                        "minus_di": minus_di,
                        "breakout_state": breakout_state,
                        "reclaim_state": reclaim_state,
                        "sweep_confirmation_state": sweep_confirmation_state,
                        "first_pullback_state": first_pullback_state,
                    },
                )
            if breakout_state.startswith("above_"):
                confidence = min(confidence + 0.12, 1.0)
            if sweep_confirmation_state.startswith("bullish_"):
                confidence = min(confidence + 0.10, 1.0)
                structure_note = sweep_confirmation_state
            if first_pullback_state.startswith("bullish_"):
                confidence = min(confidence + 0.08, 1.0)
                structure_note = first_pullback_state
        elif action == "sell":
            if reclaim_state.startswith("bullish_") or sweep_confirmation_state.startswith(
                "bullish_"
            ):
                failed_state = (
                    sweep_confirmation_state
                    if sweep_confirmation_state.startswith("bullish_")
                    else reclaim_state
                )
                return SignalDecision(
                    strategy=self.name,
                    symbol=context.symbol,
                    timeframe=context.timeframe,
                    direction="hold",
                    confidence=0.15,
                    reason=f"failed_structure_breakout:{failed_state}",
                    used_indicators=used or ["donchian20", "adx14"],
                    metadata={
                        "close": d_close,
                        "donchian_upper": d_upper,
                        "donchian_lower": d_lower,
                        "channel_width": channel_width,
                        "adx": adx,
                        "plus_di": plus_di,
                        "minus_di": minus_di,
                        "breakout_state": breakout_state,
                        "reclaim_state": reclaim_state,
                        "sweep_confirmation_state": sweep_confirmation_state,
                        "first_pullback_state": first_pullback_state,
                    },
                )
            if breakout_state.startswith("below_"):
                confidence = min(confidence + 0.12, 1.0)
            if sweep_confirmation_state.startswith("bearish_"):
                confidence = min(confidence + 0.10, 1.0)
                structure_note = sweep_confirmation_state
            if first_pullback_state.startswith("bearish_"):
                confidence = min(confidence + 0.08, 1.0)
                structure_note = first_pullback_state

        # HTF confirmation: H1 Donchian channel position validates breakout
        htf_bonus = 0.0
        htf = context.htf_indicators.get("H1", {})
        htf_dc = htf.get("donchian20", {})
        htf_upper = htf_dc.get("donchian_upper")
        htf_lower = htf_dc.get("donchian_lower")
        if htf_upper is not None and htf_lower is not None and d_close is not None:
            if action == "buy" and d_close >= htf_upper:
                htf_bonus = 0.10  # Also breaking H1 channel — strong
            elif action == "sell" and d_close <= htf_lower:
                htf_bonus = 0.10
            elif action == "buy" and d_close < htf_lower:
                htf_bonus = -0.05  # H1 bearish channel, short TF buy risky
            elif action == "sell" and d_close > htf_upper:
                htf_bonus = -0.05
        confidence = min(confidence + htf_bonus, 1.0)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=(
                f"close={d_close:.2f},d_upper={d_upper:.2f},d_lower={d_lower:.2f},"
                f"adx={adx:.1f},structure={structure_note}"
            ),
            used_indicators=used or ["donchian20", "adx14"],
            metadata={
                "close": d_close,
                "donchian_upper": d_upper,
                "donchian_lower": d_lower,
                "channel_width": channel_width,
                "adx": adx,
                "plus_di": plus_di,
                "minus_di": minus_di,
                "breakout_state": breakout_state,
                "reclaim_state": reclaim_state,
                "sweep_confirmation_state": sweep_confirmation_state,
                "first_pullback_state": first_pullback_state,
                "htf_bonus": htf_bonus,
            },
        )


class FakeBreakoutDetector:
    """Detect failed Donchian breakouts that reject back into the channel."""

    name = "fake_breakout"
    category = "breakout"
    required_indicators = ("donchian20", "atr14")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 0.20,
        RegimeType.RANGING: 1.00,
        RegimeType.BREAKOUT: 0.70,
        RegimeType.UNCERTAIN: 0.80,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        upper, d_name = _resolve_indicator_value(
            context.indicators, (("donchian20", "donchian_upper"),)
        )
        lower, _ = _resolve_indicator_value(
            context.indicators, (("donchian20", "donchian_lower"),)
        )
        atr, atr_name = _resolve_indicator_value(
            context.indicators, (("atr14", "atr"), ("atr", "atr"))
        )
        bars = _recent_bars(context)
        current = bars[-1] if bars else None
        used = [name for name in (d_name, atr_name) if name]

        if current is None or upper is None or lower is None or atr is None or atr <= 0:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_breakout_context",
                used_indicators=used or ["donchian20", "atr14"],
            )

        open_price = _bar_price(current, "open")
        high = _bar_price(current, "high")
        low = _bar_price(current, "low")
        close = _bar_price(current, "close")
        if None in {open_price, high, low, close}:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_bar_ohlc",
                used_indicators=used or ["donchian20", "atr14"],
            )

        body_top = max(open_price, close)
        body_bottom = min(open_price, close)
        upper_wick = max(high - body_top, 0.0)
        lower_wick = max(body_bottom - low, 0.0)
        structure = _market_structure(context)
        reclaim_state = str(structure.get("reclaim_state") or "none")
        sweep_confirmation_state = str(
            structure.get("sweep_confirmation_state") or "none"
        )

        bearish_fake = (
            high > upper
            and close < upper
            and close <= open_price
            and upper_wick >= atr * 0.5
        )
        bullish_fake = (
            low < lower
            and close > lower
            and close >= open_price
            and lower_wick >= atr * 0.5
        )

        if bearish_fake:
            wick_factor = min(upper_wick / atr, 2.0)
            rejection_factor = min((upper - close) / atr, 1.5)
            confidence = min(
                0.45 + wick_factor * 0.18 + rejection_factor * 0.12,
                0.95,
            )
            if reclaim_state.startswith("bearish_") or sweep_confirmation_state.startswith(
                "bearish_"
            ):
                confidence = min(confidence + 0.10, 1.0)
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="sell",
                confidence=confidence,
                reason=(
                    f"failed_upper_breakout:high={high:.2f},close={close:.2f},"
                    f"upper={upper:.2f}"
                ),
                used_indicators=used or ["donchian20", "atr14"],
                metadata={
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "donchian_upper": upper,
                    "donchian_lower": lower,
                    "atr": atr,
                    "upper_wick": upper_wick,
                    "reclaim_state": reclaim_state,
                    "sweep_confirmation_state": sweep_confirmation_state,
                },
            )

        if bullish_fake:
            wick_factor = min(lower_wick / atr, 2.0)
            rejection_factor = min((close - lower) / atr, 1.5)
            confidence = min(
                0.45 + wick_factor * 0.18 + rejection_factor * 0.12,
                0.95,
            )
            if reclaim_state.startswith("bullish_") or sweep_confirmation_state.startswith(
                "bullish_"
            ):
                confidence = min(confidence + 0.10, 1.0)
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="buy",
                confidence=confidence,
                reason=(
                    f"failed_lower_breakout:low={low:.2f},close={close:.2f},"
                    f"lower={lower:.2f}"
                ),
                used_indicators=used or ["donchian20", "atr14"],
                metadata={
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "donchian_upper": upper,
                    "donchian_lower": lower,
                    "atr": atr,
                    "lower_wick": lower_wick,
                    "reclaim_state": reclaim_state,
                    "sweep_confirmation_state": sweep_confirmation_state,
                },
            )

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.1,
            reason="no_fake_breakout",
            used_indicators=used or ["donchian20", "atr14"],
            metadata={
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "donchian_upper": upper,
                "donchian_lower": lower,
                "atr": atr,
            },
        )


class SqueezeReleaseFollow:
    """Follow directional release when Bollinger exits the Keltner envelope."""

    name = "squeeze_release"
    category = "breakout"
    required_indicators = ("boll20", "keltner20", "macd")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 0.60,
        RegimeType.RANGING: 0.15,
        RegimeType.BREAKOUT: 1.00,
        RegimeType.UNCERTAIN: 0.50,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        bb_upper, bb_name = _resolve_indicator_value(
            context.indicators, (("boll20", "bb_upper"),)
        )
        bb_lower, _ = _resolve_indicator_value(
            context.indicators, (("boll20", "bb_lower"),)
        )
        bb_mid, _ = _resolve_indicator_value(
            context.indicators, (("boll20", "bb_mid"),)
        )
        kc_upper, kc_name = _resolve_indicator_value(
            context.indicators, (("keltner20", "kc_upper"),)
        )
        kc_lower, _ = _resolve_indicator_value(
            context.indicators, (("keltner20", "kc_lower"),)
        )
        hist, macd_name = _resolve_indicator_value(
            context.indicators, (("macd", "hist"),)
        )
        used = [name for name in (bb_name, kc_name, macd_name) if name]

        if any(
            value is None
            for value in (bb_upper, bb_lower, bb_mid, kc_upper, kc_lower, hist)
        ):
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["boll20", "keltner20", "macd"],
            )

        structure = _market_structure(context)
        compression_state = str(structure.get("compression_state") or "unknown")
        structure_bias = str(structure.get("structure_bias") or "neutral")
        # MACD 动量加速度（delta_bars 自动计算的 hist 3-bar 变化量）
        macd_data = context.indicators.get("macd", {})
        hist_d3 = macd_data.get("hist_d3")
        # 动量加速奖励/减速惩罚
        momentum_bonus = 0.0
        if hist_d3 is not None:
            if (hist > 0 and hist_d3 > 0) or (hist < 0 and hist_d3 < 0):
                momentum_bonus = 0.10  # 同向加速
            elif (hist > 0 and hist_d3 < 0) or (hist < 0 and hist_d3 > 0):
                momentum_bonus = -0.08  # 动量衰减

        band_width = max(bb_upper - bb_lower, 1e-9)
        upside_release = bb_upper > kc_upper and hist > 0
        downside_release = bb_lower < kc_lower and hist < 0
        release_strength = 0.0
        if upside_release:
            release_strength = max((bb_upper - kc_upper) / band_width, 0.0)
        elif downside_release:
            release_strength = max((kc_lower - bb_lower) / band_width, 0.0)

        compression_bonus = (
            0.12
            if compression_state == "contracted"
            else 0.06 if compression_state == "normal" else 0.0
        )
        expansion_bonus = (
            0.08 if structure_bias in {"compression", "expansion"} else 0.0
        )

        if upside_release:
            confidence = min(
                0.52
                + min(release_strength, 0.25)
                + compression_bonus
                + expansion_bonus
                + momentum_bonus,
                0.95,
            )
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="buy",
                confidence=confidence,
                reason=(
                    f"bullish_squeeze_release:bb_upper={bb_upper:.2f}>"
                    f"kc_upper={kc_upper:.2f},hist={hist:.3f}"
                ),
                used_indicators=used or ["boll20", "keltner20", "macd"],
                metadata={
                    "bb_upper": bb_upper,
                    "bb_lower": bb_lower,
                    "bb_mid": bb_mid,
                    "kc_upper": kc_upper,
                    "kc_lower": kc_lower,
                    "macd_hist": hist,
                    "compression_state": compression_state,
                    "structure_bias": structure_bias,
                },
            )

        if downside_release:
            confidence = min(
                0.52
                + min(release_strength, 0.25)
                + compression_bonus
                + expansion_bonus
                + momentum_bonus,
                0.95,
            )
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="sell",
                confidence=confidence,
                reason=(
                    f"bearish_squeeze_release:bb_lower={bb_lower:.2f}<"
                    f"kc_lower={kc_lower:.2f},hist={hist:.3f}"
                ),
                used_indicators=used or ["boll20", "keltner20", "macd"],
                metadata={
                    "bb_upper": bb_upper,
                    "bb_lower": bb_lower,
                    "bb_mid": bb_mid,
                    "kc_upper": kc_upper,
                    "kc_lower": kc_lower,
                    "macd_hist": hist,
                    "compression_state": compression_state,
                    "structure_bias": structure_bias,
                },
            )

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.1,
            reason="no_squeeze_release",
            used_indicators=used or ["boll20", "keltner20", "macd"],
            metadata={
                "compression_state": compression_state,
                "structure_bias": structure_bias,
                "macd_hist": hist,
            },
        )


class MultiTimeframeConfirmStrategy:
    """Confirms signals when direction aligns across timeframes.

    Uses existing indicator snapshots to check if the same directional signal
    exists on a higher timeframe. Only produces signals when lower TF and
    higher TF agree on direction.

    Uses bar-close snapshots only.  The higher-timeframe direction is read from
    HTFStateCache, which is populated by consensus signals from the higher TF.
    """

    name = "multi_timeframe_confirm"
    category = "multi_tf"
    required_indicators = ("sma20", "ema50")
    preferred_scopes = ("confirmed",)
    # HTF 指标：从 H1 获取 ema50 和 sma20 判断高时间框架趋势方向

    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # 趋势市 LTF+HTF 双向一致，最可靠
        RegimeType.RANGING:   0.30,  # 震荡市 HTF 方向频繁反转，信号不稳
        RegimeType.BREAKOUT:  0.70,  # 突破初期 HTF 确认方向有价值
        RegimeType.UNCERTAIN: 0.50,
    }

    def __init__(
        self,
        *,
        state_reader: Optional[Any] = None,
        htf_cache: Optional[Any] = None,
    ):
        self._state_reader = state_reader
        self._htf_cache = htf_cache
        if htf_cache is None and state_reader is None:
            logger.warning(
                "MultiTimeframeConfirmStrategy: 未传入 htf_cache 或 state_reader，"
                "_get_htf_direction() 将始终返回 None，策略将固定输出 hold(0.1)。"
                "生产环境请通过 htf_cache= 参数注入 HTFStateCache 实例。"
            )

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
                direction="hold",
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
                direction="hold",
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
                direction=local_direction,
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
            direction="hold",
            confidence=0.15,
            reason=f"mtf_conflict:local={local_direction},htf={htf_direction}",
            used_indicators=used or ["sma20", "ema50"],
            metadata={"local_direction": local_direction, "htf_direction": htf_direction},
        )

    def _get_htf_direction(self, context: SignalContext) -> Optional[str]:
        htf = context.metadata.get("htf_direction")
        if htf in ("buy", "sell", "hold"):
            return htf
        # 优先使用 htf_indicators（H1 的 ema50/sma20）判断方向
        htf_data = context.htf_indicators.get("H1", {})
        htf_ema = htf_data.get("ema50", {}).get("ema")
        htf_sma = htf_data.get("sma20", {}).get("sma")
        if htf_ema is not None and htf_sma is not None:
            if htf_sma > htf_ema:
                return "buy"
            elif htf_sma < htf_ema:
                return "sell"
            return "hold"
        if self._htf_cache is not None:
            try:
                direction = self._htf_cache.get_htf_direction(
                    context.symbol, context.timeframe
                )
                if direction is not None:
                    return direction
            except Exception:
                pass
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
