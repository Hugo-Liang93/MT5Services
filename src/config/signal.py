from __future__ import annotations

import logging
from functools import lru_cache
from typing import Callable

logger = logging.getLogger(__name__)

from src.config.models.signal import SignalConfig
from src.config.utils import get_merged_config
from src.signals.contracts import normalize_strategy_deployments

_DEPRECATED_ECONOMIC_SIGNAL_KEYS = frozenset(
    {
        "economic_filter_enabled",
        "economic_lookahead_minutes",
        "economic_lookback_minutes",
        "economic_importance_min",
    }
)

_DEPRECATED_VOTE_SECTIONS = (
    "voting",
    "voting_groups",
    "standalone_override",
)

_DEPRECATED_VOTE_PREFIXES = (
    "voting_group.",
)

_DEPRECATED_VOTE_SIGNAL_KEYS = frozenset(
    {
        "voting_enabled",
        "voting_consensus_threshold",
        "voting_min_quorum",
        "voting_disagreement_penalty",
        "consensus_threshold",
        "min_quorum",
        "disagreement_penalty",
        "max_consensus_bonus",
    }
)


def _assert_no_deprecated_signal_keys(signal_section: dict[str, object]) -> None:
    deprecated = sorted(key for key in signal_section if key in _DEPRECATED_ECONOMIC_SIGNAL_KEYS)
    if deprecated:
        raise ValueError(
            "signal.ini no longer owns economic-event windows. "
            "Move these keys to economic.ini / EconomicConfig: "
            + ", ".join(deprecated)
        )


def _section_has_non_blank_values(section: dict[str, object]) -> bool:
    for value in section.values():
        if isinstance(value, str):
            if value.strip():
                return True
            continue
        if value is not None:
            return True
    return False


def _assert_no_vote_configuration(
    merged: dict[str, object],
    signal_section: dict[str, object],
) -> None:
    deprecated_signal_keys = sorted(
        key for key in signal_section if key in _DEPRECATED_VOTE_SIGNAL_KEYS
    )
    if deprecated_signal_keys:
        raise ValueError(
            "signal.ini no longer supports vote/consensus settings. "
            "Remove deprecated signal keys: "
            + ", ".join(deprecated_signal_keys)
        )

    deprecated_sections = [
        section_name
        for section_name in _DEPRECATED_VOTE_SECTIONS
        if _section_has_non_blank_values(dict(merged.get(section_name, {})))
    ]
    deprecated_sections.extend(
        section_name
        for section_name in merged
        if any(
            str(section_name).startswith(prefix)
            for prefix in _DEPRECATED_VOTE_PREFIXES
        )
        and _section_has_non_blank_values(dict(merged.get(section_name, {})))
    )
    if deprecated_sections:
        raise ValueError(
            "signal.ini no longer supports vote/consensus sections. "
            "Remove deprecated sections: "
            + ", ".join(sorted(set(str(name) for name in deprecated_sections)))
        )


def _resolve_spread_limits(
    signal_section: dict[str, object],
    session_section: dict[str, object],
) -> dict[str, float]:
    """将 session_spread_limits 解析为绝对点差值。

    base_spread_points × 乘数 = 实际上限。
    """
    raw = _normalize_float_map(session_section)
    base = _parse_float(signal_section.get("base_spread_points", 30))
    if base <= 0:
        raise ValueError("base_spread_points must be > 0")
    return {session: round(base * multiplier, 1) for session, multiplier in raw.items()}


def _resolve_max_spread(signal_section: dict[str, object]) -> float | None:
    """用 base_spread_points × max_spread_multiplier 计算全局点差上限。"""
    base = _parse_float(signal_section.get("base_spread_points", 30))
    if base <= 0:
        raise ValueError("base_spread_points must be > 0")
    multiplier = _parse_float(signal_section.get("max_spread_multiplier", 2.70))
    return round(base * multiplier, 1)


def _parse_float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _normalize_float_map(
    section: dict[str, object],
    *,
    key_transform: "Callable[[str], str] | None" = None,
) -> dict[str, float]:
    _transform = key_transform if key_transform is not None else (lambda v: v)
    normalized: dict[str, float] = {}
    for raw_key, raw_value in section.items():
        key = str(_transform(str(raw_key).strip()))
        if not key:
            continue
        try:
            normalized[key] = float(str(raw_value))
        except (TypeError, ValueError):
            continue
    return normalized


