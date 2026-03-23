"""data_loader.py 单元测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from src.backtesting.data_loader import HistoricalDataLoader
from src.clients.mt5_market import OHLC


def _make_ohlc_row(
    time_val: datetime,
    close: float = 2000.0,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
) -> tuple:
    """生成模拟的 DB 行 (symbol, timeframe, open, high, low, close, volume, time, indicators)。"""
    return (
        symbol,
        timeframe,
        close - 1.0,  # open
        close + 1.0,  # high
        close - 2.0,  # low
        close,  # close
        100.0,  # volume
        time_val,
        None,  # indicators
    )


class TestHistoricalDataLoader:
    def test_load_all_bars(self) -> None:
        """测试一次性加载全部 bar。"""
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 2, tzinfo=timezone.utc)

        rows = [
            _make_ohlc_row(start + timedelta(minutes=5 * i), 2000.0 + i)
            for i in range(10)
        ]

        mock_repo = MagicMock()
        mock_repo.fetch_ohlc_range.return_value = rows

        loader = HistoricalDataLoader(mock_repo)
        bars = loader.load_all_bars("XAUUSD", "M5", start, end)

        assert len(bars) == 10
        assert isinstance(bars[0], OHLC)
        assert bars[0].symbol == "XAUUSD"
        mock_repo.fetch_ohlc_range.assert_called_once()

    def test_preload_warmup_bars(self) -> None:
        """测试 warmup bar 加载。"""
        start = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
        warmup_count = 50

        rows = [
            _make_ohlc_row(
                start - timedelta(minutes=5 * (warmup_count - i)),
                2000.0 + i,
            )
            for i in range(warmup_count)
        ]

        mock_repo = MagicMock()
        mock_repo.fetch_ohlc_before.return_value = rows

        loader = HistoricalDataLoader(mock_repo)
        bars = loader.preload_warmup_bars("XAUUSD", "M5", start, warmup_count)

        assert len(bars) == warmup_count
        # 所有 bar 应该在 start_time 之前
        for b in bars:
            assert b.time < start

    def test_load_bars_chunked(self) -> None:
        """测试分块加载。"""
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 2, tzinfo=timezone.utc)

        # 第一块：5 根 bar
        chunk1 = [
            _make_ohlc_row(start + timedelta(minutes=5 * i), 2000.0 + i)
            for i in range(5)
        ]
        # 第二块：3 根 bar（不足 chunk_size，表示最后一块）
        chunk2 = [
            _make_ohlc_row(start + timedelta(minutes=5 * (5 + i)), 2005.0 + i)
            for i in range(3)
        ]

        mock_repo = MagicMock()
        mock_repo.fetch_ohlc.side_effect = [chunk1, chunk2, []]

        loader = HistoricalDataLoader(mock_repo)
        all_chunks = list(loader.load_bars("XAUUSD", "M5", start, end, chunk_size=5))

        assert len(all_chunks) == 2
        assert len(all_chunks[0]) == 5
        assert len(all_chunks[1]) == 3

    def test_row_to_ohlc(self) -> None:
        """测试行转换。"""
        t = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        row = ("XAUUSD", "M5", 1999.0, 2001.0, 1998.0, 2000.0, 100.0, t, {"rsi14": {"rsi": 55.0}})
        ohlc = HistoricalDataLoader._row_to_ohlc(row)
        assert ohlc.symbol == "XAUUSD"
        assert ohlc.close == 2000.0
        assert ohlc.indicators == {"rsi14": {"rsi": 55.0}}
