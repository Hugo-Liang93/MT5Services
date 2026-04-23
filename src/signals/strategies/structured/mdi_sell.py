"""StructuredMdiSell — 挖掘驱动的下行延续 sell 策略（2026-04-22 mining M30 Rule #1）。

规则（mined_rule, n=955, test hit 59.4%）：
    adx14.minus_di > 20.56 AND macd.hist > -13.32 AND cci20.cci > -241.14 → sell

解读：
    - minus_di > 20.56：下行方向强度足够（Why）
    - macd.hist > -13.32：MACD histogram 未到极端负（非超卖反弹临界，When）
    - cci20.cci > -241.14：CCI 未到极端超卖（非反弹临界，When）
    - THEN sell：趋势延续，不是反弹做空

来源：docs/codebase-review.md §F 2026-04-22 weekly mining M30 rule #1。
P0 Step 1：2026-04-23 启动 paper trading 验证（需门槛 PF > 1.2 且 trades > 50）。
"""

from __future__ import annotations

from typing import Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import (
    EntrySpec,
    EntryType,
    ExitSpec,
    HtfPolicy,
    StructuredStrategyBase,
)


class StructuredMdiSell(StructuredStrategyBase):
    """M30 下行延续：minus_di 强 + 未到超卖临界 → sell。"""

    name = "structured_mdi_sell"
    category = "trend"
    htf_policy = HtfPolicy.SOFT_GATE  # HTF 冲突（上行）时阻止
    preferred_scopes = ("confirmed",)
    required_indicators = ("adx14", "macd", "cci20", "atr14")
    htf_required_indicators = {"supertrend14": "H1", "adx14": "H1"}
    regime_affinity = {
        RegimeType.TRENDING: 1.00,  # 下行延续最适
        RegimeType.RANGING: 0.20,
        RegimeType.BREAKOUT: 0.60,
        RegimeType.UNCERTAIN: 0.30,
    }

    research_provenance_refs = (
        "mined_rule.m30.2026-04-22:macd>-13.32_cci>-241.14_minus_di>20.56",
    )

    # 挖掘出的阈值（可通过 signal.local.ini [strategy_params.<TF>] 覆盖）
    _minus_di_min: float = 20.56
    _macd_hist_min: float = -13.32
    _cci_min: float = -241.14
    _base_confidence: float = 0.48

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        tf = ctx.timeframe
        ad = self._adx_full(ctx)
        minus_di = ad.get("minus_di")
        if minus_di is None:
            return False, None, 0, "no_minus_di"

        min_di = get_tf_param(self, "minus_di_min", tf, self._minus_di_min)
        if minus_di <= min_di:
            return False, None, 0, f"minus_di_weak:{minus_di:.1f}"

        # HTF 冲突检查（SOFT_GATE）：HTF 上行强趋势时不做 sell
        htf = self._htf_data(ctx)
        htf_dir = htf.get("supertrend14", {}).get("direction")
        htf_adx = htf.get("adx14", {}).get("adx")
        if htf_adx is not None and float(htf_adx) > 25 and htf_dir is not None:
            if int(htf_dir) == 1:
                return False, None, 0, "htf_bullish"

        # why_score: minus_di 强度线性插值（20.56 → 0.0，35 → 1.0）
        score = self._linear_score(float(minus_di), low=min_di, high=35.0)
        return True, "sell", score, f"minus_di:{minus_di:.1f}"

    def _when(
        self, ctx: SignalContext, direction: str
    ) -> Tuple[bool, float, str]:
        tf = ctx.timeframe

        # 硬门控 1：macd.hist 未到极端负（避免做空反弹临界）
        macd_hist = ctx.indicators.get("macd", {}).get("hist")
        if macd_hist is None:
            return False, 0.0, "no_macd_hist"
        macd_min = get_tf_param(self, "macd_hist_min", tf, self._macd_hist_min)
        if float(macd_hist) <= macd_min:
            return False, 0.0, f"macd_extreme:{macd_hist:.1f}"

        # 硬门控 2：cci 未到极端超卖
        cci = ctx.indicators.get("cci20", {}).get("cci")
        if cci is None:
            return False, 0.0, "no_cci"
        cci_min = get_tf_param(self, "cci_min", tf, self._cci_min)
        if float(cci) <= cci_min:
            return False, 0.0, f"cci_extreme:{cci:.1f}"

        # when_score: macd_hist 和 cci 距离极端临界的程度
        macd_score = self._linear_score(
            float(macd_hist) - macd_min, low=0.0, high=10.0
        )
        cci_score = self._linear_score(
            float(cci) - cci_min, low=0.0, high=100.0
        )
        score = (macd_score + cci_score) / 2.0

        return True, score, f"macd={macd_hist:.1f},cci={cci:.0f}"

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        return EntrySpec(entry_type=EntryType.MARKET)

    _aggression: float = 0.20

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        # 下行延续：中等 trail + 1:1.67 RR（M30 bars_held 期望 ~12）
        aggr = get_tf_param(self, "aggression", ctx.timeframe, self._aggression)
        return ExitSpec(aggression=aggr, sl_atr=1.5, tp_atr=2.5)
