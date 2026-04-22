"""Phase R.2 — cross_tf provider 3 个 _compute_* 函数 Numba JIT 等价性测试。

确保 JIT 重写与原始 Python 实现数值等价（容差 1e-9）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import numpy as np
import pytest

from src.research.features.cross_tf.provider import (
    _CHILD_ATR_KEY,
    _CHILD_TREND_KEY,
    _compute_delta,
    _compute_delta_jit,
    _compute_dist_to_ema,
    _compute_dist_to_ema_jit,
    _compute_trend_align,
    _compute_trend_align_jit,
)

# ── Reference 实现（原版逻辑） ─────────────────────────────────────────────


def _ref_trend_align(
    aligned_parent_trend: Optional[np.ndarray],
    child_trend_list: Optional[List[Optional[float]]],
    n: int,
) -> List[Optional[float]]:
    if aligned_parent_trend is None:
        return [None] * n
    out: List[Optional[float]] = []
    for i in range(n):
        p_val = aligned_parent_trend[i]
        if not np.isfinite(p_val) or p_val == 0.0:
            out.append(None)
            continue
        if child_trend_list is None or i >= len(child_trend_list):
            out.append(None)
            continue
        c_raw = child_trend_list[i]
        if c_raw is None or c_raw == 0.0:
            out.append(None)
            continue
        child_sign = 1.0 if float(c_raw) > 0 else -1.0
        parent_sign = 1.0 if float(p_val) > 0 else -1.0
        out.append(child_sign * parent_sign)
    return out


def _ref_dist_to_ema(
    closes: Optional[List[Optional[float]]],
    aligned_ema: Optional[np.ndarray],
    atr_list: Optional[List[Optional[float]]],
    n: int,
) -> List[Optional[float]]:
    if aligned_ema is None:
        return [None] * n
    out: List[Optional[float]] = []
    for i in range(n):
        if closes is None or i >= len(closes):
            out.append(None)
            continue
        close_raw = closes[i]
        if close_raw is None:
            out.append(None)
            continue
        ema_val = aligned_ema[i]
        if not np.isfinite(ema_val):
            out.append(None)
            continue
        if atr_list is None or i >= len(atr_list):
            out.append(None)
            continue
        atr_raw = atr_list[i]
        if atr_raw is None:
            out.append(None)
            continue
        atr_val = float(atr_raw)
        if atr_val < 1e-9:
            out.append(None)
            continue
        out.append((float(close_raw) - float(ema_val)) / atr_val)
    return out


def _ref_delta(
    aligned: Optional[np.ndarray], window: int, n: int
) -> List[Optional[float]]:
    if aligned is None:
        return [None] * n
    out: List[Optional[float]] = []
    for i in range(n):
        if i < window:
            out.append(None)
            continue
        cur = aligned[i]
        ref = aligned[i - window]
        if np.isfinite(cur) and np.isfinite(ref):
            out.append(float(cur - ref))
        else:
            out.append(None)
    return out


def _assert_lists_close(
    a: List[Optional[float]],
    b: List[Optional[float]],
    tol: float = 1e-9,
    label: str = "",
) -> None:
    assert len(a) == len(b), f"[{label}] length mismatch: {len(a)} vs {len(b)}"
    for i, (x, y) in enumerate(zip(a, b)):
        if x is None or y is None:
            assert x == y, f"[{label}] None mismatch at i={i}: {x} vs {y}"
        else:
            assert (
                abs(x - y) <= tol
            ), f"[{label}] mismatch at i={i}: {x} vs {y} (diff={abs(x-y):.3e})"


def _make_matrix(
    closes: Optional[List[Optional[float]]] = None,
    indicator_series: Optional[dict] = None,
) -> Any:
    return SimpleNamespace(
        closes=closes,
        indicator_series=indicator_series or {},
    )


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def rng():
    return np.random.default_rng(42)


# ── trend_align ────────────────────────────────────────────────────────────


def test_trend_align_basic_equivalence(rng):
    n = 100
    parent = rng.choice([-1.0, 1.0, np.nan], size=n)
    child_list: List[Optional[float]] = []
    for _ in range(n):
        v = rng.choice([-1.0, 1.0, 0.0, None], p=[0.4, 0.4, 0.1, 0.1])
        child_list.append(None if v is None else float(v))

    matrix = _make_matrix(indicator_series={_CHILD_TREND_KEY: child_list})
    ref = _ref_trend_align(parent, child_list, n)
    new = _compute_trend_align(matrix, parent, n)
    _assert_lists_close(ref, new, label="trend_align random")


def test_trend_align_aligned_parent_none():
    n = 50
    matrix = _make_matrix(indicator_series={_CHILD_TREND_KEY: [1.0] * n})
    out = _compute_trend_align(matrix, None, n)
    assert all(v is None for v in out)


def test_trend_align_child_missing():
    n = 20
    parent = np.ones(n)
    matrix = _make_matrix(indicator_series={})  # 子趋势缺失
    out = _compute_trend_align(matrix, parent, n)
    assert all(v is None for v in out)


def test_trend_align_zero_treated_as_none():
    n = 10
    parent = np.array([1.0, 0.0, -1.0, 1.0, np.nan, 1.0, 1.0, -1.0, 1.0, 1.0])
    child = [1.0, 1.0, 0.0, -1.0, 1.0, None, 1.0, 1.0, -1.0, 1.0]
    matrix = _make_matrix(indicator_series={_CHILD_TREND_KEY: child})
    ref = _ref_trend_align(parent, child, n)
    new = _compute_trend_align(matrix, parent, n)
    _assert_lists_close(ref, new, label="trend_align zero handling")


# ── dist_to_ema ────────────────────────────────────────────────────────────


def test_dist_to_ema_basic_equivalence(rng):
    n = 100
    closes_list = [float(v) for v in rng.standard_normal(n) * 10 + 1900]
    closes_list[5] = None  # type: ignore[call-overload]
    closes_list[20] = None  # type: ignore[call-overload]
    aligned_ema = rng.standard_normal(n) * 5 + 1900
    aligned_ema[10] = np.nan
    atr_list = [float(v) for v in np.abs(rng.standard_normal(n) * 5 + 10)]
    atr_list[15] = 0.0  # < 1e-9
    atr_list[25] = None  # type: ignore[call-overload]

    matrix = _make_matrix(
        closes=closes_list, indicator_series={_CHILD_ATR_KEY: atr_list}
    )
    ref = _ref_dist_to_ema(closes_list, aligned_ema, atr_list, n)
    new = _compute_dist_to_ema(matrix, aligned_ema, n)
    _assert_lists_close(ref, new, label="dist_to_ema random")


def test_dist_to_ema_aligned_ema_none():
    n = 30
    matrix = _make_matrix(
        closes=[1.0] * n, indicator_series={_CHILD_ATR_KEY: [1.0] * n}
    )
    out = _compute_dist_to_ema(matrix, None, n)
    assert all(v is None for v in out)


def test_dist_to_ema_no_closes():
    n = 20
    aligned_ema = np.ones(n) * 1900.0
    matrix = _make_matrix(closes=None, indicator_series={_CHILD_ATR_KEY: [10.0] * n})
    out = _compute_dist_to_ema(matrix, aligned_ema, n)
    assert all(v is None for v in out)


def test_dist_to_ema_no_atr():
    n = 20
    aligned_ema = np.ones(n) * 1900.0
    matrix = _make_matrix(closes=[1900.0] * n, indicator_series={})
    out = _compute_dist_to_ema(matrix, aligned_ema, n)
    assert all(v is None for v in out)


def test_dist_to_ema_atr_below_threshold():
    """ATR < 1e-9 应返回 None。"""
    n = 5
    aligned_ema = np.full(n, 1900.0)
    closes = [1905.0] * n
    atr = [1e-12, 1e-10, 0.5, 1.0, 2.0]
    matrix = _make_matrix(closes=closes, indicator_series={_CHILD_ATR_KEY: atr})
    ref = _ref_dist_to_ema(closes, aligned_ema, atr, n)
    new = _compute_dist_to_ema(matrix, aligned_ema, n)
    _assert_lists_close(ref, new, label="atr < 1e-9 boundary")
    # 验证前两个是 None
    assert ref[0] is None
    assert ref[1] is None
    assert ref[2] is not None


# ── delta ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("window", [1, 3, 5, 10])
def test_delta_basic_equivalence(rng, window):
    n = 100
    aligned = rng.standard_normal(n) * 10
    aligned[3] = np.nan
    aligned[20] = np.nan
    ref = _ref_delta(aligned, window, n)
    new = _compute_delta(aligned, window, n)
    _assert_lists_close(ref, new, label=f"delta w={window}")


def test_delta_aligned_none():
    n = 30
    out = _compute_delta(None, 5, n)
    assert all(v is None for v in out)
    assert len(out) == n


def test_delta_window_larger_than_array():
    n = 5
    aligned = np.arange(n, dtype=np.float64)
    out = _compute_delta(aligned, 10, n)
    assert all(v is None for v in out)


def test_delta_first_window_bars_none():
    n = 10
    aligned = np.arange(n, dtype=np.float64)
    out = _compute_delta(aligned, 3, n)
    assert out[:3] == [None, None, None]
    # 之后应该是 3.0（差 3 步）
    for i in range(3, n):
        assert out[i] == 3.0


# ── JIT 内层返回类型契约 ────────────────────────────────────────────────────


def test_jit_funcs_return_ndarray():
    """JIT 内层必须返回 np.ndarray。"""
    n = 10
    parent = np.ones(n)
    child = np.ones(n)
    closes = np.ones(n) * 1900.0
    ema = np.ones(n) * 1890.0
    atr = np.ones(n) * 5.0
    arr = np.arange(n, dtype=np.float64)

    assert isinstance(_compute_trend_align_jit(parent, child, n), np.ndarray)
    assert isinstance(_compute_dist_to_ema_jit(closes, ema, atr, n), np.ndarray)
    assert isinstance(_compute_delta_jit(arr, 3), np.ndarray)
