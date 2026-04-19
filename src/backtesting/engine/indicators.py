from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from src.clients.mt5_market import OHLC
from src.signals.evaluation.regime import RegimeType

if TYPE_CHECKING:
    from .runner import BacktestEngine

logger = logging.getLogger(__name__)


def precompute_all_indicators(
    engine: "BacktestEngine",
    symbol: str,
    timeframe: str,
    all_bars: List[OHLC],
    warmup_count: int,
) -> List[Dict[str, Dict[str, Any]]]:
    """一次性预计算所有 bar 位置的指标快照，并注入 delta metrics。"""
    t0 = time.monotonic()
    snapshots: List[Dict[str, Dict[str, Any]]] = []
    for i in range(len(all_bars)):
        window_start = max(0, i - warmup_count)
        window = all_bars[window_start : i + 1]
        indicators = compute_indicators(engine, symbol, timeframe, window)
        snapshots.append(indicators)
    compute_elapsed = int((time.monotonic() - t0) * 1000)

    # 注入 delta metrics（与生产 UnifiedIndicatorManager 的行为对齐）
    # 生产路径：query_services/runtime.apply_delta_metrics_query 依赖 MarketService 历史
    # 回测路径：snapshots 自身就是完整历史，直接按索引回看前 N 根
    t1 = time.monotonic()
    delta_config = _build_delta_config()
    if delta_config:
        _apply_delta_to_snapshots(snapshots, delta_config)
    delta_elapsed = int((time.monotonic() - t1) * 1000)

    logger.info(
        "Pre-computed indicators for %d bars in %dms (delta metrics %dms, %d configs)",
        len(all_bars),
        compute_elapsed,
        delta_elapsed,
        len(delta_config),
    )
    return snapshots


def _build_delta_config() -> Dict[str, Tuple[int, ...]]:
    """从 global indicator config 读 delta_bars，构建 {indicator_name: (deltas)}。"""
    from src.config.indicator_config import get_global_config_manager

    configs = get_global_config_manager().get_config().indicators
    mapping: Dict[str, Tuple[int, ...]] = {}
    for cfg in configs or []:
        bars = getattr(cfg, "delta_bars", None)
        if not bars:
            continue
        values = tuple(sorted({int(item) for item in bars if int(item) > 0}))
        if values:
            mapping[cfg.name] = values
    return mapping


def _apply_delta_to_snapshots(
    snapshots: List[Dict[str, Dict[str, Any]]],
    delta_config: Dict[str, Tuple[int, ...]],
) -> None:
    """In-place 给每个 snapshot 添加 `{metric}_d{N}` 字段。

    与 src/indicators/runtime/delta_metrics.py 的 apply_delta_metrics 逻辑等价，
    但数据源来自 snapshots 列表而非 MarketService 历史。
    """
    for i, indicators in enumerate(snapshots):
        for ind_name, payload in indicators.items():
            deltas = delta_config.get(ind_name)
            if not deltas:
                continue
            for delta in deltas:
                prev_idx = i - delta
                if prev_idx < 0:
                    continue
                prev_payload = snapshots[prev_idx].get(ind_name)
                if prev_payload is None:
                    continue
                for metric, cur_val in list(payload.items()):
                    if not isinstance(cur_val, (int, float)):
                        continue
                    # 跳过已计算的 delta key，避免 rsi_d3_d3 这种递归
                    if "_d" in metric and metric.rsplit("_d", 1)[-1].isdigit():
                        continue
                    prev_val = prev_payload.get(metric)
                    if not isinstance(prev_val, (int, float)):
                        continue
                    payload[f"{metric}_d{delta}"] = round(
                        float(cur_val) - float(prev_val), 6
                    )


def compute_indicators(
    engine: "BacktestEngine",
    symbol: str,
    timeframe: str,
    bars: List[OHLC],
    indicator_names: Any = None,
    *,
    use_required_default: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """使用生产 Pipeline 计算指标。"""
    if len(bars) < 2:
        return {}
    names = engine._required_indicators if use_required_default else indicator_names
    try:
        return engine._pipeline.compute(symbol, timeframe, bars, names)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Indicator computation failed: %s", exc)
        return {}
    except Exception:
        logger.warning("Unexpected indicator computation error", exc_info=True)
        return {}


def detect_regime(
    engine: "BacktestEngine",
    indicators: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[RegimeType], Optional[Dict[str, Any]]]:
    """检测市场 Regime。异常时返回 (None, None) 跳过该 bar。"""
    try:
        regime = engine._regime_detector.detect(indicators)
    except Exception:
        logger.debug("Regime detection failed", exc_info=True)
        return None, None
    soft_regime_dict: Optional[Dict[str, Any]] = None
    try:
        soft_result = engine._regime_detector.detect_soft(indicators)
        if soft_result is not None:
            soft_regime_dict = soft_result.to_dict()
    except Exception:
        pass
    return regime, soft_regime_dict


def preload_htf_indicators(
    engine: "BacktestEngine",
    symbol: str,
    htf_timeframes: List[str],
    start_time: datetime,
    end_time: datetime,
    warmup_bars: int = 200,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """预加载高时间框架指标的时序数据，供逐 bar 查找。"""
    htf_data: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for tf in htf_timeframes:
        try:
            warmup = engine._data_loader.preload_warmup_bars(
                symbol, tf, start_time, warmup_bars
            )
            htf_bars = engine._data_loader.load_all_bars(
                symbol, tf, start_time, end_time
            )
            all_htf = warmup + htf_bars
            if len(all_htf) < 2:
                logger.warning(
                    "Backtest HTF: insufficient bars for %s/%s (%d)",
                    symbol,
                    tf,
                    len(all_htf),
                )
                continue
            ts_list: List[tuple[datetime, Dict[str, Dict[str, Any]]]] = []
            window_size = min(warmup_bars, len(all_htf))
            for end_idx in range(window_size, len(all_htf) + 1):
                window = all_htf[max(0, end_idx - window_size) : end_idx]
                snap = compute_indicators(
                    engine,
                    symbol,
                    tf,
                    window,
                    indicator_names=None,
                    use_required_default=False,
                )
                if snap:
                    ts_list.append((window[-1].time, snap))
            if ts_list:
                engine._htf_timeseries[tf] = ts_list
                engine._htf_time_indexes[tf] = [t[0] for t in ts_list]
                htf_data[tf] = ts_list[-1][1]
                logger.info(
                    "Backtest HTF: loaded %d bars for %s/%s, %d indicators",
                    len(all_htf),
                    symbol,
                    tf,
                    len(ts_list[-1][1]),
                )
        except Exception:
            logger.warning(
                "Backtest HTF: failed to load %s/%s", symbol, tf, exc_info=True
            )
    return htf_data


def lookup_htf_at_time(
    engine: "BacktestEngine", bar_time: datetime
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """按 bar 时间查找对应时点的 HTF 指标。"""
    from bisect import bisect_right

    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for tf, ts_list in engine._htf_timeseries.items():
        if not ts_list:
            continue
        times = engine._htf_time_indexes.get(tf)
        if times is None:
            continue
        idx = bisect_right(times, bar_time) - 1
        if idx >= 0:
            result[tf] = ts_list[idx][1]
    return result
