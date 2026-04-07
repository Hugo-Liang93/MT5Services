"""StructuredRangeReversion — 结构位回归。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import StructuredStrategyBase, _structure_bias_bonus, _near_structure_level


class StructuredRangeReversion(StructuredStrategyBase):
    """RSI/BB 极端 + 低 ADX + 结构水平接近加分。"""

    name = "structured_range_reversion"
    category = "reversion"
    required_indicators = ("rsi14", "atr14", "adx14", "boll20")
    preferred_scopes = ("confirmed", "intrabar")
    regime_affinity = {
        RegimeType.TRENDING: 0.05,
        RegimeType.RANGING: 1.00,
        RegimeType.BREAKOUT: 0.10,
        RegimeType.UNCERTAIN: 0.50,
    }

    _adx_max: float = 22.0
    _rsi_oversold: float = 28.0
    _rsi_overbought: float = 72.0
    _bb_low: float = 0.15
    _bb_high: float = 0.85
    _base_confidence: float = 0.48

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        tf = ctx.timeframe
        ad = self._adx_full(ctx)
        adx = ad["adx"]
        adx_max = get_tf_param(self, "adx_max", tf, self._adx_max)
        if adx is not None and adx > adx_max:
            return False, None, 0, f"trending:{adx:.0f}"

        # HTF 不应有强同向趋势
        htf = self._htf_data(ctx)
        htf_adx = htf.get("adx14", {}).get("adx")
        htf_dir = htf.get("supertrend14", {}).get("direction")

        rsi, rsi_d3 = self._rsi(ctx)
        bb_pos = self._bb_position(ctx)
        if rsi is None:
            return False, None, 0, "no_rsi"

        rsi_os = get_tf_param(self, "rsi_oversold", tf, self._rsi_oversold)
        rsi_ob = get_tf_param(self, "rsi_overbought", tf, self._rsi_overbought)
        bb_lo = get_tf_param(self, "bb_low", tf, self._bb_low)
        bb_hi = get_tf_param(self, "bb_high", tf, self._bb_high)

        if rsi <= rsi_os and (bb_pos is None or bb_pos <= bb_lo):
            if rsi_d3 is not None and rsi_d3 < 0:
                return False, None, 0, "still_falling"
            # HTF 冲突检查
            if htf_adx is not None and float(htf_adx) > 25 and htf_dir is not None:
                if int(htf_dir) == -1:
                    return False, None, 0, "htf_bearish"
            adx_score = 0.7 if adx is not None and adx < 18 else 0.4
            return True, "buy", adx_score, f"oversold:rsi={rsi:.0f},bb={bb_pos}"

        if rsi >= rsi_ob and (bb_pos is None or bb_pos >= bb_hi):
            if rsi_d3 is not None and rsi_d3 > 0:
                return False, None, 0, "still_rising"
            if htf_adx is not None and float(htf_adx) > 25 and htf_dir is not None:
                if int(htf_dir) == 1:
                    return False, None, 0, "htf_bullish"
            adx_score = 0.7 if adx is not None and adx < 18 else 0.4
            return True, "sell", adx_score, f"overbought:rsi={rsi:.0f},bb={bb_pos}"

        return False, None, 0, f"rsi_mid:{rsi:.0f}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        # Stoch RSI 交叉加分
        stoch = ctx.indicators.get("stoch_rsi14", {})
        k, d = stoch.get("k"), stoch.get("d")
        score = 0.0
        if k is not None and d is not None:
            sk, sd = float(k), float(d)
            if direction == "buy" and sk > sd and sk < 25:
                score = 0.8
            elif direction == "sell" and sk < sd and sk > 75:
                score = 0.8
        return True, score, "timing_ok"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        close = self._close(ctx)
        atr = self._atr(ctx)
        ms = self._ms(ctx)
        if close is not None and atr is not None and atr > 0:
            if _near_structure_level(close, ms, atr, max_atr=0.8):
                return 1.0, "near_level"
        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        mfi = self._mfi(ctx)
        if mfi is None:
            return 0.0
        # MFI 超卖/超买的连续化评分（买方 MFI 越低越好，卖方 MFI 越高越好）
        if direction == "buy":
            return self._linear_score(40.0 - mfi, low=0.0, high=15.0)
        return self._linear_score(mfi - 60.0, low=0.0, high=15.0)

    def _entry_spec(self, ctx: SignalContext, direction: str) -> Dict[str, Any]:
        return {"entry_type": "limit", "entry_price": None, "entry_zone_atr": 0.4}

    _aggression: float = 0.15

    def _exit_spec(self, ctx: SignalContext, direction: str) -> Dict[str, Any]:
        # 均值回归：紧 trail + 快锁利，反转幅度有限
        aggr = get_tf_param(self, "aggression", ctx.timeframe, self._aggression)
        return {"aggression": aggr, "sl_atr": 1.0, "tp_atr": 1.8}
