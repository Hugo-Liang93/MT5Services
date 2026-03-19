"""均值回归策略

包含基于超买超卖指标的反转策略，适合震荡行情（ADX 低位）。
"""

from __future__ import annotations

import logging

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import _resolve_indicator_value

logger = logging.getLogger(__name__)


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
    regime_affinity = {
        RegimeType.TRENDING:  0.25,  # 强趋势中 RSI 可长时间维持极值，逆势信号危险
        RegimeType.RANGING:   1.00,  # 震荡区间均值回归是 RSI 的核心应用场景
        RegimeType.BREAKOUT:  0.35,  # 突破初期 RSI 极值往往持续，不宜逆势
        RegimeType.UNCERTAIN: 0.60,
    }

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


class StochRsiStrategy:
    """基于 Stochastic RSI 的超买超卖策略。

    Stochastic RSI 比普通 RSI 更敏感，能提前捕捉黄金动量耗尽：
    - StochRSI_K < 20 且 K > D（死叉结束，动量向上）→ BUY
    - StochRSI_K > 80 且 K < D（金叉结束，动量向下）→ SELL
    - 过滤掉震荡信号：只有 K 线与 D 线有明确交叉方向时才发信号

    支持 intrabar 快照——Stochastic RSI 的极值常发生在 bar 中间。
    """

    name = "stoch_rsi"
    required_indicators = ("stoch_rsi14",)
    preferred_scopes = ("intrabar", "confirmed")
    regime_affinity = {
        RegimeType.TRENDING:  0.25,  # 趋势中随机指标长时间嵌顶/嵌底，逆势陷阱
        RegimeType.RANGING:   1.00,  # 振荡市 K/D 交叉的核心使用场景
        RegimeType.BREAKOUT:  0.30,  # 突破前 StochRSI 往往已超买/超卖，不可逆势
        RegimeType.UNCERTAIN: 0.60,
    }

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

        if k < 20 and k > d:
            action = "buy"
            depth = (20.0 - k) / 20.0
            cross_strength = min((k - d) / 5.0, 1.0)
            confidence = min(0.5 + depth * 0.3 + cross_strength * 0.2, 1.0)
        elif k > 80 and k < d:
            action = "sell"
            depth = (k - 80.0) / 20.0
            cross_strength = min((d - k) / 5.0, 1.0)
            confidence = min(0.5 + depth * 0.3 + cross_strength * 0.2, 1.0)
        else:
            action = "hold"
            confidence = 0.15

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"stoch_rsi_k={k:.2f},d={d:.2f}",
            used_indicators=used,
            metadata={"stoch_rsi_k": k, "stoch_rsi_d": d},
        )
