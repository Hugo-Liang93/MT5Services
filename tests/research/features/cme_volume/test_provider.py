"""CMEVolumeFeatureProvider — joins XAUUSD bars to daily CME GC volume,
emits volume-relative features as mining inputs.

Output features (4 fields):
  cme_volume_zscore_20d  (WHEN)  — volume relative to its own 20-day mean (institutional flow surge)
  cme_volume_change_5d   (WHY)   — 5-day cme_volume change (momentum of institutional activity)
  cme_volume_ratio       (WHY)   — today's volume / yesterday's volume
  cme_volume_pct_rank    (WHEN)  — today's volume percentile rank in trailing 60d
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import pytest

from src.research.features.cme_volume.provider import CMEVolumeFeatureProvider
from src.research.features.protocol import FeatureRole


class _FakeMatrix:
    def __init__(self, bar_times: List[datetime]):
        self.bar_times = bar_times
        self.n_bars = len(bar_times)
        self.indicator_series: dict = {}


def _bar_times_h1(days: int = 5, hours_per_day: int = 24) -> List[datetime]:
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    return [
        base + timedelta(days=d, hours=h)
        for d in range(days)
        for h in range(hours_per_day)
    ]


def test_role_mapping_assigns_each_field() -> None:
    p = CMEVolumeFeatureProvider(daily_volume_lookup=lambda d: None)
    roles = p.role_mapping()
    assert roles["cme_volume_zscore_20d"] == FeatureRole.WHEN
    assert roles["cme_volume_change_5d"] == FeatureRole.WHY
    assert roles["cme_volume_ratio"] == FeatureRole.WHY
    assert roles["cme_volume_pct_rank"] == FeatureRole.WHEN


def test_compute_emits_one_value_per_bar() -> None:
    bars = _bar_times_h1(days=5)
    matrix = _FakeMatrix(bars)
    # Mock lookup: every date returns 100k volume
    lookup = lambda d: 100_000.0
    p = CMEVolumeFeatureProvider(daily_volume_lookup=lookup)
    out = p.compute(matrix)

    for key, values in out.items():
        assert len(values) == matrix.n_bars, f"{key} length mismatch"


def test_compute_returns_none_when_volume_lookup_missing() -> None:
    bars = _bar_times_h1(days=5)
    matrix = _FakeMatrix(bars)
    p = CMEVolumeFeatureProvider(daily_volume_lookup=lambda d: None)
    out = p.compute(matrix)

    # All values should be None when lookup returns None for every date
    for key, values in out.items():
        assert all(v is None for v in values), f"{key} expected all None"


def test_zscore_increases_when_today_volume_spikes() -> None:
    bars = _bar_times_h1(days=22)
    matrix = _FakeMatrix(bars)
    # 20 days of 100k, last 2 days 500k spike
    def lookup(d: date) -> float:
        idx = (d - bars[0].date()).days
        return 500_000.0 if idx >= 20 else 100_000.0

    p = CMEVolumeFeatureProvider(daily_volume_lookup=lookup)
    out = p.compute(matrix)

    z = out[("cme_volume", "cme_volume_zscore_20d")]
    # First bars (no 20d history) should be None
    assert z[0] is None
    # Last bars (after spike) should have positive z-score > 2
    last = z[-1]
    assert last is not None and last > 2.0
