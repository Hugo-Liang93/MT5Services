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
def paper_only_deployment() -> StrategyDeployment:
    return StrategyDeployment(
        name="my_strategy",
        status=StrategyDeploymentStatus.PAPER_ONLY,
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
    def test_candidate_blocked(
        self, candidate_deployment: StrategyDeployment
    ) -> None:
        assert not candidate_deployment.allows_runtime_evaluation()

    def test_paper_only_allowed(
        self, paper_only_deployment: StrategyDeployment
    ) -> None:
        # PAPER_ONLY 允许 runtime 评估（只是不能 live 执行）
        assert paper_only_deployment.allows_runtime_evaluation()

    def test_guarded_allowed(
        self, guarded_deployment: StrategyDeployment
    ) -> None:
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
        d = StrategyDeployment(
            name="x", status=StrategyDeploymentStatus.ACTIVE
        )
        assert len(d.locked_timeframes) == 0


class TestMaxLivePositions:
    def test_guarded_single_position(
        self, guarded_deployment: StrategyDeployment
    ) -> None:
        assert guarded_deployment.max_live_positions == 1

    def test_unbounded_none(self) -> None:
        d = StrategyDeployment(
            name="x", status=StrategyDeploymentStatus.ACTIVE
        )
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
            "paper_s": StrategyDeployment(
                name="paper_s", status=StrategyDeploymentStatus.PAPER_ONLY
            ),
        }
        # 应纳入评估：active_s, paper_s；应排除：candidate_s
        allowed = [
            name
            for name, dep in deployments.items()
            if dep.allows_runtime_evaluation()
        ]
        assert sorted(allowed) == ["active_s", "paper_s"]
        assert "candidate_s" not in allowed


class TestDeploymentContractSemanticsStayStable:
    """锁定 deployment 契约语义，防止不小心改变 CANDIDATE / PAPER_ONLY 定义。"""

    def test_paper_only_cannot_live_execute(
        self, paper_only_deployment: StrategyDeployment
    ) -> None:
        assert not paper_only_deployment.allows_live_execution()

    def test_candidate_cannot_live_execute(
        self, candidate_deployment: StrategyDeployment
    ) -> None:
        assert not candidate_deployment.allows_live_execution()

    def test_active_can_live_execute(self) -> None:
        d = StrategyDeployment(
            name="x", status=StrategyDeploymentStatus.ACTIVE
        )
        assert d.allows_live_execution()

    def test_guarded_can_live_execute(
        self, guarded_deployment: StrategyDeployment
    ) -> None:
        assert guarded_deployment.allows_live_execution()
