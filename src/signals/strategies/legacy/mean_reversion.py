"""均值回归策略

包含基于超买超卖指标的反转策略，适合震荡行情（ADX 低位）。
"""

from __future__ import annotations

import logging

from typing import Any, Dict, List, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext, SignalDecision
from ..base import _resolve_indicator_value, get_tf_param

logger = logging.getLogger(__name__)


def _get_delta(
    indicators: Dict[str, Dict[str, object]],
    indicator_name: str,
    metric: str,
    delta_bars: int = 3,
) -> Optional[float]:
    """从指标 payload 中提取 delta 值（如 rsi_d3）。"""
    payload = indicators.get(indicator_name)
    if not isinstance(payload, dict):
        return None
    val = payload.get(f"{metric}_d{delta_bars}")
    return float(val) if isinstance(val, (int, float)) else None


# Delta momentum bonus defaults (overridable via signal.ini [strategy_params])
_DELTA_D3_SCALE: float = 10.0        # d3 缩放系数
_DELTA_D3_CAP: float = 0.05          # d3 加分上限
_DELTA_D5_THRESHOLD: float = 8.0     # d5 耗竭判定阈值
_DELTA_D5_BONUS: float = 0.03        # d5 耗竭固定加分


def configure_delta_params(
    *,
    d3_scale: Optional[float] = None,
    d3_cap: Optional[float] = None,
    d5_threshold: Optional[float] = None,
    d5_bonus: Optional[float] = None,
) -> None:
    """Override delta momentum bonus parameters (called at startup)."""
    global _DELTA_D3_SCALE, _DELTA_D3_CAP, _DELTA_D5_THRESHOLD, _DELTA_D5_BONUS
    if d3_scale is not None:
        _DELTA_D3_SCALE = d3_scale
    if d3_cap is not None:
        _DELTA_D3_CAP = d3_cap
    if d5_threshold is not None:
        _DELTA_D5_THRESHOLD = d5_threshold
    if d5_bonus is not None:
        _DELTA_D5_BONUS = d5_bonus


def _delta_momentum_bonus(
    d3: Optional[float],
    d5: Optional[float],
    action: str,
) -> Tuple[float, str]:
    """根据 delta 指标计算均值回归策略的置信度修正。

    核心逻辑：
    - 买入信号 + 短期 delta > 0（指标已开始回升）→ 反转确认，加分
    - 卖出信号 + 短期 delta < 0（指标已开始回落）→ 反转确认，加分
    - 中期 delta 幅度大（|d5| 大）→ 深度超买超卖后反弹概率高，微加分

    返回 (bonus, reason_suffix)。
    """
    bonus = 0.0
    parts: list[str] = []

    if d3 is not None:
        if action == "buy" and d3 > 0:
            bonus += min(d3 / _DELTA_D3_SCALE, _DELTA_D3_CAP)
            parts.append(f"d3_bounce={d3:+.1f}")
        elif action == "sell" and d3 < 0:
            bonus += min(abs(d3) / _DELTA_D3_SCALE, _DELTA_D3_CAP)
            parts.append(f"d3_bounce={d3:+.1f}")

    if d5 is not None:
        if action == "buy" and d5 < -_DELTA_D5_THRESHOLD:
            bonus += _DELTA_D5_BONUS
            parts.append(f"d5_exhaust={d5:+.1f}")
        elif action == "sell" and d5 > _DELTA_D5_THRESHOLD:
            bonus += _DELTA_D5_BONUS
            parts.append(f"d5_exhaust={d5:+.1f}")

    suffix = ",".join(parts) if parts else ""
    return bonus, suffix


