"""tests/signals/test_trendline_strategy.py — TrendlineThreeTouchStrategy 单元测试。"""
from __future__ import annotations

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext
from src.signals.strategies.legacy.trendline import (
    SwingPoint,
    TrendlineThreeTouchStrategy,
    _detect_swing_highs,
    _detect_swing_lows,
    _evaluate_trendline_touch,
    _find_best_trendline,
)


def _bar(open_: float, high: float, low: float, close: float) -> dict[str, float]:
    return {"open": open_, "high": high, "low": low, "close": close}


def _context(bars: list[dict], atr: float = 8.0, tf: str = "H1") -> SignalContext:
    return SignalContext(
        symbol="XAUUSD",
        timeframe=tf,
        strategy="trendline_3touch",
        indicators={"atr14": {"atr": atr}},
        metadata={"recent_bars": bars},
    )


class TestSwingDetection:
    def test_detect_swing_lows_basic(self):
        bars = [
            _bar(100, 105, 95, 102),
            _bar(102, 103, 90, 91),
            _bar(91, 92, 85, 87),
            _bar(87, 95, 86, 94),
            _bar(94, 100, 93, 99),
            _bar(99, 105, 98, 104),
        ]
        swings = _detect_swing_lows(bars, window=2, min_spacing=1)
        assert len(swings) >= 1
        assert swings[0].price == 85.0

    def test_detect_swing_highs_basic(self):
        bars = [
            _bar(100, 105, 95, 102),
            _bar(102, 110, 101, 108),
            _bar(108, 115, 107, 113),
            _bar(113, 112, 105, 106),
            _bar(106, 107, 100, 101),
            _bar(101, 103, 98, 100),
        ]
        swings = _detect_swing_highs(bars, window=2, min_spacing=1)
        assert len(swings) >= 1
        assert swings[0].price == 115.0

    def test_min_spacing_filters_close_swings(self):
        bars = [
            _bar(100, 102, 90, 91),
            _bar(91, 93, 85, 87),
            _bar(87, 95, 86, 94),
            _bar(94, 96, 84, 88),
            _bar(88, 100, 87, 99),
            _bar(99, 105, 98, 104),
        ]
        swings = _detect_swing_lows(bars, window=1, min_spacing=5)
        assert len(swings) == 1
        assert swings[0].price == 84.0


class TestTrendlineCandidateSearch:
    def test_best_ascending_trendline_prefers_more_covered_points(self):
        swings = [
            SwingPoint(index=0, price=100.0),
            SwingPoint(index=20, price=104.0),
            SwingPoint(index=40, price=108.0),
            SwingPoint(index=60, price=112.0),
            SwingPoint(index=75, price=130.0),
        ]
        candidate = _find_best_trendline(
            swings,
            atr=8.0,
            current_bar_index=79,
            direction="ascending",
            min_slope_atr=0.01,
            max_slope_atr=0.15,
            fit_tolerance_atr=0.5,
            min_covered_points=3,
        )
        assert candidate is not None
        assert candidate.direction == "ascending"
        assert len(candidate.covered_points) == 4
        assert 0.02 < candidate.normalized_slope < 0.03

    def test_best_trendline_rejects_flat_slope(self):
        flat = [
            SwingPoint(index=0, price=100.0),
            SwingPoint(index=20, price=100.01),
            SwingPoint(index=40, price=100.02),
        ]
        candidate = _find_best_trendline(
            flat,
            atr=8.0,
            current_bar_index=45,
            direction="ascending",
            min_slope_atr=0.01,
            max_slope_atr=0.15,
            fit_tolerance_atr=0.5,
        )
        assert candidate is None

    def test_best_trendline_rejects_steep_slope(self):
        steep = [
            SwingPoint(index=0, price=100.0),
            SwingPoint(index=5, price=110.0),
            SwingPoint(index=10, price=120.0),
        ]
        candidate = _find_best_trendline(
            steep,
            atr=8.0,
            current_bar_index=12,
            direction="ascending",
            min_slope_atr=0.01,
            max_slope_atr=0.15,
            fit_tolerance_atr=0.5,
        )
        assert candidate is None

    def test_best_trendline_rejects_support_line_with_break_violation(self):
        violating = [
            SwingPoint(index=0, price=100.0),
            SwingPoint(index=20, price=104.0),
            SwingPoint(index=30, price=96.0),
            SwingPoint(index=40, price=108.0),
            SwingPoint(index=60, price=112.0),
        ]
        candidate = _find_best_trendline(
            violating,
            atr=8.0,
            current_bar_index=65,
            direction="ascending",
            min_slope_atr=0.01,
            max_slope_atr=0.15,
            fit_tolerance_atr=0.5,
            min_covered_points=3,
            max_break_violations=0,
        )
        assert candidate is None


class TestTrendlineTouchRule:
    def test_buy_touch_accepts_light_touch_inside_band(self):
        touch = _evaluate_trendline_touch(
            direction="buy",
            trendline_price=3000.5,
            touch_price=3000.0,
            close_price=3005.0,
            tolerance=2.4,
        )
        assert touch.is_valid is True

    def test_buy_touch_rejects_deep_break_below_trendline(self):
        touch = _evaluate_trendline_touch(
            direction="buy",
            trendline_price=3000.5,
            touch_price=2994.0,
            close_price=3005.0,
            tolerance=2.4,
        )
        assert touch.is_valid is False

    def test_sell_touch_rejects_deep_break_above_trendline(self):
        touch = _evaluate_trendline_touch(
            direction="sell",
            trendline_price=3000.5,
            touch_price=3007.0,
            close_price=2998.0,
            tolerance=2.4,
        )
        assert touch.is_valid is False


