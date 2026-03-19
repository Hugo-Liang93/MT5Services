"""趋势跟踪策略

包含基于均线/趋势指标的方向性策略，适合趋势行情（ADX 高位）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import _resolve_indicator_value

logger = logging.getLogger(__name__)


class SmaTrendStrategy:
    """Simple trend signal based on fast/slow SMA relation.

    Uses bar-close snapshots only.  SMA/EMA crossovers are meaningful only
    when a bar has *closed* — intrabar MA values oscillate continuously as
    the live price moves, generating excessive false crossover noise.
    """

    name = "sma_trend"
    required_indicators = ("sma20", "ema50")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # MA 金叉/死叉在趋势中最可靠
        RegimeType.RANGING:   0.20,  # 震荡市 MA 频繁交叉，产生大量虚假信号
        RegimeType.BREAKOUT:  0.50,  # 突破初期 MA 尚未跟上，信号滞后
        RegimeType.UNCERTAIN: 0.50,
    }

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


class SupertrendStrategy:
    """基于 Supertrend 的趋势跟踪策略。

    Supertrend 是黄金日内交易最有效的趋势指标之一：
    - 当价格从 Supertrend 下方突破至上方（direction -1→1）→ BUY
    - 当价格从 Supertrend 上方跌破至下方（direction 1→-1）→ SELL
    - 结合 ADX 过滤，只在趋势明确时（ADX > adx_threshold）发出信号

    仅在 bar 收盘时评估（confirmed scope），Supertrend 方向在 bar 中间频繁
    翻转会产生大量假信号。
    """

    name = "supertrend"
    required_indicators = ("supertrend14", "adx14")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # Supertrend 专为趋势行情设计
        RegimeType.RANGING:   0.30,  # 震荡市 Supertrend 方向频繁翻转，假信号多
        RegimeType.BREAKOUT:  0.60,  # 突破时可以确认方向，但信号有一定滞后
        RegimeType.UNCERTAIN: 0.55,
    }

    def __init__(self, *, adx_threshold: float = 20.0) -> None:
        self._adx_threshold = adx_threshold

    def evaluate(self, context: SignalContext) -> SignalDecision:
        st_direction, st_name = _resolve_indicator_value(
            context.indicators,
            (
                ("supertrend14", "direction"),
                ("supertrend", "direction"),
            ),
        )
        st_value, _ = _resolve_indicator_value(
            context.indicators,
            (
                ("supertrend14", "supertrend"),
                ("supertrend", "supertrend"),
            ),
        )
        adx_value, adx_name = _resolve_indicator_value(
            context.indicators,
            (
                ("adx14", "adx"),
                ("adx", "adx"),
            ),
        )
        used = [n for n in (st_name, adx_name) if n]

        if st_direction is None or st_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:supertrend",
                used_indicators=used or ["supertrend14"],
            )

        adx = adx_value if adx_value is not None else 0.0
        if adx < self._adx_threshold:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.1,
                reason=f"adx_too_low:{adx:.1f}<{self._adx_threshold}",
                used_indicators=used or ["supertrend14", "adx14"],
                metadata={"adx": adx, "st_direction": st_direction},
            )

        if st_direction > 0:
            action = "buy"
        elif st_direction < 0:
            action = "sell"
        else:
            action = "hold"

        adx_confidence = min((adx - self._adx_threshold) / 30.0 + 0.6, 1.0)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=adx_confidence,
            reason=f"supertrend_direction={st_direction:.0f},adx={adx:.1f}",
            used_indicators=used or ["supertrend14", "adx14"],
            metadata={
                "supertrend": st_value,
                "direction": st_direction,
                "adx": adx,
            },
        )


class MacdMomentumStrategy:
    """基于 MACD 柱状图动量的趋势策略。

    黄金趋势行情中 MACD 柱状图的方向翻转是重要的动量确认信号：
    - hist 由负转正（从负值区域向上穿越零轴）→ BUY
    - hist 由正转负（从正值区域向下穿越零轴）→ SELL
    - hist 绝对值越大，动量越强，置信度越高
    - 附加过滤：macd 线与 signal 线方向一致时才发信号

    仅在 bar 收盘时评估（confirmed scope）。
    """

    name = "macd_momentum"
    required_indicators = ("macd",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # MACD 柱状图反映趋势动量，趋势中最准确
        RegimeType.RANGING:   0.30,  # 震荡市 MACD 来回交叉，产生频繁假信号
        RegimeType.BREAKOUT:  0.70,  # 突破初期 MACD 柱常领先确认方向
        RegimeType.UNCERTAIN: 0.55,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        macd_val, macd_name = _resolve_indicator_value(
            context.indicators,
            (
                ("macd", "macd"),
                ("macd12_26_9", "macd"),
            ),
        )
        signal_val, _ = _resolve_indicator_value(
            context.indicators,
            (
                ("macd", "signal"),
                ("macd12_26_9", "signal"),
            ),
        )
        hist_val, _ = _resolve_indicator_value(
            context.indicators,
            (
                ("macd", "hist"),
                ("macd12_26_9", "hist"),
            ),
        )
        used = [macd_name] if macd_name else ["macd"]

        if macd_val is None or signal_val is None or hist_val is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:macd",
                used_indicators=used,
            )

        hist = hist_val
        macd_line = macd_val
        signal_line = signal_val

        if hist > 0 and macd_line > signal_line:
            action = "buy"
            magnitude = abs(hist) / (abs(macd_line) + 1e-10)
            confidence = min(0.45 + min(magnitude * 2.0, 0.45), 0.9)
        elif hist < 0 and macd_line < signal_line:
            action = "sell"
            magnitude = abs(hist) / (abs(macd_line) + 1e-10)
            confidence = min(0.45 + min(magnitude * 2.0, 0.45), 0.9)
        else:
            action = "hold"
            confidence = 0.1

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"macd={macd_val:.4f},signal={signal_val:.4f},hist={hist_val:.4f}",
            used_indicators=used,
            metadata={
                "macd": macd_val,
                "signal": signal_val,
                "hist": hist_val,
            },
        )


class EmaRibbonStrategy:
    """EMA 带状对齐趋势策略。

    核心逻辑：
    - 三条均线（EMA9 快线、HMA20 中线、EMA50 慢线）全部同向排列时产生信号
    - 多头排列：EMA9 > HMA20 > EMA50（快 > 中 > 慢）
    - 空头排列：EMA9 < HMA20 < EMA50
    - 置信度由三条线之间的相对间距决定（间距越大趋势越强）

    仅在 bar 收盘时评估：intrabar MA 值持续波动，噪声大。
    """

    name = "ema_ribbon"
    required_indicators = ("ema9", "hma20", "ema50")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # 三线对齐是趋势行情的黄金信号
        RegimeType.RANGING:   0.10,  # 震荡市三线互相缠绕交叉，完全无效
        RegimeType.BREAKOUT:  0.40,  # 突破初期 EMA50 还未偏转，排列不完整
        RegimeType.UNCERTAIN: 0.40,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        ema9_val, e9_name = _resolve_indicator_value(
            context.indicators, (("ema9", "ema"), ("ema9", "value"))
        )
        hma_val, hma_name = _resolve_indicator_value(
            context.indicators, (("hma20", "hma"), ("hma20", "value"))
        )
        ema50_val, e50_name = _resolve_indicator_value(
            context.indicators, (("ema50", "ema"), ("ema50", "value"))
        )
        used = [n for n in (e9_name, hma_name, e50_name) if n]

        if ema9_val is None or hma_val is None or ema50_val is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["ema9", "hma20", "ema50"],
            )

        slow = ema50_val
        if slow == 0:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                action="hold",
                confidence=0.0,
                reason="ema50_is_zero",
                used_indicators=used,
            )

        fast_mid_gap = abs(ema9_val - hma_val) / slow
        mid_slow_gap = abs(hma_val - ema50_val) / slow
        total_span = abs(ema9_val - ema50_val) / slow

        if ema9_val > hma_val > ema50_val:
            action = "buy"
            confidence = min(0.5 + total_span * 80, 0.9)
        elif ema9_val < hma_val < ema50_val:
            action = "sell"
            confidence = min(0.5 + total_span * 80, 0.9)
        else:
            action = "hold"
            confidence = 0.1

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=(
                f"ema9={ema9_val:.2f},hma20={hma_val:.2f},ema50={ema50_val:.2f}"
            ),
            used_indicators=used or ["ema9", "hma20", "ema50"],
            metadata={
                "ema9": ema9_val,
                "hma20": hma_val,
                "ema50": ema50_val,
                "fast_mid_gap_pct": fast_mid_gap * 100,
                "mid_slow_gap_pct": mid_slow_gap * 100,
                "total_span_pct": total_span * 100,
            },
        )
