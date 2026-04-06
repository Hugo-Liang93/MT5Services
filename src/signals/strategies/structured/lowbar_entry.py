"""StructuredLowbarEntry — 数据挖掘策略：bar 极端收盘位反转入场。

来源：Research 模块 Rule Mining（10 个月 H1 数据, 4683 bar）
  Rule #3: ADX <= 36 AND RSI <= 72.5 AND close_position <= 0.19 → BUY
  训练集 61.2%/n=416, 测试集 59.6%/n=139

逻辑：
  Why:   ADX 未处于极端趋势 → 确认非单边行情，反转可行
  When:  close_position 处于 bar 极端位（底部买/顶部卖）+ RSI 未过热
  Where: 接近结构水平 → 加分
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import StructuredStrategyBase, _near_structure_level, _structure_bias_bonus


class StructuredLowbarEntry(StructuredStrategyBase):
    """Bar 极端收盘位反转入场。

    买入：bar 收在底部（close_position <= 0.19）+ ADX 非极端 + RSI 未过热
    卖出：bar 收在顶部（close_position >= 0.81）+ ADX 非极端 + RSI 未过冷
    """

    name = "structured_lowbar_entry"
    category = "reversion"
    required_indicators = ("adx14", "rsi14", "bar_stats20", "atr14")
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 0.15,
        RegimeType.RANGING: 1.00,
        RegimeType.BREAKOUT: 0.20,
        RegimeType.UNCERTAIN: 0.70,
    }

    _base_confidence: float = 0.48

    # Why 参数
    _adx_max: float = 36.0

    # When 参数
    _close_pos_buy: float = 0.19  # bar 收在底部 19% 以内
    _close_pos_sell: float = 0.81  # bar 收在顶部 19% 以内
    _rsi_max_buy: float = 72.5  # 买入时 RSI 不超过此值
    _rsi_min_sell: float = 27.5  # 卖出时 RSI 不低于此值

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        """ADX 非极端趋势 + close_position 确定方向。"""
        ad = self._adx_full(ctx)
        adx = ad["adx"]
        if adx is None:
            return False, None, 0, "no_adx"

        adx_max = get_tf_param(self, "adx_max", ctx.timeframe, self._adx_max)
        if adx > adx_max:
            return False, None, 0, f"trending:{adx:.0f}>{adx_max:.0f}"

        # close_position 决定方向
        bs = ctx.indicators.get("bar_stats20", {})
        cp = bs.get("close_position")
        if cp is None:
            return False, None, 0, "no_close_pos"
        cp_f = float(cp)

        buy_thr = get_tf_param(self, "close_pos_buy", ctx.timeframe, self._close_pos_buy)
        sell_thr = get_tf_param(
            self, "close_pos_sell", ctx.timeframe, self._close_pos_sell
        )

        if cp_f <= buy_thr:
            score = max(0.0, (adx_max - adx) / adx_max)
            return True, "buy", score, f"cp={cp_f:.2f},adx={adx:.0f}"
        elif cp_f >= sell_thr:
            score = max(0.0, (adx_max - adx) / adx_max)
            return True, "sell", score, f"cp={cp_f:.2f},adx={adx:.0f}"

        return False, None, 0, f"cp_mid:{cp_f:.2f}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """RSI 不处于极端反向区（避免逆势）。"""
        rsi, rsi_d3 = self._rsi(ctx)
        if rsi is None:
            return False, 0, "no_rsi"

        tf = ctx.timeframe
        if direction == "buy":
            rsi_max = get_tf_param(self, "rsi_max_buy", tf, self._rsi_max_buy)
            if rsi > rsi_max:
                return False, 0, f"rsi_hot:{rsi:.0f}>{rsi_max:.0f}"
            rsi_score = max(0.0, (50.0 - rsi) / 50.0)
            if rsi_d3 is not None and rsi_d3 > 0:
                rsi_score = min(rsi_score + 0.3, 1.0)
            return True, rsi_score, f"rsi={rsi:.0f}"
        else:
            rsi_min = get_tf_param(self, "rsi_min_sell", tf, self._rsi_min_sell)
            if rsi < rsi_min:
                return False, 0, f"rsi_cold:{rsi:.0f}<{rsi_min:.0f}"
            rsi_score = max(0.0, (rsi - 50.0) / 50.0)
            if rsi_d3 is not None and rsi_d3 < 0:
                rsi_score = min(rsi_score + 0.3, 1.0)
            return True, rsi_score, f"rsi={rsi:.0f}"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        close = self._close(ctx)
        atr = self._atr(ctx)
        ms = self._ms(ctx)

        score = 0.0
        info = ""

        if close is not None and atr is not None and atr > 0:
            if _near_structure_level(close, ms, atr, max_atr=1.0):
                score += 0.4
                info = "near_level"

        sb_score, sb_info = _structure_bias_bonus(ms, direction)
        score = min(score + sb_score * 0.6, 1.0)
        if sb_info:
            info = f"{info},{sb_info}" if info else sb_info

        return score, info

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        vr = self._volume_ratio(ctx)
        return 1.0 if vr is not None and vr > 1.5 else 0.0

    def _entry_spec(self, ctx: SignalContext, direction: str) -> Dict[str, Any]:
        return {"entry_type": "market", "entry_price": None, "entry_zone_atr": 0.3}

    def _exit_spec(self, ctx: SignalContext, direction: str) -> Dict[str, Any]:
        # 极端 bar 反转：紧 trail 快锁利，SL/TP 用全局默认
        return {"aggression": 0.15, "sl_atr": None, "tp_atr": None}
