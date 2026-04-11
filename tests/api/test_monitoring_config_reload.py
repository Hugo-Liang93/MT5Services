from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException

from src.api.monitoring import trigger_config_reload


def test_trigger_config_reload_clears_caches_and_reloads_file(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "src.api.monitoring_routes.runtime.reload_configs",
        lambda: calls.append("reload_configs"),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.runtime.get_file_config_manager",
        lambda: SimpleNamespace(
            reload=lambda filename: calls.append(f"file:{filename}") or True
        ),
    )

    response = asyncio.run(trigger_config_reload("signal.ini"))

    assert response.success is True
    assert response.data["success"] is True
    assert response.data["reloaded"] == "signal.ini"
    assert response.data["cache_cleared"] is True
    assert calls == ["reload_configs", "file:signal.ini"]


def test_trigger_config_reload_raises_when_file_missing(monkeypatch) -> None:
    monkeypatch.setattr("src.api.monitoring_routes.runtime.reload_configs", lambda: None)
    monkeypatch.setattr(
        "src.api.monitoring_routes.runtime.get_file_config_manager",
        lambda: SimpleNamespace(reload=lambda filename: False),
    )

    try:
        asyncio.run(trigger_config_reload("missing.ini"))
    except HTTPException as exc:
        assert exc.status_code == 404
        assert exc.detail == "config file not found: missing.ini"
    else:
        raise AssertionError("expected trigger_config_reload to fail for missing file")
