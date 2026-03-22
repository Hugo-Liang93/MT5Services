from __future__ import annotations

from datetime import datetime, timezone

from src.signals.execution.filters import (
    SessionFilter,
    SessionTransitionFilter,
    SignalFilterChain,
    SpreadFilter,
    VolatilitySpikeFilter,
)


def test_session_filter_normalizes_newyork_alias() -> None:
    session_filter = SessionFilter(allowed_sessions=("london", "newyork"))
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
        session_filter=SessionFilter(allowed_sessions=("asia", "london", "newyork")),
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