class RsiReversionStrategy:
    """Mean reversion signal based on RSI overbought/oversold zones.

    Receives both intrabar and confirmed snapshots.  RSI extreme readings
    (≤30 oversold, ≥70 overbought) are meaningful in real time — the deepest
    extreme often occurs mid-bar before price reverts.  Bar-close confirmation
    verifies the reading was sustained through candle close.
    """

    name = "rsi_reversion"
    category = "reversion"
    required_indicators = ("rsi14",)
    preferred_scopes = ("intrabar", "confirmed")

    regime_affinity = {
        RegimeType.TRENDING:  0.25,
        RegimeType.RANGING:   1.00,
        RegimeType.BREAKOUT:  0.35,
        RegimeType.UNCERTAIN: 0.60,
    }

    def __init__(self, *, overbought: float = 70, oversold: float = 30) -> None:
        self._overbought = overbought
        self._oversold = oversold

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
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicator:rsi",
                used_indicators=[rsi_name] if rsi_name else ["rsi14"],
            )

        rsi = rsi_value
        tf = context.timeframe
        overbought = get_tf_param(self, "overbought", tf, self._overbought)
        oversold = get_tf_param(self, "oversold", tf, self._oversold)
        if rsi <= oversold:
            action = "buy"
            confidence = min((oversold - rsi) / 30 + 0.4, 1.0)
        elif rsi >= overbought:
            action = "sell"
            confidence = min((rsi - overbought) / 30 + 0.4, 1.0)
        else:
            action = "hold"
            confidence = 0.2

        delta_suffix = ""
        if action in ("buy", "sell"):
            d3 = _get_delta(context.indicators, "rsi14", "rsi", 3)
            d5 = _get_delta(context.indicators, "rsi14", "rsi", 5)
            bonus, delta_suffix = _delta_momentum_bonus(d3, d5, action)
            confidence = min(confidence + bonus, 1.0)

        reason = f"rsi={rsi:.2f}"
        if delta_suffix:
            reason += f",{delta_suffix}"

        # HTF RSI confirmation — same-direction extreme validates reversion
        # HTF TF 由 signal.ini [strategy_htf] 驱动，不硬编码 TF
        htf_bonus = 0.0
        htf_rsi = None
        for _tf, _htf_data in context.htf_indicators.items():
            _val = _htf_data.get("rsi14", {}).get("rsi")
            if _val is not None:
                htf_rsi = _val
                break
        if htf_rsi is not None and action in ("buy", "sell"):
            if action == "buy" and htf_rsi < 40:
                htf_bonus = 0.06  # HTF also leaning oversold
            elif action == "sell" and htf_rsi > 60:
                htf_bonus = 0.06  # HTF also leaning overbought
        confidence = min(confidence + htf_bonus, 1.0)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=reason,
            used_indicators=[rsi_name] if rsi_name else ["rsi14"],
            metadata={"rsi": rsi, "rsi_indicator": rsi_name or "rsi14", "htf_bonus": htf_bonus},
        )


class WilliamsRStrategy:
    """基于 Williams %R 超买超卖的均值回归策略。

    Williams %R 与 RSI 互补——%R 基于最高价/最低价计算，响应速度比 RSI 快
    1-2 根 bar，在识别短期极值方面更敏感。

    范围：-100（超卖）~ 0（超买）
    - %R ≤ -80 → 深度超卖 → buy（越低置信度越高）
    - %R ≥ -20 → 深度超买 → sell（越高置信度越高）

    同时支持 intrabar 和 confirmed：%R 极值常在 bar 中间达到顶点。
    """

    name = "williams_r"
    category = "reversion"
    required_indicators = ("williamsr14",)
    preferred_scopes = ("intrabar", "confirmed")
    regime_affinity = {
        RegimeType.TRENDING:  0.20,
        RegimeType.RANGING:   1.00,
        RegimeType.BREAKOUT:  0.30,
        RegimeType.UNCERTAIN: 0.55,
    }

    def __init__(self, *, overbought: float = -20, oversold: float = -80) -> None:
        self._overbought = overbought
        self._oversold = oversold

    def evaluate(self, context: SignalContext) -> SignalDecision:
        wr_value, wr_name = _resolve_indicator_value(
            context.indicators,
            (
                ("williamsr14", "williams_r"),
                ("williamsr14", "value"),
                ("williamsr", "williams_r"),
            ),
        )
        used = [wr_name] if wr_name else ["williamsr14"]

        if wr_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicator:williams_r",
                used_indicators=used,
            )

        wr = wr_value  # 范围 -100 ~ 0
        tf = context.timeframe
        overbought = get_tf_param(self, "overbought", tf, self._overbought)
        oversold = get_tf_param(self, "oversold", tf, self._oversold)
        if wr <= oversold:
            action = "buy"
            depth = (oversold - wr) / 20.0  # 0~1
            confidence = min(0.45 + depth * 0.45, 0.90)
        elif wr >= overbought:
            action = "sell"
            depth = (wr - overbought) / 20.0  # 0~1
            confidence = min(0.45 + depth * 0.45, 0.90)
        else:
            action = "hold"
            confidence = 0.15

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=f"williams_r={wr:.2f}",
            used_indicators=used,
            metadata={"williams_r": wr},
        )


