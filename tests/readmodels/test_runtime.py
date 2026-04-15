from __future__ import annotations

from datetime import datetime, timezone

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
            "threads": {"writer_alive": True, "ingest_alive": True},
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


class DummyMT5SessionState:
    def to_dict(self):
        return {
            "terminal_reachable": True,
            "terminal_process_ready": True,
            "ipc_ready": True,
            "authorized": True,
            "account_match": True,
            "session_ready": True,
            "interactive_login_required": False,
            "error_code": None,
            "error_message": None,
            "last_error": {"code": None, "message": None},
        }


class DummyMarketClient:
    def inspect_session_state(self, **kwargs):
        return DummyMT5SessionState()


class DummyMarketService:
    def __init__(self) -> None:
        self.client = DummyMarketClient()


class DummyPaperTradingBridge:
    def status(self):
        return {
            "running": True,
            "session_id": "ps_test_1",
            "started_at": "2026-01-01T00:00:00+00:00",
            "signals_received": 7,
            "signals_executed": 3,
            "signals_rejected": 4,
            "reject_reasons": {"low_confidence": 2, "no_quote": 2},
            "active_symbols": ["XAUUSD"],
            "current_balance": 10050.0,
            "floating_pnl": 12.5,
            "equity": 10062.5,
            "open_positions": 1,
            "closed_trades": 3,
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
            "active_confirmed_states": 1,
            "filter_by_scope": {
                "confirmed": {"blocked": 1, "passed": 2},
                "intrabar": {"blocked": 0, "passed": 0},
            },
            "filter_window_by_scope": {"confirmed": {"blocked": 1, "passed": 1}},
            "filter_window_seconds": 300,
            "filter_window_elapsed": 45,
            "filter_realtime_status": {
                "session": {"enabled": True, "blocked": 1},
                "spread": {"enabled": True, "blocked": 0},
            },
            "intrabar_runtime_slos": {
                "drop_rates": {"intrabar_queue_drop_vs_arrived_pct": 0.4},
                "queue": {"size": 0, "capacity": 16, "dropped_total": 2},
                "slo_ms": {"queue_age_p95": 120.0, "processing_latency_p95": 45.0},
                "sample_counts": {"queue_age_sample_count": 20, "processing_latency_sample_count": 20},
                "latest": {"queue_age_ms": 80.0, "processing_latency_ms": 35.0},
            },
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
            "execution_gate": {
                "intrabar_trading_enabled": True,
                "intrabar_enabled_strategies": ["structured_breakout_follow"],
            },
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


class DummyTradingStateStoreWithDatetime:
    def load_trade_control_state(self):
        return {
            "auto_entry_enabled": False,
            "close_only_mode": True,
            "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "request_context": {"source": "test"},
        }


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
            "after_eod_action": "ingest_only",
            "auto_check_interval_seconds": 15.0,
            "components": {"trade_execution": self._current_mode == "full"},
        }


class DummyExposureCloseoutController:
    def status(self):
        return {
            "status": "completed",
            "last_reason": "end_of_day",
            "last_comment": "end_of_day_closeout",
            "last_requested_at": "2026-01-01T00:00:00+00:00",
            "last_completed_at": "2026-01-01T00:00:00+00:00",
            "result": {
                "completed": True,
                "positions": {"completed": [101], "failed": [], "requested": [101], "error": None},
                "orders": {"completed": [201], "failed": [], "requested": [201], "error": None},
                "remaining_positions": [],
                "remaining_orders": [],
            },
        }


class DummyRuntimeIdentity:
    def __init__(
        self,
        instance_role: str = "main",
        *,
        instance_id: str = "live-main",
        account_key: str = "live_main",
        live_topology_mode: str = "single_account",
    ) -> None:
        self.instance_role = instance_role
        self.instance_id = instance_id
        self.account_key = account_key
        self.live_topology_mode = live_topology_mode


class DummyPipelineTraceWriter:
    def __init__(self) -> None:
        self.last_kwargs = None

    def fetch_pipeline_trace_filtered(self, **kwargs):
        self.last_kwargs = dict(kwargs)
        return [
            {
                "id": 2,
                "trace_id": "trace_2",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "scope": "confirmed",
                "event_type": "command_completed",
                "recorded_at": datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
                "payload": {"status": "completed"},
                "instance_id": kwargs.get("instance_id"),
                "instance_role": "executor",
                "account_key": kwargs.get("account_key"),
                "signal_id": "sig_2",
                "intent_id": "intent_2",
                "command_id": "cmd_2",
                "action_id": "act_2",
            },
            {
                "id": 1,
                "trace_id": "trace_1",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "scope": "confirmed",
                "event_type": "admission_report_appended",
                "recorded_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
                "payload": {"decision": "allow"},
                "instance_id": kwargs.get("instance_id"),
                "instance_role": "executor",
                "account_key": kwargs.get("account_key"),
                "signal_id": "sig_1",
                "intent_id": "intent_1",
                "command_id": None,
                "action_id": None,
            },
        ]


