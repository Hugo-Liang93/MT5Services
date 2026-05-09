from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class RecoveryCostCalibrationGuardSettings:
    enabled: bool = True
    min_samples: int = 50
    max_target_shortfall_p90_points: float = 0.0
    min_net_margin_p50_points: float = 0.0

    def __post_init__(self) -> None:
        if self.min_samples < 0:
            raise ValueError("min_samples must be >= 0")
        if self.max_target_shortfall_p90_points < 0:
            raise ValueError("max_target_shortfall_p90_points must be >= 0")


@dataclass(frozen=True)
class RecoveryCostCalibrationDecision:
    allowed: bool
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


class RecoveryCostCalibrationGuard:
    """Fail-closed guard before real recovery runner entries.

    The guard is a read-only projection consumer. It does not mutate analytics,
    change policy thresholds, or dispatch trades.
    """

    def assess(
        self,
        *,
        settings: RecoveryCostCalibrationGuardSettings,
        dry_run: bool,
        analytics_snapshot: Mapping[str, Any],
    ) -> RecoveryCostCalibrationDecision:
        metadata = {
            "enabled": bool(settings.enabled),
            "dry_run": bool(dry_run),
            "min_samples": int(settings.min_samples),
            "max_target_shortfall_p90_points": float(
                settings.max_target_shortfall_p90_points
            ),
            "min_net_margin_p50_points": float(settings.min_net_margin_p50_points),
        }
        if not settings.enabled:
            return RecoveryCostCalibrationDecision(
                True,
                "calibration_guard_disabled",
                metadata,
            )
        if dry_run:
            return RecoveryCostCalibrationDecision(
                True,
                "calibration_guard_dry_run",
                metadata,
            )

        calibration = _mapping(analytics_snapshot.get("entry_calibration"))
        shortfall_stats = _mapping(calibration.get("target_shortfall_points"))
        margin_stats = _mapping(calibration.get("net_margin_points"))
        shortfall_samples = _optional_int(shortfall_stats.get("sample_count")) or 0
        margin_samples = _optional_int(margin_stats.get("sample_count")) or 0
        sample_count = min(shortfall_samples, margin_samples)
        target_shortfall_p90 = _optional_float(shortfall_stats.get("p90_recent"))
        net_margin_p50 = _optional_float(margin_stats.get("p50_recent"))
        metadata.update(
            {
                "sample_count": sample_count,
                "target_shortfall_p90_points": target_shortfall_p90,
                "net_margin_p50_points": net_margin_p50,
            }
        )

        if sample_count < int(settings.min_samples):
            return RecoveryCostCalibrationDecision(
                False,
                "calibration_guard_samples_insufficient",
                metadata,
            )
        if target_shortfall_p90 is None:
            return RecoveryCostCalibrationDecision(
                False,
                "calibration_guard_target_shortfall_missing",
                metadata,
            )
        if target_shortfall_p90 > float(settings.max_target_shortfall_p90_points):
            return RecoveryCostCalibrationDecision(
                False,
                "calibration_guard_target_shortfall",
                metadata,
            )
        if net_margin_p50 is None:
            return RecoveryCostCalibrationDecision(
                False,
                "calibration_guard_net_margin_missing",
                metadata,
            )
        if net_margin_p50 <= float(settings.min_net_margin_p50_points):
            return RecoveryCostCalibrationDecision(
                False,
                "calibration_guard_net_margin_insufficient",
                metadata,
            )
        return RecoveryCostCalibrationDecision(
            True,
            "calibration_guard_passed",
            metadata,
        )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
