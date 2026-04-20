from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.readmodels.freshness import (
    FRESHNESS_STATE_FRESH,
    FRESHNESS_STATE_STALE,
    FRESHNESS_STATE_UNKNOWN,
    FRESHNESS_STATE_WARN,
    age_seconds,
    build_freshness_block,
    freshness_state,
    iso_or_none,
)


def test_iso_or_none_handles_datetime_str_none() -> None:
    dt = datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc)

    assert iso_or_none(dt) == "2026-04-20T10:00:00+00:00"
    assert iso_or_none("2026-04-20T10:00:00+00:00") == "2026-04-20T10:00:00+00:00"
    assert iso_or_none(None) is None


def test_age_seconds_uses_utc_for_naive_datetime() -> None:
    now = datetime.now(timezone.utc)
    five_sec_ago = now - timedelta(seconds=5)

    age = age_seconds(five_sec_ago)

    assert age is not None
    assert 4.5 <= age <= 6.0


def test_age_seconds_parses_iso_string() -> None:
    now = datetime.now(timezone.utc)
    ten_sec_ago = (now - timedelta(seconds=10)).isoformat()

    age = age_seconds(ten_sec_ago)

    assert age is not None
    assert 9.0 <= age <= 11.0


def test_age_seconds_returns_none_for_garbage() -> None:
    assert age_seconds(None) is None
    assert age_seconds("not-a-date") is None
    assert age_seconds(12345) is None


def test_freshness_state_tiers() -> None:
    assert (
        freshness_state(5.0, stale_after_seconds=10, max_age_seconds=60)
        == FRESHNESS_STATE_FRESH
    )
    assert (
        freshness_state(30.0, stale_after_seconds=10, max_age_seconds=60)
        == FRESHNESS_STATE_WARN
    )
    assert (
        freshness_state(120.0, stale_after_seconds=10, max_age_seconds=60)
        == FRESHNESS_STATE_STALE
    )
    assert (
        freshness_state(None, stale_after_seconds=10, max_age_seconds=60)
        == FRESHNESS_STATE_UNKNOWN
    )


def test_build_freshness_block_contract_keys() -> None:
    now = datetime.now(timezone.utc)
    block = build_freshness_block(
        observed_at=now.isoformat(),
        data_updated_at=now - timedelta(seconds=3),
        stale_after_seconds=10,
        max_age_seconds=60,
    )

    assert set(block.keys()) == {
        "observed_at",
        "data_updated_at",
        "age_seconds",
        "stale_after_seconds",
        "max_age_seconds",
        "freshness_state",
        "source_kind",
        "fallback_applied",
        "fallback_reason",
    }
    assert block["source_kind"] == "native"
    assert block["fallback_applied"] is False
    assert block["freshness_state"] == FRESHNESS_STATE_FRESH


def test_build_freshness_block_propagates_fallback_flags() -> None:
    block = build_freshness_block(
        observed_at="2026-04-20T10:00:00+00:00",
        data_updated_at=None,
        stale_after_seconds=10,
        max_age_seconds=60,
        source_kind="fallback",
        fallback_applied=True,
        fallback_reason="db_degraded",
    )

    assert block["source_kind"] == "fallback"
    assert block["fallback_applied"] is True
    assert block["fallback_reason"] == "db_degraded"
    assert block["freshness_state"] == FRESHNESS_STATE_UNKNOWN
    assert block["age_seconds"] is None
