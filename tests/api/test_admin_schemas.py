"""Admin Schemas 单元测试。"""

from __future__ import annotations

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

    def test_preserves_real_runtime_payload_blocks(self) -> None:
        """P2 回归：dashboard_overview() 实际返回 trading_state / account_risk /
        validation / external_dependencies 4 个顶层区块，旧 DashboardOverview
        只声明 7 字段 → Pydantic extra='ignore' 默认裁掉这 4 块 →
        admin dashboard 永远暴露不出交易态/风险态/MT5 依赖。
        """
        overview = DashboardOverview(
            **{
                "system": {"status": "healthy", "ready": True},
                "account": {"balance": 10000},
                "positions": {"count": 2},
                "trading_state": {"trade_control": {"auto_entry_enabled": True}},
                "account_risk": {"daily_pnl_pct": -1.2, "circuit_open": False},
                "signals": {"running": True},
                "executor": {"enabled": True, "circuit_open": False},
                "validation": {},
                "external_dependencies": {"mt5_session": {"session_ready": True}},
                "storage": {"queues": 6},
                "indicators": {"count": 21},
            }
        )
        d = overview.model_dump()
        # 4 个之前被裁掉的字段必须保留
        assert (
            "trading_state" in d
        ), f"trading_state 字段被 schema 裁掉；输出 keys: {list(d.keys())!r}"
        assert d["trading_state"]["trade_control"]["auto_entry_enabled"] is True
        assert "account_risk" in d
        assert d["account_risk"]["daily_pnl_pct"] == -1.2
        assert "validation" in d
        assert "external_dependencies" in d
        assert d["external_dependencies"]["mt5_session"]["session_ready"] is True


class TestExecutorSnapshotPreservesDiagnostics:
    """P2 回归：ExecutorSnapshot 旧只 6 字段 → trade_executor_summary() 的
    status/running/last_error/signals/execution_gate/pending_entries/
    recent_executions 等关键诊断信息全部被裁。

    前端无法判断执行器是 disabled / delegated / critical / blocked-by-gate。
    """

    def test_preserves_critical_runtime_diagnostic_fields(self) -> None:
        snap = ExecutorSnapshot(
            **{
                "status": "ok",
                "running": True,
                "configured": True,
                "armed": True,
                "enabled": True,
                "circuit_open": False,
                "consecutive_failures": 0,
                "execution_count": 17,
                "last_execution_at": "2026-04-26T10:00:00+00:00",
                "last_error": None,
                "last_risk_block": None,
                "signals": {"received": 50, "passed": 30, "blocked": 20},
                "execution_gate": {"voting_groups": []},
                "pending_entries_count": 2,
                "pending_entries": {"active_count": 2},
                "recent_executions": [{"id": 1}],
                "execution_scope": "local",
            }
        )
        d = snap.model_dump()
        # 关键诊断字段必须保留
        assert d.get("status") == "ok"
        assert d.get("running") is True
        assert d.get("last_error") is None
        assert d.get("signals", {}).get("passed") == 30
        assert d.get("execution_gate") is not None
        assert d.get("recent_executions") == [{"id": 1}]
        assert d.get("execution_scope") == "local"


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
            "htf_policy": "soft_gate",
        }
        sd = StrategyDetail.model_validate(data)
        assert sd.model_dump()["htf_policy"] == "soft_gate"

    def test_preserves_htf_policy_field(self) -> None:
        """P3 回归：structured strategy 必须声明 htf_policy
        (HARD_GATE / SOFT_GATE / SOFT_BONUS / NONE)。
        旧 StrategyDetail 缺该字段 → describe_strategy() 给的 htf_policy='hard_gate'
        被 Pydantic extra='ignore' 静默裁掉 → /v1/admin/strategies 列表无法
        给出完整策略契约。
        """
        sd = StrategyDetail(
            name="trend_continuation",
            category="trend",
            htf_policy="hard_gate",
        )
        d = sd.model_dump()
        assert "htf_policy" in d, f"htf_policy 必须保留；输出 keys: {list(d.keys())!r}"
        assert d["htf_policy"] == "hard_gate"


class TestStrategySessionDetailView:
    """P3 回归：详情 endpoint 双重裁剪 — build_strategy_detail() →
    StrategyDetail（已修，加 htf_policy）→ model_dump → StrategySessionDetailView
    （仍缺 htf_policy）→ /v1/admin/strategies/{name} 输出无 htf_policy。
    """

    def test_preserves_htf_policy(self) -> None:
        from src.api.admin_routes.view_models import StrategySessionDetailView

        view = StrategySessionDetailView(
            name="trend_continuation",
            category="trend",
            htf_policy="hard_gate",
            session_performance={"win_rate": 0.55},
        )
        d = view.model_dump()
        assert "htf_policy" in d, f"htf_policy 必须保留；输出 keys: {list(d.keys())!r}"
        assert d["htf_policy"] == "hard_gate"
        assert d["session_performance"]["win_rate"] == 0.55
