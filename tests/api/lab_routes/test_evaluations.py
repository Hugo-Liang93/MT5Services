"""Tests for GET /v1/lab/evaluations/{run_id} — P11 Phase 5。"""

from __future__ import annotations

from typing import Any, Optional

from src.api.lab_routes.evaluations import lab_evaluation
from src.readmodels.lab_evaluation import LabEvaluationReadModel


class _FakeBacktestDetail:
    def __init__(self, *, metrics: Optional[dict] = None, **extra: Any) -> None:
        self._metrics = metrics
        self._extra = extra

    def build_metrics_summary(self, run_id: str) -> Optional[dict]:
        return self._metrics

    def build_equity_curve(self, run_id: str) -> Optional[dict]:
        return self._extra.get("equity")

    def build_monthly_returns(self, run_id: str) -> Optional[dict]:
        return self._extra.get("monthly")

    def build_trade_structure(self, run_id: str) -> Optional[dict]:
        return self._extra.get("trade_structure")

    def build_execution_realism(self, run_id: str) -> Optional[dict]:
        return self._extra.get("execution_realism")


class _NullWF:
    def build_by_wf_run_id(self, run_id: str) -> Optional[dict]:
        return None

    def build_by_backtest_run_id(self, run_id: str) -> Optional[dict]:
        return None


class _NullCorr:
    def build_latest_for_run(self, run_id: str) -> Optional[dict]:
        return None


def _freshness() -> dict:
    return {
        "observed_at": "2026-04-21T01:00:00+00:00",
        "data_updated_at": "2026-04-21T00:00:00+00:00",
        "age_seconds": 3600.0,
        "stale_after_seconds": 86400.0,
        "max_age_seconds": 2592000.0,
        "freshness_state": "fresh",
        "source_kind": "native",
        "fallback_applied": False,
        "fallback_reason": None,
    }


def _metrics_payload(run_id: str = "bt_test") -> dict:
    return {
        "run_id": run_id,
        "total_pnl": 100.0,
        "total_pnl_pct": 0.01,
        "total_trades": 2,
        "winning_trades": 1,
        "losing_trades": 1,
        "win_rate": 0.5,
        "profit_factor": 1.5,
        "expectancy": 50.0,
        "expectancy_r": None,
        "avg_r_multiple": None,
        "max_drawdown": 50.0,
        "max_drawdown_pct": 0.005,
        "max_drawdown_duration_bars": 2,
        "sharpe_ratio": 1.0,
        "sortino_ratio": 1.2,
        "calmar_ratio": 2.0,
        "avg_win": 100.0,
        "avg_loss": -50.0,
        "avg_bars_held": 5.0,
        "max_consecutive_wins": 1,
        "max_consecutive_losses": 1,
        "monthly_returns": [],
        "freshness": _freshness(),
    }


def test_envelope_returned_when_run_exists() -> None:
    rm = LabEvaluationReadModel(
        backtest_detail=_FakeBacktestDetail(metrics=_metrics_payload()),
        walk_forward=_NullWF(),
        correlation=_NullCorr(),
    )

    response = lab_evaluation(run_id="bt_test", read_model=rm)

    assert response.success is True
    assert response.data is not None
    assert response.data.run_id == "bt_test"
    assert response.data.metrics_summary.total_trades == 2
    assert response.data.capability.walk_forward == "missing"
    assert response.data.capability.correlation_analysis == "missing"


def test_not_found_when_metrics_missing() -> None:
    rm = LabEvaluationReadModel(
        backtest_detail=_FakeBacktestDetail(metrics=None),
        walk_forward=_NullWF(),
        correlation=_NullCorr(),
    )

    response = lab_evaluation(run_id="bt_missing", read_model=rm)

    assert response.success is False
    assert response.error is not None
    assert response.error["code"] == "not_found"
    assert response.error["details"]["run_id"] == "bt_missing"


def test_internal_error_on_readmodel_exception() -> None:
    class _Broken:
        def build_metrics_summary(self, run_id: str) -> dict:
            raise RuntimeError("metrics down")

        def build_equity_curve(self, run_id: str) -> Optional[dict]:
            return None

        def build_monthly_returns(self, run_id: str) -> Optional[dict]:
            return None

        def build_trade_structure(self, run_id: str) -> Optional[dict]:
            return None

        def build_execution_realism(self, run_id: str) -> Optional[dict]:
            return None

    # LabEvaluationReadModel._safe_call 会吞 metrics_summary 的异常，
    # 返回 None → 走 NOT_FOUND 路径（契约：metrics 是主锚点）。
    rm = LabEvaluationReadModel(
        backtest_detail=_Broken(),
        walk_forward=_NullWF(),
        correlation=_NullCorr(),
    )

    response = lab_evaluation(run_id="bt_test", read_model=rm)

    assert response.success is False
    assert response.error is not None
    assert response.error["code"] == "not_found"
