"""StructuredStrongTrendFollow — 强趋势延续（挖掘驱动）。

核心逻辑：ADX 极端值（>40）标志强趋势，DI 方向确认多头，ADX 仍在上行
（与 regime_exhaustion 互斥），MACD hist 回归中性 + ROC 未崩溃 → 顺势 buy。

挖掘来源：2026-04-17 H1 rule_mining #5
  IF adx14.adx > 40.12 AND macd_fast.hist <= 1.61 AND roc12.roc > -1.17 THEN buy
  (test WR 60.1% / n=143)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import EntrySpec, ExitMode, ExitSpec, HtfPolicy, StructuredStrategyBase


class StructuredStrongTrendFollow(StructuredStrategyBase):
    """ADX 极端 + DI 多头 + MACD 温和 + ROC 稳 → 顺势延续入场。"""

    name = "structured_strong_trend_follow"
    category = "trend_continuation"
    htf_policy = HtfPolicy.NONE
    required_indicators = (
        "atr14",
        "adx14",
        "macd_fast",
        "roc12",
        "volume_ratio20",
    )
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.BREAKOUT: 0.60,
        RegimeType.RANGING: 0.00,
        RegimeType.UNCERTAIN: 0.20,
    }
    research_provenance_refs = ("2026-04-17-H1-rule-mining-#5",)

    # ── 可调参数（挖掘阈值为默认值）──
    _adx_extreme: float = 40.0
    _adx_d3_min_strict: float = 0.0       # 与 regime_exhaustion 互斥：需严格 > 此值
    _macd_hist_upper: float = 1.61        # 挖掘阈值
    _macd_hist_lower: float = -2.0        # 下限保护
    _roc_lower: float = -1.17             # 挖掘阈值
    _sl_atr: float = 1.5
    _tp_atr: float = 2.5
    _time_bars: int = 20

    def _why(
        self, ctx: SignalContext
    ) -> Tuple[bool, Optional[str], float, str]:
        adx_data = self._adx_full(ctx)
        adx = adx_data["adx"]
        adx_d3 = adx_data["adx_d3"]
        plus_di = adx_data["plus_di"]
        minus_di = adx_data["minus_di"]

        if adx is None or plus_di is None or minus_di is None:
            return False, None, 0.0, "no_adx_data"

        threshold = get_tf_param(
            self, "adx_extreme", ctx.timeframe, self._adx_extreme
        )
        if adx <= threshold:
            return False, None, 0.0, f"adx_low:{adx:.0f}"

        if plus_di <= minus_di:
            return False, None, 0.0, (
                f"di_not_bullish:+{plus_di:.0f}/-{minus_di:.0f}"
            )

        # adx_d3 必须严格为正（与 regime_exhaustion 在 adx_d3<=0 时互斥）
        d3_min = get_tf_param(
            self, "adx_d3_min_strict", ctx.timeframe, self._adx_d3_min_strict
        )
        if adx_d3 is None or adx_d3 <= d3_min:
            return False, None, 0.0, f"adx_not_rising:d3={adx_d3}"

        score = self._linear_score(adx, low=threshold, high=threshold + 15.0)
        score = max(score, 0.4)

        return (
            True,
            "buy",
            score,
            f"strong_trend:adx={adx:.0f},di_diff={plus_di - minus_di:.0f}",
        )

    def _when(
        self, ctx: SignalContext, direction: str
    ) -> Tuple[bool, float, str]:
        macd = ctx.indicators.get("macd_fast", {})
        roc_data = ctx.indicators.get("roc12", {})

        macd_hist = macd.get("hist")
        roc = roc_data.get("roc")

        if macd_hist is None or roc is None:
            return False, 0.0, "no_macd_or_roc"

        tf = ctx.timeframe
        hist_hi = get_tf_param(
            self, "macd_hist_upper", tf, self._macd_hist_upper
        )
        hist_lo = get_tf_param(
            self, "macd_hist_lower", tf, self._macd_hist_lower
        )
        roc_lo = get_tf_param(self, "roc_lower", tf, self._roc_lower)

        if macd_hist > hist_hi:
            return False, 0.0, f"macd_hist_too_high:{macd_hist:.2f}"
        if macd_hist < hist_lo:
            return False, 0.0, f"macd_hist_too_low:{macd_hist:.2f}"
        if roc <= roc_lo:
            return False, 0.0, f"roc_too_low:{roc:.2f}"

        # 评分：macd_hist 越接近 0 越"温和"，score 越高
        center = 0.0
        half_range = max(abs(hist_hi - center), abs(hist_lo - center))
        distance = abs(macd_hist - center)
        score = (
            max(0.0, 1.0 - distance / half_range) if half_range > 0 else 0.0
        )
        score = max(score, 0.3)

        return True, score, f"timing:macd_hist={macd_hist:.2f},roc={roc:.2f}"

    def _where(
        self, ctx: SignalContext, direction: str
    ) -> Tuple[float, str]:
        ms = self._ms(ctx)
        if str(ms.get("compression_state", "none")) != "none":
            return 0.8, f"compression={ms.get('compression_state')}"
        if str(ms.get("breakout_state", "none")) != "none":
            return 0.8, f"breakout={ms.get('breakout_state')}"
        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        return self._linear_score(self._volume_ratio(ctx), low=1.2, high=1.5)

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        raise NotImplementedError  # Task 5 实现

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        raise NotImplementedError  # Task 5 实现
