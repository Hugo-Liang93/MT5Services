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
    category = "reversion"
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
        RegimeType.TRENDING:  0.20,  # 强趋势中 %R 会长时间停留极值区，逆势陷阱
        RegimeType.RANGING:   1.00,  # 震荡市超买超卖是核心反转场景
        RegimeType.BREAKOUT:  0.30,  # 突破前 %R 极值往往持续，不宜逆势
        RegimeType.UNCERTAIN: 0.55,
    }

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
        if wr <= -80:
            action = "buy"
            # 越接近 -100 置信度越高
            depth = (-80 - wr) / 20.0  # 0~1
            confidence = min(0.45 + depth * 0.45, 0.90)
        elif wr >= -20:
            action = "sell"
            # 越接近 0 置信度越高
            depth = (wr - (-20)) / 20.0  # 0~1
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
        RegimeType.TRENDING:  0.30,  # 趋势中 CCI 可长期处于极值区，逆势危险
        RegimeType.RANGING:   0.95,  # 震荡市均值回归是 CCI 的核心应用场景
        RegimeType.BREAKOUT:  0.50,  # CCI 突破 ±100 也可作为突破方向确认
        RegimeType.UNCERTAIN: 0.60,
    }

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
        if cci <= -100:
            action = "buy"
            # 超过 -100 基础 0.45，每超出 100 点加 0.15，上限 0.90
            excess = min(abs(cci) - 100, 200) / 200.0
            confidence = min(0.45 + excess * 0.45, 0.90)
        elif cci >= 100:
            action = "sell"
            excess = min(cci - 100, 200) / 200.0
            confidence = min(0.45 + excess * 0.45, 0.90)
        else:
            action = "hold"
            confidence = 0.10

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            action=action,
            confidence=confidence,
            reason=f"cci={cci:.2f}",
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
