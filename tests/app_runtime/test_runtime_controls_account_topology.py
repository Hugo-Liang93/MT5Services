from __future__ import annotations

from types import SimpleNamespace

from src.app_runtime.builder_phases.runtime_controls import (
    build_runtime_component_registry,
    build_runtime_controls,
)
from src.app_runtime.container import AppContainer
from src.app_runtime.mode_policy import RuntimeMode
from src.config.runtime_identity import RuntimeIdentity, build_account_key


class _StartStopComponent:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.running = False

    def start(self, *args, **kwargs) -> None:
        self.start_calls += 1
        self.running = True

    def stop(self, *args, **kwargs) -> None:
        self.stop_calls += 1
        self.running = False

    def shutdown(self, *args, **kwargs) -> None:
        self.stop_calls += 1
        self.running = False

    def is_running(self) -> bool:
        return self.running


class _Calibrator:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0

    def load(self, *_args, **_kwargs) -> None:
        return None

    def start_background_refresh(self) -> None:
        self.start_calls += 1

    def stop_background_refresh(self) -> None:
        self.stop_calls += 1


class _PositionManager(_StartStopComponent):
    def start(self, *args, **kwargs) -> None:
        super().start(*args, **kwargs)

    def sync_open_positions(self):
        return {"synced": True}

    def force_close_overnight(self):
        return None


class _PendingEntryManager(_StartStopComponent):
    def shutdown(self, *args, **kwargs) -> None:
        self.stop(*args, **kwargs)


class _StorageWriter(_StartStopComponent):
    def __init__(self) -> None:
        super().__init__()
        self.db = SimpleNamespace(
            write_account_risk_states=lambda rows: None,
            signal_repo=SimpleNamespace(
                fetch_recent_trade_outcomes=lambda **kwargs: [],
            ),
        )


def _executor_identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        instance_name="live-exec-a",
        environment="live",
        instance_id="executor-live_exec_a",
        instance_role="executor",
        live_topology_mode="multi_account",
        account_alias="live_exec_a",
        account_label="Live Exec A",
        account_key=build_account_key("live", "Broker-Live", 1002),
        mt5_server="Broker-Live",
        mt5_login=1002,
        mt5_path="C:/MT5/live_exec_a/terminal64.exe",
    )


def test_executor_runtime_registry_does_not_start_shared_compute_stack() -> None:
    container = AppContainer()
    container.runtime_identity = _executor_identity()
    container.storage_writer = _StorageWriter()
    container.pipeline_trace_recorder = _StartStopComponent()
    container.trade_executor = _StartStopComponent()
    container.execution_intent_consumer = _StartStopComponent()
    container.pending_entry_manager = _PendingEntryManager()
    container.position_manager = _PositionManager()
    container.account_risk_state_projector = _StartStopComponent()
    container.trading_state_recovery = None
    container.trade_module = None

    registry = build_runtime_component_registry(
        container,
        signal_config_loader=lambda: SimpleNamespace(position_reconcile_interval=10),
    )

    registry.apply_mode("full")

    assert container.storage_writer.start_calls == 1
    assert container.trade_executor.start_calls == 1
    assert container.pending_entry_manager.start_calls == 1
    assert container.position_manager.start_calls == 1
    assert container.pipeline_trace_recorder.start_calls == 1
    assert container.account_risk_state_projector.start_calls == 1


class _TradeModule:
    def __init__(self) -> None:
        self._hook = None

    def set_trade_control_update_hook(self, fn) -> None:
        self._hook = fn

    def trade_control_status(self) -> dict:
        return {
            "auto_entry_enabled": True,
            "close_only_mode": False,
            "reason": None,
        }

    def account_info(self):
        return {"equity": 1000.0, "margin": 100.0, "margin_level": 1000.0}


class _TradeExecutorStatus(_StartStopComponent):
    def status(self) -> dict:
        return {
            "enabled": True,
            "circuit_breaker": {"open": False, "consecutive_failures": 0},
            "last_risk_block": None,
        }


class _PositionManagerStatus(_PositionManager):
    def status(self) -> dict:
        return {
            "running": True,
            "tracked_positions": 0,
            "reconcile_interval": 10.0,
            "reconcile_count": 0,
            "last_reconcile_at": None,
            "last_error": None,
            "margin_guard": {
                "state": "safe",
                "should_block_new_trades": False,
                "should_tighten_stops": False,
                "should_emergency_close": False,
                "margin_level": 1000.0,
            },
        }

    def active_positions(self):
        return []


class _PendingEntryManagerStatus(_PendingEntryManager):
    def status(self) -> dict:
        return {"active_count": 0, "entries": [], "stats": {}}

    def active_execution_contexts(self):
        return []


def test_build_runtime_controls_registers_and_starts_account_risk_projection() -> None:
    container = AppContainer()
    container.runtime_identity = _executor_identity()
    container.storage_writer = _StorageWriter()
    container.pipeline_trace_recorder = _StartStopComponent()
    container.trade_module = _TradeModule()
    container.trade_executor = _TradeExecutorStatus()
    container.execution_intent_consumer = _StartStopComponent()
    container.pending_entry_manager = _PendingEntryManagerStatus()
    container.position_manager = _PositionManagerStatus()
    container.trading_state_recovery = None
    container.market_service = None

    build_runtime_controls(
        container,
        signal_config_loader=lambda: SimpleNamespace(position_reconcile_interval=10),
    )

    projector_component = container.runtime_component_registry.get(
        "account_risk_state_projection"
    )
    assert projector_component is not None
    assert RuntimeMode.FULL.value in projector_component.supported_modes

    container.runtime_mode_controller.start()

    assert container.account_risk_state_projector is not None
    assert container.account_risk_state_projector.is_running() is True
