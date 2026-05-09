from __future__ import annotations

import pytest

from src.config.models import RiskConfig
from src.config.risk import normalize_risk_config_payload


def test_risk_config_has_builtin_profile_contracts() -> None:
    cfg = RiskConfig()

    assert cfg.risk_profiles["standard_kline"].policy == "standard_kline"
    assert cfg.risk_profiles["standard_kline"].trade_frequency_enabled is True
    assert cfg.risk_profiles["recovery_budgeted"].policy == "recovery_budgeted"
    assert cfg.risk_profiles["recovery_budgeted"].trade_frequency_enabled is False


def test_risk_profile_sections_are_loaded_into_risk_config() -> None:
    payload = normalize_risk_config_payload(
        {"enabled": "true"},
        risk_profile_bindings_section={
            "tick_recovery_probe": "recovery_budgeted",
            "structured_pa_breakout": "standard_kline",
        },
        risk_profile_sections={
            "recovery_budgeted": {
                "policy": "recovery_budgeted",
                "trade_frequency_enabled": "false",
                "max_daily_recovery_loss_amount": "15.5",
                "max_rolling_recovery_loss_amount": "8.0",
                "rolling_loss_window_minutes": "90",
                "max_consecutive_loss_cycles": "2",
                "loss_lockout_minutes": "45",
            }
        },
    )

    cfg = RiskConfig.model_validate(payload)

    assert cfg.risk_profile_bindings == {
        "tick_recovery_probe": "recovery_budgeted",
        "structured_pa_breakout": "standard_kline",
    }
    profile = cfg.risk_profiles["recovery_budgeted"]
    assert profile.policy == "recovery_budgeted"
    assert profile.trade_frequency_enabled is False
    assert cfg.risk_profiles["standard_kline"].policy == "standard_kline"
    assert cfg.risk_profiles["standard_kline"].trade_frequency_enabled is True
    assert profile.max_daily_recovery_loss_amount == 15.5
    assert profile.max_rolling_recovery_loss_amount == 8.0
    assert profile.rolling_loss_window_minutes == 90
    assert profile.max_consecutive_loss_cycles == 2
    assert profile.loss_lockout_minutes == 45


def test_risk_profile_pre_trade_rules_are_loaded() -> None:
    payload = normalize_risk_config_payload(
        {"enabled": "true"},
        risk_profile_sections={
            "recovery_budgeted": {
                "policy": "recovery_budgeted",
                "pre_trade_rules": ("account_snapshot,margin_availability,protection"),
            }
        },
    )

    cfg = RiskConfig.model_validate(payload)

    assert cfg.risk_profiles["recovery_budgeted"].pre_trade_rules == [
        "account_snapshot",
        "margin_availability",
        "protection",
    ]


def test_recovery_budgeted_profile_defaults_to_recovery_rule_set() -> None:
    payload = normalize_risk_config_payload(
        {"enabled": "true"},
        risk_profile_sections={
            "recovery_fast": {
                "policy": "recovery_budgeted",
                "trade_frequency_enabled": "false",
            }
        },
    )

    cfg = RiskConfig.model_validate(payload)

    rules = cfg.risk_profiles["recovery_fast"].pre_trade_rules
    assert "trade_frequency" not in rules
    assert "market_structure" not in rules
    assert "margin_availability" in rules


def test_risk_profile_rejects_trade_frequency_flag_mismatch() -> None:
    with pytest.raises(ValueError, match="trade_frequency_enabled"):
        RiskConfig.model_validate(
            {
                "risk_profiles": {
                    "bad_profile": {
                        "policy": "recovery_budgeted",
                        "trade_frequency_enabled": False,
                        "pre_trade_rules": [
                            "account_snapshot",
                            "trade_frequency",
                        ],
                    }
                }
            }
        )


