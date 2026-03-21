from __future__ import annotations

import pytest

from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.regime import RegimeType


def test_confidence_calibrator_phase1_defaults() -> None:
    calibrator = ConfidenceCalibrator(fetch_winrates_fn=lambda **kwargs: [])

    state = calibrator.describe()

    assert state["alpha"] == pytest.approx(0.15)
    assert state["min_samples"] == 50
    assert state["recency_hours"] == 8


def test_confidence_calibrator_refresh_uses_phase1_sample_thresholds() -> None:
    def fetch_winrates(*, hours: int, symbol=None):
        if hours == 168:
            return [
                ("supertrend", "buy", 49, 30, 0.61, 0.70, 5.0, "trending"),
                ("supertrend", "buy", 50, 31, 0.62, 0.70, 5.0, "trending"),
            ]
        if hours == 8:
            return [
                ("supertrend", "buy", 24, 15, 0.61, 0.70, 5.0, "trending"),
                ("supertrend", "buy", 25, 15, 0.60, 0.70, 5.0, "trending"),
            ]
        raise AssertionError(f"unexpected hours={hours}")

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch_winrates)

    count = calibrator.refresh()
    adjusted = calibrator.calibrate("supertrend", "buy", 0.80, RegimeType.TRENDING)
    state = calibrator.describe()

    assert count == 1
    assert state["cache_entries"] == 1
    assert state["recent_cache_entries"] == 1
    assert adjusted > 0.80


def test_confidence_calibrator_uses_staged_alpha_by_sample_count() -> None:
    def fetch_winrates(*, hours: int, symbol=None):
        if hours == 168:
            return [
                ("supertrend", "buy", 60, 42, 0.70, 0.70, 5.0, "trending"),
                ("macd_momentum", "buy", 120, 84, 0.70, 0.70, 5.0, "trending"),
            ]
        if hours == 8:
            return [
                ("supertrend", "buy", 30, 21, 0.70, 0.70, 5.0, "trending"),
                ("macd_momentum", "buy", 60, 42, 0.70, 0.70, 5.0, "trending"),
            ]
        raise AssertionError(f"unexpected hours={hours}")

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch_winrates)
    calibrator.refresh()

    warm = calibrator.calibrate("supertrend", "buy", 0.80, RegimeType.TRENDING)
    full = calibrator.calibrate("macd_momentum", "buy", 0.80, RegimeType.TRENDING)

    assert warm == pytest.approx(0.824)
    assert full == pytest.approx(0.836)


# ── 补充测试：分阶段 alpha、底线保护、recency 防正反馈 ─────────────────

def _noop_fetch(**kwargs):
    return []


def test_alpha_zero_returns_raw_confidence() -> None:
    """alpha=0 时 calibrate() 直接返回原始值，不查缓存。"""
    calibrator = ConfidenceCalibrator(fetch_winrates_fn=_noop_fetch, alpha=0.0)
    result = calibrator.calibrate("any", "buy", 0.75, RegimeType.TRENDING)
    assert result == pytest.approx(0.75)


def test_below_min_samples_returns_raw_confidence() -> None:
    """历史样本 < min_samples(50) 时不校准。"""
    def fetch(*, hours, symbol=None):
        return [("strat", "buy", 49, 30, 0.61, 0.7, 5.0, "trending")]

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch)
    calibrator.refresh()
    result = calibrator.calibrate("strat", "buy", 0.80, RegimeType.TRENDING)
    assert result == pytest.approx(0.80)  # 样本不足，不校准


def test_50_to_100_samples_uses_warmup_alpha() -> None:
    """50-100 样本使用 warmup_alpha(0.10)，而非 full alpha(0.15)。"""
    # win_rate=0.70, factor=0.70/0.50=1.40 → capped by max_boost=1.30
    # calibrated = 0.80 * (1 + (1.30 - 1) * 0.10) = 0.80 * 1.03 = 0.824
    def fetch(*, hours, symbol=None):
        if hours == 168:
            return [("strat", "buy", 75, 52, 0.70, 0.7, 5.0, "trending")]
        return [("strat", "buy", 37, 26, 0.70, 0.7, 5.0, "trending")]

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch)
    calibrator.refresh()

    result = calibrator.calibrate("strat", "buy", 0.80, RegimeType.TRENDING)
    # warmup_alpha=0.10, factor=min(0.70/0.50, 1.30)=1.30
    # 0.80 * (1 + (1.30 - 1) * 0.10) = 0.80 * 1.03 = 0.824
    assert result == pytest.approx(0.824)


