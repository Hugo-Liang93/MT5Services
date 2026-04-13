from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app_runtime.builder_phases.signal import _validate_intrabar_trigger_coverage
from src.signals.contracts.capability import StrategyCapability


def _signal_module(*capabilities: StrategyCapability) -> SimpleNamespace:
    return SimpleNamespace(
        strategy_capability_catalog=lambda: list(capabilities),
    )


def test_validate_intrabar_runtime_contract_rejects_missing_child_timeframe() -> None:
    signal_module = _signal_module(
        StrategyCapability(
            name="structured_range_reversion",
            valid_scopes=("confirmed", "intrabar"),
            needed_indicators=("rsi14", "atr14"),
            needs_intrabar=True,
            needs_htf=True,
            regime_affinity={},
            htf_requirements={"supertrend14": "H1"},
        )
    )
    signal_config = SimpleNamespace(
        intrabar_trading_enabled=True,
        intrabar_trading_trigger_map={"M30": "M1"},
        intrabar_trading_enabled_strategies=["structured_range_reversion"],
        strategy_timeframes={"structured_range_reversion": ["M30"]},
    )

    with pytest.raises(ValueError, match="missing from app.ini\\[trading\\]\\.timeframes"):
        _validate_intrabar_trigger_coverage(
            signal_module,
            signal_config,
            effective_timeframes=("M5", "M15", "M30", "H1", "H4", "D1"),
        )


def test_validate_intrabar_runtime_contract_accepts_closed_trigger_loop() -> None:
    signal_module = _signal_module(
        StrategyCapability(
            name="structured_range_reversion",
            valid_scopes=("confirmed", "intrabar"),
            needed_indicators=("rsi14", "atr14"),
            needs_intrabar=True,
            needs_htf=True,
            regime_affinity={},
            htf_requirements={"supertrend14": "H1"},
        )
    )
    signal_config = SimpleNamespace(
        intrabar_trading_enabled=True,
        intrabar_trading_trigger_map={"M30": "M1"},
        intrabar_trading_enabled_strategies=["structured_range_reversion"],
        strategy_timeframes={"structured_range_reversion": ["M30"]},
    )

    _validate_intrabar_trigger_coverage(
        signal_module,
        signal_config,
        effective_timeframes=("M1", "M5", "M15", "M30", "H1", "H4", "D1"),
    )


def test_validate_intrabar_runtime_contract_rejects_non_intrabar_enabled_strategy() -> None:
    signal_module = _signal_module(
        StrategyCapability(
            name="structured_breakout_follow",
            valid_scopes=("confirmed",),
            needed_indicators=("adx14", "atr14"),
            needs_intrabar=False,
            needs_htf=True,
            regime_affinity={},
            htf_requirements={"supertrend14": "H1"},
        )
    )
    signal_config = SimpleNamespace(
        intrabar_trading_enabled=True,
        intrabar_trading_trigger_map={"M30": "M1"},
        intrabar_trading_enabled_strategies=["structured_breakout_follow"],
        strategy_timeframes={"structured_breakout_follow": ["M30"]},
    )

    with pytest.raises(ValueError, match="do not declare intrabar capability"):
        _validate_intrabar_trigger_coverage(
            signal_module,
            signal_config,
            effective_timeframes=("M1", "M30", "H1"),
        )
