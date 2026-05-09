"""StructuredPriceAction — M15 价格行为入场策略（重写版，无兼容设计）。

设计哲学：plan §0zl ABCD 探索数据驱动，原 PA (PF 0.285 / DD 99%) 因 _why-When-Where
评分函数结构性过宽（trend_bars 兜底 + _where 软门控）导致 1167 trades/1y 大量
低质量入场。本版完全重写为「硬条件多重 vetting + 共振 _when + Chandelier 出场」：

1. _why（4 件套硬 vetting）：
   - structure_type ≠ 0 OR |trend_bars| ≥ _trend_bars_min（不再无门槛兜底）
   - ADX ≥ _adx_min（要求趋势强度）
   - close vs EMA50 同侧（顺势过滤）
   - BB + Keltner 同时在极端区（hard veto，不达标直接拒绝）

2. _when（3 件套共振硬条件）：
   - K 线形态出现（pin / engulfing / three_method / hammer 任一）
   - body_ratio ≥ _body_ratio_min（实体足够强）
   - close_position 极端（buy ≤ _close_pos_buy_max；sell ≥ _close_pos_sell_min）

3. _where：保留软分（_why 已 hard veto），按 BB+Keltner 精确度加分

4. 出场：固定 Chandelier α 系数（不再切 BARRIER；BARRIER 适合挖掘规则的 fixed RR
   语义，不适合形态信号）

目标：trades 50-200/year，WR 45%+，PF ≥ 1.0（plan §0zl 决策树选项 1）
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import ExitMode, ExitSpec, HtfPolicy, StructuredStrategyBase


class StructuredPriceAction(StructuredStrategyBase):
    """M15 价格行为：4 件套 _why 硬 vetting + 3 件套 _when 共振 + Chandelier 出场。"""

    name = "structured_price_action"
    category = "price_action"
    htf_policy = HtfPolicy.NONE
    preferred_scopes = ("confirmed",)
    required_indicators = (
        "candle_pattern",
        "bar_stats20",
        "price_struct20",
        "atr14",
        "boll20",
        "keltner20",
        "adx14",
        "ema50",
    )
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.BREAKOUT: 0.70,
        RegimeType.RANGING: 0.40,
        RegimeType.UNCERTAIN: 0.20,
    }

    # ── _why 硬条件参数 ──
    _adx_min: float = 18.0
    _trend_bars_min: int = 5
    _ema50_filter_enabled: bool = True
    _bb_buy_max: float = 0.30  # buy 时 BB+KC 平均位置必须 ≤ 此值
    _bb_sell_min: float = 0.70  # sell 时 BB+KC 平均位置必须 ≥ 此值

    # ── _when 共振参数 ──
    _body_ratio_min: float = 1.0  # K 线实体/均值的最小比
    _close_pos_buy_max: float = 0.30  # buy 时 close_position 必须 ≤
    _close_pos_sell_min: float = 0.70  # sell 时 close_position 必须 ≥

    # ── 出场 ──
    _aggression: float = 0.50

    def _candle(self, ctx: SignalContext) -> Dict[str, float]:
        return ctx.indicators.get("candle_pattern", {})

    def _bar_stats(self, ctx: SignalContext) -> Dict[str, float]:
        return ctx.indicators.get("bar_stats20", {})

    def _price_struct(self, ctx: SignalContext) -> Dict[str, float]:
        return ctx.indicators.get("price_struct20", {})

    def _ema50(self, ctx: SignalContext) -> Optional[float]:
        ema_data = ctx.indicators.get("ema50", {})
        value = ema_data.get("ema") if isinstance(ema_data, dict) else None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _keltner_position(self, ctx: SignalContext) -> Optional[float]:
        kc = ctx.indicators.get("keltner20", {})
        upper, lower = kc.get("kc_upper"), kc.get("kc_lower")
        close = self._close(ctx)
        if upper is None or lower is None or close is None:
            return None
        w = float(upper) - float(lower)
        return (close - float(lower)) / w if w > 0 else 0.5

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        """4 件套硬 vetting：structure/trend + ADX + EMA50 + BB/KC 极端。"""
        ps = self._price_struct(ctx)
        structure = ps.get("structure_type", 0.0)
        trend_bars = ps.get("trend_bars", 0.0)
        trend_bars_min = int(
            get_tf_param(self, "trend_bars_min", ctx.timeframe, self._trend_bars_min)
        )

        # 1. 方向（structure 优先；trend_bars 必须 ≥ 阈值才兜底）
        if structure > 0:
            direction = "buy"
        elif structure < 0:
            direction = "sell"
        elif abs(trend_bars) >= trend_bars_min:
            direction = "buy" if trend_bars > 0 else "sell"
        else:
            return False, None, 0.0, f"no_strong_trend:tb={trend_bars:.0f}"

        # 2. ADX 硬条件
        adx_data = self._adx_full(ctx)
        adx = adx_data.get("adx")
        adx_min = float(get_tf_param(self, "adx_min", ctx.timeframe, self._adx_min))
        if adx is None or float(adx) < adx_min:
            return False, None, 0.0, f"adx_low:{adx}<{adx_min:.0f}"

        # 3. EMA50 同侧
        if self._ema50_filter_enabled:
            ema50 = self._ema50(ctx)
            close = self._close(ctx)
            if ema50 is not None and close is not None:
                if direction == "buy" and close < ema50:
                    return False, None, 0.0, f"buy_below_ema50:{close:.2f}<{ema50:.2f}"
                if direction == "sell" and close > ema50:
                    return False, None, 0.0, f"sell_above_ema50:{close:.2f}>{ema50:.2f}"

        # 4. BB + Keltner 同时在极端区（hard veto）
        bb_pos = self._bb_position(ctx)
        kc_pos = self._keltner_position(ctx)
        if bb_pos is None or kc_pos is None:
            return False, None, 0.0, "no_channel_data"
        bb_buy_max = float(
            get_tf_param(self, "bb_buy_max", ctx.timeframe, self._bb_buy_max)
        )
        bb_sell_min = float(
            get_tf_param(self, "bb_sell_min", ctx.timeframe, self._bb_sell_min)
        )
        if direction == "buy":
            if bb_pos > bb_buy_max or kc_pos > bb_buy_max:
                return (
                    False,
                    None,
                    0.0,
                    f"buy_not_extreme:bb={bb_pos:.2f},kc={kc_pos:.2f}",
                )
        else:
            if bb_pos < bb_sell_min or kc_pos < bb_sell_min:
                return (
                    False,
                    None,
                    0.0,
                    f"sell_not_extreme:bb={bb_pos:.2f},kc={kc_pos:.2f}",
                )

        # 评分（trend 长度 + ADX 强度）
        trend_score = min(abs(float(trend_bars)) / 10.0, 1.0)
        adx_score = min(max(0.0, (float(adx) - adx_min) / 20.0), 1.0)
        score = 0.6 * trend_score + 0.4 * adx_score

        return (
            True,
            direction,
            score,
            f"struct:{structure:.0f},tb:{trend_bars:.0f},adx:{float(adx):.0f}",
        )

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """3 件套共振硬条件：K 线形态 + body_ratio + close_position 极端。"""
        candle = self._candle(ctx)
        stats = self._bar_stats(ctx)

        pin = candle.get("pin_bar", 0.0)
        eng = candle.get("engulfing", 0.0)
        ham = candle.get("hammer", 0.0)
        three = candle.get("three_method", 0.0)
        body_ratio = stats.get("body_ratio", 0.0)
        close_pos = stats.get("close_position", 0.5)

        # 1. K 线形态硬条件
        patterns: list[Tuple[float, str]] = []
        if direction == "buy":
            if pin > 0:
                patterns.append((0.9, "pin_bull"))
            if eng > 0:
                patterns.append((0.85, "eng_bull"))
            if three > 0:
                patterns.append((0.8, "three_sol"))
            if ham > 0:
                patterns.append((0.7, "hammer_bull"))
        else:
            if pin < 0:
                patterns.append((0.9, "pin_bear"))
            if eng < 0:
                patterns.append((0.85, "eng_bear"))
            if three < 0:
                patterns.append((0.8, "three_crow"))
            if ham < 0:
                patterns.append((0.7, "hammer_bear"))
        if not patterns:
            return False, 0.0, "no_pattern"

        # 2. body_ratio 硬条件
        body_min = float(
            get_tf_param(self, "body_ratio_min", ctx.timeframe, self._body_ratio_min)
        )
        if body_ratio < body_min:
            return False, 0.0, f"body_low:{body_ratio:.2f}<{body_min:.2f}"

        # 3. close_position 硬条件
        cp_buy_max = float(
            get_tf_param(
                self, "close_pos_buy_max", ctx.timeframe, self._close_pos_buy_max
            )
        )
        cp_sell_min = float(
            get_tf_param(
                self, "close_pos_sell_min", ctx.timeframe, self._close_pos_sell_min
            )
        )
        if direction == "buy" and close_pos > cp_buy_max:
            return False, 0.0, f"close_high:{close_pos:.2f}>{cp_buy_max:.2f}"
        if direction == "sell" and close_pos < cp_sell_min:
            return False, 0.0, f"close_low:{close_pos:.2f}<{cp_sell_min:.2f}"

        # 取最强形态
        best_score, best_reason = max(patterns, key=lambda x: x[0])
        return (
            True,
            best_score,
            f"{best_reason}+body{body_ratio:.1f}+cpos{close_pos:.2f}",
        )

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        """精确度加分（_why 已 hard veto），位置越极端分越高。"""
        bb_pos = self._bb_position(ctx)
        kc_pos = self._keltner_position(ctx)
        if bb_pos is None or kc_pos is None:
            return 0.0, ""
        avg_pos = (bb_pos + kc_pos) / 2.0
        bb_buy_max = float(
            get_tf_param(self, "bb_buy_max", ctx.timeframe, self._bb_buy_max)
        )
        bb_sell_min = float(
            get_tf_param(self, "bb_sell_min", ctx.timeframe, self._bb_sell_min)
        )
        if direction == "buy":
            score = 1.0 - (avg_pos / bb_buy_max) if bb_buy_max > 0 else 0.0
            return min(max(score, 0.0), 1.0), f"channel:{avg_pos:.2f}"
        score = (
            (avg_pos - bb_sell_min) / (1.0 - bb_sell_min) if bb_sell_min < 1.0 else 0.0
        )
        return min(max(score, 0.0), 1.0), f"channel:{avg_pos:.2f}"

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        vr = self._volume_ratio(ctx)
        if vr is None:
            return 0.0
        return self._linear_score(vr, low=1.0, high=2.0)

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        """固定 Chandelier α 出场（plan §0zl 选项 1：BARRIER 不适合形态信号）。"""
        aggression = float(
            get_tf_param(self, "aggression", ctx.timeframe, self._aggression)
        )
        return ExitSpec(aggression=aggression, mode=ExitMode.CHANDELIER)
