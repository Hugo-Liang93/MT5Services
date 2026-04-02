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

    def active_execution_contexts(self):
        return [
            {
                "signal_id": "sig_1",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "sma_trend",
                "direction": "buy",
                "source": "pending_entry",
            },
            {
                "signal_id": "sig_2",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "sma_trend",
                "direction": "buy",
                "source": "mt5_order",
            },
        ]


class DummyTradingStateStore:
    def load_trade_control_state(self):
        return {
            "auto_entry_enabled": False,
            "close_only_mode": True,
            "reason": "persisted",
        }

    def list_pending_order_states(self, *, statuses=None, limit=100):
        rows = [
            {"order_ticket": 1, "status": "placed", "symbol": "XAUUSD"},
            {"order_ticket": 2, "status": "orphan", "symbol": "XAUUSD"},
            {"order_ticket": 3, "status": "filled", "symbol": "XAUUSD"},
            {"order_ticket": 4, "status": "expired", "symbol": "XAUUSD"},
        ]
        if statuses:
            rows = [row for row in rows if row["status"] in set(statuses)]
        return rows[:limit]

    def list_position_runtime_states(self, *, statuses=None, limit=100):
        rows = [
            {"position_ticket": 11, "status": "open", "symbol": "XAUUSD"},
            {"position_ticket": 12, "status": "closed", "symbol": "XAUUSD"},
        ]
        if statuses:
            rows = [row for row in rows if row["status"] in set(statuses)]
        return rows[:limit]


class DummyTradingStateAlerts:
    def summary(self):
        return {
            "status": "warning",
            "alerts": [{"code": "pending_orphan", "severity": "warning"}],
            "summary": [{"code": "pending_orphan", "status": "failed"}],
            "observed": {"active_pending_count": 2},
        }


class DummyRuntimeModeController:
    def __init__(self, current_mode: str = "full") -> None:
        self._current_mode = current_mode

    def snapshot(self):
        return {
            "current_mode": self._current_mode,
            "configured_mode": "full",
            "after_eod_action": "risk_off",
            "auto_check_interval_seconds": 15.0,
            "components": {"trade_listener_attached": self._current_mode == "full"},
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


def test_runtime_trading_state_projection_is_normalized() -> None:
    read_model = RuntimeReadModel(
        pending_entry_manager=DummyPendingEntryManager(),
        trading_state_store=DummyTradingStateStore(),
        trading_state_alerts=DummyTradingStateAlerts(),
        runtime_mode_controller=DummyRuntimeModeController(current_mode="observe"),
    )

    summary = read_model.trading_state_summary(pending_limit=10, position_limit=10)

    assert summary["trade_control"]["close_only_mode"] is True
    assert summary["runtime_mode"]["current_mode"] == "observe"
    assert summary["pending"]["active"]["status_counts"]["placed"] == 1
    assert summary["pending"]["active"]["status_counts"]["orphan"] == 1
    assert summary["pending"]["lifecycle"]["status_counts"]["filled"] == 1
    assert summary["pending"]["lifecycle"]["status_counts"]["expired"] == 1
    assert summary["pending"]["execution_contexts"]["source_counts"]["pending_entry"] == 1
    assert summary["pending"]["execution_contexts"]["source_counts"]["mt5_order"] == 1
    assert summary["positions"]["status_counts"]["open"] == 1
    assert summary["alerts"]["status"] == "warning"


def test_runtime_indicator_summary_marks_disabled_when_mode_intentionally_stopped() -> None:
    read_model = RuntimeReadModel(
        indicator_manager=type(
            "IndicatorManager",
            (),
            {
                "get_performance_stats": staticmethod(
                    lambda: {
                        "event_loop_running": False,
                        "failed_computations": 0,
                        "event_store": {"pending": 0, "failed": 0, "retrying": 0},
                        "pipeline": {"cache": {}},
                    }
                )
            },
        )(),
        runtime_mode_controller=DummyRuntimeModeController(current_mode="risk_off"),
    )

    summary = read_model.indicator_summary()

    assert summary["status"] == "disabled"