def test_preflight_risk_policy_section_is_loaded_into_risk_config() -> None:
    payload = normalize_risk_config_payload(
        {"enabled": "true", "max_volume_per_order": ""},
        {
            "live_max_positions_per_symbol": "6",
            "live_max_volume_per_order": "0.06",
            "live_max_volume_per_symbol": "",
            "live_max_daily_loss_limit_pct": "10.0",
        },
    )

    cfg = RiskConfig.model_validate(payload)

    assert cfg.max_volume_per_order is None
    assert cfg.preflight_policy.live_max_positions_per_symbol == 6
    assert cfg.preflight_policy.live_max_volume_per_order == 0.06
    assert cfg.preflight_policy.live_max_volume_per_symbol == 0.03
    assert cfg.preflight_policy.live_max_daily_loss_limit_pct == 10.0


def test_recovery_execution_canary_section_is_loaded_into_risk_config() -> None:
    payload = normalize_risk_config_payload(
        {"enabled": "true"},
        {},
        {
            "enabled": "true",
            "dry_run": "true",
            "order_kind": "market",
            "deviation": "12",
            "magic": "930001",
            "comment_prefix": "recovery-demo",
            "protective_stop_points": "120",
        },
    )

    cfg = RiskConfig.model_validate(payload)

    assert cfg.recovery_execution_canary.enabled is True
    assert cfg.recovery_execution_canary.dry_run is True
    assert cfg.recovery_execution_canary.order_kind == "market"
    assert cfg.recovery_execution_canary.deviation == 12
    assert cfg.recovery_execution_canary.magic == 930001
    assert cfg.recovery_execution_canary.comment_prefix == "recovery-demo"
    assert cfg.recovery_execution_canary.protective_stop_points == 120.0


def test_recovery_runtime_runner_section_is_loaded_into_risk_config() -> None:
    payload = normalize_risk_config_payload(
        {"enabled": "true"},
        {},
        {},
        {
            "enabled": "true",
            "dry_run": "true",
            "demo_only": "true",
            "symbol": "XAUUSD",
            "direction": "buy",
            "strategy": "tick_recovery_probe",
            "timeframe": "TICK",
            "base_volume": "0.01",
            "multiplier": "2.0",
            "max_steps": "1",
            "max_total_volume": "0.03",
            "max_next_volume": "0.02",
            "step_distance_points": "80",
            "max_step_adverse_move_points": "140",
            "recovery_target_points": "5",
            "point": "0.01",
            "direction_mode": "auto",
            "min_directional_move_points": "5",
            "max_directional_move_points": "150",
            "min_pressure_delta": "0.2",
            "max_entry_spread_points": "25",
            "slippage_budget_points": "2",
            "commission_points": "1",
            "min_net_profit_points": "5",
            "max_cycle_loss_points": "120",
            "max_cycle_duration_seconds": "900",
            "max_steps_exit_mode": "close_cycle",
            "max_steps_hold_seconds": "20",
            "entry_confirmation_snapshots": "2",
            "entry_confirmation_max_gap_seconds": "3",
            "real_trade_calibration_guard_enabled": "true",
            "real_trade_calibration_min_samples": "25",
            "real_trade_calibration_max_target_shortfall_p90_points": "0",
            "real_trade_calibration_min_net_margin_p50_points": "1.5",
            "protective_stop_points": "80",
            "snapshot_stale_seconds": "5",
            "risk_profile": "recovery_budgeted",
        },
    )

    cfg = RiskConfig.model_validate(payload)

    runner = cfg.recovery_runtime_runner
    assert runner.enabled is True
    assert runner.dry_run is True
    assert runner.demo_only is True
    assert runner.symbol == "XAUUSD"
    assert runner.direction == "buy"
    assert runner.strategy == "tick_recovery_probe"
    assert runner.max_steps == 1
    assert runner.max_total_volume == 0.03
    assert runner.max_next_volume == 0.02
    assert runner.step_distance_points == 80.0
    assert runner.max_step_adverse_move_points == 140.0
    assert runner.direction_mode == "auto"
    assert runner.min_directional_move_points == 5.0
    assert runner.max_directional_move_points == 150.0
    assert runner.min_pressure_delta == 0.2
    assert runner.max_entry_spread_points == 25.0
    assert runner.slippage_budget_points == 2.0
    assert runner.commission_points == 1.0
    assert runner.min_net_profit_points == 5.0
    assert runner.max_cycle_loss_points == 120.0
    assert runner.max_cycle_duration_seconds == 900.0
    assert runner.max_steps_exit_mode == "close_cycle"
    assert runner.max_steps_hold_seconds == 20.0
    assert runner.entry_confirmation_snapshots == 2
    assert runner.entry_confirmation_max_gap_seconds == 3.0
    assert runner.real_trade_calibration_guard_enabled is True
    assert runner.real_trade_calibration_min_samples == 25
    assert runner.real_trade_calibration_max_target_shortfall_p90_points == 0.0
    assert runner.real_trade_calibration_min_net_margin_p50_points == 1.5
    assert runner.protective_stop_points == 80.0
    assert runner.risk_profile == "recovery_budgeted"


