from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from threading import RLock
from types import SimpleNamespace

from src.indicators.query_services.read import get_snapshot


def test_get_snapshot_uses_indicator_snapshot_for_in_memory_results() -> None:
    timestamp = datetime(2026, 4, 11, 1, 0, tzinfo=timezone.utc)
    manager = SimpleNamespace(
        state=SimpleNamespace(
            results=OrderedDict(
                {
                    "XAUUSD_M1_fast": SimpleNamespace(
                        symbol="XAUUSD",
                        timeframe="M1",
                        name="fast",
                        value=1.0,
                        timestamp=timestamp,
                        bar_time=timestamp,
                        compute_time_ms=2.5,
                    ),
                    "XAUUSD_M1_slow": SimpleNamespace(
                        symbol="XAUUSD",
                        timeframe="M1",
                        name="slow",
                        value=2.0,
                        timestamp=timestamp,
                        bar_time=timestamp,
                        compute_time_ms=2.5,
                    ),
                }
            ),
            results_lock=RLock(),
        ),
        market_service=SimpleNamespace(
            latest_indicators=lambda symbol, timeframe: {},
            get_ohlc_closed=lambda symbol, timeframe, limit=1: [],
        ),
    )

    snapshot = get_snapshot(manager, "XAUUSD", "M1")

    assert snapshot is not None
    assert snapshot.symbol == "XAUUSD"
    assert snapshot.timeframe == "M1"
    assert snapshot.data == {"fast": 1.0, "slow": 2.0}
    assert snapshot.bar_time == timestamp
