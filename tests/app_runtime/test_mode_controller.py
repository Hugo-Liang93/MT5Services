from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app_runtime.container import AppContainer
from src.app_runtime.lifecycle import (
    FunctionalRuntimeComponent,
    RuntimeComponentRegistry,
)
from src.app_runtime.mode_controller import RuntimeModeController
from src.app_runtime.mode_policy import (
    RuntimeMode,
    RuntimeModeAutoTransitionPolicy,
    RuntimeModePolicy,
    RuntimeModeTransitionGuard,
)


class _StartStopComponent:
    def __init__(self, thread_attr: str) -> None:
        self._thread_attr = thread_attr
        self.start_calls = 0
        self.stop_calls = 0
        setattr(self, thread_attr, None)

    def start(self) -> None:
        self.start_calls += 1
        setattr(self, self._thread_attr, SimpleNamespace(is_alive=lambda: True))

    def stop(self) -> None:
        self.stop_calls += 1
        setattr(self, self._thread_attr, None)

    def shutdown(self) -> None:
        self.stop()

    def is_running(self) -> bool:
        thread = getattr(self, self._thread_attr, None)
        return bool(thread is not None and thread.is_alive())


class _SignalRuntime(_StartStopComponent):
    def __init__(self) -> None:
        super().__init__("_thread")
        self.listeners: list = []

    def add_signal_listener(self, listener) -> None:
        if listener not in self.listeners:
            self.listeners.append(listener)

    def remove_signal_listener(self, listener) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)


class _Executor:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_calls += 1

    def on_signal_event(self, event) -> None:
        return None

    def shutdown(self, timeout: float = 5.0) -> None:
        self.shutdown_calls += 1


class _PendingEntry(_StartStopComponent):
    def __init__(self) -> None:
        super().__init__("_monitor_thread")


class _PositionManager(_StartStopComponent):
    def __init__(self) -> None:
        super().__init__("_reconcile_thread")
        self.sync_calls = 0
        self.force_close_calls = 0
        self.after_eod = False

    def start(self, reconcile_interval: float = 10.0) -> None:
        self.reconcile_interval = reconcile_interval
        super().start()

    def sync_open_positions(self) -> dict:
        self.sync_calls += 1
        return {"synced": 0}

    def force_close_overnight(self):
        self.force_close_calls += 1
        return None

    def is_after_eod_today(self) -> bool:
        return self.after_eod


class _Calibrator:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0

    def start_background_refresh(self) -> None:
        self.start_calls += 1

    def stop_background_refresh(self) -> None:
        self.stop_calls += 1


class _Recovery:
    def __init__(self) -> None:
        self.calls = 0

    def restore_pending_orders(self, *, pending_entry_manager, trading_module) -> dict:
        self.calls += 1
        return {"restored": 1}


class _TradeModule:
    def __init__(self, *, positions=None, orders=None) -> None:
        self._positions = list(positions or [])
        self._orders = list(orders or [])

    def get_positions(self):
        return list(self._positions)

    def get_orders(self):
        return list(self._orders)


