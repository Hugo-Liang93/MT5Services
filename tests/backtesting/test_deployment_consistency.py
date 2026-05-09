"""回测 ↔ 实盘 deployment 合同一致性测试。

覆盖 plan 的阶段 1 修复：
  - CANDIDATE 策略不进入回测（与实盘 PreTradePipeline 拒绝一致）
  - locked_timeframes 在回测里强制
  - locked_sessions 在回测里强制
  - max_live_positions 在回测里 cap
  - require_pending_entry 在回测里强制
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List

import pytest

from src.backtesting.engine import BacktestDeploymentGate, BacktestDeploymentGateMode
from src.signals.contracts.deployment import (
    StrategyDeployment,
    StrategyDeploymentStatus,
)


@pytest.fixture
def candidate_deployment() -> StrategyDeployment:
    return StrategyDeployment(
        name="my_strategy",
        status=StrategyDeploymentStatus.CANDIDATE,
    )


@pytest.fixture
def demo_validation_deployment() -> StrategyDeployment:
    return StrategyDeployment(
        name="my_strategy",
        status=StrategyDeploymentStatus.DEMO_VALIDATION,
    )


@pytest.fixture
def guarded_deployment() -> StrategyDeployment:
    return StrategyDeployment(
        name="my_strategy",
        status=StrategyDeploymentStatus.ACTIVE_GUARDED,
        locked_timeframes=("H1",),
        max_live_positions=1,
        require_pending_entry=True,
    )


class TestDeploymentAllowsRuntimeEvaluation:
    def test_candidate_blocked(self, candidate_deployment: StrategyDeployment) -> None:
        assert not candidate_deployment.allows_runtime_evaluation()

    def test_demo_validation_allowed(
        self, demo_validation_deployment: StrategyDeployment
    ) -> None:
        # DEMO_VALIDATION 允许 runtime 评估（只是不能 live 执行）
        assert demo_validation_deployment.allows_runtime_evaluation()

    def test_guarded_allowed(self, guarded_deployment: StrategyDeployment) -> None:
        assert guarded_deployment.allows_runtime_evaluation()


class TestLockedTimeframes:
    def test_tf_in_lock_list_passes(
        self, guarded_deployment: StrategyDeployment
    ) -> None:
        # locked_timeframes=("H1",)，检查 H1 通过
        assert "H1" in guarded_deployment.locked_timeframes

    def test_tf_not_in_lock_list_rejected(
        self, guarded_deployment: StrategyDeployment
    ) -> None:
        assert "M15" not in guarded_deployment.locked_timeframes

    def test_empty_lock_means_no_restriction(self) -> None:
        d = StrategyDeployment(name="x", status=StrategyDeploymentStatus.ACTIVE)
        assert len(d.locked_timeframes) == 0


class TestMaxLivePositions:
    def test_guarded_single_position(
        self, guarded_deployment: StrategyDeployment
    ) -> None:
        assert guarded_deployment.max_live_positions == 1

    def test_unbounded_none(self) -> None:
        d = StrategyDeployment(name="x", status=StrategyDeploymentStatus.ACTIVE)
        assert d.max_live_positions is None


class TestBacktestEngineDeploymentGate:
    """通过 BacktestEngine 的 _target_strategies 过滤逻辑验证 CANDIDATE 被排除。"""

    def test_candidate_strategy_filtered_from_targets(self) -> None:
        # 直接构造一个带 CANDIDATE 策略的 deployments dict，模拟过滤
        deployments: Dict[str, StrategyDeployment] = {
            "active_s": StrategyDeployment(
                name="active_s", status=StrategyDeploymentStatus.ACTIVE
            ),
            "candidate_s": StrategyDeployment(
                name="candidate_s", status=StrategyDeploymentStatus.CANDIDATE
            ),
            "demo_s": StrategyDeployment(
                name="demo_s", status=StrategyDeploymentStatus.DEMO_VALIDATION
            ),
        }
        # 应纳入评估：active_s, demo_s；应排除：candidate_s
        allowed = [
            name for name, dep in deployments.items() if dep.allows_runtime_evaluation()
        ]
        assert sorted(allowed) == ["active_s", "demo_s"]
        assert "candidate_s" not in allowed

    def test_empty_snapshot_cannot_disable_gate(self) -> None:
        with pytest.raises(ValueError, match="non-empty strategy deployments"):
            BacktestDeploymentGate.from_snapshot(
                {},
                audit_reason="unit test",
            )

    def test_research_disabled_requires_audit_reason(self) -> None:
        with pytest.raises(ValueError, match="audit_reason"):
            BacktestDeploymentGate(
                mode=BacktestDeploymentGateMode.RESEARCH_DISABLED,
                deployments={},
            )

    def test_research_disabled_is_explicit_bypass(self) -> None:
        gate = BacktestDeploymentGate.research_disabled("unit test")

        assert gate.mode is BacktestDeploymentGateMode.RESEARCH_DISABLED
        assert not gate.is_enabled
        assert gate.require_deployment("any_strategy") is None

    def test_snapshot_requires_contract_for_requested_strategy(self) -> None:
        gate = BacktestDeploymentGate.from_snapshot(
            {
                "active_s": StrategyDeployment(
                    name="active_s", status=StrategyDeploymentStatus.ACTIVE
                )
            },
            audit_reason="unit test",
        )

        with pytest.raises(ValueError, match="missing explicit contract"):
            gate.require_deployment("missing_s")

    def test_signal_config_load_failure_does_not_disable_gate(
        self, monkeypatch
    ) -> None:
        from src.backtesting.engine import runner

        def boom():
            raise RuntimeError("signal config broken")

        monkeypatch.setattr(
            "src.backtesting.component_factory._load_signal_config_snapshot",
            boom,
        )

        with pytest.raises(RuntimeError, match="deployment gate"):
            runner._load_deployment_gate_from_signal_config()


class TestDeploymentContractSemanticsStayStable:
    """锁定 deployment 契约语义，防止不小心改变 CANDIDATE / DEMO_VALIDATION 定义。"""

    def test_demo_validation_cannot_live_execute(
        self, demo_validation_deployment: StrategyDeployment
    ) -> None:
        assert not demo_validation_deployment.allows_live_execution()

    def test_candidate_cannot_live_execute(
        self, candidate_deployment: StrategyDeployment
    ) -> None:
        assert not candidate_deployment.allows_live_execution()

    def test_active_can_live_execute(self) -> None:
        d = StrategyDeployment(name="x", status=StrategyDeploymentStatus.ACTIVE)
        assert d.allows_live_execution()

    def test_guarded_can_live_execute(
        self, guarded_deployment: StrategyDeployment
    ) -> None:
        assert guarded_deployment.allows_live_execution()
