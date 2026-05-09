from __future__ import annotations

from src.trading.recovery.calibration_guard import (
    RecoveryCostCalibrationGuard,
    RecoveryCostCalibrationGuardSettings,
)


def _snapshot(
    *,
    samples: int,
    target_shortfall_p90: float,
    net_margin_p50: float,
) -> dict:
    return {
        "entry_calibration": {
            "target_shortfall_points": {
                "sample_count": samples,
                "p90_recent": target_shortfall_p90,
            },
            "net_margin_points": {
                "sample_count": samples,
                "p50_recent": net_margin_p50,
            },
        }
    }


def test_calibration_guard_blocks_real_trade_when_samples_are_insufficient() -> None:
    guard = RecoveryCostCalibrationGuard()

    decision = guard.assess(
        settings=RecoveryCostCalibrationGuardSettings(min_samples=5),
        dry_run=False,
        analytics_snapshot=_snapshot(
            samples=4,
            target_shortfall_p90=0.0,
            net_margin_p50=3.0,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "calibration_guard_samples_insufficient"
    assert decision.metadata["sample_count"] == 4
    assert decision.metadata["min_samples"] == 5


def test_calibration_guard_blocks_real_trade_when_cost_distribution_is_negative() -> (
    None
):
    guard = RecoveryCostCalibrationGuard()
    settings = RecoveryCostCalibrationGuardSettings(
        min_samples=5,
        max_target_shortfall_p90_points=0.0,
        min_net_margin_p50_points=0.0,
    )

    shortfall = guard.assess(
        settings=settings,
        dry_run=False,
        analytics_snapshot=_snapshot(
            samples=5,
            target_shortfall_p90=1.0,
            net_margin_p50=2.0,
        ),
    )
    margin = guard.assess(
        settings=settings,
        dry_run=False,
        analytics_snapshot=_snapshot(
            samples=5,
            target_shortfall_p90=0.0,
            net_margin_p50=-0.1,
        ),
    )

    assert shortfall.allowed is False
    assert shortfall.reason == "calibration_guard_target_shortfall"
    assert margin.allowed is False
    assert margin.reason == "calibration_guard_net_margin_insufficient"


def test_calibration_guard_allows_real_trade_when_distribution_is_calibrated() -> None:
    guard = RecoveryCostCalibrationGuard()

    decision = guard.assess(
        settings=RecoveryCostCalibrationGuardSettings(
            min_samples=5,
            max_target_shortfall_p90_points=0.0,
            min_net_margin_p50_points=0.0,
        ),
        dry_run=False,
        analytics_snapshot=_snapshot(
            samples=6,
            target_shortfall_p90=0.0,
            net_margin_p50=1.5,
        ),
    )

    assert decision.allowed is True
    assert decision.reason == "calibration_guard_passed"
