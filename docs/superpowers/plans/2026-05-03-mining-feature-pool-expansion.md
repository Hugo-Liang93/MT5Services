# Mining Feature Pool Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Break the mining pipeline out of "no informational edge" by adding two external data sources (CME Gold Futures real volume, cross-asset DXY/10Y/SPX), then validate the full ML pipeline (state_edge → entry_meta) end-to-end on the expanded feature pool.

**Architecture:** Three-phase incremental data + algorithmic improvement:
- **Phase 0** (1 day): Validate ML pipeline works end-to-end on existing 121 features. If state_edge can't produce a passable artifact on current features, no point adding more — go fix the pipeline first.
- **Phase 1** (5-7 days): Add `CMEVolumeFeatureProvider` reading daily CME GC futures volume (proxy for "real" XAUUSD volume). New schema, new ingestor, new provider, new tests, mining re-run.
- **Phase 2** (7-14 days): Add cross-asset ingestor for DXY (US dollar index), 10Y yield, SPX (S&P 500). New `CrossAssetFeatureProvider`. These have well-documented correlation with XAUUSD (negative DXY correlation, real-yield inverse, risk-on/off).
- **Phase 3** (3-5 days): Re-run mining + state_edge + entry_meta with the expanded ~150-feature pool. Compare new vs baseline. Decision gate: do new artifacts pass `state_edge_overlay_report` non-degradation?

**Tech Stack:**
- Python 3.10-3.12, pandas, numpy
- TimescaleDB hypertable (`daily_external_ohlc` new table)
- yfinance (new dependency, free Yahoo Finance API for daily OHLCV) OR pandas_datareader as fallback
- Existing: `FeatureProvider` Protocol (`src/research/features/protocol.py`), `FeatureHub` (`src/research/features/hub.py`), `DataMatrix.indicator_series` (`src/research/core/data_matrix.py:130`)
- Existing tools: `src.ops.cli.mining_runner`, `src.ops.cli.state_edge_lab`, `src.ops.cli.entry_meta_lab`, `src.ops.cli.backtest_runner`

**Scope notes:**
- Each phase produces a self-contained, testable deliverable. If Phase 0 reveals the ML pipeline is broken, halt and fix before Phase 1.
- Phase 1 and Phase 2 can run in parallel by different engineers (independent data sources, independent providers).
- yfinance is "good enough" for daily data; if the team prefers paid feeds (Refinitiv, Bloomberg) later, the schema and FeatureProvider stay the same — only the ingestor swaps.
- All new external data is **daily resolution** — joined to intraday bars via `bar.time.date()`. This gives slow-moving context features, NOT high-frequency microstructure.

---

## File Structure

**Phase 0 (no new files)** — only runs existing CLIs + reads artifacts.

**Phase 1 (CME GC volume):**
- Create `src/persistence/schema/daily_external_ohlc.py` (new table DDL + INSERT_SQL)
- Create `src/research/external/__init__.py` (subpackage)
- Create `src/research/external/yfinance_client.py` (HTTP client wrapper)
- Create `src/research/external/cme_backfill.py` (one-shot CLI to backfill last 3y)
- Create `src/research/features/cme_volume/__init__.py` (provider package)
- Create `src/research/features/cme_volume/provider.py` (`CMEVolumeFeatureProvider`)
- Modify `src/research/features/hub.py` (register provider)
- Modify `config/research.ini` (`[feature_providers]` add `cme_volume = true`)
- Create `tests/research/external/test_cme_backfill.py`
- Create `tests/research/features/cme_volume/test_provider.py`
- Modify `pyproject.toml` (add yfinance dep)

**Phase 2 (cross-asset ingestor):**
- Modify `src/research/external/yfinance_client.py` (add multi-symbol support)
- Create `src/research/external/cross_asset_backfill.py` (CLI for DXY/10Y/SPX)
- Create `src/research/features/cross_asset/__init__.py`
- Create `src/research/features/cross_asset/provider.py` (`CrossAssetFeatureProvider`)
- Modify `src/research/features/hub.py` (register provider)
- Modify `config/research.ini` (add `cross_asset = true` + `[feature_providers.cross_asset]`)
- Create `tests/research/external/test_cross_asset_backfill.py`
- Create `tests/research/features/cross_asset/test_provider.py`

**Phase 3 (validation):**
- Modify `docs/research/2026-05-03-high-freq-architecture-audit.md` (append validation results)
- Create `docs/research/<date>-feature-pool-expansion-results.md` (snapshot)

---

## Phase 0: ML Pipeline Sanity Gate

**Purpose:** Before adding any new data source, verify the existing ML pipeline (state_edge_lab → backtest_runner overlay → state_edge_overlay_report) runs end-to-end without errors on current 121-feature pool. If it fails here, no amount of new features helps.

### Task 0.1: Run state_edge_lab on H1 baseline

**Files:** None (CLI invocation only)

- [ ] **Step 1: Verify research config loads correctly**

```bash
python -c "from src.research.core.config import load_research_config; c = load_research_config(); print('feature providers enabled:', [n for n, v in vars(c.feature_providers).items() if v is True])"
```

Expected: lists `temporal microstructure regime_transition session_event intrabar candle_patterns` (all True). Records the baseline feature pool size.

- [ ] **Step 2: Run state_edge_lab end-to-end on H1 (last 12 months)**

```bash
python -m src.ops.cli.state_edge_lab \
  --environment live \
  --tf H1 \
  --start 2025-04-01 --end 2026-04-15 \
  --backend cpu \
  --json-output artifacts/phase0_state_edge_h1_baseline.json \
  --no-auto-backfill
```

Expected: exit 0, artifact written. If it fails — that's the bug to fix BEFORE Phase 1.

- [ ] **Step 3: Run baseline backtest (no overlay)**

```bash
python -m src.ops.cli.backtest_runner \
  --environment live \
  --tf H1 \
  --start 2026-01-01 --end 2026-04-15 \
  --include-demo-validation \
  --json-output artifacts/phase0_baseline_h1.json
```

Expected: exit 0; even with empty catalog (post-cleanup) the runner should produce a valid report (0 trades acceptable). If runner crashes with empty strategy set — that's an infrastructure bug; file as separate fix.

- [ ] **Step 4: Decision gate — record outcome**

Append to `docs/research/2026-05-03-high-freq-architecture-audit.md` a new section:
```
## Phase 0 Sanity (YYYY-MM-DD)
- state_edge_lab H1 12mo run: <exit code, artifact size, model accuracy>
- backtest_runner: <trades, runtime>
- Verdict: <PROCEED to Phase 1 / HALT and fix>
```

If state_edge_lab fails or produces unusable artifact: STOP. File a P0 fix issue. Do not continue to Phase 1.

- [ ] **Step 5: Commit**

```bash
git add docs/research/2026-05-03-high-freq-architecture-audit.md artifacts/phase0_*.json
git commit -m "docs: phase 0 ML pipeline sanity validation"
```

---

## Phase 1: CME GC Volume FeatureProvider

**Purpose:** XAUUSD broker tick_volume is not real volume. CME GC futures (the institutional gold market) provides real daily volume. Adding it as a feature gives mining its first non-OHLC information source.

**Why CME GC**: it's the largest gold futures contract, daily-resolution data is free via Yahoo Finance (`GC=F`), and academic literature shows ~0.7-0.85 correlation with XAUUSD spot price-action (lagged by hours).

### Task 1.1: Add yfinance dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add yfinance to dependencies**

In `pyproject.toml`, find the `dependencies = [...]` block (around line 30-50) and add:
```toml
"yfinance>=0.2.40",
```
Also append to the `[[tool.mypy.overrides]]` ignore_missing_imports list:
```toml
"yfinance.*",
```

