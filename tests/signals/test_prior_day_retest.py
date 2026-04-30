"""StructuredPriorDayRetest — 触及昨日 H/L + 反转 K 线 → 反向入场。"""

from __future__ import annotations

from src.signals.models import SignalContext
from src.signals.strategies.structured.prior_day_retest import (
    StructuredPriorDayRetest,
)


def _make_context(
    *,
    # Prior day levels
    prev_high: float = 2710.0,
    prev_low: float = 2680.0,
    prev_close: float = 2700.0,
    close_now: float,
    atr: float = 5.0,
    # Candle pattern
    pin_bar: float = 0.0,
    engulfing: float = 0.0,
    hammer: float = 0.0,
    rejection: float = 0.0,
    shooting_star: float = 0.0,  # 用 hammer 负值代表（与 base 约定一致）
    # Bar stats
    body_ratio: float = 1.0,
    close_position: float = 0.5,
    # ADX
    adx: float = 18.0,
    # Regime
    regime: str = "ranging",
) -> SignalContext:
    prev_range = prev_high - prev_low
    position = (
        (close_now - prev_low) / prev_range if prev_range > 0 else 0.5
    )
    position = max(0.0, min(1.0, position))
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="structured_prior_day_retest",
        indicators={
            "prior_day_levels": {
                "prev_day_high": prev_high,
                "prev_day_low": prev_low,
                "prev_day_close": prev_close,
                "prev_day_range": prev_range,
                "position_in_prev_range": round(position, 4),
                "distance_to_prev_high": round(prev_high - close_now, 4),
                "distance_to_prev_low": round(close_now - prev_low, 4),
            },
            "candle_pattern": {
                "pin_bar": pin_bar,
                "engulfing": engulfing,
                "hammer": hammer if shooting_star == 0.0 else -shooting_star,
                "rejection": rejection,
            },
            "bar_stats20": {
                "body_ratio": body_ratio,
                "close_position": close_position,
                "is_bullish": 1.0 if close_position > 0.5 else -1.0,
            },
            "atr14": {"atr": atr},
            "boll20": {"close": close_now},
            "adx14": {
                "adx": adx,
                "plus_di": 20.0,
                "minus_di": 15.0,
            },
        },
        metadata={"_regime": regime, "market_structure": {}},
        htf_indicators={},
    )


class TestPriorDayRetestBuy:
    def setup_method(self) -> None:
        self.strategy = StructuredPriorDayRetest()

    def test_buy_signal_at_prev_low_with_pin_bar(self) -> None:
        """收盘价接近昨日 low + 看涨 pin bar → buy。"""
        ctx = _make_context(
            close_now=2682.0,  # 接近 prev_low=2680（距离 2 < 0.5 ATR=2.5）
            pin_bar=1.0,
            close_position=0.7,
            atr=5.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        assert decision.confidence > 0.55

    def test_buy_signal_at_prev_low_with_hammer(self) -> None:
        ctx = _make_context(
            close_now=2682.0,
            hammer=1.0,
            close_position=0.65,
            atr=5.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"

    def test_buy_no_pattern_holds(self) -> None:
        """触及 prev low 但无反转 K 线 → hold。"""
        ctx = _make_context(close_now=2682.0, atr=5.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_buy_too_far_from_prev_low_holds(self) -> None:
        """收盘价距 prev_low 过远（> proximity_atr × atr）→ hold。"""
        ctx = _make_context(
            close_now=2700.0,  # 距 prev_low=2680 是 20，远超 0.5 ATR=2.5
            pin_bar=1.0,
            close_position=0.7,
            atr=5.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_buy_breakthrough_too_deep_holds(self) -> None:
        """收盘价已破 prev_low 太多（破位非反转）→ hold。"""
        ctx = _make_context(
            close_now=2675.0,  # 破 prev_low=2680 5 个点 = 1.0 ATR > 0.5 max_breakthrough
            pin_bar=1.0,
            close_position=0.7,
            atr=5.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_buy_high_adx_rejected(self) -> None:
        """高 ADX = 强趋势，mean-reversion 反转概率低 → hold。"""
        ctx = _make_context(
            close_now=2682.0,
            pin_bar=1.0,
            close_position=0.7,
            atr=5.0,
            adx=45.0,  # > _max_adx = 35
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"


class TestPriorDayRetestSell:
    def setup_method(self) -> None:
        self.strategy = StructuredPriorDayRetest()

    def test_sell_signal_at_prev_high_with_pin_bar(self) -> None:
        """收盘价接近昨日 high + 看跌 pin bar → sell。"""
        ctx = _make_context(
            close_now=2708.0,  # 接近 prev_high=2710（距离 2 < 0.5 ATR=2.5）
            pin_bar=-1.0,
            close_position=0.3,
            atr=5.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "sell"
        assert decision.confidence > 0.55

    def test_sell_with_shooting_star(self) -> None:
        ctx = _make_context(
            close_now=2708.0,
            shooting_star=1.0,
            close_position=0.3,
            atr=5.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "sell"

    def test_sell_breakthrough_too_deep_holds(self) -> None:
        """收盘价已破 prev_high 太多 → hold。"""
        ctx = _make_context(
            close_now=2715.0,
            pin_bar=-1.0,
            close_position=0.3,
            atr=5.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"


class TestPriorDayRetestExit:
    def setup_method(self) -> None:
        self.strategy = StructuredPriorDayRetest()

    def test_exit_spec_is_barrier_with_short_sl(self) -> None:
        """exit 配置：BARRIER 模式 + 紧 SL（retest 失败要快止损）。"""
        ctx = _make_context(
            close_now=2682.0, pin_bar=1.0, close_position=0.7, atr=5.0
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        spec = decision.metadata.get("exit_spec") or {}
        assert spec.get("mode") == "barrier"
        assert spec.get("sl_atr") is not None and spec["sl_atr"] <= 1.5
        assert spec.get("tp_atr") is not None and spec["tp_atr"] >= 1.5
        assert spec.get("time_bars") is not None and spec["time_bars"] >= 8


class TestPriorDayRetestMetadata:
    def setup_method(self) -> None:
        self.strategy = StructuredPriorDayRetest()

    def test_required_indicators_includes_new_indicator(self) -> None:
        assert "prior_day_levels" in self.strategy.required_indicators
        assert "candle_pattern" in self.strategy.required_indicators
        assert "atr14" in self.strategy.required_indicators

    def test_no_htf_dependency(self) -> None:
        assert self.strategy.htf_policy.value == "none"

    def test_regime_affinity_favors_ranging(self) -> None:
        from src.signals.evaluation.regime import RegimeType

        aff = self.strategy.regime_affinity
        # mean-reversion at SR 在震荡市最有效
        assert aff[RegimeType.RANGING] >= aff[RegimeType.TRENDING]
        assert aff[RegimeType.RANGING] >= aff[RegimeType.BREAKOUT]
