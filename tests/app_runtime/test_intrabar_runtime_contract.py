from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app_runtime.builder_phases.signal import _validate_intrabar_trigger_coverage
from src.app_runtime.builder_phases.signal import _wire_required_market_data_lanes
from src.app_runtime.builder_phases.account_runtime import (
    _should_install_intrabar_guard,
)
from src.app_runtime.factories.signals import build_execution_gate_config
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


def test_empty_intrabar_strategy_list_disables_runtime_contract_validation() -> None:
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
        intrabar_trading_trigger_map={},
        intrabar_trading_enabled_strategies=[],
        strategy_timeframes={"structured_range_reversion": ["M30"]},
    )

    _validate_intrabar_trigger_coverage(
        signal_module,
        signal_config,
        effective_timeframes=("M1", "M30", "H1"),
    )


def test_execution_gate_treats_empty_intrabar_strategy_list_as_disabled() -> None:
    gate = build_execution_gate_config(
        SimpleNamespace(
            intrabar_trading_enabled=True,
            intrabar_trading_enabled_strategies=[],
        )
    )

    assert gate.intrabar_trading_enabled is False
    assert gate.intrabar_enabled_strategies == frozenset()


def test_account_runtime_guard_installation_uses_intrabar_active_contract() -> None:
    assert (
        _should_install_intrabar_guard(
            SimpleNamespace(
                intrabar_trading_enabled=True,
                intrabar_trading_enabled_strategies=[],
            )
        )
        is False
    )
    assert (
        _should_install_intrabar_guard(
            SimpleNamespace(
                intrabar_trading_enabled=True,
                intrabar_trading_enabled_strategies=["structured_range_reversion"],
            )
        )
        is True
    )


def test_signal_layer_wires_runtime_market_data_requirements_to_ingestor() -> None:
    calls: list[tuple[str, ...]] = []
    ingestor = SimpleNamespace(
        set_required_market_data_lanes=lambda lanes: calls.append(tuple(lanes))
    )
    signal_runtime = SimpleNamespace(
        required_market_data_lanes=lambda: ("tick:XAUUSD",)
    )

    _wire_required_market_data_lanes(ingestor, signal_runtime)

    assert calls == [("tick:XAUUSD",)]


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