- [ ] **Step 2: Install in dev environment**

```bash
pip install "yfinance>=0.2.40"
```

Verify: `python -c "import yfinance; print(yfinance.__version__)"` — expect a version >= 0.2.40.

- [ ] **Step 3: Smoke test (no commit yet, just verify the API works)**

```bash
python -c "import yfinance as yf; df = yf.Ticker('GC=F').history(period='5d', interval='1d'); print(df[['Close', 'Volume']].tail())"
```

Expected: 5 rows of recent daily data with non-zero Volume column. If Volume is all zero or NaN, file a data-source issue (try `pandas_datareader` Stooq as fallback).

- [ ] **Step 4: Commit dependency change only**

```bash
git add pyproject.toml
git commit -m "deps: add yfinance for external daily OHLCV"
```

### Task 1.2: Create daily_external_ohlc table schema

**Files:**
- Create: `src/persistence/schema/daily_external_ohlc.py`
- Modify: `src/persistence/schema/__init__.py` (export the new module)
- Test: `tests/persistence/schema/test_daily_external_ohlc.py`

- [ ] **Step 1: Write the failing schema test**

Create `tests/persistence/schema/test_daily_external_ohlc.py`:
```python
"""daily_external_ohlc table schema — daily OHLCV for non-broker external symbols
(CME GC, DXY, ^TNX, ^GSPC). Composite PK (symbol, date). TimescaleDB hypertable
on date column; symbol is text to keep schema generic for future symbols.
"""
from src.persistence.schema.daily_external_ohlc import DDL, INSERT_SQL


def test_ddl_creates_hypertable_with_composite_pk() -> None:
    assert "CREATE TABLE IF NOT EXISTS daily_external_ohlc" in DDL
    assert "PRIMARY KEY (symbol, date)" in DDL
    assert "create_hypertable" in DDL
    assert "'date'" in DDL  # hypertable time column


def test_insert_sql_has_seven_placeholders() -> None:
    # symbol, date, open, high, low, close, volume
    assert INSERT_SQL.count("%s") == 7
    assert "ON CONFLICT (symbol, date) DO UPDATE" in INSERT_SQL


def test_indices_for_query_patterns() -> None:
    # Need symbol + date DESC for "latest N days for symbol X" pattern
    assert "idx_daily_external_ohlc_symbol_date" in DDL
```

Also create `tests/persistence/schema/__init__.py` (empty) if missing.

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/persistence/schema/test_daily_external_ohlc.py -v
```

Expected: ImportError (module not found).

- [ ] **Step 3: Write the implementation**

Create `src/persistence/schema/daily_external_ohlc.py`:
```python
"""daily_external_ohlc table DDL.

Stores daily OHLCV for non-broker external symbols used by research feature
providers (CME GC futures, DXY, 10Y yield, S&P 500). Composite PK
(symbol, date) keeps the schema generic for any new symbol added later.

Resolution: 1 day. Joined to intraday bars in feature providers via
``bar.time.date()`` lookup.
"""

DDL = """
CREATE TABLE IF NOT EXISTS daily_external_ohlc (
    symbol  text NOT NULL,
    date    date NOT NULL,
    open    double precision,
    high    double precision,
    low     double precision,
    close   double precision,
    volume  double precision,
    PRIMARY KEY (symbol, date)
);
SELECT create_hypertable('daily_external_ohlc', 'date',
                          if_not_exists => TRUE, migrate_data => TRUE);
CREATE INDEX IF NOT EXISTS idx_daily_external_ohlc_symbol_date
ON daily_external_ohlc (symbol, date DESC);
"""

INSERT_SQL = """
INSERT INTO daily_external_ohlc (symbol, date, open, high, low, close, volume)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, date) DO UPDATE SET
    open   = EXCLUDED.open,
    high   = EXCLUDED.high,
    low    = EXCLUDED.low,
    close  = EXCLUDED.close,
    volume = EXCLUDED.volume
"""
```

- [ ] **Step 4: Register in schema __init__**

Open `src/persistence/schema/__init__.py` and add the import + export. Look for the existing pattern (other schemas like `signal_outcomes` have similar imports). Add:
```python
from src.persistence.schema import daily_external_ohlc as _daily_external_ohlc
```
And add `_daily_external_ohlc` to whatever registry the file uses for DDL execution. Check the existing pattern (likely an `ALL_DDL` list or similar).

- [ ] **Step 5: Run the test — confirm it passes**

```bash
python -m pytest tests/persistence/schema/test_daily_external_ohlc.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Apply DDL to the live DB**

```bash
python -c "
from src.config.database import load_db_settings
from src.persistence.db import TimescaleWriter
from src.persistence.schema.daily_external_ohlc import DDL
w = TimescaleWriter(load_db_settings('live'))
with w.connection() as conn, conn.cursor() as cur:
    cur.execute(DDL)
    conn.commit()
print('daily_external_ohlc table created')
"
```

Verify: `psql ... -c '\d daily_external_ohlc'` should show the table with hypertable status. If using demo DB also: re-run with `load_db_settings('demo')`.

- [ ] **Step 7: Commit**

```bash
git add src/persistence/schema/daily_external_ohlc.py src/persistence/schema/__init__.py tests/persistence/schema/
git commit -m "feat: add daily_external_ohlc hypertable schema"
```

### Task 1.3: yfinance client wrapper

**Files:**
- Create: `src/research/external/__init__.py`
- Create: `src/research/external/yfinance_client.py`
- Test: `tests/research/external/test_yfinance_client.py`

- [ ] **Step 1: Write the failing client test**

Create `tests/research/external/__init__.py` (empty file).
Create `tests/research/external/test_yfinance_client.py`:
```python
"""yfinance_client thin wrapper — single responsibility: fetch daily OHLCV
for a yahoo symbol over a date range, return list of (date, OHLCV) tuples.
Network calls mocked; production behavior verified separately."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.research.external.yfinance_client import (
    YFinanceClient,
    YFinanceError,
    DailyBar,
)


def _fake_history_df(rows: list[tuple[str, float, float, float, float, float]]):
    import pandas as pd
    df = pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}
         for _, o, h, l, c, v in rows],
        index=pd.to_datetime([r[0] for r in rows]),
    )
    return df


def test_fetch_daily_returns_typed_bars() -> None:
    fake_df = _fake_history_df([
        ("2026-04-01", 2300.0, 2310.0, 2295.0, 2305.0, 250000.0),
        ("2026-04-02", 2305.0, 2320.0, 2300.0, 2315.0, 280000.0),
    ])
    client = YFinanceClient()
    with patch.object(client, "_history_for", return_value=fake_df):
        bars = client.fetch_daily("GC=F", start=date(2026, 4, 1), end=date(2026, 4, 2))
    assert len(bars) == 2
    assert bars[0] == DailyBar(
        symbol="GC=F", date=date(2026, 4, 1),
        open=2300.0, high=2310.0, low=2295.0, close=2305.0, volume=250000.0,
    )


def test_empty_response_raises_yfinance_error() -> None:
    import pandas as pd
    client = YFinanceClient()
    with patch.object(client, "_history_for", return_value=pd.DataFrame()):
        with pytest.raises(YFinanceError) as excinfo:
            client.fetch_daily("BAD=X", start=date(2026, 4, 1), end=date(2026, 4, 2))
    assert "no data" in str(excinfo.value).lower()


def test_nan_volume_normalized_to_zero() -> None:
    import pandas as pd
    fake_df = _fake_history_df([("2026-04-01", 2300.0, 2310.0, 2295.0, 2305.0, 0.0)])
    fake_df.loc[fake_df.index[0], "Volume"] = float("nan")
    client = YFinanceClient()
    with patch.object(client, "_history_for", return_value=fake_df):
        bars = client.fetch_daily("GC=F", start=date(2026, 4, 1), end=date(2026, 4, 1))
    assert bars[0].volume == 0.0
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/research/external/test_yfinance_client.py -v
```
Expected: ImportError (module not found).

