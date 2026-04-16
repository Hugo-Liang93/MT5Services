"""新增派生特征（order-flow 代理、时段、多 bar 结构）正确性测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.research.features.engineer import build_default_engineer


def _build_matrix(bars: list[dict[str, Any]]) -> Any:
    """构造最小 DataMatrix stub（只填充特征依赖的字段）。"""
    from src.research.core.data_matrix import DataMatrix

    return DataMatrix(
        symbol="XAUUSD",
        timeframe="M15",
        n_bars=len(bars),
        bar_times=[b["time"] for b in bars],
        opens=[b["open"] for b in bars],
        highs=[b["high"] for b in bars],
        lows=[b["low"] for b in bars],
        closes=[b["close"] for b in bars],
        volumes=[b.get("volume", 100.0) for b in bars],
        indicators=[{} for _ in bars],
        regimes=["unknown"] * len(bars),
        soft_regimes=[None] * len(bars),
        forward_returns={},
        indicator_series={
            ("atr14", "atr"): [10.0] * len(bars),
        },
    )


def _bar(
    hour: int = 10,
    open_: float = 2000,
    high: float = 2010,
    low: float = 1990,
    close: float = 2005,
    volume: float = 100.0,
) -> dict[str, Any]:
    return {
        "time": datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _compute(feature_name: str, matrix: Any, i: int) -> Any:
    eng = build_default_engineer()
    defn = eng.definition(feature_name)
    assert defn is not None, f"feature {feature_name} not registered"
    return defn.func(matrix, i)


class TestOrderFlowProxies:
    def test_close_in_range_strong_close(self) -> None:
        m = _build_matrix([_bar(close=2010, high=2010, low=1990)])
        assert _compute("close_in_range", m, 0) == pytest.approx(1.0)

    def test_close_in_range_weak_close(self) -> None:
        m = _build_matrix([_bar(close=1990, high=2010, low=1990)])
        assert _compute("close_in_range", m, 0) == pytest.approx(0.0)

    def test_body_ratio_full_body(self) -> None:
        m = _build_matrix([_bar(open_=1990, close=2010, high=2010, low=1990)])
        assert _compute("body_ratio", m, 0) == pytest.approx(1.0)

    def test_body_ratio_doji(self) -> None:
        m = _build_matrix([_bar(open_=2000, close=2000, high=2010, low=1990)])
        assert _compute("body_ratio", m, 0) == pytest.approx(0.0)

    def test_upper_wick_only(self) -> None:
        # open=close=1990, high=2010, low=1990 → upper wick = 20, range = 20
        m = _build_matrix([_bar(open_=1990, close=1990, high=2010, low=1990)])
        assert _compute("upper_wick_ratio", m, 0) == pytest.approx(1.0)
        assert _compute("lower_wick_ratio", m, 0) == pytest.approx(0.0)

    def test_oc_imbalance_bullish(self) -> None:
        m = _build_matrix([_bar(open_=1990, close=2010, high=2010, low=1990)])
        assert _compute("oc_imbalance", m, 0) == pytest.approx(1.0)

    def test_range_expansion(self) -> None:
        # range = 20, atr14 = 10 → 2.0
        m = _build_matrix([_bar(high=2010, low=1990)])
        assert _compute("range_expansion", m, 0) == pytest.approx(2.0)


class TestSessionFeatures:
    def test_asia_phase(self) -> None:
        m = _build_matrix([_bar(hour=3)])
        assert _compute("session_phase", m, 0) == 0.0
        assert _compute("london_session", m, 0) == 0.0
        assert _compute("ny_session", m, 0) == 0.0

    def test_london_phase(self) -> None:
        m = _build_matrix([_bar(hour=9)])
        assert _compute("session_phase", m, 0) == 1.0
        assert _compute("london_session", m, 0) == 1.0

    def test_ny_phase(self) -> None:
        m = _build_matrix([_bar(hour=14)])
        assert _compute("session_phase", m, 0) == 2.0
        assert _compute("ny_session", m, 0) == 1.0
        assert _compute("london_session", m, 0) == 1.0  # 重叠

    def test_day_progress(self) -> None:
        m = _build_matrix([_bar(hour=12)])
        assert _compute("day_progress", m, 0) == pytest.approx(0.5)


class TestMultiBarStructure:
    def test_close_to_close_3_uptrend(self) -> None:
        bars = [_bar(close=2000) for _ in range(4)]
        bars[0]["close"] = 1980
        bars[3]["close"] = 2020
        m = _build_matrix(bars)
        # close[3]=2020, close[0]=1980 → (2020-1980)/1980
        assert _compute("close_to_close_3", m, 3) == pytest.approx(40.0 / 1980.0)

    def test_consecutive_3_up_bars(self) -> None:
        bars = [
            _bar(open_=1990, close=2000),
            _bar(open_=2000, close=2010),
            _bar(open_=2010, close=2020),
        ]
        m = _build_matrix(bars)
        assert _compute("consecutive_same_color", m, 2) == 3.0

    def test_consecutive_down_bars(self) -> None:
        bars = [
            _bar(open_=2020, close=2010),
            _bar(open_=2010, close=2000),
        ]
        m = _build_matrix(bars)
        assert _compute("consecutive_same_color", m, 1) == -2.0


class TestEconomicEventFeatures:
    def _matrix_with_events(
        self,
        bar_hours: list[int],
        event_hours: list[int],
    ) -> Any:
        from src.research.core.data_matrix import DataMatrix

        bars = [_bar(hour=h) for h in bar_hours]
        event_times = tuple(
            datetime(2026, 1, 1, h, 0, tzinfo=timezone.utc) for h in event_hours
        )
        return DataMatrix(
            symbol="XAUUSD",
            timeframe="M15",
            n_bars=len(bars),
            bar_times=[b["time"] for b in bars],
            opens=[b["open"] for b in bars],
            highs=[b["high"] for b in bars],
            lows=[b["low"] for b in bars],
            closes=[b["close"] for b in bars],
            volumes=[100.0] * len(bars),
            indicators=[{} for _ in bars],
            regimes=["unknown"] * len(bars),
            soft_regimes=[None] * len(bars),
            forward_returns={},
            indicator_series={("atr14", "atr"): [10.0] * len(bars)},
            high_impact_event_times=event_times,
        )

    def test_bars_to_next_event_quarter_hour_bars(self) -> None:
        # bar at 10:00, event at 11:00 → 60 min / 15 = 4 bars
        m = self._matrix_with_events([10], [11])
        assert _compute("bars_to_next_high_impact_event", m, 0) == pytest.approx(4.0)

    def test_bars_since_last_event(self) -> None:
        m = self._matrix_with_events([12], [11])
        assert _compute("bars_since_last_high_impact_event", m, 0) == pytest.approx(4.0)

    def test_in_news_window_inside_pre(self) -> None:
        # bar at 10:45, event at 11:00 → 15 min前 → 在窗口
        from src.research.core.data_matrix import DataMatrix

        bar = _bar()
        bar["time"] = datetime(2026, 1, 1, 10, 45, tzinfo=timezone.utc)
        m = DataMatrix(
            symbol="XAUUSD",
            timeframe="M15",
            n_bars=1,
            bar_times=[bar["time"]],
            opens=[bar["open"]], highs=[bar["high"]],
            lows=[bar["low"]], closes=[bar["close"]],
            volumes=[100.0], indicators=[{}], regimes=["unknown"],
            soft_regimes=[None], forward_returns={},
            indicator_series={("atr14", "atr"): [10.0]},
            high_impact_event_times=(datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc),),
        )
        assert _compute("in_news_window", m, 0) == 1.0

    def test_in_news_window_outside(self) -> None:
        m = self._matrix_with_events([8], [12])
        # 8:00 距 12:00 = 4h，远大于 30 分钟
        assert _compute("in_news_window", m, 0) == 0.0

    def test_no_events_returns_none(self) -> None:
        m = self._matrix_with_events([10], [])
        assert _compute("bars_to_next_high_impact_event", m, 0) is None
        assert _compute("bars_since_last_high_impact_event", m, 0) is None
        assert _compute("in_news_window", m, 0) == 0.0


def test_feature_count_expanded() -> None:
    """确认派生特征池扩充（统计功率底线）；volume 特征已删。"""
    eng = build_default_engineer()
    inv = eng.inventory()
    names = set(inv["active_features"].keys())
    assert len(names) >= 17
    assert "volume_momentum" not in names
    assert "signed_volume" not in names
    assert "bars_to_next_high_impact_event" in names
