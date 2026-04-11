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
        return {"ticks": {"pending": 0}, "threads": {"ingest_alive": True, "writer_alive": True}}


class DummyIndicatorManager:
    def get_performance_stats(self):
        return {"event_loop_running": True}


class DummySignalRuntime:
    def is_running(self):
        return True


class DummyExecutor:
    def is_running(self):
        return True


class DummyPendingManager:
    def is_running(self):
        return True


class DummyPositionManager:
    def is_running(self):
        return True


class DummyCalendarService:
    def stats(self):
        return {"running": True, "last_refresh_status": "ok"}


def test_health_uses_trading_module_health(monkeypatch):
    monkeypatch.setattr("src.api.deps.get_ingestor", lambda: DummyIngestor())
    monkeypatch.setattr("src.api.deps.get_indicator_manager", lambda: DummyIndicatorManager())
    monkeypatch.setattr("src.api.deps.get_signal_runtime", lambda: DummySignalRuntime())
    monkeypatch.setattr("src.api.deps.get_trade_executor", lambda: DummyExecutor())
    monkeypatch.setattr("src.api.deps.get_pending_entry_manager", lambda: DummyPendingManager())
    monkeypatch.setattr("src.api.deps.get_position_manager", lambda: DummyPositionManager())
    monkeypatch.setattr("src.api.deps.get_economic_calendar_service", lambda: DummyCalendarService())
    monkeypatch.setattr("src.api.deps.get_runtime_mode", lambda: "FULL")

    response = health(service=DummyMarketService(), trading=DummyTradingModule())

    assert response.success is True
    assert response.data["market"]["connected"] is True
    assert response.data["trading"]["account_alias"] == "live"
    assert response.data["trading"]["login"] == 1001
    assert response.data["runtime"]["components"]["indicator_engine"]["running"] is True
    assert response.data["runtime"]["components"]["signal_runtime"]["running"] is True
    assert response.data["runtime"]["components"]["economic_calendar"]["running"] is True