- [ ] **Step 3: Write the implementation**

Create `src/research/external/__init__.py` (empty). Create `src/research/external/yfinance_client.py`:

```python
"""Thin wrapper around yfinance for daily OHLCV fetches.

Single responsibility: fetch daily OHLCV for one Yahoo Finance symbol over
a date range, return typed DailyBar values. NaN volumes normalized to 0.0
(yfinance returns NaN for symbols where Yahoo lacks volume — e.g. some indices).

NOT responsible for: persistence, retries beyond yfinance defaults, multi-symbol
parallel fetching, rate limiting (Yahoo's free tier is generous for daily data).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, List

logger = logging.getLogger(__name__)


class YFinanceError(RuntimeError):
    """Raised when yfinance returns no data or a malformed response."""


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


class YFinanceClient:
    """Synchronous wrapper. yfinance is itself synchronous (urllib under
    the hood); we don't add async because daily backfills are < 100 RPS."""

    def fetch_daily(
        self, symbol: str, *, start: date, end: date
    ) -> List[DailyBar]:
        """Fetch daily OHLCV for [start, end] inclusive.

        Raises YFinanceError if the response is empty (Yahoo returns empty
        DataFrame for invalid symbols silently).
        """
        df = self._history_for(symbol, start=start, end=end)
        if df is None or df.empty:
            raise YFinanceError(
                f"yfinance returned no data for {symbol} {start}..{end}"
            )

        bars: List[DailyBar] = []
        for ts, row in df.iterrows():
            volume = float(row.get("Volume", 0.0) or 0.0)
            if math.isnan(volume):
                volume = 0.0
            bars.append(
                DailyBar(
                    symbol=symbol,
                    date=ts.date() if hasattr(ts, "date") else ts,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=volume,
                )
            )
        return bars

    def _history_for(self, symbol: str, *, start: date, end: date) -> Any:
        """Isolated for test patching. Calls yfinance.Ticker(...).history(...)."""
        import yfinance as yf

        # yfinance end is exclusive; bump by 1 day so the range is inclusive
        # (matches our DailyBar contract).
        return yf.Ticker(symbol).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=False,
        )
```

- [ ] **Step 4: Run the test — confirm it passes**

```bash
python -m pytest tests/research/external/test_yfinance_client.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/research/external/__init__.py src/research/external/yfinance_client.py tests/research/external/
git commit -m "feat: yfinance daily OHLCV client wrapper"
```

### Task 1.4: CME backfill CLI

**Files:**
- Create: `src/research/external/cme_backfill.py`
- Test: `tests/research/external/test_cme_backfill.py`

- [ ] **Step 1: Write the failing CLI test**

Create `tests/research/external/test_cme_backfill.py`:
```python
"""cme_backfill CLI — pulls daily CME GC futures (`GC=F`) from yfinance and
writes to daily_external_ohlc table. Idempotent via ON CONFLICT UPDATE.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.research.external.cme_backfill import backfill, parse_args
from src.research.external.yfinance_client import DailyBar


def test_parse_args_defaults_to_three_years_back() -> None:
    args = parse_args(["--environment", "live"])
    assert args.environment == "live"
    assert args.symbol == "GC=F"  # default symbol
    assert (date.today() - args.start).days >= 365 * 3 - 1


def test_backfill_inserts_fetched_bars() -> None:
    fake_bars = [
        DailyBar("GC=F", date(2026, 4, 1), 2300, 2310, 2295, 2305, 250000),
        DailyBar("GC=F", date(2026, 4, 2), 2305, 2320, 2300, 2315, 280000),
    ]
    fake_writer = MagicMock()
    fake_client = MagicMock()
    fake_client.fetch_daily.return_value = fake_bars

    inserted = backfill(
        client=fake_client,
        writer=fake_writer,
        symbol="GC=F",
        start=date(2026, 4, 1),
        end=date(2026, 4, 2),
    )

    assert inserted == 2
    fake_client.fetch_daily.assert_called_once_with(
        "GC=F", start=date(2026, 4, 1), end=date(2026, 4, 2)
    )
    # writer.execute called once per bar
    assert fake_writer.execute.call_count == 2
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/research/external/test_cme_backfill.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write the implementation**

Create `src/research/external/cme_backfill.py`:
```python
"""cme_backfill — one-shot CLI to backfill CME GC daily OHLCV into
daily_external_ohlc table.

Usage:
    python -m src.research.external.cme_backfill --environment live
    python -m src.research.external.cme_backfill --environment live --start 2023-01-01

Idempotent: ON CONFLICT (symbol, date) DO UPDATE in DDL means re-running
overwrites existing rows with fresh data.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, List, Sequence

from src.persistence.schema.daily_external_ohlc import INSERT_SQL
from src.research.external.yfinance_client import DailyBar, YFinanceClient

logger = logging.getLogger(__name__)


@dataclass
class _CliArgs:
    environment: str
    symbol: str
    start: date
    end: date


def parse_args(argv: Sequence[str] | None = None) -> _CliArgs:
    parser = argparse.ArgumentParser(prog="cme_backfill")
    parser.add_argument("--environment", required=True, choices=("live", "demo"))
    parser.add_argument("--symbol", default="GC=F")
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
        symbol=ns.symbol,
        start=ns.start,
        end=ns.end,
    )


def backfill(
    *,
    client: Any,
    writer: Any,
    symbol: str,
    start: date,
    end: date,
) -> int:
    """Fetch + insert. Returns number of rows inserted/updated."""
    bars: List[DailyBar] = client.fetch_daily(symbol, start=start, end=end)
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
    logger.info("cme_backfill: %s %d rows inserted/updated", symbol, len(bars))
    return len(bars)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = parse_args(argv)

    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter

    settings = load_db_settings(args.environment)
    db_writer = TimescaleWriter(settings)

    client = YFinanceClient()
    with db_writer.connection() as conn, conn.cursor() as cur:
        n = backfill(
            client=client,
            writer=cur,
            symbol=args.symbol,
            start=args.start,
            end=args.end,
        )
        conn.commit()
    print(f"backfilled {n} rows for {args.symbol}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test — confirm it passes**

```bash
python -m pytest tests/research/external/test_cme_backfill.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Run real backfill on live DB**

```bash
python -m src.research.external.cme_backfill --environment live --start 2023-01-01
```
Expected: prints `backfilled N rows for GC=F` where N >= 700 (3 years of trading days).

Verify: `psql -c "SELECT MIN(date), MAX(date), COUNT(*) FROM daily_external_ohlc WHERE symbol='GC=F'"` returns the expected range.

- [ ] **Step 6: Commit**

```bash
git add src/research/external/cme_backfill.py tests/research/external/test_cme_backfill.py
git commit -m "feat: CME GC volume backfill CLI"
```

### Task 1.5: CMEVolumeFeatureProvider

**Files:**
- Create: `src/research/features/cme_volume/__init__.py`
- Create: `src/research/features/cme_volume/provider.py`
- Test: `tests/research/features/cme_volume/test_provider.py`

- [ ] **Step 1: Write the failing provider test**

Create `tests/research/features/cme_volume/__init__.py` (empty).
Create `tests/research/features/cme_volume/test_provider.py`:
```python
"""CMEVolumeFeatureProvider — joins XAUUSD bars to daily CME GC volume,
emits volume-relative features as mining inputs.

