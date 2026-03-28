from __future__ import annotations

from src.readmodels.runtime import RuntimeReadModel


class DummyHealthMonitor:
    def generate_report(self, hours: int):
        return {"status": "ok", "hours": hours}


class DummyIngestor:
    def queue_stats(self):
        return {
            "summary": {"total": 1, "high": 0, "critical": 0, "full": 0},
            "queues": {
                "ticks": {
                    "status": "normal",
                    "utilization_pct": 10.0,
                    "pending": 1,
                }
            },
            "threads": {"writer_alive": True},
        }


class DummyIndicatorManager:
    def get_performance_stats(self):
        return {
            "event_loop_running": True,
            "failed_computations": 0,
            "event_store": {"pending": 1, "failed": 0, "retrying": 0},
            "pipeline": {"cache": {"size": 1}},
            "timestamp": "2026-01-01T00:00:00+00:00",
        }


class DummyTradingService:
    def health(self):
        return {"balance": 10000, "equity": 10000}

    def monitoring_summary(self, hours: int = 24):
        return {
            "active_account_alias": "live",
            "accounts": ["live"],
            "daily": {"risk": {"blocked": 0}},
            "summary": [],
            "recent": [],
        }


class DummySignalRuntime:
    def status(self):
        return {
            "running": True,
            "target_count": 2,
            "trigger_mode": {"confirmed_snapshot": True, "intrabar": False},
            "confirmed_queue_size": 1,
            "confirmed_queue_capacity": 32,
            "intrabar_queue_size": 0,
            "intrabar_queue_capacity": 16,
            "processed_events": 12,
            "dropped_events": 0,
            "confirmed_backpressure_failures": 0,
            "warmup_ready": True,
            "active_preview_states": 1,
            "active_confirmed_states": 1,
        }


class DummyTradeExecutor:
    def status(self):
        return {
            "enabled": True,
            "signals_received": 3,
            "signals_passed": 2,
            "signals_blocked": 1,
            "execution_count": 2,
            "last_execution_at": None,
            "execution_quality": {"risk_blocks": 1},
            "circuit_breaker": {"open": False, "consecutive_failures": 0},
            "pending_entries": {"active_count": 1},
        }


class DummyPositionManager:
    def status(self):
        return {
            "running": True,
            "tracked_positions": 2,
            "reconcile_interval": 30,
            "reconcile_count": 5,
            "last_reconcile_at": "2026-01-01T00:00:00+00:00",
            "margin_guard": {"armed": True},
        }

    def active_positions(self):
        return [{"ticket": 1}, {"ticket": 2}]


class DummyPendingEntryManager:
    def status(self):
        return {
            "active_count": 1,
            "entries": [{"signal_id": "sig_1"}],
            "stats": {"total_submitted": 2, "fill_rate": 0.5},
        }


def test_health_report_contains_unified_runtime_sections() -> None:
    read_model = RuntimeReadModel(
        health_monitor=DummyHealthMonitor(),
        ingestor=DummyIngestor(),
        indicator_manager=DummyIndicatorManager(),
        trading_service=DummyTradingService(),
    )

    report = read_model.health_report(hours=6)

    assert report["status"] == "ok"
    assert report["hours"] == 6
    assert report["runtime"]["storage"]["status"] == "healthy"
    assert report["runtime"]["indicators"]["status"] == "healthy"
    assert report["runtime"]["trading"]["active_account_alias"] == "live"


def test_dashboard_overview_uses_unified_projection_shape() -> None:
    read_model = RuntimeReadModel(
        ingestor=DummyIngestor(),
        indicator_manager=DummyIndicatorManager(),
        trading_service=DummyTradingService(),
        signal_runtime=DummySignalRuntime(),
        trade_executor=DummyTradeExecutor(),
        position_manager=DummyPositionManager(),
        pending_entry_manager=DummyPendingEntryManager(),
    )

    overview = read_model.dashboard_overview(
        {
            "ready": True,
            "phase": "running",
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:05+00:00",
        }
    )

    assert overview["system"]["status"] == "healthy"
    assert overview["positions"]["count"] == 2
    assert overview["executor"]["pending_entries_count"] == 1
    assert overview["signals"]["running"] is True
    assert overview["signals"]["status"] == "healthy"
    assert overview["positions"]["manager"]["tracked_positions"] == 2


def test_runtime_trade_and_position_projections_are_normalized() -> None:
    read_model = RuntimeReadModel(
        signal_runtime=DummySignalRuntime(),
        trade_executor=DummyTradeExecutor(),
        position_manager=DummyPositionManager(),
        pending_entry_manager=DummyPendingEntryManager(),
    )

    signal_runtime = read_model.signal_runtime_summary()
    executor = read_model.trade_executor_summary()
    positions = read_model.tracked_positions_payload(limit=10)
    pending = read_model.pending_entries_summary()

    assert signal_runtime["queues"]["confirmed"]["size"] == 1
    assert executor["signals"]["blocked"] == 1
    assert positions["manager"]["reconcile"]["count"] == 5
    assert pending["entries"][0]["signal_id"] == "sig_1"
