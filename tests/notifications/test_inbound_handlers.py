"""QueryHandlers behavior: readmodel shapes in, Markdown replies out."""

from __future__ import annotations

from typing import Any

import pytest

from src.notifications.inbound.handlers import QueryHandlers


class StubReadModel:
    """Feed arbitrary return values into each readmodel method."""

    def __init__(self, **methods: Any) -> None:
        for name, value in methods.items():
            setattr(self, f"_{name}", value)

    def _pull(self, name: str) -> Any:
        attr = f"_{name}"
        if not hasattr(self, attr):
            return {}
        value = getattr(self, attr)
        if callable(value):
            return value()
        return value

    def health_report(self):
        return self._pull("health_report")

    def trade_executor_summary(self):
        return self._pull("trade_executor_summary")

    def tracked_positions_payload(self):
        return self._pull("tracked_positions_payload")

    def signal_runtime_summary(self):
        return self._pull("signal_runtime_summary")

    def runtime_mode_summary(self):
        return self._pull("runtime_mode_summary")

    def pending_entries_summary(self):
        return self._pull("pending_entries_summary")


class TestHelp:
    def test_help_lists_commands(self):
        handlers = QueryHandlers(runtime_read_model=None, instance="live-main")
        out = handlers.handle_help()
        assert "/health" in out
        assert "/positions" in out
        assert "/daily-report" in out


class TestHealth:
    def test_ok(self):
        rrm = StubReadModel(health_report={"status": "ok", "active_alerts": []})
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live-main")
        out = handlers.handle_health()
        assert "ok" in out
        assert "0" in out  # active alerts
        # Hyphen is NOT in legacy-Markdown's escape set, so stays raw.
        assert "live-main" in out

    def test_top_alerts_rendered(self):
        rrm = StubReadModel(
            health_report={
                "status": "warning",
                "active_alerts": [
                    {
                        "component": "data",
                        "metric_name": "data_latency",
                        "alert_level": "warning",
                    },
                    {
                        "component": "ingest",
                        "metric_name": "queue_depth",
                        "alert_level": "critical",
                    },
                ],
            }
        )
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live-main")
        out = handlers.handle_health()
        assert "data" in out
        assert "data\\_latency" in out  # escaped
        assert "queue\\_depth" in out

    def test_readmodel_missing(self):
        handlers = QueryHandlers(runtime_read_model=None, instance="live-main")
        out = handlers.handle_health()
        assert "不可用" in out


class TestPositions:
    def test_empty(self):
        rrm = StubReadModel(tracked_positions_payload={"count": 0, "items": []})
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live")
        out = handlers.handle_positions()
        assert "无持仓" in out

    def test_with_items(self):
        rrm = StubReadModel(
            tracked_positions_payload={
                "count": 2,
                "items": [
                    {
                        "symbol": "XAUUSD",
                        "direction": "long",
                        "volume": 0.1,
                        "unrealized_pnl": 15.5,
                    },
                    {"symbol": "XAUUSD", "direction": "short"},
                ],
            }
        )
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live")
        out = handlers.handle_positions()
        assert "2" in out
        assert "XAUUSD" in out
        assert "long" in out
        assert "short" in out


class TestExecutor:
    def test_full_payload(self):
        rrm = StubReadModel(
            trade_executor_summary={
                "enabled": True,
                "circuit_open": False,
                "execution_count": 5,
                "signals": {"received": 10, "passed": 5},
                "last_error": None,
            }
        )
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live")
        out = handlers.handle_executor()
        assert "✅" in out  # enabled=True
        assert "❌" in out  # circuit_open=False
        assert "5" in out
        assert "10" in out

    def test_circuit_open(self):
        rrm = StubReadModel(
            trade_executor_summary={
                "enabled": True,
                "circuit_open": True,
                "execution_count": 0,
                "last_error": "timeout on send",
            }
        )
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live")
        out = handlers.handle_executor()
        # Two yes/no fields: enabled→✅, circuit_open→✅ (both true)
        assert out.count("✅") == 2
        assert "timeout on send" in out


class TestSignals:
    def test_running(self):
        rrm = StubReadModel(
            signal_runtime_summary={
                "running": True,
                "queues": {"confirmed_depth": 3, "intrabar_depth": 7},
                "processing": {"drop_rate_1m": 0.0},
            }
        )
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live")
        out = handlers.handle_signals()
        assert "✅" in out
        assert "3" in out
        assert "7" in out


class TestMode:
    def test_basic(self):
        rrm = StubReadModel(
            runtime_mode_summary={
                "current_mode": "full",
                "configured_mode": "full",
                "last_transition_at": "2026-04-18T12:00:00Z",
            }
        )
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live")
        out = handlers.handle_mode()
        assert "full" in out
        assert "2026" in out

    def test_mode_drift(self):
        rrm = StubReadModel(
            runtime_mode_summary={
                "current_mode": "risk_off",
                "configured_mode": "full",
                "last_transition_reason": "equity_below_threshold",
            }
        )
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live")
        out = handlers.handle_mode()
        assert "risk\\_off" in out
        assert "full" in out
        assert "equity\\_below\\_threshold" in out


class TestPending:
    def test_empty(self):
        rrm = StubReadModel(pending_entries_summary={"count": 0, "entries": []})
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live")
        out = handlers.handle_pending()
        assert "无" in out

    def test_listed(self):
        rrm = StubReadModel(
            pending_entries_summary={
                "active_count": 1,
                "entries": [
                    {"signal_id": "abc123", "strategy": "trend_h1", "direction": "long"}
                ],
            }
        )
        handlers = QueryHandlers(runtime_read_model=rrm, instance="live")
        out = handlers.handle_pending()
        assert "trend\\_h1" in out
        assert "abc123" in out


class TestDailyReport:
    def test_no_submit_fn(self):
        handlers = QueryHandlers(runtime_read_model=None, instance="live")
        out = handlers.handle_daily_report()
        assert "未接入" in out

    def test_submitted(self):
        handlers = QueryHandlers(
            runtime_read_model="any",
            instance="live",
            daily_report_submit=lambda: True,
        )
        out = handlers.handle_daily_report()
        assert "已入队" in out

    def test_not_submitted(self):
        handlers = QueryHandlers(
            runtime_read_model="any",
            instance="live",
            daily_report_submit=lambda: False,
        )
        out = handlers.handle_daily_report()
        assert "未入队" in out

    def test_submit_raises(self):
        def _bad():
            raise RuntimeError("explode")

        handlers = QueryHandlers(
            runtime_read_model="any",
            instance="live",
            daily_report_submit=_bad,
        )
        out = handlers.handle_daily_report()
        assert "失败" in out


class TestFailureIsolation:
    def test_readmodel_raise_handled(self):
        class BrokenReadModel:
            def health_report(self):
                raise RuntimeError("boom")

        handlers = QueryHandlers(runtime_read_model=BrokenReadModel(), instance="live")
        out = handlers.handle_health()
        # Degrades to "unavailable" message, doesn't raise.
        assert "不可用" in out