Output features (4 fields):
  cme_volume_zscore_20d  (WHEN)  — volume relative to its own 20-day mean (institutional flow surge)
  cme_volume_change_5d   (WHY)   — 5-day cme_volume change (momentum of institutional activity)
  cme_volume_ratio       (WHY)   — today's volume / yesterday's volume
  cme_volume_pct_rank    (WHEN)  — today's volume percentile rank in trailing 60d
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import pytest

from src.research.features.cme_volume.provider import CMEVolumeFeatureProvider
from src.research.features.protocol import FeatureRole


class _FakeMatrix:
    def __init__(self, bar_times: List[datetime]):
        self.bar_times = bar_times
        self.n_bars = len(bar_times)
        self.indicator_series: dict = {}


def _bar_times_h1(days: int = 5, hours_per_day: int = 24) -> List[datetime]:
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    return [
        base + timedelta(days=d, hours=h)
        for d in range(days)
        for h in range(hours_per_day)
    ]


def test_role_mapping_assigns_each_field() -> None:
    p = CMEVolumeFeatureProvider(daily_volume_lookup=lambda d: None)
    roles = p.role_mapping()
    assert roles["cme_volume_zscore_20d"] == FeatureRole.WHEN
    assert roles["cme_volume_change_5d"] == FeatureRole.WHY
    assert roles["cme_volume_ratio"] == FeatureRole.WHY
    assert roles["cme_volume_pct_rank"] == FeatureRole.WHEN


def test_compute_emits_one_value_per_bar() -> None:
    bars = _bar_times_h1(days=5)
    matrix = _FakeMatrix(bars)
    # Mock lookup: every date returns 100k volume
    lookup = lambda d: 100_000.0
    p = CMEVolumeFeatureProvider(daily_volume_lookup=lookup)
    out = p.compute(matrix)

    for key, values in out.items():
        assert len(values) == matrix.n_bars, f"{key} length mismatch"


def test_compute_returns_none_when_volume_lookup_missing() -> None:
    bars = _bar_times_h1(days=5)
    matrix = _FakeMatrix(bars)
    p = CMEVolumeFeatureProvider(daily_volume_lookup=lambda d: None)
    out = p.compute(matrix)

    # All values should be None when lookup returns None for every date
    for key, values in out.items():
        assert all(v is None for v in values), f"{key} expected all None"


def test_zscore_increases_when_today_volume_spikes() -> None:
    bars = _bar_times_h1(days=22)
    matrix = _FakeMatrix(bars)
    # 20 days of 100k, last 2 days 500k spike
    def lookup(d: date) -> float:
        idx = (d - bars[0].date()).days
        return 500_000.0 if idx >= 20 else 100_000.0

    p = CMEVolumeFeatureProvider(daily_volume_lookup=lookup)
    out = p.compute(matrix)

    z = out[("cme_volume", "cme_volume_zscore_20d")]
    # First bars (no 20d history) should be None
    assert z[0] is None
    # Last bars (after spike) should have positive z-score > 2
    last = z[-1]
    assert last is not None and last > 2.0
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/research/features/cme_volume/test_provider.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write the implementation**

Create `src/research/features/cme_volume/__init__.py`:
```python
from src.research.features.cme_volume.provider import CMEVolumeFeatureProvider

__all__ = ["CMEVolumeFeatureProvider"]
```

Create `src/research/features/cme_volume/provider.py`:
```python
"""CMEVolumeFeatureProvider — daily CME GC futures volume features.

Joins each intraday bar to its trading-day's CME GC volume via
``bar.time.date()``. Emits volume-relative features that capture
institutional gold market activity (proxy for "real" XAUUSD volume that
broker tick_volume cannot observe).

Daily resolution intentional: CME GC volume only publishes daily totals
publicly. Intraday bars within the same day all share the same daily
volume value (this is fine — we want regime context, not high-freq).

All features emit None when:
  - daily_volume_lookup returns None for the bar's date (pre-data window
    or weekend with no published volume)
  - rolling-window features lack enough history (first 5 / 20 / 60 bars)
"""
from __future__ import annotations

import statistics
from collections import deque
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.research.features.protocol import (
    FeatureProvider,
    FeatureRole,
    ProviderDataRequirement,
)


_FEATURE_KEYS: Tuple[Tuple[str, str], ...] = (
    ("cme_volume", "cme_volume_zscore_20d"),
    ("cme_volume", "cme_volume_change_5d"),
    ("cme_volume", "cme_volume_ratio"),
    ("cme_volume", "cme_volume_pct_rank"),
)


class CMEVolumeFeatureProvider:
    """FeatureProvider Protocol implementation; see _FEATURE_KEYS for outputs."""

    name = "cme_volume"
    feature_count = len(_FEATURE_KEYS)

    def __init__(
        self,
        daily_volume_lookup: Callable[[date], Optional[float]],
    ) -> None:
        """daily_volume_lookup: callable that returns daily CME volume for a
        given date, or None when no data is available. Production callers
        wire a DB-backed lookup; tests inject in-memory lambdas."""
        self._lookup = daily_volume_lookup

    def required_columns(self) -> List[Tuple[str, str]]:
        return []  # No DataMatrix dependencies; we read external data via lookup.

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        return None

    def role_mapping(self) -> Dict[str, FeatureRole]:
        return {
            "cme_volume_zscore_20d": FeatureRole.WHEN,
            "cme_volume_change_5d": FeatureRole.WHY,
            "cme_volume_ratio": FeatureRole.WHY,
            "cme_volume_pct_rank": FeatureRole.WHEN,
        }

    def compute(
        self,
        matrix: Any,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        n = matrix.n_bars
        out: Dict[Tuple[str, str], List[Optional[float]]] = {
            key: [None] * n for key in _FEATURE_KEYS
        }

        # Resolve daily volume for each bar's date (cache to avoid N*1d lookups)
        date_volume: Dict[date, Optional[float]] = {}
        per_bar_volumes: List[Optional[float]] = []
        for bt in matrix.bar_times:
            d = bt.date()
            if d not in date_volume:
                date_volume[d] = self._lookup(d)
            per_bar_volumes.append(date_volume[d])

        # Roll daily-distinct values (not bar-by-bar; intraday bars share the day's value)
        ordered_dates: List[date] = []
        ordered_vols: List[Optional[float]] = []
        seen: set = set()
        for bt in matrix.bar_times:
            d = bt.date()
            if d in seen:
                continue
            seen.add(d)
            ordered_dates.append(d)
            ordered_vols.append(date_volume[d])

        # Compute daily-resolution features then broadcast back to bars
        zscore_by_date: Dict[date, Optional[float]] = {}
        change_by_date: Dict[date, Optional[float]] = {}
        ratio_by_date: Dict[date, Optional[float]] = {}
        pctrank_by_date: Dict[date, Optional[float]] = {}

        for i, d in enumerate(ordered_dates):
            v = ordered_vols[i]
            if v is None:
                zscore_by_date[d] = None
                change_by_date[d] = None
                ratio_by_date[d] = None
                pctrank_by_date[d] = None
                continue

            # zscore over last 20 days (excluding today)
            history_20 = [
                vol for vol in ordered_vols[max(0, i - 20) : i] if vol is not None
            ]
            if len(history_20) >= 20:
                mean_20 = sum(history_20) / len(history_20)
                std_20 = statistics.pstdev(history_20)
                zscore_by_date[d] = (
                    (v - mean_20) / std_20 if std_20 > 0 else 0.0
                )
            else:
                zscore_by_date[d] = None

            # 5-day change (today / 5d-ago - 1)
            if i >= 5 and ordered_vols[i - 5] is not None and ordered_vols[i - 5] > 0:
                change_by_date[d] = v / ordered_vols[i - 5] - 1.0
            else:
                change_by_date[d] = None

            # ratio today/yesterday
            if i >= 1 and ordered_vols[i - 1] is not None and ordered_vols[i - 1] > 0:
                ratio_by_date[d] = v / ordered_vols[i - 1]
            else:
                ratio_by_date[d] = None

            # percentile rank in trailing 60 days
            history_60 = [
                vol for vol in ordered_vols[max(0, i - 60) : i] if vol is not None
            ]
            if len(history_60) >= 30:
                rank = sum(1 for x in history_60 if x <= v) / len(history_60)
                pctrank_by_date[d] = rank
            else:
                pctrank_by_date[d] = None

        # Broadcast daily values to per-bar arrays
        for bar_i, bt in enumerate(matrix.bar_times):
            d = bt.date()
            out[("cme_volume", "cme_volume_zscore_20d")][bar_i] = zscore_by_date.get(d)
            out[("cme_volume", "cme_volume_change_5d")][bar_i] = change_by_date.get(d)
            out[("cme_volume", "cme_volume_ratio")][bar_i] = ratio_by_date.get(d)
            out[("cme_volume", "cme_volume_pct_rank")][bar_i] = pctrank_by_date.get(d)

        return out
```

