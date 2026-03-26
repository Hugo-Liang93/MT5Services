"""回测配置加载：从 config/backtest.ini 读取默认参数。

优先级（高到低）：
1. CLI 参数 / API 请求体
2. config/backtest.local.ini（本地覆盖，.gitignore）
3. config/backtest.ini（已提交的基础配置）
4. BacktestConfig 字段默认值（models.py）
"""

from __future__ import annotations

import configparser
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
)


def get_backtest_defaults() -> Dict[str, Any]:
    """从 backtest.ini 加载回测默认配置。

    返回平铺的字典，键名与 BacktestConfig 字段对应。
    未配置的字段不包含在返回值中（由 BacktestConfig 默认值兜底）。
    """
    parser = configparser.ConfigParser()
    ini_path = os.path.join(_CONFIG_DIR, "backtest.ini")
    local_path = os.path.join(_CONFIG_DIR, "backtest.local.ini")

    read_files = parser.read([ini_path, local_path], encoding="utf-8")
    if not read_files:
        logger.debug("No backtest.ini found, using built-in defaults")
        return {}

    result: Dict[str, Any] = {}

    # [backtest] section
    if parser.has_section("backtest"):
        _set_float(result, parser, "backtest", "default_initial_balance", "initial_balance")
        _set_int(result, parser, "backtest", "default_warmup_bars", "warmup_bars")
        _set_int(result, parser, "backtest", "max_positions_per_symbol", "max_positions")
        _set_float(result, parser, "backtest", "commission_per_lot", "commission_per_lot")
        _set_float(result, parser, "backtest", "slippage_points", "slippage_points")
        _set_float(result, parser, "backtest", "min_confidence", "min_confidence")
        _set_float(result, parser, "backtest", "contract_size", "contract_size")
        _set_float(result, parser, "backtest", "risk_percent", "risk_percent")
        _set_bool(result, parser, "backtest", "enable_state_machine", "enable_state_machine")
        _set_int(result, parser, "backtest", "min_preview_stable_bars", "min_preview_stable_bars")

    # [filters] section
    if parser.has_section("filters"):
        _set_bool(result, parser, "filters", "enabled", "filters_enabled")
        _set_bool(result, parser, "filters", "session_filter_enabled", "filter_session_enabled")
        _set_str(result, parser, "filters", "allowed_sessions", "filter_allowed_sessions")
        _set_bool(
            result, parser, "filters", "session_transition_enabled",
            "filter_session_transition_enabled",
        )
        _set_int(
            result, parser, "filters", "session_transition_cooldown_minutes",
            "filter_session_transition_cooldown",
        )
        _set_bool(result, parser, "filters", "volatility_filter_enabled", "filter_volatility_enabled")
        _set_float(
            result, parser, "filters", "volatility_spike_multiplier",
            "filter_volatility_spike_multiplier",
        )
        _set_bool(result, parser, "filters", "spread_filter_enabled", "filter_spread_enabled")
        _set_float(result, parser, "filters", "max_spread_points", "filter_max_spread_points")

    # [position] section — 持仓管理参数
    if parser.has_section("position"):
        _set_float(result, parser, "position", "trailing_atr_multiplier", "trailing_atr_multiplier")
        _set_float(result, parser, "position", "breakeven_atr_threshold", "breakeven_atr_threshold")
        _set_bool(result, parser, "position", "end_of_day_close_enabled", "end_of_day_close_enabled")
        _set_int(result, parser, "position", "end_of_day_close_hour_utc", "end_of_day_close_hour_utc")
        _set_int(result, parser, "position", "end_of_day_close_minute_utc", "end_of_day_close_minute_utc")

    # [pending_entry] section — 价格确认入场参数
    if parser.has_section("pending_entry"):
        _set_bool(result, parser, "pending_entry", "enabled", "pending_entry_enabled")
        _set_int(result, parser, "pending_entry", "expiry_bars", "pending_entry_expiry_bars")
        _set_float(
            result, parser, "pending_entry", "pullback_atr_factor",
            "pending_entry_pullback_atr_factor",
        )
        _set_float(
            result, parser, "pending_entry", "chase_atr_factor",
            "pending_entry_chase_atr_factor",
        )
        _set_float(
            result, parser, "pending_entry", "momentum_atr_factor",
            "pending_entry_momentum_atr_factor",
        )
        _set_float(
            result, parser, "pending_entry", "symmetric_atr_factor",
            "pending_entry_symmetric_atr_factor",
        )

    # [confidence] section — 置信度管线开关
    if parser.has_section("confidence"):
        _set_bool(result, parser, "confidence", "enable_regime_affinity", "enable_regime_affinity")
        _set_bool(
            result, parser, "confidence", "enable_performance_tracker",
            "enable_performance_tracker",
        )
        _set_bool(result, parser, "confidence", "enable_calibrator", "enable_calibrator")
        _set_bool(result, parser, "confidence", "enable_htf_alignment", "enable_htf_alignment")

    # [persistence] section
    if parser.has_section("persistence"):
        _set_int(
            result, parser, "persistence", "max_signal_evaluations",
            "max_signal_evaluations",
        )

    # [optimizer] section
    if parser.has_section("optimizer"):
        _set_str(result, parser, "optimizer", "default_search_mode", "search_mode")
        _set_int(result, parser, "optimizer", "max_combinations", "max_combinations")
        _set_str(result, parser, "optimizer", "sort_metric", "sort_metric")

    # signal.ini / signal.local.ini defaults — sizing-related runtime constraints
    try:
        from src.config import get_signal_config

        signal_config = get_signal_config()
        result.setdefault("min_volume", float(signal_config.min_volume))
        result.setdefault("max_volume", float(signal_config.max_volume))
    except Exception:
        logger.debug("Failed to load signal config defaults for backtest", exc_info=True)

    # risk.ini / risk.local.ini defaults — risk guard constraints
    try:
        from src.config import get_risk_config

        risk_config = get_risk_config()
        if "max_positions" not in result and risk_config.max_positions_per_symbol is not None:
            result["max_positions"] = int(risk_config.max_positions_per_symbol)
        if risk_config.max_volume_per_order is not None:
            result.setdefault("max_volume_per_order", float(risk_config.max_volume_per_order))
        if risk_config.max_volume_per_symbol is not None:
            result.setdefault("max_volume_per_symbol", float(risk_config.max_volume_per_symbol))
        if risk_config.daily_loss_limit_pct is not None:
            result.setdefault("daily_loss_limit_pct", float(risk_config.daily_loss_limit_pct))
        if risk_config.max_trades_per_day is not None:
            result.setdefault("max_trades_per_day", int(risk_config.max_trades_per_day))
        if risk_config.max_trades_per_hour is not None:
            result.setdefault("max_trades_per_hour", int(risk_config.max_trades_per_hour))
    except Exception:
        logger.debug("Failed to load risk config defaults for backtest", exc_info=True)

    logger.debug("Loaded backtest defaults from %s: %d keys", read_files, len(result))
    return result


def _set_float(
    result: Dict[str, Any],
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    target: str,
) -> None:
    try:
        result[target] = parser.getfloat(section, key)
    except (configparser.NoOptionError, ValueError):
        pass


def _set_int(
    result: Dict[str, Any],
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    target: str,
) -> None:
    try:
        result[target] = parser.getint(section, key)
    except (configparser.NoOptionError, ValueError):
        pass


def _set_bool(
    result: Dict[str, Any],
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    target: str,
) -> None:
    try:
        result[target] = parser.getboolean(section, key)
    except (configparser.NoOptionError, ValueError):
        pass


def _set_str(
    result: Dict[str, Any],
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    target: str,
) -> None:
    try:
        val = parser.get(section, key)
        if val:
            result[target] = val
    except configparser.NoOptionError:
        pass