class CciReversionStrategy:
    """基于 CCI 顺势指标的超买超卖均值回归策略。

    CCI（Commodity Channel Index）衡量价格与移动平均线的偏差程度，
    对黄金的均值回归特性尤为适合——XAUUSD 价格倾向于围绕均值震荡。

    标准信号线：
    - CCI ≤ -100 → 超卖 → buy（越负置信度越高）
    - CCI ≥ +100 → 超买 → sell（越正置信度越高）
    - CCI ±200 以上视为极端区域，置信度最高

    同时支持 intrabar 和 confirmed：CCI 极值常在 bar 中间出现。
    """

    name = "cci_reversion"
    category = "reversion"
    required_indicators = ("cci20",)
    preferred_scopes = ("intrabar", "confirmed")
    regime_affinity = {
        RegimeType.TRENDING:  0.30,
        RegimeType.RANGING:   0.95,
        RegimeType.BREAKOUT:  0.50,
        RegimeType.UNCERTAIN: 0.60,
    }

    def __init__(
        self, *, upper_threshold: float = 100, lower_threshold: float = -100,
    ) -> None:
        self._upper_threshold = upper_threshold
        self._lower_threshold = lower_threshold

    def evaluate(self, context: SignalContext) -> SignalDecision:
        cci_value, cci_name = _resolve_indicator_value(
            context.indicators,
            (
                ("cci20", "cci"),
                ("cci20", "value"),
                ("cci", "cci"),
            ),
        )
        used = [cci_name] if cci_name else ["cci20"]

        if cci_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicator:cci",
                used_indicators=used,
            )

        cci = cci_value
        tf = context.timeframe
        upper = get_tf_param(self, "upper_threshold", tf, self._upper_threshold)
        lower = get_tf_param(self, "lower_threshold", tf, self._lower_threshold)
        if cci <= lower:
            action = "buy"
            excess = min(abs(cci) - abs(lower), 200) / 200.0
            confidence = min(0.45 + excess * 0.45, 0.90)
        elif cci >= upper:
            action = "sell"
            excess = min(cci - upper, 200) / 200.0
            confidence = min(0.45 + excess * 0.45, 0.90)
        else:
            action = "hold"
            confidence = 0.10

        delta_suffix = ""
        if action in ("buy", "sell"):
            d3 = _get_delta(context.indicators, "cci20", "cci", 3)
            d5 = _get_delta(context.indicators, "cci20", "cci", 5)
            # CCI 的量纲比 RSI 大约 3-5 倍，调整阈值
            bonus = 0.0
            parts: list[str] = []
            if d3 is not None:
                if action == "buy" and d3 > 0:
                    bonus += min(d3 / 30.0, 0.05)
                    parts.append(f"d3_bounce={d3:+.1f}")
                elif action == "sell" and d3 < 0:
                    bonus += min(abs(d3) / 30.0, 0.05)
                    parts.append(f"d3_bounce={d3:+.1f}")
            if d5 is not None:
                if action == "buy" and d5 < -25:
                    bonus += 0.03
                    parts.append(f"d5_exhaust={d5:+.1f}")
                elif action == "sell" and d5 > 25:
                    bonus += 0.03
                    parts.append(f"d5_exhaust={d5:+.1f}")
            confidence = min(confidence + bonus, 0.95)
            delta_suffix = ",".join(parts)

        reason = f"cci={cci:.2f}"
        if delta_suffix:
            reason += f",{delta_suffix}"

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=reason,
            used_indicators=used,
            metadata={"cci": cci},
        )


