import math
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import numpy as np

from src.clients.mt5_market import OHLC


def sanitize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """移除结果中的 NaN/Inf 值，防止级联传播到策略和下单。

    如果所有数值字段都无效，返回空 dict（等同于指标计算失败）。
    """
    cleaned: Dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, float) and not math.isfinite(value):
            continue
        cleaned[key] = value
    # 至少保留一个数值字段才算有效结果
    has_numeric = any(isinstance(v, (int, float)) for v in cleaned.values())
    return cleaned if has_numeric else {}


def tail_bars(bars: Iterable[OHLC], lookback: int) -> List[OHLC]:
    """Return last N bars, keeping original order."""
    if lookback <= 0:
        return []
    if isinstance(bars, list):
        return bars[-lookback:]
    seq = list(bars)
    return seq[-lookback:]


def get_closes(bars: Iterable[OHLC], lookback: int) -> List[float]:
    """Helper for close price slices."""
    return [bar.close for bar in tail_bars(bars, lookback)]


def get_int(params: dict, key: str, default: int, aliases: Sequence[str] = ()) -> int:
    """Fetch int param with aliases and >=1 guard."""
    for k in (key, *aliases):
        if k in params:
            try:
                val = int(params[k])
                return val if val > 0 else default
            except Exception:
                continue
    return default


def get_float(params: dict, key: str, default: float, aliases: Sequence[str] = ()) -> float:
    """Fetch float param with aliases."""
    for k in (key, *aliases):
        if k in params:
            try:
                return float(params[k])
            except Exception:
                continue
    return default


# ---------------------------------------------------------------------------
# NumPy 向量化辅助函数
# ---------------------------------------------------------------------------


def get_closes_array(bars: Iterable[OHLC], lookback: int) -> np.ndarray:
    """返回 close 价格的 float64 numpy 数组。"""
    return np.array(get_closes(bars, lookback), dtype=np.float64)


def get_hlc_arrays(
    bars: List[OHLC], lookback: int = 0
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """一次遍历提取 high/low/close 三个 numpy 数组。

    Returns:
        (highs, lows, closes) — 均为 float64 ndarray
    """
    window = bars[-lookback:] if lookback > 0 else bars
    n = len(window)
    highs = np.empty(n, dtype=np.float64)
    lows = np.empty(n, dtype=np.float64)
    closes = np.empty(n, dtype=np.float64)
    for i, bar in enumerate(window):
        highs[i] = bar.high
        lows[i] = bar.low
        closes[i] = bar.close
    return highs, lows, closes


def get_hlcv_arrays(
    bars: List[OHLC], lookback: int = 0
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """一次遍历提取 high/low/close/volume 四个 numpy 数组。

    Returns:
        (highs, lows, closes, volumes) — 均为 float64 ndarray
    """
    window = bars[-lookback:] if lookback > 0 else bars
    n = len(window)
    highs = np.empty(n, dtype=np.float64)
    lows = np.empty(n, dtype=np.float64)
    closes = np.empty(n, dtype=np.float64)
    volumes = np.empty(n, dtype=np.float64)
    for i, bar in enumerate(window):
        highs[i] = bar.high
        lows[i] = bar.low
        closes[i] = bar.close
        volumes[i] = float(bar.volume or 0.0)
    return highs, lows, closes, volumes
