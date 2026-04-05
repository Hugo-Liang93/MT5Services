"""Unit tests for StrategyPerformanceTracker."""
from __future__ import annotations

import pytest

from src.signals.evaluation.performance import (
    PerformanceTrackerConfig,
    StrategyPerformanceTracker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tracker(
    *,
    enabled: bool = True,
    baseline: float = 0.50,
    min_mult: float = 0.50,
    max_mult: float = 1.20,
    streak_threshold: int = 3,
    category_fallback: int = 3,
    min_samples_for_penalty: int = 1,
) -> StrategyPerformanceTracker:
    cfg = PerformanceTrackerConfig(
        enabled=enabled,
        baseline_win_rate=baseline,
        min_multiplier=min_mult,
        max_multiplier=max_mult,
        streak_penalty_threshold=streak_threshold,
        category_fallback_min_samples=category_fallback,
        min_samples_for_penalty=min_samples_for_penalty,
    )
    tracker = StrategyPerformanceTracker(config=cfg)
    tracker.register_strategy("rsi_reversion", "reversion")
    tracker.register_strategy("stoch_rsi", "reversion")
    tracker.register_strategy("supertrend", "trend")
    tracker.register_strategy("ema_ribbon", "trend")
    return tracker


# ---------------------------------------------------------------------------
# Basic functionality
# ---------------------------------------------------------------------------

class TestBasicRecordAndMultiplier:
    def test_no_data_returns_neutral(self):
        tracker = _make_tracker()
        assert tracker.get_multiplier("rsi_reversion") == 1.0

    def test_disabled_always_returns_one(self):
        tracker = _make_tracker(enabled=False)
        tracker.record_outcome("rsi_reversion", won=True, pnl=5.0)
        tracker.record_outcome("rsi_reversion", won=True, pnl=5.0)
        tracker.record_outcome("rsi_reversion", won=True, pnl=5.0)
        assert tracker.get_multiplier("rsi_reversion") == 1.0

    def test_winning_strategy_gets_boost(self):
        tracker = _make_tracker(category_fallback=1)
        # 4 wins, 1 loss → win_rate = 0.80
        for _ in range(4):
            tracker.record_outcome("rsi_reversion", won=True, pnl=10.0)
        tracker.record_outcome("rsi_reversion", won=False, pnl=-5.0)

        mult = tracker.get_multiplier("rsi_reversion")
        assert mult > 1.0, f"Winning strategy should get boost, got {mult}"
        assert mult <= 1.20, f"Should not exceed max_multiplier, got {mult}"

    def test_losing_strategy_gets_suppressed(self):
        tracker = _make_tracker(category_fallback=1)
        # 1 win, 4 losses → win_rate = 0.20
        tracker.record_outcome("rsi_reversion", won=True, pnl=5.0)
        for _ in range(4):
            tracker.record_outcome("rsi_reversion", won=False, pnl=-5.0)

        mult = tracker.get_multiplier("rsi_reversion")
        assert mult < 1.0, f"Losing strategy should be suppressed, got {mult}"
        assert mult >= 0.50, f"Should not go below min_multiplier, got {mult}"

    def test_multiplier_clamped_to_bounds(self):
        tracker = _make_tracker(min_mult=0.60, max_mult=1.10, category_fallback=1)
        # All wins → should be clamped at max
        for _ in range(10):
            tracker.record_outcome("rsi_reversion", won=True, pnl=10.0)
        assert tracker.get_multiplier("rsi_reversion") <= 1.10

        # All losses → should be clamped at min
        tracker2 = _make_tracker(min_mult=0.60, max_mult=1.10, category_fallback=1)
        for _ in range(10):
            tracker2.record_outcome("supertrend", won=False, pnl=-10.0)
        assert tracker2.get_multiplier("supertrend") >= 0.60


# ---------------------------------------------------------------------------
# Streak penalty
# ---------------------------------------------------------------------------

class TestStreakPenalty:
    def test_losing_streak_suppresses_further(self):
        tracker = _make_tracker(streak_threshold=2, category_fallback=1)
        # Win first to establish baseline, then lose 4 in a row
        tracker.record_outcome("supertrend", won=True, pnl=5.0)
        tracker.record_outcome("supertrend", won=True, pnl=5.0)
        # Record the multiplier before streak
        mult_before_streak = tracker.get_multiplier("supertrend")

        # Now start losing streak
        for _ in range(4):
            tracker.record_outcome("supertrend", won=False, pnl=-5.0)

        mult_after_streak = tracker.get_multiplier("supertrend")
        # Streak penalty should make it even lower
        assert mult_after_streak < mult_before_streak

    def test_winning_streak_does_not_over_boost(self):
        tracker = _make_tracker(max_mult=1.20, category_fallback=1)
        for _ in range(10):
            tracker.record_outcome("rsi_reversion", won=True, pnl=10.0)

        mult = tracker.get_multiplier("rsi_reversion")
        assert mult <= 1.20


# ---------------------------------------------------------------------------
# Category fallback
# ---------------------------------------------------------------------------

class TestCategoryFallback:
    def test_category_fallback_when_insufficient_samples(self):
        tracker = _make_tracker(category_fallback=5)
        # Add lots of data for stoch_rsi (same category: reversion)
        for _ in range(10):
            tracker.record_outcome("stoch_rsi", won=True, pnl=10.0)
        tracker.record_outcome("stoch_rsi", won=False, pnl=-5.0)

        # rsi_reversion has no data → should use reversion category aggregate
        mult = tracker.get_multiplier("rsi_reversion")
        # Category is winning, so multiplier should be > 1.0
        assert mult >= 1.0

    def test_mixed_individual_and_category(self):
        tracker = _make_tracker(category_fallback=5)
        # rsi_reversion: 2 samples (< fallback threshold of 5)
        tracker.record_outcome("rsi_reversion", won=False, pnl=-10.0)
        tracker.record_outcome("rsi_reversion", won=False, pnl=-10.0)

        # stoch_rsi: 10 wins → strong category performance
        for _ in range(10):
            tracker.record_outcome("stoch_rsi", won=True, pnl=10.0)

        # rsi_reversion has only 2 samples → weight=2/5=0.4 individual + 0.6 category
        mult = tracker.get_multiplier("rsi_reversion")
        # Individual is losing but category is winning → blended
        # Should be between pure loss and pure win
        assert 0.50 <= mult <= 1.20

    def test_unknown_strategy_returns_neutral(self):
        tracker = _make_tracker()
        # Strategy not registered → no category → neutral fallback
        assert tracker.get_multiplier("unknown_strategy") == 1.0


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

class TestSessionManagement:
    def test_reset_clears_stats(self):
        tracker = _make_tracker()
        tracker.record_outcome("rsi_reversion", won=True, pnl=5.0)
        tracker.record_outcome("rsi_reversion", won=True, pnl=5.0)

        summary = tracker.reset_session()
        assert summary["total_recorded"] == 2

        # After reset, back to neutral
        assert tracker.get_multiplier("rsi_reversion") == 1.0
        assert tracker.describe()["total_recorded"] == 0


# ---------------------------------------------------------------------------
# Statistics and reporting
# ---------------------------------------------------------------------------

class TestReporting:
    def test_describe_structure(self):
        tracker = _make_tracker()
        tracker.record_outcome("rsi_reversion", won=True, pnl=10.0)
        tracker.record_outcome("rsi_reversion", won=False, pnl=-5.0)
        tracker.record_outcome("supertrend", won=True, pnl=8.0)

        report = tracker.describe()
        assert report["enabled"] is True
        assert report["total_recorded"] == 3
        assert "rsi_reversion" in report["strategies"]
        assert "supertrend" in report["strategies"]
        assert "reversion" in report["categories"]
        assert "trend" in report["categories"]

    def test_strategy_stats(self):
        tracker = _make_tracker()
        tracker.record_outcome("rsi_reversion", won=True, pnl=10.0)
        tracker.record_outcome("rsi_reversion", won=False, pnl=-5.0)
        tracker.record_outcome("rsi_reversion", won=True, pnl=8.0)

        stats = tracker.get_strategy_stats("rsi_reversion")
        assert stats is not None
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["total"] == 3
        assert abs(stats["win_rate"] - 2 / 3) < 0.01
        assert "multiplier" in stats

    def test_strategy_ranking(self):
        tracker = _make_tracker(category_fallback=1)
        # rsi: 3 wins
        for _ in range(3):
            tracker.record_outcome("rsi_reversion", won=True, pnl=10.0)
        # supertrend: 3 losses
        for _ in range(3):
            tracker.record_outcome("supertrend", won=False, pnl=-10.0)

        ranking = tracker.strategy_ranking()
        assert len(ranking) == 2
        assert ranking[0]["strategy"] == "rsi_reversion"
        assert ranking[1]["strategy"] == "supertrend"
        assert ranking[0]["multiplier"] > ranking[1]["multiplier"]


# ---------------------------------------------------------------------------
# Profit factor integration
# ---------------------------------------------------------------------------

class TestProfitFactor:
    def test_high_profit_factor_slight_boost(self):
        tracker = _make_tracker(category_fallback=1)
        # 3 wins with large pnl, 2 losses with small pnl → high profit factor
        for _ in range(3):
            tracker.record_outcome("rsi_reversion", won=True, pnl=20.0)
        for _ in range(2):
            tracker.record_outcome("rsi_reversion", won=False, pnl=-3.0)

        stats = tracker.get_strategy_stats("rsi_reversion")
        assert stats is not None
        pf = stats["profit_factor"]
        assert pf is not None
        assert pf > 2.0  # 60/6 = 10.0

        mult = tracker.get_multiplier("rsi_reversion")
        # Win rate = 3/5 = 0.60, above baseline, plus high PF → should be boosted
        assert mult > 1.0

    def test_low_profit_factor_suppresses(self):
        tracker = _make_tracker(category_fallback=1)
        # 3 wins with tiny pnl, 2 losses with large pnl → low profit factor
        for _ in range(3):
            tracker.record_outcome("supertrend", won=True, pnl=1.0)
        for _ in range(2):
            tracker.record_outcome("supertrend", won=False, pnl=-20.0)

        stats = tracker.get_strategy_stats("supertrend")
        assert stats is not None
        pf = stats["profit_factor"]
        assert pf is not None
        assert pf < 0.8  # 3/40 = 0.075

        mult = tracker.get_multiplier("supertrend")
        # Win rate = 0.60 (above baseline) but PF is terrible → adjustment
        # The PF penalty of 0.95 should slightly reduce
        assert mult < 1.20

    def test_profit_factor_clamped_at_50(self):
        """Extreme PF from tiny loss should be capped at 50."""
        tracker = _make_tracker(category_fallback=1)
        for _ in range(5):
            tracker.record_outcome("rsi_reversion", won=True, pnl=100.0)
        tracker.record_outcome("rsi_reversion", won=False, pnl=-0.001)

        stats = tracker.get_strategy_stats("rsi_reversion")
        assert stats is not None
        pf = stats["profit_factor"]
        assert pf is not None
        assert pf <= 50.0, f"profit_factor should be capped at 50, got {pf}"

    def test_pure_profit_strategy_gets_boost(self):
        """All wins, zero losses → PF is None but should get multiplier boost."""
        tracker = _make_tracker(category_fallback=1)
        for _ in range(5):
            tracker.record_outcome("rsi_reversion", won=True, pnl=10.0)

        stats = tracker.get_strategy_stats("rsi_reversion")
        assert stats is not None
        assert stats["profit_factor"] is None  # no losses → None

        mult = tracker.get_multiplier("rsi_reversion")
        # Pure profit strategy should get boost (×1.05 from PF branch)
        assert mult > 1.0, f"Pure profit strategy should be boosted, got {mult}"

    def test_pure_loss_strategy_gets_suppressed(self):
        """All losses, zero wins → PF is 0.0 and should be suppressed."""
        tracker = _make_tracker(category_fallback=1)
        for _ in range(5):
            tracker.record_outcome("supertrend", won=False, pnl=-10.0)

        mult = tracker.get_multiplier("supertrend")
        assert mult < 1.0, f"Pure loss strategy should be suppressed, got {mult}"


# ---------------------------------------------------------------------------
# Thread safety (basic smoke test)
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_access(self):
        import threading
        tracker = _make_tracker()
        errors = []

        def writer():
            try:
                for i in range(100):
                    tracker.record_outcome("rsi_reversion", won=i % 3 != 0, pnl=float(i))
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    tracker.get_multiplier("rsi_reversion")
                    tracker.describe()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"
