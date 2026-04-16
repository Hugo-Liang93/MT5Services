"""OHLC 合成纯函数：从子 TF bars 合成父 TF bar。

被 BackgroundIngestor（实时合成）和 BacktestEngine（回测回放）共同复用，
保证两条链路的合成语义完全一致。
"""

from __future__ import annotations

from datetime import datetime
from typing import List

from src.clients.mt5_market import OHLC
from src.utils.common import timeframe_seconds


def synthesize_parent_bar(
    child_bars: List[OHLC],
    symbol: str,
    parent_tf: str,
    parent_bar_time: datetime,
) -> OHLC:
    """从子 TF confirmed bars 合成父 TF 当前 bar。

    Args:
        child_bars: 属于同一父 TF bar 区间的子 TF bars（必须非空，按时间升序）。
        symbol: 交易品种。
        parent_tf: 父 TF 名称（如 "H1"）。
        parent_bar_time: 父 TF bar 的开盘时间（向下对齐）。

    Returns:
        合成的父 TF OHLC bar。
    """
    if not child_bars:
        raise ValueError("child_bars must not be empty")
    return OHLC(
        symbol=symbol,
        timeframe=parent_tf,
        time=parent_bar_time,
        open=child_bars[0].open,
        high=max(b.high for b in child_bars),
        low=min(b.low for b in child_bars),
        close=child_bars[-1].close,
        volume=sum(b.volume for b in child_bars),
    )


def align_parent_bar_time(child_bar_time: datetime, parent_tf: str) -> datetime:
    """将子 TF bar 时间对齐到所属父 TF bar 的开盘时间。

    Args:
        child_bar_time: 子 TF bar 的时间戳。
        parent_tf: 父 TF 名称。

    Returns:
        父 TF bar 的开盘时间（UTC）。
    """
    parent_secs = timeframe_seconds(parent_tf)
    if parent_secs <= 0:
        raise ValueError(f"Invalid parent timeframe: {parent_tf}")
    ts = child_bar_time.timestamp()
    aligned_ts = ts - (ts % parent_secs)
    return datetime.fromtimestamp(aligned_ts, tz=child_bar_time.tzinfo)


def build_child_bar_index(
    child_bars: List[OHLC],
    parent_tf: str,
) -> dict[datetime, List[OHLC]]:
    """将子 TF bars 按父 TF bar 开盘时间分组（预构建时间索引）。

    Args:
        child_bars: 全部子 TF bars（按时间升序）。
        parent_tf: 父 TF 名称。

    Returns:
        {parent_bar_time: [child_bars_in_this_parent_bar]}，每组内保持时间升序。
    """
    index: dict[datetime, List[OHLC]] = {}
    for bar in child_bars:
        parent_time = align_parent_bar_time(bar.time, parent_tf)
        index.setdefault(parent_time, []).append(bar)
    return index
