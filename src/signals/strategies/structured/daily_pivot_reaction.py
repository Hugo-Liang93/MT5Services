"""StructuredDailyPivotReaction — 触及 floor pivot levels (R1/R2/S1/S2) +
反转 K 线 → mean-reversion 入场。

设计理由（day-trading edge，与 prior_day_retest 互补）：
    daily floor pivots 是 retail trader 第二大关注的 SR levels（仅次于
    prior day H/L），公式简单（基于昨日 OHLC），价格触及反应概率高。
    本策略只取 R1/R2/S1/S2 四个 levels，**不取 P 本身**（pivot 是中性
    位置，反弹方向不明）。

入场逻辑：
    - 当前 close 距 nearest pivot level ≤ proximity ATR 倍数
    - nearest_level ∈ {s1, s2}（支撑）+ 看涨反转 → buy
    - nearest_level ∈ {r1, r2}（阻力）+ 看跌反转 → sell
    - nearest_level == pivot → 跳过（无方向）

风险逻辑：与 prior_day_retest 一致——紧 SL 1.0 ATR、TP 2.0 ATR、time 12 bars。

Regime affinity：与 prior_day_retest 一致（mean reversion 在 ranging 最强）。

互补关系（防补丁）：
    - prior_day_retest 用 prev_day_high/low（H4 上观察 retest pattern）
    - daily_pivot_reaction 用 R1/R2/S1/S2（H1 上触及频次更高）
    - 二者 alpha 来源不同（retail 关注的不同 SR 概念），不应合并
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import ExitMode, ExitSpec, HtfPolicy, StructuredStrategyBase

_BUY_LEVELS = frozenset({"s1", "s2"})
_SELL_LEVELS = frozenset({"r1", "r2"})


class StructuredDailyPivotReaction(StructuredStrategyBase):
    """触及 floor pivot R/S + 反转 → 反向入场（day-trading edge 2 — 手工）。"""

    name = "structured_daily_pivot_reaction"
    category = "reversion"
    htf_policy = HtfPolicy.NONE
    preferred_scopes = ("confirmed",)
    required_indicators = (
        "daily_pivots",
        "candle_pattern",
        "bar_stats20",
        "atr14",
        "adx14",
    )
    regime_affinity = {
        RegimeType.TRENDING: 0.50,
        RegimeType.BREAKOUT: 0.30,
        RegimeType.RANGING: 1.00,
        RegimeType.UNCERTAIN: 0.40,
    }

    # ── 触发参数 ──
    _proximity_atr: float = 0.3  # nearest level 距离 ≤ 0.3 ATR 才视为触及
    _max_adx: float = 35.0  # 高 ADX = 强趋势，反弹概率低 → 拒

    # ── Exit 参数 ──
    _sl_atr: float = 1.0
    _tp_atr: float = 2.0
    _time_bars: int = 12

    def _pivots(self, ctx: SignalContext) -> Dict[str, float]:
        return ctx.indicators.get("daily_pivots", {}) or {}

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        levels = self._pivots(ctx)
        if not levels:
            return False, None, 0.0, "no_pivots"

        atr = self._atr(ctx)
        if atr is None or atr <= 0:
            return False, None, 0.0, "no_atr"

        adx = self._adx_full(ctx).get("adx")
        if adx is not None and adx > self._max_adx:
            return False, None, 0.0, f"adx_too_high:{adx:.0f}"

        nearest_name = str(levels.get("nearest_level_name", ""))
        nearest_dist = abs(float(levels.get("nearest_level_distance", 1e9)))

        proximity = self._proximity_atr * atr
        if nearest_dist > proximity:
            return False, None, 0.0, f"too_far_from_level:{nearest_dist:.2f}>{proximity:.2f}"

        if nearest_name in _BUY_LEVELS:
            direction = "buy"
        elif nearest_name in _SELL_LEVELS:
            direction = "sell"
        else:
            # nearest 是 pivot（中性）或字段缺失 → 不交易
            return False, None, 0.0, f"neutral_level:{nearest_name}"

        # score：距离越近越好（0~proximity 线性）
        score = 1.0 - nearest_dist / max(proximity, 1e-9)
        return (
            True,
            direction,
            min(max(score, 0.0), 1.0),
            f"near_{nearest_name} dist={nearest_dist:.2f}/atr{atr:.2f}",
        )

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """入场时机：反转 K 线（与 prior_day_retest 共享判定）。"""
        candle = ctx.indicators.get("candle_pattern", {}) or {}
        stats = ctx.indicators.get("bar_stats20", {}) or {}

        pin = candle.get("pin_bar", 0.0) or 0.0
        ham = candle.get("hammer", 0.0) or 0.0
        rej = candle.get("rejection", 0.0) or 0.0
        eng = candle.get("engulfing", 0.0) or 0.0
        close_pos = stats.get("close_position", 0.5) or 0.5

        signals: list[tuple[float, str]] = []
        if direction == "buy":
            if pin > 0:
                signals.append((1.0, "pin_bull"))
            if ham > 0:
                signals.append((0.85, "hammer"))
            if eng > 0:
                signals.append((0.85, "engulfing_bull"))
            if rej > 0 and close_pos > 0.5:
                signals.append((0.65, "rejection_bull"))
        else:
            if pin < 0:
                signals.append((1.0, "pin_bear"))
            if ham < 0:
                signals.append((0.85, "shooting_star"))
            if eng < 0:
                signals.append((0.85, "engulfing_bear"))
            if rej < 0 and close_pos < 0.5:
                signals.append((0.65, "rejection_bear"))

        if not signals:
            return False, 0.0, "no_reversal_pattern"

        score, reason = max(signals, key=lambda x: x[0])
        return True, score, reason

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        sl = get_tf_param(self, "sl_atr", ctx.timeframe, self._sl_atr)
        tp = get_tf_param(self, "tp_atr", ctx.timeframe, self._tp_atr)
        tb = int(get_tf_param(self, "time_bars", ctx.timeframe, self._time_bars))
        return ExitSpec(
            sl_atr=sl,
            tp_atr=tp,
            mode=ExitMode.BARRIER,
            time_bars=tb,
        )
