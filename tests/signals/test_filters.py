from __future__ import annotations

from datetime import datetime, timezone

from src.calendar import EconomicDecayService
from src.calendar.policy import build_signal_economic_policy
from src.config import EconomicConfig
from src.signals.execution.filters import (
    EconomicEventFilter,
    SessionFilter,
    SessionTransitionFilter,
    SignalFilterChain,
    SpreadFilter,
    TrendExhaustionFilter,
    VolatilitySpikeFilter,
)


class _EconomicProviderStub:
    def __init__(self, events):
        self._events = list(events)
        self.calls = []

    def get_events(self, **kwargs):
        self.calls.append(kwargs)
        return list(self._events)


def test_session_filter_accepts_canonical_names() -> None:
    session_filter = SessionFilter(allowed_sessions=("london", "new_york"))
    assert session_filter.allowed_sessions == ("london", "new_york")


def test_session_filter_emits_new_york_session_name() -> None:
    f = SessionFilter()
    sessions = f.current_sessions(datetime(2026, 3, 19, 14, 0, tzinfo=timezone.utc))
    assert "new_york" in sessions


def test_session_filter_matches_contract_session_boundaries() -> None:
    f = SessionFilter()

    assert f.current_sessions(datetime(2026, 3, 19, 6, 59, tzinfo=timezone.utc)) == [
        "asia"
    ]
    assert f.current_sessions(datetime(2026, 3, 19, 7, 0, tzinfo=timezone.utc)) == [
        "london"
    ]
    assert f.current_sessions(datetime(2026, 3, 19, 13, 0, tzinfo=timezone.utc)) == [
        "new_york"
    ]


def test_spread_filter_uses_session_specific_limit() -> None:
    chain = SignalFilterChain(
        session_filter=SessionFilter(allowed_sessions=("asia", "london", "new_york")),
        spread_filter=SpreadFilter(
            max_spread_points=50.0,
            session_max_spread_points={"asia": 30.0, "london": 45.0},
        ),
    )

    allowed, reason = chain.should_evaluate(
        "XAUUSD",
        spread_points=35.0,
        utc_now=datetime(2026, 3, 19, 2, 0, tzinfo=timezone.utc),
    )

    assert allowed is False
    assert reason == "spread_too_wide:35.0>30.0[asia]"


def test_session_transition_filter_blocks_london_to_new_york_handoff() -> None:
    chain = SignalFilterChain(
        session_transition_filter=SessionTransitionFilter(cooldown_minutes=15),
    )

    allowed, reason = chain.should_evaluate(
        "XAUUSD",
        utc_now=datetime(2026, 3, 19, 13, 0, tzinfo=timezone.utc),
    )

    assert allowed is False
    assert reason == "session_transition_cooldown:london_to_new_york"


def test_volatility_spike_filter_blocks_when_atr_exceeds_baseline() -> None:
    chain = SignalFilterChain(
        volatility_filter=VolatilitySpikeFilter(spike_multiplier=2.5),
    )
    indicators = {"atr14": {"atr": 8.0, "atr_sma": 3.0}}

    allowed, reason = chain.should_evaluate(
        "XAUUSD",
        indicators=indicators,
    )

    assert allowed is False
    assert reason == "volatility_spike"


def test_volatility_spike_filter_allows_normal_atr() -> None:
    chain = SignalFilterChain(
        volatility_filter=VolatilitySpikeFilter(spike_multiplier=2.5),
    )
    indicators = {"atr14": {"atr": 5.0, "atr_sma": 3.0}}

    allowed, reason = chain.should_evaluate(
        "XAUUSD",
        indicators=indicators,
    )

    assert allowed is True
    assert reason == ""


def test_volatility_spike_filter_disabled_when_multiplier_is_zero() -> None:
    chain = SignalFilterChain(
        volatility_filter=VolatilitySpikeFilter(spike_multiplier=0.0),
    )
    indicators = {"atr14": {"atr": 100.0, "atr_sma": 3.0}}

    allowed, reason = chain.should_evaluate(
        "XAUUSD",
        indicators=indicators,
    )

    assert allowed is True


def test_trend_exhaustion_detects_falling_adx() -> None:
    f = TrendExhaustionFilter(high_mark=28.0, fall_rate=-2.5)
    detected, reason = f.detect(
        {
            "adx14": {"adx": 30.0, "adx_d3": -4.0},
            "rsi14": {"rsi": 50.0},
        }
    )
    assert detected is True
    assert "trend_exhaustion" in reason
    assert "rsi_neutral" in reason  # RSI 50 is in neutral zone


def test_trend_exhaustion_ignores_low_adx() -> None:
    f = TrendExhaustionFilter(high_mark=28.0, fall_rate=-2.5)
    detected, _ = f.detect(
        {
            "adx14": {"adx": 20.0, "adx_d3": -4.0},
        }
    )
    assert detected is False


def test_trend_exhaustion_ignores_rising_adx() -> None:
    f = TrendExhaustionFilter(high_mark=28.0, fall_rate=-2.5)
    detected, _ = f.detect(
        {
            "adx14": {"adx": 30.0, "adx_d3": 2.0},
        }
    )
    assert detected is False


def test_trend_exhaustion_filter_in_chain_warns_not_blocks() -> None:
    chain = SignalFilterChain(
        trend_exhaustion_filter=TrendExhaustionFilter(),
    )
    indicators = {
        "adx14": {"adx": 32.0, "adx_d3": -5.0},
        "rsi14": {"rsi": 48.0},
    }
    allowed, reason = chain.should_evaluate(
        "XAUUSD",
        indicators=indicators,
    )
    # Warn-only: allowed is True but reason is non-empty
    assert allowed is True
    assert "trend_exhaustion" in reason


def test_trend_exhaustion_filter_silent_when_no_exhaustion() -> None:
    chain = SignalFilterChain(
        trend_exhaustion_filter=TrendExhaustionFilter(),
    )
    indicators = {
        "adx14": {"adx": 25.0, "adx_d3": 3.0},
    }
    allowed, reason = chain.should_evaluate(
        "XAUUSD",
        indicators=indicators,
    )
    assert allowed is True
    assert reason == ""


def test_economic_event_filter_uses_economic_policy_window() -> None:
    provider = _EconomicProviderStub(events=[object()])
    policy = build_signal_economic_policy(
        EconomicConfig(
            enabled=True,
            pre_event_buffer_minutes=45,
            post_event_buffer_minutes=20,
            high_importance_threshold=3,
            release_watch_approaching_minutes=120,
            release_watch_post_event_minutes=15,
        )
    )
    chain = SignalFilterChain(
        economic_filter=EconomicEventFilter(
            service=EconomicDecayService(provider=provider, policy=policy),
        )
    )

    allowed, reason = chain.should_evaluate(
        "XAUUSD",
        utc_now=datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc),
    )

    assert allowed is False
    assert reason == "economic_event_block"
    call = provider.calls[0]
    assert call["importance_min"] == 3
    assert call["statuses"] == [
        "scheduled",
        "imminent",
        "pending_release",
        "released",
    ]
    assert call["start_time"] == datetime(2026, 4, 13, 7, 40, tzinfo=timezone.utc)
    assert call["end_time"] == datetime(2026, 4, 13, 8, 45, tzinfo=timezone.utc)
