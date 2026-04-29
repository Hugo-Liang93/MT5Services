"""验证 EntryPolicyRegistry 能被装配到 AppContainer 并解析正常。

P1 阶段：仅验证 (registry 装配成功) + (字段绑定到 container) + (resolve fallback to market)。
P2/P3 之后再加：与 PendingEntryManager 协作的端到端集成。
"""

from __future__ import annotations

from src.app_runtime.container import AppContainer
from src.app_runtime.factories import build_entry_policy_registry
from src.config import get_entry_policy_config, reset_entry_policy_config_cache
from src.trading.entry_policy import EntryPolicyRegistry


class TestEntryPolicyPhase:
    def test_container_field_present(self):
        container = AppContainer()
        assert hasattr(container, "entry_policy_registry")
        assert container.entry_policy_registry is None

    def test_registry_assignable_to_container(self):
        container = AppContainer()
        reset_entry_policy_config_cache()
        registry = build_entry_policy_registry(get_entry_policy_config())
        container.entry_policy_registry = registry
        assert isinstance(container.entry_policy_registry, EntryPolicyRegistry)
        # P3 起注册全部 5 policy（mapping 推荐已下发）
        assert set(container.entry_policy_registry.list_policies()) == {
            "market",
            "pullback",
            "breakout",
            "oco_pullback_breakout",
            "fib_pullback",
        }

    def test_default_resolves_to_market(self):
        reset_entry_policy_config_cache()
        registry = build_entry_policy_registry(get_entry_policy_config())
        policy = registry.resolve("structured_unknown_strategy", "M15")
        assert policy.name == "market"

    def test_describe_includes_registered_policies(self):
        reset_entry_policy_config_cache()
        registry = build_entry_policy_registry(get_entry_policy_config())
        info = registry.describe()
        assert info["default_policy"] == "market"
        assert any(p["name"] == "market" for p in info["registered_policies"])
