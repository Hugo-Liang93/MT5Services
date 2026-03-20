from __future__ import annotations

from src.signals.strategies.base import TimeframeScaler


def test_timeframe_scaler_scales_m1_more_aggressively() -> None:
    scaler = TimeframeScaler("M1")
    assert scaler.scale_period(20) == 12
    assert scaler.scale_threshold(25.0) == 15.0


def test_timeframe_scaler_keeps_h1_neutral() -> None:
    scaler = TimeframeScaler("H1")
    assert scaler.scale_period(20) == 20
    assert scaler.scale_threshold(25.0) == 25.0
