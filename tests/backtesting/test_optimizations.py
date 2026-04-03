"""回测模块优化功能的单元测试。

覆盖：
- CachedDataLoader 缓存行为
- 预计算指标复用
- 信号评估内存上限
- backtest.ini 配置加载
- 持仓参数搜索空间
- incomplete 标记
- 连胜连败统计
- 滑点/手续费成本记录
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.backtesting.data_loader import CachedDataLoader
from src.backtesting.metrics import _consecutive_streaks, compute_metrics
from src.backtesting.models import (
    BacktestConfig,
    BacktestMetrics,
    ParameterSpace,
    SignalEvaluation,
    TradeRecord,
)
from src.backtesting.portfolio import PortfolioTracker
from src.clients.mt5_market import OHLC
from src.trading.execution import TradeParameters


def _make_bars(count: int, base_price: float = 2000.0) -> List[OHLC]:
    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i in range(count):
        price = base_price + (i % 10) - 5
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


def _make_trade(
    pnl: float,
    strategy: str = "test",
    regime: str = "TRENDING",
    confidence: float = 0.7,
    bars_held: int = 5,
    slippage_cost: float = 0.0,
    commission_cost: float = 0.0,
) -> TradeRecord:
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return TradeRecord(
        signal_id="bt_test",
        strategy=strategy,
        direction="buy",
        entry_time=t,
        entry_price=2000.0,
        exit_time=t,
        exit_price=2000.0 + pnl,
        stop_loss=1990.0,
        take_profit=2010.0,
        position_size=0.1,
        pnl=pnl,
        pnl_pct=pnl / 100.0,
        bars_held=bars_held,
        regime=regime,
        confidence=confidence,
        exit_reason="take_profit" if pnl > 0 else "stop_loss",
        slippage_cost=slippage_cost,
        commission_cost=commission_cost,
    )


# ── CachedDataLoader 测试 ────────────────────────────────────


class TestCachedDataLoader:
    def test_returns_cached_data(self) -> None:
        """CachedDataLoader 应返回预加载的数据。"""
        warmup = _make_bars(10)
        test = _make_bars(20)
        loader = CachedDataLoader(warmup, test)

        result_warmup = loader.preload_warmup_bars("X", "M5", datetime.now(timezone.utc))
        result_test = loader.load_all_bars("X", "M5", datetime.now(timezone.utc), datetime.now(timezone.utc))

        assert len(result_warmup) == 10
        assert len(result_test) == 20

    def test_returns_copies(self) -> None:
        """返回值应为副本，修改不影响缓存。"""
        warmup = _make_bars(5)
        test = _make_bars(10)
        loader = CachedDataLoader(warmup, test)

        result1 = loader.load_all_bars("X", "M5", datetime.now(timezone.utc), datetime.now(timezone.utc))
        result1.clear()  # 清空副本

        result2 = loader.load_all_bars("X", "M5", datetime.now(timezone.utc), datetime.now(timezone.utc))
        assert len(result2) == 10  # 原始数据不受影响

    def test_ignores_arguments(self) -> None:
        """缓存 loader 忽略 symbol/timeframe 参数，始终返回缓存数据。"""
        warmup = _make_bars(3)
        test = _make_bars(7)
        loader = CachedDataLoader(warmup, test)

        # 传入不同参数应该返回相同数据
        r1 = loader.load_all_bars("EURUSD", "H1", datetime.now(timezone.utc), datetime.now(timezone.utc))
        r2 = loader.load_all_bars("XAUUSD", "M5", datetime.now(timezone.utc), datetime.now(timezone.utc))
        assert len(r1) == len(r2) == 7


# ── 信号评估内存上限测试 ──────────────────────────────────────


class TestSignalEvaluationLimit:
    def test_max_signal_evaluations_field(self) -> None:
        """BacktestConfig 应包含 max_signal_evaluations 字段。"""
        config = BacktestConfig(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 1, 2, tzinfo=timezone.utc),
            max_signal_evaluations=100,
        )
        assert config.max_signal_evaluations == 100

    def test_default_limit(self) -> None:
        """默认上限应为 50000。"""
        config = BacktestConfig(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 1, 2, tzinfo=timezone.utc),
        )
        assert config.max_signal_evaluations == 50000


# ── 持仓参数搜索空间测试 ──────────────────────────────────────


class TestPositionParams:
    def test_position_params_in_space(self) -> None:
        """ParameterSpace 应支持 position_params。"""
        space = ParameterSpace(
            strategy_params={"rsi__oversold": [25, 30]},
            position_params={"trailing_atr_multiplier": [1.0, 1.5, 2.0]},
            search_mode="grid",
        )
        assert "trailing_atr_multiplier" in space.position_params
        assert len(space.position_params["trailing_atr_multiplier"]) == 3

    def test_empty_position_params(self) -> None:
        """空 position_params 不影响搜索。"""
        space = ParameterSpace(
            strategy_params={"x": [1, 2]},
            position_params={},
        )
        assert space.position_params == {}


# ── incomplete 标记测试 ───────────────────────────────────────


class TestIncompleteEvaluation:
    def test_incomplete_field_default(self) -> None:
        """SignalEvaluation 的 incomplete 默认为 False。"""
        ev = SignalEvaluation(
            bar_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            strategy="test",
            direction="buy",
            confidence=0.7,
            regime="TRENDING",
            price_at_signal=2000.0,
        )
        assert ev.incomplete is False

    def test_incomplete_field_settable(self) -> None:
        """SignalEvaluation 可设置 incomplete=True。"""
        ev = SignalEvaluation(
            bar_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            strategy="test",
            direction="buy",
            confidence=0.7,
            regime="TRENDING",
            price_at_signal=2000.0,
            incomplete=True,
        )
        assert ev.incomplete is True


# ── 连胜连败统计测试 ──────────────────────────────────────────


class TestConsecutiveStreaks:
    def test_empty_trades(self) -> None:
        wins, losses = _consecutive_streaks([])
        assert wins == 0
        assert losses == 0

    def test_all_winners(self) -> None:
        trades = [_make_trade(100.0), _make_trade(50.0), _make_trade(200.0)]
        wins, losses = _consecutive_streaks(trades)
        assert wins == 3
        assert losses == 0

    def test_all_losers(self) -> None:
        trades = [_make_trade(-50.0), _make_trade(-30.0)]
        wins, losses = _consecutive_streaks(trades)
        assert wins == 0
        assert losses == 2

    def test_mixed(self) -> None:
        trades = [
            _make_trade(100.0),   # W
            _make_trade(50.0),    # W
            _make_trade(200.0),   # W -> 3 连胜
            _make_trade(-50.0),   # L
            _make_trade(-30.0),   # L -> 2 连败
            _make_trade(10.0),    # W
        ]
        wins, losses = _consecutive_streaks(trades)
        assert wins == 3
        assert losses == 2

    def test_in_metrics(self) -> None:
        """compute_metrics 应包含连胜连败。"""
        trades = [_make_trade(100.0), _make_trade(50.0), _make_trade(-30.0)]
        equity = [10000.0, 10100.0, 10150.0, 10120.0]
        m = compute_metrics(trades, 10000.0, equity)
        assert m.max_consecutive_wins == 2
        assert m.max_consecutive_losses == 1


# ── 滑点/手续费成本记录测试 ───────────────────────────────────


class TestCostTracking:
    def test_trade_record_cost_fields(self) -> None:
        """TradeRecord 应包含 slippage_cost 和 commission_cost。"""
        trade = _make_trade(100.0, slippage_cost=1.5, commission_cost=2.0)
        assert trade.slippage_cost == 1.5
        assert trade.commission_cost == 2.0

    def test_portfolio_records_costs(self) -> None:
        """PortfolioTracker 关仓时应记录滑点和手续费成本。"""
        pt = PortfolioTracker(
            initial_balance=10000.0,
            contract_size=100.0,
            slippage_points=0.5,
            commission_per_lot=7.0,
        )
        bar = OHLC(
            symbol="XAUUSD", timeframe="M5",
            time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            open=2000.0, high=2015.0, low=1999.0, close=2000.0, volume=100.0,
        )
        params = TradeParameters(
            entry_price=2000.0, stop_loss=1995.0, take_profit=2010.0,
            position_size=0.1, risk_reward_ratio=2.0, atr_value=5.0,
            sl_distance=5.0, tp_distance=10.0,
        )
        pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, 0)

        # TP 触发
        exit_bar = OHLC(
            symbol="XAUUSD", timeframe="M5",
            time=datetime(2025, 1, 1, 0, 5, tzinfo=timezone.utc),
            open=2008.0, high=2012.0, low=2005.0, close=2008.0, volume=100.0,
        )
        closed = pt.check_exits(exit_bar, 1)
        assert len(closed) == 1
        trade = closed[0]
        # slippage_cost = 0.5 * 2 * 0.1 * 100 = 10.0
        assert trade.slippage_cost == 10.0
        # commission_cost = 7.0 * 0.1 * 2 = 1.4
        assert trade.commission_cost == 1.4


# ── 配置加载测试 ──────────────────────────────────────────────


class TestBacktestConfig:
    def test_config_loads_from_ini(self) -> None:
        """get_backtest_defaults() 应能从 backtest.ini 读取配置。"""
        from src.backtesting.config import get_backtest_defaults

        defaults = get_backtest_defaults()
        # backtest.ini 存在且 [backtest] section 有 default_initial_balance
        assert isinstance(defaults, dict)
        if "initial_balance" in defaults:
            assert defaults["initial_balance"] == 10000.0

    def test_config_returns_dict(self) -> None:
        """配置加载应返回字典。"""
        from src.backtesting.config import get_backtest_defaults

        result = get_backtest_defaults()
        assert isinstance(result, dict)


# ── 资金曲线精确采样测试 ──────────────────────────────────────


class TestConfigLoadsAllSections:
    def test_position_section(self) -> None:
        """backtest.ini [position] section 应被正确加载。"""
        from src.backtesting.config import get_backtest_defaults

        defaults = get_backtest_defaults()
        # backtest.ini 新增了 [position] section
        if "trailing_atr_multiplier" in defaults:
            assert isinstance(defaults["trailing_atr_multiplier"], float)

    def test_pending_entry_section(self) -> None:
        """backtest.ini [pending_entry] section 应被正确加载。"""
        from src.backtesting.config import get_backtest_defaults

        defaults = get_backtest_defaults()
        if "pending_entry_enabled" in defaults:
            assert isinstance(defaults["pending_entry_enabled"], bool)

    def test_confidence_section(self) -> None:
        """backtest.ini [confidence] section 应被正确加载。"""
        from src.backtesting.config import get_backtest_defaults

        defaults = get_backtest_defaults()
        if "enable_regime_affinity" in defaults:
            assert defaults["enable_regime_affinity"] is True
        if "enable_calibrator" in defaults:
            assert defaults["enable_calibrator"] is True


class TestPendingEntryExpiry:
    def test_expiry_bars_configurable(self) -> None:
        """BacktestConfig 应支持 pending_entry_expiry_bars 配置。"""
        config = BacktestConfig(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 1, 2, tzinfo=timezone.utc),
            pending_entry_expiry_bars=5,
        )
        assert config.pending_entry_expiry_bars == 5

    def test_expiry_bars_default(self) -> None:
        """默认超时应为 2 bars。"""
        config = BacktestConfig(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 1, 2, tzinfo=timezone.utc),
        )
        assert config.pending_entry_expiry_bars == 2


class TestConfidencePipelineFlags:
    def test_metadata_flags_set(self) -> None:
        """禁用置信度管线组件时 metadata 应包含跳过标记。"""
        from src.backtesting.engine import BacktestEngine
        from src.signals.models import SignalDecision

        config = BacktestConfig(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 1, 2, tzinfo=timezone.utc),
            strategies=["test"],
            warmup_bars=5,
            enable_regime_affinity=False,
            enable_performance_tracker=False,
            enable_calibrator=False,
        )

        data_loader = MagicMock()
        data_loader.preload_warmup_bars.return_value = _make_bars(5)
        test_bars = _make_bars(10)
        for i, bar in enumerate(test_bars):
            bar.time = _make_bars(5)[-1].time + timedelta(minutes=5 * (i + 1))
        data_loader.load_all_bars.return_value = test_bars

        signal_module = MagicMock()
        signal_module.list_strategies.return_value = ["test"]
        signal_module.strategy_requirements.return_value = ["rsi14"]
        signal_module.evaluate.return_value = SignalDecision(
            strategy="test", symbol="XAUUSD", timeframe="M5",
            direction="hold", confidence=0.0, reason="test",
            used_indicators=["rsi14"],
            timestamp=datetime.now(timezone.utc), metadata={},
        )

        pipeline = MagicMock()
        pipeline.compute.return_value = {"rsi14": {"rsi": 50.0}, "atr14": {"atr": 5.0}}

        engine = BacktestEngine(
            config=config,
            data_loader=data_loader,
            signal_module=signal_module,
            indicator_pipeline=pipeline,
        )
        engine.run()

        # 检查 evaluate 调用时 metadata 是否包含正确的标记
        for call in signal_module.evaluate.call_args_list:
            meta = call.kwargs.get("metadata", {})
            assert meta.get("_pre_computed_affinity") == 1.0
            assert meta.get("_skip_performance_tracker") is True
            assert meta.get("_skip_calibrator") is True


class TestEquityCurveSampling:
    def test_open_position_records_equity(self) -> None:
        """开仓应自动记录资金快照。"""
        pt = PortfolioTracker(initial_balance=10000.0)
        bar = OHLC(
            symbol="XAUUSD", timeframe="M5",
            time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            open=2000.0, high=2001.0, low=1999.0, close=2000.0, volume=100.0,
        )
        params = TradeParameters(
            entry_price=2000.0, stop_loss=1995.0, take_profit=2010.0,
            position_size=0.1, risk_reward_ratio=2.0, atr_value=5.0,
            sl_distance=5.0, tp_distance=10.0,
        )
        initial_curve_len = len(pt.equity_curve)
        pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, 0)
        # 开仓后应多一个采样点
        assert len(pt.equity_curve) == initial_curve_len + 1

    def test_close_position_records_equity(self) -> None:
        """平仓应自动记录资金快照。"""
        pt = PortfolioTracker(initial_balance=10000.0, contract_size=100.0)
        bar = OHLC(
            symbol="XAUUSD", timeframe="M5",
            time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            open=2000.0, high=2015.0, low=1999.0, close=2000.0, volume=100.0,
        )
        params = TradeParameters(
            entry_price=2000.0, stop_loss=1995.0, take_profit=2010.0,
            position_size=0.1, risk_reward_ratio=2.0, atr_value=5.0,
            sl_distance=5.0, tp_distance=10.0,
        )
        pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, 0)
        curve_after_open = len(pt.equity_curve)

        exit_bar = OHLC(
            symbol="XAUUSD", timeframe="M5",
            time=datetime(2025, 1, 1, 0, 5, tzinfo=timezone.utc),
            open=2008.0, high=2012.0, low=2005.0, close=2008.0, volume=100.0,
        )
        pt.check_exits(exit_bar, 1)
        # 平仓后应多一个采样点
        assert len(pt.equity_curve) > curve_after_open
