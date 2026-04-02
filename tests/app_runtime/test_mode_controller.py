from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app_runtime.container import AppContainer
from src.app_runtime.mode_controller import (
    RuntimeMode,
    RuntimeModeController,
    RuntimeModePolicy,
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
        self.shutdown_calls = 0

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
    return container


def test_mode_controller_full_and_observe_toggle_trade_listener() -> None:
    container = _build_container()
    controller = RuntimeModeController(
        container,
        policy=RuntimeModePolicy(initial_mode=RuntimeMode.FULL),
        signal_config_loader=lambda: SimpleNamespace(position_reconcile_interval=30),
    )

    full = controller.apply_mode(RuntimeMode.FULL, reason="test")
    assert full["current_mode"] == "full"
    assert container.signal_runtime.listeners == [container.trade_executor.on_signal_event]

    observe = controller.apply_mode(RuntimeMode.OBSERVE, reason="test")

    assert observe["current_mode"] == "observe"
    assert container.signal_runtime.listeners == []


def test_mode_controller_blocks_ingest_only_when_live_risk_exists() -> None:
    container = _build_container(positions=[{"ticket": 1}])
    controller = RuntimeModeController(
        container,
        policy=RuntimeModePolicy(initial_mode=RuntimeMode.FULL),
    )

    with pytest.raises(RuntimeError, match="ingest_only"):
        controller.apply_mode(RuntimeMode.INGEST_ONLY, reason="test")
