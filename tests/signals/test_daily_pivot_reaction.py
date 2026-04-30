"""StructuredDailyPivotReaction — 触及 pivot R1/R2/S1/S2 + 反转 → 反向入场。"""

from __future__ import annotations

from src.signals.models import SignalContext
from src.signals.strategies.structured.daily_pivot_reaction import (
    StructuredDailyPivotReaction,
)


def _make_context(
    *,
    pivot: float = 2700.0,
    r1: float = 2710.0,
    s1: float = 2690.0,
    r2: float = 2720.0,
    s2: float = 2680.0,
    close_now: float,
    atr: float = 5.0,
    pin_bar: float = 0.0,
    hammer: float = 0.0,
    rejection: float = 0.0,
    engulfing: float = 0.0,
    body_ratio: float = 1.0,
    close_position: float = 0.5,
    adx: float = 18.0,
    regime: str = "ranging",
) -> SignalContext:
    levels = {"pivot": pivot, "r1": r1, "s1": s1, "r2": r2, "s2": s2}
    distances = {n: close_now - p for n, p in levels.items()}
    nearest_name = min(distances, key=lambda k: abs(distances[k]))
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="structured_daily_pivot_reaction",
        indicators={
            "daily_pivots": {
                **levels,
                "distance_to_pivot": distances["pivot"],
                "distance_to_r1": distances["r1"],
                "distance_to_s1": distances["s1"],
                "nearest_level_name": nearest_name,
                "nearest_level_distance": abs(distances[nearest_name]),
            },
            "candle_pattern": {
                "pin_bar": pin_bar,
                "engulfing": engulfing,
                "hammer": hammer,
                "rejection": rejection,
            },
            "bar_stats20": {
                "body_ratio": body_ratio,
                "close_position": close_position,
                "is_bullish": 1.0 if close_position > 0.5 else -1.0,
            },
            "atr14": {"atr": atr},
            "boll20": {"close": close_now},
            "adx14": {"adx": adx, "plus_di": 20.0, "minus_di": 15.0},
        },
        metadata={"_regime": regime, "market_structure": {}},
        htf_indicators={},
    )


class TestDailyPivotBuy:
    def setup_method(self) -> None:
        self.strategy = StructuredDailyPivotReaction()

    def test_buy_at_s1_with_pin_bar(self) -> None:
        """close 接近 S1 + 看涨 pin bar → buy。"""
        ctx = _make_context(
            close_now=2691.0,  # 接近 s1=2690（距 1 < 0.3*5=1.5 ATR proximity）
            pin_bar=1.0,
            close_position=0.7,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"

    def test_buy_at_s2_with_hammer(self) -> None:
        ctx = _make_context(close_now=2681.0, hammer=1.0, close_position=0.7)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"


class TestDailyPivotSell:
    def setup_method(self) -> None:
        self.strategy = StructuredDailyPivotReaction()

    def test_sell_at_r1_with_pin_bar(self) -> None:
        ctx = _make_context(close_now=2709.0, pin_bar=-1.0, close_position=0.3)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "sell"

    def test_sell_at_r2_with_shooting_star(self) -> None:
        ctx = _make_context(close_now=2719.0, hammer=-1.0, close_position=0.3)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "sell"


class TestDailyPivotReject:
    def setup_method(self) -> None:
        self.strategy = StructuredDailyPivotReaction()

    def test_no_pattern_holds(self) -> None:
        ctx = _make_context(close_now=2691.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_at_pivot_skipped(self) -> None:
        """close 最近的 level 是 pivot 本身（中性 level，方向不明）→ hold。"""
        ctx = _make_context(close_now=2700.5, pin_bar=1.0, close_position=0.7)
        decision = self.strategy.evaluate(ctx)
        # nearest_level_name 应是 'pivot'，策略应跳过
        assert decision.direction == "hold"

    def test_too_far_from_level_holds(self) -> None:
        """距任何 level 都超过 proximity → hold。"""
        ctx = _make_context(close_now=2696.0, pin_bar=1.0, close_position=0.7)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_high_adx_rejected(self) -> None:
        ctx = _make_context(
            close_now=2691.0, pin_bar=1.0, close_position=0.7, adx=45.0
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"


class TestDailyPivotMetadata:
    def setup_method(self) -> None:
        self.strategy = StructuredDailyPivotReaction()

    def test_required_indicators(self) -> None:
        assert "daily_pivots" in self.strategy.required_indicators
        assert "candle_pattern" in self.strategy.required_indicators
        assert "atr14" in self.strategy.required_indicators

    def test_no_htf_dependency(self) -> None:
        assert self.strategy.htf_policy.value == "none"

    def test_regime_affinity_favors_ranging(self) -> None:
        from src.signals.evaluation.regime import RegimeType

        aff = self.strategy.regime_affinity
        assert aff[RegimeType.RANGING] >= aff[RegimeType.TRENDING]
        assert aff[RegimeType.RANGING] >= aff[RegimeType.BREAKOUT]

    def test_exit_spec_barrier_mode(self) -> None:
        ctx = _make_context(close_now=2691.0, pin_bar=1.0, close_position=0.7)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        spec = decision.metadata.get("exit_spec") or {}
        assert spec.get("mode") == "barrier"
