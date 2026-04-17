"""Read-model phase builders."""

from __future__ import annotations

from src.app_runtime.container import AppContainer
from src.readmodels.runtime import RuntimeReadModel
from src.readmodels.trade_trace import TradingFlowTraceReadModel


def build_runtime_read_models(container: AppContainer) -> None:
    """Build runtime read-model projections."""
    container.runtime_read_model = RuntimeReadModel(
        health_monitor=container.health_monitor,
        market_service=container.market_service,
        storage_writer=container.storage_writer,
        ingestor=container.ingestor,
        indicator_manager=container.indicator_manager,
        trading_queries=(
            container.trade_module.queries
            if container.trade_module is not None
            else None
        ),
        signal_runtime=container.signal_runtime,
        trade_executor=container.trade_executor,
        position_manager=container.position_manager,
        pending_entry_manager=container.pending_entry_manager,
        trading_state_store=container.trading_state_store,
        trading_state_alerts=container.trading_state_alerts,
        exposure_closeout_controller=container.exposure_closeout_controller,
        runtime_mode_controller=container.runtime_mode_controller,
        runtime_identity=container.runtime_identity,
        paper_trading_bridge=container.paper_trading_bridge,
        db_writer=(
            container.storage_writer.db
            if container.storage_writer is not None
            else None
        ),
    )
    if container.storage_writer is not None and container.trade_module is not None:
        container.trade_trace_read_model = TradingFlowTraceReadModel(
            signal_repo=container.storage_writer.db.signal_repo,
            command_audit_repo=container.storage_writer.db.trade_command_repo,
            pipeline_trace_repo=container.storage_writer.db.pipeline_trace_repo,
            trading_state_repo=container.storage_writer.db.trading_state_repo,
            account_alias_getter=lambda: container.trade_module.active_account_alias,
        )