def test_recovery_runtime_runner_rejects_profile_budget_fields() -> None:
    with pytest.raises(ValueError, match="risk_profiles.recovery_budgeted"):
        normalize_risk_config_payload(
            {"enabled": "true"},
            {},
            {},
            {
                "enabled": "true",
                "risk_profile": "recovery_budgeted",
                "max_daily_recovery_loss_amount": "12.5",
            },
        )


def test_recovery_runtime_runner_enforces_adr_014_cycle_caps_when_live_dispatch() -> (
    None
):
    """ADR-014: enabled=true + dry_run=false 时 cycle 限流字段必须非 0。"""
    base_payload = {
        "enabled": "true",
        "dry_run": "false",
        "demo_only": "true",
        "base_volume": "0.01",
        "max_steps": "1",
        "max_total_volume": "0.03",
        "step_distance_points": "80",
        "recovery_target_points": "5",
    }

    payload = normalize_risk_config_payload({"enabled": "true"}, {}, {}, base_payload)
    with pytest.raises(ValueError, match="ADR-014"):
        RiskConfig.model_validate(payload)

    full_caps = {
        **base_payload,
        "max_cycles_per_day": "20",
        "max_cycles_per_hour": "3",
        "cooldown_after_cycle_close_seconds": "180",
        "min_cycle_interval_seconds": "60",
        "real_trade_calibration_min_samples": "50",
    }
    payload = normalize_risk_config_payload({"enabled": "true"}, {}, {}, full_caps)
    cfg = RiskConfig.model_validate(payload)
    assert cfg.recovery_runtime_runner.max_cycles_per_day == 20
    assert cfg.recovery_runtime_runner.max_cycles_per_hour == 3
    assert cfg.recovery_runtime_runner.cooldown_after_cycle_close_seconds == 180
    assert cfg.recovery_runtime_runner.min_cycle_interval_seconds == 60
    assert cfg.recovery_runtime_runner.real_trade_calibration_min_samples == 50


def test_recovery_runtime_runner_skips_adr_014_caps_when_dry_run() -> None:
    """ADR-014: dry_run=true 或 enabled=false 时不强制 cycle 限流（允许测试桩）。"""
    payload = normalize_risk_config_payload(
        {"enabled": "true"},
        {},
        {},
        {"enabled": "true", "dry_run": "true"},
    )
    cfg = RiskConfig.model_validate(payload)
    assert cfg.recovery_runtime_runner.dry_run is True
    assert (
        cfg.recovery_runtime_runner.max_cycles_per_day == 0
    )  # 默认 0 允许，因为是 dry run

    payload = normalize_risk_config_payload(
        {"enabled": "true"}, {}, {}, {"enabled": "false"}
    )
    cfg = RiskConfig.model_validate(payload)
    assert cfg.recovery_runtime_runner.enabled is False
