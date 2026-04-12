"""DataMatrix 单元测试 — 使用合成数据验证构建逻辑。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from src.research.core.data_matrix import DataMatrix


def _make_matrix(
    n_bars: int = 100,
    train_ratio: float = 0.7,
    forward_horizons: Optional[List[int]] = None,
) -> DataMatrix:
    """构造合成 DataMatrix，指标值与 close 线性相关。"""
    if forward_horizons is None:
        forward_horizons = [1, 5]

    closes = [100.0 + i * 0.1 for i in range(n_bars)]
    bar_times = [datetime(2026, 1, 1, tzinfo=timezone.utc) for _ in range(n_bars)]

    # 模拟指标：rsi 与 close 相关，adx 与 close 不相关
    from src.signals.evaluation.regime import RegimeType

    indicators: List[Dict[str, Dict[str, Any]]] = []
    regimes: List[RegimeType] = []
    for i in range(n_bars):
        indicators.append(
            {
                "rsi14": {"rsi": 30.0 + i * 0.4, "rsi_d3": 0.5},
                "adx14": {"adx": 25.0},
            }
        )
        if i % 3 == 0:
            regimes.append(RegimeType.TRENDING)
        elif i % 3 == 1:
            regimes.append(RegimeType.RANGING)
        else:
            regimes.append(RegimeType.BREAKOUT)

    # 前瞻收益
    forward_returns: Dict[int, List[Optional[float]]] = {}
    for h in forward_horizons:
        returns: List[Optional[float]] = []
        for i in range(n_bars):
            if i + h < n_bars and closes[i] > 0:
                returns.append((closes[i + h] - closes[i]) / closes[i])
            else:
                returns.append(None)
        forward_returns[h] = returns

    # 扁平化指标序列
    indicator_series: Dict[Tuple[str, str], List[Optional[float]]] = {
        ("rsi14", "rsi"): [30.0 + i * 0.4 for i in range(n_bars)],
        ("rsi14", "rsi_d3"): [0.5] * n_bars,
        ("adx14", "adx"): [25.0] * n_bars,
    }

    max_horizon = max(forward_horizons) if forward_horizons else 1
    train_end_idx = max(0, int(n_bars * train_ratio))
    split_idx = min(train_end_idx + max_horizon, n_bars - 1) if train_end_idx > 0 else 0

    return DataMatrix(
        symbol="XAUUSD",
        timeframe="H1",
        n_bars=n_bars,
        bar_times=bar_times,
        opens=closes,
        highs=[c + 1.0 for c in closes],
        lows=[c - 1.0 for c in closes],
        closes=closes,
        volumes=[100.0] * n_bars,
        indicators=indicators,
        regimes=regimes,
        soft_regimes=[None] * n_bars,
        forward_returns=forward_returns,
        indicator_series=indicator_series,
        train_end_idx=train_end_idx,
        split_idx=split_idx,
    )


class TestDataMatrix:
    def test_basic_properties(self) -> None:
        m = _make_matrix(100)
        assert m.n_bars == 100
        assert m.symbol == "XAUUSD"
        assert m.timeframe == "H1"
        assert len(m.closes) == 100
        assert len(m.regimes) == 100

    def test_train_test_split(self) -> None:
        m = _make_matrix(100, train_ratio=0.7)
        # train_end_idx=70, split_idx=70+5=75 (gap=max_horizon)
        assert m.train_end_idx == 70
        assert len(m.train_slice()) == 70
        # test starts after gap: [75, 100)
        assert m.split_idx == 75
        assert len(m.test_slice()) == 25

    def test_forward_returns(self) -> None:
        m = _make_matrix(100, forward_horizons=[1, 5])
        assert 1 in m.forward_returns
        assert 5 in m.forward_returns
        # 最后 1 个 bar 的 1-bar forward 应为 None
        assert m.forward_returns[1][-1] is None
        # 最后 5 个 bar 的 5-bar forward 应为 None
        for i in range(95, 100):
            assert m.forward_returns[5][i] is None
        # 第一个 bar 的 1-bar forward 应有值
        assert m.forward_returns[1][0] is not None
        assert m.forward_returns[1][0] > 0  # 上升趋势

    def test_indicator_series(self) -> None:
        m = _make_matrix(100)
        assert ("rsi14", "rsi") in m.indicator_series
        assert ("adx14", "adx") in m.indicator_series
        assert len(m.indicator_series[("rsi14", "rsi")]) == 100
        # RSI 序列应递增
        rsi = m.indicator_series[("rsi14", "rsi")]
        assert rsi[0] < rsi[50] < rsi[99]  # type: ignore[operator]

    def test_available_indicator_fields(self) -> None:
        m = _make_matrix(100)
        fields = m.available_indicator_fields()
        assert ("rsi14", "rsi") in fields
        assert ("adx14", "adx") in fields

    def test_regime_distribution(self) -> None:
        m = _make_matrix(99)  # 33 per regime
        from collections import Counter

        dist = Counter(r.value for r in m.regimes)
        assert dist["trending"] == 33
        assert dist["ranging"] == 33
        assert dist["breakout"] == 33

    def test_zero_train_ratio(self) -> None:
        """train_ratio=0 时 train_slice 应为空。"""
        m = _make_matrix(10, train_ratio=0.0)
        assert len(m.train_slice()) == 0
        # split_idx=0 when train_end_idx=0, test covers all
        assert len(m.test_slice()) == 10
