"""多时间框架联动入场策略

H1 定方向 + M5 定入场：利用 HTF 趋势过滤低 TF 噪声，
在 M5 回调到有利位置时精确入场。

设计原则：
- H1 supertrend 确定趋势方向（必须有方向，否则 hold）
- H1 ADX 确认趋势强度（弱趋势不参与）
- M5 RSI 判断回调深度（顺势回调到中性区间 = 入场窗口）
- M5 price 相对 H1 EMA50 做顺势确认（不做逆 EMA 交易）
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import get_tf_param

logger = logging.getLogger(__name__)


class HTFTrendM5Entry:
    """H1 定方向 + M5 回调入场策略。

    运行在 M5 TF 上，通过 [strategy_htf] 注入 H1 指标。
    H1 supertrend 方向 + ADX 强度 → 确定做多/做空方向；
    M5 RSI 回调到中性区间 → 触发入场。

    入场逻辑（以做多为例）：
    1. H1 supertrend 方向 = buy（价格在 supertrend 线上方）
    2. H1 ADX >= adx_min（趋势足够强）
    3. M5 close > H1 EMA50（价格在大周期均线上方，顺势确认）
    4. M5 RSI 从高位回调到 pullback 区间（40-55）→ 回调到位，入场

    优势：
    - M5 保留活跃性（RSI 回调频繁），但只做与 H1 同向的交易
    - 避免纯 M5 策略的噪声问题（HTF 方向过滤）
    - 入场点在回调末尾而非趋势启动点（成本更低）
    """

    name = "htf_trend_m5_entry"
    category = "multi_tf"
    required_indicators = ("rsi14", "atr14")
    preferred_scopes = ("confirmed",)

    regime_affinity = {
        RegimeType.TRENDING: 1.00,   # 核心场景：趋势明确时跟随 H1 方向
        RegimeType.RANGING: 0.10,    # 震荡中 H1 无方向，几乎不触发
        RegimeType.BREAKOUT: 0.60,   # 突破后趋势初建，可参与
        RegimeType.UNCERTAIN: 0.30,  # 方向不明时保守
    }

    # Per-TF 可调参数默认值
    _adx_min: float = 22.0
    _rsi_pullback_buy_low: float = 38.0
    _rsi_pullback_buy_high: float = 55.0
    _rsi_pullback_sell_low: float = 45.0
    _rsi_pullback_sell_high: float = 62.0
    _min_atr_filter: float = 0.5  # ATR 最低值（过滤极低波动时段）

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used = ["rsi14", "atr14"]
        hold = SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.0,
            reason="no_setup",
            used_indicators=used,
        )

        # ── 1. 获取 M5 指标 ──
        rsi_data = context.indicators.get("rsi14")
        atr_data = context.indicators.get("atr14")
        if not isinstance(rsi_data, dict) or not isinstance(atr_data, dict):
            return hold

        rsi = rsi_data.get("rsi")
        atr = atr_data.get("atr")
        if rsi is None or atr is None:
            return hold

        # ATR 过滤：极低波动时段不参与
        min_atr = get_tf_param(self, "min_atr_filter", context.timeframe, self._min_atr_filter)
        if atr < min_atr:
            return hold

        # ── 2. 获取 H1 HTF 指标 ──
        h1 = context.htf_indicators.get("H1", {})
        if not h1:
            return hold

        # H1 supertrend 方向
        h1_st = h1.get("supertrend14", {})
        h1_direction_raw = h1_st.get("direction")
        if h1_direction_raw is None:
            return hold

        try:
            h1_direction = int(h1_direction_raw)
        except (TypeError, ValueError):
            return hold

        # direction: 1 = bullish (价格在 supertrend 上方), -1 = bearish
        if h1_direction not in (1, -1):
            return hold

        # H1 ADX 趋势强度
        h1_adx_data = h1.get("adx14", {})
        h1_adx = h1_adx_data.get("adx")
        adx_min = get_tf_param(self, "adx_min", context.timeframe, self._adx_min)
        if h1_adx is None or h1_adx < adx_min:
            return hold

        # H1 EMA50 顺势确认
        h1_ema = h1.get("ema50", {}).get("ema")

        # 获取 M5 收盘价
        close_val = context.indicators.get("rsi14", {}).get("close")
        if close_val is None:
            # fallback：从 atr 数据中获取
            close_val = context.metadata.get("close")

        # ── 3. 参数查表 ──
        pb_buy_low = get_tf_param(
            self, "rsi_pullback_buy_low", context.timeframe, self._rsi_pullback_buy_low
        )
        pb_buy_high = get_tf_param(
            self, "rsi_pullback_buy_high", context.timeframe, self._rsi_pullback_buy_high
        )
        pb_sell_low = get_tf_param(
            self, "rsi_pullback_sell_low", context.timeframe, self._rsi_pullback_sell_low
        )
        pb_sell_high = get_tf_param(
            self, "rsi_pullback_sell_high", context.timeframe, self._rsi_pullback_sell_high
        )

        # ── 4. 入场逻辑 ──
        action = "hold"
        confidence = 0.0
        reason = "no_setup"

        if h1_direction == 1:
            # H1 趋势向上 → 寻找 M5 做多回调入场
            # EMA 顺势确认：price > H1 EMA50（在均线上方做多）
            if h1_ema is not None and close_val is not None and close_val < h1_ema:
                return hold  # 价格跌破 H1 均线，不做多

            # RSI 回调到中性区间 = 回调到位
            if pb_buy_low <= rsi <= pb_buy_high:
                action = "buy"
                # 置信度：H1 ADX 越强 + RSI 越接近中间值 → 越好
                adx_factor = min(h1_adx / 35.0, 1.0)  # ADX 35+ 归一化为 1.0
                rsi_center = (pb_buy_low + pb_buy_high) / 2
                rsi_factor = 1.0 - abs(rsi - rsi_center) / (pb_buy_high - pb_buy_low)
                confidence = 0.55 + 0.35 * adx_factor * rsi_factor
                reason = (
                    f"h1_up:adx={h1_adx:.1f},m5_rsi={rsi:.1f},"
                    f"pullback_zone=[{pb_buy_low:.0f},{pb_buy_high:.0f}]"
                )

        elif h1_direction == -1:
            # H1 趋势向下 → 寻找 M5 做空回调入场
            if h1_ema is not None and close_val is not None and close_val > h1_ema:
                return hold  # 价格在 H1 均线上方，不做空

            if pb_sell_low <= rsi <= pb_sell_high:
                action = "sell"
                adx_factor = min(h1_adx / 35.0, 1.0)
                rsi_center = (pb_sell_low + pb_sell_high) / 2
                rsi_factor = 1.0 - abs(rsi - rsi_center) / (pb_sell_high - pb_sell_low)
                confidence = 0.55 + 0.35 * adx_factor * rsi_factor
                reason = (
                    f"h1_down:adx={h1_adx:.1f},m5_rsi={rsi:.1f},"
                    f"pullback_zone=[{pb_sell_low:.0f},{pb_sell_high:.0f}]"
                )

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=confidence,
            reason=reason,
            used_indicators=used,
            metadata={
                "h1_direction": h1_direction,
                "h1_adx": h1_adx,
                "h1_ema50": h1_ema,
                "m5_rsi": rsi,
                "m5_atr": atr,
                "m5_close": close_val,
            },
        )