- [ ] **Step 4: Run the test — confirm it passes**

```bash
python -m pytest tests/research/features/cme_volume/test_provider.py -v
```
Expected: 4 passed. If `test_zscore_increases_when_today_volume_spikes` fails because the zscore is None, debug — likely the ordered_dates window is off by one.

- [ ] **Step 5: Commit**

```bash
git add src/research/features/cme_volume/ tests/research/features/cme_volume/
git commit -m "feat: CMEVolumeFeatureProvider with 4 daily-volume features"
```

### Task 1.6: Wire CMEVolumeFeatureProvider into FeatureHub

**Files:**
- Modify: `src/research/features/hub.py:48-77`
- Modify: `config/research.ini` (add `cme_volume = true` and `[feature_providers.cme_volume]`)
- Modify: `src/config/models/research.py` (add `cme_volume: bool = False` to `FeatureProvidersConfig` if needed — read the existing pattern first)
- Test: `tests/research/features/test_hub.py` (modify if exists; create if not)

- [ ] **Step 1: Find FeatureProvidersConfig**

```bash
grep -n "feature_providers\|FeatureProvidersConfig" src/config/ -r 2>&1 | head -10
```
Open the file that defines `FeatureProvidersConfig` (likely `src/config/models/research.py` or similar). Add a new field `cme_volume: bool = False`.

- [ ] **Step 2: Wire the lookup factory**

Add to `src/research/features/hub.py` import block (top of `_register_enabled_providers`):
```python
from src.research.features.cme_volume import CMEVolumeFeatureProvider
```

Inside `_register_enabled_providers`, in the `_PROVIDER_FACTORIES` dict, add:
```python
"cme_volume": lambda: CMEVolumeFeatureProvider(
    daily_volume_lookup=_make_cme_volume_lookup(),
),
```

Add the helper above the `_PROVIDER_FACTORIES` definition:
```python
def _make_cme_volume_lookup():
    """Return a callable that fetches daily CME GC volume from the live DB.

    Cached: built once per FeatureHub init (per mining run); SQL hits the
    daily_external_ohlc hypertable which is small (<2k rows for 5+ years).
    """
    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter

    writer = TimescaleWriter(load_db_settings("live"))
    cache: dict = {}

    def lookup(d):
        if d in cache:
            return cache[d]
        with writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT volume FROM daily_external_ohlc WHERE symbol=%s AND date=%s",
                ("GC=F", d),
            )
            row = cur.fetchone()
            cache[d] = float(row[0]) if row and row[0] is not None else None
        return cache[d]

    return lookup
```

- [ ] **Step 3: Add config entry**

In `config/research.ini` `[feature_providers]` block:
```ini
cme_volume = true
```

Optionally add (no params for now, but the section anchors documentation):
```ini
[feature_providers.cme_volume]
# Daily CME GC futures volume features (4 fields). Backfilled via
# `python -m src.research.external.cme_backfill --environment live`.
# Lookup queries daily_external_ohlc table on each FeatureHub init.
```

- [ ] **Step 4: Smoke test FeatureHub registration**

```bash
python -c "
from src.research.core.config import load_research_config
from src.research.features.hub import FeatureHub
hub = FeatureHub(load_research_config())
print('Registered providers:', hub.describe())
"
```
Expected: `cme_volume` appears in the dict with `feature_count: 4`.

- [ ] **Step 5: Run all feature provider tests + research config tests**

```bash
python -m pytest tests/research/features/ tests/research/core/ -v
```
Expected: all pass. If config test fails: check the FeatureProvidersConfig field addition.

- [ ] **Step 6: Commit**

```bash
git add src/research/features/hub.py src/config/models/research.py config/research.ini
git commit -m "feat: register CMEVolumeFeatureProvider in FeatureHub"
```

### Task 1.7: Mining run with new feature pool + report

**Files:** None (CLI invocation + new artifact)

- [ ] **Step 1: Run mining with new provider**

```bash
python -m src.ops.cli.mining_runner \
  --environment live \
  --tf H1 \
  --start 2025-04-01 --end 2026-04-15 \
  --providers temporal,microstructure,cross_tf,regime_transition,session_event,intrabar,candle_patterns,cme_volume \
  --child-tf M5 \
  --json-output data/research/phase1_mining_h1_with_cme.json
```

Expected: exit 0; provider_summary in output JSON includes `cme_volume: 4 features`. Mining_run completes without errors. New rules may or may not appear — that's the empirical result.

- [ ] **Step 2: Compare with baseline mining (no cme_volume)**

```bash
python -m src.ops.cli.mining_runner \
  --environment live \
  --tf H1 \
  --start 2025-04-01 --end 2026-04-15 \
  --providers temporal,microstructure,cross_tf,regime_transition,session_event,intrabar,candle_patterns \
  --child-tf M5 \
  --json-output data/research/phase1_mining_h1_baseline.json
```

- [ ] **Step 3: Diff result counts**

```bash
python -c "
import json
with open('data/research/phase1_mining_h1_with_cme.json') as f: a=json.load(f)
with open('data/research/phase1_mining_h1_baseline.json') as f: b=json.load(f)
def stats(p):
    r = p.get('results', [{}])[0]
    return {
        'total_rules': len(r.get('mined_rules', [])),
        'total_features_pp': sum(p['feature_count'] for p in r.get('predictive_powers', [])),
    }
print('with_cme:', stats(a))
print('baseline:', stats(b))
"
```

- [ ] **Step 4: Decision gate**

If `with_cme` produces ≥1 mining-promoted rule that uses `cme_volume_*` field AND that rule passes Stage-2 backtest verification (per existing PROMOTION_GATES + filter_by_backtest):
  → CME volume IS adding alpha. Log to docs and proceed to Phase 2.

If no cme_volume-using rule is promoted, OR all promoted rules fail Stage-2:
  → CME daily granularity is too coarse for H1. Options: (a) try H4 + D1 mining where daily volume context is more relevant, OR (b) accept that CME volume alone isn't sufficient and proceed to Phase 2 (DXY/10Y/SPX) which provides directional context.

Either way, append a 1-paragraph snapshot to `docs/research/2026-05-03-high-freq-architecture-audit.md` Phase 1 section.

- [ ] **Step 5: Commit**

```bash
git add data/research/phase1_*.json docs/research/2026-05-03-high-freq-architecture-audit.md
git commit -m "docs: phase 1 CME volume mining result"
```

