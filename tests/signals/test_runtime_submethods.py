"""Tests for extracted sub-methods of SignalRuntime.process_next_event.

Covers: _dequeue_event, _is_stale_intrabar, _apply_filter_chain, _detect_regime.
"""
from __future__ import annotations

import queue
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.signals.orchestration.runtime import SignalRuntime
from src.signals.evaluation.regime import MarketRegimeDetector, RegimeType


def _make_runtime(**overrides) -> SignalRuntime:
    """Create a minimal SignalRuntime with stubs."""
    service = MagicMock()
    service.list_strategies.return_value = []
    source = MagicMock()
    defaults = dict(
        service=service,
        snapshot_source=source,
        targets=[],
    )
    defaults.update(overrides)
    return SignalRuntime(**defaults)


# ── _dequeue_event ────────────────────────────────────────────────


def test_dequeue_event_prefers_confirmed() -> None:
    rt = _make_runtime()
    confirmed_event = ("confirmed", "XAUUSD", "M5", {}, {})
    intrabar_event = ("intrabar", "XAUUSD", "M5", {}, {})
    rt._confirmed_events.put(confirmed_event)
    rt._intrabar_events.put(intrabar_event)

    event = rt._dequeue_event(timeout=0.01)
    assert event[0] == "confirmed"


def test_dequeue_event_falls_back_to_intrabar() -> None:
    rt = _make_runtime()
    intrabar_event = ("intrabar", "XAUUSD", "M5", {}, {})
    rt._intrabar_events.put(intrabar_event)

    event = rt._dequeue_event(timeout=0.1)
    assert event[0] == "intrabar"


def test_dequeue_event_returns_none_on_empty() -> None:
    rt = _make_runtime()
    event = rt._dequeue_event(timeout=0.01)
    assert event is None


def test_dequeue_event_anti_starvation() -> None:
    """After _CONFIRMED_BURST_LIMIT consecutive confirmed events,
    should yield to an intrabar event if available."""
    rt = _make_runtime()
    rt._CONFIRMED_BURST_LIMIT = 2

    # Fill both queues
    for i in range(5):
        rt._confirmed_events.put(("confirmed", "XAUUSD", "M5", {}, {"id": i}))
    rt._intrabar_events.put(("intrabar", "XAUUSD", "M5", {}, {"id": "ib"}))

    # First 2 should be confirmed (burst count hits limit on 2nd)
    e1 = rt._dequeue_event(timeout=0.01)
    assert e1[0] == "confirmed"
    e2 = rt._dequeue_event(timeout=0.01)
    # After 2 consecutive confirmed, anti-starvation should yield intrabar
    assert e2[0] == "intrabar"


# ── _is_stale_intrabar ───────────────────────────────────────────


def test_stale_intrabar_detected() -> None:
    rt = _make_runtime()
    old_time = time.monotonic() - 400  # 400s ago, > 300s threshold
    metadata = {"_enqueued_at": old_time}

    assert rt._is_stale_intrabar("intrabar", "XAUUSD", "M5", metadata) is True


def test_fresh_intrabar_not_stale() -> None:
    rt = _make_runtime()
    metadata = {"_enqueued_at": time.monotonic()}

    assert rt._is_stale_intrabar("intrabar", "XAUUSD", "M5", metadata) is False


def test_confirmed_events_never_stale() -> None:
    rt = _make_runtime()
    old_time = time.monotonic() - 999
    metadata = {"_enqueued_at": old_time}

    assert rt._is_stale_intrabar("confirmed", "XAUUSD", "M5", metadata) is False


def test_missing_enqueued_at_not_stale() -> None:
    rt = _make_runtime()
    assert rt._is_stale_intrabar("intrabar", "XAUUSD", "M5", {}) is False


# ── _detect_regime ────────────────────────────────────────────────


def test_detect_regime_hard_classification() -> None:
    rt = _make_runtime()
    rt._soft_regime_enabled = False
    rt._regime_detector = MagicMock()
    rt._regime_detector.detect.return_value = RegimeType.TRENDING
    indicators = {"adx14": {"adx": 30.0}, "boll20": {"bb_width": 0.02}}

    regime, metadata = rt._detect_regime(indicators, {}, ["london"])

    assert regime == RegimeType.TRENDING
    assert metadata["_regime"] == "trending"
    assert metadata["session_buckets"] == ["london"]


def test_detect_regime_soft_classification() -> None:
    rt = _make_runtime()
    rt._soft_regime_enabled = True
    soft_result = MagicMock()
    soft_result.dominant_regime = RegimeType.BREAKOUT
    soft_result.to_dict.return_value = {"trending": 0.2, "breakout": 0.6, "ranging": 0.1, "uncertain": 0.1}
    soft_result.probability.side_effect = lambda r: {"TRENDING": 0.2, "BREAKOUT": 0.6, "RANGING": 0.1, "UNCERTAIN": 0.1}.get(r.value.upper(), 0.0)
    rt._regime_detector = MagicMock()
    rt._regime_detector.detect_soft.return_value = soft_result
    indicators = {"adx14": {"adx": 25.0}}

    regime, metadata = rt._detect_regime(indicators, {}, [])

    assert regime == RegimeType.BREAKOUT
    assert "_soft_regime" in metadata
    assert "regime_probabilities" in metadata


def test_detect_regime_injects_close_price() -> None:
    rt = _make_runtime()
    rt._soft_regime_enabled = False
    rt._regime_detector = MagicMock()
    rt._regime_detector.detect.return_value = RegimeType.UNCERTAIN
    indicators = {"sma20": {"close": 3050.0}}

    _, metadata = rt._detect_regime(indicators, {}, [])

    assert "close_price" in metadata


# ── _apply_filter_chain ──────────────────────────────────────────


def test_apply_filter_chain_none_allows() -> None:
    rt = _make_runtime()
    rt.filter_chain = None

    allowed = rt._apply_filter_chain(
        "XAUUSD", "confirmed", "M5",
        datetime.now(timezone.utc), {}, [], {},
    )
    assert allowed is True


def test_apply_filter_chain_blocked_tracks_stats() -> None:
    rt = _make_runtime()
    chain = MagicMock()
    chain.should_evaluate.return_value = (False, "spread:too_wide")
    rt.filter_chain = chain

    allowed = rt._apply_filter_chain(
        "XAUUSD", "confirmed", "M5",
        datetime.now(timezone.utc), {}, [], {"spread_points": 5.0},
    )

    assert allowed is False
    scope_stats = rt._filter_by_scope.get("confirmed", {})
    assert scope_stats["blocked"] == 1
    assert scope_stats["blocks"].get("spread") == 1


def test_apply_filter_chain_passed_tracks_stats() -> None:
    rt = _make_runtime()
    chain = MagicMock()
    chain.should_evaluate.return_value = (True, "")
    rt.filter_chain = chain

    allowed = rt._apply_filter_chain(
        "XAUUSD", "confirmed", "M5",
        datetime.now(timezone.utc), {}, [], {},
    )

    assert allowed is True
    scope_stats = rt._filter_by_scope.get("confirmed", {})
    assert scope_stats["passed"] == 1
