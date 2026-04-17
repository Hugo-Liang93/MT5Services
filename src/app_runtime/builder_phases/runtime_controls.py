"""Runtime control and lifecycle component registry phase."""

from __future__ import annotations

import os

from src.app_runtime.container import AppContainer
from src.app_runtime.lifecycle import (
    FunctionalRuntimeComponent,
    RuntimeComponentRegistry,
)
from src.app_runtime.mode_controller import RuntimeModeController
from src.app_runtime.mode_policy import (
    RuntimeMode,
    RuntimeModeAutoTransitionPolicy,
    RuntimeModeEODAction,
    RuntimeModePolicy,
    RuntimeModeTransitionGuard,
)
from src.config import get_runtime_data_path, get_trading_ops_config
from src.trading.closeout import CloseoutRuntimeModeAction, ExposureCloseoutPolicy
from src.trading.commands.consumer import OperatorCommandConsumer
from src.trading.commands.service import OperatorCommandService
from src.trading.state import AccountRiskStateProjector


def build_runtime_controls(
    container: AppContainer,
    *,
    signal_config_loader,
) -> None:
    """Build runtime mode controllers and component registry."""
    if container.trade_module is None:
        return

    trading_ops_config = get_trading_ops_config()
    container.runtime_mode_guard = RuntimeModeTransitionGuard(
        trading_module_getter=lambda: container.trade_module,
    )
    container.runtime_mode_auto_policy = RuntimeModeAutoTransitionPolicy(
        after_eod_action=RuntimeModeEODAction(
            trading_ops_config.runtime_mode_after_eod
        ),
    )
    container.runtime_mode_controller = RuntimeModeController(
        container,
        policy=RuntimeModePolicy(
            initial_mode=RuntimeMode(trading_ops_config.runtime_mode),
            after_eod_action=container.runtime_mode_auto_policy.after_eod_action,
            auto_check_interval_seconds=(
                trading_ops_config.runtime_mode_auto_check_interval_seconds
            ),
        ),
        guard=container.runtime_mode_guard,
        auto_transition_policy=container.runtime_mode_auto_policy,
    )
    if container.exposure_closeout_controller is not None:
        container.exposure_closeout_controller.configure_runtime_mode_transition(
            policy=ExposureCloseoutPolicy(
                after_manual_closeout_action=CloseoutRuntimeModeAction(
                    trading_ops_config.runtime_mode_after_manual_closeout
                )
            ),
            apply_mode=container.runtime_mode_controller.apply_mode,
        )
    if (
        container.storage_writer is not None
        and container.runtime_identity is not None
        and container.operator_command_service is None
        and hasattr(container.storage_writer.db, "write_operator_commands")
        and hasattr(container.storage_writer.db, "fetch_operator_commands")
    ):
        container.operator_command_service = OperatorCommandService(
            write_fn=container.storage_writer.db.write_operator_commands,
            fetch_fn=container.storage_writer.db.fetch_operator_commands,
            runtime_identity=container.runtime_identity,
            pipeline_event_bus=container.pipeline_event_bus,
        )
    if (
        container.storage_writer is not None
        and container.runtime_identity is not None
        and container.trade_module is not None
        and container.trade_executor is not None
        and container.position_manager is not None
    ):
        container.account_risk_state_projector = AccountRiskStateProjector(
            write_fn=container.storage_writer.db.write_account_risk_states,
            runtime_identity=container.runtime_identity,
            trade_module=container.trade_module,
            trade_executor=container.trade_executor,
            position_manager=container.position_manager,
            pending_entry_manager=container.pending_entry_manager,
            runtime_mode_controller=container.runtime_mode_controller,
            market_service=container.market_service,
        )
        _wire_trade_state_projection(container)
    if (
        container.storage_writer is not None
        and container.runtime_identity is not None
        and container.trade_module is not None
        and container.operator_command_consumer is None
        and hasattr(container.storage_writer.db, "claim_operator_commands")
        and hasattr(container.storage_writer.db, "complete_operator_command")
        and hasattr(container.storage_writer.db, "heartbeat_operator_command")
    ):
        container.operator_command_consumer = OperatorCommandConsumer(
            claim_fn=container.storage_writer.db.claim_operator_commands,
            complete_fn=container.storage_writer.db.complete_operator_command,
            heartbeat_fn=container.storage_writer.db.heartbeat_operator_command,
            runtime_identity=container.runtime_identity,
            command_service=container.trade_module.commands,
            runtime_mode_controller=container.runtime_mode_controller,
            exposure_closeout_controller=container.exposure_closeout_controller,
            pending_entry_manager=container.pending_entry_manager,
            trade_executor=container.trade_executor,
            account_risk_state_projector=container.account_risk_state_projector,
            pipeline_event_bus=container.pipeline_event_bus,
        )
    container.runtime_component_registry = build_runtime_component_registry(
        container,
        signal_config_loader=signal_config_loader,
    )


