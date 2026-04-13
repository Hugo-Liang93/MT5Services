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
    if signal_config.base_spread_points > 0:
        min_plausible_sl = signal_config.base_spread_points * 3
        implied_ratio = signal_config.base_spread_points / min_plausible_sl
        if implied_ratio > signal_config.max_spread_to_stop_ratio * 0.8:
            logger.warning(
                "Spread/cost config may be too tight: base_spread=%.0f, "
                "max_spread_to_stop_ratio=%.2f. Low-ATR timeframes "
                "might reject most trades. Consider raising the ratio.",
                signal_config.base_spread_points,
                signal_config.max_spread_to_stop_ratio,
            )

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
