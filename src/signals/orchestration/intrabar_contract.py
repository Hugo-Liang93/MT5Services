from __future__ import annotations

from typing import Any


def intrabar_enabled_strategies(signal_config: Any) -> frozenset[str]:
    return frozenset(
        str(strategy_name).strip()
        for strategy_name in (
            getattr(signal_config, "intrabar_trading_enabled_strategies", ()) or ()
        )
        if str(strategy_name).strip()
    )


def intrabar_trading_active(signal_config: Any) -> bool:
    """True only when intrabar trading is enabled and strategies are explicit."""

    return bool(getattr(signal_config, "intrabar_trading_enabled", False)) and bool(
        intrabar_enabled_strategies(signal_config)
    )