class StochRsiStrategy:
    """基于 Stochastic RSI 的超买超卖策略。

    Stochastic RSI 比普通 RSI 更敏感，能提前捕捉黄金动量耗尽：
    - StochRSI_K < 20 且 K > D（死叉结束，动量向上）→ BUY
    - StochRSI_K > 80 且 K < D（金叉结束，动量向下）→ SELL
    - 过滤掉震荡信号：只有 K 线与 D 线有明确交叉方向时才发信号

    支持 intrabar 快照——Stochastic RSI 的极值常发生在 bar 中间。
    """

    name = "stoch_rsi"
    category = "reversion"
    required_indicators = ("stoch_rsi14",)
    preferred_scopes = ("intrabar", "confirmed")
    regime_affinity = {
        RegimeType.TRENDING:  0.25,
        RegimeType.RANGING:   1.00,
        RegimeType.BREAKOUT:  0.30,
        RegimeType.UNCERTAIN: 0.60,
    }

    def __init__(self, *, overbought: float = 80, oversold: float = 20) -> None:
        self._overbought = overbought
        self._oversold = oversold

    def evaluate(self, context: SignalContext) -> SignalDecision:
        k_value, k_name = _resolve_indicator_value(
            context.indicators,
            (
                ("stoch_rsi14", "stoch_rsi_k"),
                ("stoch_rsi", "stoch_rsi_k"),
            ),
        )
        d_value, _ = _resolve_indicator_value(
            context.indicators,
            (
                ("stoch_rsi14", "stoch_rsi_d"),
                ("stoch_rsi", "stoch_rsi_d"),
            ),
        )
        used = [k_name] if k_name else ["stoch_rsi14"]

        if k_value is None or d_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_required_indicator:stoch_rsi",
                used_indicators=used,
            )

        k = k_value
        d = d_value
        tf = context.timeframe
        overbought = get_tf_param(self, "overbought", tf, self._overbought)
        oversold = get_tf_param(self, "oversold", tf, self._oversold)

        if k < oversold and k > d:
            action = "buy"
            depth = (oversold - k) / 20.0
            cross_strength = min((k - d) / 5.0, 1.0)
            confidence = min(0.5 + depth * 0.3 + cross_strength * 0.2, 1.0)
        elif k > overbought and k < d:
            action = "sell"
            depth = (k - overbought) / 20.0
            cross_strength = min((d - k) / 5.0, 1.0)
            confidence = min(0.5 + depth * 0.3 + cross_strength * 0.2, 1.0)
        else:
            action = "hold"
            confidence = 0.15

        delta_suffix = ""
        if action in ("buy", "sell"):
            k_d3 = _get_delta(context.indicators, "stoch_rsi14", "stoch_rsi_k", 3)
            k_d5 = _get_delta(context.indicators, "stoch_rsi14", "stoch_rsi_k", 5)
            bonus, delta_suffix = _delta_momentum_bonus(k_d3, k_d5, action)
            confidence = min(confidence + bonus, 1.0)

        reason = f"stoch_rsi_k={k:.2f},d={d:.2f}"
        if delta_suffix:
            reason += f",{delta_suffix}"

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=reason,
            used_indicators=used,
            metadata={"stoch_rsi_k": k, "stoch_rsi_d": d},
        )


