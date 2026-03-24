from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext, SignalDecision
from src.signals.service import SignalModule
from src.signals.strategies.breakout import (
    FakeBreakoutDetector,
    MultiTimeframeConfirmStrategy,
    SqueezeReleaseFollow,
)
from src.signals.strategies.price_action import PriceActionReversal
from src.signals.strategies.session import SessionMomentumBias


def test_fake_breakout_detector_emits_sell_on_failed_upper_breakout() -> None:
    strategy = FakeBreakoutDetector()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "donchian20": {"donchian_upper": 3000.0, "donchian_lower": 2988.0},
            "atr14": {"atr": 4.0},
        },
        metadata={
            "recent_bars": [
                {"open": 2999.0, "high": 3004.0, "low": 2996.0, "close": 2997.5}
            ],
            "market_structure": {
                "reclaim_state": "bearish_reclaim_previous_day_high",
            },
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "sell"
    assert decision.confidence > 0.7
    assert decision.metadata["reclaim_state"] == "bearish_reclaim_previous_day_high"


def test_fake_breakout_detector_emits_buy_on_failed_lower_breakout() -> None:
    strategy = FakeBreakoutDetector()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "donchian20": {"donchian_upper": 3004.0, "donchian_lower": 2990.0},
            "atr14": {"atr": 4.0},
        },
        metadata={
            "recent_bars": [
                {"open": 2992.0, "high": 2996.0, "low": 2987.0, "close": 2993.5}
            ],
            "market_structure": {
                "reclaim_state": "bullish_reclaim_previous_day_low",
            },
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "buy"
    assert decision.confidence > 0.7
    assert decision.metadata["reclaim_state"] == "bullish_reclaim_previous_day_low"


def test_fake_breakout_detector_holds_without_rejection_pattern() -> None:
    strategy = FakeBreakoutDetector()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "donchian20": {"donchian_upper": 3004.0, "donchian_lower": 2990.0},
            "atr14": {"atr": 4.0},
        },
        metadata={
            "recent_bars": [
                {"open": 2998.0, "high": 3001.0, "low": 2995.0, "close": 2999.0}
            ]
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "hold"
    assert decision.reason == "no_fake_breakout"


def test_squeeze_release_follow_emits_buy_on_upside_release() -> None:
    strategy = SqueezeReleaseFollow()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M15",
        strategy=strategy.name,
        indicators={
            "boll20": {"bb_upper": 3012.0, "bb_lower": 2998.0, "bb_mid": 3005.0},
            "keltner20": {"kc_upper": 3010.0, "kc_lower": 3000.0},
            "macd": {"hist": 0.85},
        },
        metadata={
            "market_structure": {
                "compression_state": "contracted",
                "structure_bias": "expansion",
            }
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "buy"
    assert decision.confidence > 0.75
    assert decision.metadata["compression_state"] == "contracted"


def test_squeeze_release_follow_emits_sell_on_downside_release() -> None:
    strategy = SqueezeReleaseFollow()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M15",
        strategy=strategy.name,
        indicators={
            "boll20": {"bb_upper": 3010.0, "bb_lower": 2996.0, "bb_mid": 3003.0},
            "keltner20": {"kc_upper": 3009.0, "kc_lower": 2998.0},
            "macd": {"hist": -0.72},
        },
        metadata={
            "market_structure": {
                "compression_state": "contracted",
                "structure_bias": "expansion",
            }
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "sell"
    assert decision.confidence > 0.75


def test_squeeze_release_follow_holds_without_release() -> None:
    strategy = SqueezeReleaseFollow()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M15",
        strategy=strategy.name,
        indicators={
            "boll20": {"bb_upper": 3010.0, "bb_lower": 2998.0, "bb_mid": 3004.0},
            "keltner20": {"kc_upper": 3011.0, "kc_lower": 2997.0},
            "macd": {"hist": 0.05},
        },
        metadata={"market_structure": {"compression_state": "normal"}},
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "hold"
    assert decision.reason == "no_squeeze_release"


def test_session_momentum_bias_suppresses_asia_session() -> None:
    strategy = SessionMomentumBias()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "atr14": {"atr": 1.8},
            "supertrend14": {"direction": 1},
        },
        metadata={
            "session_buckets": ["asia"],
            "close_price": 3000.0,
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "hold"
    assert decision.reason.startswith("asia_session_low_momentum")


def test_session_momentum_bias_emits_buy_when_london_trend_aligns() -> None:
    strategy = SessionMomentumBias()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "atr14": {"atr": 1.8},
            "supertrend14": {"direction": 1},
        },
        metadata={
            "session_buckets": ["london"],
            "close_price": 3000.0,
            "market_structure": {
                "breakout_state": "above_previous_day_high",
                "structure_bias": "bullish_breakout",
            },
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "buy"
    assert decision.confidence > 0.65


def test_session_momentum_bias_emits_sell_when_new_york_trend_aligns() -> None:
    strategy = SessionMomentumBias()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "atr14": {"atr": 1.5},
            "supertrend14": {"direction": -1},
        },
        metadata={
            "session_buckets": ["new_york"],
            "close_price": 3000.0,
            "market_structure": {
                "breakout_state": "below_previous_day_low",
                "structure_bias": "bearish_breakout",
            },
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "sell"
    assert decision.confidence > 0.65


def test_session_momentum_bias_holds_when_atr_is_too_low() -> None:
    strategy = SessionMomentumBias()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "atr14": {"atr": 0.3},
            "supertrend14": {"direction": 1},
        },
        metadata={
            "session_buckets": ["london"],
            "close_price": 3000.0,
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "hold"
    assert decision.reason.startswith("atr_too_low_for_session")


def test_asian_range_breakout_holds_when_atr_is_zero() -> None:
    from src.signals.strategies.session import AsianRangeBreakout

    strategy = AsianRangeBreakout()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "atr14": {"atr": 0.0},
        },
        metadata={
            "session_buckets": ["london"],
            "market_structure": {
                "asia_range_high": 3010.0,
                "asia_range_low": 2990.0,
            },
            "close_price": 3015.0,
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "hold"
    assert "missing_atr" in decision.reason


def test_price_action_reversal_detects_bearish_engulfing() -> None:
    strategy = PriceActionReversal()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M15",
        strategy=strategy.name,
        indicators={"atr14": {"atr": 4.0}},
        metadata={
            "recent_bars": [
                {"open": 3003.0, "high": 3006.0, "low": 2998.0, "close": 3005.0},
                {"open": 3006.0, "high": 3007.0, "low": 2997.0, "close": 2999.0},
            ]
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "sell"
    assert decision.reason == "bearish_engulfing"


def test_price_action_reversal_detects_bullish_engulfing() -> None:
    strategy = PriceActionReversal()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M15",
        strategy=strategy.name,
        indicators={"atr14": {"atr": 4.0}},
        metadata={
            "recent_bars": [
                {"open": 3005.0, "high": 3006.0, "low": 2998.0, "close": 3000.0},
                {"open": 2999.0, "high": 3007.0, "low": 2997.0, "close": 3006.0},
            ]
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "buy"
    assert decision.reason == "bullish_engulfing"


def test_price_action_reversal_holds_without_pattern() -> None:
    strategy = PriceActionReversal()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M15",
        strategy=strategy.name,
        indicators={"atr14": {"atr": 4.0}},
        metadata={
            "recent_bars": [
                {"open": 3000.0, "high": 3002.0, "low": 2999.0, "close": 3001.0},
                {"open": 3001.0, "high": 3003.0, "low": 3000.0, "close": 3002.0},
            ]
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "hold"
    assert decision.reason == "no_price_action_pattern"


def test_phase2_strategies_define_all_regime_affinities() -> None:
    strategies = [
        FakeBreakoutDetector(),
        SqueezeReleaseFollow(),
        SessionMomentumBias(),
        PriceActionReversal(),
    ]

    for strategy in strategies:
        assert set(strategy.regime_affinity) == set(RegimeType)


def test_multi_timeframe_confirm_uses_phase1_name() -> None:
    assert MultiTimeframeConfirmStrategy.name == "multi_timeframe_confirm"


class AttrValidationStrategy:
    name = "attr_validation"
    required_indicators = ("atr14",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {regime: 1.0 for regime in RegimeType}

    def evaluate(self, context: SignalContext) -> SignalDecision:
        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.0,
            reason="noop",
        )


@pytest.mark.parametrize(
    ("strategy", "indicators", "metadata", "expected_action"),
    [
        (
            FakeBreakoutDetector(),
            {
                "donchian20": {"donchian_upper": 3004.0, "donchian_lower": 2990.0},
                "atr14": {"atr": 4.0},
            },
            {
                "recent_bars": [
                    {"open": 2992.0, "high": 2996.0, "low": 2987.0, "close": 2993.5}
                ]
            },
            "buy",
        ),
        (
            SqueezeReleaseFollow(),
            {
                "boll20": {"bb_upper": 3012.0, "bb_lower": 2998.0, "bb_mid": 3005.0},
                "keltner20": {"kc_upper": 3010.0, "kc_lower": 3000.0},
                "macd": {"hist": 0.85},
            },
            {},
            "buy",
        ),
        (
            SessionMomentumBias(),
            {
                "atr14": {"atr": 1.8},
                "supertrend14": {"direction": 1},
            },
            {"session_buckets": ["london"], "close_price": 3000.0},
            "buy",
        ),
        (
            PriceActionReversal(),
            {"atr14": {"atr": 4.0}},
            {
                "recent_bars": [
                    {"open": 3005.0, "high": 3006.0, "low": 2998.0, "close": 3000.0},
                    {"open": 2999.0, "high": 3007.0, "low": 2997.0, "close": 3006.0},
                ]
            },
            "buy",
        ),
    ],
)
@pytest.mark.parametrize("regime", list(RegimeType))
def test_phase2_strategies_apply_expected_regime_affinity(
    strategy,
    indicators,
    metadata,
    expected_action,
    regime: RegimeType,
) -> None:
    module = SignalModule(
        indicator_source=Phase2IndicatorSource(),
        strategies=[strategy],
    )
    raw = strategy.evaluate(
        SignalContext(
            symbol="XAUUSD",
            timeframe="M5",
            strategy=strategy.name,
            indicators=indicators,
            metadata=metadata,
        )
    )

    decision = module.evaluate(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators=indicators,
        metadata={**metadata, "_regime": regime.value},
        persist=False,
    )

    assert raw.direction == expected_action
    assert decision.direction == expected_action
    expected_conf = raw.confidence * strategy.regime_affinity[regime]
    # 多层压制底线保护：service.py 中 _MIN_CALIBRATED_FLOOR = 0.10
    expected_conf = max(expected_conf, 0.10) if raw.confidence > 0 else expected_conf
    assert decision.confidence == pytest.approx(expected_conf)


class Phase2IndicatorSource:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_indicator(self, symbol: str, timeframe: str, indicator_name: str):
        return None

    def get_all_indicators(self, symbol: str, timeframe: str):
        return {}

    def list_indicators(self):
        return []

    def get_recent_bars(
        self,
        symbol: str,
        timeframe: str,
        *,
        end_time: datetime | None = None,
        limit: int = 5,
    ):
        self.calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "end_time": end_time,
                "limit": limit,
            }
        )
        return [
            {"open": 2999.0, "high": 3004.0, "low": 2996.0, "close": 2997.5},
        ]


def test_signal_module_registers_phase2_strategies() -> None:
    module = SignalModule(indicator_source=Phase2IndicatorSource())

    strategies = set(module.list_strategies())

    assert {"fake_breakout", "squeeze_release", "session_momentum", "price_action_reversal"} <= strategies


def test_signal_module_validate_strategy_attrs_accepts_phase2_contract() -> None:
    module = SignalModule(indicator_source=Phase2IndicatorSource(), strategies=[])

    module.register_strategy(AttrValidationStrategy())

    assert "attr_validation" in module.list_strategies()


def test_signal_module_injects_recent_bars_for_phase2_strategies() -> None:
    source = Phase2IndicatorSource()
    module = SignalModule(indicator_source=source)

    decision = module.evaluate(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="fake_breakout",
        indicators={
            "donchian20": {"donchian_upper": 3000.0, "donchian_lower": 2988.0},
            "atr14": {"atr": 4.0},
        },
        metadata={"bar_time": "2026-03-19T10:00:00+00:00"},
        persist=False,
    )

    assert decision.direction == "sell"
    assert len(source.calls) == 1
    assert source.calls[0]["limit"] == 5
    assert source.calls[0]["end_time"] == datetime(
        2026, 3, 19, 10, 0, tzinfo=timezone.utc
    )


def test_signal_module_injects_enough_recent_bars_for_fib_pullback() -> None:
    source = Phase2IndicatorSource()
    module = SignalModule(indicator_source=source)

    module.evaluate(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="fib_pullback",
        indicators={
            "supertrend14": {"direction": 1.0},
            "atr14": {"atr": 4.0},
        },
        metadata={"bar_time": "2026-03-19T10:00:00+00:00"},
        persist=False,
    )

    assert len(source.calls) == 1
    assert source.calls[0]["limit"] == 20


def test_signal_module_injects_enough_recent_bars_for_rsi_divergence() -> None:
    source = Phase2IndicatorSource()
    module = SignalModule(indicator_source=source)

    module.evaluate(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="rsi_divergence",
        indicators={
            "rsi14": {"rsi": 35.0},
        },
        metadata={"bar_time": "2026-03-19T10:00:00+00:00"},
        persist=False,
    )

    assert len(source.calls) == 1
    assert source.calls[0]["limit"] == 14
