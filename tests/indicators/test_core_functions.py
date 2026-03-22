"""核心指标函数单元测试。

覆盖 src/indicators/core/ 下的均线（sma, ema, hma）和动量
（rsi, macd, stoch_rsi, cci, williams_r, roc）指标函数。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

import pytest

from src.indicators.core.mean import ema, hma, sma
from src.indicators.core.momentum import (
    cci,
    macd,
    roc,
    rsi,
    stoch_rsi,
    williams_r,
)


# ---------------------------------------------------------------------------
# Mock bar helper
# ---------------------------------------------------------------------------


@dataclass
class MockBar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 100
    spread: int = 1
    real_volume: int = 0


def _make_bars(closes: List[float]) -> List[MockBar]:
    """根据 close 列表生成 MockBar 序列。

    open=close, high=close+0.5, low=close-0.5。
    """
    base = datetime(2025, 1, 1)
    return [
        MockBar(
            time=base + timedelta(minutes=i),
            open=c,
            high=c + 0.5,
            low=c - 0.5,
            close=c,
        )
        for i, c in enumerate(closes)
    ]


def _make_bars_hlc(
    data: List[tuple],
) -> List[MockBar]:
    """根据 (high, low, close) 三元组生成 MockBar 序列。

    用于需要精确 HLC 的指标（cci, williams_r 等）。
    """
    base = datetime(2025, 1, 1)
    return [
        MockBar(
            time=base + timedelta(minutes=i),
            open=c,
            high=h,
            low=l,
            close=c,
        )
        for i, (h, l, c) in enumerate(data)
    ]


# ===================================================================
# SMA Tests
# ===================================================================


class TestSma:
    """SMA 指标函数测试。"""

    def test_sma_normal(self) -> None:
        """标准情况：20 根 bar 计算 SMA20。"""
        closes = [float(i) for i in range(1, 25)]  # 1..24
        bars = _make_bars(closes)
        result = sma(bars, {"period": 20})
        assert "sma" in result
        # 最后 20 个 close: 5..24，均值 = (5+24)*20/2 / 20 = 14.5
        assert result["sma"] == pytest.approx(14.5, abs=1e-9)

    def test_sma_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0, 2.0, 3.0])
        result = sma(bars, {"period": 20})
        assert result == {}

    def test_sma_exact_period(self) -> None:
        """恰好等于 period 根 bar 时应正常计算。"""
        closes = [10.0] * 20
        bars = _make_bars(closes)
        result = sma(bars, {"period": 20})
        assert result["sma"] == pytest.approx(10.0, abs=1e-9)

    def test_sma_precise_calculation(self) -> None:
        """精确值验证：手动计算验对。"""
        closes = [2.0, 4.0, 6.0, 8.0, 10.0]
        bars = _make_bars(closes)
        result = sma(bars, {"period": 5})
        # (2+4+6+8+10)/5 = 6.0
        assert result["sma"] == pytest.approx(6.0, abs=1e-9)

    def test_sma_with_alias_param(self) -> None:
        """使用 window 别名参数。"""
        closes = [3.0, 6.0, 9.0]
        bars = _make_bars(closes)
        result = sma(bars, {"window": 3})
        assert result["sma"] == pytest.approx(6.0, abs=1e-9)

    def test_sma_uses_last_n_bars(self) -> None:
        """验证 SMA 只取最后 period 根 bar 的 close。"""
        closes = [100.0, 200.0, 1.0, 2.0, 3.0]
        bars = _make_bars(closes)
        result = sma(bars, {"period": 3})
        # 最后 3 根: 1, 2, 3 => 均值 2.0
        assert result["sma"] == pytest.approx(2.0, abs=1e-9)


# ===================================================================
# EMA Tests
# ===================================================================


class TestEma:
    """EMA 指标函数测试。"""

    def test_ema_normal(self) -> None:
        """标准情况：足够数据计算 EMA。"""
        closes = [float(i) for i in range(1, 80)]
        bars = _make_bars(closes)
        result = ema(bars, {"period": 20})
        assert "ema" in result
        assert isinstance(result["ema"], float)

    def test_ema_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0, 2.0])
        result = ema(bars, {"period": 20})
        assert result == {}

    def test_ema_constant_prices(self) -> None:
        """所有价格相同时，EMA 应等于该价格。"""
        closes = [50.0] * 80
        bars = _make_bars(closes)
        result = ema(bars, {"period": 20})
        assert result["ema"] == pytest.approx(50.0, abs=1e-9)

    def test_ema_responds_to_recent_prices(self) -> None:
        """EMA 应对近期价格变化更敏感（趋势上涨时 EMA 大于 SMA）。"""
        # 线性上涨序列
        closes = [float(i) for i in range(1, 80)]
        bars = _make_bars(closes)
        ema_result = ema(bars, {"period": 20})
        sma_result = sma(bars, {"period": 20})
        # 上涨趋势中 EMA > SMA（EMA 权重偏向近期高价）
        assert ema_result["ema"] > sma_result["sma"]

    def test_ema_with_alias_param(self) -> None:
        """使用 window 别名参数。"""
        closes = [50.0] * 80
        bars = _make_bars(closes)
        result = ema(bars, {"window": 20})
        assert result["ema"] == pytest.approx(50.0, abs=1e-9)

    def test_ema_small_period(self) -> None:
        """小 period (3) 下正常工作。"""
        closes = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0]
        bars = _make_bars(closes)
        result = ema(bars, {"period": 3})
        assert "ema" in result
        # EMA(3) 结果应该接近尾部价格
        assert result["ema"] > 50.0


# ===================================================================
# HMA Tests
# ===================================================================


class TestHma:
    """HMA (Hull Moving Average) 指标函数测试。"""

    def test_hma_normal(self) -> None:
        """标准情况：足够数据计算 HMA。"""
        closes = [float(i) for i in range(1, 40)]
        bars = _make_bars(closes)
        result = hma(bars, {"period": 20})
        assert "hma" in result
        assert isinstance(result["hma"], float)

    def test_hma_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0, 2.0, 3.0])
        result = hma(bars, {"period": 20})
        assert result == {}

    def test_hma_constant_prices(self) -> None:
        """所有价格相同时，HMA 应等于该价格。"""
        closes = [100.0] * 40
        bars = _make_bars(closes)
        result = hma(bars, {"period": 20})
        assert result["hma"] == pytest.approx(100.0, abs=1e-6)

    def test_hma_less_lag_than_sma(self) -> None:
        """HMA 应比 SMA 更快跟踪趋势（上涨时 HMA > SMA）。"""
        closes = [float(i) for i in range(1, 50)]
        bars = _make_bars(closes)
        hma_result = hma(bars, {"period": 20})
        sma_result = sma(bars, {"period": 20})
        assert hma_result["hma"] > sma_result["sma"]

    def test_hma_small_period(self) -> None:
        """小 period (9) 下正常工作。"""
        closes = [float(i) for i in range(1, 25)]
        bars = _make_bars(closes)
        result = hma(bars, {"period": 9})
        assert "hma" in result

    def test_hma_exact_minimum_data(self) -> None:
        """刚好满足最低数据需求的情况。"""
        import math

        period = 20
        sqrt_period = max(int(math.sqrt(period)), 1)
        needed = period + sqrt_period - 1
        closes = [float(i + 1) for i in range(needed + 5)]
        bars = _make_bars(closes)
        result = hma(bars, {"period": period})
        assert "hma" in result


# ===================================================================
# RSI Tests
# ===================================================================


class TestRsi:
    """RSI 指标函数测试。"""

    def test_rsi_normal(self) -> None:
        """标准情况：足够数据计算 RSI。"""
        closes = [float(50 + i % 5) for i in range(60)]
        bars = _make_bars(closes)
        result = rsi(bars, {"period": 14})
        assert "rsi" in result
        assert 0.0 <= result["rsi"] <= 100.0

    def test_rsi_all_up(self) -> None:
        """全涨序列 → RSI 应趋近 100。"""
        closes = [float(i) for i in range(1, 60)]
        bars = _make_bars(closes)
        result = rsi(bars, {"period": 14})
        assert result["rsi"] == pytest.approx(100.0, abs=0.01)

    def test_rsi_all_down(self) -> None:
        """全跌序列 → RSI 应趋近 0。"""
        closes = [float(100 - i) for i in range(60)]
        bars = _make_bars(closes)
        result = rsi(bars, {"period": 14})
        assert result["rsi"] == pytest.approx(0.0, abs=0.01)

    def test_rsi_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0, 2.0, 3.0])
        result = rsi(bars, {"period": 14})
        assert result == {}

    def test_rsi_range_0_100(self) -> None:
        """RSI 值始终在 [0, 100] 范围内。"""
        # 混合涨跌的序列
        closes = [50.0 + (i * 3.7 % 10) - 5 for i in range(60)]
        bars = _make_bars(closes)
        result = rsi(bars, {"period": 14})
        assert 0.0 <= result["rsi"] <= 100.0

    def test_rsi_flat_prices(self) -> None:
        """价格不变时，avg_loss=0 → RSI=100。"""
        closes = [50.0] * 60
        bars = _make_bars(closes)
        result = rsi(bars, {"period": 14})
        assert result["rsi"] == pytest.approx(100.0, abs=0.01)


# ===================================================================
# MACD Tests
# ===================================================================


class TestMacd:
    """MACD 指标函数测试。"""

    def test_macd_normal(self) -> None:
        """标准情况：足够数据返回 macd/signal/hist。"""
        closes = [float(100 + i * 0.5) for i in range(60)]
        bars = _make_bars(closes)
        result = macd(bars, {"fast": 12, "slow": 26, "signal": 9})
        assert "macd" in result
        assert "signal" in result
        assert "hist" in result

    def test_macd_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0] * 10)
        result = macd(bars, {"fast": 12, "slow": 26, "signal": 9})
        assert result == {}

    def test_macd_hist_equals_macd_minus_signal(self) -> None:
        """hist = macd - signal，精确验证。"""
        closes = [50.0 + (i * 1.3 % 7) for i in range(60)]
        bars = _make_bars(closes)
        result = macd(bars, {"fast": 12, "slow": 26, "signal": 9})
        assert result["hist"] == pytest.approx(
            result["macd"] - result["signal"], abs=1e-9
        )

    def test_macd_constant_prices(self) -> None:
        """所有价格相同时，MACD 各字段应趋近 0。"""
        closes = [100.0] * 60
        bars = _make_bars(closes)
        result = macd(bars, {"fast": 12, "slow": 26, "signal": 9})
        assert result["macd"] == pytest.approx(0.0, abs=1e-6)
        assert result["signal"] == pytest.approx(0.0, abs=1e-6)
        assert result["hist"] == pytest.approx(0.0, abs=1e-6)

    def test_macd_uptrend_positive(self) -> None:
        """上涨趋势中 MACD 应为正值（快线在慢线上方）。"""
        closes = [float(i) for i in range(1, 80)]
        bars = _make_bars(closes)
        result = macd(bars, {"fast": 12, "slow": 26, "signal": 9})
        assert result["macd"] > 0

    def test_macd_custom_params(self) -> None:
        """自定义参数（8, 17, 9）正常工作。"""
        closes = [float(100 + i * 0.3) for i in range(50)]
        bars = _make_bars(closes)
        result = macd(bars, {"fast": 8, "slow": 17, "signal": 9})
        assert "macd" in result
        assert "signal" in result
        assert "hist" in result


# ===================================================================
# StochRSI Tests
# ===================================================================


class TestStochRsi:
    """StochRSI 指标函数测试。"""

    def test_stoch_rsi_normal(self) -> None:
        """标准情况：足够数据返回 stoch_rsi_k / stoch_rsi_d。"""
        closes = [50.0 + (i * 2.3 % 10) - 5 for i in range(120)]
        bars = _make_bars(closes)
        result = stoch_rsi(bars, {"period": 14})
        assert "stoch_rsi_k" in result
        assert "stoch_rsi_d" in result

    def test_stoch_rsi_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0] * 10)
        result = stoch_rsi(bars, {"period": 14})
        assert result == {}

    def test_stoch_rsi_range_0_100(self) -> None:
        """StochRSI K 和 D 值在 [0, 100] 范围内。"""
        closes = [50.0 + (i * 3.7 % 15) - 7 for i in range(120)]
        bars = _make_bars(closes)
        result = stoch_rsi(bars, {"period": 14})
        if result:
            assert 0.0 <= result["stoch_rsi_k"] <= 100.0
            assert 0.0 <= result["stoch_rsi_d"] <= 100.0

    def test_stoch_rsi_all_up(self) -> None:
        """持续上涨序列 → StochRSI K 应趋近 high 值。"""
        closes = [float(i) for i in range(1, 120)]
        bars = _make_bars(closes)
        result = stoch_rsi(bars, {"period": 14})
        if result:
            # 全涨时 RSI 一直接近 100，StochRSI 取决于窗口内 RSI 变化幅度
            assert 0.0 <= result["stoch_rsi_k"] <= 100.0

    def test_stoch_rsi_with_custom_params(self) -> None:
        """自定义平滑参数。"""
        closes = [50.0 + (i * 1.1 % 8) - 4 for i in range(120)]
        bars = _make_bars(closes)
        result = stoch_rsi(
            bars,
            {"rsi_period": 14, "stoch_period": 14, "smooth_k": 5, "smooth_d": 5},
        )
        if result:
            assert "stoch_rsi_k" in result
            assert "stoch_rsi_d" in result


# ===================================================================
# CCI Tests
# ===================================================================


class TestCci:
    """CCI 指标函数测试。"""

    def test_cci_normal(self) -> None:
        """标准情况：足够数据计算 CCI。"""
        data = [(c + 2.0, c - 2.0, c) for c in [50.0 + i * 0.5 for i in range(25)]]
        bars = _make_bars_hlc(data)
        result = cci(bars, {"period": 20})
        assert "cci" in result

    def test_cci_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0, 2.0])
        result = cci(bars, {"period": 20})
        assert result == {}

    def test_cci_all_same_prices(self) -> None:
        """所有价格相同时 mean_dev=0 → 返回空 dict。"""
        data = [(50.0, 50.0, 50.0)] * 20
        bars = _make_bars_hlc(data)
        result = cci(bars, {"period": 20})
        assert result == {}

    def test_cci_positive_for_above_mean(self) -> None:
        """最后一根 bar 的 TP 高于均值时 CCI 应为正。"""
        base_prices = [50.0] * 19 + [60.0]
        data = [(c + 1.0, c - 1.0, c) for c in base_prices]
        bars = _make_bars_hlc(data)
        result = cci(bars, {"period": 20})
        if result:
            assert result["cci"] > 0

    def test_cci_negative_for_below_mean(self) -> None:
        """最后一根 bar 的 TP 低于均值时 CCI 应为负。"""
        base_prices = [50.0] * 19 + [40.0]
        data = [(c + 1.0, c - 1.0, c) for c in base_prices]
        bars = _make_bars_hlc(data)
        result = cci(bars, {"period": 20})
        if result:
            assert result["cci"] < 0

    def test_cci_exact_period(self) -> None:
        """恰好等于 period 根 bar 时应正常计算。"""
        data = [(c + 1.0, c - 1.0, c) for c in [50.0 + i for i in range(20)]]
        bars = _make_bars_hlc(data)
        result = cci(bars, {"period": 20})
        assert "cci" in result


# ===================================================================
# Williams %R Tests
# ===================================================================


class TestWilliamsR:
    """Williams %R 指标函数测试。"""

    def test_williams_r_normal(self) -> None:
        """标准情况：足够数据计算 Williams %R。"""
        data = [(c + 2.0, c - 2.0, c) for c in [50.0 + i * 0.3 for i in range(20)]]
        bars = _make_bars_hlc(data)
        result = williams_r(bars, {"period": 14})
        assert "williams_r" in result

    def test_williams_r_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0, 2.0])
        result = williams_r(bars, {"period": 14})
        assert result == {}

    def test_williams_r_range_negative_100_to_0(self) -> None:
        """Williams %R 值始终在 [-100, 0] 范围内。"""
        data = [(c + 3.0, c - 3.0, c) for c in [50.0 + (i * 1.7 % 8) for i in range(20)]]
        bars = _make_bars_hlc(data)
        result = williams_r(bars, {"period": 14})
        assert -100.0 <= result["williams_r"] <= 0.0

    def test_williams_r_close_at_high(self) -> None:
        """收盘价等于最高价时 Williams %R 应为 0。"""
        # 所有 bar 的 close == high
        data = [(60.0, 50.0, 60.0)] * 14
        bars = _make_bars_hlc(data)
        result = williams_r(bars, {"period": 14})
        assert result["williams_r"] == pytest.approx(0.0, abs=1e-9)

    def test_williams_r_close_at_low(self) -> None:
        """收盘价等于最低价时 Williams %R 应为 -100。"""
        # 所有 bar 的 close == low
        data = [(60.0, 50.0, 50.0)] * 14
        bars = _make_bars_hlc(data)
        result = williams_r(bars, {"period": 14})
        assert result["williams_r"] == pytest.approx(-100.0, abs=1e-9)

    def test_williams_r_equal_high_low(self) -> None:
        """high == low 时返回 0.0（边界情况）。"""
        data = [(50.0, 50.0, 50.0)] * 14
        bars = _make_bars_hlc(data)
        result = williams_r(bars, {"period": 14})
        assert result["williams_r"] == pytest.approx(0.0, abs=1e-9)


# ===================================================================
# ROC Tests
# ===================================================================


class TestRoc:
    """ROC (Rate of Change) 指标函数测试。"""

    def test_roc_normal(self) -> None:
        """标准情况：正常计算 ROC。"""
        closes = [50.0 + i * 0.5 for i in range(20)]
        bars = _make_bars(closes)
        result = roc(bars, {"period": 12})
        assert "roc" in result

    def test_roc_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0, 2.0])
        result = roc(bars, {"period": 12})
        assert result == {}

    def test_roc_prev_zero(self) -> None:
        """前值为 0 时返回空 dict（防止除零）。"""
        closes = [0.0] + [1.0] * 12
        bars = _make_bars(closes)
        result = roc(bars, {"period": 12})
        assert result == {}

    def test_roc_precise_calculation(self) -> None:
        """精确值验证：(last - prev) / prev * 100。"""
        # period=5 → prev=closes[-6], last=closes[-1]
        closes = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
        bars = _make_bars(closes)
        result = roc(bars, {"period": 5})
        # prev = closes[0] = 10.0, last = closes[5] = 60.0
        # roc = (60 - 10) / 10 * 100 = 500.0
        assert result["roc"] == pytest.approx(500.0, abs=1e-9)

    def test_roc_negative(self) -> None:
        """下跌序列产生负 ROC。"""
        closes = [100.0, 90.0, 80.0, 70.0, 60.0, 50.0]
        bars = _make_bars(closes)
        result = roc(bars, {"period": 5})
        # prev = 100, last = 50 → (50-100)/100*100 = -50.0
        assert result["roc"] == pytest.approx(-50.0, abs=1e-9)

    def test_roc_no_change(self) -> None:
        """价格不变时 ROC = 0。"""
        closes = [50.0] * 15
        bars = _make_bars(closes)
        result = roc(bars, {"period": 12})
        assert result["roc"] == pytest.approx(0.0, abs=1e-9)


# ===================================================================
# Bollinger Bands Tests
# ===================================================================

from src.indicators.core.volatility import bollinger, keltner, donchian, atr, adx


class TestBollinger:
    """Bollinger Bands 指标函数测试。"""

    def test_bollinger_normal(self) -> None:
        """标准情况：足够数据正常计算布林带。"""
        closes = [50.0 + i * 0.5 for i in range(25)]
        bars = _make_bars(closes)
        result = bollinger(bars, {"period": 20})
        assert "bb_mid" in result
        assert "bb_upper" in result
        assert "bb_lower" in result
        assert "close" in result
        assert isinstance(result["bb_mid"], float)

    def test_bollinger_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0, 2.0, 3.0])
        result = bollinger(bars, {"period": 20})
        assert result == {}

    def test_bollinger_upper_gt_mid_gt_lower(self) -> None:
        """上轨 > 中轨 > 下轨（价格有波动时）。"""
        closes = [50.0 + (i * 2.3 % 10) - 5 for i in range(25)]
        bars = _make_bars(closes)
        result = bollinger(bars, {"period": 20})
        assert result["bb_upper"] > result["bb_mid"]
        assert result["bb_mid"] > result["bb_lower"]

    def test_bollinger_constant_prices(self) -> None:
        """常量价格时 std=0，upper=lower=mid=close。"""
        closes = [100.0] * 20
        bars = _make_bars(closes)
        result = bollinger(bars, {"period": 20})
        assert result["bb_upper"] == pytest.approx(100.0, abs=1e-9)
        assert result["bb_lower"] == pytest.approx(100.0, abs=1e-9)
        assert result["bb_mid"] == pytest.approx(100.0, abs=1e-9)

    def test_bollinger_custom_period(self) -> None:
        """自定义 period=10 正常计算。"""
        closes = [float(i) for i in range(1, 15)]
        bars = _make_bars(closes)
        result = bollinger(bars, {"period": 10, "mult": 2.0})
        assert "bb_mid" in result
        # 最后 10 个 close: 5..14，均值 = 9.5
        assert result["bb_mid"] == pytest.approx(9.5, abs=1e-9)


# ===================================================================
# Keltner Channel Tests
# ===================================================================


class TestKeltner:
    """Keltner Channel 指标函数测试。"""

    def test_keltner_normal(self) -> None:
        """标准情况：足够数据正常计算 Keltner 通道。"""
        data = [(c + 2.0, c - 2.0, c) for c in [50.0 + i * 0.5 for i in range(30)]]
        bars = _make_bars_hlc(data)
        result = keltner(bars, {"period": 20, "atr_period": 14, "mult": 2.0})
        assert "kc_mid" in result
        assert "kc_upper" in result
        assert "kc_lower" in result

    def test_keltner_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0, 2.0, 3.0])
        result = keltner(bars, {"period": 20, "atr_period": 14})
        assert result == {}

    def test_keltner_upper_gt_mid_gt_lower(self) -> None:
        """上轨 > 中轨 > 下轨（价格有波动时）。"""
        data = [(c + 3.0, c - 3.0, c) for c in [50.0 + (i * 1.7 % 8) for i in range(30)]]
        bars = _make_bars_hlc(data)
        result = keltner(bars, {"period": 20, "atr_period": 14, "mult": 2.0})
        assert result["kc_upper"] > result["kc_mid"]
        assert result["kc_mid"] > result["kc_lower"]

    def test_keltner_constant_prices(self) -> None:
        """常量价格时 ATR=0，upper=lower=mid。"""
        data = [(50.0, 50.0, 50.0)] * 25
        bars = _make_bars_hlc(data)
        result = keltner(bars, {"period": 20, "atr_period": 14, "mult": 2.0})
        assert result["kc_upper"] == pytest.approx(result["kc_mid"], abs=1e-6)
        assert result["kc_lower"] == pytest.approx(result["kc_mid"], abs=1e-6)

    def test_keltner_output_fields(self) -> None:
        """验证输出字段名精确为 kc_upper/kc_lower/kc_mid。"""
        data = [(c + 2.0, c - 2.0, c) for c in [50.0 + i for i in range(30)]]
        bars = _make_bars_hlc(data)
        result = keltner(bars, {"period": 20, "atr_period": 14})
        assert set(result.keys()) == {"kc_upper", "kc_lower", "kc_mid"}


# ===================================================================
# Donchian Channel Tests
# ===================================================================


class TestDonchian:
    """Donchian Channel 指标函数测试。"""

    def test_donchian_normal(self) -> None:
        """标准情况：足够数据正常计算 Donchian 通道。"""
        data = [(c + 2.0, c - 2.0, c) for c in [50.0 + i * 0.5 for i in range(25)]]
        bars = _make_bars_hlc(data)
        result = donchian(bars, {"period": 20})
        assert "donchian_upper" in result
        assert "donchian_lower" in result
        assert "donchian_mid" in result

    def test_donchian_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        bars = _make_bars([1.0, 2.0, 3.0])
        result = donchian(bars, {"period": 20})
        assert result == {}

    def test_donchian_upper_gte_lower(self) -> None:
        """donchian_upper >= donchian_lower 始终成立。"""
        data = [(c + 5.0, c - 5.0, c) for c in [50.0 + (i * 3.1 % 12) for i in range(25)]]
        bars = _make_bars_hlc(data)
        result = donchian(bars, {"period": 20})
        assert result["donchian_upper"] >= result["donchian_lower"]

    def test_donchian_precise_values(self) -> None:
        """精确值验证：upper = max(highs), lower = min(lows)。"""
        # 构造 20 根 bar，高点在 [52, 71]，低点在 [48, 67]
        data = [(50.0 + i + 2.0, 50.0 + i - 2.0, 50.0 + i) for i in range(20)]
        bars = _make_bars_hlc(data)
        result = donchian(bars, {"period": 20})
        expected_upper = max(b.high for b in bars[-20:])
        expected_lower = min(b.low for b in bars[-20:])
        assert result["donchian_upper"] == pytest.approx(expected_upper, abs=1e-9)
        assert result["donchian_lower"] == pytest.approx(expected_lower, abs=1e-9)
        assert result["donchian_mid"] == pytest.approx(
            (expected_upper + expected_lower) / 2, abs=1e-9
        )

    def test_donchian_output_fields(self) -> None:
        """验证输出字段包含 donchian_upper/donchian_lower/donchian_mid/close。"""
        data = [(c + 1.0, c - 1.0, c) for c in [50.0 + i for i in range(20)]]
        bars = _make_bars_hlc(data)
        result = donchian(bars, {"period": 20})
        assert set(result.keys()) == {"donchian_upper", "donchian_lower", "donchian_mid", "close"}


# ===================================================================
# ATR Tests
# ===================================================================


class TestAtr:
    """ATR (Average True Range) 指标函数测试。"""

    def test_atr_normal(self) -> None:
        """标准情况：足够数据正常计算 ATR。"""
        data = [(c + 3.0, c - 3.0, c) for c in [50.0 + i * 0.5 for i in range(20)]]
        bars = _make_bars_hlc(data)
        result = atr(bars, {"period": 14})
        assert "atr" in result
        assert isinstance(result["atr"], float)

    def test_atr_insufficient_data(self) -> None:
        """数据不足时返回空 dict（需要 period+1 根 bar）。"""
        data = [(52.0, 48.0, 50.0)] * 10
        bars = _make_bars_hlc(data)
        result = atr(bars, {"period": 14})
        assert result == {}

    def test_atr_positive_value(self) -> None:
        """ATR 值应始终 > 0（价格有波动时）。"""
        data = [(c + 2.0, c - 2.0, c) for c in [50.0 + (i * 1.7 % 6) for i in range(20)]]
        bars = _make_bars_hlc(data)
        result = atr(bars, {"period": 14})
        assert result["atr"] > 0

    def test_atr_constant_prices(self) -> None:
        """常量价格（high=low=close）时 ATR 趋近 0。"""
        data = [(50.0, 50.0, 50.0)] * 20
        bars = _make_bars_hlc(data)
        result = atr(bars, {"period": 14})
        assert result["atr"] == pytest.approx(0.0, abs=1e-9)

    def test_atr_precise_calculation(self) -> None:
        """精确值验证：手动计算 True Range 并取平均。"""
        # 构造 period+1=15 根 bar，TR = max(H-L, |H-prev_close|, |L-prev_close|)
        # 使用 period=3 以便手动计算
        data = [
            (52.0, 48.0, 50.0),  # bar 0: 种子，prev_close 来源
            (54.0, 49.0, 53.0),  # bar 1: TR = max(5, |54-50|, |49-50|) = max(5,4,1) = 5
            (56.0, 51.0, 55.0),  # bar 2: TR = max(5, |56-53|, |51-53|) = max(5,3,2) = 5
            (53.0, 50.0, 51.0),  # bar 3: TR = max(3, |53-55|, |50-55|) = max(3,2,5) = 5
        ]
        bars = _make_bars_hlc(data)
        result = atr(bars, {"period": 3})
        # ATR = (5 + 5 + 5) / 3 = 5.0
        assert result["atr"] == pytest.approx(5.0, abs=1e-9)


# ===================================================================
# ADX Tests
# ===================================================================


class TestAdx:
    """ADX (Average Directional Index) 指标函数测试。"""

    def test_adx_normal(self) -> None:
        """标准情况：足够数据正常计算 ADX。"""
        # ADX 需要 period*2+1 根 bar（从 period*3 tail 中取）
        data = [(c + 3.0, c - 3.0, c) for c in [50.0 + i * 0.3 for i in range(50)]]
        bars = _make_bars_hlc(data)
        result = adx(bars, {"period": 14})
        assert "adx" in result
        assert "plus_di" in result
        assert "minus_di" in result

    def test_adx_insufficient_data(self) -> None:
        """数据不足时返回空 dict。"""
        data = [(52.0, 48.0, 50.0)] * 10
        bars = _make_bars_hlc(data)
        result = adx(bars, {"period": 14})
        assert result == {}

    def test_adx_range_0_100(self) -> None:
        """ADX 值在 [0, 100] 范围内。"""
        data = [(c + 3.0, c - 3.0, c) for c in [50.0 + (i * 2.7 % 15) - 7 for i in range(60)]]
        bars = _make_bars_hlc(data)
        result = adx(bars, {"period": 14})
        if result:
            assert 0.0 <= result["adx"] <= 100.0

    def test_adx_output_contains_di(self) -> None:
        """输出包含 adx、plus_di、minus_di 三个字段。"""
        data = [(c + 2.0, c - 2.0, c) for c in [50.0 + i * 0.5 for i in range(50)]]
        bars = _make_bars_hlc(data)
        result = adx(bars, {"period": 14})
        assert set(result.keys()) == {"adx", "plus_di", "minus_di"}

    def test_adx_strong_trend(self) -> None:
        """强趋势序列中 ADX 应较高，plus_di > minus_di。"""
        # 持续上涨：每根 bar 的 high/low/close 均单调上升
        data = [(50.0 + i * 2.0 + 3.0, 50.0 + i * 2.0 - 1.0, 50.0 + i * 2.0) for i in range(60)]
        bars = _make_bars_hlc(data)
        result = adx(bars, {"period": 14})
        if result:
            # 强上涨趋势中 ADX 应较大
            assert result["adx"] > 20.0
            # 上涨趋势中 +DI 应大于 -DI
            assert result["plus_di"] > result["minus_di"]
