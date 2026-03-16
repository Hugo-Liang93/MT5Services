from __future__ import annotations

from src.config.centralized import get_api_config, get_config_provenance_snapshot, reload_configs


def test_api_host_port_and_key_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("MT5_API_HOST", "127.0.0.1")
    monkeypatch.setenv("MT5_API_PORT", "8810")
    monkeypatch.setenv("MT5_API_KEY", "env-secret")

    reload_configs()

    try:
        api = get_api_config()
        provenance = get_config_provenance_snapshot()

        assert api.host == "127.0.0.1"
        assert api.port == 8810
        assert api.api_key == "env-secret"
        assert provenance["api"]["host"] == "env:MT5_API_HOST"
        assert provenance["api"]["port"] == "env:MT5_API_PORT"
        assert provenance["api"]["api_key"] == "env:MT5_API_KEY"
    finally:
        monkeypatch.delenv("MT5_API_HOST", raising=False)
        monkeypatch.delenv("MT5_API_PORT", raising=False)
        monkeypatch.delenv("MT5_API_KEY", raising=False)
        reload_configs()


def test_invalid_env_port_does_not_override_api_port(monkeypatch):
    monkeypatch.setenv("MT5_API_PORT", "invalid")

    reload_configs()

    try:
        api = get_api_config()
        provenance = get_config_provenance_snapshot()

        assert api.port == 8808
        assert provenance["api"]["port"] == "market.ini[api].port"
    finally:
        monkeypatch.delenv("MT5_API_PORT", raising=False)
        reload_configs()
