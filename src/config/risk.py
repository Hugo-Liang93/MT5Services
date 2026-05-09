from __future__ import annotations

from typing import Any, Mapping


_OPTIONAL_INT_KEYS = (
    "max_positions_per_symbol",
    "max_open_positions_total",
    "max_pending_orders_per_symbol",
    "max_trades_per_day",
    "max_trades_per_hour",
)

_OPTIONAL_FLOAT_KEYS = (
    "max_volume_per_order",
    "max_volume_per_symbol",
    "max_net_lots_per_symbol",
    "daily_loss_limit_pct",
    "margin_safety_factor",
)

_PREFLIGHT_INT_KEYS = ("live_max_positions_per_symbol",)

_PREFLIGHT_FLOAT_KEYS = (
    "live_max_volume_per_order",
    "live_max_volume_per_symbol",
    "live_max_daily_loss_limit_pct",
)

_RECOVERY_CANARY_INT_KEYS = ("deviation", "magic")

_RECOVERY_CANARY_FLOAT_KEYS = ("protective_stop_points",)

_RECOVERY_RUNNER_INT_KEYS = (
    "max_steps",
    "min_step_interval_ms",
    "deviation",
    "magic",
    "max_cycles_per_session",
    "max_cycles_per_day",
    "max_cycles_per_hour",
    "entry_confirmation_snapshots",
    "real_trade_calibration_min_samples",
)

_RECOVERY_RUNNER_FLOAT_KEYS = (
    "base_volume",
    "multiplier",
    "max_total_volume",
    "max_next_volume",
    "step_distance_points",
    "max_step_adverse_move_points",
    "recovery_target_points",
    "point",
    "volume_step",
    "contract_size",
    "min_directional_move_points",
    "max_directional_move_points",
    "min_pressure_delta",
    "max_entry_spread_points",
    "slippage_budget_points",
    "commission_points",
    "min_net_profit_points",
    "max_cycle_loss_points",
    "max_cycle_duration_seconds",
    "max_steps_hold_seconds",
    "protective_stop_points",
    "min_cycle_interval_seconds",
    "cooldown_after_cycle_close_seconds",
    "snapshot_stale_seconds",
    "blocked_dispatch_retry_seconds",
    "entry_confirmation_max_gap_seconds",
    "real_trade_calibration_max_target_shortfall_p90_points",
    "real_trade_calibration_min_net_margin_p50_points",
)

_RECOVERY_RUNNER_PROFILE_BUDGET_KEYS = (
    "max_daily_recovery_loss_amount",
    "max_rolling_recovery_loss_amount",
    "rolling_loss_window_minutes",
    "max_consecutive_loss_cycles",
    "loss_lockout_minutes",
)

_RISK_PROFILE_INT_KEYS = (
    "rolling_loss_window_minutes",
    "max_consecutive_loss_cycles",
    "loss_lockout_minutes",
)

_RISK_PROFILE_FLOAT_KEYS = (
    "max_daily_recovery_loss_amount",
    "max_rolling_recovery_loss_amount",
)

_RISK_PROFILE_LIST_KEYS = ("pre_trade_rules",)

_DEFAULT_RISK_PROFILES = {
    "standard_kline": {
        "policy": "standard_kline",
        "trade_frequency_enabled": True,
        "pre_trade_rules": [
            "account_snapshot",
            "daily_loss_limit",
            "margin_availability",
            "trade_frequency",
            "protection",
            "session_window",
            "market_structure",
            "economic_event",
            "calendar_health",
        ],
    },
    "recovery_budgeted": {
        "policy": "recovery_budgeted",
        "trade_frequency_enabled": False,
        "pre_trade_rules": [
            "account_snapshot",
            "daily_loss_limit",
            "margin_availability",
            "protection",
            "session_window",
            "economic_event",
            "calendar_health",
        ],
    },
}


