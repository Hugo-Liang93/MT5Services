from __future__ import annotations

from datetime import datetime, timezone

from src.trading.recovery.analytics import RecoveryDecisionAnalytics
from src.trading.recovery.models import RecoveryDecision


def _now() -> datetime:
    return datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc)


def test_analytics_counts_reason_policy_cost_and_recent_decisions() -> None:
    analytics = RecoveryDecisionAnalytics(recent_limit=2)

    analytics.record(
        observed_at=_now(),
        action="hold",
        reason="direction_signal_too_weak",
        status="hold",
        execution={
            "direction_policy": {
                "reason": "direction_signal_too_weak",
                "direction": None,
            }
        },
    )
    analytics.record(
        observed_at=_now(),
        action="hold",
        reason="spread_too_wide",
        status="hold",
        execution={
            "direction_policy": {
                "reason": "auto_direction_buy",
                "direction": "buy",
            },
            "cost_gate": {
                "allowed": False,
                "reason": "spread_too_wide",
            },
        },
    )
    analytics.record(
        observed_at=_now(),
        action="close_cycle",
        reason="net_recovery_target_reached",
        status="closed",
        decision=RecoveryDecision(
            action="close_cycle",
            reason="net_recovery_target_reached",
            metadata={"net_profit_points": 31.0, "target_net_points": 30.0},
        ),
    )

    snapshot = analytics.snapshot()

    assert snapshot["total_decisions"] == 3
    assert snapshot["action_counts"]["hold"] == 2
    assert snapshot["reason_counts"]["spread_too_wide"] == 1
    assert snapshot["direction_policy_reason_counts"]["auto_direction_buy"] == 1
    assert snapshot["direction_counts"]["buy"] == 1
    assert snapshot["cost_gate_reason_counts"]["spread_too_wide"] == 1
    assert snapshot["cost_gate_allowed_counts"]["blocked"] == 1
    assert snapshot["net_exit"]["sample_count"] == 1
    assert snapshot["net_exit"]["last_net_profit_points"] == 31.0
    assert len(snapshot["recent_decisions"]) == 2
    assert snapshot["recent_decisions"][-1]["reason"] == "net_recovery_target_reached"


def test_analytics_extracts_policy_metadata_from_trade_payload() -> None:
    analytics = RecoveryDecisionAnalytics()

    analytics.record(
        observed_at=_now(),
        action="open_initial",
        reason="initial_dispatched",
        status="submitted",
        execution={
            "status": "submitted",
            "payload": {
                "metadata": {
                    "direction_policy": {
                        "reason": "auto_direction_sell",
                        "direction": "sell",
                    },
                    "cost_gate": {
                        "allowed": True,
                        "reason": "entry_cost_ok",
                    },
                }
            },
        },
    )

    snapshot = analytics.snapshot()

    assert snapshot["execution_status_counts"]["submitted"] == 1
    assert snapshot["direction_policy_reason_counts"]["auto_direction_sell"] == 1
    assert snapshot["direction_counts"]["sell"] == 1
    assert snapshot["cost_gate_reason_counts"]["entry_cost_ok"] == 1
    assert snapshot["cost_gate_allowed_counts"]["allowed"] == 1


def test_analytics_reports_entry_calibration_distributions() -> None:
    analytics = RecoveryDecisionAnalytics(recent_limit=3)

    analytics.record(
        observed_at=_now(),
        action="hold",
        reason="spread_too_wide",
        status="hold",
        execution={
            "direction_policy": {
                "reason": "auto_direction_buy",
                "direction": "buy",
                "metadata": {
                    "price_change_points": 7.0,
                    "pressure_delta": 0.35,
                    "min_directional_move_points": 5.0,
                    "min_pressure_delta": 0.2,
                },
            },
            "cost_gate": {
                "allowed": False,
                "reason": "spread_too_wide",
                "metadata": {
                    "spread_points": 31.0,
                    "max_entry_spread_points": 25.0,
                    "recovery_target_points": 30.0,
                    "expected_net_points": -4.0,
                    "net_margin_points": -9.0,
                    "target_shortfall_points": 9.0,
                    "min_net_profit_points": 5.0,
                },
            },
        },
    )
    analytics.record(
        observed_at=_now(),
        action="hold",
        reason="direction_pressure_mismatch",
        status="hold",
        execution={
            "direction_policy": {
                "reason": "direction_pressure_mismatch",
                "direction": None,
                "metadata": {
                    "price_change_points": -11.0,
                    "pressure_delta": -0.45,
                    "min_directional_move_points": 5.0,
                    "min_pressure_delta": 0.2,
                },
            },
        },
    )
    analytics.record(
        observed_at=_now(),
        action="hold",
        reason="spread_too_wide",
        status="hold",
        execution={
            "direction_policy": {
                "reason": "auto_direction_sell",
                "direction": "sell",
                "metadata": {
                    "price_change_points": -9.0,
                    "pressure_delta": -0.28,
                },
            },
            "cost_gate": {
                "allowed": False,
                "reason": "spread_too_wide",
                "metadata": {
                    "spread_points": 27.0,
                    "max_entry_spread_points": 25.0,
                    "expected_net_points": 0.0,
                    "net_margin_points": -5.0,
                    "target_shortfall_points": 5.0,
                },
            },
        },
    )

    calibration = analytics.snapshot()["entry_calibration"]

    assert calibration["thresholds"]["max_entry_spread_points"] == 25.0
    assert calibration["thresholds"]["min_directional_move_points"] == 5.0
    assert calibration["thresholds"]["min_pressure_delta"] == 0.2
    assert calibration["spread_points"]["sample_count"] == 2
    assert calibration["spread_points"]["avg"] == 29.0
    assert calibration["spread_points"]["min"] == 27.0
    assert calibration["spread_points"]["max"] == 31.0
    assert calibration["abs_price_change_points"]["sample_count"] == 3
    assert calibration["abs_price_change_points"]["avg"] == 9.0
    assert calibration["abs_pressure_delta"]["sample_count"] == 3
    assert calibration["expected_net_points"]["sample_count"] == 2
    assert calibration["expected_net_points"]["min"] == -4.0
    assert calibration["net_margin_points"]["sample_count"] == 2
    assert calibration["net_margin_points"]["avg"] == -7.0
    assert calibration["target_shortfall_points"]["sample_count"] == 2
    assert calibration["target_shortfall_points"]["max"] == 9.0
