from __future__ import annotations

from src.research.state_edge.evaluation import (
    build_overlay_validation_report,
    build_threshold_grid_report,
    evaluate_overlay_increment,
)


def _result(
    *,
    pf: float,
    expectancy: float,
    max_dd: float,
    trades: int,
    pnl: float = 0.0,
    threshold: float | None = None,
    state_edge_overlay: dict | None = None,
    strategy_stats: dict | None = None,
    regime_stats: dict | None = None,
    execution_summary: dict | None = None,
    raw_trades: list[dict] | None = None,
) -> dict:
    result = {
        "threshold": threshold,
        "metrics": {
            "pf": pf,
            "expectancy": expectancy,
            "max_dd": max_dd,
            "trades": trades,
            "pnl": pnl,
        },
        "strategy_stats": strategy_stats or {},
        "regime_stats": regime_stats or {},
        "execution_summary": execution_summary or {},
        "raw_trades": raw_trades or [],
    }
    if state_edge_overlay is not None:
        result["state_edge_overlay"] = state_edge_overlay
    return {key: value for key, value in result.items() if value is not None}


def test_decision_accepts_pf_or_expectancy_gain_without_dd_breach() -> None:
    baseline = _result(pf=1.10, expectancy=1.0, max_dd=5.0, trades=40)
    overlay = _result(pf=1.30, expectancy=1.2, max_dd=5.4, trades=34)

    decision = evaluate_overlay_increment(baseline, overlay)

    assert decision.status == "accepted"
    assert decision.pf_delta == 0.20
    assert decision.expectancy_delta == 0.20


def test_decision_rejects_pf_only_gain_when_pnl_and_expectancy_degrade() -> None:
    baseline = _result(
        pf=2.299,
        expectancy=10.97,
        max_dd=3.69,
        trades=53,
        pnl=581.49,
    )
    overlay = _result(
        pf=2.315,
        expectancy=9.75,
        max_dd=3.69,
        trades=45,
        pnl=438.94,
    )

    decision = evaluate_overlay_increment(baseline, overlay)

    assert decision.status == "rejected"
    assert decision.reason == "return_expectancy_degraded"
    assert decision.pf_delta == 0.016
    assert decision.expectancy_delta == -1.22
    assert decision.pnl_delta == -142.55


def test_decision_rejects_drawdown_breach() -> None:
    baseline = _result(pf=1.10, expectancy=1.0, max_dd=5.0, trades=40)
    overlay = _result(pf=1.40, expectancy=1.2, max_dd=6.0, trades=34)

    decision = evaluate_overlay_increment(baseline, overlay)

    assert decision.status == "rejected"
    assert decision.reason == "max_dd_breach"


def test_decision_marks_refit_when_trade_count_is_too_low() -> None:
    baseline = _result(pf=1.10, expectancy=1.0, max_dd=5.0, trades=40)
    overlay = _result(pf=1.80, expectancy=2.0, max_dd=4.5, trades=4)

    decision = evaluate_overlay_increment(baseline, overlay, min_trades=10)

    assert decision.status == "refit"
    assert decision.reason == "insufficient_trades"


def test_threshold_report_selects_best_accepted_threshold_by_trade_increment() -> None:
    baseline = _result(pf=1.0, expectancy=1.0, max_dd=5.0, trades=40)
    overlays = [
        {"threshold": 0.50, **_result(pf=1.10, expectancy=1.1, max_dd=5.2, trades=36)},
        {"threshold": 0.55, **_result(pf=1.30, expectancy=1.2, max_dd=5.1, trades=32)},
        {"threshold": 0.60, **_result(pf=1.25, expectancy=1.4, max_dd=5.3, trades=20)},
    ]

    report = build_threshold_grid_report(baseline=baseline, overlays=overlays)

    assert report.status == "accepted"
    assert report.best_threshold == 0.60
    assert report.best_decision is not None
    assert report.best_decision.status == "accepted"


