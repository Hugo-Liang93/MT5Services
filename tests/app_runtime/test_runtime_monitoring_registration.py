from __future__ import annotations

from types import SimpleNamespace

from src.app_runtime.runtime import AppRuntime


class _MonitoringManager:
    def __init__(self) -> None:
        self.components: list[tuple[str, object, list[str], bool]] = []
        self.started = False

    def register_component(
        self,
        name: str,
        component_obj: object,
        check_methods: list[str],
        *,
        trading_only: bool = False,
    ) -> None:
        self.components.append((name, component_obj, check_methods, trading_only))

    def start(self) -> None:
        self.started = True


def test_executor_runtime_registers_command_consumers_for_monitoring() -> None:
    manager = _MonitoringManager()
    container = SimpleNamespace(
        monitoring_manager=manager,
        runtime_identity=SimpleNamespace(instance_role="executor"),
        trade_module=object(),
        position_manager=None,
        trade_executor=None,
        trading_state_alerts=None,
        pending_entry_manager=None,
        account_risk_state_projector=None,
        execution_intent_consumer=object(),
        operator_command_consumer=object(),
        recovery_runner=object(),
        health_monitor=None,
    )
    runtime = object.__new__(AppRuntime)
    runtime.container = container
    runtime._status = {"steps": {}}
    runtime._mark_step = lambda *args, **kwargs: None
    runtime._record_task_status = lambda *args, **kwargs: None

    AppRuntime._register_monitoring(runtime)

    registered = {name: methods for name, _obj, methods, _flag in manager.components}
    assert registered["execution_intent_consumer"] == ["status"]
    assert registered["operator_command_consumer"] == ["status"]
    assert registered["recovery_runner"] == ["status"]
    assert manager.started is True
