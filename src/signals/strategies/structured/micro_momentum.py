"""M1/M5 confirmed-bar micro momentum strategy."""

from __future__ import annotations

from typing import Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import ExitMode, ExitSpec, HtfPolicy, StructuredStrategyBase


class StructuredMicroMomentum(StructuredStrategyBase):
    """M1/M5 intraday momentum using only confirmed bar facts."""

    name = "structured_micro_momentum"
    category = "trend"
    htf_policy = HtfPolicy.NONE
    preferred_scopes = ("confirmed",)
    required_indicators = (
        "bar_stats20",
        "price_struct20",
        "atr14",
        "boll20",
        "keltner20",
        "adx14",
        "volume_ratio20",
    )
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.BREAKOUT: 0.90,
        RegimeType.RANGING: 0.25,
        RegimeType.UNCERTAIN: 0.15,
    }
    research_provenance_refs = ("goal:m1_m5_hft_v1",)

    _base_confidence: float = 0.52

    _adx_floor: float = 18.0
    _di_spread_floor: float = 4.0
    _min_trend_bars: float = 2.0
    _min_body_ratio: float = 1.15
    _min_volume_ratio: float = 1.15
    _close_buy_floor: float = 0.68
    _close_sell_ceiling: float = 0.32
    _channel_buy_floor: float = 0.56
    _channel_sell_ceiling: float = 0.44

    _default_exit_by_tf: dict[str, tuple[float, float, int]] = {
        "M1": (0.90, 1.35, 8),
        "M5": (1.10, 1.65, 6),
    }

    def _bar_stats(self, ctx: SignalContext) -> dict:
        return ctx.indicators.get("bar_stats20", {})

    def _price_struct(self, ctx: SignalContext) -> dict:
        return ctx.indicators.get("price_struct20", {})

    def _channel_position(self, ctx: SignalContext) -> Optional[float]:
        close = self._close(ctx)
        positions: list[float] = []

        boll = ctx.indicators.get("boll20", {})
        bb_upper = boll.get("bb_upper")
        bb_lower = boll.get("bb_lower")
        if close is not None and bb_upper is not None and bb_lower is not None:
            width = float(bb_upper) - float(bb_lower)
            if width > 0:
                positions.append((close - float(bb_lower)) / width)

        keltner = ctx.indicators.get("keltner20", {})
        kc_upper = keltner.get("kc_upper")
        kc_lower = keltner.get("kc_lower")
        if close is not None and kc_upper is not None and kc_lower is not None:
            width = float(kc_upper) - float(kc_lower)
            if width > 0:
                positions.append((close - float(kc_lower)) / width)

        if not positions:
            return None
        return max(0.0, min(1.0, sum(positions) / len(positions)))

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        ps = self._price_struct(ctx)
        structure = float(ps.get("structure_type", 0.0) or 0.0)
        trend_bars = float(ps.get("trend_bars", 0.0) or 0.0)

        min_trend = get_tf_param(
            self, "min_trend_bars", ctx.timeframe, self._min_trend_bars
        )
        if structure > 0 or trend_bars >= min_trend:
            direction = "buy"
        elif structure < 0 or trend_bars <= -min_trend:
            direction = "sell"
        else:
            return False, None, 0.0, "no_micro_trend"

        adx_data = self._adx_full(ctx)
        adx = adx_data.get("adx")
        plus_di = adx_data.get("plus_di")
        minus_di = adx_data.get("minus_di")
        if adx is None or plus_di is None or minus_di is None:
            return False, None, 0.0, "adx_di_missing"

        adx_floor = get_tf_param(self, "adx_floor", ctx.timeframe, self._adx_floor)
        if adx < adx_floor:
            return False, None, 0.0, f"adx_low:{adx:.0f}<{adx_floor:.0f}"

        di_spread_floor = get_tf_param(
            self, "di_spread_floor", ctx.timeframe, self._di_spread_floor
        )
        di_spread = (plus_di - minus_di) if direction == "buy" else (minus_di - plus_di)
        if di_spread < di_spread_floor:
            return (
                False,
                None,
                0.0,
                f"di_spread_low:{di_spread:.1f}<{di_spread_floor:.1f}",
            )

        adx_score = self._linear_score(adx, adx_floor, adx_floor + 12.0)
        trend_score = self._linear_score(abs(trend_bars), min_trend, min_trend + 4.0)
        di_score = self._linear_score(
            di_spread, di_spread_floor, di_spread_floor + 10.0
        )
        score = 0.40 * adx_score + 0.30 * trend_score + 0.30 * di_score
        return (
            True,
            direction,
            score,
            f"micro_trend:{structure:.0f},trend_bars:{trend_bars:.0f},di:{di_spread:.1f}",
        )

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        stats = self._bar_stats(ctx)
        body_ratio = float(stats.get("body_ratio", 0.0) or 0.0)
        close_position = float(stats.get("close_position", 0.5) or 0.5)

        min_body = get_tf_param(
            self, "min_body_ratio", ctx.timeframe, self._min_body_ratio
        )
        if body_ratio < min_body:
            return False, 0.0, f"body_small:{body_ratio:.2f}<{min_body:.2f}"

        if direction == "buy":
            close_floor = get_tf_param(
                self, "close_buy_floor", ctx.timeframe, self._close_buy_floor
            )
            if close_position < close_floor:
                return False, 0.0, f"close_not_high:{close_position:.2f}<{close_floor:.2f}"
            close_score = self._linear_score(close_position, close_floor, 0.95)
        else:
            close_ceiling = get_tf_param(
                self,
                "close_sell_ceiling",
                ctx.timeframe,
                self._close_sell_ceiling,
            )
            if close_position > close_ceiling:
                return (
                    False,
                    0.0,
                    f"close_not_low:{close_position:.2f}>{close_ceiling:.2f}",
                )
            close_score = self._linear_score(1.0 - close_position, 1.0 - close_ceiling, 0.95)

        volume_ratio = self._volume_ratio(ctx)
        min_volume = get_tf_param(
            self, "min_volume_ratio", ctx.timeframe, self._min_volume_ratio
        )
        if volume_ratio is None:
            return False, 0.0, "volume_missing"
        if volume_ratio < min_volume:
            return False, 0.0, f"volume_low:{volume_ratio:.2f}<{min_volume:.2f}"

        body_score = self._linear_score(body_ratio, min_body, 2.0)
        volume_score = self._linear_score(volume_ratio, min_volume, 2.0)
        score = 0.40 * body_score + 0.35 * close_score + 0.25 * volume_score
        return (
            True,
            score,
            f"impulse:body:{body_ratio:.2f},close:{close_position:.2f},vol:{volume_ratio:.2f}",
        )

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        channel_pos = self._channel_position(ctx)
        if channel_pos is None:
            return 0.0, ""

        if direction == "buy":
            floor = get_tf_param(
                self, "channel_buy_floor", ctx.timeframe, self._channel_buy_floor
            )
            if channel_pos < floor:
                return 0.0, f"channel_not_expanding:{channel_pos:.2f}"
            return self._linear_score(channel_pos, floor, 0.90), f"channel_high:{channel_pos:.2f}"

        ceiling = get_tf_param(
            self, "channel_sell_ceiling", ctx.timeframe, self._channel_sell_ceiling
        )
        if channel_pos > ceiling:
            return 0.0, f"channel_not_expanding:{channel_pos:.2f}"
        return (
            self._linear_score(1.0 - channel_pos, 1.0 - ceiling, 0.90),
            f"channel_low:{channel_pos:.2f}",
        )

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        min_volume = get_tf_param(
            self, "min_volume_ratio", ctx.timeframe, self._min_volume_ratio
        )
        return self._linear_score(self._volume_ratio(ctx), min_volume, 2.0)

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        default_sl, default_tp, default_bars = self._default_exit_by_tf.get(
            str(ctx.timeframe).upper(),
            self._default_exit_by_tf["M5"],
        )
        sl = get_tf_param(self, "sl_atr", ctx.timeframe, default_sl)
        tp = get_tf_param(self, "tp_atr", ctx.timeframe, default_tp)
        time_bars = int(get_tf_param(self, "time_bars", ctx.timeframe, default_bars))
        return ExitSpec(
            sl_atr=sl,
            tp_atr=tp,
            mode=ExitMode.BARRIER,
            time_bars=time_bars,
        )
