from __future__ import annotations

import pytest

from src.app_runtime.factories.signals import (
    _should_attach_local_account_runtime,
    _validate_execution_contracts,
    _validate_strategy_deployment_contracts,
    build_executor_config,
    build_signal_filter_chain,
)
from src.config import EconomicConfig
from src.config.models.signal import SignalConfig
from src.config.runtime_identity import RuntimeIdentity, build_account_key
from src.signals.contracts import StrategyDeployment, StrategyDeploymentStatus
from src.trading.execution.executor import ExecutorConfig


def test_build_executor_config_preserves_htf_conflict_contract() -> None:
    signal_config = SignalConfig(
        auto_trade_enabled=True,
        auto_trade_min_confidence=0.82,
        timeframe_min_confidence={"M5": 0.86},
        htf_conflict_block_timeframes=frozenset({"M5", "M15"}),
        htf_conflict_exempt_categories=frozenset({"reversion", "breakout"}),
    )

    config = build_executor_config(signal_config)

    assert isinstance(config, ExecutorConfig)
    assert config.enabled is True
    assert config.min_confidence == 0.82
    assert config.timeframe_min_confidence == {"M5": 0.86}
    assert config.htf_conflict_block_timeframes == frozenset({"M5", "M15"})
    assert config.htf_conflict_exempt_categories == frozenset({"reversion", "breakout"})


def test_build_executor_config_preserves_strategy_deployments() -> None:
    signal_config = SignalConfig(
        auto_trade_enabled=True,
        strategy_deployments={
            "structured_session_breakout": StrategyDeployment(
                name="structured_session_breakout",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
                locked_timeframes=("M30",),
                locked_sessions=("london",),
                min_final_confidence=0.55,
                max_live_positions=1,
                require_pending_entry=True,
                paper_shadow_required=True,
                robustness_tier="tf_specific",
                research_provenance="cand_test",
            )
        },
    )

    config = build_executor_config(signal_config)

    deployment = config.strategy_deployments["structured_session_breakout"]
    assert deployment.status is StrategyDeploymentStatus.ACTIVE_GUARDED
    assert deployment.locked_timeframes == ("M30",)
    assert deployment.locked_sessions == ("london",)
    assert deployment.min_final_confidence == 0.55
    assert deployment.max_live_positions == 1
    assert deployment.require_pending_entry is True
    assert deployment.paper_shadow_required is True


def test_validate_strategy_deployment_contracts_requires_explicit_contracts() -> None:
    signal_config = SignalConfig(
        strategy_deployments={
            "structured_trend_continuation": StrategyDeployment(
                name="structured_trend_continuation",
                status=StrategyDeploymentStatus.DEMO_VALIDATION,
            )
        }
    )

    with pytest.raises(
        ValueError, match="Explicit strategy deployment contracts are required"
    ):
        _validate_strategy_deployment_contracts(
            signal_config,
            {
                "structured_trend_continuation": object(),
                "structured_breakout_follow": object(),
            },
        )


def test_validate_execution_contracts_requires_explicit_binding_for_live_strategies(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.app_runtime.factories.signals.load_group_mt5_settings",
        lambda **kwargs: {
            "live_main": object(),
        },
    )
    runtime_identity = RuntimeIdentity(
        instance_name="live-main",
        environment="live",
        instance_id="main-live-main",
        instance_role="main",
        live_topology_mode="single_account",
        account_alias="live_main",
        account_label="Live Main",
        account_key=build_account_key("live", "Broker-Live", 1001),
        mt5_server="Broker-Live",
        mt5_login=1001,
        mt5_path="C:/MT5/live_main/terminal64.exe",
    )
    signal_config = SignalConfig(
        auto_trade_enabled=True,
        strategy_deployments={
            "structured_breakout_follow": StrategyDeployment(
                name="structured_breakout_follow",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
            )
        },
        account_bindings={},
    )

    with pytest.raises(ValueError, match="explicit account_bindings"):
        _validate_execution_contracts(
            signal_config=signal_config,
            deployments=signal_config.strategy_deployments,
            runtime_identity=runtime_identity,
        )


def test_multi_account_main_skips_local_account_runtime_without_explicit_live_binding() -> (
    None
):
    runtime_identity = RuntimeIdentity(
        instance_name="live-main",
        environment="live",
        instance_id="main-live-main",
        instance_role="main",
        live_topology_mode="multi_account",
        account_alias="live_main",
        account_label="Live Main",
        account_key=build_account_key("live", "Broker-Live", 1001),
        mt5_server="Broker-Live",
        mt5_login=1001,
        mt5_path="C:/MT5/live_main/terminal64.exe",
    )
    deployments = {
        "structured_breakout_follow": StrategyDeployment(
            name="structured_breakout_follow",
            status=StrategyDeploymentStatus.ACTIVE_GUARDED,
        )
    }
    signal_config = SignalConfig(
        account_bindings={"live_exec_a": ["structured_breakout_follow"]},
        strategy_deployments=deployments,
    )

    assert (
        _should_attach_local_account_runtime(
            signal_config=signal_config,
            deployments=deployments,
            runtime_identity=runtime_identity,
        )
        is False
    )


def test_multi_account_main_keeps_local_account_runtime_when_explicitly_bound() -> None:
    runtime_identity = RuntimeIdentity(
        instance_name="live-main",
        environment="live",
        instance_id="main-live-main",
        instance_role="main",
        live_topology_mode="multi_account",
        account_alias="live_main",
        account_label="Live Main",
        account_key=build_account_key("live", "Broker-Live", 1001),
        mt5_server="Broker-Live",
        mt5_login=1001,
        mt5_path="C:/MT5/live_main/terminal64.exe",
    )
    deployments = {
        "structured_breakout_follow": StrategyDeployment(
            name="structured_breakout_follow",
            status=StrategyDeploymentStatus.ACTIVE_GUARDED,
        )
    }
    signal_config = SignalConfig(
        account_bindings={"live_main": ["structured_breakout_follow"]},
        strategy_deployments=deployments,
    )

    assert (
        _should_attach_local_account_runtime(
            signal_config=signal_config,
            deployments=deployments,
            runtime_identity=runtime_identity,
        )
        is True
    )


def test_build_signal_filter_chain_uses_economic_config_as_ssot() -> None:
    filter_chain = build_signal_filter_chain(
        SignalConfig(),
        economic_calendar_service=object(),
        economic_config=EconomicConfig(
            enabled=True,
            pre_event_buffer_minutes=45,
            post_event_buffer_minutes=20,
            high_importance_threshold=3,
            release_watch_approaching_minutes=90,
            release_watch_post_event_minutes=10,
        ),
    )

    assert filter_chain.economic_filter is not None
    assert filter_chain.economic_filter.service is not None
    assert (
        filter_chain.economic_filter.service.policy.filter_window.lookahead_minutes
        == 45
    )
    assert (
        filter_chain.economic_filter.service.policy.filter_window.lookback_minutes == 20
    )
    assert (
        filter_chain.economic_filter.service.policy.query_window.lookahead_minutes == 90
    )
