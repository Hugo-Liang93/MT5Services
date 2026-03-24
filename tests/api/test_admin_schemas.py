"""Admin Schemas 单元测试。"""

from __future__ import annotations

import pytest

from src.api.admin_schemas import (
    ConfigView,
    DashboardOverview,
    ExecutorSnapshot,
    StrategyDetail,
    StrategyPerformanceReport,
    SystemStatusSnapshot,
)


class TestSystemStatusSnapshot:
    def test_defaults(self) -> None:
        snap = SystemStatusSnapshot(status="healthy")
        assert snap.status == "healthy"
        assert snap.uptime_seconds is None
        assert snap.started_at is None
        assert snap.ready is False
        assert snap.phase == "unknown"

    def test_full(self) -> None:
        snap = SystemStatusSnapshot(
            status="healthy",
            uptime_seconds=3600.5,
            started_at="2025-01-01T00:00:00Z",
            ready=True,
            phase="running",
        )
        d = snap.model_dump()
        assert d["uptime_seconds"] == 3600.5
        assert d["ready"] is True

    def test_roundtrip(self) -> None:
        data = {"status": "warning", "phase": "starting", "ready": False}
        snap = SystemStatusSnapshot.model_validate(data)
        assert snap.status == "warning"
        assert snap.model_dump()["phase"] == "starting"


class TestExecutorSnapshot:
    def test_defaults(self) -> None:
        snap = ExecutorSnapshot()
        assert snap.enabled is False
        assert snap.circuit_open is False
        assert snap.consecutive_failures == 0
        assert snap.execution_count == 0
        assert snap.last_execution_at is None
        assert snap.pending_entries_count == 0

    def test_full(self) -> None:
        snap = ExecutorSnapshot(
            enabled=True,
            circuit_open=True,
            consecutive_failures=3,
            execution_count=42,
            last_execution_at="2025-01-01T12:00:00Z",
            pending_entries_count=2,
        )
        d = snap.model_dump()
        assert d["enabled"] is True
        assert d["execution_count"] == 42


class TestDashboardOverview:
    def test_minimal(self) -> None:
        overview = DashboardOverview(
            system=SystemStatusSnapshot(status="healthy"),
            executor=ExecutorSnapshot(),
        )
        d = overview.model_dump()
        assert d["system"]["status"] == "healthy"
        assert d["account"] == {}
        assert d["positions"] == {}
        assert d["signals"] == {}
        assert d["storage"] == {}
        assert d["indicators"] == {}

    def test_with_data(self) -> None:
        overview = DashboardOverview(
            system=SystemStatusSnapshot(status="healthy", ready=True),
            account={"balance": 10000},
            positions={"count": 2},
            signals={"running": True},
            executor=ExecutorSnapshot(enabled=True),
            storage={"queues": 6},
            indicators={"count": 21},
        )
        d = overview.model_dump()
        assert d["account"]["balance"] == 10000
        assert d["executor"]["enabled"] is True


class TestConfigView:
    def test_defaults(self) -> None:
        cv = ConfigView()
        assert cv.effective == {}
        assert cv.provenance == {}
        assert cv.files == []

    def test_roundtrip(self) -> None:
        data = {
            "effective": {"trading": {"auto_trade": True}},
            "provenance": {"trading": {"source": "signal.ini"}},
            "files": ["app.ini", "signal.ini"],
        }
        cv = ConfigView.model_validate(data)
        assert len(cv.files) == 2
        assert cv.model_dump() == data


class TestStrategyPerformanceReport:
    def test_defaults(self) -> None:
        r = StrategyPerformanceReport()
        assert r.session_ranking == []
        assert r.session_summary == {}
        assert r.historical_winrates == []
        assert r.calibrator == {}

    def test_with_data(self) -> None:
        r = StrategyPerformanceReport(
            session_ranking=[{"strategy": "rsi_reversion", "pnl": 150.0}],
            session_summary={"total_signals": 20},
            historical_winrates=[{"strategy": "sma_trend", "win_rate": 0.6}],
            calibrator={"status": "active"},
        )
        d = r.model_dump()
        assert len(d["session_ranking"]) == 1
        assert d["calibrator"]["status"] == "active"


class TestStrategyDetail:
    def test_minimal(self) -> None:
        sd = StrategyDetail(name="test_strategy")
        assert sd.name == "test_strategy"
        assert sd.category is None
        assert sd.preferred_scopes == []
        assert sd.required_indicators == []
        assert sd.regime_affinity == {}

    def test_full(self) -> None:
        sd = StrategyDetail(
            name="rsi_reversion",
            category="mean_reversion",
            preferred_scopes=["intrabar", "confirmed"],
            required_indicators=["rsi14"],
            regime_affinity={"trending": 0.3, "ranging": 1.0},
        )
        d = sd.model_dump()
        assert d["name"] == "rsi_reversion"
        assert d["regime_affinity"]["ranging"] == 1.0

    def test_roundtrip(self) -> None:
        data = {
            "name": "supertrend",
            "category": "trend",
            "preferred_scopes": ["confirmed"],
            "required_indicators": ["supertrend14", "adx14"],
            "regime_affinity": {"trending": 1.0, "ranging": 0.2},
        }
        sd = StrategyDetail.model_validate(data)
        assert sd.model_dump() == data
