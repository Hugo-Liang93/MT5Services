"""StructuredNYReversal — 美盘开盘第一两根 H1 + 反转 K 线 → 反向交易。

alpha 来源：
    - NY 开盘后 30-60min 的 session-transition reversal 是 day-trading
      经典 edge：欧盘趋势在 NY 进入时常因流动性回归而反转
    - 不需要新 indicator —— 用现有 ema21/ema55 cross + bar_time.hour + sessions
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalContext
from src.signals.strategies.structured.ny_reversal import StructuredNYReversal


def _make_context(
    *,
    hour_utc: int = 13,
    sessions: list[str] | None = None,
    ema21: float = 2700.0,
    ema55: float = 2700.0,
    pin_bar: float = 0.0,
    hammer: float = 0.0,
    engulfing: float = 0.0,
    rejection: float = 0.0,
    body_ratio: float = 1.0,
    close_position: float = 0.5,
    atr: float = 5.0,
    adx: float = 18.0,
    regime: str = "trending",
) -> SignalContext:
    bt = datetime(2026, 4, 15, hour_utc, 0, tzinfo=timezone.utc)
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="structured_ny_reversal",
        indicators={
            "ema21": {"ema": ema21},
            "ema55": {"ema": ema55},
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
            "adx14": {"adx": adx, "plus_di": 20.0, "minus_di": 15.0},
            "boll20": {"close": ema21},
        },
        metadata={
            MK.BAR_TIME: bt.isoformat(),
            MK.SESSION_BUCKETS: sessions if sessions is not None else ["new_york"],
            "_regime": regime,
            "market_structure": {},
        },
        htf_indicators={},
    )


class TestNYReversalBuy:
    def setup_method(self) -> None:
        self.strategy = StructuredNYReversal()

    def test_buy_after_bearish_london_with_pin_bull(self) -> None:
        """伦盘下跌 (ema21<ema55) + NY 第一根 + 看涨 pin → buy（反转）。"""
        ctx = _make_context(
            hour_utc=13,
            ema21=2695.0,
            ema55=2705.0,  # 下跌结构
            pin_bar=1.0,
            close_position=0.7,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"

    def test_buy_with_hammer(self) -> None:
        ctx = _make_context(
            hour_utc=14, ema21=2695.0, ema55=2705.0, hammer=1.0, close_position=0.7
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"


class TestNYReversalSell:
    def setup_method(self) -> None:
        self.strategy = StructuredNYReversal()

    def test_sell_after_bullish_london_with_pin_bear(self) -> None:
        """伦盘上涨 (ema21>ema55) + NY 第一根 + 看跌 pin → sell（反转）。"""
        ctx = _make_context(
            hour_utc=13,
            ema21=2710.0,
            ema55=2700.0,  # 上涨结构
            pin_bar=-1.0,
            close_position=0.3,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "sell"


class TestNYReversalReject:
    def setup_method(self) -> None:
        self.strategy = StructuredNYReversal()

    def test_outside_ny_open_window_holds(self) -> None:
        """非 NY 开盘前两小时（hour ∉ {13, 14}）→ hold。"""
        ctx = _make_context(
            hour_utc=10,  # 伦盘
            ema21=2695.0,
            ema55=2705.0,
            pin_bar=1.0,
            sessions=["london"],
            close_position=0.7,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_no_new_york_in_sessions_holds(self) -> None:
        """hour=13 但 SESSION_BUCKETS 不含 new_york（异常情况）→ hold。"""
        ctx = _make_context(
            hour_utc=13,
            sessions=["london"],
            ema21=2695.0,
            ema55=2705.0,
            pin_bar=1.0,
            close_position=0.7,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_no_pattern_holds(self) -> None:
        """方向条件成立但无反转 K 线 → hold。"""
        ctx = _make_context(hour_utc=13, ema21=2695.0, ema55=2705.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_pattern_and_trend_misaligned_holds(self) -> None:
        """伦盘下跌 + 看跌 pin → 不应该 sell（这不是反转，是延续）→ hold。"""
        ctx = _make_context(
            hour_utc=13,
            ema21=2695.0,
            ema55=2705.0,  # 下跌
            pin_bar=-1.0,  # 看跌
            close_position=0.3,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_high_adx_rejected(self) -> None:
        """高 ADX 强趋势 → 不进反转单。"""
        ctx = _make_context(
            hour_utc=13,
            ema21=2695.0,
            ema55=2705.0,
            pin_bar=1.0,
            close_position=0.7,
            adx=45.0,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_flat_ema_cross_holds(self) -> None:
        """ema21 ≈ ema55（横盘无方向）→ 没有 setup → hold。"""
        ctx = _make_context(
            hour_utc=13,
            ema21=2700.0,
            ema55=2700.05,  # 几乎相等
            pin_bar=1.0,
            close_position=0.7,
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"


class TestNYReversalMetadata:
    def setup_method(self) -> None:
        self.strategy = StructuredNYReversal()

    def test_required_indicators(self) -> None:
        for ind in ("ema21", "ema55", "candle_pattern", "atr14", "adx14"):
            assert ind in self.strategy.required_indicators

    def test_no_htf_dependency(self) -> None:
        assert self.strategy.htf_policy.value == "none"

    def test_exit_spec_barrier_mode(self) -> None:
        ctx = _make_context(
            hour_utc=13, ema21=2695.0, ema55=2705.0, pin_bar=1.0, close_position=0.7
        )
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "buy"
        spec = decision.metadata.get("exit_spec") or {}
        assert spec.get("mode") == "barrier"
