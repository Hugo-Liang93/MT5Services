"""yfinance_client thin wrapper — implements ExternalDataSource Protocol.

Network calls mocked; production behavior verified separately.
"""
from __future__ import annotations

import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.research.external import DailyBar, ExternalDataSource, get_source
from src.research.external.yfinance_client import (
    YFinanceClient,
    YFinanceError,
)


def test_yfinance_client_implements_external_data_source_protocol() -> None:
    client = YFinanceClient()
    assert isinstance(client, ExternalDataSource)
    assert client.name == "yfinance"


def test_yfinance_auto_registered_under_canonical_name() -> None:
    # Importing the yfinance_client module must register the source.
    src = get_source("yfinance")
    assert isinstance(src, YFinanceClient)


def test_supports_symbol_returns_true_for_any_string() -> None:
    # yfinance accepts any symbol; if it's invalid Yahoo returns empty data
    # and fetch_daily raises YFinanceError. supports_symbol is permissive.
    client = YFinanceClient()
    assert client.supports_symbol("GC=F") is True
    assert client.supports_symbol("DX-Y.NYB") is True
    assert client.supports_symbol("^TNX") is True


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


def test_history_for_bumps_end_by_one_day_for_inclusive_range() -> None:
    """Yahoo's `end` is exclusive; the wrapper bumps it by 1 day so the
    DailyBar contract is inclusive.

    This test verifies that _history_for passes (start, end+1day) to yfinance,
    ensuring the inclusive [start, end] contract is met (since Yahoo's end
    parameter is exclusive)."""
    import pandas as pd
    from datetime import timedelta

    client = YFinanceClient()
    # Capture what parameters _history_for receives vs what it passes to yfinance
    call_log = {}

    def mock_yf_call(**kwargs):
        call_log["yfinance_call"] = kwargs
        return pd.DataFrame()

    # Patch the yfinance module import to capture the call
    with patch.dict("sys.modules", {"yfinance": MagicMock(Ticker=MagicMock(return_value=MagicMock(history=mock_yf_call)))}):
        with pytest.raises(YFinanceError):
            client.fetch_daily("GC=F", start=date(2026, 4, 1), end=date(2026, 4, 2))

    # Verify _history_for bumped the end date by 1 day before calling yfinance
    yf_kwargs = call_log.get("yfinance_call", {})
    assert yf_kwargs.get("start") == "2026-04-01", f"Expected start='2026-04-01', got {yf_kwargs.get('start')}"
    assert yf_kwargs.get("end") == "2026-04-03", f"Expected end='2026-04-03' (bumped from 2026-04-02), got {yf_kwargs.get('end')}"
    assert yf_kwargs.get("interval") == "1d"
    assert yf_kwargs.get("auto_adjust") is False
    assert yf_kwargs.get("actions") is False
