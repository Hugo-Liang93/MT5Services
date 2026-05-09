from __future__ import annotations

from src.research.entry_meta.evaluation import build_entry_meta_overlay_report


def test_pf_gain_cannot_accept_when_return_and_expectancy_degrade() -> None:
    baseline = {
        "metrics": {
            "trades": 24,
            "pnl": 120.0,
            "pf": 1.20,
            "expectancy": 5.0,
            "max_dd": 8.0,
        }
    }
    filter_result = {
        "entry_meta_threshold": 0.65,
        "metrics": {
            "trades": 24,
            "pnl": 90.0,
            "pf": 1.25,
            "expectancy": 3.75,
            "max_dd": 7.5,
        },
    }

    report = build_entry_meta_overlay_report(
        baseline,
        shadow=None,
        filters=[filter_result],
        min_trades=10,
    )

    payload = report.to_dict()
    decision = payload["threshold_report"]["threshold_results"][0]["decision"]
    assert report.status == "rejected"
    assert decision["status"] == "rejected"
    assert decision["reason"] == "return_expectancy_degraded"
    assert decision["deltas"]["pnl"] == -30.0
    assert decision["deltas"]["expectancy"] == -1.25


def test_return_gain_can_accept_when_drawdown_worsens_within_limit() -> None:
    baseline = {
        "metrics": {
            "trades": 24,
            "pnl": 120.0,
            "pf": 1.20,
            "expectancy": 5.0,
            "max_dd": 8.0,
        }
    }
    filter_result = {
        "entry_meta_threshold": 0.65,
        "metrics": {
            "trades": 24,
            "pnl": 130.0,
            "pf": 1.25,
            "expectancy": 5.5,
            "max_dd": 8.5,
        },
    }

    report = build_entry_meta_overlay_report(
        baseline,
        shadow=None,
        filters=[filter_result],
        max_dd_worsen_ratio=0.10,
        min_trades=10,
    )

    decision = report.to_dict()["threshold_report"]["threshold_results"][0]["decision"]
    assert report.status == "accepted"
    assert decision["status"] == "accepted"
    assert decision["reason"] == "incremental_gain"


def test_blocked_events_are_attributed_to_matching_baseline_raw_trades() -> None:
    baseline = {
        "metrics": {
            "trades": 12,
            "pnl": 120.0,
            "pf": 1.20,
            "expectancy": 10.0,
            "max_dd": 8.0,
        },
        "raw_trades": [
            {
                "entry_time": "2026-01-01T00:00:00+00:00",
                "exit_time": "2026-01-01T03:00:00+00:00",
                "strategy": "breakout",
                "direction": "BUY",
                "pnl": 42.5,
                "close_reason": "take_profit",
            },
            {
                "entry_time": "2026-01-01T01:00:00+00:00",
                "exit_time": "2026-01-01T04:00:00+00:00",
                "strategy": "pullback",
                "direction": "sell",
                "pnl": -15.0,
                "exit_reason": "stop_loss",
            },
        ],
    }
    filter_result = {
        "entry_meta_threshold": 0.70,
        "metrics": {
            "trades": 10,
            "pnl": 130.0,
            "pf": 1.25,
            "expectancy": 13.0,
            "max_dd": 7.0,
        },
        "execution_summary": {
            "blocked_entry_events": [
                {
                    "source": "entry_meta_overlay",
                    "bar_time": "2026-01-01T00:00:00Z",
                    "strategy": "Breakout",
                    "direction": "buy",
                    "take_entry_prob": 0.41,
                    "block_entry_prob": 0.59,
                    "threshold": 0.70,
                },
                {
                    "bar_time": "2026-01-01T01:00:00+00:00",
                    "strategy": "pullback",
                    "direction": "SELL",
                    "take_entry_prob": 0.33,
                    "block_entry_prob": 0.67,
                    "threshold": 0.70,
                },
                {
                    "source": "state_edge_overlay",
                    "bar_time": "2026-01-01T02:00:00+00:00",
                    "strategy": "breakout",
                    "direction": "buy",
                },
                {
                    "source": "entry_meta_overlay",
                    "bar_time": "2026-01-01T05:00:00+00:00",
                    "strategy": "missing",
                    "direction": "buy",
                    "take_entry_prob": 0.20,
                    "block_entry_prob": 0.80,
                    "threshold": 0.70,
                },
            ]
        },
    }

    payload = build_entry_meta_overlay_report(
        baseline,
        shadow=None,
        filters=[filter_result],
        min_trades=10,
    ).to_dict()

    attribution = payload["filter_diagnostics"][0]["blocked_trade_attribution"]
    assert attribution["matched_trades"] == 2
    assert attribution["unmatched_events"] == 1
    assert attribution["matched_pnl"] == 27.5
    assert attribution["matched_wins"] == 1
    assert attribution["matched_losses"] == 1
    assert attribution["matched_by_exit_reason"] == {
        "stop_loss": {"matched_trades": 1, "matched_pnl": -15.0},
        "take_profit": {"matched_trades": 1, "matched_pnl": 42.5},
    }
    assert attribution["attributed_events"][0]["take_entry_prob"] == 0.41
    assert attribution["attributed_events"][0]["block_entry_prob"] == 0.59
    assert attribution["attributed_events"][0]["threshold"] == 0.70
    assert attribution["attributed_events"][0]["matched_trade"] == {
        "entry_time": "2026-01-01T00:00:00+00:00",
        "exit_time": "2026-01-01T03:00:00+00:00",
        "strategy": "breakout",
        "direction": "BUY",
        "pnl": 42.5,
        "exit_reason": "take_profit",
    }


