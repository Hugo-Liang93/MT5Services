"""StructuredSqueezeBreakoutBuy — 低 ADX + squeeze 蓄势做多（H1，intrabar 友好）。

挖掘来源（2026-04-15 mining_2026-04-15_live_6mo.json）：
    Rule I-2 [H1 BUY]:
      di_spread14.di_spread > -0.44
      AND adx14.adx <= 10.84
      AND squeeze20.squeeze_intensity <= 0.21

    train=72.0%/n=164  test=60.4%/n=53  (6 个月 XAUUSD)

语义解读：
    - DI spread 未深度为负（买方并非被完全压制）
    - ADX 极低（<11，方向感缺失，纯震荡）
    - Squeeze intensity 适中（KC/BB 收窄但未夸张 → 波动率已压缩）
    → 盘整蓄势末期，倾向往上突破

纯连续性指标组合，子 TF 合成快照可算，允许 intrabar 触发以捕捉突破早期。

样本量较小（train 164, test 53），标记 deployment_stage=paper_only 并限定 H1 单 TF。
"""

from __future__ import annotations

from typing import Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import EntrySpec, ExitSpec, HtfPolicy, StructuredStrategyBase


class StructuredSqueezeBreakoutBuy(StructuredStrategyBase):
    """低 ADX + squeeze → 盘整蓄势 → 偏多（H1）。"""

    name = "structured_squeeze_breakout_buy"
    category = "breakout"
    htf_policy = HtfPolicy.NONE
    required_indicators = ("di_spread14", "adx14", "squeeze20", "atr14")
    htf_required_indicators = {}
    preferred_scopes = ("confirmed", "intrabar")
    research_provenance_refs = (
        "rule_mining.i2_h1_buy_2026-04-15",
    )
    regime_affinity = {
        RegimeType.TRENDING: 0.15,
        RegimeType.RANGING: 1.00,  # 这是 ranging 末期的专用模式
        RegimeType.BREAKOUT: 0.40,
        RegimeType.UNCERTAIN: 0.60,
    }

    _di_spread_min: float = -0.44
    _adx_max: float = 10.84
    _squeeze_intensity_max: float = 0.21

    _base_confidence: float = 0.50
    _aggression: float = 0.55  # 盘整突破：适度紧 trail 防止假突破

    def _why(
        self, ctx: SignalContext
    ) -> Tuple[bool, Optional[str], float, str]:
        tf = ctx.timeframe
        di_spread = ctx.indicators.get("di_spread14", {}).get("di_spread")
        ad = self._adx_full(ctx)
        adx = ad.get("adx")
        squeeze_intensity = ctx.indicators.get("squeeze20", {}).get(
            "squeeze_intensity"
        )

        if di_spread is None or adx is None or squeeze_intensity is None:
            return False, None, 0.0, "no_inputs"

        di_f = float(di_spread)
        adx_f = float(adx)
        si_f = float(squeeze_intensity)

        di_min = get_tf_param(self, "di_spread_min", tf, self._di_spread_min)
        adx_max = get_tf_param(self, "adx_max", tf, self._adx_max)
        si_max = get_tf_param(
            self, "squeeze_intensity_max", tf, self._squeeze_intensity_max
        )

        if di_f <= di_min:
            return False, None, 0.0, f"di_bearish:{di_f:.2f}"
        if adx_f > adx_max:
            return False, None, 0.0, f"adx_high:{adx_f:.2f}"
        if si_f > si_max:
            return False, None, 0.0, f"squeeze_loose:{si_f:.2f}"

        # Score：di_spread 越正分越高 + ADX 越低分越高 + squeeze 越近 0 越强
        di_score = min(1.0, max(0.0, (di_f + 1.0) / 2.0))
        adx_score = 1.0 - min(1.0, max(0.0, adx_f / adx_max))
        sq_score = 1.0 - min(1.0, max(0.0, si_f / (si_max + 0.01)))
        score = 0.4 * di_score + 0.3 * adx_score + 0.3 * sq_score
        reason = f"di+={di_f:+.2f},adx={adx_f:.1f},sq={si_f:.2f}"
        return True, "buy", score, reason

    def _when(
        self, ctx: SignalContext, direction: str
    ) -> Tuple[bool, float, str]:
        atr = self._atr(ctx)
        if atr is None or atr <= 0:
            return False, 0.0, "no_atr"
        return True, 1.0, "confirmed"

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        return EntrySpec()

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        aggr = get_tf_param(self, "aggression", ctx.timeframe, self._aggression)
        return ExitSpec(aggression=aggr)
