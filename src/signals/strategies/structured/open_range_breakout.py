"""StructuredOpenRangeBreakout — 亚盘区间 → 欧美盘突破入场。

核心逻辑（London/New York Breakout of Asia Range）：

1. Why（方向确认）：亚盘区间形成 + 欧盘/美盘突破
   - 亚盘（UTC 0:00-8:00）建立当日高低区间
   - 欧盘或美盘时段，价格突破亚盘高点 → buy
   - 欧盘或美盘时段，价格跌破亚盘低点 → sell
   - 亚盘区间需在合理范围（0.5~2.0 ATR，过窄=无效，过宽=风险大）

2. When（时机精度）：ADX 趋势确认 + 突破幅度
   - ADX > 20 确认有趋势动能
   - 突破幅度 ≥ 0.15 ATR（过滤假突破）
   - Donchian 通道方向一致加分

3. Where（结构位加分）：市场结构偏向确认

4. Entry（入场方式）：STOP 单挂在亚盘区间边界外
   - Buy STOP：亚盘高点 + 0.15 ATR
   - Sell STOP：亚盘低点 - 0.15 ATR
   - 未触发则挂单在下次 bar 过期

5. Exit：Chandelier 模式，aggression=0.65（偏保守，时段突破利润有限）

与旧 session_breakout 的关键差异：
- 旧版用 market 单直接入场，假突破率高
- ORB 用 STOP 单等待确认 + 更严格的区间过滤 + ADX 趋势确认

参考：
- github.com/yulz008/GOLD_ORB — Open Range Breakout EA
- London Breakout Strategy（亚盘区间 → 欧盘突破，历史胜率 65-70%）
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


class StructuredOpenRangeBreakout(StructuredStrategyBase):
    """亚盘区间 → 欧美盘 STOP 单突破入场。"""

    name = "structured_open_range_breakout"
    category = "breakout"
    htf_policy = HtfPolicy.SOFT_BONUS
    required_indicators = ("atr14", "adx14", "donchian20", "volume_ratio20")
    regime_affinity = {
        RegimeType.TRENDING: 1.00,  # 趋势中的亚盘突破最可靠
        RegimeType.BREAKOUT: 0.40,  # 回测显示 breakout regime 假突破多，降权
        RegimeType.RANGING: 0.10,
        RegimeType.UNCERTAIN: 0.15,
    }

    # ── Why 参数 ──
    _asia_range_min_atr: float = 0.5  # 亚盘区间最小宽度（ATR 倍数）
    _asia_range_max_atr: float = 2.0  # 亚盘区间最大宽度
    _penetration_min_atr: float = 0.15  # 突破幅度最小值（ATR 倍数）

    # ── When 参数 ──
    _adx_min: float = 20.0  # 趋势强度下限

    # ── Entry 参数 ──
    _entry_offset_atr: float = 0.15  # STOP 单偏移（亚盘边界外多少 ATR）
    _entry_zone_atr: float = 0.25  # 入场区间宽度

    # ── Exit 参数 ──
    _aggression: float = 0.65  # 偏保守（时段突破利润窗口有限）

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        """方向确认：欧/美盘时段 + 亚盘区间突破。"""
        ms = self._ms(ctx)
        session = ms.get("current_session", "")
        if session not in ("london", "new_york"):
            return False, None, 0, f"session:{session}"

        asia_high = ms.get("asia_range_high")
        asia_low = ms.get("asia_range_low")
        if asia_high is None or asia_low is None:
            return False, None, 0, "no_asia_range"

        close = self._close(ctx)
        atr = self._atr(ctx)
        if close is None or atr is None or atr <= 0:
            return False, None, 0, "no_data"

        tf = ctx.timeframe
        ah, al = float(asia_high), float(asia_low)
        asia_range = ah - al
        if asia_range <= 0:
            return False, None, 0, "asia_range_zero"

        # 亚盘区间宽度过滤
        range_min = get_tf_param(
            self, "asia_range_min_atr", tf, self._asia_range_min_atr
        )
        range_max = get_tf_param(
            self, "asia_range_max_atr", tf, self._asia_range_max_atr
        )
        range_ratio = asia_range / atr
        if range_ratio < range_min:
            return False, None, 0, f"range_narrow:{range_ratio:.2f}<{range_min:.1f}"
        if range_ratio > range_max:
            return False, None, 0, f"range_wide:{range_ratio:.2f}>{range_max:.1f}"

        # 突破方向判定
        pen_min = get_tf_param(
            self, "penetration_min_atr", tf, self._penetration_min_atr
        )
        pen_threshold = atr * pen_min

        buy_penetration = close - ah
        sell_penetration = al - close

        if buy_penetration >= pen_threshold:
            direction = "buy"
            pen = buy_penetration
        elif sell_penetration >= pen_threshold:
            direction = "sell"
            pen = sell_penetration
        else:
            return False, None, 0, "no_breakout"

        # 评分：突破幅度越大越好（0.15 ATR→0.0, 0.5 ATR→1.0）
        score = self._linear_score(pen / atr, low=pen_min, high=0.50)

        # 伦敦时段加分（欧盘突破统计上更可靠）
        if session == "london":
            score = min(1.0, score + 0.10)

        reason = f"orb_{direction}:{session},pen={pen:.1f},range={asia_range:.1f}"
        return True, direction, score, reason

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """时机确认：ADX 趋势强度 + Donchian 方向一致性。"""
        tf = ctx.timeframe

        adx_data = self._adx_full(ctx)
        adx = adx_data.get("adx")
        if adx is None:
            return False, 0, "no_adx"

        adx_min = get_tf_param(self, "adx_min", tf, self._adx_min)
        if adx < adx_min:
            return False, 0, f"adx_low:{adx:.0f}<{adx_min:.0f}"

        # ADX 基础评分
        score = self._linear_score(adx, low=adx_min, high=35.0)

        # Donchian 通道方向确认加分
        donchian = ctx.indicators.get("donchian20", {})
        don_high = donchian.get("high")
        don_low = donchian.get("low")
        close = self._close(ctx)
        if don_high is not None and don_low is not None and close is not None:
            don_mid = (float(don_high) + float(don_low)) / 2
            if direction == "buy" and close > don_mid:
                score = min(1.0, score + 0.15)
            elif direction == "sell" and close < don_mid:
                score = min(1.0, score + 0.15)

        # DI 方向确认
        plus_di = adx_data.get("plus_di")
        minus_di = adx_data.get("minus_di")
        if plus_di is not None and minus_di is not None:
            if direction == "buy" and plus_di > minus_di:
                score = min(1.0, score + 0.10)
            elif direction == "sell" and minus_di > plus_di:
                score = min(1.0, score + 0.10)

        return True, score, f"adx={adx:.0f}"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        return _structure_bias_bonus(self._ms(ctx), direction)

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        """量能确认：突破时放量为佳（volume_ratio > 1.0 加分）。"""
        vr = self._volume_ratio(ctx)
        if vr is None:
            return 0.0
        # 突破放量：volume_ratio 1.0→0.0, 1.5→0.5, 2.0→1.0
        return self._linear_score(vr, low=1.0, high=2.0)

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        """STOP 单入场：挂在亚盘区间边界外，等待突破确认。"""
        zone = get_tf_param(self, "entry_zone_atr", ctx.timeframe, self._entry_zone_atr)
        return EntrySpec(
            entry_type=EntryType.STOP,
            entry_zone_atr=zone,
        )

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        aggr = get_tf_param(self, "aggression", ctx.timeframe, self._aggression)
        return ExitSpec(aggression=aggr)
