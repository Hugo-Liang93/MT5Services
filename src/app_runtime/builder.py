"""App builder: construct all runtime components without starting threads."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.app_runtime.container import AppContainer
from src.app_runtime.builder_phases import (
    build_account_runtime_layer,
    build_market_layer,
    build_monitoring_layer,
    build_paper_trading_layer,
    build_runtime_controls,
    build_runtime_read_models,
    build_signal_layer,
    build_studio_service_layer,
    build_trading_layer,
)
from src.config import (
    get_effective_config_snapshot,
    get_risk_config,
    get_runtime_identity,
    get_runtime_ingest_settings,
    get_runtime_market_settings,
    get_signal_config,
)
from src.config import get_economic_config

logger = logging.getLogger(__name__)


def _enum_or_raw(value: Any) -> str:
    if value is None:
        return ""
    return getattr(value, "value", value)


def _iter_spread_cost_sanity_issues(signal_config: Any) -> list[str]:
    base_spread = float(getattr(signal_config, "base_spread_points", 0.0) or 0.0)
    if base_spread <= 0:
        return []

    issues: list[str] = []
    max_spread_points = getattr(signal_config, "max_spread_points", None)
    if max_spread_points is not None:
        global_cap = float(max_spread_points or 0.0)
        if global_cap > 0 and global_cap < base_spread:
            issues.append(
                "global spread cap is below baseline spread "
                f"(max_spread_points={global_cap:.1f} < base_spread_points={base_spread:.1f})"
            )

    session_limits = getattr(signal_config, "session_spread_limits", {}) or {}
    if isinstance(session_limits, dict):
        too_tight_sessions = {
            session: float(limit)
            for session, limit in session_limits.items()
            if limit is not None and float(limit) > 0 and float(limit) < base_spread
        }
        if too_tight_sessions:
            details = ", ".join(
                f"{session}={limit:.1f}"
                for session, limit in sorted(too_tight_sessions.items())
            )
            issues.append(
                "session spread cap is below baseline spread "
                f"(base_spread_points={base_spread:.1f}; {details})"
            )

    return issues


def _log_spread_cost_sanity(signal_config: Any) -> None:
    base_spread = float(getattr(signal_config, "base_spread_points", 0.0) or 0.0)
    if base_spread <= 0:
        return

    max_spread_to_stop_ratio = float(
        getattr(signal_config, "max_spread_to_stop_ratio", 0.0) or 0.0
    )
    max_spread_points = getattr(signal_config, "max_spread_points", None)
    min_stop_from_base = None
    min_stop_from_cap = None
    if max_spread_to_stop_ratio > 0:
        min_stop_from_base = round(base_spread / max_spread_to_stop_ratio, 1)
        if max_spread_points is not None and float(max_spread_points or 0.0) > 0:
            min_stop_from_cap = round(
                float(max_spread_points) / max_spread_to_stop_ratio,
                1,
            )

    logger.info(
        "Execution cost gates: base_spread=%.1f, max_spread_points=%s, "
        "max_spread_to_stop_ratio=%.2f, min_stop_points_from_base=%s, "
        "min_stop_points_from_cap=%s",
        base_spread,
        (
            f"{float(max_spread_points):.1f}"
            if max_spread_points is not None and float(max_spread_points or 0.0) > 0
            else "n/a"
        ),
        max_spread_to_stop_ratio,
        f"{min_stop_from_base:.1f}" if min_stop_from_base is not None else "n/a",
        f"{min_stop_from_cap:.1f}" if min_stop_from_cap is not None else "n/a",
    )

    for issue in _iter_spread_cost_sanity_issues(signal_config):
        logger.warning("Spread/cost config is internally inconsistent: %s", issue)


def build_app_container(
    *,
    signal_config_loader: Any = None,
) -> AppContainer:
    """Build all components and wire dependencies. No threads are started."""
    if signal_config_loader is None:
        signal_config_loader = get_signal_config

    container = AppContainer()
    container.runtime_identity = get_runtime_identity()
    ingest_settings = get_runtime_ingest_settings()
    market_settings = get_runtime_market_settings()
    signal_config = signal_config_loader()
    runtime_identity = container.runtime_identity
    is_executor = runtime_identity.instance_role == "executor"

    # Phase 1: market data / storage / indicators
    build_market_layer(
        container,
        ingest_settings=ingest_settings,
        market_settings=market_settings,
        include_ingestion=not is_executor,
        include_indicators=not is_executor,
    )

    # Phase 2: trading and economic calendar
    economic_settings = get_economic_config()
    active_account_alias: Optional[str] = build_trading_layer(
        container,
        economic_settings=economic_settings,
        enable_calendar_sync=not is_executor,
    )

    # Phase 3: role-aware runtime assembly
    if is_executor:
        build_account_runtime_layer(
            container,
            signal_config_loader=signal_config_loader,
            signal_config=signal_config,
        )
    else:
        build_signal_layer(
            container,
            signal_config_loader=signal_config_loader,
            signal_config=signal_config,
        )
        build_paper_trading_layer(container)

    # Runtime control plane
    build_runtime_controls(
        container,
        signal_config_loader=signal_config_loader,
    )

    # Phase 4: monitoring
    build_monitoring_layer(
        container,
        ingest_settings=ingest_settings,
        economic_settings=economic_settings,
    )

    # Phase 5: runtime read-models
    build_runtime_read_models(container)

    # Phase 6: frontend observability
    build_studio_service_layer(container)

    # Spread / cost sanity check
    _log_spread_cost_sanity(signal_config)

    logger.info(
        "Effective runtime config: %s",
        json.dumps(
            {
                **get_effective_config_snapshot(),
                "risk": get_risk_config().model_dump(),
                "active_trading_account": active_account_alias,
                "runtime_identity": (
                    container.runtime_identity.__dict__
                    if container.runtime_identity is not None
                    else None
                ),
                "trading_account": (
                    container.trade_module.list_accounts()[0]
                    if container.trade_module
                    else None
                ),
                "indicator_scope": (
                    {
                        "symbols": list(container.indicator_manager.config.symbols),
                        "timeframes": list(container.indicator_manager.config.timeframes),
                        "inherit_symbols": (
                            container.indicator_manager.config.inherit_symbols
                        ),
                        "inherit_timeframes": (
                            container.indicator_manager.config.inherit_timeframes
                        ),
                        "indicator_reload_interval": (
                            container.indicator_manager.config.reload_interval
                        ),
                        "indicator_poll_interval": (
                            container.indicator_manager.config.pipeline.poll_interval
                        ),
                        "indicator_cache_maxsize": (
                            container.indicator_manager.config.pipeline.cache_maxsize
                        ),
                        "indicator_cache_strategy": _enum_or_raw(
                            container.indicator_manager.config.pipeline.cache_strategy
                        ),
                    }
                    if container.indicator_manager is not None
                    else None
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )

    return container
