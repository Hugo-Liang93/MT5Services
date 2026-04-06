"""multi_tf_entry.py 单元测试 — HTFTrendPullback 策略。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.signals.models import SignalContext, SignalDecision
from src.signals.strategies.legacy.multi_tf_entry import HTFTrendPullback


def _make_context(
    rsi: float = 50.0,
    atr: float = 5.0,
    h1_direction: float = 1.0,
    h1_adx: float = 28.0,
    h1_ema50: float = 4500.0,
    close: float = 4520.0,
    timeframe: str = "M5",
    htf_indicators: dict | None = None,
) -> SignalContext:
    indicators = {
        "rsi14": {"rsi": rsi, "close": close},
        "atr14": {"atr": atr},
    }
    if htf_indicators is None:
        htf_indicators = {
            "H1": {
                "supertrend14": {"direction": h1_direction},
                "adx14": {"adx": h1_adx},
                "ema50": {"ema": h1_ema50},
            }
        }
    return SignalContext(
        symbol="XAUUSD",
        timeframe=timeframe,
        strategy="htf_trend_pullback",
        indicators=indicators,
        htf_indicators=htf_indicators,
        metadata={"close": close},
    )


class TestHTFTrendPullback:
    def setup_method(self) -> None:
        self.strategy = HTFTrendPullback()

    def test_attributes(self) -> None:
        assert self.strategy.name == "htf_trend_pullback"
        assert self.strategy.category == "multi_tf"
        assert "rsi14" in self.strategy.required_indicators
        assert "atr14" in self.strategy.required_indicators

    def test_buy_on_h1_uptrend_rsi_pullback(self) -> None:
        """H1 趋势向上 + M5 RSI 回调到中性区 → buy。"""
        ctx = _make_context(
            rsi=45.0,  # pullback 区间 [38, 55]
            h1_direction=1.0,  # bullish
            h1_adx=28.0,
            close=4520.0,
            h1_ema50=4500.0,  # price > ema50
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        assert decision.confidence > 0.5

    def test_sell_on_h1_downtrend_rsi_pullback(self) -> None:
        """H1 趋势向下 + M5 RSI 回调到中性区 → sell。"""
        ctx = _make_context(
            rsi=55.0,  # pullback 区间 [45, 62]
            h1_direction=-1.0,  # bearish
            h1_adx=30.0,
            close=4480.0,
            h1_ema50=4500.0,  # price < ema50
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "sell"
        assert decision.confidence > 0.5

    def test_hold_when_no_htf_data(self) -> None:
        """无 HTF 数据 → hold。"""
        ctx = _make_context(htf_indicators={})
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_hold_when_adx_too_low(self) -> None:
        """H1 ADX 太低（趋势不够强）→ hold。"""
        ctx = _make_context(h1_adx=15.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_hold_when_rsi_not_in_pullback_zone(self) -> None:
        """RSI 不在回调区间 → hold（不追高/不追低）。"""
        # RSI=70 超出 buy pullback 区间 [38, 55]
        ctx = _make_context(rsi=70.0, h1_direction=1.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_hold_when_price_below_ema_in_uptrend(self) -> None:
        """H1 趋势向上但 price < H1 EMA50 → hold（不做逆均线交易）。"""
        ctx = _make_context(
            rsi=45.0,
            h1_direction=1.0,
            close=4480.0,  # below ema50=4500
            h1_ema50=4500.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_hold_when_price_above_ema_in_downtrend(self) -> None:
        """H1 趋势向下但 price > H1 EMA50 → hold。"""
        ctx = _make_context(
            rsi=55.0,
            h1_direction=-1.0,
            close=4520.0,  # above ema50=4500
            h1_ema50=4500.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_hold_when_atr_too_low(self) -> None:
        """ATR 极低（流动性不足）→ hold。"""
        ctx = _make_context(atr=0.1)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_confidence_increases_with_adx(self) -> None:
        """H1 ADX 越高 → 置信度越高。"""
        low_adx = _make_context(rsi=46.0, h1_adx=22.0, h1_direction=1.0)
        high_adx = _make_context(rsi=46.0, h1_adx=35.0, h1_direction=1.0)
        d_low = self.strategy.evaluate(low_adx)
        d_high = self.strategy.evaluate(high_adx)
        assert d_low.direction == "buy"
        assert d_high.direction == "buy"
        assert d_high.confidence > d_low.confidence

    def test_metadata_contains_htf_info(self) -> None:
        """决策 metadata 包含 HTF 方向和 ADX 信息。"""
        ctx = _make_context(rsi=45.0, h1_direction=1.0, h1_adx=28.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.metadata["htf_direction"] == 1
        assert decision.metadata["htf_adx"] == 28.0
        assert decision.metadata["htf"] == "H1"