def test_duplicate_match_keys_consume_distinct_baseline_trades_once() -> None:
    baseline = {
        "metrics": {
            "trades": 12,
            "pnl": 120.0,
            "pf": 1.20,
            "expectancy": 10.0,
            "max_dd": 8.0,
        },
        "raw_trades": [
            {
                "entry_time": "2026-01-01T00:00:00+00:00",
                "exit_time": "2026-01-01T02:00:00+00:00",
                "strategy": "breakout",
                "direction": "buy",
                "pnl": 10.0,
                "exit_reason": "take_profit",
            },
            {
                "entry_time": "2026-01-01T00:00:00+00:00",
                "exit_time": "2026-01-01T03:00:00+00:00",
                "strategy": "breakout",
                "direction": "BUY",
                "pnl": -3.0,
                "exit_reason": "stop_loss",
            },
        ],
    }
    filter_result = {
        "entry_meta_threshold": 0.70,
        "metrics": {
            "trades": 10,
            "pnl": 130.0,
            "pf": 1.25,
            "expectancy": 13.0,
            "max_dd": 7.0,
        },
        "execution_summary": {
            "blocked_entry_events": [
                {
                    "source": "entry_meta_overlay",
                    "bar_time": "2026-01-01T00:00:00Z",
                    "strategy": "breakout",
                    "direction": "buy",
                    "take_entry_prob": 0.41,
                    "block_entry_prob": 0.59,
                    "threshold": 0.70,
                },
                {
                    "source": "entry_meta_overlay",
                    "bar_time": "2026-01-01T00:00:00+00:00",
                    "strategy": "Breakout",
                    "direction": "buy",
                    "take_entry_prob": 0.39,
                    "block_entry_prob": 0.61,
                    "threshold": 0.70,
                },
                {
                    "source": "entry_meta_overlay",
                    "bar_time": "2026-01-01T00:00:00+00:00",
                    "strategy": "breakout",
                    "direction": "buy",
                    "take_entry_prob": 0.37,
                    "block_entry_prob": 0.63,
                    "threshold": 0.70,
                },
            ]
        },
    }

    payload = build_entry_meta_overlay_report(
        baseline,
        shadow=None,
        filters=[filter_result],
        min_trades=10,
    ).to_dict()

    attribution = payload["filter_diagnostics"][0]["blocked_trade_attribution"]
    assert attribution["matched_trades"] == 2
    assert attribution["unmatched_events"] == 1
    assert attribution["matched_pnl"] == 7.0
    assert [
        event["matched_trade"]["pnl"] for event in attribution["attributed_events"]
    ] == [10.0, -3.0]