class DummyExecutorIngestor:
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
            "threads": {"writer_alive": True, "ingest_alive": False},
        }


class DummyStorageWriter:
    def __init__(self, *, writer_alive: bool = True) -> None:
        self._writer_alive = writer_alive

    def is_running(self) -> bool:
        return self._writer_alive

    def stats(self):
        return {
            "summary": {"total": 2, "high": 0, "critical": 0, "full": 0},
            "queues": {
                "quotes": {
                    "status": "normal",
                    "utilization_pct": 5.0,
                    "pending": 0,
                }
            },
            "threads": {"writer_alive": self._writer_alive},
        }


def test_health_report_contains_unified_runtime_sections() -> None:
    read_model = RuntimeReadModel(
        health_monitor=DummyHealthMonitor(),
        market_service=DummyMarketService(),
        ingestor=DummyIngestor(),
        indicator_manager=DummyIndicatorManager(),
        trading_queries=DummyTradingService(),
    )

    report = read_model.health_report(hours=6)

    assert report["status"] == "ok"
    assert report["hours"] == 6
    assert report["runtime"]["storage"]["status"] == "healthy"
    assert report["runtime"]["indicators"]["status"] == "healthy"
    assert report["runtime"]["trading"]["active_account_alias"] == "live"
    assert report["runtime"]["external_dependencies"]["mt5_session"]["status"] == "healthy"


def test_persisted_trade_control_payload_normalizes_datetime_values() -> None:
    read_model = RuntimeReadModel(
        trading_state_store=DummyTradingStateStoreWithDatetime(),
    )

    payload = read_model.persisted_trade_control_payload()

    assert payload is not None
    assert payload["updated_at"] == "2026-01-01T00:00:00+00:00"
    assert payload["request_context"]["source"] == "test"


