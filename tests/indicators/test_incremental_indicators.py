"""Tests for incremental EMA and ATR computation.

These tests verify that:
1. EmaIncremental and AtrIncremental produce correct values on full computation.
2. After seeding via _compute_full, each subsequent bar close uses the O(1)
   incremental path and matches the result of a fresh full computation.
3. The manager's _load_incremental_class wires up the right classes for
   compute_mode=incremental indicators and returns None for others.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from src.indicators.cache.incremental import IndicatorState, IncrementalIndicator
from src.indicators.core.mean import EmaIncremental, ema
from src.indicators.core.volatility import AtrIncremental, atr


# ---------------------------------------------------------------------------
# Minimal OHLC stub
# ---------------------------------------------------------------------------


class Bar:
    def __init__(self, close: float, high: float = 0.0, low: float = 0.0, t: int = 0):
        self.close = close
        self.high = high if high else close + 0.5
        self.low = low if low else close - 0.5
        self.open = close
        self.volume = 100
        self.time = datetime.fromtimestamp(t, tz=timezone.utc)


def _bars(closes: list[float], step: int = 60) -> list[Bar]:
    """Build a list of Bar objects with incrementing timestamps."""
    return [Bar(c, t=i * step) for i, c in enumerate(closes)]


# ---------------------------------------------------------------------------
# EmaIncremental
# ---------------------------------------------------------------------------


class TestEmaIncremental:
    PARAMS = {"period": 5}

    def test_full_matches_ema_function(self) -> None:
        bars = _bars([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        ind = EmaIncremental("ema", self.PARAMS)
        result = ind._compute_full(bars)
        expected = ema(bars, self.PARAMS)
        assert result == expected

    def test_incremental_matches_full_after_one_new_bar(self) -> None:
        # Seed on first 10 bars
        history = _bars(list(range(1, 11)))
        ind = EmaIncremental("ema", self.PARAMS)

        # First compute: full path, seeds state
        r_full = ind.compute(history, "SYM", "H1")
        assert "ema" in r_full
        state = ind.get_state("SYM", "H1")
        assert state is not None

        # Add one new bar with a fresh timestamp
        new_bar = Bar(15.0, t=10 * 60)
        extended = history + [new_bar]

        # Incremental result
        r_incr = ind.compute(extended, "SYM", "H1")

        # Reference: full recompute on same extended bars
        r_ref = ema(extended, self.PARAMS)
        assert pytest.approx(r_incr["ema"], rel=1e-9) == r_ref["ema"]

    def test_incremental_state_is_updated_each_bar(self) -> None:
        bars = _bars(list(range(1, 16)))
        ind = EmaIncremental("ema", self.PARAMS)
        ind.compute(bars[:10], "SYM", "M5")
        prev_state = ind.get_state("SYM", "M5")
        assert prev_state is not None

        new_bar = Bar(20.0, t=10 * 60)
        ind.compute(bars[:10] + [new_bar], "SYM", "M5")
        new_state = ind.get_state("SYM", "M5")
        assert new_state is not None
        assert new_state.value != prev_state.value

    def test_intrabar_and_confirmed_use_separate_states(self) -> None:
        """intrabar and confirmed must not share last_bar_time.

        Previously both scopes wrote the same state key, so an intrabar call
        on bar T would stamp last_bar_time=T, causing the subsequent confirmed
        call for the same bar T to fall back to full recompute (time not
        advanced).  With scope-isolated keys each path evolves independently.
        """
        bars = _bars(list(range(1, 12)))   # 11 bars; bar #11 is "in progress"
        history = bars[:10]                # 10 closed bars
        intrabar_bar = bars[10]            # bar #11 currently in progress

        ind = EmaIncremental("ema", self.PARAMS)

        # Seed confirmed state on the first 10 bars
        ind.compute(history, "SYM", "H1", scope="confirmed")
        confirmed_state_before = ind.get_state("SYM", "H1", scope="confirmed")
        assert confirmed_state_before is not None

        # Simulate intrabar firing for bar #11 (same bar, different scope)
        extended = history + [intrabar_bar]
        ind.compute(extended, "SYM", "H1", scope="intrabar")

        # The confirmed state's last_bar_time must be unchanged (bar #10)
        confirmed_state_after = ind.get_state("SYM", "H1", scope="confirmed")
        assert confirmed_state_after is not None
        assert confirmed_state_after.last_bar_time == confirmed_state_before.last_bar_time

        # Now fire confirmed for bar #11 — last_bar_time advanced, so incremental fires
        ind.compute(extended, "SYM", "H1", scope="confirmed")
        confirmed_state_final = ind.get_state("SYM", "H1", scope="confirmed")
        assert confirmed_state_final is not None
        assert confirmed_state_final.last_bar_time > confirmed_state_before.last_bar_time


# ---------------------------------------------------------------------------
# AtrIncremental
# ---------------------------------------------------------------------------


class TestAtrIncremental:
    PARAMS = {"period": 3}

    def _hlc_bars(self, n: int, step: float = 1.0) -> list[Bar]:
        """Bars where H = close+1, L = close-1 so TR is always 2 (unless gap)."""
        return [
            Bar(float(i) * step, high=float(i) * step + 1, low=float(i) * step - 1, t=i * 60)
            for i in range(1, n + 1)
        ]

    def test_full_matches_atr_function(self) -> None:
        bars = self._hlc_bars(10)
        ind = AtrIncremental("atr", self.PARAMS)
        result = ind._compute_full(bars)
        expected = atr(bars, self.PARAMS)
        assert result == expected

    def test_incremental_matches_wilders_formula(self) -> None:
        """After seeding, new bar's ATR follows Wilder's smoothing."""
        bars = self._hlc_bars(8)
        ind = AtrIncremental("atr", self.PARAMS)

        # Seed state
        r0 = ind.compute(bars, "SYM", "H4")
        assert "atr" in r0
        state = ind.get_state("SYM", "H4")
        assert state is not None
        assert state.intermediate_results is not None
        assert "prev_close" in state.intermediate_results

        # Add one new bar
        new_bar = Bar(10.0, high=11.5, low=8.5, t=8 * 60)
        extended = bars + [new_bar]

        r_incr = ind.compute(extended, "SYM", "H4")

        # Manual Wilder's calc for verification
        period = self.PARAMS["period"]
        prev_close = float(state.intermediate_results["prev_close"])
        tr = max(
            new_bar.high - new_bar.low,
            abs(new_bar.high - prev_close),
            abs(new_bar.low - prev_close),
        )
        expected_atr = (float(state.value) * (period - 1) + tr) / period
        assert pytest.approx(r_incr["atr"], rel=1e-9) == expected_atr

    def test_prev_close_saved_in_state(self) -> None:
        bars = self._hlc_bars(6)
        ind = AtrIncremental("atr", self.PARAMS)
        ind.compute(bars, "SYM", "D1")
        state = ind.get_state("SYM", "D1")
        assert state is not None
        assert state.intermediate_results is not None
        assert state.intermediate_results["prev_close"] == bars[-1].close


# ---------------------------------------------------------------------------
# Manager._load_incremental_class convention
# ---------------------------------------------------------------------------


class TestManagerIncrementalClassLoader:
    def _make_manager(self):
        """Build a minimal UnifiedIndicatorManager with mocked dependencies."""
        from src.config.indicator_config import UnifiedIndicatorConfig, get_global_config_manager

        mock_market = MagicMock()
        mock_market.market_settings.intrabar_max_points = 100

        with patch("src.indicators.manager.get_global_pipeline") as mock_pipe, \
             patch("src.indicators.manager.get_global_dependency_manager") as mock_dep, \
             patch("src.indicators.manager.get_event_store") as mock_es, \
             patch("src.indicators.manager.get_global_collector"):
            mock_pipe.return_value = MagicMock()
            mock_dep.return_value = MagicMock()
            mock_es.return_value = MagicMock()

            from src.indicators.manager import UnifiedIndicatorManager

            cfg = get_global_config_manager().config
            manager = UnifiedIndicatorManager.__new__(UnifiedIndicatorManager)
            manager.config = cfg
            manager._indicator_funcs = {}
            manager._intrabar_eligible_cache = None
            manager.pipeline = mock_pipe.return_value
            manager.dependency_manager = mock_dep.return_value
            return manager

    def test_returns_ema_incremental_for_ema(self):
        from src.config.indicator_config import ComputeMode, IndicatorConfig
        from src.indicators.core.mean import EmaIncremental
        from src.indicators.manager import UnifiedIndicatorManager

        mock_market = MagicMock()
        mock_market.market_settings.intrabar_max_points = 100

        with patch("src.indicators.manager.get_global_pipeline") as mock_pipe, \
             patch("src.indicators.manager.get_global_dependency_manager"), \
             patch("src.indicators.manager.get_event_store") as mock_es, \
             patch("src.indicators.manager.get_global_collector"):
            mock_pipe.return_value = MagicMock()
            mock_es.return_value = MagicMock()

            manager = UnifiedIndicatorManager.__new__(UnifiedIndicatorManager)
            manager._indicator_funcs = {}
            manager._intrabar_eligible_cache = None
            manager.pipeline = mock_pipe.return_value

            cfg = IndicatorConfig(
                name="ema50",
                func_path="src.indicators.core.mean.ema",
                params={"period": 50},
                compute_mode=ComputeMode.INCREMENTAL,
            )
            cls = manager._load_incremental_class(cfg)
            assert cls is EmaIncremental

    def test_returns_atr_incremental_for_atr(self):
        from src.config.indicator_config import ComputeMode, IndicatorConfig
        from src.indicators.core.volatility import AtrIncremental
        from src.indicators.manager import UnifiedIndicatorManager

        with patch("src.indicators.manager.get_global_pipeline") as mock_pipe, \
             patch("src.indicators.manager.get_global_dependency_manager"), \
             patch("src.indicators.manager.get_event_store") as mock_es, \
             patch("src.indicators.manager.get_global_collector"):
            mock_pipe.return_value = MagicMock()
            mock_es.return_value = MagicMock()

            manager = UnifiedIndicatorManager.__new__(UnifiedIndicatorManager)
            manager._indicator_funcs = {}
            manager._intrabar_eligible_cache = None
            manager.pipeline = mock_pipe.return_value

            cfg = IndicatorConfig(
                name="atr14",
                func_path="src.indicators.core.volatility.atr",
                params={"period": 14},
                compute_mode=ComputeMode.INCREMENTAL,
            )
            cls = manager._load_incremental_class(cfg)
            assert cls is AtrIncremental

    def test_returns_none_for_standard_mode(self):
        from src.config.indicator_config import ComputeMode, IndicatorConfig
        from src.indicators.manager import UnifiedIndicatorManager

        with patch("src.indicators.manager.get_global_pipeline") as mock_pipe, \
             patch("src.indicators.manager.get_global_dependency_manager"), \
             patch("src.indicators.manager.get_event_store") as mock_es, \
             patch("src.indicators.manager.get_global_collector"):
            mock_pipe.return_value = MagicMock()
            mock_es.return_value = MagicMock()

            manager = UnifiedIndicatorManager.__new__(UnifiedIndicatorManager)
            manager._indicator_funcs = {}
            manager._intrabar_eligible_cache = None
            manager.pipeline = mock_pipe.return_value

            cfg = IndicatorConfig(
                name="sma20",
                func_path="src.indicators.core.mean.sma",
                params={"period": 20},
                compute_mode=ComputeMode.STANDARD,
            )
            assert manager._load_incremental_class(cfg) is None
