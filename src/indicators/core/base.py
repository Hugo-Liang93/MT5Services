import math
from typing import Any, Dict, Iterable, List, Sequence

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