def _build_container(*, positions=None, orders=None) -> AppContainer:
    container = AppContainer()
    listener_state = {"attached": False}
    container.storage_writer = _StartStopComponent("_thread")
    container.ingestor = _StartStopComponent("_thread")
    container.indicator_manager = _StartStopComponent("_event_thread")
    container.signal_runtime = _SignalRuntime()
    container.trade_executor = _Executor()
    container.pending_entry_manager = _PendingEntry()
    container.position_manager = _PositionManager()
    container.economic_calendar_service = _StartStopComponent("_thread")
    container.calibrator = _Calibrator()
    container.trade_module = _TradeModule(positions=positions, orders=orders)
    container.trading_state_recovery = _Recovery()
    container.runtime_component_registry = RuntimeComponentRegistry(
        [
            FunctionalRuntimeComponent(
                name="storage",
                supported_modes=frozenset(mode.value for mode in RuntimeMode),
                start_fn=container.storage_writer.start,
                stop_fn=container.storage_writer.stop,
                is_running_fn=container.storage_writer.is_running,
            ),
            FunctionalRuntimeComponent(
                name="ingestion",
                supported_modes=frozenset(mode.value for mode in RuntimeMode),
                start_fn=container.ingestor.start,
                stop_fn=container.ingestor.stop,
                is_running_fn=container.ingestor.is_running,
            ),
            FunctionalRuntimeComponent(
                name="indicators",
                supported_modes=frozenset(
                    {RuntimeMode.FULL.value, RuntimeMode.OBSERVE.value}
                ),
                start_fn=lambda: (
                    container.indicator_manager.start(),
                    container.calibrator.start_background_refresh(),
                    container.economic_calendar_service.start(),
                ),
                stop_fn=lambda: (
                    container.calibrator.stop_background_refresh(),
                    container.economic_calendar_service.stop(),
                    container.indicator_manager.shutdown(),
                ),
                is_running_fn=container.indicator_manager.is_running,
            ),
            FunctionalRuntimeComponent(
                name="signals",
                supported_modes=frozenset(
                    {RuntimeMode.FULL.value, RuntimeMode.OBSERVE.value}
                ),
                start_fn=container.signal_runtime.start,
                stop_fn=container.signal_runtime.stop,
                is_running_fn=container.signal_runtime.is_running,
            ),
            FunctionalRuntimeComponent(
                name="trade_execution",
                supported_modes=frozenset({RuntimeMode.FULL.value}),
                start_fn=lambda: (
                    container.trade_executor.start(),
                    container.signal_runtime.add_signal_listener(
                        container.trade_executor.on_signal_event
                    ),
                    listener_state.__setitem__("attached", True),
                ),
                stop_fn=lambda: (
                    container.signal_runtime.remove_signal_listener(
                        container.trade_executor.on_signal_event
                    ),
                    listener_state.__setitem__("attached", False),
                    container.trade_executor.stop(),
                ),
                is_running_fn=lambda: bool(listener_state["attached"]),
            ),
            FunctionalRuntimeComponent(
                name="pending_entry",
                supported_modes=frozenset(
                    {
                        RuntimeMode.FULL.value,
                        RuntimeMode.OBSERVE.value,
                        RuntimeMode.RISK_OFF.value,
                    }
                ),
                start_fn=container.pending_entry_manager.start,
                stop_fn=container.pending_entry_manager.shutdown,
                is_running_fn=container.pending_entry_manager.is_running,
            ),
            FunctionalRuntimeComponent(
                name="position_manager",
                supported_modes=frozenset(
                    {
                        RuntimeMode.FULL.value,
                        RuntimeMode.OBSERVE.value,
                        RuntimeMode.RISK_OFF.value,
                    }
                ),
                start_fn=lambda: container.position_manager.start(
                    reconcile_interval=30
                ),
                stop_fn=container.position_manager.stop,
                is_running_fn=container.position_manager.is_running,
            ),
        ]
    )
    return container


def test_mode_controller_full_and_observe_toggle_trade_listener() -> None:
    container = _build_container()
    controller = RuntimeModeController(
        container,
        policy=RuntimeModePolicy(initial_mode=RuntimeMode.FULL),
        guard=RuntimeModeTransitionGuard(trading_module_getter=lambda: container.trade_module),
        auto_transition_policy=RuntimeModeAutoTransitionPolicy(),
    )

    full = controller.apply_mode(RuntimeMode.FULL, reason="test")
    assert full["current_mode"] == "full"
    assert container.signal_runtime.listeners == [container.trade_executor.on_signal_event]
    assert container.trade_executor.start_calls == 1

    observe = controller.apply_mode(RuntimeMode.OBSERVE, reason="test")

    assert observe["current_mode"] == "observe"
    assert container.signal_runtime.listeners == []
    assert container.trade_executor.stop_calls == 1


def test_mode_controller_blocks_ingest_only_when_live_risk_exists() -> None:
    container = _build_container(positions=[{"ticket": 1}])
    controller = RuntimeModeController(
        container,
        policy=RuntimeModePolicy(initial_mode=RuntimeMode.FULL),
        guard=RuntimeModeTransitionGuard(trading_module_getter=lambda: container.trade_module),
        auto_transition_policy=RuntimeModeAutoTransitionPolicy(),
    )

    with pytest.raises(RuntimeError, match="ingest_only"):
        controller.apply_mode(RuntimeMode.INGEST_ONLY, reason="test")


