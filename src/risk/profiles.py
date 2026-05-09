from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.config import RiskConfig
from src.config.models.runtime import RiskProfileConfig
from src.risk.models import TradeIntent


class RiskProfileResolutionError(ValueError):
    """Raised when an intent references an unknown risk profile."""


@dataclass(frozen=True)
class RiskProfileSelection:
    name: str
    config: RiskProfileConfig
    source: str

    @property
    def policy(self) -> str:
        return str(self.config.policy)

    @property
    def trade_frequency_enabled(self) -> bool:
        return "trade_frequency" in self.pre_trade_rules

    @property
    def pre_trade_rules(self) -> tuple[str, ...]:
        return tuple(str(item) for item in list(self.config.pre_trade_rules or []))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "policy": self.policy,
            "trade_frequency_enabled": self.trade_frequency_enabled,
            "pre_trade_rules": list(self.pre_trade_rules),
            "source": self.source,
        }

    def enables_rule(self, rule_name: str) -> bool:
        return str(rule_name or "").strip() in set(self.pre_trade_rules)


class RiskProfileResolver:
    """Resolves the risk contract for a pre-trade intent.

    Resolution order is explicit metadata, configured strategy binding, then the
    built-in ``standard_kline`` profile. Unknown names fail closed.
    """

    def __init__(self, settings: RiskConfig) -> None:
        self._settings = settings

    def resolve(self, intent: TradeIntent) -> RiskProfileSelection:
        metadata = _metadata(intent)
        explicit = str(metadata.get("risk_profile") or "").strip()
        if explicit:
            return self._selection(explicit, source="intent_metadata")

        strategy = str(metadata.get("strategy") or "").strip()
        if strategy:
            bindings = dict(getattr(self._settings, "risk_profile_bindings", {}) or {})
            bound = str(bindings.get(strategy) or "").strip()
            if bound:
                return self._selection(bound, source="strategy_binding")

        return self._selection("standard_kline", source="default")

    def _selection(self, name: str, *, source: str) -> RiskProfileSelection:
        return _select_profile(self._settings, name, source=source)


def _metadata(intent: TradeIntent) -> dict[str, Any]:
    return dict(intent.metadata or {}) if isinstance(intent.metadata, dict) else {}


def resolve_recovery_budget_profile_settings(
    settings: RiskConfig,
    *,
    profile_name: str,
) -> dict[str, Any]:
    """Resolve recovery loss-budget fields from a named risk profile.

    The resident recovery runner owns cycle state and execution orchestration,
    but the risk profile owns which loss-budget contract applies to that
    strategy family. Returning a plain payload keeps runtime wiring explicit
    without making the recovery domain depend on the config model.
    """

    selection = _select_profile(settings, profile_name, source="recovery_runner")
    if selection.policy != "recovery_budgeted":
        raise RiskProfileResolutionError(
            f"risk_profile_policy_mismatch:{selection.name}:{selection.policy}"
        )
    config = selection.config
    return {
        "risk_profile": selection.name,
        "max_daily_recovery_loss_amount": float(
            config.max_daily_recovery_loss_amount or 0.0
        ),
        "max_rolling_recovery_loss_amount": float(
            config.max_rolling_recovery_loss_amount or 0.0
        ),
        "rolling_loss_window_minutes": int(config.rolling_loss_window_minutes),
        "max_consecutive_loss_cycles": int(config.max_consecutive_loss_cycles),
        "loss_lockout_minutes": int(config.loss_lockout_minutes),
    }


def resolve_recovery_risk_profile_contract(
    settings: RiskConfig,
    *,
    profile_name: str,
) -> dict[str, Any]:
    selection = _select_profile(settings, profile_name, source="recovery_runner")
    if selection.policy != "recovery_budgeted":
        raise RiskProfileResolutionError(
            f"risk_profile_policy_mismatch:{selection.name}:{selection.policy}"
        )
    return selection.to_dict()


def validate_strategy_risk_profile_binding(
    settings: RiskConfig,
    *,
    strategy: str,
    profile_name: str,
) -> None:
    strategy_name = str(strategy or "").strip()
    expected_profile = str(profile_name or "").strip()
    if not strategy_name or not expected_profile:
        return
    bindings = dict(getattr(settings, "risk_profile_bindings", {}) or {})
    bound_profile = str(bindings.get(strategy_name) or "").strip()
    if not bound_profile or bound_profile == expected_profile:
        return
    raise RiskProfileResolutionError(
        "risk_profile_binding_mismatch:"
        f"{strategy_name}:bound={bound_profile}:runner={expected_profile}"
    )


def _select_profile(
    settings: RiskConfig,
    name: str,
    *,
    source: str,
) -> RiskProfileSelection:
    normalized = str(name or "").strip()
    if not normalized:
        raise RiskProfileResolutionError("risk_profile_required")
    profiles = dict(getattr(settings, "risk_profiles", {}) or {})
    config = profiles.get(normalized)
    if config is None:
        raise RiskProfileResolutionError(f"unknown_risk_profile:{normalized}")
    if isinstance(config, Mapping):
        config = RiskProfileConfig.model_validate(config)
    return RiskProfileSelection(name=normalized, config=config, source=source)
