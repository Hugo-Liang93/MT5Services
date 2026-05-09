from __future__ import annotations

from src.app_runtime.container import AppContainer
from src.app_runtime.builder_phases.tick_features import attach_tick_feature_listener
from src.config.models import RiskConfig
from src.risk.profiles import (
    resolve_recovery_budget_profile_settings,
    resolve_recovery_risk_profile_contract,
    validate_strategy_risk_profile_binding,
)
from src.trading.recovery.runner import (
    DemoBoundedRecoveryRunner,
    RecoveryRuntimeRunnerSettings,
)


def build_recovery_runtime_layer(
    container: AppContainer,
    *,
    risk_config: RiskConfig,
) -> None:
    """Wire resident recovery services.

    The recovery runner is deliberately outside SignalRuntime. It consumes
    tick-derived feature snapshots from the shared tick feature bus and writes
    recovery-cycle state through TradingStateStore.
    """

    settings_model = risk_config.recovery_runtime_runner
    if not settings_model.enabled:
        return
    validate_strategy_risk_profile_binding(
        risk_config,
        strategy=str(settings_model.strategy),
        profile_name=str(settings_model.risk_profile),
    )
    _assert_recovery_dependencies(container)
    if not settings_model.dry_run and container.position_manager is None:
        raise RuntimeError(
            "recovery_runtime_runner dry_run=false requires position_manager"
        )
    settings_payload = settings_model.model_dump()
    settings_payload.update(
        resolve_recovery_budget_profile_settings(
            risk_config,
            profile_name=str(settings_model.risk_profile),
        )
    )
    profile_contract = resolve_recovery_risk_profile_contract(
        risk_config,
        profile_name=str(settings_model.risk_profile),
    )
    settings = RecoveryRuntimeRunnerSettings(**settings_payload)
    trade_module = container.trade_module
    runner = DemoBoundedRecoveryRunner(
        settings=settings,
        account_alias=str(getattr(trade_module, "active_account_alias", "")),
        account_key=str(getattr(trade_module, "active_account_key", "")),
        state_store=container.trading_state_store,
        trading_port=trade_module,
        position_snapshot_provider=container.position_manager,
        risk_profile_contract=profile_contract,
    )
    if not attach_tick_feature_listener(container, runner.on_tick_feature_snapshot):
        raise RuntimeError("recovery_runtime_runner requires tick feature runtime")
    container.recovery_runner = runner


def _assert_recovery_dependencies(container: AppContainer) -> None:
    missing: list[str] = []
    if container.market_service is None:
        missing.append("market_service")
    if container.trade_module is None:
        missing.append("trade_module")
    if container.trading_state_store is None:
        missing.append("trading_state_store")
    if missing:
        raise RuntimeError(
            "recovery_runtime_runner enabled but dependencies are missing: "
            + ", ".join(missing)
        )
