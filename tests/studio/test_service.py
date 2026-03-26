"""Tests for src/studio/service.py — StudioService registry + events."""
from __future__ import annotations

from src.studio.models import build_agent, build_event
from src.studio.service import StudioService


def test_register_and_build_agents() -> None:
    studio = StudioService()
    studio.register_agent("collector", lambda: build_agent("collector", "working", "采集中"))
    studio.register_agent("analyst", lambda: build_agent("analyst", "idle", "等待"))

    agents = studio.build_agents()
    assert len(agents) == 2
    ids = {a["id"] for a in agents}
    assert ids == {"collector", "analyst"}


def test_failing_provider_returns_error_agent() -> None:
    studio = StudioService()
    studio.register_agent("collector", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    agents = studio.build_agents()
    assert len(agents) == 1
    assert agents[0]["id"] == "collector"
    assert agents[0]["status"] == "error"
    assert agents[0]["alertLevel"] == "error"


def test_emit_event_stored_in_buffer() -> None:
    studio = StudioService(event_buffer_size=10)
    evt = build_event("signal_generated", "strategist", "XAUUSD M5 buy")
    studio.emit_event(evt)

    events = studio.recent_events()
    assert len(events) == 1
    assert events[0]["type"] == "signal_generated"
    assert events[0]["source"] == "strategist"


def test_build_summary() -> None:
    studio = StudioService()
    studio.register_agent("collector", lambda: build_agent("collector", "working", "ok"))
    studio.register_agent("analyst", lambda: build_agent("analyst", "error", "fail", alert_level="error"))
    studio.register_agent("trader", lambda: build_agent("trader", "idle", "wait"))
    studio.register_summary_provider(lambda: {"account": "12345", "symbol": "XAUUSD"})

    summary = studio.build_summary()
    assert summary["activeAgents"] == 1  # only "working" counts
    assert summary["alertCount"] == 1
    assert summary["health"] == "unhealthy"  # has error agent
    assert summary["account"] == "12345"
    assert summary["symbol"] == "XAUUSD"


def test_build_snapshot_contains_all() -> None:
    studio = StudioService()
    studio.register_agent("collector", lambda: build_agent("collector", "working", "ok"))
    studio.emit_event(build_event("test", "src", "msg"))

    snapshot = studio.build_snapshot()
    assert "agents" in snapshot
    assert "events" in snapshot
    assert "summary" in snapshot
    assert len(snapshot["agents"]) == 1
    assert len(snapshot["events"]) == 1


def test_summary_provider_failure_is_safe() -> None:
    studio = StudioService()
    studio.register_summary_provider(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    studio.register_summary_provider(lambda: {"symbol": "XAUUSD"})

    summary = studio.build_summary()
    # First provider fails, second succeeds
    assert summary["symbol"] == "XAUUSD"
    assert "activeAgents" in summary


def test_overall_health_logic() -> None:
    studio = StudioService()

    # All healthy
    studio.register_agent("a", lambda: build_agent("a", "working", "ok"))
    assert studio.build_summary()["health"] == "healthy"

    # Warning
    studio.register_agent("b", lambda: build_agent("b", "warning", "warn", alert_level="warning"))
    assert studio.build_summary()["health"] == "degraded"

    # Error overrides warning
    studio.register_agent("c", lambda: build_agent("c", "error", "fail", alert_level="error"))
    assert studio.build_summary()["health"] == "unhealthy"
