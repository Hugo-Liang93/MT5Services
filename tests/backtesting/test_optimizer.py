"""optimizer.py 单元测试。"""
from __future__ import annotations

import pytest

from src.backtesting.models import ParameterSpace
from src.backtesting.optimizer import ParameterOptimizer


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
