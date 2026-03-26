"""Tests for src/studio/mappers.py — pure mapping functions.

Each test verifies status/task/alertLevel for key state transitions.
"""
from __future__ import annotations

from src.studio.mappers import (
    map_collector,
    map_analyst,
    map_strategist,
    map_voter,
    map_risk_officer,
    map_trader,
    map_position_manager,
    map_accountant,
    map_calendar_reporter,
    map_inspector,
)


# ── collector ──────────────────────────────────────────────────


class TestCollector:
    def test_working(self) -> None:
        stats = {"threads": {"ingest_alive": True}, "summary": {"total": 6, "critical": 0, "full": 0}}
        agent = map_collector(stats, is_backfilling=False)
        assert agent["status"] == "working"
        assert agent["alertLevel"] == "none"

    def test_backfilling(self) -> None:
        stats = {"threads": {"ingest_alive": True}, "summary": {}}
        agent = map_collector(stats, is_backfilling=True)
        assert agent["status"] == "thinking"

    def test_thread_dead(self) -> None:
        stats = {"threads": {"ingest_alive": False}, "summary": {}}
        agent = map_collector(stats, is_backfilling=False)
        assert agent["status"] == "disconnected"
        assert agent["alertLevel"] == "error"

    def test_queue_full(self) -> None:
        stats = {"threads": {"ingest_alive": True}, "summary": {"full": 2, "critical": 0, "total": 6}}
        agent = map_collector(stats, is_backfilling=False)
        assert agent["status"] == "blocked"
        assert agent["alertLevel"] == "error"

    def test_queue_critical(self) -> None:
        stats = {"threads": {"ingest_alive": True}, "summary": {"full": 0, "critical": 3, "total": 6}}
        agent = map_collector(stats, is_backfilling=False)
        assert agent["status"] == "warning"
        assert agent["alertLevel"] == "warning"


# ── analyst ────────────────────────────────────────────────────


class TestAnalyst:
    def test_working(self) -> None:
        perf = {
            "event_loop_running": True,
            "total_computations": 100,
            "success_rate": 99.0,
            "cache_hits": 50,
            "scope_stats": {"confirmed": {"computations": 100, "indicators": 5}},
        }
        agent = map_analyst(perf)
        assert agent["status"] == "working"

    def test_no_loop(self) -> None:
        agent = map_analyst({"event_loop_running": False})
        assert agent["status"] == "error"

    def test_waiting(self) -> None:
        perf = {"event_loop_running": True, "total_computations": 0, "success_rate": 0.0}
        agent = map_analyst(perf)
        assert agent["status"] == "thinking"

    def test_high_failure(self) -> None:
        perf = {
            "event_loop_running": True,
            "total_computations": 10,
            "failed_computations": 5,
            "success_rate": 50.0,
            "scope_stats": {"confirmed": {"computations": 10, "indicators": 5}},
        }
        agent = map_analyst(perf)
        assert agent["status"] == "warning"


# ── strategist ─────────────────────────────────────────────────


class TestStrategist:
    def test_with_signals(self) -> None:
        signals = [{"direction": "buy"}, {"direction": "sell"}, {"direction": "buy"}]
        agent = map_strategist(20, signals)
        assert agent["status"] == "working"
        assert agent["metrics"]["buy_count"] == 2
        assert agent["metrics"]["sell_count"] == 1

    def test_no_signals(self) -> None:
        agent = map_strategist(20, [])
        assert agent["status"] == "idle"

    def test_no_strategies(self) -> None:
        agent = map_strategist(0, [])
        assert agent["status"] == "error"


# ── voter ──────────────────────────────────────────────────────


class TestVoter:
    def test_active_confirmed(self) -> None:
        status = {"running": True, "active_confirmed_states": 3, "active_preview_states": 0, "processed_events": 100, "dropped_events": 0}
        agent = map_voter(status)
        assert agent["status"] == "working"

    def test_preview_only(self) -> None:
        status = {"running": True, "active_confirmed_states": 0, "active_preview_states": 2, "processed_events": 50, "dropped_events": 0}
        agent = map_voter(status)
        assert agent["status"] == "thinking"

    def test_idle(self) -> None:
        status = {"running": True, "active_confirmed_states": 0, "active_preview_states": 0, "processed_events": 10, "dropped_events": 0}
        agent = map_voter(status)
        assert agent["status"] == "idle"

    def test_not_running(self) -> None:
        agent = map_voter({"running": False})
        assert agent["status"] == "disconnected"


# ── risk_officer ───────────────────────────────────────────────


class TestRiskOfficer:
    def test_approved(self) -> None:
        status = {"signals_received": 5, "signals_passed": 5, "signals_blocked": 0, "execution_quality": {"risk_blocks": 0}}
        agent = map_risk_officer(status)
        assert agent["status"] == "approved"

    def test_reviewing(self) -> None:
        status = {"signals_received": 5, "signals_passed": 3, "signals_blocked": 2, "execution_quality": {"risk_blocks": 0}}
        agent = map_risk_officer(status)
        assert agent["status"] == "reviewing"

    def test_risk_blocked(self) -> None:
        status = {"signals_received": 5, "signals_passed": 3, "signals_blocked": 2, "execution_quality": {"risk_blocks": 1}}
        agent = map_risk_officer(status)
        assert agent["status"] == "blocked"
        assert agent["alertLevel"] == "warning"


