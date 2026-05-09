"""Bounded recovery trading contracts.

This package owns recovery / martingale-style money-management state. Signal
strategies may trigger a cycle, but sizing escalation and recovery gates stay in
this domain so they remain testable, bounded, and auditable.
"""

from .controller import BoundedRecoveryController
from .analytics import RecoveryDecisionAnalytics
from .calibration_guard import (
    RecoveryCostCalibrationDecision,
    RecoveryCostCalibrationGuard,
    RecoveryCostCalibrationGuardSettings,
)
from .entry_policies import (
    RecoveryCostDecision,
    RecoveryCostGate,
    RecoveryDirectionDecision,
    RecoveryDirectionPolicy,
    RecoveryExitModel,
)
from .execution import RecoveryExecutionAdapter, RecoveryExecutionCanaryPolicy
from .exposure_ledger import RecoveryExposureLedger, RecoveryExposureSnapshot
from .guard import RecoveryPreTradeDecision, RecoveryPreTradeGuard
from .models import (
    PositionScalingIntent,
    RecoveryCycleState,
    RecoveryCycleStateRecord,
    RecoveryDecision,
    RecoveryExecutionPlan,
    RecoveryMarketSnapshot,
    RecoveryPolicy,
)

_RUNNER_EXPORTS = {
    "DemoBoundedRecoveryRunner",
    "RecoveryRuntimeRunnerSettings",
}


def __getattr__(name: str):
    if name in _RUNNER_EXPORTS:
        from .runner import DemoBoundedRecoveryRunner, RecoveryRuntimeRunnerSettings

        exports = {
            "DemoBoundedRecoveryRunner": DemoBoundedRecoveryRunner,
            "RecoveryRuntimeRunnerSettings": RecoveryRuntimeRunnerSettings,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "BoundedRecoveryController",
    "DemoBoundedRecoveryRunner",
    "PositionScalingIntent",
    "RecoveryCostDecision",
    "RecoveryCostCalibrationDecision",
    "RecoveryCostCalibrationGuard",
    "RecoveryCostCalibrationGuardSettings",
    "RecoveryCostGate",
    "RecoveryDirectionDecision",
    "RecoveryDirectionPolicy",
    "RecoveryExecutionAdapter",
    "RecoveryExecutionCanaryPolicy",
    "RecoveryExposureLedger",
    "RecoveryExposureSnapshot",
    "RecoveryPreTradeDecision",
    "RecoveryPreTradeGuard",
    "RecoveryExitModel",
    "RecoveryCycleState",
    "RecoveryCycleStateRecord",
    "RecoveryDecision",
    "RecoveryDecisionAnalytics",
    "RecoveryExecutionPlan",
    "RecoveryMarketSnapshot",
    "RecoveryPolicy",
    "RecoveryRuntimeRunnerSettings",
]
