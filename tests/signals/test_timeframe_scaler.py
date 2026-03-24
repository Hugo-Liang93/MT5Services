from __future__ import annotations

from src.signals.strategies.base import TimeframeScaler


def test_timeframe_scaler_scales_m5_more_aggressively() -> None:
    scaler = TimeframeScaler("M5")
    assert scaler.scale_period(20) == 15  # 20 * 0.75 = 15
    assert scaler.scale_threshold(25.0) == 25.0 * 0.75


def test_timeframe_scaler_keeps_h1_neutral() -> None:
    scaler = TimeframeScaler("H1")
    assert scaler.scale_period(20) == 20
    assert scaler.scale_threshold(25.0) == 25.0


def test_timeframe_scaler_m30() -> None:
    scaler = TimeframeScaler("M30")
    assert scaler.scale_period(20) == 18  # 20 * 0.92 = 18.4 → 18
    assert scaler.scale_threshold(25.0) == 25.0 * 0.92
