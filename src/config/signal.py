from __future__ import annotations

from functools import lru_cache

from src.config.models.signal import SignalConfig
from src.config.utils import get_merged_config


def _normalize_float_map(
    section: dict[str, object],
    *,
    key_transform=lambda value: value,
) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for raw_key, raw_value in section.items():
        key = str(key_transform(str(raw_key).strip()))
        if not key:
            continue
        try:
            normalized[key] = float(raw_value)
        except (TypeError, ValueError):
            continue
    return normalized


def _normalize_session_map(section: dict[str, object]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for raw_key, raw_value in section.items():
        key = str(raw_key).strip()
        values = [
            str(item).strip().lower()
            for item in str(raw_value).split(",")
            if str(item).strip()
        ]
        if key and values:
            normalized[key] = values
    return normalized


@lru_cache
def get_signal_config() -> SignalConfig:
    merged = get_merged_config("signal.ini")
    signal_section = dict(merged.get("signal", {}))
    preview_section = dict(merged.get("preview", {}))
    regime_section = dict(merged.get("regime", {}))
    voting_section = dict(merged.get("voting", {}))
    position_section = dict(merged.get("position_management", {}))
    circuit_breaker_section = dict(merged.get("circuit_breaker", {}))
    contract_sizes_section = dict(merged.get("contract_sizes", {}))
    session_spread_limits_section = dict(merged.get("session_spread_limits", {}))
    strategy_sessions_section = dict(merged.get("strategy_sessions", {}))
    strategy_timeframes_section = dict(merged.get("strategy_timeframes", {}))
    execution_costs_section = dict(merged.get("execution_costs", {}))
    market_structure_section = dict(merged.get("market_structure", {}))
    safety_section = dict(merged.get("safety", {}))
    trade_triggers_section = dict(merged.get("trade_triggers", {}))
    voting_groups_section = dict(merged.get("voting_groups", {}))
    standalone_override_section = dict(merged.get("standalone_override", {}))
    perf_tracker_section = dict(merged.get("performance_tracker", {}))
    htf_cache_section = dict(merged.get("htf_cache", {}))
    signal_quality_section = dict(merged.get("signal_quality", {}))

    renamed_preview = {
        ("min_preview_confidence" if key == "min_confidence" else key): value
        for key, value in preview_section.items()
    }
    field_renames = {
        "stable_seconds": "preview_stable_seconds",
        "cooldown_seconds": "preview_cooldown_seconds",
        "min_bar_progress": "min_preview_bar_progress",
    }
    renamed_preview = {
        field_renames.get(key, key): value for key, value in renamed_preview.items()
    }
    renamed_position = {
        ("position_reconcile_interval" if key == "reconcile_interval" else key): value
        for key, value in position_section.items()
    }
    voting_field_renames = {
        "enabled": "voting_enabled",
        "consensus_threshold": "voting_consensus_threshold",
        "min_quorum": "voting_min_quorum",
        "disagreement_penalty": "voting_disagreement_penalty",
    }
    renamed_voting = {
        voting_field_renames.get(key, key): value
        for key, value in voting_section.items()
    }
    # ── Voting Groups 解析 ──────────────────────────────────────────────
    # [voting_groups] 节：group_name = strategy1,strategy2,...
    # 每个 group 可有对应的 [voting_group.<name>] 子节覆盖参数
    voting_group_configs: list[dict] = []
    for raw_key, raw_value in voting_groups_section.items():
        group_name = str(raw_key).strip()
        if not group_name:
            continue
        strategies = [
            s.strip() for s in str(raw_value).split(",") if s.strip()
        ]
        if not strategies:
            continue
        # 读取对应的子节参数（如有）
        subsection = dict(merged.get(f"voting_group.{group_name}", {}))
        group_cfg: dict = {"name": group_name, "strategies": strategies}
        if "consensus_threshold" in subsection:
            try:
                group_cfg["consensus_threshold"] = float(subsection["consensus_threshold"])
            except (TypeError, ValueError):
                pass
        if "min_quorum" in subsection:
            try:
                group_cfg["min_quorum"] = int(subsection["min_quorum"])
            except (TypeError, ValueError):
                pass
        if "disagreement_penalty" in subsection:
            try:
                group_cfg["disagreement_penalty"] = float(subsection["disagreement_penalty"])
            except (TypeError, ValueError):
                pass
        voting_group_configs.append(group_cfg)

    # ── Standalone Override 解析 ────────────────────────────────────────
    standalone_override = [
        s.strip()
        for s in str(standalone_override_section.get("strategies", "")).split(",")
        if s.strip()
    ]

    combined = {
        **signal_section,
        **renamed_preview,
        **regime_section,
        **renamed_voting,
        **renamed_position,
        **circuit_breaker_section,
        **execution_costs_section,
        **market_structure_section,
        **safety_section,
        "contract_size_map": _normalize_float_map(
            contract_sizes_section,
            key_transform=lambda value: value.upper(),
        ),
        "session_spread_limits": _normalize_float_map(session_spread_limits_section),
        "strategy_sessions": _normalize_session_map(strategy_sessions_section),
        "strategy_timeframes": _normalize_session_map(strategy_timeframes_section),
        "trade_trigger_strategies": [
            s.strip()
            for s in str(trade_triggers_section.get("allowed_strategies", "")).split(",")
            if s.strip()
        ],
        "voting_group_configs": voting_group_configs,
        "standalone_override": standalone_override,
        **{
            f"perf_tracker_{key}": value
            for key, value in perf_tracker_section.items()
        },
        **{
            f"htf_cache_{key}": value
            for key, value in htf_cache_section.items()
        },
        **{
            f"signal_quality_{key}": value
            for key, value in signal_quality_section.items()
        },
    }
    return SignalConfig.model_validate(combined)
