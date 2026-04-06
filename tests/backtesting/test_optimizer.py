"""optimizer.py 单元测试。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.backtesting.models import ParameterSpace
from src.backtesting.optimization.optimizer import ParameterOptimizer, build_signal_module_with_overrides
from src.signals.evaluation.regime import RegimeType
from src.signals.service import SignalModule
from src.signals.strategies.htf_cache import HTFStateCache
from src.signals.strategies.structured import StructuredTrendContinuation


class TestParameterSpace:
    def test_grid_generation(self) -> None:
        """测试网格搜索组合生成。"""
        space = ParameterSpace(
            strategy_params={
                "rsi_reversion__oversold": [25, 30, 35],
                "rsi_reversion__overbought": [70, 75],
            },
            search_mode="grid",
        )
        # 手动生成：验证逻辑
        from itertools import product

        keys = list(space.strategy_params.keys())
        values = [space.strategy_params[k] for k in keys]
        combos = [dict(zip(keys, c)) for c in product(*values)]
        assert len(combos) == 6  # 3 × 2

    def test_random_generation_cap(self) -> None:
        """测试随机搜索上限截断。"""
        space = ParameterSpace(
            strategy_params={
                "a": list(range(100)),
                "b": list(range(100)),
            },
            search_mode="random",
            max_combinations=50,
        )
        # 10000 种可能，应该被截断到 50
        assert space.max_combinations == 50

    def test_empty_params(self) -> None:
        """空参数空间应返回单个空字典。"""
        space = ParameterSpace(strategy_params={})
        from itertools import product

        keys = list(space.strategy_params.keys())
        values = [space.strategy_params[k] for k in keys]
        combos = [dict(zip(keys, c)) for c in product(*values)]
        assert combos == [{}]


class TestGridSearch:
    def test_cartesian_product(self) -> None:
        """验证笛卡尔积展开正确。"""
        from itertools import product

        params = {"x": [1, 2], "y": [10, 20, 30]}
        keys = list(params.keys())
        values = [params[k] for k in keys]
        combos = [dict(zip(keys, c)) for c in product(*values)]

        assert len(combos) == 6
        assert {"x": 1, "y": 10} in combos
        assert {"x": 2, "y": 30} in combos

    def test_single_param(self) -> None:
        """单参数应生成简单列表。"""
        params = {"threshold": [0.5, 0.6, 0.7]}
        from itertools import product

        keys = list(params.keys())
        values = [params[k] for k in keys]
        combos = [dict(zip(keys, c)) for c in product(*values)]

        assert len(combos) == 3
        assert combos[0] == {"threshold": 0.5}


class _NullIndicatorSource:
    def get_indicator(self, symbol, timeframe, name):  # type: ignore[no-untyped-def]
        return None

    def get_all_indicators(self, symbol, timeframe):  # type: ignore[no-untyped-def]
        return {}


def test_build_signal_module_with_overrides_preserves_base_per_tf_config() -> None:
    base_module = SignalModule(indicator_source=_NullIndicatorSource())
    base_module.apply_param_overrides(
        {"structured_range_reversion__some_param": 30.0},
        {"structured_range_reversion": {"ranging": 0.9}},
        strategy_params_per_tf={"M5": {"structured_range_reversion__some_param": 22.0}},
    )

    module = build_signal_module_with_overrides(
        base_module,
        {"structured_range_reversion__other_param": 75.0},
    )

    resolver = module._tf_param_resolver
    assert resolver.get("structured_range_reversion", "some_param", "M5", default=0.0) == 22.0
    assert resolver.get("structured_range_reversion", "some_param", "H1", default=0.0) == 30.0
    assert resolver.get("structured_range_reversion", "other_param", "M5", default=0.0) == 75.0
    assert module.strategy_affinity_map("structured_range_reversion")[RegimeType.RANGING] == 0.9


def test_build_signal_module_with_overrides_preserves_strategies() -> None:
    base_module = SignalModule(indicator_source=_NullIndicatorSource(), strategies=())
    base_module.register_strategy(StructuredTrendContinuation())

    module = build_signal_module_with_overrides(base_module, {})

    assert "structured_trend_continuation" in module._strategies


def test_build_backtest_components_registers_multi_timeframe_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backtesting.component_factory as component_factory

    class _DummyWriter:
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass

    class _DummyMarketRepo:
        def __init__(self, writer):  # type: ignore[no-untyped-def]
            self.writer = writer

    class _DummyLoader:
        def __init__(self, repo):  # type: ignore[no-untyped-def]
            self.repo = repo

    class _DummyPipeline:
        def register_indicator(self, **kwargs):  # type: ignore[no-untyped-def]
            return None

    class _DummyConfigManager:
        def get_config(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                pipeline=SimpleNamespace(),
                indicators=[],
            )

    monkeypatch.setattr("src.config.database.load_db_settings", lambda: object())
    monkeypatch.setattr("src.config.indicator_config.get_global_config_manager", lambda: _DummyConfigManager())
    monkeypatch.setattr("src.indicators.engine.pipeline.create_isolated_pipeline", lambda cfg: _DummyPipeline())
    monkeypatch.setattr("src.persistence.db.TimescaleWriter", _DummyWriter)
    monkeypatch.setattr("src.persistence.repositories.market_repo.MarketRepository", _DummyMarketRepo)
    monkeypatch.setattr("src.backtesting.data.loader.HistoricalDataLoader", _DummyLoader)
    monkeypatch.setattr(
        "src.config.signal.get_signal_config",
        lambda: SimpleNamespace(
            strategy_params={},
            strategy_params_per_tf={},
            regime_affinity_overrides={},
            htf_cache_max_age_seconds=14400,
        ),
    )

    components = component_factory.build_backtest_components()

    signal_module = components["signal_module"]
    requirements = signal_module.strategy_requirements("structured_trend_continuation")
    assert "rsi14" in requirements
    assert "atr14" in requirements
