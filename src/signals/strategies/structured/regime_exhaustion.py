"""StructuredRegimeExhaustion — 趋势耗竭反转（挖掘驱动）。

核心逻辑：ADX 极端值（>55）标志趋势过度延伸，结合 DI 方向判断哪一方耗竭，
ADX 转向下行确认耗竭已开始，StochRSI 极端值确认反转时机。

挖掘来源：2026-04-17 H1 regime transition 规则挖掘，bars_in_regime 为根节点，
运行时用 ADX 极端 + DI 方向 + ADX 转向替代 bars_in_regime（研究特征）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import EntrySpec, ExitMode, ExitSpec, HtfPolicy, StructuredStrategyBase


class StructuredRegimeExhaustion(StructuredStrategyBase):
    """ADX 极端 + DI 方向耗竭 + StochRSI 时机确认 → 反转入场。"""

    name = "structured_regime_exhaustion"
    category = "reversion"
    htf_policy = HtfPolicy.NONE
    required_indicators = (
        "rsi14",
        "atr14",
        "adx14",
        "stoch_rsi14",
        "bar_stats20",
        "volume_ratio20",
    )
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.BREAKOUT: 0.60,
        RegimeType.RANGING: 0.00,
        RegimeType.UNCERTAIN: 0.20,
    }
    research_provenance_refs = ("2026-04-17-H1-regime-transition-rules",)

    # ── 可调参数 ──
    _adx_extreme: float = 55.0
    _stoch_overbought: float = 70.0
    _stoch_oversold: float = 30.0
    _rsi_sell_low: float = 45.0
    _rsi_sell_high: float = 65.0
    _rsi_buy_low: float = 35.0
    _rsi_buy_high: float = 55.0
    _sl_atr: float = 1.5
    _tp_atr: float = 2.0
    _time_bars: int = 20

    def _stoch_rsi(self, ctx: SignalContext) -> Tuple[Optional[float], Optional[float]]:
        """返回 (stoch_rsi_k, stoch_rsi_d)。"""
        data = ctx.indicators.get("stoch_rsi14", {})
        k = data.get("stoch_rsi_k")
        d = data.get("stoch_rsi_d")
        return (
            float(k) if k is not None else None,
            float(d) if d is not None else None,
        )

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        adx_data = self._adx_full(ctx)
        adx = adx_data["adx"]
        adx_d3 = adx_data["adx_d3"]
        plus_di = adx_data["plus_di"]
        minus_di = adx_data["minus_di"]

        if adx is None or plus_di is None or minus_di is None:
            return False, None, 0.0, "no_adx_data"

        # ADX 必须处于极端值
        adx_threshold = get_tf_param(
            self, "adx_extreme", ctx.timeframe, self._adx_extreme
        )
        if adx <= adx_threshold:
            return False, None, 0.0, f"adx_low:{adx:.0f}"

        # ADX 必须转向下行（趋势动量衰退）
        if adx_d3 is not None and adx_d3 > 0:
            return False, None, 0.0, f"adx_still_rising:d3={adx_d3:.1f}"

        # DI 方向决定哪一方耗竭
        if plus_di > minus_di:
            direction = "sell"  # 多头耗竭 → 做空
        else:
            direction = "buy"  # 空头耗竭 → 做多

        # 评分：ADX 越极端，耗竭信号越强（55→0.4, 70→1.0）
        score = self._linear_score(adx, low=adx_threshold, high=adx_threshold + 15.0)
        score = max(score, 0.4)  # 达到阈值即至少 0.4

        return True, direction, score, f"exhaustion:{direction}:adx={adx:.0f}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        stoch_k, _ = self._stoch_rsi(ctx)
        rsi, _ = self._rsi(ctx)

        if stoch_k is None:
            return False, 0.0, "no_stoch_rsi"

        tf = ctx.timeframe

        # StochRSI 确认：卖出时仍在超买区，买入时仍在超卖区
        overbought = get_tf_param(
            self, "stoch_overbought", tf, self._stoch_overbought
        )
        oversold = get_tf_param(self, "stoch_oversold", tf, self._stoch_oversold)

        if direction == "sell" and stoch_k < overbought:
            return False, 0.0, f"stoch_not_overbought:{stoch_k:.0f}"
        if direction == "buy" and stoch_k > oversold:
            return False, 0.0, f"stoch_not_oversold:{stoch_k:.0f}"

        # RSI 范围确认：不在极端区本身（还有下跌/上涨空间）
        if rsi is not None:
            if direction == "sell":
                rsi_lo = get_tf_param(self, "rsi_sell_low", tf, self._rsi_sell_low)
                rsi_hi = get_tf_param(self, "rsi_sell_high", tf, self._rsi_sell_high)
                if rsi < rsi_lo or rsi > rsi_hi:
                    return False, 0.0, f"rsi_out_of_range:{rsi:.0f}"
            else:
                rsi_lo = get_tf_param(self, "rsi_buy_low", tf, self._rsi_buy_low)
                rsi_hi = get_tf_param(self, "rsi_buy_high", tf, self._rsi_buy_high)
                if rsi < rsi_lo or rsi > rsi_hi:
                    return False, 0.0, f"rsi_out_of_range:{rsi:.0f}"

        # 评分：StochRSI 越极端越好
        if direction == "sell":
            score = self._linear_score(stoch_k, low=overbought, high=95.0)
        else:
            score = self._linear_score(oversold - stoch_k, low=0.0, high=oversold)
        score = max(score, 0.3)  # 通过门控即至少 0.3

        return True, score, f"timing:stoch={stoch_k:.0f}"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        ms = self._ms(ctx)
        compression = ms.get("compression_state", "none")
        breakout = ms.get("breakout_state", "none")

        if str(compression) != "none":
            return 0.8, f"compression={compression}"
        if str(breakout) != "none":
            return 0.8, f"breakout={breakout}"

        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        return self._linear_score(self._volume_ratio(ctx), low=1.2, high=1.5)

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        # 反转策略：市价入场
        return EntrySpec()

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        """BARRIER 模式：固定 SL/TP/Time，与挖掘 barrier_stats 一致。"""
        sl = get_tf_param(self, "sl_atr", ctx.timeframe, self._sl_atr)
        tp = get_tf_param(self, "tp_atr", ctx.timeframe, self._tp_atr)
        tb = int(get_tf_param(self, "time_bars", ctx.timeframe, self._time_bars))
        return ExitSpec(sl_atr=sl, tp_atr=tp, mode=ExitMode.BARRIER, time_bars=tb)
