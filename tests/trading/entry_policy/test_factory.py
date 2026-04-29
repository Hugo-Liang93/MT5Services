"""build_entry_policy_registry factory 单元测试。"""

from __future__ import annotations

import pytest

from src.app_runtime.factories import (
    UnknownEntryPolicyError,
    build_entry_policy_registry,
)
from src.config.models.entry_policy import EntryPolicyConfig


def _make_config(**overrides) -> EntryPolicyConfig:
    base: dict = {
        "enabled_policies": ["market"],
        "default_policy": "market",
        "strategy_mapping": {},
        "strategy_tf_mapping": {},
        "policy_params": {},
        "policy_tf_params": {},
        "fill_semantics_tie_break": "limit_first",
    }
    base.update(overrides)
    return EntryPolicyConfig(**base)


class TestBuildEntryPolicyRegistry:
    def test_p1_minimal_market_only(self):
        registry = build_entry_policy_registry(_make_config())
        assert registry.list_policies() == ["market"]

    def test_unknown_policy_fails_fast(self):
        cfg = _make_config(enabled_policies=["market", "phantom"])
        with pytest.raises(UnknownEntryPolicyError, match="phantom"):
            build_entry_policy_registry(cfg)

    def test_resolve_returns_market_policy(self):
        registry = build_entry_policy_registry(_make_config())
        policy = registry.resolve("any_strategy", "M15")
        assert policy.name == "market"
