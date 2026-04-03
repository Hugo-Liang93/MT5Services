from __future__ import annotations

import asyncio

from fastapi import HTTPException

from src.api.monitoring import health_ready


def test_health_ready_raises_when_critical_component_is_degraded(monkeypatch) -> None:
    monkeypatch.setattr("src.api.monitoring_routes.health.get_startup_status", lambda: {"ready": True, "phase": "running"})
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_ingestor",
        lambda: type(
            "Ingestor",
            (),
            {"queue_stats": lambda self: {"threads": {"writer_alive": False}}},
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
        assert "storage_writer" in exc.detail["failed_checks"]
    else:
        raise AssertionError("expected readiness probe to fail on degraded storage writer")


def test_health_ready_returns_ready_when_checks_pass(monkeypatch) -> None:
    monkeypatch.setattr("src.api.monitoring_routes.health.get_startup_status", lambda: {"ready": True, "phase": "running"})
    monkeypatch.setattr(
        "src.api.monitoring_routes.health.get_ingestor",
        lambda: type(
            "Ingestor",
            (),
            {"queue_stats": lambda self: {"threads": {"writer_alive": True}}},
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
        "indicator_engine": "ok",
    }
