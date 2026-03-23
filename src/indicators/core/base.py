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
# NumPy 向量化辅助函数 + 同批次 bars 数组缓存
# ---------------------------------------------------------------------------

# 轻量缓存：同一 pipeline 批次内 21 个指标共享提取结果。
# 以 (id(bars), len(bars), last_bar_fingerprint) 为键，仅缓存最近一次提取。
_bars_array_cache: Dict[str, Any] = {
    "bars_id": -1, "bars_len": -1, "bars_fp": None, "hlcv": None,
}


def _get_full_hlcv(bars: List[OHLC]) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """提取全量 bars 的 HLCV 数组（带同批次缓存）。

    同一 pipeline 批次中，bars 是同一个 list 对象（id 相同），
    21 个指标调用此函数只会遍历 bars 一次。
    """
    bars_id = id(bars)
    bars_len = len(bars)
    # 指纹：首尾 bar 的 (time, high, low, close) 防止 id 回收误命中
    last = bars[-1] if bars else None
    first = bars[0] if bars else None
    fp = (
        getattr(first, "time", None), getattr(first, "high", None),
        getattr(last, "time", None), getattr(last, "high", None),
        getattr(last, "low", None), getattr(last, "close", None),
    ) if last else None
    cache = _bars_array_cache
    if (cache["bars_id"] == bars_id
            and cache["bars_len"] == bars_len
            and cache["bars_fp"] == fp):
        return cache["hlcv"]  # type: ignore[return-value]

    n = bars_len
    highs = np.empty(n, dtype=np.float64)
    lows = np.empty(n, dtype=np.float64)
    closes = np.empty(n, dtype=np.float64)
    volumes = np.empty(n, dtype=np.float64)
    for i, bar in enumerate(bars):
        highs[i] = bar.high
        lows[i] = bar.low
        closes[i] = bar.close
        volumes[i] = float(getattr(bar, "volume", None) or 0.0)

    result = (highs, lows, closes, volumes)
    cache["bars_id"] = bars_id
    cache["bars_len"] = bars_len
    cache["bars_fp"] = fp
    cache["hlcv"] = result
    return result


def get_closes_array(bars: Iterable[OHLC], lookback: int) -> np.ndarray:
    """返回 close 价格的 float64 numpy 数组。"""
    if isinstance(bars, list):
        _, _, closes, _ = _get_full_hlcv(bars)
        return closes[-lookback:].copy() if lookback > 0 else closes.copy()
    return np.array(get_closes(bars, lookback), dtype=np.float64)


def get_hlc_arrays(
    bars: List[OHLC], lookback: int = 0
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """一次遍历提取 high/low/close 三个 numpy 数组。

    Returns:
        (highs, lows, closes) — 均为 float64 ndarray
    """
    if isinstance(bars, list):
        highs, lows, closes, _ = _get_full_hlcv(bars)
        if lookback > 0:
            return highs[-lookback:], lows[-lookback:], closes[-lookback:]
        return highs, lows, closes
    # fallback for non-list iterables
    window = list(bars)[-lookback:] if lookback > 0 else list(bars)
    n = len(window)
    h = np.empty(n, dtype=np.float64)
    l = np.empty(n, dtype=np.float64)
    c = np.empty(n, dtype=np.float64)
    for i, bar in enumerate(window):
        h[i] = bar.high
        l[i] = bar.low
        c[i] = bar.close
    return h, l, c


def get_hlcv_arrays(
    bars: List[OHLC], lookback: int = 0
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """一次遍历提取 high/low/close/volume 四个 numpy 数组。

    Returns:
        (highs, lows, closes, volumes) — 均为 float64 ndarray
    """
    if isinstance(bars, list):
        highs, lows, closes, volumes = _get_full_hlcv(bars)
        if lookback > 0:
            return highs[-lookback:], lows[-lookback:], closes[-lookback:], volumes[-lookback:]
        return highs, lows, closes, volumes
    # fallback for non-list iterables
    window = list(bars)[-lookback:] if lookback > 0 else list(bars)
    n = len(window)
    h = np.empty(n, dtype=np.float64)
    l_ = np.empty(n, dtype=np.float64)
    c = np.empty(n, dtype=np.float64)
    v = np.empty(n, dtype=np.float64)
    for i, bar in enumerate(window):
        h[i] = bar.high
        l_[i] = bar.low
        c[i] = bar.close
        v[i] = float(bar.volume or 0.0)
    return h, l_, c, v
