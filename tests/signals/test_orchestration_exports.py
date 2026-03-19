from __future__ import annotations

from src.signals.orchestration import SignalRuntime, SignalTarget


def test_orchestration_exports_runtime_symbols() -> None:
    assert SignalRuntime is not None
    assert SignalTarget is not None
