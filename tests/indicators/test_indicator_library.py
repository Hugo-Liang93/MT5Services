from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.indicators.core.momentum import stochastic, williams_r
from src.indicators.core.volatility import adx
from src.indicators.core.volume import mfi


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _bars(count: int = 80) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = []
    price = 2600.0
    for idx in range(count):
        drift = 1.2 if idx % 7 else -0.6
        close = price + drift
        high = max(price, close) + 0.8
        low = min(price, close) - 0.7
        bars.append(
            Bar(
                time=start + timedelta(minutes=idx),
                open=price,
                high=high,
                low=low,
                close=close,
                volume=1000 + idx * 25,
            )
        )
        price = close
    return bars


def test_stochastic_indicator_returns_k_and_d():
    result = stochastic(_bars(), {"k_period": 14, "d_period": 3})

    assert set(result) == {"stoch_k", "stoch_d"}


def test_williams_r_indicator_returns_value():
    result = williams_r(_bars(), {"period": 14})

    assert "williams_r" in result


def test_adx_indicator_returns_directional_values():
    result = adx(_bars(), {"period": 14})

    assert set(result) == {"adx", "plus_di", "minus_di"}


def test_mfi_indicator_returns_value():
    result = mfi(_bars(), {"period": 14})

    assert "mfi" in result