# ── trader ─────────────────────────────────────────────────────


class TestTrader:
    def test_disabled(self) -> None:
        agent = map_trader({"enabled": False, "circuit_breaker": {}})
        assert agent["status"] == "disconnected"

    def test_circuit_open(self) -> None:
        status = {"enabled": True, "circuit_breaker": {"open": True, "consecutive_failures": 3}, "execution_count": 5}
        agent = map_trader(status)
        assert agent["status"] == "blocked"
        assert agent["alertLevel"] == "error"

    def test_working_with_executions(self) -> None:
        status = {
            "enabled": True, "circuit_breaker": {"open": False}, "execution_count": 10,
            "last_error": None, "recent_executions": [{"direction": "buy", "symbol": "XAUUSD"}],
        }
        agent = map_trader(status)
        assert agent["status"] == "working"

    def test_idle(self) -> None:
        status = {"enabled": True, "circuit_breaker": {"open": False}, "execution_count": 0, "last_error": None, "recent_executions": []}
        agent = map_trader(status)
        assert agent["status"] == "idle"


# ── position_manager ──────────────────────────────────────────


class TestPositionManager:
    def test_with_positions(self) -> None:
        positions = [{"profit": 50.0}, {"profit": -20.0}]
        status = {"running": True, "tracked_positions": 2, "reconcile_count": 10}
        agent = map_position_manager(positions, status)
        assert agent["status"] == "working"
        assert agent["metrics"]["total_pnl"] == 30.0

    def test_no_positions(self) -> None:
        agent = map_position_manager([], {"running": True, "tracked_positions": 0})
        assert agent["status"] == "idle"

    def test_not_running(self) -> None:
        agent = map_position_manager([], {"running": False})
        assert agent["status"] == "disconnected"

    def test_negative_pnl_warns(self) -> None:
        positions = [{"profit": -100.0}]
        status = {"running": True, "tracked_positions": 1, "reconcile_count": 5}
        agent = map_position_manager(positions, status)
        assert agent["alertLevel"] == "warning"


# ── accountant ─────────────────────────────────────────────────


class TestAccountant:
    def test_healthy(self) -> None:
        account = {"balance": 10000, "equity": 10050, "margin": 200, "free_margin": 9800}
        control = {"auto_entry_enabled": True, "close_only_mode": False}
        agent = map_accountant(account, control)
        assert agent["status"] == "working"

    def test_low_margin(self) -> None:
        # equity/margin = 5000/4000 = 125% < 150% threshold
        account = {"balance": 10000, "equity": 5000, "margin": 4000, "free_margin": 1000}
        control = {"auto_entry_enabled": True, "close_only_mode": False}
        agent = map_accountant(account, control)
        assert agent["status"] == "alert"
        assert agent["alertLevel"] == "error"

    def test_close_only(self) -> None:
        account = {"balance": 10000, "equity": 10000, "margin": 0, "free_margin": 10000}
        control = {"auto_entry_enabled": True, "close_only_mode": True}
        agent = map_accountant(account, control)
        assert agent["status"] == "warning"


# ── calendar_reporter ──────────────────────────────────────────


class TestCalendarReporter:
    def test_working(self) -> None:
        stats = {"running": "true", "stale": "false", "consecutive_failures": "0"}
        agent = map_calendar_reporter(stats, [])
        assert agent["status"] == "working"

    def test_stale(self) -> None:
        stats = {"running": "true", "stale": "true", "consecutive_failures": "0"}
        agent = map_calendar_reporter(stats, [])
        assert agent["status"] == "alert"
        assert agent["alertLevel"] == "error"

    def test_high_impact_active(self) -> None:
        stats = {"running": "true", "stale": "false", "consecutive_failures": "0"}
        windows = [{"impact": "high", "guard_active": True, "event_name": "FOMC"}]
        agent = map_calendar_reporter(stats, windows)
        assert agent["status"] == "warning"
        assert agent["metrics"]["high_impact_active"] == 1

    def test_not_running(self) -> None:
        stats = {"running": "false", "stale": "false", "consecutive_failures": "0"}
        agent = map_calendar_reporter(stats, [])
        assert agent["status"] == "disconnected"


# ── inspector ──────────────────────────────────────────────────


class TestInspector:
    def test_healthy(self) -> None:
        report = {"overall_status": "healthy", "active_alerts": []}
        agent = map_inspector(report)
        assert agent["status"] == "reviewing"
        assert agent["alertLevel"] == "none"

    def test_critical(self) -> None:
        report = {"overall_status": "critical", "active_alerts": [{"id": 1}, {"id": 2}]}
        agent = map_inspector(report)
        assert agent["status"] == "error"
        assert agent["alertLevel"] == "error"

    def test_warning(self) -> None:
        report = {"overall_status": "warning", "active_alerts": [{"id": 1}]}
        agent = map_inspector(report)
        assert agent["status"] == "alert"