def _compute_rsi_series(closes: List[float], period: int = 14) -> List[float]:
    """从 close 序列计算 RSI 序列（Wilder 平滑）。

    返回长度 = len(closes) - period 的 RSI 列表，
    索引 0 对应 closes[period] 的 RSI。
    """
    if len(closes) <= period:
        return []
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas[:period]]
    losses = [max(-d, 0.0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsi_values: List[float] = []
    if avg_loss == 0.0:
        rsi_values.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(100.0 - 100.0 / (1.0 + rs))
    for i in range(period, len(deltas)):
        d = deltas[i]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        if avg_loss == 0.0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - 100.0 / (1.0 + rs))
    return rsi_values


class MacdDivergenceStrategy:
    """MACD 背离策略 — 价格与 MACD 柱状图方向分歧的反转预警。

    原理：价格创新高但 MACD 柱状图未跟随（顶背离），
    或价格创新低但 MACD 柱状图未跟随（底背离）。

    与 RsiDivergence 的区别：
    - RSI 背离检测振荡器极值分歧
    - MACD 背离检测动量衰竭（histogram 缩小 = 多空力量消退）
    - MACD 对趋势末期的快慢均线收敛更敏感

    仅在 confirmed scope 评估（需要完整 bar 确认高低点）。
    """

    name = "macd_divergence"
    category = "reversion"
    required_indicators = ("macd",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 0.80,   # 趋势末端背离最可靠
        RegimeType.RANGING: 0.50,
        RegimeType.BREAKOUT: 0.35,
        RegimeType.UNCERTAIN: 0.60,
    }

    def __init__(self, *, lookback_bars: int = 14) -> None:
        self._lookback_bars = lookback_bars
        self.recent_bars_depth: int = lookback_bars + 5

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used: List[str] = ["macd"]
        hold = SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.0,
            reason="no_macd_divergence",
            used_indicators=used,
        )

        macd_data = context.indicators.get("macd")
        if not isinstance(macd_data, dict):
            return hold

        current_hist = macd_data.get("hist")
        current_macd = macd_data.get("macd")
        if current_hist is None or current_macd is None:
            return hold

        recent_bars: Any = context.metadata.get("recent_bars")
        if not isinstance(recent_bars, (list, tuple)) or len(recent_bars) < self._lookback_bars:
            return hold

        # 提取价格序列
        all_highs: List[float] = []
        all_lows: List[float] = []
        all_closes: List[float] = []
        for bar in recent_bars:
            if isinstance(bar, dict):
                h, lo, c = bar.get("high"), bar.get("low"), bar.get("close")
            elif hasattr(bar, "high"):
                h = getattr(bar, "high", None)
                lo = getattr(bar, "low", None)
                c = getattr(bar, "close", None)
            else:
                continue
            if h is not None and lo is not None and c is not None:
                try:
                    all_highs.append(float(h))
                    all_lows.append(float(lo))
                    all_closes.append(float(c))
                except (TypeError, ValueError):
                    continue

        n = min(self._lookback_bars, len(all_highs))
        if n < 5:
            return hold

        highs = all_highs[-n:]
        lows = all_lows[-n:]

        # 从 close 序列计算简化 MACD histogram 序列
        # 用快慢 EMA 差值的变化趋势作为 histogram 代理
        fast_k = 2.0 / (12 + 1)
        slow_k = 2.0 / (26 + 1)
        # 用所有可用的 close 计算 MACD 序列
        all_for_macd = all_closes[-(n + 30):]  # 多取一些做 warmup
        if len(all_for_macd) < 26:
            return hold

        ema_f = all_for_macd[0]
        ema_s = all_for_macd[0]
        macd_hist_series: List[float] = []
        for price in all_for_macd:
            ema_f = ema_f + fast_k * (price - ema_f)
            ema_s = ema_s + slow_k * (price - ema_s)
            macd_hist_series.append(ema_f - ema_s)

        # 取最后 n 个与价格对齐
        hist_aligned = macd_hist_series[-n:]
        current_high = highs[-1]
        current_low = lows[-1]

        # 搜索前半段极值（排除最近 2 根）
        search_end = len(highs) - 2
        if search_end < 2:
            return hold

        prev_highest_idx = max(range(search_end), key=lambda i: highs[i])
        prev_lowest_idx = min(range(search_end), key=lambda i: lows[i])
        prev_high = highs[prev_highest_idx]
        prev_low = lows[prev_lowest_idx]
        prev_high_hist = hist_aligned[prev_highest_idx]
        prev_low_hist = hist_aligned[prev_lowest_idx]
        current_hist_val = hist_aligned[-1]

        action = "hold"
        confidence = 0.0
        divergence_type = ""

        # 顶背离：价格创新高但 MACD histogram 下降
        if current_high >= prev_high * 0.999:
            hist_drop = prev_high_hist - current_hist_val
            if hist_drop > 0 and prev_high_hist > 0:
                action = "sell"
                divergence_type = "bearish_macd_divergence"
                strength = min(hist_drop / max(abs(prev_high_hist), 0.01), 1.0)
                confidence = 0.50 + strength * 0.28

        # 底背离：价格创新低但 MACD histogram 上升
        if action == "hold" and current_low <= prev_low * 1.001:
            hist_rise = current_hist_val - prev_low_hist
            if hist_rise > 0 and prev_low_hist < 0:
                action = "buy"
                divergence_type = "bullish_macd_divergence"
                strength = min(hist_rise / max(abs(prev_low_hist), 0.01), 1.0)
                confidence = 0.50 + strength * 0.28

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=min(confidence, 0.85),
            reason=f"macd_div:{divergence_type},hist={current_hist_val:.4f}",
            used_indicators=used,
            metadata={
                "macd_hist": current_hist_val,
                "divergence_type": divergence_type,
                "current_high": current_high,
                "current_low": current_low,
            },
        )


