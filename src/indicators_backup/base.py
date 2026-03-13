from typing import Iterable, List, Any, Sequence

from src.clients.mt5_market import OHLC


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
