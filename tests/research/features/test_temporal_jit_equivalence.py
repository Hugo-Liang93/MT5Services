"""Phase R.2 — temporal provider 4 个 _batch_* 函数 Numba JIT 等价性测试。

确保 Numba JIT 重写与原始 NumPy/Python 实现数值等价（容差 1e-9），
保证 mining 输出不变。

历史教训：Numba JIT 改写不能引入精度漂移，否则 IC 排名会变，
candidate 评估失真。每个 _batch_* 函数必须有独立等价性测试守护。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.research.features.temporal.provider import (
    _batch_bars_since_cross,
    _batch_bars_since_cross_jit,
    _batch_delta,
    _batch_delta_jit,
    _batch_slope,
    _batch_slope_jit,
    _batch_zscore,
    _batch_zscore_jit,
)

# ── Reference 实现（原版逻辑，用于等价性比对） ─────────────────────────────


def _ref_delta(arr, w):
    n = len(arr)
    out = []
    for i in range(n):
        if i < w:
            out.append(None)
            continue
        cur, ref = arr[i], arr[i - w]
        if np.isnan(cur) or np.isnan(ref):
            out.append(None)
        else:
            out.append(float(cur - ref))
    return out


def _ref_slope(arr, w):
    """用 np.polyfit 的原版（floating point ref）。"""
    n = len(arr)
    out = []
    x = np.arange(w + 1, dtype=np.float64)
    for i in range(n):
        if i < w:
            out.append(None)
            continue
        window = arr[i - w : i + 1]
        if not np.all(np.isfinite(window)):
            out.append(None)
            continue
        out.append(float(np.polyfit(x, window, 1)[0]))
    return out


def _ref_zscore(arr, w):
    n = len(arr)
    out = []
    for i in range(n):
        if i < w - 1:
            out.append(None)
            continue
        window = arr[i - w + 1 : i + 1]
        if not np.all(np.isfinite(window)):
            out.append(None)
            continue
        std = float(np.std(window))
        if std < 1e-9:
            out.append(None)
            continue
        mean = float(np.mean(window))
        out.append(float((arr[i] - mean) / std))
    return out


def _ref_bars_since_cross(arr, level):
    n = len(arr)
    out = [None] * n
    last_cross = None
    for i in range(1, n):
        prev, cur = arr[i - 1], arr[i]
        if np.isnan(prev) or np.isnan(cur):
            if last_cross is not None:
                out[i] = float(i - last_cross)
            continue
        if (prev < level and cur >= level) or (prev >= level and cur < level):
            last_cross = i
        if last_cross is not None:
            out[i] = float(i - last_cross)
    return out


def _assert_lists_close(a, b, tol=1e-9, label=""):
    assert len(a) == len(b), f"[{label}] length mismatch: {len(a)} vs {len(b)}"
    for i, (x, y) in enumerate(zip(a, b)):
        if x is None or y is None:
            assert x == y, f"[{label}] None mismatch at i={i}: {x} vs {y}"
        else:
            assert (
                abs(x - y) <= tol
            ), f"[{label}] value mismatch at i={i}: {x} vs {y} (diff={abs(x-y):.3e})"


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(
    params=[
        (
            "clean_random",
            lambda: np.random.default_rng(0).standard_normal(200) * 10 + 100,
        ),
        ("with_nans", lambda: _make_with_nans(200, 10)),
        ("constant", lambda: np.full(100, 5.0)),
        ("monotonic", lambda: np.arange(150, dtype=np.float64)),
        ("short", lambda: np.array([1.0, 2.0, 3.0, 4.0, 5.0])),
    ],
    ids=["clean_random", "with_nans", "constant", "monotonic", "short"],
)
def arr_fixture(request):
    return request.param[1]()


def _make_with_nans(n, k):
    rng = np.random.default_rng(1)
    arr = rng.standard_normal(n) * 5 + 50
    nan_idx = rng.choice(n, size=k, replace=False)
    arr[nan_idx] = np.nan
    return arr


# ── _batch_delta ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("w", [1, 3, 5, 10])
def test_batch_delta_equivalence(arr_fixture, w):
    if len(arr_fixture) <= w:
        pytest.skip("array too short for window")
    ref = _ref_delta(arr_fixture, w)
    new = _batch_delta(arr_fixture, w)
    _assert_lists_close(ref, new, label=f"delta w={w}")


# ── _batch_slope ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("w", [3, 5, 10])
def test_batch_slope_equivalence_with_polyfit(arr_fixture, w):
    """JIT slope (closed-form) vs np.polyfit。容差稍宽（浮点累积误差）。"""
    if len(arr_fixture) <= w + 1:
        pytest.skip("array too short")
    ref = _ref_slope(arr_fixture, w)
    new = _batch_slope(arr_fixture, w)
    # closed-form vs polyfit (SVD)：1e-6 容差足够
    _assert_lists_close(ref, new, tol=1e-6, label=f"slope w={w}")


# ── _batch_zscore ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("w", [3, 5, 10, 20])
def test_batch_zscore_equivalence(arr_fixture, w):
    if len(arr_fixture) < w:
        pytest.skip("array too short for window")
    ref = _ref_zscore(arr_fixture, w)
    new = _batch_zscore(arr_fixture, w)
    # sum²-mean² 公式 vs np.std：浮点累积误差，容差 1e-9 (实测 < 1e-12)
    _assert_lists_close(ref, new, tol=1e-9, label=f"zscore w={w}")


# ── _batch_bars_since_cross ────────────────────────────────────────────────


@pytest.mark.parametrize("level", [0.0, 50.0, 100.0])
def test_batch_bars_since_cross_equivalence(arr_fixture, level):
    ref = _ref_bars_since_cross(arr_fixture, level)
    new = _batch_bars_since_cross(arr_fixture, level)
    _assert_lists_close(ref, new, tol=0.0, label=f"bars_since_cross level={level}")


# ── 边界场景 ─────────────────────────────────────────────────────────────────


def test_empty_array():
    """空数组应返回空列表，不报错。"""
    empty = np.array([], dtype=np.float64)
    assert _batch_delta(empty, 3) == []
    assert _batch_slope(empty, 3) == []
    assert _batch_zscore(empty, 3) == []
    assert _batch_bars_since_cross(empty, 50.0) == []


def test_all_nan_array():
    """全 NaN 数组应全返回 None。"""
    nan_arr = np.full(20, np.nan)
    assert all(v is None for v in _batch_delta(nan_arr, 3))
    assert all(v is None for v in _batch_slope(nan_arr, 3))
    assert all(v is None for v in _batch_zscore(nan_arr, 3))


def test_zscore_constant_window_returns_none():
    """std ≈ 0 应返回 None。"""
    const = np.full(20, 7.0)
    out = _batch_zscore(const, 5)
    # 第 4 个开始有窗口，但 std=0
    assert all(v is None for v in out[4:])


def test_bars_since_cross_no_cross_returns_none():
    """从未穿越的数组应全 None（除最初）。"""
    above = np.full(10, 100.0)
    out = _batch_bars_since_cross(above, 50.0)
    assert all(v is None for v in out)


# ── JIT 内层函数返回类型契约 ────────────────────────────────────────────────


def test_jit_funcs_return_ndarray():
    """JIT 内层必须返回 np.ndarray（不能是 list）—— 确保接口契约。"""
    arr = np.arange(20, dtype=np.float64)
    assert isinstance(_batch_delta_jit(arr, 3), np.ndarray)
    assert isinstance(_batch_slope_jit(arr, 3), np.ndarray)
    assert isinstance(_batch_zscore_jit(arr, 3), np.ndarray)
    assert isinstance(_batch_bars_since_cross_jit(arr, 10.0), np.ndarray)
