from __future__ import annotations

from types import SimpleNamespace

from src.api import deps


def test_shutdown_initialized_runtime_resets_globals(monkeypatch) -> None:
    state = {"stopped": 0}

    monkeypatch.setattr(deps, "_runtime", SimpleNamespace(stop=lambda: state.__setitem__("stopped", 1)))
    monkeypatch.setattr(deps, "_container", object())

    deps._shutdown_initialized_runtime()

    assert state["stopped"] == 1
    assert deps._runtime is None
    assert deps._container is None
