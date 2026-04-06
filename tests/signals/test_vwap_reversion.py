"""tests/signals/test_vwap_reversion.py — VwapReversionStrategy 单元测试。"""
from __future__ import annotations

import pytest

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext
from src.signals.strategies.legacy.mean_reversion import VwapReversionStrategy


def _context(
    vwap: float,
    close: float,
    upper: float,
    lower: float,
    std_dev: float | None = None,
) -> SignalContext:
    data: dict[str, object] = {
        "vwap": vwap,
        "upper_band": upper,
        "lower_band": lower,
        "close": close,
    }
    if std_dev is not None:
        data["std_dev"] = std_dev
    return SignalContext(
        symbol="XAUUSD",
        timeframe="M15",
        strategy="vwap_reversion",
        indicators={"vwap30": data},
        metadata={},
    )


class TestVwapReversionSignals:
    def test_price_above_upper_band_sell(self):
        """Price above VWAP + 1.5σ → sell."""
        strategy = VwapReversionStrategy()
        ctx = _context(
            vwap=3000.0, close=3010.0, upper=3006.0, lower=2994.0, std_dev=4.0
        )
        # deviation = (3010-3000)/4 = 2.5 > 1.5 → sell
        decision = strategy.evaluate(ctx)
        assert decision.direction == "sell"
        assert decision.confidence > 0.50

    def test_price_below_lower_band_buy(self):
        """Price below VWAP - 1.5σ → buy."""
        strategy = VwapReversionStrategy()
        ctx = _context(
            vwap=3000.0, close=2990.0, upper=3006.0, lower=2994.0, std_dev=4.0
        )
        # deviation = (2990-3000)/4 = -2.5, abs > 1.5 → buy
        decision = strategy.evaluate(ctx)
        assert decision.direction == "buy"
        assert decision.confidence > 0.50

    def test_price_near_vwap_hold(self):
        """Price near VWAP → hold."""
        strategy = VwapReversionStrategy()
        ctx = _context(
            vwap=3000.0, close=3001.0, upper=3006.0, lower=2994.0, std_dev=4.0
        )
        # deviation = 0.25 < 1.5 → hold
        decision = strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_missing_vwap_data_hold(self):
        """No VWAP indicator data → hold."""
        strategy = VwapReversionStrategy()
        ctx = SignalContext(
            symbol="XAUUSD",
            timeframe="M15",
            strategy="vwap_reversion",
            indicators={},
            metadata={},
        )
        decision = strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_fallback_without_std_dev(self):
        """Without std_dev, uses band width to estimate deviation."""
        strategy = VwapReversionStrategy()
        # band_sigma=1.5, band_width=12, half_band=4 (=6/1.5)
        # deviation = (3010-3000)/4 = 2.5 > 1.5 → sell
        ctx = _context(vwap=3000.0, close=3010.0, upper=3006.0, lower=2994.0)
        decision = strategy.evaluate(ctx)
        assert decision.direction == "sell"

    def test_confidence_capped_at_088(self):
        """Confidence should not exceed 0.88."""
        strategy = VwapReversionStrategy()
        ctx = _context(
            vwap=3000.0, close=3030.0, upper=3006.0, lower=2994.0, std_dev=4.0
        )
        # deviation = 7.5, very far from VWAP
        decision = strategy.evaluate(ctx)
        assert decision.confidence <= 0.88


class TestVwapReversionAttributes:
    def test_required_attributes(self):
        strategy = VwapReversionStrategy()
        assert strategy.name == "vwap_reversion"
        assert strategy.category == "reversion"
        assert "vwap30" in strategy.required_indicators
        assert "intrabar" in strategy.preferred_scopes
        assert "confirmed" in strategy.preferred_scopes
        assert RegimeType.TRENDING in strategy.regime_affinity
        assert RegimeType.RANGING in strategy.regime_affinity