class RsiDivergenceStrategy:
    """RSI 背离策略 — 价格与 RSI 方向分歧的反转预警。

    原理：价格创新高但 RSI 未创新高（顶背离，bearish divergence），
    或价格创新低但 RSI 未创新低（底背离，bullish divergence）。

    实现：从 recent_bars 的 close 序列计算 RSI 序列，
    在两个价格极值点上对比各自对应的 RSI 值。

    与 RsiReversionStrategy 的区别：
    - RsiReversion 看 RSI 绝对值（超买超卖区域）
    - RsiDivergence 看价格与 RSI 的方向分歧（相对关系）

    仅在 confirmed scope 评估（需要完整 bar 确认高低点）。
    """

    name = "rsi_divergence"
    category = "reversion"
    required_indicators = ("rsi14",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 0.75,   # 趋势末端背离信号最可靠
        RegimeType.RANGING: 0.60,
        RegimeType.BREAKOUT: 0.40,
        RegimeType.UNCERTAIN: 0.65,
    }

    _RSI_PERIOD: int = 14

    def __init__(self, *, lookback_bars: int = 14) -> None:
        self._lookback_bars = lookback_bars
        # 需要额外 RSI_PERIOD 根 bar 作为 RSI 计算的 warmup
        self.recent_bars_depth: int = lookback_bars + self._RSI_PERIOD

    def evaluate(self, context: SignalContext) -> SignalDecision:
        rsi_value, rsi_name = _resolve_indicator_value(
            context.indicators,
            (
                ("rsi14", "rsi"),
                ("rsi14", "value"),
                ("rsi", "rsi"),
            ),
        )
        used: List[str] = [rsi_name] if rsi_name else ["rsi14"]

        if rsi_value is None:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="missing_rsi",
                used_indicators=used,
            )

        recent_bars: Any = context.metadata.get("recent_bars")
        min_bars_needed = self._lookback_bars + self._RSI_PERIOD
        if not isinstance(recent_bars, (list, tuple)) or len(recent_bars) < min_bars_needed:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="insufficient_recent_bars",
                used_indicators=used,
            )

        # 提取 OHLC
        all_highs: List[float] = []
        all_lows: List[float] = []
        all_closes: List[float] = []
        for bar in recent_bars:
            if isinstance(bar, dict):
                h, lo, c = bar.get("high"), bar.get("low"), bar.get("close")
            elif hasattr(bar, "high"):
                h = getattr(bar, "high", None)
                lo = getattr(bar, "low", None)
                c = getattr(bar, "close", None)
            else:
                continue
            if h is not None and lo is not None and c is not None:
                try:
                    all_highs.append(float(h))
                    all_lows.append(float(lo))
                    all_closes.append(float(c))
                except (TypeError, ValueError):
                    continue

        if len(all_closes) < min_bars_needed:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="insufficient_valid_bars",
                used_indicators=used,
            )

        # 计算 RSI 序列
        rsi_series = _compute_rsi_series(all_closes, self._RSI_PERIOD)
        # rsi_series[i] 对应 all_closes[i + RSI_PERIOD]
        # 截取最后 lookback_bars 段（与价格对齐）
        n = min(self._lookback_bars, len(rsi_series))
        if n < 5:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="insufficient_rsi_series",
                used_indicators=used,
            )

        # 对齐：取最后 n 根 bar 的价格和 RSI
        highs = all_highs[-n:]
        lows = all_lows[-n:]
        rsi_aligned = rsi_series[-n:]
        current_rsi = rsi_value  # 使用指标系统的精确值
        current_high = highs[-1]
        current_low = lows[-1]

        # 在前半段（排除最近 2 根）搜索价格极值
        search_end = len(highs) - 2
        if search_end < 2:
            return SignalDecision(
                strategy=self.name,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction="hold",
                confidence=0.0,
                reason="not_enough_bars_for_divergence",
                used_indicators=used,
            )

        prev_highest_idx = max(range(search_end), key=lambda i: highs[i])
        prev_lowest_idx = min(range(search_end), key=lambda i: lows[i])
        prev_high = highs[prev_highest_idx]
        prev_low = lows[prev_lowest_idx]
        prev_high_rsi = rsi_aligned[prev_highest_idx]
        prev_low_rsi = rsi_aligned[prev_lowest_idx]

        action = "hold"
        confidence = 0.0
        divergence_type = ""

        # 顶背离：价格创新高（或持平）但 RSI 低于前高点的 RSI
        if current_high >= prev_high * 0.999:
            rsi_drop = prev_high_rsi - current_rsi
            if rsi_drop > 3.0:
                action = "sell"
                divergence_type = "bearish_divergence"
                divergence_strength = min(rsi_drop / 15.0, 1.0)
                confidence = 0.50 + divergence_strength * 0.30
                if current_rsi > 55:
                    confidence += 0.06

        # 底背离：价格创新低（或持平）但 RSI 高于前低点的 RSI
        if action == "hold" and current_low <= prev_low * 1.001:
            rsi_rise = current_rsi - prev_low_rsi
            if rsi_rise > 3.0:
                action = "buy"
                divergence_type = "bullish_divergence"
                divergence_strength = min(rsi_rise / 15.0, 1.0)
                confidence = 0.50 + divergence_strength * 0.30
                if current_rsi < 45:
                    confidence += 0.06

        if action == "hold":
            confidence = 0.0

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=min(confidence, 0.88),
            reason=f"rsi_divergence:{divergence_type},rsi={current_rsi:.1f}",
            used_indicators=used,
            metadata={
                "rsi": current_rsi,
                "divergence_type": divergence_type,
                "current_high": current_high,
                "current_low": current_low,
                "prev_high": prev_high,
                "prev_low": prev_low,
                "prev_high_rsi": prev_high_rsi if action != "hold" else None,
                "prev_low_rsi": prev_low_rsi if action != "hold" else None,
            },
        )


