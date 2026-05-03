"""yfinance_client thin wrapper — implements ExternalDataSource Protocol.

Network calls mocked; production behavior verified separately.
"""
from __future__ import annotations

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
