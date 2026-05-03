"""Generic source-agnostic backfill CLI.

Pulls daily OHLCV from any registered ExternalDataSource and writes to
daily_external_ohlc. Per-symbol failures (UnknownSourceError, YFinanceError,
etc.) don't abort — count is recorded as 0 and the run continues.

Usage:
    # Phase 1 — CME volume
    python -m src.research.external.backfill --environment live \\
        --source yfinance --symbols GC=F --start 2023-01-01

    # Phase 2 — cross-asset (same CLI)
    python -m src.research.external.backfill --environment live \\
        --source yfinance --symbols DX-Y.NYB,^TNX,^GSPC --start 2023-01-01

    # Future FRED — same CLI, different source
    python -m src.research.external.backfill --environment live \\
        --source fred --symbols DGS10,DTWEXBGS --start 2023-01-01

Idempotent: daily_external_ohlc has ON CONFLICT (symbol, date) DO UPDATE.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Sequence

from src.persistence.schema.daily_external_ohlc import INSERT_SQL
from src.research.external import DailyBar, get_source

logger = logging.getLogger(__name__)


@dataclass
class _CliArgs:
    environment: str
    source: str
    symbols: List[str]
    start: date
    end: date


def parse_args(argv: Sequence[str] | None = None) -> _CliArgs:
    parser = argparse.ArgumentParser(prog="backfill")
    parser.add_argument("--environment", required=True, choices=("live", "demo"))
    parser.add_argument(
        "--source",
        required=True,
        help="Registered ExternalDataSource name (e.g. yfinance)",
    )
    parser.add_argument(
        "--symbols",
        required=True,
        type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
        help="Comma-separated symbol list (source-specific format)",
    )
    parser.add_argument(
        "--start",
        type=lambda s: date.fromisoformat(s),
        default=date.today() - timedelta(days=365 * 3),
    )
    parser.add_argument(
        "--end",
        type=lambda s: date.fromisoformat(s),
        default=date.today(),
    )
    ns = parser.parse_args(argv)
    return _CliArgs(
        environment=ns.environment,
        source=ns.source,
        symbols=list(ns.symbols),
        start=ns.start,
        end=ns.end,
    )


def backfill_symbols(
    *,
    source: Any,
    writer: Any,
    symbols: List[str],
    start: date,
    end: date,
) -> Dict[str, int]:
    """Fetch from `source`, write each DailyBar via `writer.execute(INSERT_SQL, ...)`.

    Returns per-symbol row counts. A failed symbol records 0 and logs the error.
    """
    counts: Dict[str, int] = {}
    for sym in symbols:
        try:
            bars: List[DailyBar] = source.fetch_daily(sym, start=start, end=end)
        except Exception as exc:
            logger.warning("backfill: %s failed: %s", sym, exc)
            counts[sym] = 0
            continue
        for bar in bars:
            writer.execute(
                INSERT_SQL,
                (
                    bar.symbol,
                    bar.date,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                ),
            )
        counts[sym] = len(bars)
        logger.info("backfill: %s %d rows", sym, len(bars))
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = parse_args(argv)

    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter

    settings = load_db_settings(args.environment)
    db_writer = TimescaleWriter(settings)

    source = get_source(args.source)  # raises UnknownSourceError if --source bad
    with db_writer.connection() as conn, conn.cursor() as cur:
        counts = backfill_symbols(
            source=source,
            writer=cur,
            symbols=args.symbols,
            start=args.start,
            end=args.end,
        )
        conn.commit()
    print("backfilled:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
