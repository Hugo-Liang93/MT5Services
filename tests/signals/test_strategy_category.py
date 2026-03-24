"""StrategyCategory 枚举校验测试。"""

from __future__ import annotations

import pytest

from src.signals.service import SignalModule
from src.signals.strategies.base import StrategyCategory
from src.signals.evaluation.regime import RegimeType


def _make_strategy(name, category):
    """创建一个带指定 category 的最小策略 stub。"""
    from src.signals.models import SignalDecision

    class Stub:
        pass

    s = Stub()
    s.name = name
    s.required_indicators = ("rsi14",)
    s.preferred_scopes = ("confirmed",)
    s.regime_affinity = {
        RegimeType.TRENDING: 0.5,
        RegimeType.RANGING: 0.5,
        RegimeType.BREAKOUT: 0.5,
        RegimeType.UNCERTAIN: 0.5,
    }
    s.category = category
    s.evaluate = lambda ctx: SignalDecision(
        strategy=name, symbol="X", timeframe="M5",
        direction="hold", confidence=0.0, reason="stub",
    )
    return s


class _DummyIndicatorSource:
    def get_indicator(self, symbol, tf, name):
        return None
    def get_all_indicators(self, symbol, tf):
        return {}
    def list_indicators(self):
        return []


class TestCategoryValidation:

    def test_valid_string_category_accepted(self):
        module = SignalModule(indicator_source=_DummyIndicatorSource())
        module.register_strategy(_make_strategy("ok_str", "trend"))

    def test_valid_enum_category_accepted(self):
        module = SignalModule(indicator_source=_DummyIndicatorSource())
        module.register_strategy(_make_strategy("ok_enum", StrategyCategory.BREAKOUT))

    def test_invalid_category_rejected(self):
        module = SignalModule(indicator_source=_DummyIndicatorSource())
        with pytest.raises(AttributeError, match="invalid category"):
            module.register_strategy(_make_strategy("bad", "nonexistent_category"))
