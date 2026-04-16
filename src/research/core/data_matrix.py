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
from src.research.core.barrier import (
    DEFAULT_BARRIER_CONFIGS,
    BarrierConfig,
    BarrierOutcome,
    compute_barrier_returns,
)
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

    # Triple-Barrier forward_return（F-12a）——替代朴素 N-bar forward_return。
    # key = (sl_atr, tp_atr, time_bars) 元组；value = 对齐 bar 的 outcome 序列。
    # 同时提供 long 和 short 两个方向，供分析器按信号方向消费。
    barrier_returns_long: Dict[tuple, List[Optional[BarrierOutcome]]] = field(
        default_factory=dict
    )
    barrier_returns_short: Dict[tuple, List[Optional[BarrierOutcome]]] = field(
        default_factory=dict
    )
    barrier_configs: tuple[BarrierConfig, ...] = ()

    # 经济事件时间线（UTC datetime 升序），供 bars_to/since_high_impact 特征使用。
    # 仅包含 importance >= high_impact_threshold 的事件。
    high_impact_event_times: Tuple[datetime, ...] = ()

    # 每个 forward_horizon 对应的 ACF 自适应 block_size（供排列检验复用，避免重算）。
    # key = horizon_bars；value = auto_block_size(forward_returns[horizon]) 结果。
    forward_return_block_sizes: Dict[int, int] = field(default_factory=dict)

    # Intrabar 子 TF bars：{bar_index: [该父 bar 区间内的子 TF bars]}
    # 供 intrabar 派生特征使用（child_bar_consensus / range_acceleration 等）。
    # 空 dict 表示未加载子 TF 数据。
    child_bars: Dict[int, List[Any]] = field(default_factory=dict)
    # 子 TF 名称（如 "M5"），空字符串表示无子 TF 数据
    child_tf: str = ""

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
    barrier_configs: Optional[tuple[BarrierConfig, ...]] = None,
    high_impact_event_times: Optional[Tuple[datetime, ...]] = None,
    child_tf: str = "",
    child_coverage_min: float = 0.80,
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
    required_indicators = [ind_cfg.name for ind_cfg in indicator_config.indicators]

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

    # Train/test 分割（加 gap 防止 forward_return + barrier outcome 信息泄露）
    #
    # 泄漏分析（train 最后一个样本 i = train_end_idx - 1）：
    #   forward_return[h]: 用到 closes[i + h] = closes[train_end_idx - 1 + h]
    #     → 需 split_idx > train_end_idx - 1 + max_forward
    #     → split_idx >= train_end_idx + max_forward
    #   barrier_return: entry_idx = i + 1, 扫描 [i+2, i+1+time_bars]
    #     → 最远访问 closes[train_end_idx + time_bars]
    #     → 需 split_idx > train_end_idx + max_barrier_time
    #     → split_idx >= train_end_idx + max_barrier_time + 1  (barrier 的 +1 偏移)
    #
    # 因此 gap 必须 ≥ max(max_forward, max_barrier_time + 1)。
    max_forward = max(forward_horizons) if forward_horizons else 1
    effective_barriers_for_gap: tuple[BarrierConfig, ...] = (
        barrier_configs if barrier_configs is not None else DEFAULT_BARRIER_CONFIGS
    )
    max_barrier_time = max(
        (cfg.time_bars for cfg in effective_barriers_for_gap), default=0
    )
    max_horizon = max(max_forward, max_barrier_time + 1)
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

    # Triple-Barrier forward_return（F-12a）：对每个 bar 同时计算 long/short 两方向。
    effective_barriers: tuple[BarrierConfig, ...] = effective_barriers_for_gap
    opens_list = [bar.open for bar in test_bars]
    highs_list = [bar.high for bar in test_bars]
    lows_list = [bar.low for bar in test_bars]
    barrier_returns_long = compute_barrier_returns(
        opens=opens_list,
        highs=highs_list,
        lows=lows_list,
        closes=closes,
        indicators=test_indicators,
        configs=effective_barriers,
        direction="long",
        round_trip_cost_pct=round_trip_cost_pct,
    )
    barrier_returns_short = compute_barrier_returns(
        opens=opens_list,
        highs=highs_list,
        lows=lows_list,
        closes=closes,
        indicators=test_indicators,
        configs=effective_barriers,
        direction="short",
        round_trip_cost_pct=round_trip_cost_pct,
    )

    # 预计算 ACF 自适应 block_size：每 forward_horizon 算一次，避免分析器重算
    from .statistics import auto_block_size as _auto_block_size

    forward_return_block_sizes: Dict[int, int] = {}
    for h, returns in forward_returns.items():
        valid = [r for r in returns if r is not None]
        if valid:
            forward_return_block_sizes[h] = _auto_block_size(valid)

    # 加载子 TF bars（供 intrabar 派生特征使用）
    child_bars_map: Dict[int, List[Any]] = {}
    effective_child_tf = ""
    if child_tf:
        try:
            from src.market.synthesis import build_child_bar_index
            from src.utils.common import timeframe_seconds

            all_child = data_loader.load_child_bars(
                symbol, child_tf, start_time, end_time
            )
            if all_child:
                child_index = build_child_bar_index(all_child, timeframe)
                parent_secs = timeframe_seconds(timeframe)
                child_secs = timeframe_seconds(child_tf)
                expected = parent_secs // child_secs if child_secs > 0 else 0
                for i, bar in enumerate(test_bars):
                    children = child_index.get(bar.time, [])
                    if expected > 0 and len(children) / expected < child_coverage_min:
                        continue
                    child_bars_map[i] = children
                effective_child_tf = child_tf
                logger.info(
                    "DataMatrix: loaded %d child bars (%s), "
                    "%d/%d parent bars with sufficient coverage",
                    len(all_child),
                    child_tf,
                    len(child_bars_map),
                    n,
                )
        except Exception:
            logger.warning(
                "DataMatrix: child TF %s loading failed", child_tf, exc_info=True
            )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "DataMatrix built: %d bars, %d indicators, %d fields, "
        "split=%d/gap=%d/test=%d, barrier_configs=%d, %dms",
        n,
        len(required_indicators),
        len(indicator_series),
        train_end_idx,
        split_idx - train_end_idx,
        n - split_idx,
        len(effective_barriers),
        elapsed_ms,
    )

    return DataMatrix(
        symbol=symbol,
        timeframe=timeframe,
        n_bars=n,
        bar_times=[bar.time for bar in test_bars],
        opens=opens_list,
        highs=highs_list,
        lows=lows_list,
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
        barrier_returns_long=barrier_returns_long,
        barrier_returns_short=barrier_returns_short,
        barrier_configs=effective_barriers,
        high_impact_event_times=tuple(high_impact_event_times or ()),
        forward_return_block_sizes=forward_return_block_sizes,
        child_bars=child_bars_map,
        child_tf=effective_child_tf,
    )
