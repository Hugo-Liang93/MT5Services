"""engine.py 单元测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.backtesting.engine import BacktestEngine
from src.backtesting.models import BacktestConfig
from src.clients.mt5_market import OHLC
from src.signals.models import SignalDecision


def _make_bars(count: int, base_price: float = 2000.0) -> List[OHLC]:
    """生成测试用 OHLC bar 序列。"""
    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i in range(count):
        price = base_price + (i % 10) - 5  # 在 base_price ± 5 范围波动
        bars.append(
            OHLC(
                symbol="XAUUSD",
                timeframe="M5",
                time=base_time + timedelta(minutes=5 * i),
                open=price - 0.5,
                high=price + 2.0,
                low=price - 2.0,
                close=price,
                volume=100.0,
            )
        )
    return bars


class TestBacktestEngine:
    def _make_config(self, **kwargs: Any) -> BacktestConfig:
        defaults = {
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "start_time": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "end_time": datetime(2025, 1, 2, tzinfo=timezone.utc),
            "strategies": ["rsi_reversion"],
            "initial_balance": 10000.0,
            "warmup_bars": 50,
        }
        defaults.update(kwargs)
        return BacktestConfig(**defaults)

    def test_empty_data(self) -> None:
        """测试无数据时返回空结果。"""
        config = self._make_config()
        data_loader = MagicMock()
        data_loader.preload_warmup_bars.return_value = []
        data_loader.load_all_bars.return_value = []

        signal_module = MagicMock()
        signal_module.list_strategies.return_value = ["rsi_reversion"]
        signal_module.strategy_requirements.return_value = ["rsi14"]

        pipeline = MagicMock()

        engine = BacktestEngine(
            config=config,
            data_loader=data_loader,
            signal_module=signal_module,
            indicator_pipeline=pipeline,
        )
        result = engine.run()
        assert result.metrics.total_trades == 0

    def test_basic_run(self) -> None:
        """测试基本的回测流程。"""
        config = self._make_config(warmup_bars=20)
        warmup = _make_bars(20, 2000.0)
        test_data = _make_bars(50, 2000.0)
        # 调整时间使 test_data 在 warmup 之后
        for i, bar in enumerate(test_data):
            bar.time = warmup[-1].time + timedelta(minutes=5 * (i + 1))

        data_loader = MagicMock()
        data_loader.preload_warmup_bars.return_value = warmup
        data_loader.load_all_bars.return_value = test_data

        # Mock 信号模块
        signal_module = MagicMock()
        signal_module.list_strategies.return_value = ["rsi_reversion"]
        signal_module.strategy_requirements.return_value = ["rsi14"]

        # 交替生成 buy/hold 信号
        call_count = 0

        def mock_evaluate(**kwargs: Any) -> SignalDecision:
            nonlocal call_count
            call_count += 1
            if call_count % 10 == 1:
                action, conf = "buy", 0.7
            else:
                action, conf = "hold", 0.0
            return SignalDecision(
                strategy="rsi_reversion",
                symbol="XAUUSD",
                timeframe="M5",
                action=action,
                confidence=conf,
                reason="test",
                used_indicators=["rsi14"],
                timestamp=datetime.now(timezone.utc),
                metadata={},
            )

        signal_module.evaluate.side_effect = mock_evaluate

        # Mock 指标管线
        pipeline = MagicMock()
        pipeline.compute.return_value = {
            "rsi14": {"rsi": 30.0},
            "atr14": {"atr": 5.0},
        }

        engine = BacktestEngine(
            config=config,
            data_loader=data_loader,
            signal_module=signal_module,
            indicator_pipeline=pipeline,
        )
        result = engine.run()

        assert result.run_id.startswith("bt_")
        assert result.config == config
        assert isinstance(result.metrics.win_rate, float)
        # 管线应该被调用了
        assert pipeline.compute.call_count > 0

    def test_indicator_failure_skips_bar(self) -> None:
        """指标计算失败时应跳过该 bar。"""
        config = self._make_config(warmup_bars=5)
        warmup = _make_bars(5)
        test_data = _make_bars(10)
        for i, bar in enumerate(test_data):
            bar.time = warmup[-1].time + timedelta(minutes=5 * (i + 1))

        data_loader = MagicMock()
        data_loader.preload_warmup_bars.return_value = warmup
        data_loader.load_all_bars.return_value = test_data

        signal_module = MagicMock()
        signal_module.list_strategies.return_value = ["test_strategy"]
        signal_module.strategy_requirements.return_value = ["rsi14"]

        pipeline = MagicMock()
        pipeline.compute.side_effect = Exception("compute failed")

        engine = BacktestEngine(
            config=config,
            data_loader=data_loader,
            signal_module=signal_module,
            indicator_pipeline=pipeline,
        )
        result = engine.run()
        # 所有 bar 都应被跳过，无交易
        assert result.metrics.total_trades == 0
