"""src/research/features/cross_tf/provider.py

CrossTFFeatureProvider — 跨时间框架特征集。

计算以下特征（均挂在 group="cross_tf"）：

  父 TF 对齐特征（前向填充）：
    parent_trend_dir    — 父 TF supertrend_direction 对齐值            Role: WHY
    parent_rsi          — 父 TF rsi14 对齐值                           Role: WHY
    parent_adx          — 父 TF adx14 对齐值                           Role: WHY
    parent_bb_pos       — 父 TF bb_position 对齐值                     Role: WHERE

  复合特征：
    tf_trend_align      — sign(child_trend) × sign(parent_trend)       Role: WHY
                          +1=顺势，-1=逆势；子趋势缺失→None
    dist_to_parent_ema  — (close - parent_ema50) / ATR                Role: WHERE
                          ATR 缺失或为 0 → None
    parent_rsi_delta_5  — 对齐后父 RSI 序列的 5-bar 变化量             Role: WHY
    parent_adx_delta_5  — 对齐后父 ADX 序列的 5-bar 变化量             Role: WHY

数据对齐算法：使用 numpy searchsorted 将父 TF 时间戳前向填充到子 TF bars。

性能（Phase R.2，2026-04-22 实施）：
  3 个 `_compute_*` 函数从 per-bar Python 循环改为 Numba `@njit` 内层。
  公开 API（返回 List[Optional[float]]）保持不变以兼容下游消费者。
  数值精度与原实现完全等价（无浮点漂移，仅消除 dispatch / Python 层开销）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.research.core.config import CrossTFProviderConfig
from src.research.features.protocol import (
    FeatureProvider,
    FeatureRole,
    ProviderDataRequirement,
)

try:
    from numba import njit  # type: ignore[import-untyped]

    _NUMBA_AVAILABLE = True
except ImportError:  # pragma: no cover - numba 未装走 NumPy fallback
    _NUMBA_AVAILABLE = False

    def njit(*args, **kwargs):  # type: ignore[no-redef]
        def _identity(fn):
            return fn

        # 兼容 @njit(...) 和 @njit 两种调用形式
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return _identity


# 固定特征数
_FEATURE_COUNT = 8

# delta 窗口
_DELTA_WINDOW = 5

# 子 TF 趋势方向指标的 indicator_series 键
_CHILD_TREND_KEY: Tuple[str, str] = ("supertrend", "direction")

# 子 TF ATR 指标的 indicator_series 键
_CHILD_ATR_KEY: Tuple[str, str] = ("atr14", "atr")


class CrossTFFeatureProvider:
    """跨时间框架特征计算器。

    满足 FeatureProvider Protocol。

    通过 required_extra_data() 声明所需的父 TF 数据；
    compute() 接收调用方注入的 extra_data 字典执行对齐与特征计算。
    当 extra_data 为 None 或空字典时，优雅降级返回空字典。
    """

    def __init__(
        self,
        config: Optional[CrossTFProviderConfig] = None,
    ) -> None:
        self._cfg = config or CrossTFProviderConfig()

    # ------------------------------------------------------------------
    # FeatureProvider Protocol
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "cross_tf"

    @property
    def feature_count(self) -> int:
        return _FEATURE_COUNT

    def required_columns(self) -> List[Tuple[str, str]]:
        """声明 compute() 依赖的子 TF indicator_series 键。"""
        return [_CHILD_TREND_KEY, _CHILD_ATR_KEY]

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        """声明跨 TF 数据需求——父 TF 映射与所需指标名称。"""
        return ProviderDataRequirement(
            parent_tf_mapping=dict(self._cfg.parent_tf_map),
            parent_indicators=list(self._cfg.parent_indicators),
        )

    def role_mapping(self) -> Dict[str, FeatureRole]:
        return {
            "parent_trend_dir": FeatureRole.WHY,
            "parent_rsi": FeatureRole.WHY,
            "parent_adx": FeatureRole.WHY,
            "tf_trend_align": FeatureRole.WHY,
            "dist_to_parent_ema": FeatureRole.WHERE,
            "parent_rsi_delta_5": FeatureRole.WHY,
            "parent_adx_delta_5": FeatureRole.WHY,
            "parent_bb_pos": FeatureRole.WHERE,
        }

    def compute(
        self,
        matrix: Any,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        """执行跨 TF 特征计算。

        Args:
            matrix:     DataMatrix 实例
            extra_data: 调用方注入的父 TF 数据；为 None 或空时返回 {}

        Returns:
            8 个特征列，格式与 DataMatrix.indicator_series 一致。
            当 extra_data 无效时，返回空字典（优雅降级）。
        """
        # --- 优雅降级：无父 TF 数据时返回空字典 ---
        if not extra_data:
            return {}

        n: int = matrix.n_bars
        if n == 0:
            return {}

        # --- 解析 extra_data ---
        parent_bar_times = extra_data.get("parent_bar_times")
        parent_indicators: Dict[str, List[float]] = extra_data.get(
            "parent_indicators", {}
        )

        if not parent_bar_times or not parent_indicators:
            return {}

        # --- 时间对齐：子 TF → 父 TF 前向填充 ---
        child_ts = np.array([t.timestamp() for t in matrix.bar_times], dtype=np.float64)
        parent_ts = np.array(
            [t.timestamp() for t in parent_bar_times], dtype=np.float64
        )
        n_parent = len(parent_ts)

        # searchsorted(side="right") - 1：对每个子 TF 时间戳，找到最近且不超过它的父 TF bar
        raw_idx = np.searchsorted(parent_ts, child_ts, side="right") - 1
        idx = np.clip(raw_idx, 0, n_parent - 1)

        # --- 对齐各父 TF 指标序列 ---
        def _align(ind_name: str) -> Optional[np.ndarray]:
            vals = parent_indicators.get(ind_name)
            if vals is None:
                return None
            arr = np.array(vals, dtype=np.float64)
            return arr[idx]

        aligned_trend = _align("supertrend_direction")
        aligned_rsi = _align("rsi14")
        aligned_adx = _align("adx14")
        aligned_ema = _align("ema50")
        aligned_bb = _align("bb_position")

        result: Dict[Tuple[str, str], List[Optional[float]]] = {}

        # --- 1. parent_trend_dir ---
        result[("cross_tf", "parent_trend_dir")] = _array_to_list(aligned_trend, n)

        # --- 2. parent_rsi ---
        result[("cross_tf", "parent_rsi")] = _array_to_list(aligned_rsi, n)

        # --- 3. parent_adx ---
        result[("cross_tf", "parent_adx")] = _array_to_list(aligned_adx, n)

        # --- 4. parent_bb_pos ---
        result[("cross_tf", "parent_bb_pos")] = _array_to_list(aligned_bb, n)

        # --- 5. tf_trend_align = sign(child_trend) × sign(parent_trend) ---
        result[("cross_tf", "tf_trend_align")] = _compute_trend_align(
            matrix, aligned_trend, n
        )

        # --- 6. dist_to_parent_ema = (close - aligned_ema) / ATR ---
        result[("cross_tf", "dist_to_parent_ema")] = _compute_dist_to_ema(
            matrix, aligned_ema, n
        )

        # --- 7. parent_rsi_delta_5 ---
        result[("cross_tf", "parent_rsi_delta_5")] = _compute_delta(
            aligned_rsi, _DELTA_WINDOW, n
        )

        # --- 8. parent_adx_delta_5 ---
        result[("cross_tf", "parent_adx_delta_5")] = _compute_delta(
            aligned_adx, _DELTA_WINDOW, n
        )

        return result


# ---------------------------------------------------------------------------
# 内部计算函数
# ---------------------------------------------------------------------------


def _array_to_list(arr: Optional[np.ndarray], n: int) -> List[Optional[float]]:
    """将 numpy 数组转换为 Optional[float] 列表；arr 为 None 时全返回 None。"""
    if arr is None:
        return [None] * n
    out: List[Optional[float]] = []
    for v in arr:
        out.append(float(v) if np.isfinite(v) else None)
    return out


def _optional_list_to_array(
    series: Optional[List[Optional[float]]], n: int
) -> np.ndarray:
    """List[Optional[float]] → np.ndarray (None / 越界 → NaN)。"""
    arr = np.full(n, np.nan)
    if series is None:
        return arr
    limit = min(n, len(series))
    for i in range(limit):
        v = series[i]
        if v is not None:
            arr[i] = v
    return arr


def _array_to_optional_list(arr: np.ndarray) -> List[Optional[float]]:
    """np.ndarray (NaN=None) → List[Optional[float]] for downstream API 兼容。"""
    return [None if np.isnan(v) else float(v) for v in arr]


# ── Numba JIT 内层（Phase R.2） ──────────────────────────────────────────


@njit(cache=True)
def _compute_trend_align_jit(
    parent_trend: np.ndarray, child_trend: np.ndarray, n: int
) -> np.ndarray:
    """sign(child) × sign(parent)；任一为 NaN 或 0 → NaN。"""
    out = np.full(n, np.nan)
    for i in range(n):
        p = parent_trend[i]
        if np.isnan(p) or p == 0.0:
            continue
        c = child_trend[i]
        if np.isnan(c) or c == 0.0:
            continue
        child_sign = 1.0 if c > 0.0 else -1.0
        parent_sign = 1.0 if p > 0.0 else -1.0
        out[i] = child_sign * parent_sign
    return out


@njit(cache=True)
def _compute_dist_to_ema_jit(
    closes: np.ndarray, aligned_ema: np.ndarray, atr: np.ndarray, n: int
) -> np.ndarray:
    """(close - ema) / atr；任一缺失或 atr<1e-9 → NaN。"""
    out = np.full(n, np.nan)
    for i in range(n):
        c = closes[i]
        if np.isnan(c):
            continue
        e = aligned_ema[i]
        if np.isnan(e):
            continue
        a = atr[i]
        if np.isnan(a) or a < 1e-9:
            continue
        out[i] = (c - e) / a
    return out


@njit(cache=True)
def _compute_delta_jit(arr: np.ndarray, window: int) -> np.ndarray:
    """arr[i] - arr[i-w]；i<w 或涉及 NaN → NaN。"""
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(n):
        if i < window:
            continue
        cur = arr[i]
        ref = arr[i - window]
        if np.isnan(cur) or np.isnan(ref):
            continue
        out[i] = cur - ref
    return out


def _compute_trend_align(
    matrix: Any,
    aligned_parent_trend: Optional[np.ndarray],
    n: int,
) -> List[Optional[float]]:
    """计算 tf_trend_align = sign(child_trend) × sign(parent_trend)。

    子趋势方向从 matrix.indicator_series[("supertrend","direction")] 读取。
    父趋势或子趋势缺失/为零时，对应 bar 返回 None。
    """
    if aligned_parent_trend is None:
        return [None] * n

    ind_series: Dict[Tuple[str, str], List[Optional[float]]] = matrix.indicator_series
    child_trend_arr = _optional_list_to_array(ind_series.get(_CHILD_TREND_KEY), n)
    return _array_to_optional_list(
        _compute_trend_align_jit(aligned_parent_trend, child_trend_arr, n)
    )


def _compute_dist_to_ema(
    matrix: Any,
    aligned_ema: Optional[np.ndarray],
    n: int,
) -> List[Optional[float]]:
    """计算 (close - aligned_parent_ema) / ATR。

    ATR 从 matrix.indicator_series[("atr14","atr")] 读取。
    ATR 缺失、为零或 close 缺失时返回 None。
    """
    if aligned_ema is None:
        return [None] * n

    closes_list: Optional[List[Optional[float]]] = getattr(matrix, "closes", None)
    closes_arr = _optional_list_to_array(closes_list, n)

    ind_series: Dict[Tuple[str, str], List[Optional[float]]] = matrix.indicator_series
    atr_arr = _optional_list_to_array(ind_series.get(_CHILD_ATR_KEY), n)

    return _array_to_optional_list(
        _compute_dist_to_ema_jit(closes_arr, aligned_ema, atr_arr, n)
    )


def _compute_delta(
    aligned: Optional[np.ndarray],
    window: int,
    n: int,
) -> List[Optional[float]]:
    """计算对齐后序列的 w-bar 变化量：aligned[i] - aligned[i-w]。

    前 w 个 bar 或涉及 nan 时返回 None。
    """
    if aligned is None:
        return [None] * n
    return _array_to_optional_list(_compute_delta_jit(aligned, window))
