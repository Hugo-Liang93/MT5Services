"""Admin API 路由单元测试。

使用 FastAPI TestClient + mock 依赖注入，测试各端点的基本行为。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.admin import _build_strategy_detail, _load_json_config, router
from src.api.admin_schemas import (
    DashboardOverview,
    ExecutorSnapshot,
    StrategyDetail,
    SystemStatusSnapshot,
)
from src.api.schemas import ApiResponse
from src.monitoring.pipeline_event_bus import PipelineEventBus


# ── Helpers ───────────────────────────────────────────────────


def _create_app(**dep_overrides: Any) -> FastAPI:
    """创建带有 mock 依赖注入的 FastAPI 测试应用。"""
    from src.api import deps

    app = FastAPI()
    app.include_router(router, prefix="/v1")

    # 默认 mock 对象
    trading = dep_overrides.get("trading", MagicMock())
    trading.health.return_value = {"balance": 10000, "equity": 10000}

    position_mgr = dep_overrides.get("position_mgr", MagicMock())
    position_mgr.active_positions.return_value = []

    signal_runtime = dep_overrides.get("signal_runtime", MagicMock())
    signal_runtime.status.return_value = {"running": True}
    signal_runtime.get_regime_stability.return_value = {"regime": "trending"}

    executor = dep_overrides.get("executor", MagicMock())
    executor.status.return_value = {
        "enabled": True,
        "circuit_breaker": {"open": False, "consecutive_failures": 0},
        "execution_count": 5,
        "last_execution_at": None,
        "pending_entries": {"active_count": 0},
    }

    indicator_mgr = dep_overrides.get("indicator_mgr", MagicMock())
    indicator_mgr.get_performance_stats.return_value = {"count": 21}

    signal_svc = dep_overrides.get("signal_svc", MagicMock())
    signal_svc.list_strategies.return_value = ["sma_trend", "rsi_reversion"]
    signal_svc.strategy_affinity_map.return_value = {}
    signal_svc.strategy_scopes.return_value = ("confirmed",)
    signal_svc.strategy_requirements.return_value = ("sma20", "ema50")
    signal_svc.strategy_winrates.return_value = []
    signal_svc._strategy_affinity_cache = {}

    perf_tracker = dep_overrides.get("perf_tracker", MagicMock())
    perf_tracker.strategy_ranking.return_value = []
    perf_tracker.describe.return_value = {}
    perf_tracker.get_strategy_stats.return_value = None
    perf_tracker.get_multiplier.return_value = 1.0

    calibrator = dep_overrides.get("calibrator", MagicMock())
    calibrator.describe.return_value = {"status": "active"}

    pipeline_bus = dep_overrides.get("pipeline_bus", PipelineEventBus())

    ingestor = dep_overrides.get("ingestor", MagicMock())
    ingestor.queue_stats.return_value = {"total_queues": 6}

    app.dependency_overrides[deps.get_trading_service] = lambda: trading
    app.dependency_overrides[deps.get_position_manager] = lambda: position_mgr
    app.dependency_overrides[deps.get_signal_runtime] = lambda: signal_runtime
    app.dependency_overrides[deps.get_trade_executor] = lambda: executor
    app.dependency_overrides[deps.get_indicator_manager] = lambda: indicator_mgr
    app.dependency_overrides[deps.get_signal_service] = lambda: signal_svc
    app.dependency_overrides[deps.get_performance_tracker] = lambda: perf_tracker
    app.dependency_overrides[deps.get_calibrator] = lambda: calibrator
    app.dependency_overrides[deps.get_pipeline_event_bus] = lambda: pipeline_bus

    # get_ingestor 通过 patch 处理（不是 Depends 参数）
    app.state.ingestor = ingestor

    return app


@pytest.fixture
def client() -> TestClient:
    app = _create_app()
    return TestClient(app)


# ═══════════════════════════════════════════════════════════════
# 3.1 仪表板概览
# ═══════════════════════════════════════════════════════════════


class TestDashboard:
    @patch("src.api.admin.deps.get_startup_status")
    @patch("src.api.admin.deps.get_ingestor")
    def test_dashboard_success(
        self, mock_ingestor: MagicMock, mock_startup: MagicMock, client: TestClient
    ) -> None:
        mock_startup.return_value = {
            "ready": True,
            "started_at": "2025-01-01T00:00:00+00:00",
            "completed_at": "2025-01-01T00:00:05+00:00",
            "phase": "running",
        }
        ingestor = MagicMock()
        ingestor.queue_stats.return_value = {"total_queues": 6}
        mock_ingestor.return_value = ingestor

        resp = client.get("/v1/admin/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        overview = data["data"]
        assert overview["system"]["ready"] is True
        assert overview["system"]["status"] == "healthy"
        assert "account" in overview
        assert "executor" in overview

    @patch("src.api.admin.deps.get_startup_status")
    @patch("src.api.admin.deps.get_ingestor")
    def test_dashboard_service_unavailable(
        self, mock_ingestor: MagicMock, mock_startup: MagicMock
    ) -> None:
        """子服务异常时返回 error: unavailable，不影响其他字段。"""
        mock_startup.return_value = {"ready": False, "phase": "starting"}

        trading = MagicMock()
        trading.health.side_effect = RuntimeError("connection lost")

        ingestor = MagicMock()
        ingestor.queue_stats.side_effect = RuntimeError("db down")
        mock_ingestor.return_value = ingestor

        app = _create_app(trading=trading)
        tc = TestClient(app)
        resp = tc.get("/v1/admin/dashboard")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["account"] == {"error": "unavailable"}
        assert data["system"]["status"] == "starting"


# ═══════════════════════════════════════════════════════════════
# 3.2 配置查看
# ═══════════════════════════════════════════════════════════════


class TestConfig:
    @patch("src.api.admin.get_effective_config_snapshot")
    @patch("src.api.admin.get_config_provenance_snapshot")
    def test_config_full(
        self, mock_prov: MagicMock, mock_eff: MagicMock, client: TestClient
    ) -> None:
        mock_eff.return_value = {"trading": {"x": 1}, "risk": {"y": 2}}
        mock_prov.return_value = {"trading": {"source": "signal.ini"}}

        resp = client.get("/v1/admin/config")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "trading" in data["effective"]
        assert "risk" in data["effective"]
        assert len(data["files"]) > 0

    @patch("src.api.admin.get_effective_config_snapshot")
    @patch("src.api.admin.get_config_provenance_snapshot")
    def test_config_section_filter(
        self, mock_prov: MagicMock, mock_eff: MagicMock, client: TestClient
    ) -> None:
        mock_eff.return_value = {"trading": {"x": 1}, "risk": {"y": 2}}
        mock_prov.return_value = {"trading": {"source": "signal.ini"}, "risk": {}}

        resp = client.get("/v1/admin/config?section=risk")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "risk" in data["effective"]
        assert "trading" not in data["effective"]

    @patch("src.api.admin.get_signal_config")
    def test_config_signal(self, mock_cfg: MagicMock, client: TestClient) -> None:
        cfg = MagicMock()
        cfg.model_dump.return_value = {"auto_trade": True}
        mock_cfg.return_value = cfg

        resp = client.get("/v1/admin/config/signal")
        assert resp.status_code == 200
        assert resp.json()["data"]["auto_trade"] is True

    @patch("src.api.admin.get_risk_config")
    def test_config_risk(self, mock_cfg: MagicMock, client: TestClient) -> None:
        cfg = MagicMock()
        cfg.model_dump.return_value = {"max_positions": 5}
        mock_cfg.return_value = cfg

        resp = client.get("/v1/admin/config/risk")
        assert resp.status_code == 200
        assert resp.json()["data"]["max_positions"] == 5

    @patch("src.api.admin._load_json_config")
    def test_config_indicators(self, mock_load: MagicMock, client: TestClient) -> None:
        mock_load.return_value = [
            {"name": "rsi14", "enabled": True},
            {"name": "wma20", "enabled": False},
        ]
        resp = client.get("/v1/admin/config/indicators")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total_count"] == 2
        assert data["enabled_count"] == 1

    @patch("src.api.admin._load_json_config")
    def test_config_indicators_enabled_only(
        self, mock_load: MagicMock, client: TestClient
    ) -> None:
        mock_load.return_value = [
            {"name": "rsi14", "enabled": True},
            {"name": "wma20", "enabled": False},
        ]
        resp = client.get("/v1/admin/config/indicators?enabled_only=true")
        data = resp.json()["data"]
        assert data["total_count"] == 1  # only enabled

    @patch("src.api.admin._load_json_config")
    def test_config_composites(self, mock_load: MagicMock, client: TestClient) -> None:
        mock_load.return_value = [{"name": "trend_triple_confirm"}]
        resp = client.get("/v1/admin/config/composites")
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1


# ═══════════════════════════════════════════════════════════════
# 3.3 绩效报表
# ═══════════════════════════════════════════════════════════════


class TestPerformance:
    def test_performance_strategies(self, client: TestClient) -> None:
        resp = client.get("/v1/admin/performance/strategies")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "session_ranking" in data
        assert "calibrator" in data

    def test_performance_strategies_with_hours(self, client: TestClient) -> None:
        resp = client.get("/v1/admin/performance/strategies?hours=24")
        assert resp.status_code == 200

    def test_confidence_pipeline(self, client: TestClient) -> None:
        resp = client.get("/v1/admin/performance/confidence-pipeline/XAUUSD/M5")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["symbol"] == "XAUUSD"
        assert data["timeframe"] == "M5"
        assert "regime" in data
        assert "strategies" in data
        assert len(data["strategies"]) == 2  # sma_trend + rsi_reversion


# ═══════════════════════════════════════════════════════════════
# 3.4 策略详情
# ═══════════════════════════════════════════════════════════════


class TestStrategies:
    def test_list_strategies(self, client: TestClient) -> None:
        resp = client.get("/v1/admin/strategies")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 2
        assert data[0]["name"] in ("sma_trend", "rsi_reversion")

    def test_strategy_detail_found(self, client: TestClient) -> None:
        resp = client.get("/v1/admin/strategies/sma_trend")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["name"] == "sma_trend"
        assert "session_performance" in data

    def test_strategy_detail_not_found(self, client: TestClient) -> None:
        resp = client.get("/v1/admin/strategies/nonexistent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        error = body.get("error", {})
        assert "not found" in error.get("message", "").lower()


# ═══════════════════════════════════════════════════════════════
# 3.6 Pipeline stats
# ═══════════════════════════════════════════════════════════════


class TestPipelineStats:
    def test_pipeline_stats(self, client: TestClient) -> None:
        resp = client.get("/v1/admin/pipeline/stats")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "listeners" in data
        assert "total_emitted" in data


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════


class TestHelpers:
    @patch("src.api.admin.resolve_config_path")
    def test_load_json_config_missing(self, mock_resolve: MagicMock) -> None:
        mock_resolve.return_value = None
        result = _load_json_config("nonexistent.json")
        assert result == []

    @patch("src.api.admin.resolve_config_path")
    def test_load_json_config_exists(
        self, mock_resolve: MagicMock, tmp_path: Any
    ) -> None:
        f = tmp_path / "test.json"
        f.write_text('[{"name": "test"}]')
        mock_resolve.return_value = str(f)
        result = _load_json_config("test.json")
        assert result == [{"name": "test"}]

    def test_build_strategy_detail(self) -> None:
        svc = MagicMock()
        svc.strategy_affinity_map.return_value = {}
        svc.strategy_scopes.return_value = ("confirmed", "intrabar")
        svc.strategy_requirements.return_value = ("rsi14",)
        svc._strategy_affinity_cache = {}

        detail = _build_strategy_detail("rsi_reversion", svc)
        assert detail.name == "rsi_reversion"
        assert detail.preferred_scopes == ["confirmed", "intrabar"]
        assert detail.required_indicators == ["rsi14"]

    def test_build_strategy_detail_error_handling(self) -> None:
        """strategy_scopes / strategy_requirements 抛异常时不崩溃。"""
        svc = MagicMock()
        svc.strategy_affinity_map.return_value = None
        svc.strategy_scopes.side_effect = ValueError("not found")
        svc.strategy_requirements.side_effect = ValueError("not found")
        svc._strategy_affinity_cache = {}

        detail = _build_strategy_detail("unknown", svc)
        assert detail.preferred_scopes == []
        assert detail.required_indicators == []
        assert detail.regime_affinity == {}
