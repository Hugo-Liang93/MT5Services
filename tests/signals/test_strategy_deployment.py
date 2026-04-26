from __future__ import annotations

import pytest

from src.signals.contracts import (
    StrategyDeployment,
    StrategyDeploymentStatus,
    validate_strategy_deployments,
)


def test_validate_strategy_deployments_rejects_tf_specific_active_status() -> None:
    with pytest.raises(ValueError, match="tf_specific strategy cannot use status=active"):
        validate_strategy_deployments(
            deployments={
                "structured_session_breakout": StrategyDeployment(
                    name="structured_session_breakout",
                    status=StrategyDeploymentStatus.ACTIVE,
                    locked_timeframes=("M30",),
                    locked_sessions=("london",),
                    max_live_positions=1,
                    require_pending_entry=True,
                    demo_validation_required=True,
                    robustness_tier="tf_specific",
                )
            },
            known_strategies=("structured_session_breakout",),
            strategy_timeframes_policy={"structured_session_breakout": ("M30",)},
            strategy_sessions_policy={"structured_session_breakout": ("london",)},
        )


def test_validate_strategy_deployments_rejects_scope_conflict_with_policy() -> None:
    with pytest.raises(ValueError, match="locked_timeframes"):
        validate_strategy_deployments(
            deployments={
                "structured_session_breakout": StrategyDeployment(
                    name="structured_session_breakout",
                    status=StrategyDeploymentStatus.ACTIVE_GUARDED,
                    locked_timeframes=("M30",),
                    locked_sessions=("london",),
                    max_live_positions=1,
                    require_pending_entry=True,
                    demo_validation_required=True,
                    robustness_tier="tf_specific",
                )
            },
            known_strategies=("structured_session_breakout",),
            strategy_timeframes_policy={"structured_session_breakout": ("H1",)},
            strategy_sessions_policy={"structured_session_breakout": ("london",)},
        )


def test_validate_strategy_deployments_rejects_legacy_zero_affinity_freeze() -> None:
    with pytest.raises(ValueError, match="Regime-affinity freeze is no longer allowed"):
        validate_strategy_deployments(
            deployments={
                "structured_session_breakout": StrategyDeployment(
                    name="structured_session_breakout",
                    status=StrategyDeploymentStatus.CANDIDATE,
                    locked_timeframes=("M30",),
                    locked_sessions=("london",),
                    max_live_positions=1,
                    require_pending_entry=True,
                    demo_validation_required=True,
                    robustness_tier="tf_specific",
                )
            },
            known_strategies=("structured_session_breakout",),
            strategy_timeframes_policy={"structured_session_breakout": ("M30",)},
            strategy_sessions_policy={"structured_session_breakout": ("london",)},
            regime_affinity_overrides={
                "structured_session_breakout": {
                    "trending": 0.0,
                    "ranging": 0.0,
                    "breakout": 0.0,
                    "uncertain": 0.0,
                }
            },
        )


def test_active_guarded_effective_min_confidence_uses_harder_floor() -> None:
    deployment = StrategyDeployment(
        name="structured_session_breakout",
        status=StrategyDeploymentStatus.ACTIVE_GUARDED,
    )

    assert deployment.effective_min_confidence(
        timeframe_baseline=0.44,
        global_min_confidence=0.40,
    ) == pytest.approx(0.50)
    assert deployment.effective_min_confidence(
        timeframe_baseline=0.58,
        global_min_confidence=0.40,
    ) == pytest.approx(0.63)
