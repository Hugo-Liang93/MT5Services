"""Phase R.2 — microstructure provider 12 个 _batch_* 函数 Numba JIT 等价性测试。

针对 12 个新增 JIT 函数与原始 NumPy/Python 实现做数值等价比对（容差 1e-9）。
现有 test_microstructure_provider.py 已覆盖 known_values，本文件
提供独立 reference impl 守护，便于未来 refactor 时快速暴露偏差。
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
import pytest

from src.research.features.microstructure.provider import (
    _batch_bb_width_change,
    _batch_consecutive_down,
    _batch_consecutive_same_color,
    _batch_consecutive_up,
    _batch_gap_ratio,
    _batch_hh_count,
    _batch_ll_count,
    _batch_range_position,
    _batch_vol_price_accord,
    _batch_volatility_ratio,
    _batch_volume_surge,
    _bb_width_change_jit,
    _consecutive_down_jit,
    _consecutive_same_color_jit,
    _consecutive_up_jit,
    _gap_ratio_jit,
    _hh_count_jit,
    _ll_count_jit,
    _range_position_jit,
    _rolling_mean,
    _rolling_mean_jit,
    _vol_price_accord_jit,
    _volatility_ratio_jit,
    _volume_surge_jit,
)

# ── Reference 实现（重构前的原版 Python 逻辑） ────────────────────────────


def _ref_consecutive_same_color(opens, closes, n):
    out = np.zeros(n, dtype=np.float64)
    signs = np.where(closes > opens, 1, np.where(closes < opens, -1, 0))
    for i in range(n):
        if signs[i] == 0:
            out[i] = 0.0
            continue
        run = 1
        for j in range(i - 1, max(-1, i - 10), -1):
            if signs[j] == 0:
                break
            if signs[j] == signs[i]:
                run += 1
            else:
                break
        out[i] = float(run * signs[i])
    return [float(v) for v in out]


def _ref_consecutive_up(closes, n):
    out = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        out[i] = out[i - 1] + 1.0 if closes[i] > closes[i - 1] else 0.0
    return [float(v) for v in out]


def _ref_consecutive_down(closes, n):
    out = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        out[i] = out[i - 1] + 1.0 if closes[i] < closes[i - 1] else 0.0
    return [float(v) for v in out]


def _ref_vol_price_accord(closes, volumes, n, w):
    out: List[Optional[float]] = [None] * n
    sign_cd = np.zeros(n, dtype=np.float64)
    sign_vd = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        sign_cd[i] = float(np.sign(closes[i] - closes[i - 1]))
        sign_vd[i] = float(np.sign(volumes[i] - volumes[i - 1]))
    accord = sign_cd * sign_vd
    for i in range(w, n):
        out[i] = float(np.sum(accord[i - w + 1 : i + 1]) / w)
    return out


def _ref_volatility_ratio(atr_arr, n, w):
    if atr_arr is None:
        return [None] * n
    out: List[Optional[float]] = [None] * n
    for i in range(w, n):
        cur = atr_arr[i]
        window = atr_arr[i - w : i]
        if not np.isfinite(cur) or not np.all(np.isfinite(window)):
            continue
        mean_w = float(np.mean(window))
        if mean_w < 1e-9:
            continue
        out[i] = float(cur / mean_w)
    return out


def _ref_bb_width_change(bb_series, n, w):
    if bb_series is None:
        return [None] * n
    out: List[Optional[float]] = [None] * n
    for i in range(w, n):
        cur = bb_series[i]
        ref = bb_series[i - w]
        if cur is None or ref is None or abs(ref) < 1e-9:
            continue
        out[i] = float(cur / ref)
    return out


def _ref_rolling_mean(arr, n, w):
    out: List[Optional[float]] = [None] * n
    for i in range(w - 1, n):
        out[i] = float(np.mean(arr[i - w + 1 : i + 1]))
    return out


def _ref_hh_count(highs, n, w):
    out: List[Optional[float]] = [None] * n
    for i in range(w, n):
        c = sum(1 for j in range(i - w + 1, i + 1) if highs[j] > highs[j - 1])
        out[i] = float(c)
    return out


def _ref_ll_count(lows, n, w):
    out: List[Optional[float]] = [None] * n
    for i in range(w, n):
        c = sum(1 for j in range(i - w + 1, i + 1) if lows[j] < lows[j - 1])
        out[i] = float(c)
    return out


def _ref_gap_ratio(opens, closes, atr_arr, n):
    if atr_arr is None:
        return [None] * n
    out: List[Optional[float]] = [None] * n
    for i in range(1, n):
        a = atr_arr[i]
        if not np.isfinite(a) or a < 1e-9:
            continue
        out[i] = float((opens[i] - closes[i - 1]) / a)
    return out


def _ref_range_position(highs, lows, closes, n, w):
    out: List[Optional[float]] = [None] * n
    for i in range(w - 1, n):
        h_max = float(np.max(highs[i - w + 1 : i + 1]))
        l_min = float(np.min(lows[i - w + 1 : i + 1]))
        spread = h_max - l_min
        if spread < 1e-9:
            out[i] = 0.5
        else:
            out[i] = float((closes[i] - l_min) / spread)
    return out


def _ref_volume_surge(volumes, n, w):
    out: List[Optional[float]] = [None] * n
    for i in range(w, n):
        mean_w = float(np.mean(volumes[i - w : i]))
        if mean_w < 1e-9:
            continue
        out[i] = float(volumes[i] / mean_w)
    return out


def _assert_lists_close(a, b, tol=1e-9, label=""):
    assert len(a) == len(b), f"[{label}] length: {len(a)} vs {len(b)}"
    for i, (x, y) in enumerate(zip(a, b)):
        if x is None or y is None:
            assert x == y, f"[{label}] None at i={i}: {x} vs {y}"
        else:
            assert (
                abs(x - y) <= tol
            ), f"[{label}] at i={i}: {x} vs {y} (diff={abs(x-y):.3e})"


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def ohlcv():
    """200 bars 随机 OHLCV，含边界 (constant high=low、低 vol)。"""
    rng = np.random.default_rng(7)
    n = 200
    opens = rng.standard_normal(n) * 5 + 1900
    closes = opens + rng.standard_normal(n) * 2
    highs = np.maximum(opens, closes) + np.abs(rng.standard_normal(n) * 1.5)
    lows = np.minimum(opens, closes) - np.abs(rng.standard_normal(n) * 1.5)
    volumes = np.abs(rng.standard_normal(n) * 100 + 500)
    # 注入零 range bar
    highs[50] = lows[50] = closes[50] = opens[50]
    return opens, highs, lows, closes, volumes


# ── consecutive_* ──────────────────────────────────────────────────────────


def test_consecutive_same_color_equivalence(ohlcv):
    opens, _, _, closes, _ = ohlcv
    n = len(opens)
    ref = _ref_consecutive_same_color(opens, closes, n)
    new = _batch_consecutive_same_color(opens, closes, n)
    _assert_lists_close(ref, new, tol=0.0, label="consecutive_same_color")


def test_consecutive_up_equivalence(ohlcv):
    _, _, _, closes, _ = ohlcv
    n = len(closes)
    ref = _ref_consecutive_up(closes, n)
    new = _batch_consecutive_up(closes, n)
    _assert_lists_close(ref, new, tol=0.0, label="consecutive_up")


def test_consecutive_down_equivalence(ohlcv):
    _, _, _, closes, _ = ohlcv
    n = len(closes)
    ref = _ref_consecutive_down(closes, n)
    new = _batch_consecutive_down(closes, n)
    _assert_lists_close(ref, new, tol=0.0, label="consecutive_down")


# ── vol_price_accord ───────────────────────────────────────────────────────


@pytest.mark.parametrize("w", [3, 5, 10])
def test_vol_price_accord_equivalence(ohlcv, w):
    _, _, _, closes, volumes = ohlcv
    n = len(closes)
    ref = _ref_vol_price_accord(closes, volumes, n, w)
    new = _batch_vol_price_accord(closes, volumes, n, w)
    _assert_lists_close(ref, new, label=f"vol_price_accord w={w}")


# ── volatility_ratio ───────────────────────────────────────────────────────


@pytest.mark.parametrize("w", [5, 10])
def test_volatility_ratio_equivalence(ohlcv, w):
    n = len(ohlcv[0])
    rng = np.random.default_rng(11)
    atr = np.abs(rng.standard_normal(n) * 3 + 5)
    atr[20] = np.nan
    atr[40] = 0.0  # < 1e-9
    ref = _ref_volatility_ratio(atr, n, w)
    new = _batch_volatility_ratio(atr, n, w)
    _assert_lists_close(ref, new, label=f"volatility_ratio w={w}")


def test_volatility_ratio_atr_none():
    out = _batch_volatility_ratio(None, 50, 5)
    assert all(v is None for v in out)


# ── bb_width_change ────────────────────────────────────────────────────────


@pytest.mark.parametrize("w", [3, 5])
def test_bb_width_change_equivalence(w):
    n = 100
    rng = np.random.default_rng(13)
    bb = [float(v) for v in np.abs(rng.standard_normal(n) + 0.5)]
    bb[10] = None  # type: ignore[call-overload]
    bb[20] = 1e-12
    ref = _ref_bb_width_change(bb, n, w)
    new = _batch_bb_width_change(bb, n, w)
    _assert_lists_close(ref, new, label=f"bb_width_change w={w}")


def test_bb_width_change_none_series():
    out = _batch_bb_width_change(None, 30, 5)
    assert all(v is None for v in out)


# ── rolling_mean ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("w", [3, 5, 10])
def test_rolling_mean_equivalence(ohlcv, w):
    closes = ohlcv[3]
    n = len(closes)
    ref = _ref_rolling_mean(closes, n, w)
    new = _rolling_mean(closes, n, w)
    _assert_lists_close(ref, new, label=f"rolling_mean w={w}")


# ── hh_count / ll_count ────────────────────────────────────────────────────


@pytest.mark.parametrize("w", [5, 10])
def test_hh_count_equivalence(ohlcv, w):
    _, highs, _, _, _ = ohlcv
    n = len(highs)
    ref = _ref_hh_count(highs, n, w)
    new = _batch_hh_count(highs, n, w)
    _assert_lists_close(ref, new, tol=0.0, label=f"hh_count w={w}")


@pytest.mark.parametrize("w", [5, 10])
def test_ll_count_equivalence(ohlcv, w):
    _, _, lows, _, _ = ohlcv
    n = len(lows)
    ref = _ref_ll_count(lows, n, w)
    new = _batch_ll_count(lows, n, w)
    _assert_lists_close(ref, new, tol=0.0, label=f"ll_count w={w}")


# ── gap_ratio ──────────────────────────────────────────────────────────────


def test_gap_ratio_equivalence(ohlcv):
    opens, _, _, closes, _ = ohlcv
    n = len(opens)
    rng = np.random.default_rng(17)
    atr = np.abs(rng.standard_normal(n) * 3 + 5)
    atr[15] = np.nan
    atr[35] = 0.0
    ref = _ref_gap_ratio(opens, closes, atr, n)
    new = _batch_gap_ratio(opens, closes, atr, n)
    _assert_lists_close(ref, new, label="gap_ratio")


def test_gap_ratio_atr_none():
    n = 20
    opens = np.zeros(n)
    closes = np.zeros(n)
    out = _batch_gap_ratio(opens, closes, None, n)
    assert all(v is None for v in out)


# ── range_position ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("w", [3, 5, 10])
def test_range_position_equivalence(ohlcv, w):
    _, highs, lows, closes, _ = ohlcv
    n = len(highs)
    ref = _ref_range_position(highs, lows, closes, n, w)
    new = _batch_range_position(highs, lows, closes, n, w)
    _assert_lists_close(ref, new, label=f"range_position w={w}")


# ── volume_surge ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("w", [3, 5])
def test_volume_surge_equivalence(ohlcv, w):
    volumes = ohlcv[4]
    n = len(volumes)
    ref = _ref_volume_surge(volumes, n, w)
    new = _batch_volume_surge(volumes, n, w)
    _assert_lists_close(ref, new, label=f"volume_surge w={w}")


# ── JIT 内层返回类型契约 ────────────────────────────────────────────────────


def test_jit_funcs_return_ndarray(ohlcv):
    opens, highs, lows, closes, volumes = ohlcv
    n = len(opens)
    atr = np.full(n, 5.0)

    assert isinstance(_consecutive_same_color_jit(opens, closes, n), np.ndarray)
    assert isinstance(_consecutive_up_jit(closes, n), np.ndarray)
    assert isinstance(_consecutive_down_jit(closes, n), np.ndarray)
    assert isinstance(_vol_price_accord_jit(closes, volumes, n, 5), np.ndarray)
    assert isinstance(_volatility_ratio_jit(atr, n, 5), np.ndarray)
    assert isinstance(_bb_width_change_jit(atr, n, 5), np.ndarray)
    assert isinstance(_rolling_mean_jit(closes, n, 5), np.ndarray)
    assert isinstance(_hh_count_jit(highs, n, 5), np.ndarray)
    assert isinstance(_ll_count_jit(lows, n, 5), np.ndarray)
    assert isinstance(_gap_ratio_jit(opens, closes, atr, n), np.ndarray)
    assert isinstance(_range_position_jit(highs, lows, closes, n, 5), np.ndarray)
    assert isinstance(_volume_surge_jit(volumes, n, 5), np.ndarray)
