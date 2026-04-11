"""Runtime control and lifecycle component registry phase."""

from __future__ import annotations

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
from src.config import get_trading_ops_config
from src.config import get_runtime_data_path
from src.trading.closeout import CloseoutRuntimeModeAction, ExposureCloseoutPolicy
import os


def build_runtime_controls(
    container: AppContainer,
    *,
    signal_config_loader,
) -> None:
    """Build runtime mode controllers and component registry."""
    if container.trade_module is None:
        return

    trading_ops_config = get_trading_ops_config()
    container.runtime_component_registry = build_runtime_component_registry(
        container,
        signal_config_loader=signal_config_loader,
    )
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


def build_runtime_component_registry(
    container: AppContainer,
    *,
    signal_config_loader,
) -> RuntimeComponentRegistry:
    """Build component lifecycle registry."""
    all_modes = frozenset(mode.value for mode in RuntimeMode)
    signal_modes = frozenset({RuntimeMode.FULL.value, RuntimeMode.OBSERVE.value})
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
        perf = container.performance_tracker
        sw = container.storage_writer
        if perf is None or sw is None:
            return
        try:
            rows = sw.db.signal_repo.fetch_recent_outcomes(hours=24)
            perf.warm_up_from_db(rows)
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
        runtime = container.signal_runtime
        executor = container.trade_executor
        if runtime is None or executor is None:
            return
        executor.start()
        if not listener_state["attached"]:
            runtime.add_signal_listener(executor.on_signal_event)
            listener_state["attached"] = True

    def _stop_trade_execution() -> None:
        runtime = container.signal_runtime
        executor = container.trade_executor
        if runtime is None or executor is None:
            return
        if listener_state["attached"]:
            runtime.remove_signal_listener(executor.on_signal_event)
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
        if tracker is not None:
            tracker.start()
        bridge.start()

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
                supported_modes=all_modes,
                start_fn=lambda: (
                    container.ingestor.start() if container.ingestor is not None else None
                ),
                stop_fn=lambda: (
                    container.ingestor.stop() if container.ingestor is not None else None
                ),
                is_running_fn=lambda: bool(
                    container.ingestor is not None and container.ingestor.is_running()
                ),
            ),
            FunctionalRuntimeComponent(
                name="pipeline_trace",
                supported_modes=signal_modes,
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
                supported_modes=frozenset({RuntimeMode.FULL.value}),
                start_fn=_start_trade_execution,
                stop_fn=_stop_trade_execution,
                is_running_fn=lambda: bool(listener_state["attached"]),
            ),
            FunctionalRuntimeComponent(
                name="pending_entry",
                supported_modes=risk_modes,
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
                supported_modes=risk_modes,
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
                supported_modes=frozenset({RuntimeMode.FULL.value, RuntimeMode.OBSERVE.value}),
                start_fn=lambda: _start_paper_trading(container),
                stop_fn=lambda: _stop_paper_trading(container),
                is_running_fn=lambda: bool(
                    container.paper_trading_bridge is not None
                    and container.paper_trading_bridge.is_running()
                ),
            ),
        ]
    )
