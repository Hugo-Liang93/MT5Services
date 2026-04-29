"""EntryPolicyRegistry 单元测试。"""

from __future__ import annotations

import pytest

from src.config.models.entry_policy import EntryPolicyConfig
from src.trading.entry_policy import (
    EntryPolicyNotFoundError,
    EntryPolicyRegistry,
    MarketEntryPolicy,
)


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


def _make_registry(**overrides) -> EntryPolicyRegistry:
    cfg = _make_config(**overrides)
    return EntryPolicyRegistry(policies={"market": MarketEntryPolicy()}, config=cfg)


class TestResolution:
    def test_default_fallback(self):
        reg = _make_registry()
        policy = reg.resolve("structured_unknown", "M15")
        assert policy.name == "market"

    def test_per_strategy_override(self):
        # 临时再注册一个 fake policy
        class FakePolicy:
            name = "fake"

            def derive(self, intent, market, params):  # noqa: ARG002
                raise NotImplementedError

            def describe(self):
                return {"name": "fake"}

        cfg = _make_config(
            enabled_policies=["market", "fake"],
            strategy_mapping={"strat_a": "fake"},
        )
        reg = EntryPolicyRegistry(
            policies={"market": MarketEntryPolicy(), "fake": FakePolicy()},
            config=cfg,
        )
        assert reg.resolve("strat_a", "M15").name == "fake"
        assert reg.resolve("strat_b", "M15").name == "market"

    def test_per_tf_override_wins_over_strategy(self):
        class FakePolicy:
            name = "fake"

            def derive(self, intent, market, params):  # noqa: ARG002
                raise NotImplementedError

            def describe(self):
                return {"name": "fake"}

        cfg = _make_config(
            enabled_policies=["market", "fake"],
            strategy_mapping={"strat_a": "market"},
            strategy_tf_mapping={("strat_a", "H1"): "fake"},
        )
        reg = EntryPolicyRegistry(
            policies={"market": MarketEntryPolicy(), "fake": FakePolicy()},
            config=cfg,
        )
        assert reg.resolve("strat_a", "M15").name == "market"
        assert reg.resolve("strat_a", "H1").name == "fake"


class TestParamsResolution:
    def test_returns_empty_when_no_section(self):
        reg = _make_registry()
        assert reg.resolve_params("market", "M15") == {}

    def test_base_params_returned(self):
        reg = _make_registry(
            policy_params={"market": {"foo": 1.5, "bar": "baz"}},
        )
        params = reg.resolve_params("market", "M15")
        assert params == {"foo": 1.5, "bar": "baz"}

    def test_per_tf_overrides_base(self):
        reg = _make_registry(
            policy_params={"market": {"foo": 1.0, "bar": 2.0}},
            policy_tf_params={("market", "M15"): {"bar": 99.0}},
        )
        params = reg.resolve_params("market", "M15")
        assert params == {"foo": 1.0, "bar": 99.0}

        params_other_tf = reg.resolve_params("market", "H1")
        assert params_other_tf == {"foo": 1.0, "bar": 2.0}


class TestInvariants:
    def test_default_must_be_registered(self):
        # default_policy refers to a policy not in policies dict
        cfg = EntryPolicyConfig(
            enabled_policies=["market", "ghost"],
            default_policy="ghost",
            strategy_mapping={},
            strategy_tf_mapping={},
            policy_params={},
            policy_tf_params={},
            fill_semantics_tie_break="limit_first",
        )
        with pytest.raises(EntryPolicyNotFoundError):
            EntryPolicyRegistry(policies={"market": MarketEntryPolicy()}, config=cfg)

    def test_mapping_must_reference_registered(self):
        cfg = EntryPolicyConfig(
            enabled_policies=["market", "ghost"],
            default_policy="market",
            strategy_mapping={"x": "ghost"},
            strategy_tf_mapping={},
            policy_params={},
            policy_tf_params={},
            fill_semantics_tie_break="limit_first",
        )
        with pytest.raises(EntryPolicyNotFoundError):
            EntryPolicyRegistry(policies={"market": MarketEntryPolicy()}, config=cfg)

    def test_pydantic_rejects_default_outside_enabled(self):
        with pytest.raises(ValueError, match="default_policy"):
            EntryPolicyConfig(
                enabled_policies=["market"],
                default_policy="ghost",
                strategy_mapping={},
                strategy_tf_mapping={},
                policy_params={},
                policy_tf_params={},
                fill_semantics_tie_break="limit_first",
            )

    def test_pydantic_rejects_unknown_tf(self):
        with pytest.raises(ValueError, match="unknown timeframe"):
            EntryPolicyConfig(
                enabled_policies=["market"],
                default_policy="market",
                strategy_mapping={},
                strategy_tf_mapping={("x", "BAD"): "market"},
                policy_params={},
                policy_tf_params={},
                fill_semantics_tie_break="limit_first",
            )


class TestDescribe:
    def test_describe_returns_full_state(self):
        reg = _make_registry(strategy_mapping={"strat_a": "market"})
        d = reg.describe()
        assert d["default_policy"] == "market"
        assert d["fill_semantics_tie_break"] == "limit_first"
        assert any(p["name"] == "market" for p in d["registered_policies"])
        assert d["strategy_mapping"] == {"strat_a": "market"}

    def test_list_policies(self):
        reg = _make_registry()
        assert reg.list_policies() == ["market"]
