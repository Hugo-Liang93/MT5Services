"""backfill — generic source-agnostic backfill CLI.

Pulls daily OHLCV via any registered ExternalDataSource, writes to
daily_external_ohlc. Per-symbol failures don't abort the run.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from src.research.external import DailyBar, register_source
from src.research.external.backfill import (
    backfill_symbols,
    parse_args,
)


def test_parse_args_requires_source_and_symbols() -> None:
    args = parse_args(
        ["--environment", "live", "--source", "yfinance", "--symbols", "GC=F"]
    )
    assert args.environment == "live"
    assert args.source == "yfinance"
    assert args.symbols == ["GC=F"]


def test_parse_args_supports_multi_symbols_csv() -> None:
    args = parse_args(
        [
            "--environment", "live",
            "--source", "yfinance",
            "--symbols", "GC=F,DX-Y.NYB,^TNX,^GSPC",
        ]
    )
    assert args.symbols == ["GC=F", "DX-Y.NYB", "^TNX", "^GSPC"]


def test_parse_args_default_three_year_window() -> None:
    args = parse_args(
        ["--environment", "live", "--source", "yfinance", "--symbols", "GC=F"]
    )
    assert (date.today() - args.start).days >= 365 * 3 - 1


def test_backfill_symbols_returns_per_symbol_counts() -> None:
    fake_writer = MagicMock()
    fake_source = MagicMock()
    fake_source.fetch_daily.side_effect = lambda sym, **kw: [
        DailyBar(sym, date(2026, 4, 1), 100, 101, 99, 100.5, 0.0)
    ]

    counts = backfill_symbols(
        source=fake_source,
        writer=fake_writer,
        symbols=["GC=F", "DX-Y.NYB"],
        start=date(2026, 4, 1),
        end=date(2026, 4, 1),
    )

    assert counts == {"GC=F": 1, "DX-Y.NYB": 1}
    assert fake_source.fetch_daily.call_count == 2
    assert fake_writer.execute.call_count == 2


def test_backfill_symbols_continues_after_per_symbol_failure() -> None:
    """If yfinance throws YFinanceError for one symbol, others still succeed."""
    from src.research.external.yfinance_client import YFinanceError

    fake_writer = MagicMock()
    fake_source = MagicMock()

    def fake_fetch(sym, **kw):
        if sym == "BAD":
            raise YFinanceError("no data")
        return [DailyBar(sym, date(2026, 4, 1), 100, 101, 99, 100.5, 0.0)]

    fake_source.fetch_daily.side_effect = fake_fetch

    counts = backfill_symbols(
        source=fake_source,
        writer=fake_writer,
        symbols=["BAD", "GOOD"],
        start=date(2026, 4, 1),
        end=date(2026, 4, 1),
    )

    assert counts == {"BAD": 0, "GOOD": 1}
