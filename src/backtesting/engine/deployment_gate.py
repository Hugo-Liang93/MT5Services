from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from src.signals.contracts.deployment import (
    StrategyDeployment,
    normalize_strategy_deployments,
)


class BacktestDeploymentGateMode(str, Enum):
    CONFIG = "config"
    SNAPSHOT = "snapshot"
    RESEARCH_DISABLED = "research_disabled"


@dataclass(frozen=True)
class BacktestDeploymentGate:
    """Backtest deployment-gate contract.

    CONFIG/SNAPSHOT modes require explicit deployment contracts. Research-only
    bypass must use RESEARCH_DISABLED with an audit reason instead of passing an
    empty mapping through the engine constructor.
    """

    mode: BacktestDeploymentGateMode
    deployments: Mapping[str, StrategyDeployment] = field(default_factory=dict)
    audit_reason: str | None = None

    def __post_init__(self) -> None:
        mode = (
            self.mode
            if isinstance(self.mode, BacktestDeploymentGateMode)
            else BacktestDeploymentGateMode(str(self.mode).strip().lower())
        )
        normalized = normalize_strategy_deployments(self.deployments)
        reason = (self.audit_reason or "").strip() or None

        if mode is BacktestDeploymentGateMode.RESEARCH_DISABLED:
            if normalized:
                raise ValueError(
                    "research_disabled deployment gate must not carry deployments"
                )
            if not reason:
                raise ValueError(
                    "research_disabled deployment gate requires audit_reason"
                )
        elif not normalized:
            raise ValueError(
                "Backtest deployment gate requires non-empty strategy deployments; "
                "use BacktestDeploymentGate.research_disabled(...) for research-only bypass"
            )

        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "deployments", normalized)
        object.__setattr__(self, "audit_reason", reason)

    @classmethod
    def from_config(
        cls, deployments: Mapping[str, StrategyDeployment]
    ) -> "BacktestDeploymentGate":
        return cls(
            mode=BacktestDeploymentGateMode.CONFIG,
            deployments=deployments,
        )

    @classmethod
    def from_snapshot(
        cls,
        deployments: Mapping[str, StrategyDeployment],
        *,
        audit_reason: str | None = None,
    ) -> "BacktestDeploymentGate":
        return cls(
            mode=BacktestDeploymentGateMode.SNAPSHOT,
            deployments=deployments,
            audit_reason=audit_reason,
        )

    @classmethod
    def research_disabled(cls, audit_reason: str) -> "BacktestDeploymentGate":
        return cls(
            mode=BacktestDeploymentGateMode.RESEARCH_DISABLED,
            deployments={},
            audit_reason=audit_reason,
        )

    @property
    def is_enabled(self) -> bool:
        return self.mode is not BacktestDeploymentGateMode.RESEARCH_DISABLED

    def require_deployment(self, strategy_name: str) -> StrategyDeployment | None:
        if not self.is_enabled:
            return None
        name = str(strategy_name).strip()
        deployment = self.deployments.get(name)
        if deployment is None:
            raise ValueError(
                "Backtest deployment gate missing explicit contract for strategy: "
                f"{name}"
            )
        return deployment
