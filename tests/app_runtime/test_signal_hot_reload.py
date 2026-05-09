from __future__ import annotations

from types import SimpleNamespace

from src.app_runtime.factories.signals import register_signal_hot_reload


class DummyConfigManager:
    def __init__(self) -> None:
        self.callback = None

    def register_change_callback(self, callback):
        self.callback = callback

    def unregister_change_callback(self, callback):
        if self.callback is callback:
            self.callback = None


def test_signal_hot_reload_refreshes_ingestor_market_data_dependencies(monkeypatch) -> None:
    manager = DummyConfigManager()
    monkeypatch.setattr(
        "src.app_runtime.factories.signals.get_file_config_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "src.app_runtime.factories.signals.build_signal_policy",
        lambda signal_config: "policy",
    )
    monkeypatch.setattr(
        "src.app_runtime.factories.signals.build_economic_decay_service",
        lambda economic_calendar_service, economic_config: "decay",
    )
    monkeypatch.setattr(
        "src.app_runtime.factories.signals.build_signal_filter_chain",
        lambda signal_config, economic_decay_service: "filter_chain",
    )

    class RuntimeStub:
        def __init__(self) -> None:
            self.policies = []
            self.decay_services = []
            self.filter_chain = None

        def update_policy(self, policy):
            self.policies.append(policy)

        def set_economic_decay_service(self, service):
            self.decay_services.append(service)

        def required_market_data_lanes(self):
            return ("tick:XAUUSD",)

    ingestor_calls: list[tuple[str, ...]] = []
    ingestor = SimpleNamespace(
        set_required_market_data_lanes=lambda lanes: ingestor_calls.append(tuple(lanes))
    )
    runtime = RuntimeStub()

    cleanup = register_signal_hot_reload(
        runtime,
        lambda: SimpleNamespace(),
        economic_config_loader=lambda: SimpleNamespace(),
        ingestor=ingestor,
    )
    manager.callback("signal.ini")

    assert runtime.policies == ["policy"]
    assert runtime.decay_services == ["decay"]
    assert runtime.filter_chain == "filter_chain"
    assert ingestor_calls == [("tick:XAUUSD",)]

    cleanup()
    assert manager.callback is None
