from __future__ import annotations

from src.api import health


class DummyMarketService:
    def health(self):
        return {"connected": True, "symbols": 3}


class DummyTradingModule:
    def health(self):
        return {"connected": True, "account_alias": "live", "login": 1001}


class DummyIngestor:
    def queue_stats(self):
        return {"ticks": {"pending": 0}}


def test_health_uses_trading_module_health(monkeypatch):
    monkeypatch.setattr("src.api.deps.get_ingestor", lambda: DummyIngestor())
    monkeypatch.setattr("src.api.deps.get_runtime_mode", lambda: "FULL")

    response = health(service=DummyMarketService(), trading=DummyTradingModule())

    assert response.success is True
    assert response.data["market"]["connected"] is True
    assert response.data["trading"]["account_alias"] == "live"
    assert response.data["trading"]["login"] == 1001
