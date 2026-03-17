from __future__ import annotations

import pytest

from app import APP_TARGET, launch, resolve_runtime_target


def test_resolve_target_uses_defaults():
    target, host, port = resolve_runtime_target()

    assert target == APP_TARGET
    assert isinstance(host, str)
    assert isinstance(port, int)


def test_resolve_target_ignores_env_overrides(monkeypatch):
    monkeypatch.setenv("MT5_API_HOST", "127.0.0.1")
    monkeypatch.setenv("MT5_API_PORT", "9900")

    target, host, port = resolve_runtime_target()

    assert target == APP_TARGET
    assert host == "0.0.0.0"
    assert port == 8808


def test_launch_invokes_uvicorn(monkeypatch):
    calls = {}
    pytest.importorskip("uvicorn")

    def fake_run(target, host, port, reload, access_log):
        calls.update(
            {
                "target": target,
                "host": host,
                "port": port,
                "reload": reload,
                "access_log": access_log,
            }
        )

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    launch()

    assert calls["target"] == APP_TARGET
    assert calls["reload"] is False
