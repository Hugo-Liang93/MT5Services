from __future__ import annotations

import asyncio

from fastapi import HTTPException

from src.api.monitoring import health_ready


def test_health_ready_raises_when_critical_component_is_degraded(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_startup_status",
        lambda: {"ready": True, "phase": "running"},
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_runtime_read_model",
        lambda: type(
            "RuntimeReadModel",
            (),
            {
                "runtime_identity": type(
                    "RuntimeIdentity",
                    (),
                    {"instance_role": "main"},
                )(),
                "storage_summary": staticmethod(
                    lambda: {
                        "threads": {"writer_alive": True, "ingest_alive": False},
                        "summary": {},
                    }
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_indicator_manager",
        lambda: type(
            "IndicatorManager",
            (),
            {"get_performance_stats": lambda self: {"event_loop_running": True}},
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_ingestor",
        lambda: type(
            "Ingestor",
            (),
            {"health_snapshot": lambda self: {"status": "healthy", "stalled": False}},
        )(),
    )

    try:
        asyncio.run(health_ready())
    except HTTPException as exc:
        assert exc.status_code == 503
        assert exc.detail["status"] == "not_ready"
        assert "ingestion" in exc.detail["failed_checks"]
    else:
        raise AssertionError(
            "expected readiness probe to fail on degraded storage writer"
        )


def test_health_ready_returns_ready_when_checks_pass(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_startup_status",
        lambda: {"ready": True, "phase": "running"},
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_runtime_read_model",
        lambda: type(
            "RuntimeReadModel",
            (),
            {
                "runtime_identity": type(
                    "RuntimeIdentity",
                    (),
                    {"instance_role": "main"},
                )(),
                "storage_summary": staticmethod(
                    lambda: {
                        "threads": {"writer_alive": True, "ingest_alive": True},
                        "summary": {},
                    }
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_indicator_manager",
        lambda: type(
            "IndicatorManager",
            (),
            {"get_performance_stats": lambda self: {"event_loop_running": True}},
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_ingestor",
        lambda: type(
            "Ingestor",
            (),
            {"health_snapshot": lambda self: {"status": "healthy", "stalled": False}},
        )(),
    )

    payload = asyncio.run(health_ready())

    assert payload["status"] == "ready"
    assert payload["checks"] == {
        "storage_writer": "ok",
        "ingestion": "ok",
        "market_data_health": "ok",
        "indicator_engine": "ok",
    }


def test_health_ready_rejects_main_when_market_data_health_is_critical(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_startup_status",
        lambda: {"ready": True, "phase": "running"},
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_runtime_read_model",
        lambda: type(
            "RuntimeReadModel",
            (),
            {
                "runtime_identity": type(
                    "RuntimeIdentity",
                    (),
                    {"instance_role": "main"},
                )(),
                "storage_summary": staticmethod(
                    lambda: {
                        "threads": {"writer_alive": True, "ingest_alive": True},
                        "summary": {},
                    }
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_indicator_manager",
        lambda: type(
            "IndicatorManager",
            (),
            {"get_performance_stats": lambda self: {"event_loop_running": True}},
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_ingestor",
        lambda: type(
            "Ingestor",
            (),
            {
                "health_snapshot": lambda self: {
                    "status": "critical",
                    "mt5": {"circuit_open": True},
                    "freshness": {"stale_count": 1},
                }
            },
        )(),
    )

    try:
        asyncio.run(health_ready())
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "market_data_health" in exc.detail["failed_checks"]
        assert (
            exc.detail["details"]["market_data_health"]["mt5"]["circuit_open"] is True
        )
    else:
        raise AssertionError(
            "expected readiness probe to fail on critical market data health"
        )


def test_health_ready_rejects_when_tick_feature_health_blocks(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_startup_status",
        lambda: {"ready": True, "phase": "running"},
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_runtime_read_model",
        lambda: type(
            "RuntimeReadModel",
            (),
            {
                "runtime_identity": type(
                    "RuntimeIdentity",
                    (),
                    {"instance_role": "main"},
                )(),
                "storage_summary": staticmethod(
                    lambda: {
                        "threads": {"writer_alive": True, "ingest_alive": True},
                        "summary": {},
                    }
                ),
                "tick_feature_health_summary": staticmethod(
                    lambda: {
                        "status": "critical",
                        "blocking": True,
                        "blocked_symbols": ["XAUUSD"],
                    }
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_indicator_manager",
        lambda: type(
            "IndicatorManager",
            (),
            {"get_performance_stats": lambda self: {"event_loop_running": True}},
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_ingestor",
        lambda: type(
            "Ingestor",
            (),
            {"health_snapshot": lambda self: {"status": "healthy", "stalled": False}},
        )(),
    )

    try:
        asyncio.run(health_ready())
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "tick_feature_health" in exc.detail["failed_checks"]
        assert exc.detail["details"]["tick_feature_health"]["blocked_symbols"] == [
            "XAUUSD"
        ]
    else:
        raise AssertionError("expected readiness probe to fail on tick feature health")


def test_health_ready_rejects_when_recovery_runner_is_stalled(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_startup_status",
        lambda: {"ready": True, "phase": "running"},
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_runtime_read_model",
        lambda: type(
            "RuntimeReadModel",
            (),
            {
                "runtime_identity": type(
                    "RuntimeIdentity",
                    (),
                    {"instance_role": "main"},
                )(),
                "storage_summary": staticmethod(
                    lambda: {
                        "threads": {"writer_alive": True, "ingest_alive": True},
                        "summary": {},
                    }
                ),
                "recovery_runner_summary": staticmethod(
                    lambda: {
                        "enabled": True,
                        "running": True,
                        "stalled": True,
                        "stall_reasons": ["tick_feature_snapshot_stale"],
                    }
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_indicator_manager",
        lambda: type(
            "IndicatorManager",
            (),
            {"get_performance_stats": lambda self: {"event_loop_running": True}},
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_ingestor",
        lambda: type(
            "Ingestor",
            (),
            {"health_snapshot": lambda self: {"status": "healthy", "stalled": False}},
        )(),
    )

    try:
        asyncio.run(health_ready())
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "recovery_runner" in exc.detail["failed_checks"]
        assert exc.detail["details"]["recovery_runner"]["stall_reasons"] == [
            "tick_feature_snapshot_stale"
        ]
    else:
        raise AssertionError(
            "expected readiness probe to fail on stalled recovery runner"
        )


def test_health_ready_returns_executor_ready_when_account_runtime_checks_pass(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_startup_status",
        lambda: {"ready": True, "phase": "running"},
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_runtime_read_model",
        lambda: type(
            "RuntimeReadModel",
            (),
            {
                "runtime_identity": type(
                    "RuntimeIdentity",
                    (),
                    {"instance_role": "executor"},
                )(),
                "storage_summary": staticmethod(
                    lambda: {"threads": {"writer_alive": True}, "summary": {}}
                ),
                "account_risk_state_summary": staticmethod(
                    lambda: {"account_key": "live:test:1"}
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_pending_entry_manager",
        lambda: type("PendingEntryManager", (), {"is_running": lambda self: True})(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_position_manager",
        lambda: type("PositionManager", (), {"is_running": lambda self: True})(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_execution_intent_consumer",
        lambda: type(
            "ExecutionIntentConsumer", (), {"is_running": lambda self: True}
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_operator_command_consumer",
        lambda: type(
            "OperatorCommandConsumer", (), {"is_running": lambda self: True}
        )(),
    )

    payload = asyncio.run(health_ready())

    assert payload["status"] == "ready"
    assert payload["checks"] == {
        "storage_writer": "ok",
        "execution_intent_consumer": "ok",
        "operator_command_consumer": "ok",
        "pending_entry": "ok",
        "position_manager": "ok",
        "account_risk_state": "ok",
    }


def test_health_ready_rejects_executor_when_intent_consumer_stopped(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_startup_status",
        lambda: {"ready": True, "phase": "running"},
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_runtime_read_model",
        lambda: type(
            "RuntimeReadModel",
            (),
            {
                "runtime_identity": type(
                    "RuntimeIdentity",
                    (),
                    {"instance_role": "executor"},
                )(),
                "storage_summary": staticmethod(
                    lambda: {"threads": {"writer_alive": True}, "summary": {}}
                ),
                "account_risk_state_summary": staticmethod(
                    lambda: {"account_key": "live:test:1"}
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_pending_entry_manager",
        lambda: type("PendingEntryManager", (), {"is_running": lambda self: True})(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_position_manager",
        lambda: type("PositionManager", (), {"is_running": lambda self: True})(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_execution_intent_consumer",
        lambda: type(
            "ExecutionIntentConsumer", (), {"is_running": lambda self: False}
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_operator_command_consumer",
        lambda: type(
            "OperatorCommandConsumer", (), {"is_running": lambda self: True}
        )(),
    )

    try:
        asyncio.run(health_ready())
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "execution_intent_consumer" in exc.detail["failed_checks"]
    else:
        raise AssertionError(
            "expected readiness probe to fail when intent consumer is stopped"
        )


def test_health_ready_rejects_executor_when_consumer_is_stalled(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_startup_status",
        lambda: {"ready": True, "phase": "running"},
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_runtime_read_model",
        lambda: type(
            "RuntimeReadModel",
            (),
            {
                "runtime_identity": type(
                    "RuntimeIdentity",
                    (),
                    {"instance_role": "executor"},
                )(),
                "storage_summary": staticmethod(
                    lambda: {"threads": {"writer_alive": True}, "summary": {}}
                ),
                "account_risk_state_summary": staticmethod(
                    lambda: {"account_key": "live:test:1"}
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_pending_entry_manager",
        lambda: type("PendingEntryManager", (), {"is_running": lambda self: True})(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_position_manager",
        lambda: type("PositionManager", (), {"is_running": lambda self: True})(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_execution_intent_consumer",
        lambda: type(
            "ExecutionIntentConsumer",
            (),
            {
                "is_running": lambda self: True,
                "health_snapshot": lambda self: {
                    "running": True,
                    "stalled": True,
                    "stall_reasons": ["poll_stalled"],
                },
            },
        )(),
    )
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_operator_command_consumer",
        lambda: type(
            "OperatorCommandConsumer",
            (),
            {
                "is_running": lambda self: True,
                "health_snapshot": lambda self: {
                    "running": True,
                    "stalled": False,
                },
            },
        )(),
    )

    try:
        asyncio.run(health_ready())
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "execution_intent_consumer" in exc.detail["failed_checks"]
        assert exc.detail["details"]["execution_intent_consumer"]["stall_reasons"] == [
            "poll_stalled"
        ]
    else:
        raise AssertionError(
            "expected readiness probe to fail when consumer progress is stalled"
        )
