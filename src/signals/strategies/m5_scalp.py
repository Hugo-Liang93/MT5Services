"""M5 专用快速标量策略 — 基于 intrabar 实时信号

M5 特点：bar 周期短（5 分钟），价格变化快，需要更快反应。
核心设计：
  - preferred_scopes 包含 intrabar，盘中实时捕捉极值（不等 bar close）
  - RSI 触极端值时立即入场（比 confirmed 策略更紧的阈值）
  - HTF 方向过滤：只做顺势交易，避免逆势被趋势碾压
  - 专为 M5 TF 设计，不建议用于更高 TF
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import get_tf_param

logger = logging.getLogger(__name__)


class M5ScalpRSI:
    """M5 RSI 极值快速标量 — intrabar + HTF 方向过滤。

    利用 M5 intrabar 的 RSI 数据在盘中实时捕捉超买超卖极值，
    结合 HTF（默认 M30）supertrend 方向过滤逆势信号。

    与 rsi_reversion 的区别：
      - RSI 阈值更极端（默认 25/75 vs 30/70）→ 更少但更精准的信号
      - 强制 HTF 方向过滤 → 只做顺势回调，不做逆势赌博
      - intrabar 优先 → 盘中触值即入场，不等 bar close

    入场条件（以做多为例）：
      1. HTF supertrend direction = 1（大周期向上）
      2. M5 RSI ≤ oversold（盘中触及极值 = 超卖回调到位）
      3. 置信度由 RSI 深度 + HTF ADX 强度决定
    """

    name = "m5_scalp_rsi"
    category = "reversion"
    required_indicators = ("rsi14",)
    preferred_scopes = ("intrabar", "confirmed")

    regime_affinity = {
        RegimeType.TRENDING: 0.80,    # 顺势回调是核心场景
        RegimeType.RANGING: 0.60,     # 震荡中 RSI 极值也有效
        RegimeType.BREAKOUT: 0.30,    # 突破中 RSI 极值可能是假信号
        RegimeType.UNCERTAIN: 0.15,
    }

    _overbought: float = 75.0
    _oversold: float = 25.0
    _htf_adx_min: float = 18.0  # HTF ADX 门槛较低（M30 ADX 整体偏低）

    def __init__(
        self,
        *,
        name: str | None = None,
        htf: str = "M30",
        regime_affinity: dict[RegimeType, float] | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        self._htf = htf
        if regime_affinity is not None:
            self.regime_affinity = dict(regime_affinity)

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used = ["rsi14"]
        hold = SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.0,
            reason="no_setup",
            used_indicators=used,
        )

        # ── 1. LTF RSI（M5 当前 bar 或 intrabar） ──
        rsi_data = context.indicators.get("rsi14", {})
        rsi = rsi_data.get("rsi")
        if rsi is None:
            return hold

        overbought = get_tf_param(self, "overbought", context.timeframe, self._overbought)
        oversold = get_tf_param(self, "oversold", context.timeframe, self._oversold)

        # RSI 不在极值区间 → 不入场
        if oversold < rsi < overbought:
            return hold

        # ── 2. HTF 方向过滤 ──
        htf_data = context.htf_indicators.get(self._htf, {})
        htf_st = htf_data.get("supertrend14", {})
        htf_dir_raw = htf_st.get("direction")
        htf_adx = htf_data.get("adx14", {}).get("adx")

        # 有 HTF 数据时做方向过滤；无 HTF 数据时仍允许入场（降级为纯 RSI）
        htf_direction: int | None = None
        htf_filtered = False
        if htf_dir_raw is not None:
            try:
                htf_direction = int(htf_dir_raw)
            except (TypeError, ValueError):
                pass

        htf_adx_min = get_tf_param(self, "htf_adx_min", context.timeframe, self._htf_adx_min)

        # ── 3. 入场逻辑 ──
        action = "hold"
        confidence = 0.0
        htf_label = self._htf.lower()

        if rsi <= oversold:
            # RSI 超卖 → 潜在做多
            if htf_direction is not None and htf_direction == -1:
                return hold  # HTF 向下，不逆势做多
            action = "buy"
            depth = (oversold - rsi) / 25.0  # 25→0: 0.0, 0→1.0
            confidence = 0.50 + min(depth, 1.0) * 0.30
            # HTF ADX 加成
            if htf_adx is not None and htf_adx >= htf_adx_min:
                adx_bonus = min((htf_adx - htf_adx_min) / 20.0, 1.0) * 0.15
                confidence += adx_bonus
                htf_filtered = True
            confidence = min(confidence, 0.95)

        elif rsi >= overbought:
            # RSI 超买 → 潜在做空
            if htf_direction is not None and htf_direction == 1:
                return hold  # HTF 向上，不逆势做空
            action = "sell"
            depth = (rsi - overbought) / 25.0
            confidence = 0.50 + min(depth, 1.0) * 0.30
            if htf_adx is not None and htf_adx >= htf_adx_min:
                adx_bonus = min((htf_adx - htf_adx_min) / 20.0, 1.0) * 0.15
                confidence += adx_bonus
                htf_filtered = True
            confidence = min(confidence, 0.95)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=f"rsi={rsi:.1f},{htf_label}_dir={htf_direction},htf_filtered={htf_filtered}",
            used_indicators=used,
            metadata={
                "htf": self._htf,
                "htf_direction": htf_direction,
                "htf_adx": htf_adx,
                "ltf_rsi": rsi,
            },
        )


class M5MomentumBurst:
    """M5 动量突发策略 — 双 TF ADX 共振 + RSI 动量方向。

    捕捉 M5 级别的动量爆发：当 HTF 和 LTF 的 ADX 都在上升
    且 RSI 处于顺势区间时入场。适合趋势加速阶段。

    与 DualTFMomentum 的区别：
      - 不要求 supertrend 同向（ADX 上升 + RSI 方向即可）
      - RSI 用方向而非极值（40-60 不入场，>60 做多，<40 做空）
      - 更适合 M5 的快速动量捕捉

    入场条件（以做多为例）：
      1. HTF supertrend direction = 1（方向过滤）
      2. LTF RSI > rsi_momentum_buy（动量向上，默认 60）
      3. LTF ADX > ltf_adx_min（M5 趋势成立）
      4. HTF ADX > htf_adx_min（大周期趋势成立）
    """

    name = "m5_momentum_burst"
    category = "breakout"
    required_indicators = ("rsi14", "adx14")
    preferred_scopes = ("intrabar", "confirmed")

    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.RANGING: 0.05,
        RegimeType.BREAKOUT: 0.70,
        RegimeType.UNCERTAIN: 0.10,
    }

    _htf_adx_min: float = 20.0
    _ltf_adx_min: float = 22.0
    _rsi_momentum_buy: float = 60.0   # RSI > 此值 = 动量向上
    _rsi_momentum_sell: float = 40.0  # RSI < 此值 = 动量向下

    def __init__(
        self,
        *,
        name: str | None = None,
        htf: str = "M30",
        regime_affinity: dict[RegimeType, float] | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        self._htf = htf
        if regime_affinity is not None:
            self.regime_affinity = dict(regime_affinity)

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used = ["rsi14", "adx14"]
        hold = SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.0,
            reason="no_setup",
            used_indicators=used,
        )

        # ── 1. LTF RSI + ADX ──
        rsi = context.indicators.get("rsi14", {}).get("rsi")
        ltf_adx = context.indicators.get("adx14", {}).get("adx")
        if rsi is None or ltf_adx is None:
            return hold

        ltf_adx_min = get_tf_param(self, "ltf_adx_min", context.timeframe, self._ltf_adx_min)
        if ltf_adx < ltf_adx_min:
            return hold

        rsi_buy = get_tf_param(self, "rsi_momentum_buy", context.timeframe, self._rsi_momentum_buy)
        rsi_sell = get_tf_param(self, "rsi_momentum_sell", context.timeframe, self._rsi_momentum_sell)

        # RSI 在中性区间 → 无动量方向
        if rsi_sell <= rsi <= rsi_buy:
            return hold

        # ── 2. HTF 方向 + ADX ──
        htf_data = context.htf_indicators.get(self._htf, {})
        if not htf_data:
            return hold

        htf_dir_raw = htf_data.get("supertrend14", {}).get("direction")
        htf_adx = htf_data.get("adx14", {}).get("adx")
        if htf_dir_raw is None or htf_adx is None:
            return hold

        try:
            htf_direction = int(htf_dir_raw)
        except (TypeError, ValueError):
            return hold

        htf_adx_min = get_tf_param(self, "htf_adx_min", context.timeframe, self._htf_adx_min)
        if htf_adx < htf_adx_min:
            return hold

        # ── 3. 方向确认 ──
        action = "hold"
        confidence = 0.0
        htf_label = self._htf.lower()

        if rsi > rsi_buy and htf_direction == 1:
            # RSI 动量向上 + HTF 向上 = 做多
            action = "buy"
            rsi_strength = min((rsi - rsi_buy) / 20.0, 1.0)
            adx_factor = min(ltf_adx / 35.0, 1.0) * min(htf_adx / 35.0, 1.0)
            confidence = 0.55 + 0.35 * rsi_strength * adx_factor

        elif rsi < rsi_sell and htf_direction == -1:
            # RSI 动量向下 + HTF 向下 = 做空
            action = "sell"
            rsi_strength = min((rsi_sell - rsi) / 20.0, 1.0)
            adx_factor = min(ltf_adx / 35.0, 1.0) * min(htf_adx / 35.0, 1.0)
            confidence = 0.55 + 0.35 * rsi_strength * adx_factor

        confidence = min(confidence, 0.95)

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=f"rsi={rsi:.1f},ltf_adx={ltf_adx:.1f},"
                   f"{htf_label}_dir={htf_direction},{htf_label}_adx={htf_adx:.1f}",
            used_indicators=used,
            metadata={
                "htf": self._htf,
                "htf_direction": htf_direction,
                "htf_adx": htf_adx,
                "ltf_rsi": rsi,
                "ltf_adx": ltf_adx,
            },
        )
