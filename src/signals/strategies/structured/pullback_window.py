"""StructuredPullbackWindow — 趋势确认 + 回调等待 + 波动率扩张突破入场。

核心逻辑（改编自 Sunrise Ogle 4-Phase State Machine，适配 H1/M30）：

1. Why（方向确认）：EMA 趋势确认 + ADX 趋势强度
   - EMA9 > EMA21 > EMA55 且 ADX > 阈值 → buy
   - EMA9 < EMA21 < EMA55 且 ADX > 阈值 → sell
   - 多层 EMA 排列比单个交叉更稳健

2. When（回调时机）：RSI 回调区间 + Keltner/BB 回归
   - Buy：RSI 从超买回落到 40-55（回调但未过度）
   - Sell：RSI 从超卖反弹到 45-60（回调但未过度）
   - BB position 确认价格已回到通道中轨附近

3. Where（结构位加分）：价格结构偏向确认

4. Entry（入场方式）：STOP 单挂在 ATR channel 突破位
   - Buy：当前价 + 0.3 ATR（等待向上突破确认）
   - Sell：当前价 - 0.3 ATR（等待向下突破确认）
   - 未突破则挂单过期，避免假信号入场

5. Exit：Chandelier 模式，中等 aggression（0.70）

与现有策略的差异：
- trend_continuation 在 RSI 回调区间直接 market 入场
- pullback_window 等待回调后再用 STOP 单确认突破方向，过滤假回调

参考：github.com/ilahuerta-IA/backtrader-pullback-window-xauusd
原策略 5 年回测：Sharpe 0.89 | PF 1.64 | WR 55.4% | DD 5.81%
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import (
    EntrySpec,
    EntryType,
    ExitSpec,
    HtfPolicy,
    StructuredStrategyBase,
    _structure_bias_bonus,
)


class StructuredPullbackWindow(StructuredStrategyBase):
    """趋势 EMA 排列 + RSI 回调 + STOP 突破入场。"""

    name = "structured_pullback_window"
    category = "trend"
    htf_policy = HtfPolicy.SOFT_BONUS
    required_indicators = (
        "ema9",
        "ema21",
        "ema55",
        "rsi14",
        "atr14",
        "adx14",
        "boll20",
    )
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.BREAKOUT: 0.50,
        RegimeType.RANGING: 0.05,
        RegimeType.UNCERTAIN: 0.10,
    }

    # ── Why 参数 ──
    _adx_min: float = 25.0  # 趋势强度下限（过滤弱趋势）
    _adx_strong: float = 40.0  # 满分 ADX

    # ── When 参数 ──
    _rsi_pullback_buy_low: float = 40.0  # buy 回调 RSI 下限（收紧避免超卖区抄底）
    _rsi_pullback_buy_high: float = 52.0  # buy 回调 RSI 上限（确保真回调）
    _rsi_pullback_sell_low: float = 48.0  # sell 回调 RSI 下限（确保真回调）
    _rsi_pullback_sell_high: float = 60.0  # sell 回调 RSI 上限（收紧避免超买区追空）
    _bb_pullback_buy_max: float = 0.50  # buy 时 BB position 上限（必须回到中轨以下）
    _bb_pullback_sell_min: float = 0.50  # sell 时 BB position 下限（必须回到中轨以上）

    # ── Entry 参数 ──
    _entry_offset_atr: float = 0.3  # STOP 单偏移（ATR 倍数）
    _entry_zone_atr: float = 0.3  # 入场区间宽度

    # ── Exit 参数 ──
    _aggression: float = 0.70  # 中等 trail，平衡趋势跟随和保护

    def _ema_triple(
        self, ctx: SignalContext
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """读取 EMA9/21/55 三重均线。"""
        ema9 = ctx.indicators.get("ema9", {}).get("ema")
        ema21 = ctx.indicators.get("ema21", {}).get("ema")
        ema55 = ctx.indicators.get("ema55", {}).get("ema")
        return (
            float(ema9) if ema9 is not None else None,
            float(ema21) if ema21 is not None else None,
            float(ema55) if ema55 is not None else None,
        )

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        """方向确认：三重 EMA 排列 + ADX 趋势强度。"""
        ema9, ema21, ema55 = self._ema_triple(ctx)
        if ema9 is None or ema21 is None or ema55 is None:
            return False, None, 0, "no_ema"

        adx_data = self._adx_full(ctx)
        adx = adx_data.get("adx")
        plus_di = adx_data.get("plus_di")
        minus_di = adx_data.get("minus_di")
        if adx is None:
            return False, None, 0, "no_adx"

        adx_min = get_tf_param(self, "adx_min", ctx.timeframe, self._adx_min)
        if adx < adx_min:
            return False, None, 0, f"adx_low:{adx:.0f}<{adx_min:.0f}"

        # EMA 排列判断
        bullish_order = ema9 > ema21 > ema55
        bearish_order = ema9 < ema21 < ema55

        if not bullish_order and not bearish_order:
            return False, None, 0, "ema_no_order"

        # DI 交叉确认方向
        if bullish_order:
            if plus_di is not None and minus_di is not None and plus_di <= minus_di:
                return False, None, 0, "di_conflict_buy"
            direction = "buy"
        else:
            if plus_di is not None and minus_di is not None and minus_di <= plus_di:
                return False, None, 0, "di_conflict_sell"
            direction = "sell"

        # ADX 评分：20→0.0, 35→1.0
        adx_strong = get_tf_param(self, "adx_strong", ctx.timeframe, self._adx_strong)
        score = self._linear_score(adx, low=adx_min, high=adx_strong)

        # EMA 间距加分：间距越大趋势越明确
        ema_spread = abs(ema9 - ema55)
        atr = self._atr(ctx)
        if atr and atr > 0:
            spread_ratio = ema_spread / atr
            score = min(1.0, score + 0.1 * min(spread_ratio / 2.0, 1.0))

        return True, direction, score, f"ema_order:{direction},adx={adx:.0f}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """入场时机：RSI 回调区间 + BB position 回归确认。"""
        tf = ctx.timeframe
        rsi, rsi_d3 = self._rsi(ctx)
        if rsi is None:
            return False, 0, "no_rsi"

        bb_pos = self._bb_position(ctx)

        if direction == "buy":
            lo = get_tf_param(
                self, "rsi_pullback_buy_low", tf, self._rsi_pullback_buy_low
            )
            hi = get_tf_param(
                self, "rsi_pullback_buy_high", tf, self._rsi_pullback_buy_high
            )
            if not (lo <= rsi <= hi):
                return False, 0, f"rsi={rsi:.0f}∉[{lo:.0f},{hi:.0f}]"
            # RSI 应该在回升中（d3 > 0 或至少不再下跌）
            if rsi_d3 is not None and rsi_d3 < -5.0:
                return False, 0, f"rsi_still_falling:d3={rsi_d3:.1f}"
            # BB position 确认已回调到中轨以下
            bb_max = get_tf_param(
                self, "bb_pullback_buy_max", tf, self._bb_pullback_buy_max
            )
            if bb_pos is not None and bb_pos > bb_max:
                return False, 0, f"bb_high:{bb_pos:.2f}>{bb_max:.2f}"
        else:
            lo = get_tf_param(
                self, "rsi_pullback_sell_low", tf, self._rsi_pullback_sell_low
            )
            hi = get_tf_param(
                self, "rsi_pullback_sell_high", tf, self._rsi_pullback_sell_high
            )
            if not (lo <= rsi <= hi):
                return False, 0, f"rsi={rsi:.0f}∉[{lo:.0f},{hi:.0f}]"
            if rsi_d3 is not None and rsi_d3 > 5.0:
                return False, 0, f"rsi_still_rising:d3={rsi_d3:.1f}"
            bb_min = get_tf_param(
                self, "bb_pullback_sell_min", tf, self._bb_pullback_sell_min
            )
            if bb_pos is not None and bb_pos < bb_min:
                return False, 0, f"bb_low:{bb_pos:.2f}<{bb_min:.2f}"

        # 评分：RSI 越接近区间中心越好
        center = (lo + hi) / 2
        rsi_score = max(0.0, 1.0 - abs(rsi - center) / 15.0)
        # BB 回调加分
        bb_score = 0.0
        if bb_pos is not None:
            if direction == "buy":
                bb_score = max(0.0, 1.0 - bb_pos / 0.6)  # bb_pos 越低越好
            else:
                bb_score = max(0.0, (bb_pos - 0.4) / 0.6)  # bb_pos 越高越好
        score = 0.6 * rsi_score + 0.4 * bb_score

        reason = f"rsi={rsi:.0f}"
        if bb_pos is not None:
            reason += f",bb={bb_pos:.2f}"
        return True, score, reason

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        return _structure_bias_bonus(self._ms(ctx), direction)

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        """量能确认：回调时缩量为佳（量比 < 1.0 加分）。"""
        vr = self._volume_ratio(ctx)
        if vr is None:
            return 0.0
        # 回调缩量：volume_ratio 0.5→1.0 分, 1.0→0.5 分, 1.5→0.0 分
        return max(0.0, min(1.0, 1.5 - vr))

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        """STOP 单入场：等待回调后的突破确认。"""
        zone = get_tf_param(self, "entry_zone_atr", ctx.timeframe, self._entry_zone_atr)
        return EntrySpec(
            entry_type=EntryType.STOP,
            entry_zone_atr=zone,
        )

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        aggr = get_tf_param(self, "aggression", ctx.timeframe, self._aggression)
        return ExitSpec(aggression=aggr)