---

## Phase 2: Cross-Asset Ingestor (DXY / 10Y / SPX)

**Purpose:** XAUUSD has well-documented multi-decade correlation with USD strength (DXY, negative), real interest rates (10Y yield, negative), and risk sentiment (SPX, mixed regime-dependent). These are the textbook macro drivers absent from current 121-feature pool.

### Task 2.1: Extend backfill CLI to multiple symbols

**Files:**
- Create: `src/research/external/cross_asset_backfill.py`
- Test: `tests/research/external/test_cross_asset_backfill.py`

- [ ] **Step 1: Write the failing test**

Create `tests/research/external/test_cross_asset_backfill.py`:
```python
"""cross_asset_backfill — pulls DXY (^DXY/DX-Y.NYB), 10Y (^TNX), SPX (^GSPC)
daily OHLCV via yfinance into daily_external_ohlc.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from src.research.external.cross_asset_backfill import (
    CROSS_ASSET_SYMBOLS,
    backfill_all,
    parse_args,
)
from src.research.external.yfinance_client import DailyBar


def test_default_symbol_set() -> None:
    # Three macro drivers
    assert set(CROSS_ASSET_SYMBOLS) == {"DX-Y.NYB", "^TNX", "^GSPC"}


def test_parse_args_optional_symbol_filter() -> None:
    args = parse_args(["--environment", "live", "--symbols", "DX-Y.NYB,^TNX"])
    assert args.symbols == ["DX-Y.NYB", "^TNX"]


def test_backfill_all_calls_per_symbol() -> None:
    fake_bars_factory = lambda sym: [
        DailyBar(sym, date(2026, 4, 1), 100, 101, 99, 100.5, 0)
    ]
    fake_writer = MagicMock()
    fake_client = MagicMock()
    fake_client.fetch_daily.side_effect = lambda s, **kw: fake_bars_factory(s)

    counts = backfill_all(
        client=fake_client,
        writer=fake_writer,
        symbols=["DX-Y.NYB", "^TNX"],
        start=date(2026, 4, 1),
        end=date(2026, 4, 1),
    )

    assert counts == {"DX-Y.NYB": 1, "^TNX": 1}
    assert fake_client.fetch_daily.call_count == 2
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/research/external/test_cross_asset_backfill.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write the implementation**

Create `src/research/external/cross_asset_backfill.py`:
```python
"""cross_asset_backfill — backfill DXY/10Y/SPX daily OHLCV via yfinance.

Usage:
    python -m src.research.external.cross_asset_backfill --environment live
    python -m src.research.external.cross_asset_backfill --environment live --symbols ^TNX
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from src.persistence.schema.daily_external_ohlc import INSERT_SQL
from src.research.external.yfinance_client import DailyBar, YFinanceClient, YFinanceError

logger = logging.getLogger(__name__)

CROSS_ASSET_SYMBOLS: List[str] = ["DX-Y.NYB", "^TNX", "^GSPC"]


@dataclass
class _CliArgs:
    environment: str
    symbols: List[str]
    start: date
    end: date


def parse_args(argv: Sequence[str] | None = None) -> _CliArgs:
    parser = argparse.ArgumentParser(prog="cross_asset_backfill")
    parser.add_argument("--environment", required=True, choices=("live", "demo"))
    parser.add_argument(
        "--symbols",
        type=lambda s: s.split(","),
        default=CROSS_ASSET_SYMBOLS,
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
        symbols=list(ns.symbols),
        start=ns.start,
        end=ns.end,
    )


def backfill_all(
    *,
    client: Any,
    writer: Any,
    symbols: List[str],
    start: date,
    end: date,
) -> Dict[str, int]:
    """Returns per-symbol row count. Failures on one symbol don't stop others."""
    counts: Dict[str, int] = {}
    for sym in symbols:
        try:
            bars: List[DailyBar] = client.fetch_daily(sym, start=start, end=end)
        except YFinanceError as exc:
            logger.warning("cross_asset_backfill: %s skipped: %s", sym, exc)
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
        logger.info("cross_asset_backfill: %s %d rows", sym, len(bars))
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = parse_args(argv)

    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter

    settings = load_db_settings(args.environment)
    db_writer = TimescaleWriter(settings)

    client = YFinanceClient()
    with db_writer.connection() as conn, conn.cursor() as cur:
        counts = backfill_all(
            client=client,
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
```

- [ ] **Step 4: Run the test — confirm it passes**

```bash
python -m pytest tests/research/external/test_cross_asset_backfill.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Run real backfill**

```bash
python -m src.research.external.cross_asset_backfill --environment live --start 2023-01-01
```
Expected: prints `backfilled: {'DX-Y.NYB': 700+, '^TNX': 700+, '^GSPC': 700+}`. If any symbol shows 0, log a warning and proceed (the provider must handle missing data gracefully).

- [ ] **Step 6: Commit**

```bash
git add src/research/external/cross_asset_backfill.py tests/research/external/test_cross_asset_backfill.py
git commit -m "feat: cross-asset backfill CLI for DXY/10Y/SPX"
```

### Task 2.2: CrossAssetFeatureProvider

**Files:**
- Create: `src/research/features/cross_asset/__init__.py`
- Create: `src/research/features/cross_asset/provider.py`
- Test: `tests/research/features/cross_asset/test_provider.py`

- [ ] **Step 1: Write the failing test**

Create `tests/research/features/cross_asset/__init__.py` (empty).
Create `tests/research/features/cross_asset/test_provider.py`:
```python
"""CrossAssetFeatureProvider — DXY/10Y/SPX daily features joined to intraday bars.

Output features (9 fields, 3 per asset):
  dxy_return_1d, dxy_return_5d, dxy_zscore_20d
  tnx_return_1d, tnx_return_5d, tnx_zscore_20d
  spx_return_1d, spx_return_5d, spx_zscore_20d

return_1d / return_5d are simple close-to-close returns; zscore_20d is the
asset's own price-return volatility-normalized signal.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import pytest

from src.research.features.cross_asset.provider import CrossAssetFeatureProvider
from src.research.features.protocol import FeatureRole


class _FakeMatrix:
    def __init__(self, bar_times: List[datetime]):
        self.bar_times = bar_times
        self.n_bars = len(bar_times)
        self.indicator_series: dict = {}


def _bar_times_h1(days: int = 5) -> List[datetime]:
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    return [
        base + timedelta(days=d, hours=h)
        for d in range(days)
        for h in range(24)
    ]


def test_role_mapping_covers_all_nine_fields() -> None:
    p = CrossAssetFeatureProvider(daily_close_lookup=lambda s, d: None)
    roles = p.role_mapping()
    expected_fields = {
        f"{a}_{m}"
        for a in ("dxy", "tnx", "spx")
        for m in ("return_1d", "return_5d", "zscore_20d")
    }
    assert set(roles) == expected_fields


def test_compute_emits_one_value_per_bar() -> None:
    bars = _bar_times_h1(days=10)
    matrix = _FakeMatrix(bars)
    p = CrossAssetFeatureProvider(daily_close_lookup=lambda s, d: 100.0)
    out = p.compute(matrix)
    for key, values in out.items():
        assert len(values) == matrix.n_bars


def test_return_1d_computed_when_yesterday_close_present() -> None:
    bars = _bar_times_h1(days=2)
    matrix = _FakeMatrix(bars)
    closes = {
        ("DX-Y.NYB", date(2026, 4, 1)): 100.0,
        ("DX-Y.NYB", date(2026, 4, 2)): 101.0,
        ("^TNX", date(2026, 4, 1)): 4.0,
        ("^TNX", date(2026, 4, 2)): 4.05,
        ("^GSPC", date(2026, 4, 1)): 5000.0,
        ("^GSPC", date(2026, 4, 2)): 5050.0,
    }
    p = CrossAssetFeatureProvider(daily_close_lookup=lambda s, d: closes.get((s, d)))
    out = p.compute(matrix)

    # Day 1 (no yesterday): return_1d = None
    day1_idx = 0
    assert out[("cross_asset", "dxy_return_1d")][day1_idx] is None

    # Day 2: return_1d = 101/100 - 1 = 0.01
    day2_idx = 24
    val = out[("cross_asset", "dxy_return_1d")][day2_idx]
    assert val is not None and abs(val - 0.01) < 1e-9
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/research/features/cross_asset/test_provider.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write the implementation**

Create `src/research/features/cross_asset/__init__.py`:
```python
from src.research.features.cross_asset.provider import CrossAssetFeatureProvider

__all__ = ["CrossAssetFeatureProvider"]
```

Create `src/research/features/cross_asset/provider.py`:
```python
"""CrossAssetFeatureProvider — daily DXY/10Y/SPX features for each XAUUSD bar.

Joins each intraday bar's date to the daily close of three macro symbols:
- DXY (DX-Y.NYB): US dollar index, classic inverse correlation with gold
- ^TNX: 10-year US Treasury yield, inverse to gold via real-rate channel
- ^GSPC: S&P 500, risk-on/off signal

Three features per asset = 9 total: return_1d, return_5d, zscore_20d.

All values None when daily_close_lookup returns None (weekend / holiday /
pre-data window) or when the rolling window lacks history.
"""
from __future__ import annotations

import statistics
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.research.features.protocol import (
    FeatureProvider,
    FeatureRole,
    ProviderDataRequirement,
)


_SYMBOLS: Tuple[Tuple[str, str], ...] = (
    ("dxy", "DX-Y.NYB"),
    ("tnx", "^TNX"),
    ("spx", "^GSPC"),
)


class CrossAssetFeatureProvider:
    name = "cross_asset"
    feature_count = 9  # 3 features × 3 symbols

    def __init__(
        self,
        daily_close_lookup: Callable[[str, date], Optional[float]],
    ) -> None:
        """daily_close_lookup(symbol, date) → close price or None."""
        self._lookup = daily_close_lookup

    def required_columns(self) -> List[Tuple[str, str]]:
        return []

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        return None

    def role_mapping(self) -> Dict[str, FeatureRole]:
        roles: Dict[str, FeatureRole] = {}
        for prefix, _sym in _SYMBOLS:
            roles[f"{prefix}_return_1d"] = FeatureRole.WHY
            roles[f"{prefix}_return_5d"] = FeatureRole.WHY
            roles[f"{prefix}_zscore_20d"] = FeatureRole.WHEN
        return roles

    def compute(
        self,
        matrix: Any,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        n = matrix.n_bars

        # Distinct dates, ordered, for daily-resolution computation
        ordered_dates: List[date] = []
        seen: set = set()
        for bt in matrix.bar_times:
            d = bt.date()
            if d not in seen:
                seen.add(d)
                ordered_dates.append(d)

        out: Dict[Tuple[str, str], List[Optional[float]]] = {}

        for prefix, symbol in _SYMBOLS:
            return_1d_by_date: Dict[date, Optional[float]] = {}
            return_5d_by_date: Dict[date, Optional[float]] = {}
            zscore_20d_by_date: Dict[date, Optional[float]] = {}

            closes: List[Optional[float]] = [
                self._lookup(symbol, d) for d in ordered_dates
            ]

            for i, d in enumerate(ordered_dates):
                c = closes[i]
                if c is None:
                    return_1d_by_date[d] = None
                    return_5d_by_date[d] = None
                    zscore_20d_by_date[d] = None
                    continue

                # 1-day return
                if i >= 1 and closes[i - 1] is not None and closes[i - 1] > 0:
                    return_1d_by_date[d] = c / closes[i - 1] - 1.0
                else:
                    return_1d_by_date[d] = None

                # 5-day return
                if i >= 5 and closes[i - 5] is not None and closes[i - 5] > 0:
                    return_5d_by_date[d] = c / closes[i - 5] - 1.0
                else:
                    return_5d_by_date[d] = None

                # 20-day zscore of 1-day returns
                history_returns: List[float] = []
                for j in range(max(0, i - 20), i):
                    if (
                        closes[j] is not None
                        and j >= 1
                        and closes[j - 1] is not None
                        and closes[j - 1] > 0
                    ):
                        history_returns.append(closes[j] / closes[j - 1] - 1.0)
                if len(history_returns) >= 20 and return_1d_by_date[d] is not None:
                    mean_r = sum(history_returns) / len(history_returns)
                    std_r = statistics.pstdev(history_returns)
                    zscore_20d_by_date[d] = (
                        (return_1d_by_date[d] - mean_r) / std_r if std_r > 0 else 0.0
                    )
                else:
                    zscore_20d_by_date[d] = None

            # Init the 3 output arrays
            out[("cross_asset", f"{prefix}_return_1d")] = [None] * n
            out[("cross_asset", f"{prefix}_return_5d")] = [None] * n
            out[("cross_asset", f"{prefix}_zscore_20d")] = [None] * n

            # Broadcast daily values to per-bar arrays
            for bar_i, bt in enumerate(matrix.bar_times):
                d = bt.date()
                out[("cross_asset", f"{prefix}_return_1d")][bar_i] = return_1d_by_date.get(d)
                out[("cross_asset", f"{prefix}_return_5d")][bar_i] = return_5d_by_date.get(d)
                out[("cross_asset", f"{prefix}_zscore_20d")][bar_i] = zscore_20d_by_date.get(d)

        return out
```

- [ ] **Step 4: Run the test — confirm it passes**

```bash
python -m pytest tests/research/features/cross_asset/test_provider.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/research/features/cross_asset/ tests/research/features/cross_asset/
git commit -m "feat: CrossAssetFeatureProvider with 9 DXY/10Y/SPX features"
```

### Task 2.3: Wire CrossAssetFeatureProvider into FeatureHub

**Files:**
- Modify: `src/research/features/hub.py` (mirror Task 1.6)
- Modify: `config/research.ini` (add `cross_asset = true`)
- Modify: `src/config/models/research.py` (add `cross_asset: bool = False`)

- [ ] **Step 1: Add factory + lookup helper**

In `src/research/features/hub.py`, add the import:
```python
from src.research.features.cross_asset import CrossAssetFeatureProvider
```

Add the helper above `_PROVIDER_FACTORIES`:
```python
def _make_cross_asset_close_lookup():
    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter

    writer = TimescaleWriter(load_db_settings("live"))
    cache: dict = {}

    def lookup(symbol, d):
        key = (symbol, d)
        if key in cache:
            return cache[key]
        with writer.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT close FROM daily_external_ohlc WHERE symbol=%s AND date=%s",
                (symbol, d),
            )
            row = cur.fetchone()
            cache[key] = float(row[0]) if row and row[0] is not None else None
        return cache[key]

    return lookup
```

In `_PROVIDER_FACTORIES`:
```python
"cross_asset": lambda: CrossAssetFeatureProvider(
    daily_close_lookup=_make_cross_asset_close_lookup(),
),
```

- [ ] **Step 2: Add config field + ini entry**

Add `cross_asset: bool = False` to `FeatureProvidersConfig` (mirror Task 1.6 Step 1).
Add to `config/research.ini` `[feature_providers]`:
```ini
cross_asset = true

[feature_providers.cross_asset]
# DXY (DX-Y.NYB), 10Y (^TNX), SPX (^GSPC) daily OHLCV.
# Backfilled via `python -m src.research.external.cross_asset_backfill --environment live`.
```

- [ ] **Step 3: Smoke test**

```bash
python -c "
from src.research.core.config import load_research_config
from src.research.features.hub import FeatureHub
hub = FeatureHub(load_research_config())
print(hub.describe())
"
```
Expected: `cross_asset` appears with `feature_count: 9`.

- [ ] **Step 4: Run all feature tests**

```bash
python -m pytest tests/research/features/ tests/research/core/ -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/research/features/hub.py src/config/models/research.py config/research.ini
git commit -m "feat: register CrossAssetFeatureProvider in FeatureHub"
```

### Task 2.4: Mining + state_edge re-run with full extended pool

**Files:** None (CLI invocation + new artifacts)

- [ ] **Step 1: Mining run with all 9 providers (incl cme_volume + cross_asset)**

```bash
python -m src.ops.cli.mining_runner \
  --environment live \
  --tf H1 \
  --start 2025-04-01 --end 2026-04-15 \
  --providers temporal,microstructure,cross_tf,regime_transition,session_event,intrabar,candle_patterns,cme_volume,cross_asset \
  --child-tf M5 \
  --json-output data/research/phase2_mining_h1_full.json
```

Expected: provider_summary shows ~134 features (121 + 4 + 9). Mining completes; check whether any rules use `cme_volume_*` or `cross_asset_*` fields.

- [ ] **Step 2: state_edge re-run with full feature pool**

```bash
python -m src.ops.cli.state_edge_lab \
  --environment live \
  --tf H1 \
  --start 2025-04-01 --end 2026-04-15 \
  --backend cpu \
  --json-output artifacts/phase2_state_edge_h1_full.json \
  --no-auto-backfill
```

Expected: artifact written; record model_id + accuracy.

- [ ] **Step 3: Compare baseline (Phase 0) vs full (Phase 2) state_edge artifacts**

```bash
python -c "
import json
with open('artifacts/phase0_state_edge_h1_baseline.json') as f: a=json.load(f)
with open('artifacts/phase2_state_edge_h1_full.json') as f: b=json.load(f)
def model_meta(p):
    r = p.get('results', [{}])[0]
    return {
        'feature_count': r.get('feature_count'),
        'accuracy': r.get('model_metrics', {}).get('accuracy'),
        'precision_buy': r.get('model_metrics', {}).get('precision_buy'),
        'precision_sell': r.get('model_metrics', {}).get('precision_sell'),
    }
print('baseline:', model_meta(a))
print('full:    ', model_meta(b))
"
```

Record the deltas. Improvement of accuracy/precision in full vs baseline = evidence that new features carry signal.

- [ ] **Step 4: Run state_edge_overlay_report on both artifacts**

```bash
python -m src.ops.cli.state_edge_overlay_report \
  --baseline-artifact artifacts/phase0_state_edge_h1_baseline.json \
  --full-artifact artifacts/phase2_state_edge_h1_full.json \
  --json-output artifacts/phase2_overlay_comparison.json
```

If the CLI doesn't support comparison directly: run two separate `state_edge_overlay_report` invocations against the same backtest baseline and diff manually.

- [ ] **Step 5: Decision gate + snapshot**

Append a comprehensive section to `docs/research/2026-05-03-high-freq-architecture-audit.md` with:
- Phase 0 / Phase 1 / Phase 2 mining + state_edge metric comparison table
- Whether new features moved any quality metric meaningfully
- Verdict: (a) ready to design new strategies on top of this richer feature pool, (b) need more data sources, or (c) ML pipeline itself needs tuning

- [ ] **Step 6: Commit**

```bash
git add data/research/phase2_*.json artifacts/phase2_*.json docs/research/2026-05-03-high-freq-architecture-audit.md
git commit -m "docs: phase 2 cross-asset + cme_volume mining/state_edge results"
```

---

## Phase 3: Decision Gate + Strategy Design Handoff

**Purpose:** With Phases 0-2 complete, decide whether the expanded feature pool is enough to design a real high-frequency strategy, or whether more infrastructure (real tick data, news semantic, etc) is needed.

### Task 3.1: Final architecture audit update

**Files:**
- Modify: `docs/research/2026-05-03-high-freq-architecture-audit.md`

- [ ] **Step 1: Write the verdict section**

Append:
```markdown
## Final Verdict (Phase 3)

### What changed
- Feature pool: 121 → 134 (added 4 cme_volume + 9 cross_asset)
- New data sources: CME GC daily volume, DXY/10Y/SPX daily close
- New CLIs: `cme_backfill`, `cross_asset_backfill`
- Mining + state_edge artifacts re-run with extended pool

### Quantitative result (per Phase 0/1/2 artifact comparisons)
[Fill in concrete numbers from the artifact comparisons]

### Qualitative result
[Did mining find new rules using new features? Did state_edge accuracy improve?]

### Next-step decision
[Pick one of the following based on results:]
- (A) Pool now sufficient — design 2-3 baseline strategies on top + run entry_meta lab
- (B) Pool insufficient — need more data sources (sentiment, news semantic)
- (C) ML pipeline needs algorithmic improvement (boosting / DL) before more data
```

- [ ] **Step 2: Commit**

```bash
git add docs/research/2026-05-03-high-freq-architecture-audit.md
git commit -m "docs: phase 3 final verdict on feature pool expansion"
```

### Task 3.2: Hand off to next-iteration plan

- [ ] **Step 1: Brainstorm + write the next plan**

Based on the Phase 3 verdict, invoke `superpowers:brainstorming` to scope the next iteration:
- If verdict (A): plan 2-3 baseline high-frequency strategies + entry_meta workflow
- If verdict (B): plan additional data source ingestion
- If verdict (C): plan algorithmic upgrade (e.g., XGBoost mining provider)

Save the next plan as `docs/superpowers/plans/<date>-<next-feature>.md`.

- [ ] **Step 2: Commit the new plan**

```bash
git add docs/superpowers/plans/<new-plan>.md
git commit -m "docs: <next-iteration> implementation plan"
```

---

## Self-Review Checklist (per writing-plans skill)

**Spec coverage:**
- [x] Phase 0 sanity gate: state_edge end-to-end with current features
- [x] Phase 1: CME GC volume — schema, ingestor, provider, hub wiring, mining run
- [x] Phase 2: Cross-asset (DXY/10Y/SPX) — backfill, provider, hub wiring, mining + state_edge
- [x] Phase 3: Decision gate + handoff
- [x] All file paths concrete; all code shown verbatim; no "TODO" or "fill in"

**Type consistency:**
- [x] `DailyBar` fields consistent across `yfinance_client.py`, `cme_backfill.py`, `cross_asset_backfill.py`
- [x] `INSERT_SQL` from `daily_external_ohlc.py` consumed identically in both backfill CLIs
- [x] `_PROVIDER_FACTORIES` extension pattern same for both new providers
- [x] `daily_volume_lookup` (Task 1.5) and `daily_close_lookup` (Task 2.2) signatures distinct + documented

**Open issue (not gating):** `FeatureProvidersConfig` field addition (Task 1.6 Step 1, Task 2.3 Step 2) requires reading the existing pydantic model first — the plan does not pre-emptively show its source code because the existing pattern dictates style. The engineer is told exactly which file to read and what field type to add.

---

## Out of scope (for separate future plans)

- News sentiment NLP feature provider
- Tick data ingestor (broker MT5 API limitation)
- Algorithmic mining upgrade (XGBoost / SHAP)
- live runtime EntryMeta integration (separate plan, depends on Phase 3 verdict)
- TradeFrequency rule per-strategy cap (separate plan, prerequisite for high-freq deployment)