class VwapReversionStrategy:
    """VWAP 均值回归策略 — 价格偏离 VWAP 标准差带时回归入场。

    VWAP（成交量加权平均价）是日内机构的基准价格。
    价格偏离 VWAP 超过 ±band_sigma 个标准差时，
    预期回归到 VWAP 附近。

    注意：需要 vwap30 指标启用（indicators.json 中 enabled=true）。
    Demo 账户无真实成交量时，指标数据为空，策略自动 hold。

    接收 intrabar + confirmed：VWAP 带的触及是实时事件。
    """

    name = "vwap_reversion"
    category = "reversion"
    required_indicators = ("vwap30",)
    preferred_scopes = ("intrabar", "confirmed")
    regime_affinity = {
        RegimeType.TRENDING: 0.25,
        RegimeType.RANGING: 1.00,
        RegimeType.BREAKOUT: 0.30,
        RegimeType.UNCERTAIN: 0.55,
    }

    _band_sigma: float = 1.5  # 偏离 N 个标准差时触发

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used: List[str] = ["vwap30"]
        hold = SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.0,
            reason="no_vwap_setup",
            used_indicators=used,
        )

        vwap_data = context.indicators.get("vwap30")
        if not isinstance(vwap_data, dict):
            return hold

        vwap = vwap_data.get("vwap")
        upper = vwap_data.get("upper_band")
        lower = vwap_data.get("lower_band")
        std_dev = vwap_data.get("std_dev")

        if vwap is None or upper is None or lower is None:
            return hold

        close_val = vwap_data.get("close")
        if close_val is None:
            close_val = context.metadata.get("close")
        if close_val is None:
            return hold

        tf = context.timeframe
        band_sigma = get_tf_param(self, "band_sigma", tf, self._band_sigma)

        # 计算实际偏离（以标准差为单位）
        if std_dev is not None and std_dev > 0:
            deviation = (close_val - vwap) / std_dev
        elif vwap > 0:
            # fallback：用 band 宽度估算
            band_width = upper - lower
            if band_width > 0:
                deviation = (close_val - vwap) / (band_width / (2 * band_sigma))
            else:
                return hold
        else:
            return hold

        action = "hold"
        confidence = 0.0

        if deviation >= band_sigma:
            # 价格在 VWAP 上方 N σ → 预期回落 → sell
            action = "sell"
            excess = deviation - band_sigma
            confidence = min(0.50 + excess * 0.15, 0.88)
        elif deviation <= -band_sigma:
            # 价格在 VWAP 下方 N σ → 预期回升 → buy
            action = "buy"
            excess = abs(deviation) - band_sigma
            confidence = min(0.50 + excess * 0.15, 0.88)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=f"vwap_dev={deviation:.2f}sigma,band={band_sigma:.1f}",
            used_indicators=used,
            metadata={
                "vwap": vwap,
                "close": close_val,
                "deviation_sigma": round(deviation, 3),
                "upper_band": upper,
                "lower_band": lower,
            },
        )
