from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.backtesting.models import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResult,
    ValidationDecision,
)
from src.backtesting.validation import (
    attach_validation_decision,
    evaluate_promotion_validation,
)


def _metrics(
    *,
    total_trades: int = 90,
    expectancy: float = 0.12,
    profit_factor: float = 1.35,
    max_drawdown: float = 0.12,
) -> BacktestMetrics:
    return BacktestMetrics(
        total_trades=total_trades,
        winning_trades=55,
        losing_trades=35,
        win_rate=55 / 90,
        expectancy=expectancy,
        profit_factor=profit_factor,
        sharpe_ratio=1.2,
        sortino_ratio=1.4,
        max_drawdown=max_drawdown,
        max_drawdown_duration=12,
        avg_win=2.0,
        avg_loss=-1.0,
        avg_bars_held=6.0,
        total_pnl=250.0,
        total_pnl_pct=12.5,
        calmar_ratio=1.8,
        max_consecutive_wins=6,
        max_consecutive_losses=3,
    )


def _backtest_result(
    *,
    metrics: BacktestMetrics | None = None,
    execution_summary: dict | None = None,
    monte_carlo_result: dict | None = None,
    simulation_mode: str = "research",
) -> BacktestResult:
    config = BacktestConfig.from_flat(
        symbol="XAUUSD",
        timeframe="M30",
        start_time=datetime(2025, 9, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 3, 30, tzinfo=timezone.utc),
        initial_balance=10000.0,
        simulation_mode=simulation_mode,
    )
    return BacktestResult(
        config=config,
        run_id="bt_test",
        started_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        completed_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        trades=[],
        equity_curve=[],
        metrics=metrics or _metrics(),
        metrics_by_regime={},
        metrics_by_strategy={},
        metrics_by_confidence={},
        param_set={},
        execution_summary=execution_summary
        or {
            "simulation_mode": "execution_feasibility",
            "accepted_entries": 90,
            "rejected_entries": 10,
            "rejection_reasons": {},
        },
        monte_carlo_result=monte_carlo_result
        or {
            "sharpe_p_value": 0.04,
            "profit_factor_p_value": 0.08,
        },
    )


def _walk_forward_result(
    *,
    total_trades: int = 70,
    profit_factor: float = 1.2,
    max_drawdown: float = 0.10,
    overfitting_ratio: float = 1.2,
    consistency_rate: float = 0.75,
):
    return SimpleNamespace(
        aggregate_metrics=_metrics(
            total_trades=total_trades,
            expectancy=0.08,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
        ),
        overfitting_ratio=overfitting_ratio,
        consistency_rate=consistency_rate,
    )


def _paper_verdict(*, passed: bool, sufficient: bool = True):
    return SimpleNamespace(
        overall_pass=passed,
        trade_count_sufficient=sufficient,
        summary="paper shadow ok" if passed else "paper shadow drift",
    )


def test_evaluate_promotion_validation_rejects_divergent_signal() -> None:
    report = evaluate_promotion_validation(
        _backtest_result(),
        robustness_tier="divergent",
    )

    assert report.decision is ValidationDecision.REJECT
    assert report.checks["cross_tf_consistency"]["passed"] is False


def test_evaluate_promotion_validation_returns_refit_when_monte_carlo_fails() -> None:
    report = evaluate_promotion_validation(
        _backtest_result(
            monte_carlo_result={
                "sharpe_p_value": 0.25,
                "profit_factor_p_value": 0.22,
            }
        ),
        robustness_tier="tf_specific",
        walk_forward_result=_walk_forward_result(),
    )

    assert report.decision is ValidationDecision.REFIT
    assert report.checks["monte_carlo"]["passed"] is False


def test_evaluate_promotion_validation_returns_paper_only_before_paper_shadow_passes() -> None:
    report = evaluate_promotion_validation(
        _backtest_result(),
        robustness_tier="tf_specific",
        walk_forward_result=_walk_forward_result(),
    )

    assert report.decision is ValidationDecision.PAPER_ONLY
    assert report.checks["paper_shadow"]["passed"] is False


def test_evaluate_promotion_validation_promotes_tf_specific_to_active_guarded_after_paper_pass() -> None:
    report = evaluate_promotion_validation(
        _backtest_result(),
        robustness_tier="tf_specific",
        walk_forward_result=_walk_forward_result(),
        paper_verdict=_paper_verdict(passed=True),
    )

    assert report.decision is ValidationDecision.ACTIVE_GUARDED


def test_evaluate_promotion_validation_uses_execution_feasibility_result_for_execution_gate() -> None:
    report = evaluate_promotion_validation(
        _backtest_result(),
        robustness_tier="tf_specific",
        execution_feasibility_result=_backtest_result(
            execution_summary={
                "simulation_mode": "execution_feasibility",
                "accepted_entries": 0,
                "rejected_entries": 20,
                "rejection_reasons": {
                    "below_min_volume_for_execution_feasibility": 20,
                },
            },
            simulation_mode="execution_feasibility",
        ),
        walk_forward_result=_walk_forward_result(),
    )

    assert report.decision is ValidationDecision.REFIT
    assert report.checks["execution_feasibility"]["passed"] is False
    assert report.checks["execution_feasibility"]["blocked_reject_reasons"] == [
        "below_min_volume_for_execution_feasibility"
    ]
    assert report.checks["execution_feasibility"]["source_simulation_mode"].value == (
        "execution_feasibility"
    )


def test_attach_validation_decision_promotes_robust_to_active() -> None:
    result = attach_validation_decision(
        _backtest_result(),
        robustness_tier="robust",
        walk_forward_result=_walk_forward_result(),
        paper_verdict=_paper_verdict(passed=True),
        feature_candidate_id="feat_123",
        promoted_indicator_name="momentum_consensus14",
        strategy_candidate_id="cand_abc",
        research_provenance="mine_run_1",
    )

    assert result.validation_decision is not None
    assert result.validation_decision.decision is ValidationDecision.ACTIVE
    assert result.validation_decision.feature_candidate_id == "feat_123"
    assert result.validation_decision.promoted_indicator_name == "momentum_consensus14"
    assert result.validation_decision.strategy_candidate_id == "cand_abc"
