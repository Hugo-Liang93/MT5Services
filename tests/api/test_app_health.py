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


class DummyRuntimeReadModel:
    def __init__(self, *, instance_role: str = "main") -> None:
        self.runtime_identity = type(
            "RuntimeIdentity",
            (),
            {"instance_role": instance_role},
        )()

    def storage_summary(self):
        return {
            "status": "healthy",
            "threads": {"writer_alive": True, "ingest_alive": True},
            "summary": {},
        }

    def indicator_summary(self):
        return {"status": "healthy", "event_loop_running": True}

    def signal_runtime_summary(self):
        return {"status": "healthy", "running": True}

    def trade_executor_summary(self):
        return {"status": "healthy", "enabled": True}

    def pending_entries_summary(self):
        return {"status": "healthy"}

    def position_manager_summary(self):
        return {"status": "healthy", "running": True}


class DummyCalendarService:
    def stats(self):
        return {"running": True, "last_refresh_status": "ok"}


def test_health_uses_trading_module_health(monkeypatch):
    monkeypatch.setattr("src.api.deps.get_ingestor", lambda: DummyIngestor())
    monkeypatch.setattr("src.api.deps.get_economic_calendar_service", lambda: DummyCalendarService())
    monkeypatch.setattr("src.api.deps.get_runtime_mode", lambda: "FULL")
    monkeypatch.setattr(
        "src.api.deps.get_runtime_read_model",
        lambda: DummyRuntimeReadModel(instance_role="main"),
    )

    response = health(
        service=DummyMarketService(),
        trading=DummyTradingModule(),
        runtime_read_model=DummyRuntimeReadModel(instance_role="main"),
    )

    assert response.success is True
    assert response.data["market"]["connected"] is True
    assert response.data["trading"]["account_alias"] == "live"
    assert response.data["trading"]["login"] == 1001
    assert response.data["runtime"]["components"]["indicator_engine"]["running"] is True
    assert response.data["runtime"]["components"]["signal_runtime"]["running"] is True
    assert response.data["runtime"]["components"]["economic_calendar"]["running"] is True


def test_health_executor_uses_runtime_read_model_without_ingestor(monkeypatch):
    monkeypatch.setattr("src.api.deps.get_economic_calendar_service", lambda: DummyCalendarService())
    monkeypatch.setattr("src.api.deps.get_runtime_mode", lambda: "FULL")

    response = health(
        service=DummyMarketService(),
        trading=DummyTradingModule(),
        runtime_read_model=DummyRuntimeReadModel(instance_role="executor"),
    )

    assert response.success is True
    assert response.data["ingestor"]["queues"]["role"] == "executor"
    assert response.data["runtime"]["components"]["ingestor"]["status"] == "disabled"
    assert response.data["runtime"]["components"]["indicator_engine"]["status"] == "healthy"
