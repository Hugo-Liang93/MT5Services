from __future__ import annotations

from src.calendar.policy import build_signal_economic_policy
from src.config import EconomicConfig


def test_signal_economic_policy_builds_decay_from_economic_config() -> None:
    policy = build_signal_economic_policy(
        EconomicConfig(
            enabled=True,
            pre_event_buffer_minutes=30,
            post_event_buffer_minutes=30,
            high_importance_threshold=3,
            release_watch_approaching_minutes=120,
            release_watch_post_event_minutes=15,
        )
    )

    assert policy.filter_window.lookahead_minutes == 30
    assert policy.filter_window.lookback_minutes == 30
    assert policy.filter_window.importance_min == 3
    assert policy.query_window.lookahead_minutes == 120
    assert policy.query_window.lookback_minutes == 30
    assert policy.decay_pre_minutes == 120
    assert policy.decay_post_minutes == 30


def test_signal_economic_policy_decay_has_soft_pre_event_ramp() -> None:
    policy = build_signal_economic_policy(
        EconomicConfig(
            enabled=True,
            pre_event_buffer_minutes=30,
            post_event_buffer_minutes=30,
            high_importance_threshold=3,
            release_watch_approaching_minutes=120,
            release_watch_post_event_minutes=15,
        )
    )

    assert policy.decay_for_delta(150) == 1.0
    assert policy.decay_for_delta(120) == 1.0
    assert policy.decay_for_delta(75) == 0.5
    assert policy.decay_for_delta(30) == 0.0
    assert policy.decay_for_delta(10) == 0.0
    assert policy.decay_for_delta(-10) == 0.0
    assert policy.decay_for_delta(-45) == 1.0