def normalize_risk_config_payload(
    risk_section: Mapping[str, Any] | None,
    preflight_policy_section: Mapping[str, Any] | None = None,
    recovery_execution_canary_section: Mapping[str, Any] | None = None,
    recovery_runtime_runner_section: Mapping[str, Any] | None = None,
    risk_profile_bindings_section: Mapping[str, Any] | None = None,
    risk_profile_sections: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    risk_config: dict[str, Any] = dict(risk_section or {})
    for optional_int_key in _OPTIONAL_INT_KEYS:
        if str(risk_config.get(optional_int_key, "")).strip() == "":
            risk_config[optional_int_key] = None
    for optional_float_key in _OPTIONAL_FLOAT_KEYS:
        if str(risk_config.get(optional_float_key, "")).strip() == "":
            risk_config[optional_float_key] = None
    if str(risk_config.get("data_unavailable_policy", "")).strip() == "":
        risk_config.pop("data_unavailable_policy", None)

    preflight_policy = _normalize_preflight_policy(preflight_policy_section)
    if preflight_policy:
        risk_config["preflight_policy"] = preflight_policy
    recovery_execution_canary = _normalize_recovery_execution_canary(
        recovery_execution_canary_section
    )
    if recovery_execution_canary:
        risk_config["recovery_execution_canary"] = recovery_execution_canary
    recovery_runtime_runner = _normalize_recovery_runtime_runner(
        recovery_runtime_runner_section
    )
    if recovery_runtime_runner:
        risk_config["recovery_runtime_runner"] = recovery_runtime_runner
    profile_bindings = _normalize_risk_profile_bindings(risk_profile_bindings_section)
    if profile_bindings:
        risk_config["risk_profile_bindings"] = profile_bindings
    profiles = _normalize_risk_profiles(risk_profile_sections)
    if profiles:
        risk_config["risk_profiles"] = profiles
    return risk_config


def _normalize_preflight_policy(
    section: Mapping[str, Any] | None,
) -> dict[str, Any]:
    policy: dict[str, Any] = dict(section or {})
    for key in _PREFLIGHT_INT_KEYS + _PREFLIGHT_FLOAT_KEYS:
        if str(policy.get(key, "")).strip() == "":
            policy.pop(key, None)
    return policy


def _normalize_recovery_execution_canary(
    section: Mapping[str, Any] | None,
) -> dict[str, Any]:
    policy: dict[str, Any] = dict(section or {})
    for key in _RECOVERY_CANARY_INT_KEYS + _RECOVERY_CANARY_FLOAT_KEYS:
        if str(policy.get(key, "")).strip() == "":
            policy.pop(key, None)
    return policy


def _normalize_recovery_runtime_runner(
    section: Mapping[str, Any] | None,
) -> dict[str, Any]:
    policy: dict[str, Any] = dict(section or {})
    misplaced = [
        key for key in _RECOVERY_RUNNER_PROFILE_BUDGET_KEYS if key in policy
    ]
    if misplaced:
        raise ValueError(
            "recovery_runtime_runner loss-budget fields moved to "
            "risk_profiles.recovery_budgeted: "
            + ", ".join(misplaced)
        )
    for key in _RECOVERY_RUNNER_INT_KEYS + _RECOVERY_RUNNER_FLOAT_KEYS:
        if str(policy.get(key, "")).strip() == "":
            policy.pop(key, None)
    return policy


def _normalize_risk_profile_bindings(
    section: Mapping[str, Any] | None,
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for raw_strategy, raw_profile in dict(section or {}).items():
        strategy = str(raw_strategy or "").strip()
        profile = str(raw_profile or "").strip()
        if strategy and profile:
            bindings[strategy] = profile
    return bindings


def _normalize_risk_profiles(
    sections: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if not sections:
        return {}
    profiles: dict[str, dict[str, Any]] = {
        name: dict(payload) for name, payload in _DEFAULT_RISK_PROFILES.items()
    }
    for raw_name, raw_section in dict(sections or {}).items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        profile = {
            **dict(profiles.get(name, {})),
            **dict(raw_section or {}),
        }
        for key in _RISK_PROFILE_INT_KEYS + _RISK_PROFILE_FLOAT_KEYS:
            if str(profile.get(key, "")).strip() == "":
                profile.pop(key, None)
        for key in _RISK_PROFILE_LIST_KEYS:
            if key in profile:
                profile[key] = _split_csv(profile.get(key))
        profiles[name] = profile
    return profiles


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = str(value or "").split(",")
    return [str(item or "").strip() for item in raw_items if str(item or "").strip()]
