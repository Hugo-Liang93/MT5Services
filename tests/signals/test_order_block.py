"""tests/signals/test_order_block.py — OrderBlockEntryStrategy 单元测试。"""
from __future__ import annotations

import pytest

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext
from src.signals.strategies.price_action import OrderBlockEntryStrategy


def _make_bar(
    open_: float, high: float, low: float, close: float
) -> dict[str, float]:
    return {"open": open_, "high": high, "low": low, "close": close}


def _context(
    bars: list[dict[str, float]],
    atr: float = 3.0,
    timeframe: str = "M15",
) -> SignalContext:
    return SignalContext(
        symbol="XAUUSD",
        timeframe=timeframe,
        strategy="order_block_entry",
        indicators={"atr14": {"atr": atr}},
        metadata={"recent_bars": bars},
    )


class TestOrderBlockDetection:
    def test_bullish_ob_retest_buy(self):
        """Bearish bar → bullish displacement → price retests OB zone → buy."""
        strategy = OrderBlockEntryStrategy()
        bars: list[dict[str, float]] = []
        # 填充历史（使足够长度）
        for i in range(30):
            bars.append(_make_bar(3000.0, 3001.0, 2999.0, 3000.0))

        # OB bar: 最后的 bearish bar（close < open）
        bars.append(_make_bar(3000.0, 3001.0, 2997.0, 2998.0))  # index 30: bearish OB

        # Bullish displacement: 连续 3 根 bullish bars, 合计 > 1.5*ATR=4.5
        bars.append(_make_bar(2998.0, 3001.0, 2997.5, 3000.5))  # +2.5
        bars.append(_make_bar(3000.5, 3003.0, 3000.0, 3002.5))  # +2.0
        bars.append(_make_bar(3002.5, 3005.0, 3002.0, 3004.5))  # +2.0  total=+6.5 > 4.5

        # 几根中性 bar
        bars.append(_make_bar(3004.5, 3005.0, 3003.0, 3004.0))
        bars.append(_make_bar(3004.0, 3004.5, 3002.0, 3002.5))

        # 当前 bar: 回踩到 OB 区间 [2997, 3001]
        bars.append(_make_bar(3002.0, 3002.5, 2998.0, 2999.0))

        decision = strategy.evaluate(_context(bars))
        assert decision.direction == "buy"
        assert decision.confidence >= 0.55
        assert "ob_retest" in decision.reason

    def test_bearish_ob_retest_sell(self):
        """Bullish bar → bearish displacement → price retests OB zone → sell."""
        strategy = OrderBlockEntryStrategy()
        bars: list[dict[str, float]] = []
        for i in range(30):
            bars.append(_make_bar(3000.0, 3001.0, 2999.0, 3000.0))

        # OB bar: 最后的 bullish bar
        bars.append(_make_bar(3000.0, 3003.0, 2999.5, 3002.0))  # bullish OB

        # Bearish displacement: 连续 3 根 bearish bars
        bars.append(_make_bar(3002.0, 3002.5, 2999.0, 2999.5))  # -2.5
        bars.append(_make_bar(2999.5, 3000.0, 2997.0, 2997.5))  # -2.0
        bars.append(_make_bar(2997.5, 2998.0, 2995.0, 2995.5))  # -2.0

        # 中性 bar
        bars.append(_make_bar(2995.5, 2997.0, 2995.0, 2996.0))
        bars.append(_make_bar(2996.0, 2999.0, 2995.5, 2998.5))

        # 当前 bar: 反弹到 OB 区间 [2999.5, 3003]
        bars.append(_make_bar(2999.0, 3002.0, 2998.5, 3001.0))

        decision = strategy.evaluate(_context(bars))
        assert decision.direction == "sell"
        assert decision.confidence >= 0.55

    def test_no_displacement_returns_hold(self):
        """No strong displacement → hold."""
        strategy = OrderBlockEntryStrategy()
        # All neutral bars with small body
        bars = [_make_bar(3000.0, 3001.0, 2999.0, 3000.2) for _ in range(40)]
        decision = strategy.evaluate(_context(bars))
        assert decision.direction == "hold"

    def test_missing_atr_returns_hold(self):
        strategy = OrderBlockEntryStrategy()
        ctx = SignalContext(
            symbol="XAUUSD",
            timeframe="M15",
            strategy="order_block_entry",
            indicators={},
            metadata={"recent_bars": [_make_bar(3000.0, 3001.0, 2999.0, 3000.0)] * 40},
        )
        decision = strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_insufficient_bars_returns_hold(self):
        strategy = OrderBlockEntryStrategy()
        bars = [_make_bar(3000.0, 3001.0, 2999.0, 3000.0)] * 5
        decision = strategy.evaluate(_context(bars))
        assert decision.direction == "hold"


class TestOrderBlockAttributes:
    def test_required_attributes(self):
        strategy = OrderBlockEntryStrategy()
        assert strategy.name == "order_block_entry"
        assert strategy.category == "price_action"
        assert "atr14" in strategy.required_indicators
        assert "confirmed" in strategy.preferred_scopes
        assert RegimeType.TRENDING in strategy.regime_affinity
        assert RegimeType.RANGING in strategy.regime_affinity
        assert RegimeType.BREAKOUT in strategy.regime_affinity
        assert RegimeType.UNCERTAIN in strategy.regime_affinity

    def test_confidence_capped(self):
        """Confidence should not exceed 0.90."""
        strategy = OrderBlockEntryStrategy()
        bars: list[dict[str, float]] = []
        for _ in range(30):
            bars.append(_make_bar(3000.0, 3001.0, 2999.0, 3000.0))

        # Strong OB + massive displacement
        bars.append(_make_bar(3000.0, 3001.0, 2997.0, 2998.0))
        for _ in range(5):
            bars.append(_make_bar(2998.0, 3010.0, 2997.0, 3008.0))  # huge bullish

        bars.append(_make_bar(3008.0, 3009.0, 2998.0, 2999.0))  # retest OB

        decision = strategy.evaluate(_context(bars, atr=2.0))
        assert decision.confidence <= 0.90
