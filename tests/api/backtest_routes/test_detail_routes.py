"""Tests for backtest detail routes — P11 Phase 1。

直接以函数形式调路由端点（类似 tests/api/test_account_api.py 范式），
跳过 FastAPI DI 机制，把 BacktestDetailReadModel 作为参数传入。
"""

from __future__ import annotations

from typing import Any

from src.api.backtest_routes.detail.equity_curve import get_equity_curve
from src.api.backtest_routes.detail.execution_realism import get_execution_realism
from src.api.backtest_routes.detail.metrics_summary import get_metrics_summary
from src.api.backtest_routes.detail.monthly_returns import get_monthly_returns
from src.api.backtest_routes.detail.trade_structure import get_trade_structure
from src.readmodels.backtest_detail import BacktestDetailReadModel


class _FakeBacktestRepo:
    def __init__(
        self,
        *,
        runs: dict[str, dict[str, Any]] | None = None,
        trades: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._runs = runs or {}
        self._trades = trades or {}

    def fetch_run(self, run_id: str) -> dict[str, Any] | None:
        return self._runs.get(run_id)

    def fetch_trades(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._trades.get(run_id, []))


def _build_read_model(
    *,
    runs: dict[str, dict[str, Any]] | None = None,
    trades: dict[str, list[dict[str, Any]]] | None = None,
) -> BacktestDetailReadModel:
    repo = _FakeBacktestRepo(runs=runs, trades=trades)
    return BacktestDetailReadModel(backtest_repo=repo)


def _sample_run() -> dict[str, Any]:
    return {
        "run_id": "bt_sample",
        "created_at": "2026-04-20T00:00:00+00:00",
        "config": {"initial_balance": 10000.0},
        "metrics": {
            "total_trades": 4,
            "winning_trades": 3,
            "losing_trades": 1,
            "win_rate": 0.75,
            "profit_factor": 3.0,
            "expectancy": 75.0,
            "sharpe_ratio": 2.1,
            "sortino_ratio": 2.5,
            "calmar_ratio": 3.2,
            "max_drawdown": 100.0,
            "max_drawdown_duration": 5,
            "avg_win": 100.0,
            "avg_loss": -50.0,
            "avg_bars_held": 10.0,
            "total_pnl": 300.0,
            "total_pnl_pct": 0.03,
            "max_consecutive_wins": 3,
            "max_consecutive_losses": 1,
        },
        "equity_curve": [
            ["2026-04-01T00:00:00+00:00", 10000.0],
            ["2026-04-15T00:00:00+00:00", 10200.0],
            ["2026-04-30T00:00:00+00:00", 10300.0],
        ],
    }


# ────────────────────── metrics-summary ──────────────────────


def test_metrics_summary_success_response() -> None:
    rm = _build_read_model(runs={"bt_sample": _sample_run()})
    response = get_metrics_summary(run_id="bt_sample", read_model=rm)

    assert response.success is True
    assert response.error is None
    assert response.data is not None
    assert response.data.run_id == "bt_sample"
    assert response.data.total_pnl == 300.0
    assert response.data.max_drawdown_pct == 0.01  # 100 / 10000
    assert response.data.expectancy_r is None  # 无 trades
    assert response.data.freshness.source_kind == "native"


def test_metrics_summary_not_found_returns_error_envelope() -> None:
    rm = _build_read_model()
    response = get_metrics_summary(run_id="missing", read_model=rm)

    assert response.success is False
    assert response.data is None
    assert response.error is not None
    assert response.error["code"] == "not_found"
    assert response.error["details"] == {"run_id": "missing"}


# ────────────────────── equity-curve ──────────────────────


def test_equity_curve_success_response() -> None:
    rm = _build_read_model(runs={"bt_sample": _sample_run()})
    response = get_equity_curve(run_id="bt_sample", read_model=rm)

    assert response.success is True
    assert response.data is not None
    assert response.data.run_id == "bt_sample"
    assert response.data.initial_balance == 10000.0
    assert len(response.data.points) == 3
    first = response.data.points[0]
    assert first.equity == 10000.0
    assert first.drawdown == 0.0
    assert first.pnl_cumulative == 0.0


def test_equity_curve_not_found() -> None:
    rm = _build_read_model()
    response = get_equity_curve(run_id="missing", read_model=rm)

    assert response.success is False
    assert response.error is not None
    assert response.error["code"] == "not_found"


# ────────────────────── monthly-returns ──────────────────────


def test_monthly_returns_success_response() -> None:
    rm = _build_read_model(runs={"bt_sample": _sample_run()})
    response = get_monthly_returns(run_id="bt_sample", read_model=rm)

    assert response.success is True
    assert response.data is not None
    assert response.data.run_id == "bt_sample"
    assert len(response.data.months) == 1
    month = response.data.months[0]
    assert month.label == "2026-04"
    # initial_balance=10000，月末 10300 → pnl = 300
    assert month.pnl == 300.0
    assert abs(month.return_pct - 0.03) < 1e-9
    assert month.trade_count == 0  # 无 trades


def test_monthly_returns_not_found() -> None:
    rm = _build_read_model()
    response = get_monthly_returns(run_id="missing", read_model=rm)

    assert response.success is False
    assert response.error is not None
    assert response.error["code"] == "not_found"


# ────────────────────── Phase 4a: trade-structure ──────────────────────


def test_trade_structure_success_response() -> None:
    trades = [
        {
            "pnl": 10.0,
            "pnl_pct": 0.1,
            "hold_minutes": 30,
            "mfe_pct": 1.0,
            "mae_pct": 0.5,
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "position_size": 1.0,
            "exit_time": "2026-04-05T10:00:00+00:00",
        },
        {
            "pnl": -5.0,
            "pnl_pct": -0.05,
            "hold_minutes": 60,
            "mfe_pct": 0.5,
            "mae_pct": 1.2,
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "position_size": 1.0,
            "exit_time": "2026-04-06T10:00:00+00:00",
        },
    ]
    rm = _build_read_model(
        runs={"bt_sample": _sample_run()}, trades={"bt_sample": trades}
    )
    response = get_trade_structure(run_id="bt_sample", read_model=rm)

    assert response.success is True
    assert response.data is not None
    assert response.data.total_trades == 2
    assert response.data.partial_data is False
    assert response.data.avg_hold_minutes == 45.0
    assert response.data.max_loss_streak == 1


def test_trade_structure_not_found() -> None:
    rm = _build_read_model()
    response = get_trade_structure(run_id="missing", read_model=rm)

    assert response.success is False
    assert response.error is not None
    assert response.error["code"] == "not_found"


# ────────────────────── Phase 4a: execution-realism ──────────────────────


def test_execution_realism_available_false_for_legacy_run() -> None:
    rm = _build_read_model(runs={"bt_sample": _sample_run()})
    response = get_execution_realism(run_id="bt_sample", read_model=rm)

    assert response.success is True
    assert response.data is not None
    assert response.data.available is False
    assert response.data.spread_sensitivity is None


def test_execution_realism_not_found() -> None:
    rm = _build_read_model()
    response = get_execution_realism(run_id="missing", read_model=rm)

    assert response.success is False
    assert response.error is not None
    assert response.error["code"] == "not_found"
