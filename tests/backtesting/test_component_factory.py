"""component_factory.py 单元级测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


class TestBuildBacktestComponents:
    """测试 build_backtest_components 的组件构建逻辑。"""

    @patch("src.backtesting.component_factory._load_signal_config_snapshot")
    @patch("src.backtesting.data.loader.HistoricalDataLoader")
    @patch("src.persistence.db.TimescaleWriter")
    @patch("src.persistence.repositories.market_repo.MarketRepository")
    def test_returns_required_keys(
        self,
        mock_market_repo: MagicMock,
        mock_writer: MagicMock,
        mock_loader: MagicMock,
        mock_signal_config: MagicMock,
    ) -> None:
        """build_backtest_components 应返回 signal_module, pipeline 等核心组件。"""
        mock_signal_config.return_value = SimpleNamespace(
            strategy_params={},
            strategy_params_per_tf={},
            regime_affinity_overrides={},
            strategy_timeframes={},
            strategy_sessions={},
            regime_detector={},
            soft_regime_enabled=True,
            intrabar_confidence_factor=0.85,
            htf_indicator_config={},
        )

        from src.backtesting.component_factory import build_backtest_components

        components = build_backtest_components()

        assert "signal_module" in components
        assert "pipeline" in components
        assert "regime_detector" in components
        assert "data_loader" in components

    @patch("src.backtesting.component_factory._load_signal_config_snapshot")
    @patch("src.backtesting.data.loader.HistoricalDataLoader")
    @patch("src.persistence.db.TimescaleWriter")
    @patch("src.persistence.repositories.market_repo.MarketRepository")
    def test_strategy_params_override(
        self,
        mock_market_repo: MagicMock,
        mock_writer: MagicMock,
        mock_loader: MagicMock,
        mock_signal_config: MagicMock,
    ) -> None:
        """strategy_params 覆盖应传递到 SignalModule。"""
        mock_signal_config.return_value = SimpleNamespace(
            strategy_params={"rsi_reversion__overbought": 75},
            strategy_params_per_tf={},
            regime_affinity_overrides={},
            strategy_timeframes={},
            strategy_sessions={},
            regime_detector={},
            soft_regime_enabled=True,
            intrabar_confidence_factor=0.85,
            htf_indicator_config={},
        )

        from src.backtesting.component_factory import build_backtest_components

        components = build_backtest_components(
            strategy_params={"rsi_reversion__overbought": 72},
        )
        assert components["signal_module"] is not None