def build_runtime_component_registry(
    container: AppContainer,
    *,
    signal_config_loader,
) -> RuntimeComponentRegistry:
    """Build component lifecycle registry."""
    all_modes = frozenset(mode.value for mode in RuntimeMode)
    signal_modes = (
        frozenset({RuntimeMode.FULL.value, RuntimeMode.OBSERVE.value})
        if container.indicator_manager is not None
        and container.signal_runtime is not None
        else frozenset()
    )
    risk_modes = frozenset(
        {
            RuntimeMode.FULL.value,
            RuntimeMode.OBSERVE.value,
            RuntimeMode.RISK_OFF.value,
        }
    )
    warmup_state = {"done": False}
    listener_state = {"attached": False}

    def _start_indicator_stack() -> None:
        indicators = container.indicator_manager
        if indicators is not None:
            indicators.start()
        calibrator = container.calibrator
        if calibrator is not None:
            try:
                calibrator_cache_path = get_runtime_data_path("calibrator_cache.json")
                os.makedirs(os.path.dirname(calibrator_cache_path), exist_ok=True)
                calibrator.load(calibrator_cache_path)
            except Exception:
                import logging

                logging.getLogger(__name__).debug(
                    "Calibrator warm-start load failed",
                    exc_info=True,
                )
            try:
                calibrator.start_background_refresh()
            except Exception:
                import logging

                logging.getLogger(__name__).debug(
                    "Calibrator start failed",
                    exc_info=True,
                )
        calendar = container.economic_calendar_service
        if calendar is not None:
            calendar.start()

    def _start_performance_warmup() -> None:
        if warmup_state["done"]:
            return
        sw = container.storage_writer
        signal_perf = container.signal_performance_tracker
        execution_perf = container.execution_performance_tracker
        runtime_identity = container.runtime_identity
        if sw is None:
            return
        try:
            if signal_perf is not None and container.signal_runtime is not None:
                signal_rows = sw.db.signal_repo.fetch_recent_signal_outcomes(hours=24)
                signal_perf.warm_up_from_db(signal_rows)
            if execution_perf is not None and runtime_identity is not None:
                trade_rows = sw.db.signal_repo.fetch_recent_trade_outcomes(
                    hours=24,
                    account_key=runtime_identity.account_key,
                )
                execution_perf.warm_up_from_db(trade_rows)
            warmup_state["done"] = True
        except Exception:
            import logging

            logging.getLogger(__name__).debug(
                "PerformanceTracker: warm-up from DB failed (non-fatal)",
                exc_info=True,
            )

    def _stop_indicator_stack() -> None:
        calibrator = container.calibrator
        if calibrator is not None:
            try:
                calibrator.stop_background_refresh()
            except Exception:
                import logging

                logging.getLogger(__name__).debug(
                    "Calibrator stop failed",
                    exc_info=True,
                )
        calendar = container.economic_calendar_service
        if calendar is not None:
            calendar.stop()
        indicators = container.indicator_manager
        if indicators is not None:
            indicators.shutdown()

    def _start_signals() -> None:
        runtime = container.signal_runtime
        if runtime is not None:
            runtime.start()

    def _stop_signals() -> None:
        runtime = container.signal_runtime
        if runtime is not None:
            runtime.stop()

    def _start_trade_execution() -> None:
        executor = container.trade_executor
        intent_consumer = container.execution_intent_consumer
        command_consumer = container.operator_command_consumer
        if executor is None:
            return
        executor.start()
        if intent_consumer is not None and not listener_state["attached"]:
            intent_consumer.start()
            if command_consumer is not None:
                command_consumer.start()
            listener_state["attached"] = True

    def _stop_trade_execution() -> None:
        executor = container.trade_executor
        intent_consumer = container.execution_intent_consumer
        command_consumer = container.operator_command_consumer
        if executor is None:
            return
        if intent_consumer is not None and listener_state["attached"]:
            if command_consumer is not None:
                command_consumer.stop()
            intent_consumer.stop()
            listener_state["attached"] = False
        executor.stop()

    def _start_pending_entry() -> None:
        pending = container.pending_entry_manager
        if pending is None:
            return
        was_running = pending.is_running()
        pending.start()
        recovery = container.trading_state_recovery
        trading = container.trade_module
        if not was_running and recovery is not None and trading is not None:
            try:
                result = recovery.restore_pending_orders(
                    pending_entry_manager=pending,
                    trading_module=trading,
                )
                import logging

                logging.getLogger(__name__).info(
                    "Pending order recovery: %s",
                    result,
                )
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "Pending order restore failed",
                    exc_info=True,
                )

    def _start_position_manager() -> None:
        position_manager = container.position_manager
        if position_manager is None:
            return
        was_running = position_manager.is_running()
        reconcile_interval = float(signal_config_loader().position_reconcile_interval)
        position_manager.start(reconcile_interval=reconcile_interval)
        if not was_running:
            try:
                recovery_result = position_manager.sync_open_positions()
                import logging

                logging.getLogger(__name__).info(
                    "PositionManager mode sync: %s",
                    recovery_result,
                )
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "PositionManager mode sync failed",
                    exc_info=True,
                )
            try:
                overnight_result = position_manager.force_close_overnight()
                if overnight_result is not None:
                    import logging

                    logging.getLogger(__name__).warning(
                        "Overnight force close on mode start: %s",
                        overnight_result,
                    )
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "Overnight force close failed",
                    exc_info=True,
                )

    def _start_paper_trading(c: AppContainer) -> None:
        bridge = c.paper_trading_bridge
        tracker = c.paper_trade_tracker
        if bridge is None:
            return
        # 顺序关键：先 bridge.start() 创建 session，再 tracker.start() 触发
        # 立即 flush 把初始 session 写 DB，避免 30s flush 窗口内崩溃丢失起始记录。
        # bridge 构造时已注入 on_trade_opened/closed 回调，tracker 未启动前产生的 trade
        # 会先入 tracker 内部 pending 队列，等 tracker.start() 后由 flush loop 刷出。
        bridge.start()
        if tracker is not None:
            tracker.start()

    def _stop_paper_trading(c: AppContainer) -> None:
        bridge = c.paper_trading_bridge
        tracker = c.paper_trade_tracker
        if bridge is not None:
            bridge.stop()
            if tracker is not None and bridge.session is not None:
                tracker.save_session(bridge.session)
                tracker.stop()

    return RuntimeComponentRegistry(
        [
            FunctionalRuntimeComponent(
                name="storage",
                supported_modes=all_modes,
                start_fn=lambda: (
                    container.storage_writer.start()
                    if container.storage_writer is not None
                    else None
                ),
                stop_fn=lambda: (
                    container.storage_writer.stop()
                    if container.storage_writer is not None
                    else None
                ),
                is_running_fn=lambda: bool(
                    container.storage_writer is not None
                    and container.storage_writer.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="performance_warmup",
                supported_modes=all_modes,
                start_fn=_start_performance_warmup,
                stop_fn=lambda: None,
                is_running_fn=lambda: bool(
                    warmup_state["done"]
                    and container.storage_writer is not None
                    and container.storage_writer.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="ingestion",
                supported_modes=(
                    all_modes if container.ingestor is not None else frozenset()
                ),
                start_fn=lambda: (
                    container.ingestor.start()
                    if container.ingestor is not None
                    else None
                ),
                stop_fn=lambda: (
                    container.ingestor.stop()
                    if container.ingestor is not None
                    else None
                ),
                is_running_fn=lambda: bool(
                    container.ingestor is not None and container.ingestor.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="pipeline_trace",
                supported_modes=all_modes,
                start_fn=lambda: (
                    container.pipeline_trace_recorder.start()
                    if container.pipeline_trace_recorder is not None
                    else None
                ),
                stop_fn=lambda: (
                    container.pipeline_trace_recorder.stop()
                    if container.pipeline_trace_recorder is not None
                    else None
                ),
                is_running_fn=lambda: bool(
                    container.pipeline_trace_recorder is not None
                    and container.pipeline_trace_recorder.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="indicators",
                supported_modes=signal_modes,
                start_fn=_start_indicator_stack,
                stop_fn=_stop_indicator_stack,
                is_running_fn=lambda: bool(
                    container.indicator_manager is not None
                    and container.indicator_manager.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="signals",
                supported_modes=signal_modes,
                start_fn=_start_signals,
                stop_fn=_stop_signals,
                is_running_fn=lambda: bool(
                    container.signal_runtime is not None
                    and container.signal_runtime.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="trade_execution",
                supported_modes=(
                    frozenset({RuntimeMode.FULL.value})
                    if container.trade_executor is not None
                    else frozenset()
                ),
                start_fn=_start_trade_execution,
                stop_fn=_stop_trade_execution,
                is_running_fn=lambda: bool(
                    container.trade_executor is not None
                    and container.trade_executor.is_running()
                    and (
                        container.execution_intent_consumer is None
                        or container.execution_intent_consumer.is_running()
                    )
                    and (
                        container.operator_command_consumer is None
                        or container.operator_command_consumer.is_running()
                    )
                ),
            ),
            FunctionalRuntimeComponent(
                name="pending_entry",
                supported_modes=(
                    risk_modes
                    if container.pending_entry_manager is not None
                    else frozenset()
                ),
                start_fn=_start_pending_entry,
                stop_fn=lambda: (
                    container.pending_entry_manager.shutdown()
                    if container.pending_entry_manager is not None
                    else None
                ),
                is_running_fn=lambda: bool(
                    container.pending_entry_manager is not None
                    and container.pending_entry_manager.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="position_manager",
                supported_modes=(
                    risk_modes
                    if container.position_manager is not None
                    else frozenset()
                ),
                start_fn=_start_position_manager,
                stop_fn=lambda: (
                    container.position_manager.stop()
                    if container.position_manager is not None
                    else None
                ),
                is_running_fn=lambda: bool(
                    container.position_manager is not None
                    and container.position_manager.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="paper_trading",
                supported_modes=(
                    frozenset({RuntimeMode.FULL.value, RuntimeMode.OBSERVE.value})
                    if container.paper_trading_bridge is not None
                    else frozenset()
                ),
                start_fn=lambda: _start_paper_trading(container),
                stop_fn=lambda: _stop_paper_trading(container),
                is_running_fn=lambda: bool(
                    container.paper_trading_bridge is not None
                    and container.paper_trading_bridge.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="account_risk_state_projection",
                supported_modes=(
                    risk_modes
                    if container.account_risk_state_projector is not None
                    else frozenset()
                ),
                start_fn=lambda: (
                    container.account_risk_state_projector.start()
                    if container.account_risk_state_projector is not None
                    else None
                ),
                stop_fn=lambda: (
                    container.account_risk_state_projector.stop()
                    if container.account_risk_state_projector is not None
                    else None
                ),
                is_running_fn=lambda: bool(
                    container.account_risk_state_projector is not None
                    and container.account_risk_state_projector.is_running()
                ),
            ),
        ]
    )


def _wire_trade_state_projection(container: AppContainer) -> None:
    if container.trade_module is None or container.trading_state_store is None:
        return

    def _projection_hook(state: dict[str, object]) -> None:
        container.trading_state_store.sync_trade_control(state)
        projector = container.account_risk_state_projector
        if projector is not None:
            projector.project_now()

    container.trade_module.set_trade_control_update_hook(_projection_hook)