def test_dashboard_overview_uses_unified_projection_shape() -> None:
    read_model = RuntimeReadModel(
        market_service=DummyMarketService(),
        ingestor=DummyIngestor(),
        indicator_manager=DummyIndicatorManager(),
        trading_queries=DummyTradingService(),
        signal_runtime=DummySignalRuntime(),
        trade_executor=DummyTradeExecutor(),
        position_manager=DummyPositionManager(),
        pending_entry_manager=DummyPendingEntryManager(),
        paper_trading_bridge=DummyPaperTradingBridge(),
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
    assert overview["validation"]["paper_trading"]["kind"] == "validation_sidecar"
    assert overview["validation"]["paper_trading"]["running"] is True
    assert overview["validation"]["paper_trading"]["signals_received"] == 7
    assert overview["external_dependencies"]["mt5_session"]["connected"] is True


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
    assert signal_runtime["intrabar_runtime_slos"]["queue"]["dropped_total"] == 2
    assert signal_runtime["executor_enabled"] is True
    assert signal_runtime["execution_gate"]["intrabar_enabled_strategies"] == [
        "structured_breakout_follow"
    ]
    assert signal_runtime["execution_gate"]["intrabar_trading_enabled"] is True
    assert signal_runtime["active_filters"] == ["session", "spread"]
    assert signal_runtime["filter_stats"]["totals"]["confirmed"]["blocked"] == 1
    assert executor["signals"]["blocked"] == 1
    assert positions["manager"]["reconcile"]["count"] == 5
    assert pending["entries"][0]["signal_id"] == "sig_1"


def test_runtime_trading_state_projection_is_normalized() -> None:
    read_model = RuntimeReadModel(
        pending_entry_manager=DummyPendingEntryManager(),
        trading_state_store=DummyTradingStateStore(),
        trading_state_alerts=DummyTradingStateAlerts(),
        exposure_closeout_controller=DummyExposureCloseoutController(),
        runtime_mode_controller=DummyRuntimeModeController(current_mode="observe"),
        paper_trading_bridge=DummyPaperTradingBridge(),
    )

    summary = read_model.trading_state_summary(pending_limit=10, position_limit=10)

    assert summary["trade_control"]["close_only_mode"] is True
    assert summary["runtime_mode"]["current_mode"] == "observe"
    assert summary["closeout"]["status"] == "completed"
    assert summary["closeout"]["result"]["orders"]["completed"] == [201]
    assert summary["pending"]["active"]["status_counts"]["placed"] == 1
    assert summary["pending"]["active"]["status_counts"]["orphan"] == 1
    assert summary["pending"]["lifecycle"]["status_counts"]["filled"] == 1
    assert summary["pending"]["lifecycle"]["status_counts"]["expired"] == 1
    assert summary["pending"]["execution_contexts"]["source_counts"]["pending_entry"] == 1
    assert summary["pending"]["execution_contexts"]["source_counts"]["mt5_order"] == 1
    assert summary["positions"]["status_counts"]["open"] == 1
    assert summary["alerts"]["status"] == "warning"
    assert summary["validation"]["paper_trading"]["kind"] == "validation_sidecar"
    assert summary["validation"]["paper_trading"]["signals_executed"] == 3


def test_runtime_mode_summary_exposes_validation_sidecars() -> None:
    read_model = RuntimeReadModel(
        runtime_mode_controller=DummyRuntimeModeController(current_mode="full"),
        paper_trading_bridge=DummyPaperTradingBridge(),
    )

    summary = read_model.runtime_mode_summary()

    assert summary["components"]["trade_execution"] is True
    assert summary["validation_sidecars"]["paper_trading"]["kind"] == "validation_sidecar"
    assert summary["validation_sidecars"]["paper_trading"]["status"] == "healthy"


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


def test_executor_runtime_health_views_mark_shared_compute_as_disabled() -> None:
    read_model = RuntimeReadModel(
        storage_writer=DummyStorageWriter(),
        trade_executor=DummyTradeExecutor(),
        runtime_identity=DummyRuntimeIdentity(instance_role="executor"),
    )

    storage = read_model.storage_summary()
    indicators = read_model.indicator_summary()
    signals = read_model.signal_runtime_summary()

    assert storage["status"] == "healthy"
    assert storage["threads"]["writer_alive"] is True
    assert storage["threads"]["ingest_alive"] is False
    assert storage["ingestion"] == "disabled"
    assert indicators["status"] == "disabled"
    assert signals["status"] == "disabled"
    assert signals["role"] == "executor"


def test_main_multi_account_runtime_views_mark_remote_execution_as_disabled() -> None:
    read_model = RuntimeReadModel(
        runtime_identity=DummyRuntimeIdentity(
            instance_role="main",
            live_topology_mode="multi_account",
        ),
    )

    executor = read_model.trade_executor_summary()
    pending = read_model.pending_entries_summary()
    positions = read_model.position_manager_summary()

    assert executor["status"] == "disabled"
    assert executor["state"] == "delegated"
    assert executor["execution_scope"] == "remote_executor"
    assert executor["configured"] is False
    assert pending["status"] == "disabled"
    assert pending["state"] == "delegated"
    assert pending["execution_scope"] == "remote_executor"
    assert pending["running"] is False
    assert positions["status"] == "disabled"
    assert positions["state"] == "delegated"
    assert positions["execution_scope"] == "remote_executor"


def test_recent_trade_pipeline_events_payload_scopes_to_runtime_identity() -> None:
    writer = DummyPipelineTraceWriter()
    read_model = RuntimeReadModel(
        db_writer=writer,
        runtime_identity=DummyRuntimeIdentity(
            instance_role="executor",
            instance_id="live-exec-a",
            account_key="live_exec_a",
        ),
    )

    payload = read_model.recent_trade_pipeline_events_payload(limit=5)

    assert writer.last_kwargs == {
        "instance_id": "live-exec-a",
        "account_key": "live_exec_a",
        "event_types": [
            "admission_report_appended",
            "intent_published",
            "intent_claimed",
            "intent_reclaimed",
            "intent_dead_lettered",
            "command_submitted",
            "command_claimed",
            "command_completed",
            "command_failed",
            "risk_state_changed",
            "unmanaged_position_detected",
        ],
        "limit": 5,
        "offset": 0,
    }
    assert payload["count"] == 2
    assert [item["id"] for item in payload["items"]] == [1, 2]
    assert payload["items"][0]["event_type"] == "admission_report_appended"
    assert payload["items"][0]["payload"]["decision"] == "allow"
    assert payload["items"][1]["event_type"] == "command_completed"
