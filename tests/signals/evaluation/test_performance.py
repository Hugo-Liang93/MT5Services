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


# ---------------------------------------------------------------------------
# warm_up_from_db — DB replay path (regression: P2 FrozenInstanceError)
# ---------------------------------------------------------------------------


def _warmup_tracker(*, max_consec: int = 3) -> StrategyPerformanceTracker:
    """Tracker with PnL circuit configured to a small threshold for testing."""
    cfg = PerformanceTrackerConfig(
        enabled=True,
        pnl_circuit_enabled=True,
        pnl_circuit_max_consecutive_losses=max_consec,
        pnl_circuit_cooldown_minutes=15,
    )
    return StrategyPerformanceTracker(config=cfg)


def test_warm_up_from_db_does_not_mutate_frozen_config() -> None:
    """回归：warm_up 不得 mutate frozen PerformanceTrackerConfig 字段。

    历史 bug：line 525-526 直接 self._config.pnl_circuit_enabled = False
    抛 FrozenInstanceError，被上层 except Exception 静默吞噬数月。
    """
    tracker = _warmup_tracker(max_consec=3)
    rows = [{"strategy": "s1", "won": False, "pnl": -10.0, "source": "trade"}]
    # 不抛即可（旧实现会抛 FrozenInstanceError）
    tracker.warm_up_from_db(rows)
    # config 必须仍是 frozen 且字段未变（saved_pnl_enabled 恢复到 True）
    assert tracker._config.pnl_circuit_enabled is True


def test_warm_up_from_db_replays_outcomes_returns_count() -> None:
    tracker = _warmup_tracker()
    rows = [
        {"strategy": "s1", "won": True, "pnl": 5.0, "source": "trade"},
        {"strategy": "s1", "won": False, "pnl": -3.0, "source": "trade"},
        {"strategy": "s2", "won": True, "pnl": 2.0, "source": "signal"},
    ]
    count = tracker.warm_up_from_db(rows)
    assert count == 3
    s1 = tracker.get_strategy_stats("s1")
    assert s1 is not None and s1["total"] == 2


def test_warm_up_from_db_skips_invalid_rows() -> None:
    tracker = _warmup_tracker()
    rows = [
        {"strategy": "s1", "won": True, "pnl": 1.0, "source": "trade"},
        {"strategy": None, "won": True, "pnl": 1.0},  # missing strategy
        {"strategy": "s1", "won": None, "pnl": 1.0},  # missing won
    ]
    count = tracker.warm_up_from_db(rows)
    assert count == 1


def test_warm_up_from_db_does_not_trigger_pnl_circuit() -> None:
    """warm_up 期间历史亏损不应触发本 session PnL 熔断器。

    设置 max_consec=2，回放 5 条连败。若 warm_up 期间 circuit 被触发，
    pnl_circuit_paused 会变 True；正确实现下应一直 False（warm_up 期间禁用）。
    """
    tracker = _warmup_tracker(max_consec=2)
    rows = [
        {"strategy": "s1", "won": False, "pnl": -10.0, "source": "trade"}
        for _ in range(5)
    ]
    tracker.warm_up_from_db(rows)
    # warm_up 完成后 circuit 必须未触发，loss streak 已重置为 0
    assert tracker._pnl_circuit_paused is False
    assert tracker._global_trade_loss_streak == 0


def test_warm_up_restores_pnl_circuit_for_subsequent_real_trades() -> None:
    """warm_up 完成后，正常 record_outcome 路径下 PnL 熔断器必须重新生效。

    防回归：if `pnl_circuit_enabled` 被错误永久置 False 或 _warmup_active 未重置，
    warm_up 后真实交易亏损将不再触发熔断 → 风控失效。
    """
    tracker = _warmup_tracker(max_consec=2)
    # warm_up 一些历史
    tracker.warm_up_from_db(
        [{"strategy": "s1", "won": False, "pnl": -1.0, "source": "trade"}]
    )
    # 然后真实 session 内连亏 2 次（达到熔断阈值）
    tracker.record_outcome("s1", won=False, pnl=-1.0, source="trade")
    tracker.record_outcome("s1", won=False, pnl=-1.0, source="trade")
    assert tracker._pnl_circuit_paused is True


def test_warm_up_propagates_coding_errors_fail_fast() -> None:
    """coding error（下游 record_outcome 抛非 frozen-related 异常）应直接传播，
    禁止被 warm_up_from_db 内部静默吞噬（同 ADR-011 异常分层契约）。

    用 NameError 而非 AttributeError，避免与 FrozenInstanceError（AttributeError 子类）
    巧合命中——若未来谁加 except Exception 兜底，本测试会立即红。
    """
    tracker = _warmup_tracker()

    def boom(*args, **kwargs):  # noqa: ANN001, ANN202
        raise NameError("simulated coding bug downstream")

    tracker.record_outcome = boom  # type: ignore[method-assign]
    rows = [{"strategy": "s1", "won": True, "pnl": 1.0, "source": "trade"}]
    with pytest.raises(NameError):
        tracker.warm_up_from_db(rows)
