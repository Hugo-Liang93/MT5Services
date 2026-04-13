"""Trading component phase builders."""

from __future__ import annotations

from typing import Any, Optional

from src.app_runtime.container import AppContainer
from src.app_runtime.factories import build_trading_components
from src.config import get_trading_ops_config
from src.trading.state import (
    TradingStateAlerts,
    TradingStateRecovery,
    TradingStateRecoveryPolicy,
    TradingStateStore,
)


def build_trading_layer(
    container: AppContainer,
    *,
    economic_settings: Any,
    enable_calendar_sync: bool = True,
) -> Optional[str]:
    """Build trading module and optional trading-state helpers."""
    trading_components = build_trading_components(
        container.storage_writer,
        economic_settings,
        runtime_identity=container.runtime_identity,
        enable_calendar_sync=enable_calendar_sync,
    )
    container.economic_calendar_service = trading_components.economic_calendar_service
    container.trade_registry = trading_components.trade_registry
    container.trade_module = trading_components.trade_module
    active_account_alias: Optional[str] = trading_components.active_account_alias

    if container.trade_module is not None and container.storage_writer is not None:
        trading_ops_config = get_trading_ops_config()
        container.trading_state_store = TradingStateStore(
            container.storage_writer.db,
            account_alias_getter=lambda: container.trade_module.active_account_alias,
            account_key_getter=lambda: container.trade_module.active_account_key,
        )
        container.trading_state_alerts = TradingStateAlerts(
            state_store=container.trading_state_store,
            trading_module=container.trade_module,
            account_alias_getter=lambda: container.trade_module.active_account_alias,
        )
        container.trading_state_recovery_policy = TradingStateRecoveryPolicy(
            container.trading_state_store,
            orphan_action=trading_ops_config.pending_recovery_orphan_action,
            missing_action=trading_ops_config.pending_recovery_missing_action,
        )
        container.trading_state_recovery = TradingStateRecovery(
            container.trading_state_store,
            policy=container.trading_state_recovery_policy,
        )
        container.trade_module.set_trade_control_update_hook(
            container.trading_state_store.sync_trade_control
        )

    if enable_calendar_sync and economic_settings.market_impact_enabled:
        from src.calendar.economic_calendar.market_impact import MarketImpactAnalyzer

        ingestor_ref = container.ingestor
        container.market_impact_analyzer = MarketImpactAnalyzer(
            db_writer=container.storage_writer.db,
            market_repo=container.storage_writer.db.market_repo,
            settings=economic_settings,
            warmup_ready_fn=(
                (lambda: not ingestor_ref.is_backfilling)
                if ingestor_ref is not None
                else None
            ),
        )
        container.economic_calendar_service.market_impact_analyzer = (
            container.market_impact_analyzer
        )

    return active_account_alias
