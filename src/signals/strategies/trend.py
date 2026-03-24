"""趋势跟踪策略

包含基于均线/趋势指标的方向性策略，适合趋势行情（ADX 高位）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import _resolve_indicator_value

logger = logging.getLogger(__name__)


def _market_structure(context: SignalContext) -> dict[str, Any]:
    payload = context.metadata.get("market_structure")
    return payload if isinstance(payload, dict) else {}


class SmaTrendStrategy:
    """Simple trend signal based on fast/slow SMA relation.

    Uses bar-close snapshots only.  SMA/EMA crossovers are meaningful only
    when a bar has *closed* — intrabar MA values oscillate continuously as
    the live price moves, generating excessive false crossover noise.
    """

    name = "sma_trend"
    category = "trend"
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
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used_indicators or ["sma20", "ema50"],
            )

        spread = fast - slow
        relative_spread = spread / slow if slow else 0.0
        # 方向稳定性过滤：均线差距太小时视为无方向（防止震荡市反复交叉）
        min_spread_pct = 0.0005  # 0.05%
        if abs(relative_spread) < min_spread_pct:
            action = "hold"
        elif spread > 0:
            action = "buy"
        elif spread < 0:
            action = "sell"
        else:
            action = "hold"

        confidence = min(abs(relative_spread) * 100, 1.0)
        structure = _market_structure(context)
        structure_bias = str(structure.get("structure_bias") or "neutral")
        reclaim_state = str(structure.get("reclaim_state") or "none")
        sweep_confirmation_state = str(
            structure.get("sweep_confirmation_state") or "none"
        )
        first_pullback_state = str(structure.get("first_pullback_state") or "none")
        structure_note = structure_bias
        if action == "buy":
            if structure_bias in {
                "bullish_breakout",
                "bullish_reclaim",
                "bullish_sweep_confirmed",
                "bullish_pullback",
                "expansion",
            }:
                confidence = min(confidence + 0.10, 1.0)
            if sweep_confirmation_state.startswith("bullish_"):
                confidence = min(confidence + 0.14, 1.0)
                structure_note = sweep_confirmation_state
            if first_pullback_state.startswith("bullish_"):
                confidence = min(confidence + 0.12, 1.0)
                structure_note = first_pullback_state
            if reclaim_state.startswith("bearish_"):
                confidence = max(confidence - 0.15, 0.0)
                structure_note = reclaim_state
            if sweep_confirmation_state.startswith("bearish_"):
                confidence = max(confidence - 0.22, 0.0)
                structure_note = sweep_confirmation_state
        elif action == "sell":
            if structure_bias in {
                "bearish_breakout",
                "bearish_reclaim",
                "bearish_sweep_confirmed",
                "bearish_pullback",
                "expansion",
            }:
                confidence = min(confidence + 0.10, 1.0)
            if sweep_confirmation_state.startswith("bearish_"):
                confidence = min(confidence + 0.14, 1.0)
                structure_note = sweep_confirmation_state
            if first_pullback_state.startswith("bearish_"):
                confidence = min(confidence + 0.12, 1.0)
                structure_note = first_pullback_state
            if reclaim_state.startswith("bullish_"):
                confidence = max(confidence - 0.15, 0.0)
                structure_note = reclaim_state
            if sweep_confirmation_state.startswith("bullish_"):
                confidence = max(confidence - 0.22, 0.0)
                structure_note = sweep_confirmation_state
        # D1 HTF confirmation: daily trend alignment
        htf_bonus = 0.0
        htf = context.htf_indicators.get("D1", {})
        htf_sma = htf.get("sma20", {}).get("sma")
        htf_ema = htf.get("ema50", {}).get("ema")
        if htf_sma is not None and htf_ema is not None:
            htf_bullish = htf_sma > htf_ema
            if action == "buy" and htf_bullish:
                htf_bonus = 0.10
            elif action == "sell" and not htf_bullish:
                htf_bonus = 0.10
            elif action == "buy" and not htf_bullish:
                htf_bonus = -0.08
            elif action == "sell" and htf_bullish:
                htf_bonus = -0.08
        confidence = min(max(confidence + htf_bonus, 0.0), 1.0)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=(
                f"sma_spread={spread:.6f},relative={relative_spread:.6f},"
                f"structure={structure_note}"
            ),
            used_indicators=used_indicators or ["sma20", "ema50"],
            metadata={
                "spread": spread,
                "relative_spread": relative_spread,
                "fast_indicator": fast_name or "sma20",
                "slow_indicator": slow_name or "ema50",
                "structure_bias": structure_bias,
                "reclaim_state": reclaim_state,
                "sweep_confirmation_state": sweep_confirmation_state,
                "first_pullback_state": first_pullback_state,
                "htf_bonus": htf_bonus,
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
    category = "trend"
    required_indicators = ("supertrend14", "adx14")
    preferred_scopes = ("confirmed",)

    regime_affinity = {
        RegimeType.TRENDING:  1.00,  # Supertrend 专为趋势行情设计
        RegimeType.RANGING:   0.30,  # 震荡市 Supertrend 方向频繁翻转，假信号多
        RegimeType.BREAKOUT:  0.60,  # 突破时可以确认方向，但信号有一定滞后
        RegimeType.UNCERTAIN: 0.55,
    }

    def __init__(self, *, adx_threshold: float = 23.0) -> None:
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
                direction="hold",
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
                direction="hold",
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

        # HTF confirmation: if H1 supertrend agrees, boost confidence
        htf_bonus = 0.0
        htf = context.htf_indicators.get("H1", {})
        htf_st = htf.get("supertrend14", {})
        htf_dir = htf_st.get("direction")
        if htf_dir is not None:
            expected_dir = 1.0 if action == "buy" else -1.0
            if htf_dir == expected_dir:
                htf_bonus = 0.08
            elif htf_dir == -expected_dir:
                htf_bonus = -0.05

        confidence = min(adx_confidence + htf_bonus, 1.0)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=f"supertrend_direction={st_direction:.0f},adx={adx:.1f}",
            used_indicators=used or ["supertrend14", "adx14"],
            metadata={
                "supertrend": st_value,
                "direction": st_direction,
                "adx": adx,
                "htf_bonus": htf_bonus,
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
    category = "trend"
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
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicator:macd",
                used_indicators=used,
            )

        hist = hist_val
        macd_line = macd_val
        signal_line = signal_val

        # 动量加速度：hist 的 3-bar 变化量（delta_bars 自动计算）
        macd_data = context.indicators.get("macd") or context.indicators.get("macd12_26_9") or {}
        hist_d3 = macd_data.get("hist_d3")

        if hist > 0 and macd_line > signal_line:
            action = "buy"
            magnitude = abs(hist) / (abs(macd_line) + 1e-10)
            confidence = min(0.45 + min(magnitude * 2.0, 0.45), 0.9)
            # 动量加速加分：hist_d3 > 0 表示柱状图正在放大
            if hist_d3 is not None and hist_d3 > 0:
                confidence = min(confidence + 0.08, 0.95)
            # hist 接近零（弱动量）→ 降权，防止刚穿零轴的虚假信号
            if abs(hist) < abs(macd_line) * 0.05:
                confidence *= 0.75
        elif hist < 0 and macd_line < signal_line:
            action = "sell"
            magnitude = abs(hist) / (abs(macd_line) + 1e-10)
            confidence = min(0.45 + min(magnitude * 2.0, 0.45), 0.9)
            if hist_d3 is not None and hist_d3 < 0:
                confidence = min(confidence + 0.08, 0.95)
            if abs(hist) < abs(macd_line) * 0.05:
                confidence *= 0.75
        else:
            action = "hold"
            confidence = 0.1

        # D1 HTF confirmation: daily MACD histogram direction
        htf_bonus = 0.0
        htf = context.htf_indicators.get("D1", {})
        htf_hist = htf.get("macd", {}).get("hist")
        if htf_hist is not None:
            if action == "buy" and htf_hist > 0:
                htf_bonus = 0.08
            elif action == "sell" and htf_hist < 0:
                htf_bonus = 0.08
            elif action == "buy" and htf_hist < 0:
                htf_bonus = -0.06
            elif action == "sell" and htf_hist > 0:
                htf_bonus = -0.06
        confidence = min(max(confidence + htf_bonus, 0.0), 1.0)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=f"macd={macd_val:.4f},signal={signal_val:.4f},hist={hist_val:.4f}",
            used_indicators=used,
            metadata={
                "macd": macd_val,
                "signal": signal_val,
                "hist": hist_val,
                "hist_d3": hist_d3,
                "htf_bonus": htf_bonus,
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
    category = "trend"
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
                direction="hold",
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
                direction="hold",
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
            direction=action,
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


class HmaCrossStrategy:
    """基于 HMA/EMA 交叉的低滞后趋势策略。

    HMA（Hull MA）的最大优势是极低滞后——比同周期 EMA 快约半个周期响应。
    HMA(20) 穿越 EMA(50) 比 SMA(20)/EMA(50) 提前 1-3 根 bar 发出信号，
    适合 M1 等快节奏时间框架捕捉趋势初期机会。

    信号逻辑：
    - HMA20 > EMA50 且间距扩大（动量向上）→ buy
    - HMA20 < EMA50 且间距扩大（动量向下）→ sell
    - 置信度由两线间距（相对 EMA50 的百分比）决定

    仅在 bar 收盘时评估：均线交叉需要收盘确认避免假突破。
    """

    name = "hma_cross"
    category = "trend"
    required_indicators = ("hma20", "ema50")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  0.90,  # HMA 低滞后趋势跟踪，趋势市表现最优
        RegimeType.RANGING:   0.15,  # 震荡市两线频繁交叉，产生过多假信号
        RegimeType.BREAKOUT:  0.60,  # 突破时 HMA 快速响应，可捕捉趋势启动
        RegimeType.UNCERTAIN: 0.45,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        hma_val, hma_name = _resolve_indicator_value(
            context.indicators,
            (("hma20", "hma"), ("hma20", "value")),
        )
        ema50_val, e50_name = _resolve_indicator_value(
            context.indicators,
            (("ema50", "ema"), ("ema50", "value")),
        )
        used = [n for n in (hma_name, e50_name) if n]

        if hma_val is None or ema50_val is None or ema50_val == 0:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicator:hma_cross",
                used_indicators=used or ["hma20", "ema50"],
            )

        gap_pct = (hma_val - ema50_val) / ema50_val  # 正=多头，负=空头

        if gap_pct > 0:
            action = "buy"
            confidence = min(0.45 + min(gap_pct * 200, 0.45), 0.90)
        elif gap_pct < 0:
            action = "sell"
            confidence = min(0.45 + min(abs(gap_pct) * 200, 0.45), 0.90)
        else:
            action = "hold"
            confidence = 0.10

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=f"hma20={hma_val:.2f},ema50={ema50_val:.2f},gap={gap_pct*100:.3f}%",
            used_indicators=used or ["hma20", "ema50"],
            metadata={"hma20": hma_val, "ema50": ema50_val, "gap_pct": gap_pct * 100},
        )


class RocMomentumStrategy:
    """基于 ROC 动量加速度的趋势确认策略。

    ROC（Rate of Change）衡量价格的变动速率，代表动量的加速度而非方向本身。
    在趋势行情中，ROC 绝对值扩大表示动量加速，是趋势持续的有力确认。

    信号逻辑：
    - ROC > 阈值 且 ADX > adx_min（趋势中动量向上加速）→ buy
    - ROC < -阈值 且 ADX > adx_min（趋势中动量向下加速）→ sell
    - ADX < adx_min 时（震荡市）不发信号

    仅在 bar 收盘时评估，ROC 使用收盘价计算。
    """

    name = "roc_momentum"
    category = "trend"
    required_indicators = ("roc12", "adx14", "atr14")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING:  0.85,  # 趋势中动量加速是强烈的持续信号
        RegimeType.RANGING:   0.20,  # 震荡市 ROC 噪声大，方向无意义
        RegimeType.BREAKOUT:  0.70,  # 突破时 ROC 急剧变化，可作为方向确认
        RegimeType.UNCERTAIN: 0.40,
    }

    def __init__(self, *, adx_min: float = 23.0, roc_threshold: float = 0.1) -> None:
        self._adx_min = adx_min
        self._roc_threshold = roc_threshold  # ROC 触发基准阈值（%），ATR 归一化时作为下限

    def evaluate(self, context: SignalContext) -> SignalDecision:
        roc_value, roc_name = _resolve_indicator_value(
            context.indicators,
            (("roc12", "roc"), ("roc12", "value"), ("roc", "roc")),
        )
        adx_value, adx_name = _resolve_indicator_value(
            context.indicators,
            (("adx14", "adx"), ("adx", "adx")),
        )
        used = [n for n in (roc_name, adx_name) if n]

        if roc_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicator:roc",
                used_indicators=used or ["roc12"],
            )

        adx = adx_value if adx_value is not None else 0.0
        if adx < self._adx_min:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.10,
                reason=f"adx_too_low:{adx:.1f}<{self._adx_min}",
                used_indicators=used or ["roc12", "adx14"],
                metadata={"roc": roc_value, "adx": adx},
            )

        # ATR 归一化 ROC 阈值：高波动市提高阈值，低波动市降低阈值
        atr_data = context.indicators.get("atr14") or {}
        atr_val = atr_data.get("atr")
        close_price = float(context.metadata.get("close_price") or 0)
        if atr_val and close_price > 0:
            atr_pct = (atr_val / close_price) * 100
            roc_threshold = max(atr_pct * 0.2, self._roc_threshold)
        else:
            roc_threshold = self._roc_threshold

        roc = roc_value
        if roc > roc_threshold:
            action = "buy"
            # ADX 越强、ROC 越大，置信度越高
            adx_conf = min((adx - self._adx_min) / 30.0 * 0.3, 0.30)
            roc_conf = min(abs(roc) / 1.0 * 0.35, 0.35)
            confidence = min(0.40 + adx_conf + roc_conf, 0.90)
        elif roc < -roc_threshold:
            action = "sell"
            adx_conf = min((adx - self._adx_min) / 30.0 * 0.3, 0.30)
            roc_conf = min(abs(roc) / 1.0 * 0.35, 0.35)
            confidence = min(0.40 + adx_conf + roc_conf, 0.90)
        else:
            action = "hold"
            confidence = 0.10

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=f"roc={roc:.3f}%,adx={adx:.1f}",
            used_indicators=used or ["roc12", "adx14"],
            metadata={"roc": roc, "adx": adx},
        )


class FibPullbackStrategy:
    """Fibonacci 回撤入场策略 — 在确认趋势中等待价格回撤至 Fib 关键水平后顺势入场。

    原理：XAUUSD 强趋势中的回撤深度在 38.2%-61.8% 之间的概率约 60-65%。
    策略在 HTF 趋势方向确认（Supertrend）后，从 recent_bars 找到 swing high/low，
    计算 Fibonacci 回撤水平，当价格回撤至关键位并企稳时顺势入场。

    Fib 关键水平：38.2%、50.0%、61.8%
    """

    name = "fib_pullback"
    category = "trend"
    required_indicators = ("supertrend14", "atr14")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.RANGING: 0.15,
        RegimeType.BREAKOUT: 0.50,
        RegimeType.UNCERTAIN: 0.40,
    }

    _FIB_LEVELS = (0.382, 0.500, 0.618)
    _FIB_TOLERANCE = 0.03  # ±3% 容差带

    def __init__(self, *, swing_lookback: int = 20) -> None:
        self._swing_lookback = swing_lookback

    def evaluate(self, context: SignalContext) -> SignalDecision:
        direction, st_name = _resolve_indicator_value(
            context.indicators,
            (("supertrend14", "direction"), ("supertrend", "direction")),
        )
        atr, atr_name = _resolve_indicator_value(
            context.indicators, (("atr14", "atr"), ("atr", "atr"))
        )
        used: List[str] = [n for n in (st_name, atr_name) if n]

        if direction is None or atr is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicators",
                used_indicators=used or ["supertrend14", "atr14"],
            )

        # 趋势方向确认
        if direction > 0:
            trend = "bullish"
        elif direction < 0:
            trend = "bearish"
        else:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="no_trend_direction",
                used_indicators=used,
            )

        # 从 recent_bars 提取 swing high/low
        recent_bars: Any = context.metadata.get("recent_bars")
        if not isinstance(recent_bars, (list, tuple)) or len(recent_bars) < 5:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="insufficient_recent_bars",
                used_indicators=used,
            )

        lookback = min(self._swing_lookback, len(recent_bars))
        bars = recent_bars[-lookback:]

        highs: List[float] = []
        lows: List[float] = []
        for bar in bars:
            if isinstance(bar, dict):
                h, lo = bar.get("high"), bar.get("low")
            elif hasattr(bar, "high"):
                h, lo = getattr(bar, "high", None), getattr(bar, "low", None)
            else:
                continue
            if h is not None and lo is not None:
                try:
                    highs.append(float(h))
                    lows.append(float(lo))
                except (TypeError, ValueError):
                    continue

        if len(highs) < 5:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="insufficient_valid_bars",
                used_indicators=used,
            )

        swing_high = max(highs)
        swing_low = min(lows)
        swing_range = swing_high - swing_low

        if swing_range < atr * 0.5:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="swing_range_too_small",
                used_indicators=used,
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

        # 计算 Fibonacci 回撤水平
        action = "hold"
        confidence = 0.0
        hit_level = 0.0
        fib_price = 0.0

        if trend == "bullish":
            # 上升趋势：回撤 = 从 swing_high 向下
            for level in self._FIB_LEVELS:
                fib_p = swing_high - swing_range * level
                lower_bound = fib_p - swing_range * self._FIB_TOLERANCE
                upper_bound = fib_p + swing_range * self._FIB_TOLERANCE
                if lower_bound <= close_val <= upper_bound:
                    action = "buy"
                    hit_level = level
                    fib_price = fib_p
                    break
        else:  # bearish
            # 下降趋势：回撤 = 从 swing_low 向上
            for level in self._FIB_LEVELS:
                fib_p = swing_low + swing_range * level
                lower_bound = fib_p - swing_range * self._FIB_TOLERANCE
                upper_bound = fib_p + swing_range * self._FIB_TOLERANCE
                if lower_bound <= close_val <= upper_bound:
                    action = "sell"
                    hit_level = level
                    fib_price = fib_p
                    break

        if action == "hold":
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="price_not_at_fib_level",
                used_indicators=used,
                metadata={
                    "trend": trend,
                    "swing_high": swing_high,
                    "swing_low": swing_low,
                    "close": close_val,
                },
            )

        # 置信度：38.2% 最高（浅回撤趋势更强），61.8% 最低（深回撤风险大）
        level_confidence = {0.382: 0.70, 0.500: 0.62, 0.618: 0.55}
        confidence = level_confidence.get(hit_level, 0.55)

        # HTF Supertrend 对齐加成
        htf = context.htf_indicators.get("H1", {})
        htf_direction = htf.get("supertrend14", {}).get("direction")
        if htf_direction is not None:
            if (trend == "bullish" and htf_direction > 0) or (
                trend == "bearish" and htf_direction < 0
            ):
                confidence += 0.08  # HTF 同向确认

        # 市场结构加成
        structure = _market_structure(context)
        first_pullback = str(structure.get("first_pullback_state") or "none")
        if trend == "bullish" and first_pullback.startswith("bullish_"):
            confidence += 0.06
        elif trend == "bearish" and first_pullback.startswith("bearish_"):
            confidence += 0.06

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=min(confidence, 0.90),
            reason=f"fib_pullback:{trend},level={hit_level:.1%},fib_price={fib_price:.2f}",
            used_indicators=used,
            metadata={
                "trend": trend,
                "hit_level": hit_level,
                "fib_price": fib_price,
                "swing_high": swing_high,
                "swing_low": swing_low,
                "close": close_val,
                "first_pullback_state": first_pullback,
            },
        )