def test_auto_eod_sets_flag_and_session_start_restores() -> None:
    """EOD 自动降级后，新交易日自动恢复初始模式。"""
    from src.app_runtime.mode_policy import RuntimeModeEODAction

    container = _build_container()
    pos_mgr: _PositionManager = container.position_manager  # type: ignore[assignment]
    controller = RuntimeModeController(
        container,
        policy=RuntimeModePolicy(initial_mode=RuntimeMode.FULL),
        guard=RuntimeModeTransitionGuard(trading_module_getter=lambda: container.trade_module),
        auto_transition_policy=RuntimeModeAutoTransitionPolicy(
            after_eod_action=RuntimeModeEODAction.INGEST_ONLY,
        ),
    )

    # 启动到 FULL
    controller.apply_mode(RuntimeMode.FULL, reason="startup")
    assert controller.current_mode == RuntimeMode.FULL
    snap = controller.snapshot()
    assert snap["is_auto_transitioned"] is False

    # 模拟 EOD 触发自动降级
    pos_mgr.after_eod = True
    controller._evaluate_auto_transition()  # type: ignore[attr-defined]
    assert controller.current_mode == RuntimeMode.INGEST_ONLY
    snap = controller.snapshot()
    assert snap["is_auto_transitioned"] is True
    assert snap["last_transition_reason"] == "after_eod"

    # 模拟新交易日 → 自动恢复
    pos_mgr.after_eod = False
    controller._evaluate_auto_transition()  # type: ignore[attr-defined]
    assert controller.current_mode == RuntimeMode.FULL
    snap = controller.snapshot()
    assert snap["is_auto_transitioned"] is False
    assert snap["last_transition_reason"] == "session_start"


def test_manual_ingest_only_not_auto_restored() -> None:
    """手动切换到 INGEST_ONLY 不会被自动恢复。"""
    from src.app_runtime.mode_policy import RuntimeModeEODAction

    container = _build_container()
    pos_mgr: _PositionManager = container.position_manager  # type: ignore[assignment]
    controller = RuntimeModeController(
        container,
        policy=RuntimeModePolicy(initial_mode=RuntimeMode.FULL),
        guard=RuntimeModeTransitionGuard(trading_module_getter=lambda: container.trade_module),
        auto_transition_policy=RuntimeModeAutoTransitionPolicy(
            after_eod_action=RuntimeModeEODAction.INGEST_ONLY,
        ),
    )

    # 手动切换到 INGEST_ONLY
    controller.apply_mode(RuntimeMode.INGEST_ONLY, reason="manual")
    assert controller.current_mode == RuntimeMode.INGEST_ONLY
    assert controller.snapshot()["is_auto_transitioned"] is False

    # 新交易日不会恢复
    pos_mgr.after_eod = False
    controller._evaluate_auto_transition()  # type: ignore[attr-defined]
    assert controller.current_mode == RuntimeMode.INGEST_ONLY


def test_apply_mode_partial_failure_still_updates_mode() -> None:
    """组件 start() 部分失败时，模式仍更新，last_error 记录失败详情。"""
    container = _build_container()
    # 让 indicator_manager.start() 抛异常
    original_start = container.indicator_manager.start
    call_count = {"n": 0}

    def flaky_start() -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated indicator start failure")
        original_start()

    container.indicator_manager.start = flaky_start  # type: ignore[assignment]

    controller = RuntimeModeController(
        container,
        policy=RuntimeModePolicy(initial_mode=RuntimeMode.FULL),
        guard=RuntimeModeTransitionGuard(trading_module_getter=lambda: container.trade_module),
        auto_transition_policy=RuntimeModeAutoTransitionPolicy(),
    )

    # 首次 apply_mode(FULL) 成功
    controller.apply_mode(RuntimeMode.FULL, reason="setup")
    assert controller.current_mode == RuntimeMode.FULL

    # 切到 INGEST_ONLY（indicators.stop() 正常）
    controller.apply_mode(RuntimeMode.INGEST_ONLY, reason="eod")
    assert controller.current_mode == RuntimeMode.INGEST_ONLY

    # 切回 FULL → indicators.start() 抛异常
    snap = controller.apply_mode(RuntimeMode.FULL, reason="restore")
    # 模式仍更新为 FULL
    assert controller.current_mode == RuntimeMode.FULL
    # last_error 记录了部分失败
    assert snap["last_error"] is not None
    assert "partial failure" in str(snap["last_error"])
    # 其他组件仍正常运行
    assert container.signal_runtime.is_running()  # type: ignore[union-attr]
