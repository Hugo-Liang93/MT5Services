"""StructuredPriorDayRetest — 触及昨日 H/L + 反转 K 线 → mean-reversion 入场。

设计理由（day-trading edge，非 mining 衍生）：
    昨日 H/L 是 retail trader 普遍参考的 SR levels（图表上一目了然），
    价格触及后反应概率显著高于随机水平。配合反转 K 线（pin bar / hammer
    / rejection）形成 confluence → mean-reversion 入场。

入场逻辑：
    - buy:  close 接近 prev_day_low（位置 ≤ 0.20）+ 距离 ≤ proximity ATR
            + 未深度破位（distance_to_prev_low ≥ -max_breakthrough × ATR）
            + 看涨反转 K 线（pin bar bull / hammer / rejection bull）
    - sell: close 接近 prev_day_high（位置 ≥ 0.80）+ 距离 ≤ proximity ATR
            + 未深度破位 + 看跌反转 K 线

风险逻辑：紧 SL 1.0 ATR（retest 失败要快止损），TP 2.0 ATR（反转后向 prev_close
靠拢通常约 2 ATR），time 12 bars 超时清仓（不长持反转）。

Regime affinity：
    - RANGING: 1.0（震荡市 mean reversion 最强）
    - TRENDING: 0.5（趋势中反转概率适中）
    - BREAKOUT: 0.3（突破中反转概率低）
    - UNCERTAIN: 0.4
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import ExitMode, ExitSpec, HtfPolicy, StructuredStrategyBase


class StructuredPriorDayRetest(StructuredStrategyBase):
    """昨日 SR 反转入场（day-trading edge 1 — 手工，无 mining 依赖）。"""

    name = "structured_prior_day_retest"
    category = "reversion"
    htf_policy = HtfPolicy.NONE
    preferred_scopes = ("confirmed",)
    required_indicators = (
        "prior_day_levels",
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

    # ── Where 参数 ──
    _buy_position_max: float = 0.20  # close 在昨日区间位置 ≤ 此值才视为接近 prev_low
    _sell_position_min: float = 0.80  # 接近 prev_high
    _proximity_atr: float = 0.5  # 距 prev H/L 不超过此 ATR 倍数才算 retest
    _max_breakthrough_atr: float = 0.5  # 破 prev H/L 超过此 ATR 倍数视为破位非反转

    # ── ADX 门控 ──
    _max_adx: float = 35.0  # 高 ADX = 强趋势，反转概率低 → 拒

    # ── Exit 参数 ──
    _sl_atr: float = 1.0
    _tp_atr: float = 2.0
    _time_bars: int = 12  # H1 = 半天

    def _prior_levels(self, ctx: SignalContext) -> Dict[str, float]:
        return ctx.indicators.get("prior_day_levels", {}) or {}

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        """方向确认：基于 close 在昨日区间位置 + 距离 prev H/L 的 ATR 倍数。"""
        levels = self._prior_levels(ctx)
        if not levels:
            return False, None, 0.0, "no_prior_levels"

        atr = self._atr(ctx)
        if atr is None or atr <= 0:
            return False, None, 0.0, "no_atr"

        # ADX 上限：mean-reversion 在强趋势市无效
        adx_data = self._adx_full(ctx)
        adx = adx_data.get("adx")
        if adx is not None and adx > self._max_adx:
            return False, None, 0.0, f"adx_too_high:{adx:.0f}>{self._max_adx:.0f}"

        position = float(levels.get("position_in_prev_range", 0.5))
        dist_high = float(levels.get("distance_to_prev_high", 0.0))
        dist_low = float(levels.get("distance_to_prev_low", 0.0))

        proximity = self._proximity_atr * atr
        breakthrough = self._max_breakthrough_atr * atr

        # buy：靠近昨低，未深破
        buy_pos = self._buy_position_max
        sell_pos = self._sell_position_min
        if position <= buy_pos:
            # dist_low > 0 表示在 prev_low 之上；dist_low < 0 表示已破位（向下）
            if abs(dist_low) <= proximity and dist_low >= -breakthrough:
                # score：越接近 prev_low 越好（位置归一化到 0~1）
                score = 1.0 - position / max(buy_pos, 1e-9)
                return (
                    True,
                    "buy",
                    min(max(score, 0.0), 1.0),
                    f"near_prev_low pos={position:.2f} dist={dist_low:.2f}/atr{atr:.2f}",
                )
            return False, None, 0.0, "buy_zone_breached_or_far"

        if position >= sell_pos:
            if abs(dist_high) <= proximity and dist_high >= -breakthrough:
                score = (position - sell_pos) / max(1.0 - sell_pos, 1e-9)
                return (
                    True,
                    "sell",
                    min(max(score, 0.0), 1.0),
                    f"near_prev_high pos={position:.2f} dist={dist_high:.2f}/atr{atr:.2f}",
                )
            return False, None, 0.0, "sell_zone_breached_or_far"

        return False, None, 0.0, f"mid_range_pos={position:.2f}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """入场时机：反转 K 线信号（pin bar / hammer / rejection）。"""
        candle = ctx.indicators.get("candle_pattern", {}) or {}
        stats = ctx.indicators.get("bar_stats20", {}) or {}

        pin = candle.get("pin_bar", 0.0) or 0.0
        ham = candle.get("hammer", 0.0) or 0.0  # > 0 hammer; < 0 shooting star
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
        else:  # sell
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

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        """精确触及加分：close 距 prev H/L 越接近越好（< 0.1 ATR 视为 precise touch）。"""
        levels = self._prior_levels(ctx)
        atr = self._atr(ctx)
        if not levels or atr is None or atr <= 0:
            return 0.0, ""

        if direction == "buy":
            dist = abs(float(levels.get("distance_to_prev_low", 0.0)))
        else:
            dist = abs(float(levels.get("distance_to_prev_high", 0.0)))

        # 0.0~0.5 ATR 线性：0 → 1.0, 0.5 ATR → 0
        proximity_atr = self._proximity_atr
        if dist <= 0.1 * atr:
            return 1.0, "precise_touch"
        if dist <= proximity_atr * atr:
            score = 1.0 - (dist / atr - 0.1) / max(proximity_atr - 0.1, 1e-9)
            return max(0.0, min(score, 1.0)), f"close_touch:{dist / atr:.2f}atr"
        return 0.0, ""

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
