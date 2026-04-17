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
        raise NotImplementedError  # Task 2 实现

    def _when(
        self, ctx: SignalContext, direction: str
    ) -> Tuple[bool, float, str]:
        raise NotImplementedError  # Task 3 实现

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        raise NotImplementedError  # Task 5 实现

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        raise NotImplementedError  # Task 5 实现