class TestTrendlineStrategy:
    def _build_ascending_bars(self) -> list[dict]:
        bars: list[dict] = []
        for i in range(20):
            bars.append(_bar(3000.0 + i * 0.1, 3002.0, 2998.0 + i * 0.1, 3001.0 + i * 0.1))

        bars.extend(
            [
                _bar(2995.0, 2997.0, 2991.0, 2995.0),
                _bar(2994.0, 2996.0, 2990.5, 2993.0),
                _bar(2993.0, 2995.0, 2990.0, 2992.0),
                _bar(2992.0, 2998.0, 2991.0, 2997.0),
                _bar(2997.0, 3005.0, 2996.0, 3004.0),
                _bar(3004.0, 3008.0, 3003.0, 3007.0),
                _bar(3007.0, 3010.0, 3005.0, 3009.0),
                _bar(3009.0, 3010.0, 3000.0, 3001.0),
                _bar(3001.0, 3003.0, 2996.0, 2997.0),
                _bar(2997.0, 2999.0, 2995.0, 2996.0),
                _bar(2996.0, 2998.0, 2994.0, 2995.0),
                _bar(2995.0, 3002.0, 2994.5, 3001.0),
                _bar(3001.0, 3008.0, 3000.0, 3007.0),
                _bar(3007.0, 3012.0, 3006.0, 3011.0),
                _bar(3011.0, 3015.0, 3010.0, 3014.0),
                _bar(3014.0, 3015.0, 3005.0, 3006.0),
                _bar(3006.0, 3008.0, 3000.0, 3001.0),
                _bar(3001.0, 3003.0, 2998.0, 2999.0),
                _bar(2999.0, 3005.0, 2998.5, 3004.0),
                _bar(3004.0, 3010.0, 3003.0, 3009.0),
                _bar(3009.0, 3012.0, 3008.0, 3011.0),
                _bar(3011.0, 3014.0, 3010.0, 3013.0),
                _bar(3013.0, 3014.0, 3005.0, 3006.0),
                _bar(3005.0, 3007.0, 3000.0, 3005.0),
            ]
        )
        return bars

    def _build_descending_bars(self) -> list[dict]:
        pivot = 6000.0
        mirrored: list[dict] = []
        for bar in self._build_ascending_bars():
            mirrored.append(
                _bar(
                    pivot - bar["open"],
                    pivot - bar["low"],
                    pivot - bar["high"],
                    pivot - bar["close"],
                )
            )
        return mirrored

    def test_ascending_trendline_buy(self):
        strategy = TrendlineThreeTouchStrategy(lookback_bars=50)
        decision = strategy.evaluate(_context(self._build_ascending_bars()))
        assert decision.direction == "buy"
        assert decision.confidence >= 0.50
        assert decision.metadata["covered_point_count"] >= 3
        assert len(decision.metadata["covered_points"]) == decision.metadata["covered_point_count"]

    def test_descending_trendline_sell(self):
        strategy = TrendlineThreeTouchStrategy(lookback_bars=50)
        decision = strategy.evaluate(_context(self._build_descending_bars()))
        assert decision.direction == "sell"
        assert decision.confidence >= 0.50
        assert decision.metadata["covered_point_count"] >= 3

    def test_hold_when_touch_bar_deeply_breaks_below_trendline(self):
        strategy = TrendlineThreeTouchStrategy(lookback_bars=50)
        bars = self._build_ascending_bars()
        bars[-1] = _bar(3005.0, 3007.0, 2994.0, 3005.0)
        decision = strategy.evaluate(_context(bars))
        assert decision.direction == "hold"

    def test_hold_when_insufficient_bars(self):
        strategy = TrendlineThreeTouchStrategy()
        decision = strategy.evaluate(_context([_bar(3000, 3005, 2995, 3002)] * 5))
        assert decision.direction == "hold"

    def test_hold_when_no_atr(self):
        strategy = TrendlineThreeTouchStrategy()
        ctx = SignalContext(
            symbol="XAUUSD",
            timeframe="H1",
            strategy="trendline_3touch",
            indicators={},
            metadata={"recent_bars": [_bar(3000, 3005, 2995, 3002)] * 30},
        )
        assert strategy.evaluate(ctx).direction == "hold"

    def test_hold_in_flat_market(self):
        strategy = TrendlineThreeTouchStrategy(lookback_bars=30)
        bars = [_bar(3000.0, 3002.0, 2998.0, 3001.0) for _ in range(30)]
        decision = strategy.evaluate(_context(bars))
        assert decision.direction == "hold"

    def test_confidence_capped_at_090(self):
        strategy = TrendlineThreeTouchStrategy()
        fake = type(
            "FakeCandidate",
            (),
            {
                "covered_points": (
                    SwingPoint(index=0, price=100.0),
                    SwingPoint(index=20, price=104.0),
                    SwingPoint(index=40, price=108.0),
                    SwingPoint(index=60, price=112.0),
                    SwingPoint(index=80, price=116.0),
                    SwingPoint(index=100, price=120.0),
                ),
                "normalized_slope": 0.06,
                "mean_error_atr": 0.0,
                "max_error_atr": 0.0,
            },
        )()
        conf = strategy._compute_confidence(fake, touch_distance=0.0, tolerance=2.4)
        assert conf <= 0.90


class TestTrendlineAttributes:
    def test_required_attributes(self):
        s = TrendlineThreeTouchStrategy()
        assert s.name == "trendline_3touch"
        assert s.category == "trend"
        assert "atr14" in s.required_indicators
        assert "confirmed" in s.preferred_scopes
        assert RegimeType.TRENDING in s.regime_affinity
        assert s.recent_bars_depth == 80