def test_overlay_validation_report_includes_shadow_check_and_filter_diagnostics() -> (
    None
):
    baseline = _result(
        pf=2.299,
        expectancy=10.97,
        max_dd=3.69,
        trades=53,
        pnl=581.49,
        strategy_stats={
            "structured_open_range_breakout": {"n": 22, "w": 10, "pnl": -11.79},
            "structured_regime_exhaustion": {"n": 5, "w": 5, "pnl": 434.05},
        },
        regime_stats={
            "trending": {"n": 46, "w": 21, "pnl": 513.51},
        },
        raw_trades=[
            {
                "signal_id": "bt_profit",
                "entry_time": "2026-01-01T01:00:00+00:00",
                "strategy": "structured_open_range_breakout",
                "direction": "buy",
                "entry_price": 2020.0,
                "exit_time": "2026-01-01T06:00:00+00:00",
                "exit_price": 2042.0,
                "pnl": 104.43,
                "pnl_pct": 5.22,
                "exit_reason": "take_profit",
                "bars_held": 5,
                "regime": "trending",
                "confidence": 0.9,
            }
        ],
    )
    shadow = _result(
        pf=2.299,
        expectancy=10.97,
        max_dd=3.69,
        trades=53,
        pnl=581.49,
        state_edge_overlay={"mode": "shadow", "observed": 49, "blocked": 0},
    )
    filter_result = _result(
        threshold=0.50,
        pf=2.315,
        expectancy=9.75,
        max_dd=3.69,
        trades=45,
        pnl=438.94,
        state_edge_overlay={
            "mode": "filter",
            "threshold": 0.50,
            "filter_directions": ["buy"],
            "observed": 58,
            "allowed": 39,
            "blocked": 19,
            "missing_predictions": 0,
            "blocked_by_direction": {"buy": 19},
            "blocked_by_reason": {
                "state_edge_probability_below_threshold": 19,
            },
        },
        strategy_stats={
            "structured_open_range_breakout": {"n": 19, "w": 9, "pnl": -20.00},
            "structured_regime_exhaustion": {"n": 4, "w": 4, "pnl": 333.29},
        },
        regime_stats={
            "trending": {"n": 39, "w": 19, "pnl": 402.00},
        },
        execution_summary={
            "accepted_entries": 45,
            "rejected_entries": 19,
            "rejection_reasons": {"state_edge_filter": 19},
            "blocked_entry_events": [
                {
                    "source": "state_edge_overlay",
                    "execution_reason": "state_edge_filter",
                    "reason": "state_edge_probability_below_threshold",
                    "bar_time": "2026-01-01T01:00:00+00:00",
                    "bar_index": 1,
                    "strategy": "structured_open_range_breakout",
                    "direction": "buy",
                    "confidence": 0.9,
                    "regime": "trending",
                    "price": 2020.0,
                    "direction_probability": 0.42,
                    "threshold": 0.50,
                }
            ],
        },
    )

    report = build_overlay_validation_report(
        baseline=baseline,
        shadow=shadow,
        filters=[filter_result],
    )
    payload = report.to_dict()

    assert payload["status"] == "rejected"
    assert payload["shadow_check"]["status"] == "passed"
    assert (
        payload["threshold_report"]["threshold_results"][0]["decision"]["status"]
        == "rejected"
    )
    assert payload["filter_diagnostics"][0]["threshold"] == 0.50
    assert payload["filter_diagnostics"][0]["blocked_entries"] == 19
    assert payload["filter_diagnostics"][0]["blocked_by_direction"] == {"buy": 19}
    assert payload["filter_diagnostics"][0]["exact_blocked_trades_available"] is True
    assert (
        payload["filter_diagnostics"][0]["diagnostic_scope"] == "exact_blocked_entries"
    )
    assert payload["filter_diagnostics"][0]["blocked_entry_summary"] == {
        "count": 1,
        "by_strategy": {"structured_open_range_breakout": 1},
        "by_regime": {"trending": 1},
        "by_direction": {"buy": 1},
        "by_reason": {"state_edge_probability_below_threshold": 1},
    }
    assert payload["filter_diagnostics"][0]["blocked_entry_events"][0]["strategy"] == (
        "structured_open_range_breakout"
    )
    assert payload["filter_diagnostics"][0]["blocked_trade_attribution"] == {
        "matching_key": ["bar_time=entry_time", "strategy", "direction"],
        "baseline_trades_available": True,
        "matched_trades": 1,
        "unmatched_events": 0,
        "matched_pnl": 104.43,
        "matched_wins": 1,
        "matched_losses": 0,
        "matched_by_exit_reason": {"take_profit": 1},
        "matched_by_strategy": {
            "structured_open_range_breakout": {
                "n": 1,
                "wins": 1,
                "losses": 0,
                "pnl": 104.43,
            }
        },
        "unmatched_by_strategy": {},
        "attributed_events": [
            {
                "bar_time": "2026-01-01T01:00:00+00:00",
                "strategy": "structured_open_range_breakout",
                "direction": "buy",
                "direction_probability": 0.42,
                "threshold": 0.50,
                "matched_baseline_trade": True,
                "baseline_trade": {
                    "signal_id": "bt_profit",
                    "entry_time": "2026-01-01T01:00:00+00:00",
                    "exit_time": "2026-01-01T06:00:00+00:00",
                    "entry_price": 2020.0,
                    "exit_price": 2042.0,
                    "pnl": 104.43,
                    "pnl_pct": 5.22,
                    "exit_reason": "take_profit",
                    "bars_held": 5,
                    "regime": "trending",
                    "confidence": 0.9,
                },
            }
        ],
    }
    assert (
        payload["filter_diagnostics"][0]["strategy_deltas"][
            "structured_regime_exhaustion"
        ]["pnl_delta"]
        == -100.76
    )


def test_threshold_report_reads_backtest_runner_state_edge_threshold() -> None:
    baseline = _result(pf=1.0, expectancy=1.0, max_dd=5.0, trades=40)
    overlay = _result(pf=1.2, expectancy=1.1, max_dd=5.0, trades=32)
    overlay["state_edge_threshold"] = 0.55

    report = build_threshold_grid_report(baseline=baseline, overlays=[overlay])

    assert report.threshold_results[0]["threshold"] == 0.55
    assert report.best_threshold == 0.55
