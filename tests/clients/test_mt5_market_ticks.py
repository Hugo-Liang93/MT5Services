from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np

from src.clients import mt5_market
from src.clients.mt5_market import MT5MarketClient, Tick


def _client() -> MT5MarketClient:
    client = object.__new__(MT5MarketClient)
    client.settings = SimpleNamespace(tick_initial_lookback_seconds=20)
    client.metrics = SimpleNamespace(record=lambda *args, **kwargs: None)
    client.connect = lambda: None
    client._get_field = lambda obj, name, default=None: getattr(obj, name, default)
    client._parse_server_timestamp = lambda value: datetime.fromtimestamp(
        int(value), tz=timezone.utc
    )
    client._parse_server_timestamp_msc = lambda value: datetime.fromtimestamp(
        int(value) / 1000, tz=timezone.utc
    )
    return client


def test_get_ticks_preserves_tradeable_tick_fields(monkeypatch) -> None:
    fake_mt5 = SimpleNamespace(
        COPY_TICKS_ALL=0,
        copy_ticks_from=lambda symbol, start, limit, flags: [
            SimpleNamespace(
                time=1_714_000_000,
                time_msc=1_714_000_000_123,
                bid=1.23450,
                ask=1.23462,
                last=1.23458,
                volume=7,
                flags=6,
            )
        ],
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setattr(mt5_market, "mt5", fake_mt5)
    client = _client()

    ticks = client.get_ticks("EURUSD", 1, None)

    assert len(ticks) == 1
    assert ticks[0].symbol == "EURUSD"
    assert ticks[0].bid == 1.23450
    assert ticks[0].ask == 1.23462
    assert ticks[0].last == 1.23458
    assert ticks[0].volume == 7
    assert ticks[0].time == datetime.fromtimestamp(1_714_000_000.123, tz=timezone.utc)
    assert ticks[0].time_msc == 1_714_000_000_123
    assert ticks[0].flags == 6
    assert ticks[0].price == 1.23458


def test_get_ticks_reads_numpy_record_flags_field_before_object_attribute(monkeypatch) -> None:
    dtype = np.dtype(
        [
            ("time", "i8"),
            ("bid", "f8"),
            ("ask", "f8"),
            ("last", "f8"),
            ("volume", "i8"),
            ("time_msc", "i8"),
            ("flags", "i8"),
        ]
    )
    rows = np.array(
        [(1_714_000_000, 1.23450, 1.23462, 1.23458, 7, 1_714_000_000_123, 6)],
        dtype=dtype,
    )
    fake_mt5 = SimpleNamespace(
        COPY_TICKS_ALL=0,
        copy_ticks_from=lambda symbol, start, limit, flags: rows,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setattr(mt5_market, "mt5", fake_mt5)
    client = object.__new__(MT5MarketClient)
    client.settings = SimpleNamespace(tick_initial_lookback_seconds=20)
    client.metrics = SimpleNamespace(record=lambda *args, **kwargs: None)
    client.connect = lambda: None
    client._parse_server_timestamp = lambda value: datetime.fromtimestamp(
        int(value), tz=timezone.utc
    )
    client._parse_server_timestamp_msc = lambda value: datetime.fromtimestamp(
        int(value) / 1000, tz=timezone.utc
    )

    ticks = client.get_ticks("EURUSD", 1, None)

    assert ticks[0].flags == 6
    assert ticks[0].bid == 1.23450
    assert ticks[0].ask == 1.23462


def test_tick_price_falls_back_to_mid_then_side_price() -> None:
    mid_tick = Tick(
        symbol="EURUSD",
        bid=1.1000,
        ask=1.1004,
        last=None,
        volume=1,
        time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        time_msc=1,
        flags=0,
    )
    bid_only_tick = Tick(
        symbol="EURUSD",
        bid=1.1000,
        ask=None,
        last=None,
        volume=1,
        time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        time_msc=1,
        flags=0,
    )

    assert mid_tick.mid == 1.1002
    assert mid_tick.price == 1.1002
    assert bid_only_tick.price == 1.1000
