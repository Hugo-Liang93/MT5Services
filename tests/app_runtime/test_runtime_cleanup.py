from __future__ import annotations

from types import SimpleNamespace

from src.app_runtime.container import AppContainer
from src.app_runtime.runtime import AppRuntime


def test_runtime_stop_runs_shutdown_callbacks_and_closes_file_manager(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr("src.app_runtime.runtime.close_file_config_manager", lambda: calls.append("file_manager"))
    monkeypatch.setattr("src.app_runtime.runtime.close_event_store", lambda **kwargs: calls.append("event_store"))
    monkeypatch.setattr("src.app_runtime.runtime.close_monitoring_manager", lambda **kwargs: calls.append("monitoring"))
    monkeypatch.setattr("src.app_runtime.runtime.close_health_monitor", lambda **kwargs: calls.append("health"))

    container = AppContainer()
    container.shutdown_callbacks.append(lambda: calls.append("callback"))
    container.health_monitor = object()
    container.monitoring_manager = object()
    container.indicator_manager = SimpleNamespace(event_store=object())

    runtime = AppRuntime(container)
    runtime.stop()

    assert calls == ["callback", "event_store", "monitoring", "health", "file_manager"]
    assert container.shutdown_callbacks == []
