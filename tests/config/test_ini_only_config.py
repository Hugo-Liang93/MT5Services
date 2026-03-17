from __future__ import annotations

from src.config.centralized import get_api_config, get_config_provenance_snapshot, reload_configs


def test_api_config_reads_from_ini_only(monkeypatch):
    monkeypatch.setenv("MT5_API_HOST", "127.0.0.1")
    monkeypatch.setenv("MT5_API_PORT", "8810")
    monkeypatch.setenv("MT5_API_KEY", "env-secret")

    reload_configs()

    try:
        api = get_api_config()
        provenance = get_config_provenance_snapshot()

        assert api.host == "0.0.0.0"
        assert api.port == 8808
        assert provenance["api"]["host"] == "market.ini[api].host"
        assert provenance["api"]["port"] == "market.ini[api].port"
        assert provenance["api"]["api_key"] == "market.ini[security].api_key"
    finally:
        monkeypatch.delenv("MT5_API_HOST", raising=False)
        monkeypatch.delenv("MT5_API_PORT", raising=False)
        monkeypatch.delenv("MT5_API_KEY", raising=False)
        reload_configs()