def test_above_100_samples_uses_full_alpha() -> None:
    """≥100 样本使用 full alpha(0.15)。"""
    def fetch(*, hours, symbol=None):
        if hours == 168:
            return [("strat", "buy", 150, 105, 0.70, 0.7, 5.0, "trending")]
        return [("strat", "buy", 75, 52, 0.70, 0.7, 5.0, "trending")]

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch)
    calibrator.refresh()

    result = calibrator.calibrate("strat", "buy", 0.80, RegimeType.TRENDING)
    # full alpha=0.15, factor=min(0.70/0.50, 1.30)=1.30
    # 0.80 * (1 + (1.30 - 1) * 0.15) = 0.80 * 1.045 = 0.836
    assert result == pytest.approx(0.836)


def test_max_boost_cap() -> None:
    """win_rate 极高时 factor 被 max_boost(1.30) 封顶。"""
    def fetch(*, hours, symbol=None):
        if hours == 168:
            return [("strat", "buy", 200, 190, 0.95, 0.9, 5.0, "trending")]
        return [("strat", "buy", 100, 95, 0.95, 0.9, 5.0, "trending")]

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch)
    calibrator.refresh()

    result = calibrator.calibrate("strat", "buy", 0.80, RegimeType.TRENDING)
    # factor = 0.95/0.50 = 1.90 → capped to 1.30
    # 0.80 * (1 + (1.30 - 1) * 0.15) = 0.836
    assert result == pytest.approx(0.836)


def test_suppression_when_win_rate_below_baseline() -> None:
    """win_rate < baseline 时 factor < 1.0，置信度被压制。"""
    def fetch(*, hours, symbol=None):
        if hours == 168:
            return [("strat", "buy", 200, 60, 0.30, 0.7, 5.0, "trending")]
        return [("strat", "buy", 100, 30, 0.30, 0.7, 5.0, "trending")]

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch)
    calibrator.refresh()

    result = calibrator.calibrate("strat", "buy", 0.80, RegimeType.TRENDING)
    # factor = 0.30/0.50 = 0.60
    # 0.80 * (1 + (0.60 - 1) * 0.15) = 0.80 * 0.94 = 0.752
    assert result == pytest.approx(0.752)
    assert result < 0.80


def test_recency_protection_caps_boost_when_recent_poor() -> None:
    """历史 7 天胜率好(boost)但近期 8h 胜率低于 baseline 时，禁止 boost。"""
    def fetch(*, hours, symbol=None):
        if hours == 168:
            # 历史胜率不错
            return [("strat", "buy", 150, 105, 0.70, 0.7, 5.0, "trending")]
        if hours == 8:
            # 近期胜率下滑到 baseline 以下
            return [("strat", "buy", 30, 12, 0.40, 0.7, 5.0, "trending")]
        return []

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch)
    calibrator.refresh()

    result = calibrator.calibrate("strat", "buy", 0.80, RegimeType.TRENDING)
    # 历史 factor=1.40→capped 1.30 (would boost)，但近期 win_rate=0.40 < baseline=0.50
    # 正反馈保护：factor 被夹到 1.0 → calibrated = 0.80 * 1.0 = 0.80
    assert result == pytest.approx(0.80)


def test_no_data_returns_raw_confidence() -> None:
    """缓存为空时不校准。"""
    calibrator = ConfidenceCalibrator(fetch_winrates_fn=_noop_fetch)
    result = calibrator.calibrate("unknown", "buy", 0.65, RegimeType.UNCERTAIN)
    assert result == pytest.approx(0.65)


def test_dump_and_load_round_trip(tmp_path) -> None:
    """dump() → load() 能完整恢复缓存。"""
    def fetch(*, hours, symbol=None):
        if hours == 168:
            return [("strat", "buy", 100, 60, 0.60, 0.7, 5.0, "trending")]
        return []

    calibrator = ConfidenceCalibrator(fetch_winrates_fn=fetch)
    calibrator.refresh()

    path = str(tmp_path / "calibrator_cache.json")
    calibrator.dump(path)

    # 新实例，空缓存
    calibrator2 = ConfidenceCalibrator(fetch_winrates_fn=_noop_fetch)
    loaded = calibrator2.load(path)
    assert loaded == 1
    # 设置 _last_refresh 避免 calibrate() 中 _auto_refresh() 清空 load 的缓存
    import time as _time
    calibrator2._last_refresh = _time.monotonic()

    # 校准结果应与原始一致（两者都没有 recent_cache，行为相同）
    result = calibrator2.calibrate("strat", "buy", 0.80, RegimeType.TRENDING)
    expected = calibrator.calibrate("strat", "buy", 0.80, RegimeType.TRENDING)
    assert result == pytest.approx(expected)
