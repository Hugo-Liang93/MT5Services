"""StructuredPriceAction — M15 价格行为入场策略。

设计理念：与指标驱动策略的核心区别——
- 指标策略：EMA/RSI/ADX 同时满足 → 信号（条件严苛，低频）
- 价格行为策略：K 线形态 + 结构位 + 趋势方向 → 信号（直觉化，高频）

核心逻辑：

1. Why（方向确认）：价格结构趋势方向
   - structure_type = 1.0（HH+HL 上升结构）→ buy
   - structure_type = -1.0（LH+LL 下降结构）→ sell
   - 比 EMA 排列更快响应趋势变化

2. When（入场触发）：K 线形态信号
   - Pin bar（多头/空头）→ 反转/顺势入场
   - Engulfing（吞没形态）→ 动量爆发
   - Hammer（锤子线）→ 底部/顶部信号
   - body_ratio > 1.5 的大 bar → 动量确认
   - 条件宽松：任一形态出现即满足（不像指标策略要求多条件同时满足）

3. Where（结构位确认）：价格在 Bollinger Band / Keltner Channel 位置
   - Buy：价格在下轨附近（回调到支撑）
   - Sell：价格在上轨附近（回调到阻力）

4. Entry：Market 单直接入场（价格行为信号本身就是确认）

5. Exit：BARRIER 模式 — 固定 SL 1.5 ATR / TP 2.5 ATR / 超时 20 bar
   - 不用 Chandelier trail（M15 利润窗口短，trail 容易被震掉）

目标性能：M15 每天 2-5 笔 | 胜率 45%+ | W/L 1.3+ | PF > 1.2
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import (
    EntrySpec,
    EntryType,
    ExitMode,
    ExitSpec,
    HtfPolicy,
    StructuredStrategyBase,
)


class StructuredPriceAction(StructuredStrategyBase):
    """M15 价格行为：K 线形态 + 价格结构方向 + 结构位确认。"""

    name = "structured_price_action"
    category = "price_action"
    htf_policy = HtfPolicy.NONE  # 不依赖 HTF，M15 独立运行
    preferred_scopes = ("confirmed",)
    required_indicators = (
        "candle_pattern",
        "bar_stats20",
        "price_struct20",
        "atr14",
        "boll20",
        "keltner20",
        "adx14",
    )
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.BREAKOUT: 0.70,
        RegimeType.RANGING: 0.40,  # 震荡中 pin bar 反转也有效
        RegimeType.UNCERTAIN: 0.20,
    }

    # ── Why 参数 ──
    _require_structure: bool = (
        False  # M15 上 structure_type 经常为 0，用 trend_bars 替代
    )
    _allow_counter_trend: bool = False  # 是否允许逆势（形态 vs 结构方向冲突）

    # ── When 参数 ──
    _min_body_ratio: float = 0.8  # 非形态信号的最低 body_ratio
    _big_bar_body_ratio: float = 1.5  # 大 bar 动量入场阈值
    _adx_floor: float = 15.0  # ADX 最低门槛（M15 用更低阈值）

    # ── Where 参数 ──
    _bb_extreme_buy: float = 0.25  # buy 时 BB position 上限（下轨区域）
    _bb_extreme_sell: float = 0.75  # sell 时 BB position 下限（上轨区域）

    # ── Exit 参数 ──
    _sl_atr: float = 1.5
    _tp_atr: float = 2.5
    _time_bars: int = 20  # 约 5 小时（20 × 15min）

    def _candle(self, ctx: SignalContext) -> Dict[str, float]:
        return ctx.indicators.get("candle_pattern", {})

    def _bar_stats(self, ctx: SignalContext) -> Dict[str, float]:
        return ctx.indicators.get("bar_stats20", {})

    def _price_struct(self, ctx: SignalContext) -> Dict[str, float]:
        return ctx.indicators.get("price_struct20", {})

    def _keltner_position(self, ctx: SignalContext) -> Optional[float]:
        """价格在 Keltner Channel 中的位置（0=下轨, 1=上轨）。"""
        kc = ctx.indicators.get("keltner20", {})
        upper, lower = kc.get("kc_upper"), kc.get("kc_lower")
        close = self._close(ctx)
        if upper is None or lower is None or close is None:
            return None
        w = float(upper) - float(lower)
        return (close - float(lower)) / w if w > 0 else 0.5

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        """方向确认：价格结构趋势。"""
        ps = self._price_struct(ctx)
        structure = ps.get("structure_type", 0.0)
        trend_bars = ps.get("trend_bars", 0.0)

        if self._require_structure and structure == 0.0:
            return False, None, 0, "no_structure"

        if structure > 0:
            direction = "buy"
        elif structure < 0:
            direction = "sell"
        else:
            # 无明确结构，用 trend_bars 方向
            if trend_bars > 0:
                direction = "buy"
            elif trend_bars < 0:
                direction = "sell"
            else:
                return False, None, 0, "no_trend"

        # ADX 基本门槛（M15 用较低阈值）
        adx_data = self._adx_full(ctx)
        adx = adx_data.get("adx")
        adx_floor = get_tf_param(self, "adx_floor", ctx.timeframe, self._adx_floor)
        if adx is not None and adx < adx_floor:
            return False, None, 0, f"adx_low:{adx:.0f}<{adx_floor:.0f}"

        # 评分：trend_bars 越长、structure 越明确越好
        trend_score = min(abs(float(trend_bars)) / 5.0, 1.0)
        struct_score = 0.5 if structure != 0.0 else 0.0
        score = 0.5 * trend_score + 0.5 * struct_score

        return (
            True,
            direction,
            score,
            f"struct:{structure:.0f},trend_bars:{trend_bars:.0f}",
        )

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        """入场触发：K 线形态信号。"""
        candle = self._candle(ctx)
        stats = self._bar_stats(ctx)

        pin = candle.get("pin_bar", 0.0)
        eng = candle.get("engulfing", 0.0)
        ham = candle.get("hammer", 0.0)
        body_ratio = stats.get("body_ratio", 0.0)
        close_pos = stats.get("close_position", 0.5)

        signals: list[tuple[float, str]] = []

        # Pin bar：最强价格行为信号
        if direction == "buy" and pin > 0:
            signals.append((0.9, "pin_bar_bull"))
        elif direction == "sell" and pin < 0:
            signals.append((0.9, "pin_bar_bear"))

        # Engulfing：动量爆发信号
        if direction == "buy" and eng > 0:
            signals.append((0.85, "engulfing_bull"))
        elif direction == "sell" and eng < 0:
            signals.append((0.85, "engulfing_sell"))

        # Hammer：经典反转信号
        if direction == "buy" and ham > 0:
            signals.append((0.7, "hammer_bull"))
        elif direction == "sell" and ham < 0:
            signals.append((0.7, "hammer_bear"))

        # 大 bar 动量确认：body > 1.5x 均值且方向一致
        big_bar = get_tf_param(
            self, "big_bar_body_ratio", ctx.timeframe, self._big_bar_body_ratio
        )
        if body_ratio >= big_bar:
            if direction == "buy" and close_pos > 0.7:
                signals.append((0.6, f"big_bar_bull:{body_ratio:.1f}"))
            elif direction == "sell" and close_pos < 0.3:
                signals.append((0.6, f"big_bar_bear:{body_ratio:.1f}"))

        if not signals:
            return False, 0, "no_pattern"

        # 取最强信号
        best_score, best_reason = max(signals, key=lambda x: x[0])
        return True, best_score, best_reason

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        """结构位确认：价格在 BB/Keltner 的位置。"""
        bb_pos = self._bb_position(ctx)
        kc_pos = self._keltner_position(ctx)

        # 取 BB 和 Keltner 的平均位置
        positions = [p for p in (bb_pos, kc_pos) if p is not None]
        if not positions:
            return 0.0, ""
        avg_pos = sum(positions) / len(positions)

        if direction == "buy":
            extreme = get_tf_param(
                self, "bb_extreme_buy", ctx.timeframe, self._bb_extreme_buy
            )
            if avg_pos <= extreme:
                score = 1.0 - avg_pos / extreme  # 越低越好
                return min(score, 1.0), f"channel_low:{avg_pos:.2f}"
            # 中性区域微弱加分
            if avg_pos < 0.5:
                return 0.2, f"channel_mid:{avg_pos:.2f}"
        else:
            extreme = get_tf_param(
                self, "bb_extreme_sell", ctx.timeframe, self._bb_extreme_sell
            )
            if avg_pos >= extreme:
                score = (avg_pos - extreme) / (1.0 - extreme) if extreme < 1.0 else 0.0
                return min(score, 1.0), f"channel_high:{avg_pos:.2f}"
            if avg_pos > 0.5:
                return 0.2, f"channel_mid:{avg_pos:.2f}"

        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        """量能确认：形态 bar 放量加分。"""
        vr = self._volume_ratio(ctx)
        if vr is None:
            return 0.0
        # 放量：1.0→0, 1.5→0.5, 2.0→1.0
        return self._linear_score(vr, low=1.0, high=2.0)

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        """Market 单直接入场——价格行为信号本身就是确认。"""
        return EntrySpec(entry_type=EntryType.MARKET)

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        """BARRIER 模式：固定 SL/TP/Time，不用 Chandelier trail。"""
        sl = get_tf_param(self, "sl_atr", ctx.timeframe, self._sl_atr)
        tp = get_tf_param(self, "tp_atr", ctx.timeframe, self._tp_atr)
        tb = int(get_tf_param(self, "time_bars", ctx.timeframe, self._time_bars))
        return ExitSpec(
            sl_atr=sl,
            tp_atr=tp,
            mode=ExitMode.BARRIER,
            time_bars=tb,
        )
