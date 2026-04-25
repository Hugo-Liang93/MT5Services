"""Phase 1 验证：装配层 environment-aware 策略过滤逻辑。

参考：docs/superpowers/specs/2026-04-25-demo-only-validation-design.md（C:\\Users\\Hugo\\.claude\\plans\\demo-wondrous-nygaard.md）

装配规则（不变量）：
  - environment="live"    → ACTIVE + ACTIVE_GUARDED
  - environment="demo"    → ACTIVE + ACTIVE_GUARDED + DEMO_VALIDATION
  - environment 未知      → 全量保留 + WARNING
"""

from __future__ import annotations

from dataclasses import dataclass

from src.app_runtime.factories.signals import _filter_strategies_for_environment
from src.signals.contracts import StrategyDeployment, StrategyDeploymentStatus


@dataclass
class _StubStrategy:
    """最小 strategy 替身：仅承载 .name 字段供过滤函数使用。"""

    name: str


def _make_deployment(name: str, status: StrategyDeploymentStatus) -> StrategyDeployment:
    return StrategyDeployment(name=name, status=status)


def _build_full_set() -> tuple[list[_StubStrategy], dict[str, StrategyDeployment]]:
    """构建 4 档状态各 1 条的策略集合，覆盖全部分支。"""
    strategies = [
        _StubStrategy("strategy_active"),
        _StubStrategy("strategy_active_guarded"),
        _StubStrategy("strategy_demo_validation"),
        _StubStrategy("strategy_candidate"),
    ]
    deployments = {
        "strategy_active": _make_deployment(
            "strategy_active", StrategyDeploymentStatus.ACTIVE
        ),
        "strategy_active_guarded": _make_deployment(
            "strategy_active_guarded", StrategyDeploymentStatus.ACTIVE_GUARDED
        ),
        "strategy_demo_validation": _make_deployment(
            "strategy_demo_validation", StrategyDeploymentStatus.DEMO_VALIDATION
        ),
        "strategy_candidate": _make_deployment(
            "strategy_candidate", StrategyDeploymentStatus.CANDIDATE
        ),
    }
    return strategies, deployments


def test_filter_for_live_environment_keeps_active_and_active_guarded() -> None:
    strategies, deployments = _build_full_set()

    filtered = _filter_strategies_for_environment(strategies, deployments, "live")

    assert {s.name for s in filtered} == {
        "strategy_active",
        "strategy_active_guarded",
    }


def test_filter_for_demo_environment_includes_demo_validation() -> None:
    strategies, deployments = _build_full_set()

    filtered = _filter_strategies_for_environment(strategies, deployments, "demo")

    assert {s.name for s in filtered} == {
        "strategy_active",
        "strategy_active_guarded",
        "strategy_demo_validation",
    }


def test_filter_for_unknown_environment_keeps_all_strategies(caplog) -> None:
    """environment=None / 其他值 → 向后兼容全量保留 + WARNING。"""
    strategies, deployments = _build_full_set()

    with caplog.at_level("WARNING", logger="src.app_runtime.factories.signals"):
        filtered = _filter_strategies_for_environment(strategies, deployments, None)

    assert {s.name for s in filtered} == {s.name for s in strategies}
    assert any(
        "environment=None" in record.getMessage()
        or "未知" in record.getMessage()
        for record in caplog.records
    )


def test_filter_keeps_strategies_without_deployment_contract() -> None:
    """没有 deployment 契约的策略保留——交由下游 _validate_strategy_deployment_contracts 报错。"""
    strategies = [
        _StubStrategy("strategy_active"),
        _StubStrategy("strategy_no_contract"),
    ]
    deployments = {
        "strategy_active": _make_deployment(
            "strategy_active", StrategyDeploymentStatus.ACTIVE
        ),
    }

    filtered = _filter_strategies_for_environment(strategies, deployments, "live")

    assert {s.name for s in filtered} == {
        "strategy_active",
        "strategy_no_contract",
    }


def test_filter_excludes_candidate_in_both_live_and_demo() -> None:
    """CANDIDATE 状态在两个 environment 都被排除。"""
    strategies, deployments = _build_full_set()

    live_filtered = _filter_strategies_for_environment(strategies, deployments, "live")
    demo_filtered = _filter_strategies_for_environment(strategies, deployments, "demo")

    assert "strategy_candidate" not in {s.name for s in live_filtered}
    assert "strategy_candidate" not in {s.name for s in demo_filtered}


def test_demo_set_strict_superset_of_live_set() -> None:
    """不变量：demo 装配集合 ⊇ live 装配集合（demo = live + DEMO_VALIDATION 候选）。"""
    strategies, deployments = _build_full_set()

    live_set = {s.name for s in _filter_strategies_for_environment(
        strategies, deployments, "live"
    )}
    demo_set = {s.name for s in _filter_strategies_for_environment(
        strategies, deployments, "demo"
    )}

    assert live_set <= demo_set
    # demo - live 应该恰好是 DEMO_VALIDATION 候选
    assert demo_set - live_set == {"strategy_demo_validation"}


def test_strategy_deployment_helper_allows_demo_validation() -> None:
    """deployment.allows_demo_validation() helper 契约验证。"""
    assert _make_deployment("a", StrategyDeploymentStatus.ACTIVE).allows_demo_validation()
    assert _make_deployment(
        "a", StrategyDeploymentStatus.ACTIVE_GUARDED
    ).allows_demo_validation()
    assert _make_deployment(
        "a", StrategyDeploymentStatus.DEMO_VALIDATION
    ).allows_demo_validation()
    assert not _make_deployment(
        "a", StrategyDeploymentStatus.CANDIDATE
    ).allows_demo_validation()
