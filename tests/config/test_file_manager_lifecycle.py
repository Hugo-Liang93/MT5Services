from __future__ import annotations

from types import SimpleNamespace

import src.config.file_manager as file_manager
from src.app_runtime.factories.signals import register_signal_hot_reload


def test_register_signal_hot_reload_returns_cleanup(monkeypatch) -> None:
    callbacks: list[object] = []

    manager = SimpleNamespace(
        register_change_callback=lambda callback: callbacks.append(callback),
        unregister_change_callback=lambda callback: callbacks.remove(callback),
    )
    monkeypatch.setattr("src.app_runtime.factories.signals.get_file_config_manager", lambda: manager)

    runtime = SimpleNamespace(update_policy=lambda policy: None, filter_chain=None)

    cleanup = register_signal_hot_reload(
        runtime,
        signal_config_loader=lambda: SimpleNamespace(
            market_structure_enabled=False,
            market_structure_lookback_bars=10,
            market_structure_m1_lookback_bars=10,
            market_structure_open_range_minutes=15,
            market_structure_compression_window_bars=5,
            market_structure_reference_window_bars=10,
        ),
    )

    assert len(callbacks) == 1
    cleanup()
    assert callbacks == []


def test_close_file_config_manager_resets_singleton(monkeypatch) -> None:
    calls: list[str] = []
    fake_manager = SimpleNamespace(stop=lambda: calls.append("stopped"))
    monkeypatch.setattr(file_manager, "_config_manager_instance", fake_manager)

    file_manager.close_file_config_manager()

    assert calls == ["stopped"]
    assert file_manager._config_manager_instance is None
