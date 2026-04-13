from __future__ import annotations

from src.signals.orchestration.runtime import SignalRuntime, SignalTarget


def test_orchestration_runtime_module_exports_runtime_symbols() -> None:
    assert SignalRuntime is not None
    assert SignalTarget is not None
