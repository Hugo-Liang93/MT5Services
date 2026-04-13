from __future__ import annotations

import asyncio

from fastapi import HTTPException

from src.api.monitoring import health_ready


def test_health_ready_raises_when_critical_component_is_degraded(monkeypatch) -> None:
    monkeypatch.setattr("src.api.monitoring_routes.health.get_startup_status", lambda: {"ready": True, "phase": "running"})
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

    try:
        asyncio.run(health_ready())
    except HTTPException as exc:
        assert exc.status_code == 503
        assert exc.detail["status"] == "not_ready"
        assert "ingestion" in exc.detail["failed_checks"]
    else:
        raise AssertionError("expected readiness probe to fail on degraded storage writer")


def test_health_ready_returns_ready_when_checks_pass(monkeypatch) -> None:
    monkeypatch.setattr("src.api.monitoring_routes.health.get_startup_status", lambda: {"ready": True, "phase": "running"})
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

    payload = asyncio.run(health_ready())

    assert payload["status"] == "ready"
    assert payload["checks"] == {
        "storage_writer": "ok",
        "ingestion": "ok",
        "indicator_engine": "ok",
    }


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

    payload = asyncio.run(health_ready())

    assert payload["status"] == "ready"
    assert payload["checks"] == {
        "storage_writer": "ok",
        "pending_entry": "ok",
        "position_manager": "ok",
        "account_risk_state": "ok",
    }
