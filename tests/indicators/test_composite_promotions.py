from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.clients.mt5_market import OHLC
from src.config.indicator_config import ConfigLoader
from src.indicators.core.composite import momentum_consensus
from src.research.features.protocol import PROMOTED_INDICATOR_PRECEDENTS


def _make_bars() -> list[OHLC]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars: list[OHLC] = []
    price = 2600.0
    for i in range(80):
        price += 1.2
        bars.append(
            OHLC(
                symbol="XAUUSD",
                timeframe="M30",
                time=start + timedelta(minutes=30 * i),
                open=price - 0.8,
                high=price + 1.5,
                low=price - 1.2,
                close=price,
                volume=100 + i,
            )
        )
    return bars


def test_momentum_consensus_indicator_emits_bullish_vote_on_monotonic_trend() -> None:
    result = momentum_consensus(_make_bars(), {"min_bars": 50})

    assert result["momentum_consensus"] > 0.0
    assert result["bullish_votes"] >= result["bearish_votes"]


def test_promoted_indicator_is_visible_in_config_and_research_inventory() -> None:
    config = ConfigLoader.load("config/indicators.json")
    indicators = {indicator.name for indicator in config.indicators}
    precedent_names = {
        item["promoted_indicator_name"]
        for item in PROMOTED_INDICATOR_PRECEDENTS
    }

    assert "momentum_consensus14" in indicators
    assert "momentum_consensus14" in precedent_names
