"""均值回归策略

包含基于超买超卖指标的反转策略，适合震荡行情（ADX 低位）。
"""

from __future__ import annotations

import logging

from typing import Dict, Optional, Tuple

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import _resolve_indicator_value

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
        # 短期反转确认：delta 方向与预期回归方向一致
        if action == "buy" and d3 > 0:
            bonus += min(d3 / 10.0, 0.05)  # 上限 +0.05
            parts.append(f"d3_bounce={d3:+.1f}")
        elif action == "sell" and d3 < 0:
            bonus += min(abs(d3) / 10.0, 0.05)
            parts.append(f"d3_bounce={d3:+.1f}")

    if d5 is not None:
        # 深度动量耗竭：大幅偏离暗示更强的均值回归概率
        if action == "buy" and d5 < -8:
            bonus += 0.03
            parts.append(f"d5_exhaust={d5:+.1f}")
        elif action == "sell" and d5 > 8:
            bonus += 0.03
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
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:rsi",
                used_indicators=[rsi_name] if rsi_name else ["rsi14"],
            )

        rsi = rsi_value
        if rsi <= self._oversold:
            action = "buy"
            confidence = min((self._oversold - rsi) / 30 + 0.4, 1.0)
        elif rsi >= self._overbought:
            action = "sell"
            confidence = min((rsi - self._overbought) / 30 + 0.4, 1.0)
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

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=reason,
            used_indicators=[rsi_name] if rsi_name else ["rsi14"],
            metadata={"rsi": rsi, "rsi_indicator": rsi_name or "rsi14"},
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
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:williams_r",
                used_indicators=used,
            )

        wr = wr_value  # 范围 -100 ~ 0
        if wr <= self._oversold:
            action = "buy"
            depth = (self._oversold - wr) / 20.0  # 0~1
            confidence = min(0.45 + depth * 0.45, 0.90)
        elif wr >= self._overbought:
            action = "sell"
            depth = (wr - self._overbought) / 20.0  # 0~1
            confidence = min(0.45 + depth * 0.45, 0.90)
        else:
            action = "hold"
            confidence = 0.15

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
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
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:cci",
                used_indicators=used,
            )

        cci = cci_value
        if cci <= self._lower_threshold:
            action = "buy"
            excess = min(abs(cci) - abs(self._lower_threshold), 200) / 200.0
            confidence = min(0.45 + excess * 0.45, 0.90)
        elif cci >= self._upper_threshold:
            action = "sell"
            excess = min(cci - self._upper_threshold, 200) / 200.0
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
            action=action,
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
                action="hold",
                confidence=0.0,
                reason="missing_required_indicator:stoch_rsi",
                used_indicators=used,
            )

        k = k_value
        d = d_value

        if k < self._oversold and k > d:
            action = "buy"
            depth = (self._oversold - k) / 20.0
            cross_strength = min((k - d) / 5.0, 1.0)
            confidence = min(0.5 + depth * 0.3 + cross_strength * 0.2, 1.0)
        elif k > self._overbought and k < d:
            action = "sell"
            depth = (k - self._overbought) / 20.0
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
            action=action,
            confidence=confidence,
            reason=reason,
            used_indicators=used,
            metadata={"stoch_rsi_k": k, "stoch_rsi_d": d},
        )