def _normalize_int_map(
    section: dict[str, object],
    *,
    key_transform: "Callable[[str], str] | None" = None,
) -> dict[str, int]:
    _transform = key_transform if key_transform is not None else (lambda v: v)
    normalized: dict[str, int] = {}
    for raw_key, raw_value in section.items():
        key = str(_transform(str(raw_key).strip()))
        if not key:
            continue
        try:
            normalized[key] = int(str(raw_value))
        except (TypeError, ValueError):
            continue
    return normalized


def _drop_blank_values(section: dict[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for raw_key, raw_value in section.items():
        key = str(raw_key).strip()
        if not key:
            continue
        if isinstance(raw_value, str) and raw_value.strip() == "":
            continue
        normalized[key] = raw_value
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
    _assert_no_deprecated_signal_keys(signal_section)
    _assert_no_vote_configuration(merged, signal_section)
    # 空值 → None，遵循 INI 约定：留空 = 不限制（与 risk.ini 加载一致）
    for _optional_key in ("max_concurrent_positions_per_symbol",):
        if str(signal_section.get(_optional_key, "")).strip() == "":
            signal_section[_optional_key] = None
    signal_section = _drop_blank_values(signal_section)
    preview_section = dict(merged.get("preview", {}))
    regime_section = dict(merged.get("regime", {}))
    position_section = dict(merged.get("position_management", {}))
    circuit_breaker_section = dict(merged.get("circuit_breaker", {}))
    contract_sizes_section = dict(merged.get("contract_sizes", {}))
    session_spread_limits_section = dict(merged.get("session_spread_limits", {}))
    strategy_sessions_section = dict(merged.get("strategy_sessions", {}))
    strategy_timeframes_section = dict(merged.get("strategy_timeframes", {}))
    account_bindings: dict[str, list[str]] = {}
    execution_costs_section = dict(merged.get("execution_costs", {}))
    market_structure_section = dict(merged.get("market_structure", {}))
    safety_section = dict(merged.get("safety", {}))
    calibrator_section = dict(merged.get("calibrator", {}))
    calibrator_recency_by_tf_section = dict(
        merged.get("calibrator.recency_hours_by_tf", {})
    )
    equity_curve_section = dict(merged.get("equity_curve_filter", {}))
    perf_tracker_section = dict(merged.get("performance_tracker", {}))
    pnl_circuit_section = dict(merged.get("pnl_circuit_breaker", {}))
    htf_cache_section = dict(merged.get("htf_cache", {}))
    htf_indicators_section = dict(merged.get("htf_indicators", {}))
    # [strategy_htf] 已废弃：HTF 配置改由策略声明自动推导
    # （htf_required_indicators + _htf 属性 → SignalModule.htf_target_config()）
    signal_quality_section = dict(merged.get("signal_quality", {}))
    htf_alignment_section = dict(merged.get("htf_alignment", {}))
    timeframe_risk_section = dict(merged.get("timeframe_risk", {}))
    timeframe_min_confidence_section = dict(merged.get("timeframe_min_confidence", {}))
    htf_conflict_block_section = dict(merged.get("htf_conflict_block", {}))
    pending_entry_section = dict(merged.get("pending_entry", {}))
    # per-TF pending entry 覆盖: [pending_entry.M5], [pending_entry.H1] 等
    pending_entry_tf_overrides: dict[str, dict[str, float]] = {}
    for section_key in merged:
        if str(section_key).startswith("pending_entry."):
            tf = str(section_key).split(".", 1)[1].strip().upper()
            if tf:
                pending_entry_tf_overrides[tf] = {
                    str(k).strip(): float(v)
                    for k, v in dict(merged[section_key]).items()
                    if str(k).strip()
                }

    # ── Chandelier Exit 配置 ──────────────────────────────────────────────
    chandelier_section = dict(merged.get("chandelier", {}))
    exit_profile_section = dict(merged.get("exit_profile", {}))
    exit_profile_tf_scale_section = dict(merged.get("exit_profile.tf_scale", {}))
    calibrator_section = _drop_blank_values(calibrator_section)
    equity_curve_section = _drop_blank_values(equity_curve_section)
    perf_tracker_section = _drop_blank_values(perf_tracker_section)
    pnl_circuit_section = _drop_blank_values(pnl_circuit_section)
    htf_cache_section = _drop_blank_values(htf_cache_section)
    signal_quality_section = _drop_blank_values(signal_quality_section)
    htf_alignment_section = _drop_blank_values(htf_alignment_section)
    chandelier_section = _drop_blank_values(chandelier_section)

    # 解析 aggression 覆盖：category__regime = alpha
    chandelier_aggression_overrides: dict[tuple[str, str], float] = {}
    for raw_key, raw_value in exit_profile_section.items():
        key = str(raw_key).strip()
        if "__" not in key:
            continue
        parts = key.split("__", 1)
        if len(parts) == 2:
            cat, regime = parts[0].strip().lower(), parts[1].strip().lower()
            try:
                chandelier_aggression_overrides[(cat, regime)] = float(raw_value)
            except (TypeError, ValueError):
                logger.warning(
                    "signal.ini [exit_profile] invalid alpha for '%s': %r",
                    key,
                    raw_value,
                )

    # 解析 per-TF trail 缩放
    chandelier_tf_trail_scale = _normalize_float_map(
        exit_profile_tf_scale_section,
        key_transform=lambda value: value.upper(),
    )

    # Only keep snapshot_dedupe_window_seconds from [preview] section
    renamed_preview = {
        key: value
        for key, value in preview_section.items()
        if key == "snapshot_dedupe_window_seconds"
    }
    renamed_position = {
        ("position_reconcile_interval" if key == "reconcile_interval" else key): value
        for key, value in position_section.items()
    }
    # ── Regime 检测阈值 ──────────────────────────────────────────────────
    regime_detector_section = dict(merged.get("regime_detector", {}))
    regime_sizing_section = dict(merged.get("regime_sizing", {}))
    regime_sizing_section = _drop_blank_values(regime_sizing_section)

    # ── 策略级可调参数 [strategy_params] + [strategy_params.<TF>] ─────────
    strategy_params_section = dict(merged.get("strategy_params", {}))
    strategy_params: dict[str, float] = {}
    for raw_key, raw_value in strategy_params_section.items():
        key = str(raw_key).strip()
        if key:
            try:
                strategy_params[key] = float(raw_value)
            except (TypeError, ValueError):
                logger.warning(
                    "signal.ini [strategy_params] invalid value for '%s': %r (skipped)",
                    key,
                    raw_value,
                )

    # Per-TF 策略参数: [strategy_params.M5], [strategy_params.H1] 等
    strategy_params_per_tf: dict[str, dict[str, float]] = {}
    for section_name, section_data in merged.items():
        if str(section_name).startswith("account_bindings."):
            account_alias = str(section_name).split(".", 1)[1].strip()
            if not account_alias:
                continue
            strategies = [
                item.strip()
                for item in str(dict(section_data).get("strategies", "")).split(",")
                if item.strip()
            ]
            if strategies:
                account_bindings[account_alias] = strategies
            continue
        if not section_name.startswith("strategy_params."):
            continue
        tf = section_name[len("strategy_params.") :].strip().upper()
        if not tf:
            continue
        tf_params: dict[str, float] = {}
        for raw_key, raw_value in dict(section_data).items():
            key = str(raw_key).strip()
            if key:
                try:
                    tf_params[key] = float(raw_value)
                except (TypeError, ValueError):
                    logger.warning(
                        "signal.ini [strategy_params.%s] invalid value for '%s': %r (skipped)",
                        tf,
                        key,
                        raw_value,
                    )
        if tf_params:
            strategy_params_per_tf[tf] = tf_params

    # ── Regime 亲和度覆盖 [regime_affinity.*] ────────────────────────────
    regime_affinity_overrides: dict[str, dict[str, float]] = {}
    for section_name, section_data in merged.items():
        if not section_name.startswith("regime_affinity."):
            continue
        strategy_name = section_name[len("regime_affinity.") :]
        if not strategy_name:
            continue
        affinity_map: dict[str, float] = {}
        for key, value in dict(section_data).items():
            try:
                affinity_map[str(key).strip()] = float(value)
            except (TypeError, ValueError):
                continue
        if affinity_map:
            regime_affinity_overrides[strategy_name] = affinity_map

    # ── 策略部署合同 [strategy_deployment.<strategy>] ────────────────────
    strategy_deployments_raw: dict[str, dict[str, object]] = {}
    for section_name, section_data in merged.items():
        if not str(section_name).startswith("strategy_deployment."):
            continue
        strategy_name = str(section_name)[len("strategy_deployment.") :].strip()
        if not strategy_name:
            continue
        strategy_deployments_raw[strategy_name] = {
            str(key).strip(): value for key, value in dict(section_data).items()
        }
    strategy_deployments = normalize_strategy_deployments(strategy_deployments_raw)

    # ── Pending Entry 解析 ─────────────────────────────────────────────
    pending_entry_timeout_bars: dict[str, float] = {}
    pending_entry_strategy_overrides: dict[str, dict[str, float]] = {}
    pending_entry_simple: dict[str, object] = {}
    for raw_key, raw_value in pending_entry_section.items():
        key = str(raw_key).strip()
        # timeout_bars_M5 = 2.0 → timeout_bars["M5"] = 2.0
        if key.startswith("timeout_bars_"):
            tf = key[len("timeout_bars_") :].strip().upper()
            if tf:
                try:
                    pending_entry_timeout_bars[tf] = float(raw_value)
                except (TypeError, ValueError):
                    pass
        # strategy_override: supertrend__pullback_atr_factor = 0.4
        elif "__" in key:
            parts = key.split("__", 1)
            if len(parts) == 2:
                strategy_name, param_name = parts
                if strategy_name not in pending_entry_strategy_overrides:
                    pending_entry_strategy_overrides[strategy_name] = {}
                try:
                    pending_entry_strategy_overrides[strategy_name][param_name] = float(
                        raw_value
                    )
                except (TypeError, ValueError):
                    pass
        else:
            pending_entry_simple[key] = raw_value

    pending_entry_simple = _drop_blank_values(pending_entry_simple)

    # ── Intrabar Trading 解析 ────────────────────────────────────────────
    intrabar_trading_section = dict(merged.get("intrabar_trading", {}))
    intrabar_trading_section = _drop_blank_values(intrabar_trading_section)
    # [intrabar_trading.trigger] 节：parent_tf = trigger_tf
    intrabar_trigger_section = dict(merged.get("intrabar_trading.trigger", {}))
    intrabar_trigger_map: dict[str, str] = {}
    for raw_key, raw_value in intrabar_trigger_section.items():
        parent_tf = str(raw_key).strip().upper()
        trigger_tf = str(raw_value).strip().upper()
        if parent_tf and trigger_tf:
            intrabar_trigger_map[parent_tf] = trigger_tf
    intrabar_enabled_strategies = [
        s.strip()
        for s in str(intrabar_trading_section.get("enabled_strategies", "")).split(",")
        if s.strip()
    ]

    combined = {
        **signal_section,
        **renamed_preview,
        **regime_section,
        **renamed_position,
        **circuit_breaker_section,
        **execution_costs_section,
        **market_structure_section,
        **safety_section,
        **{f"regime_{key}": value for key, value in regime_detector_section.items()},
        **{f"regime_{key}": value for key, value in regime_sizing_section.items()},
        "strategy_params": strategy_params,
        "strategy_params_per_tf": strategy_params_per_tf,
        "regime_affinity_overrides": regime_affinity_overrides,
        "strategy_deployments": strategy_deployments,
        "contract_size_map": _normalize_float_map(
            contract_sizes_section,
            key_transform=lambda value: value.upper(),
        ),
        "timeframe_risk_multipliers": _normalize_float_map(
            timeframe_risk_section,
            key_transform=lambda value: value.upper(),
        ),
        "timeframe_min_confidence": _normalize_float_map(
            timeframe_min_confidence_section,
            key_transform=lambda value: value.upper(),
        ),
        "htf_conflict_block_timeframes": frozenset(
            tf.strip().upper()
            for tf in str(
                htf_conflict_block_section.get("enabled_timeframes", "")
            ).split(",")
            if tf.strip()
        ),
        "htf_conflict_exempt_categories": frozenset(
            cat.strip()
            for cat in str(
                htf_conflict_block_section.get("exempt_categories", "reversion")
            ).split(",")
            if cat.strip()
        ),
        "session_spread_limits": _resolve_spread_limits(
            signal_section, session_spread_limits_section
        ),
        "strategy_sessions": _normalize_session_map(strategy_sessions_section),
        "strategy_timeframes": _normalize_session_map(strategy_timeframes_section),
        "account_bindings": account_bindings,
        **{f"calibrator_{key}": value for key, value in calibrator_section.items()},
        "calibrator_recency_hours_by_tf": _normalize_int_map(
            calibrator_recency_by_tf_section, key_transform=lambda tf: tf.upper()
        ),
        **{
            f"equity_curve_filter_{key}": value
            for key, value in equity_curve_section.items()
        },
        **{f"perf_tracker_{key}": value for key, value in perf_tracker_section.items()},
        **{
            f"perf_tracker_pnl_circuit_{key}": value
            for key, value in pnl_circuit_section.items()
        },
        **{f"htf_cache_{key}": value for key, value in htf_cache_section.items()},
        **{
            f"signal_quality_{key}": value
            for key, value in signal_quality_section.items()
        },
        **{
            f"htf_indicators_{key}": value
            for key, value in htf_indicators_section.items()
            if key != "intrabar_confidence_factor"
        },
        **(
            {
                "intrabar_confidence_factor": htf_indicators_section[
                    "intrabar_confidence_factor"
                ]
            }
            if "intrabar_confidence_factor" in htf_indicators_section
            else {}
        ),
        **{
            f"htf_alignment_{key}": value
            for key, value in htf_alignment_section.items()
        },
        **{
            f"pending_entry_{key}": value for key, value in pending_entry_simple.items()
        },
        **(
            {"pending_entry_timeout_bars": pending_entry_timeout_bars}
            if pending_entry_timeout_bars
            else {}
        ),
        **(
            {"pending_entry_strategy_overrides": pending_entry_strategy_overrides}
            if pending_entry_strategy_overrides
            else {}
        ),
        **(
            {"pending_entry_tf_overrides": pending_entry_tf_overrides}
            if pending_entry_tf_overrides
            else {}
        ),
        # ── Chandelier Exit ──
        **{f"chandelier_{key}": value for key, value in chandelier_section.items()},
        **(
            {"chandelier_aggression_overrides": chandelier_aggression_overrides}
            if chandelier_aggression_overrides
            else {}
        ),
        **(
            {"chandelier_tf_trail_scale": chandelier_tf_trail_scale}
            if chandelier_tf_trail_scale
            else {}
        ),
        # ── Intrabar Trading ──
        "intrabar_trading_enabled": intrabar_trading_section.get("enabled", "false"),
        **(
            {"intrabar_trading_trigger_map": intrabar_trigger_map}
            if intrabar_trigger_map
            else {}
        ),
        "intrabar_trading_min_parent_bar_progress": intrabar_trading_section.get(
            "min_parent_bar_progress", 0.15
        ),
        "intrabar_trading_min_stable_bars": intrabar_trading_section.get(
            "min_stable_bars", 3
        ),
        "intrabar_trading_min_confidence": intrabar_trading_section.get(
            "min_confidence", 0.75
        ),
        **(
            {"intrabar_trading_enabled_strategies": intrabar_enabled_strategies}
            if intrabar_enabled_strategies
            else {}
        ),
        "intrabar_trading_atr_source": intrabar_trading_section.get(
            "atr_source", "last_confirmed"
        ),
    }
    # 自动计算 max_spread_points（base > 0 时覆盖手动值）
    auto_max_spread = _resolve_max_spread(signal_section)
    if auto_max_spread is not None:
        combined["max_spread_points"] = auto_max_spread
    return SignalConfig.model_validate(combined)
