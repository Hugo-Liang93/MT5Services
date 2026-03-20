from __future__ import annotations

import pytest

from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.regime import RegimeType


def test_confidence_calibrator_phase1_defaults() -> None:
    calibrator = ConfidenceCalibrator(fetch_winrates_fn=lambda **kwargs: [])

    state = calibrator.describe()

    assert state["alpha"] == pytest.approx(0.15)
    assert state["min_samples"] == 50
    assert state["recency_hours"] == 8


def test_confidence_calibrator_refresh_uses_phase1_sample_thresholds() -> None:
    def fetch_winrates(*, hours: int, symbol=None):
        if hours == 168:
            return [
                ("supertrend", "buy", 49, 30, 0.61, 0.70, 5.0, "trending"),
                ("supertrend", "buy", 50, 31, 0.62, 0.70, 5.0, "trending"),
            ]
        if hours == 8:
            return [
                ("supertrend", "buy", 24, 15, 0.61, 0.70, 5.0, "trending"),
                ("supertrend", "buy", 25, 15, 0.60, 0.70, 5.0, "trending"),
            ]
        raise AssertionError(f"unexpected hours={hours}")

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch_winrates)

    count = calibrator.refresh()
    adjusted = calibrator.calibrate("supertrend", "buy", 0.80, RegimeType.TRENDING)
    state = calibrator.describe()

    assert count == 1
    assert state["cache_entries"] == 1
    assert state["recent_cache_entries"] == 1
    assert adjusted > 0.80


def test_confidence_calibrator_uses_staged_alpha_by_sample_count() -> None:
    def fetch_winrates(*, hours: int, symbol=None):
        if hours == 168:
            return [
                ("supertrend", "buy", 60, 42, 0.70, 0.70, 5.0, "trending"),
                ("macd_momentum", "buy", 120, 84, 0.70, 0.70, 5.0, "trending"),
            ]
        if hours == 8:
            return [
                ("supertrend", "buy", 30, 21, 0.70, 0.70, 5.0, "trending"),
                ("macd_momentum", "buy", 60, 42, 0.70, 0.70, 5.0, "trending"),
            ]
        raise AssertionError(f"unexpected hours={hours}")

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch_winrates)
    calibrator.refresh()

    warm = calibrator.calibrate("supertrend", "buy", 0.80, RegimeType.TRENDING)
    full = calibrator.calibrate("macd_momentum", "buy", 0.80, RegimeType.TRENDING)

    assert warm == pytest.approx(0.824)
    assert full == pytest.approx(0.836)
