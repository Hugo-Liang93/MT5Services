"""DataMatrix — 所有分析器的统一输入数据结构。

一次预计算 bar×指标×regime×前瞻收益，多个分析器复用。
构建流程 100% 复用现有基础设施（build_backtest_components）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.clients.mt5_market import OHLC
from src.signals.evaluation.regime import RegimeType, SoftRegimeResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataMatrix:
    """对齐的 bar×指标×regime×前瞻收益数据矩阵。

    所有数组按 bar 索引对齐：bars[i] 对应 indicators[i]、regimes[i] 等。
    仅包含测试 bar（warmup bar 已被指标计算消耗）。
    """

    symbol: str
    timeframe: str
    n_bars: int

    bar_times: List[datetime]
    opens: List[float]
    highs: List[float]
    lows: List[float]
    closes: List[float]
    volumes: List[float]

    # 指标快照: [n_bars] 每项 = {indicator_name: {field: value}}
    indicators: List[Dict[str, Dict[str, Any]]]

    # Regime: [n_bars]
    regimes: List[RegimeType]
    soft_regimes: List[Optional[Dict[str, float]]]

    # 前瞻收益: {horizon_bars: [n_bars 个 Optional[float]]}
    forward_returns: Dict[int, List[Optional[float]]]

    # 扁平化指标序列: {("rsi14","rsi"): [n_bars 个 Optional[float]]}
    indicator_series: Dict[Tuple[str, str], List[Optional[float]]]

    # ── 以下字段均有默认值 ──────────────────────────────────────

    # Train/test 分割:
    #   train: [0, train_end_idx)
    #   gap:   [train_end_idx, split_idx)  — 排除区间，防 forward_return 泄露
    #   test:  [split_idx, n_bars)
    train_end_idx: int = 0
    split_idx: int = 0

    # MFE/MAE: {horizon_bars: [n_bars 个 Optional[Tuple[mfe, mae]]]}
    forward_mfe_mae: Dict[int, List[Optional[Tuple[float, float]]]] = field(
        default_factory=dict
    )

    # Session 标签: [n_bars]，"asia" / "london" / "new_york" / "off_hours"
    sessions: List[str] = field(default_factory=list)

    def train_slice(self) -> range:
        """训练集索引范围（不含 gap）。"""
        return range(0, self.train_end_idx)

    def test_slice(self) -> range:
        """测试集索引范围（gap 之后）。"""
        return range(self.split_idx, self.n_bars)

    def available_indicator_fields(self) -> List[Tuple[str, str]]:
        """返回所有可用的 (indicator_name, field_name) 组合。"""
        return list(self.indicator_series.keys())


def build_data_matrix(
    symbol: str,
    timeframe: str,
    start_time: datetime,
    end_time: datetime,
    forward_horizons: List[int],
    warmup_bars: int = 200,
    train_ratio: float = 0.70,
    round_trip_cost_pct: float = 0.0,
    components: Optional[Dict[str, Any]] = None,
) -> DataMatrix:
    """构建 DataMatrix。

    Args:
        symbol: 交易品种
        timeframe: 时间框架
        start_time: 数据起始时间
        end_time: 数据结束时间
        forward_horizons: 前瞻收益的 bar 数列表
        warmup_bars: 指标预热 bar 数
        train_ratio: 训练集比例
        components: 预构建的组件字典（来自 build_backtest_components），
                    为 None 则自动构建

    Returns:
        DataMatrix 实例
    """
    t0 = time.monotonic()

    if components is None:
        from src.backtesting.component_factory import build_backtest_components

        components = build_backtest_components()

    data_loader = components["data_loader"]
    pipeline = components["pipeline"]
    regime_detector = components["regime_detector"]

    # 加载数据
    warmup = data_loader.preload_warmup_bars(symbol, timeframe, start_time, warmup_bars)
    test_bars = data_loader.load_all_bars(symbol, timeframe, start_time, end_time)

    if not test_bars:
        raise ValueError(
            f"No bars loaded for {symbol}/{timeframe} "
            f"between {start_time} and {end_time}"
        )

    all_bars: List[OHLC] = warmup + test_bars
    warmup_count = len(warmup)
    # 滑动窗口大小：优先用实际 warmup 数量，不足时用 warmup_bars 参数
    # （当 DB 中 start_time 之前无数据时，warmup_count=0，需用 test bars 自身回看）
    window_size = max(warmup_count, warmup_bars)

    logger.info(
        "Loaded %d warmup + %d test bars for %s/%s (window_size=%d)",
        warmup_count,
        len(test_bars),
        symbol,
        timeframe,
        window_size,
    )

    # 收集所有启用的指标名
    from src.config.indicator_config import get_global_config_manager

    config_manager = get_global_config_manager()
    indicator_config = config_manager.get_config()
    required_indicators = [
        ind_cfg.name for ind_cfg in indicator_config.indicators
    ]

    # 预计算指标（复用回测的 pipeline.compute）
    indicator_snapshots: List[Dict[str, Dict[str, Any]]] = []
    for i in range(len(all_bars)):
        window_start = max(0, i - window_size)
        window = all_bars[window_start : i + 1]
        if len(window) < 2:
            indicator_snapshots.append({})
            continue
        try:
            snap = pipeline.compute(symbol, timeframe, window, required_indicators)
        except Exception:
            snap = {}
        indicator_snapshots.append(snap)

    # 截取有效区间：跳过前 window_size 个 bar（指标预热不足）
    # 当 warmup_count >= warmup_bars 时：skip = warmup_count（仅跳过 warmup bar）
    # 当 warmup_count < warmup_bars 时：skip = warmup_bars（借用 test bar 做预热）
    skip_count = max(warmup_count, warmup_bars)
    test_indicators = indicator_snapshots[skip_count:]
    test_bars = test_bars[skip_count - warmup_count :]

    # 检测 Regime
    regimes: List[RegimeType] = []
    soft_regimes_list: List[Optional[Dict[str, float]]] = []
    for snap in test_indicators:
        try:
            regime = regime_detector.detect(snap)
        except Exception:
            regime = RegimeType.UNCERTAIN
        regimes.append(regime)

        try:
            soft = regime_detector.detect_soft(snap)
            soft_dict = (
                {r.value: p for r, p in soft.probabilities.items()}
                if soft is not None
                else None
            )
        except Exception:
            soft_dict = None
        soft_regimes_list.append(soft_dict)

    # 计算前瞻收益
    # 使用 next-bar open 作为入场价（避免 close-to-close 前瞻偏差）
    # forward_return = (close[i+h] - open[i+1]) / open[i+1]
    n = len(test_bars)
    closes = [bar.close for bar in test_bars]
    opens = [bar.open for bar in test_bars]
    forward_returns: Dict[int, List[Optional[float]]] = {}
    for h in forward_horizons:
        returns: List[Optional[float]] = []
        for i in range(n):
            # 入场价 = 下一根 bar 的 open；出场价 = i+h 的 close
            if i + 1 < n and i + h < n and opens[i + 1] > 0:
                entry_price = opens[i + 1]
                exit_price = closes[i + h]
                raw_return = (exit_price - entry_price) / entry_price
                # 扣除单次往返交易成本
                cost = round_trip_cost_pct / 100.0 if round_trip_cost_pct > 0 else 0.0
                returns.append(raw_return - cost)
            else:
                returns.append(None)
        forward_returns[h] = returns

    # 计算 MFE/MAE（最大顺向/逆向偏移）
    highs = [bar.high for bar in test_bars]
    lows = [bar.low for bar in test_bars]
    forward_mfe_mae: Dict[int, List[Optional[Tuple[float, float]]]] = {}
    for h in forward_horizons:
        mfe_mae_list: List[Optional[Tuple[float, float]]] = []
        for i in range(n):
            if i + 1 >= n or i + h >= n or opens[i + 1] <= 0:
                mfe_mae_list.append(None)
                continue
            entry = opens[i + 1]
            # 在 [i+1, i+h] 窗口内的 highest high 和 lowest low
            window_high = max(highs[j] for j in range(i + 1, min(i + h + 1, n)))
            window_low = min(lows[j] for j in range(i + 1, min(i + h + 1, n)))
            mfe = (window_high - entry) / entry  # 多头 MFE
            mae = (entry - window_low) / entry  # 多头 MAE
            mfe_mae_list.append((mfe, mae))
        forward_mfe_mae[h] = mfe_mae_list

    # 构建扁平化指标序列
    indicator_series: Dict[Tuple[str, str], List[Optional[float]]] = {}
    # 先收集所有出现过的 (indicator, field) 组合
    all_keys: set[Tuple[str, str]] = set()
    for snap in test_indicators:
        for ind_name, fields in snap.items():
            if isinstance(fields, dict):
                for field_name, val in fields.items():
                    if isinstance(val, (int, float)):
                        all_keys.add((ind_name, field_name))

    for key in sorted(all_keys):
        series: List[Optional[float]] = []
        ind_name, field_name = key
        for snap in test_indicators:
            ind_data = snap.get(ind_name)
            if ind_data and isinstance(ind_data, dict):
                val = ind_data.get(field_name)
                if isinstance(val, (int, float)):
                    series.append(float(val))
                else:
                    series.append(None)
            else:
                series.append(None)
        indicator_series[key] = series

    # Train/test 分割（加 gap 防止 forward_return 信息泄露）
    max_horizon = max(forward_horizons) if forward_horizons else 1
    train_end_idx = max(1, int(n * train_ratio))
    # train: [0, train_end_idx), gap: [train_end_idx, split_idx), test: [split_idx, n)
    split_idx = min(train_end_idx + max_horizon, n - 1)

    # Session 标签（基于 bar UTC 时间）
    sessions: List[str] = []
    for bar in test_bars:
        h = bar.time.hour
        if 0 <= h < 7:
            sessions.append("asia")
        elif 7 <= h < 13:
            sessions.append("london")
        elif 13 <= h < 21:
            sessions.append("new_york")
        else:
            sessions.append("off_hours")

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "DataMatrix built: %d bars, %d indicators, %d fields, "
        "split=%d/gap=%d/test=%d, %dms",
        n,
        len(required_indicators),
        len(indicator_series),
        train_end_idx,
        split_idx - train_end_idx,
        n - split_idx,
        elapsed_ms,
    )

    return DataMatrix(
        symbol=symbol,
        timeframe=timeframe,
        n_bars=n,
        bar_times=[bar.time for bar in test_bars],
        opens=[bar.open for bar in test_bars],
        highs=[bar.high for bar in test_bars],
        lows=[bar.low for bar in test_bars],
        closes=closes,
        volumes=[bar.volume for bar in test_bars],
        indicators=test_indicators,
        regimes=regimes,
        soft_regimes=soft_regimes_list,
        forward_returns=forward_returns,
        indicator_series=indicator_series,
        train_end_idx=train_end_idx,
        split_idx=split_idx,
        forward_mfe_mae=forward_mfe_mae,
        sessions=sessions,
    )
