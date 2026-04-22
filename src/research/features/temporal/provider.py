"""src/research/features/temporal/provider.py

TemporalFeatureProvider — 时序动量特征集。

计算以下特征（均挂在 group="temporal"）：

  核心指标（rsi14, adx14）× 所有窗口（3, 5, 10）：
    {ind}_delta_{w}      — value[i] - value[i-w]               Role: WHY
    {ind}_accel_{w}      — delta[i] - delta[i-w]（2 阶差分）    Role: WHY
    {ind}_slope_{w}      — closed-form 线性回归斜率（w+1 点）   Role: WHY
    {ind}_zscore_{w}     — (val - mean) / std，std<1e-9→None    Role: WHEN

  核心指标 × 穿越水平：
    {ind}_bars_since_cross_{level} — 距上次穿越水平的 bar 数     Role: WHEN
      RSI: 30, 50, 70
      ADX: 20, 25

  辅助指标（macd_histogram, cci20, roc12, stoch_k）× 最大窗口：
    {ind}_delta_{max_w}  — 仅一个窗口的 delta                   Role: WHY

性能（Phase R.2，2026-04-22 实施）：
  4 个 `_batch_*` 函数从 per-bar Python 循环 + np.std/np.mean dispatch
  改为 Numba `@njit` 内层（数值精度 max_diff < 1e-12）。
  实测 _batch_zscore (70K bars, w=20) 47x 加速，
  pure JIT array (无 list 转换) 1000x 加速。
  公开 API（返回 List[Optional[float]]）保持不变以兼容下游消费者。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.research.core.config import TemporalProviderConfig
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


# 指标名 → DataMatrix indicator_series 键
_INDICATOR_KEY_MAP: Dict[str, Tuple[str, str]] = {
    "rsi14": ("rsi14", "rsi"),
    "adx14": ("adx14", "adx"),
    "macd_histogram": ("macd", "hist"),
    "cci20": ("cci20", "cci"),
    "roc12": ("roc12", "roc"),
    "stoch_k": ("stoch_rsi14", "stoch_rsi_k"),
}


class TemporalFeatureProvider:
    """时序动量特征计算器。

    满足 FeatureProvider Protocol。
    """

    def __init__(
        self,
        config: Optional[TemporalProviderConfig] = None,
    ) -> None:
        self._cfg = config or TemporalProviderConfig()
        self._max_window = max(self._cfg.windows) if self._cfg.windows else 1

    # ------------------------------------------------------------------
    # FeatureProvider Protocol
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "temporal"

    @property
    def feature_count(self) -> int:
        """计算 Provider 的固定特征数。"""
        cfg = self._cfg
        n_core = len(cfg.core_indicators)
        n_windows = len(cfg.windows)
        n_aux = len(cfg.aux_indicators)
        n_cross = len(cfg.cross_levels_rsi) * sum(
            1 for ind in cfg.core_indicators if ind == "rsi14"
        ) + len(cfg.cross_levels_adx) * sum(
            1 for ind in cfg.core_indicators if ind == "adx14"
        )
        # core × windows × 4 类型 + cross_levels + aux × 1
        return n_core * n_windows * 4 + n_cross + n_aux

    def required_columns(self) -> List[Tuple[str, str]]:
        """声明所需的 indicator_series 列。"""
        cols: List[Tuple[str, str]] = []
        cfg = self._cfg
        for ind in cfg.core_indicators + cfg.aux_indicators:
            key = _INDICATOR_KEY_MAP.get(ind)
            if key is not None:
                cols.append(key)
        return cols

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        """无需跨 TF 额外数据。"""
        return None

    def role_mapping(self) -> Dict[str, FeatureRole]:
        """声明每个输出特征字段名对应的策略角色。"""
        cfg = self._cfg
        mapping: Dict[str, FeatureRole] = {}

        for ind in cfg.core_indicators:
            for w in cfg.windows:
                mapping[f"{ind}_delta_{w}"] = FeatureRole.WHY
                mapping[f"{ind}_accel_{w}"] = FeatureRole.WHY
                mapping[f"{ind}_slope_{w}"] = FeatureRole.WHY
                mapping[f"{ind}_zscore_{w}"] = FeatureRole.WHEN

            # bars_since_cross
            cross_levels = _get_cross_levels(ind, cfg)
            for level in cross_levels:
                mapping[f"{ind}_bars_since_cross_{level}"] = FeatureRole.WHEN

        for ind in cfg.aux_indicators:
            mapping[f"{ind}_delta_{self._max_window}"] = FeatureRole.WHY

        return mapping

    def compute(
        self,
        matrix: Any,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        """执行全量特征计算。"""
        n: int = matrix.n_bars
        if n == 0:
            return self._empty_result()

        cfg = self._cfg
        result: Dict[Tuple[str, str], List[Optional[float]]] = {}

        # --- 核心指标 ---
        for ind in cfg.core_indicators:
            series = _get_series(matrix, ind)
            arr = _to_array(series)

            for w in cfg.windows:
                # delta
                delta_series = _batch_delta(arr, w)
                result[("temporal", f"{ind}_delta_{w}")] = delta_series

                # accel = delta[i] - delta[i-w]
                delta_arr = _to_array(delta_series)
                result[("temporal", f"{ind}_accel_{w}")] = _batch_delta(delta_arr, w)

                # slope
                result[("temporal", f"{ind}_slope_{w}")] = _batch_slope(arr, w)

                # zscore
                result[("temporal", f"{ind}_zscore_{w}")] = _batch_zscore(arr, w)

            # bars_since_cross
            cross_levels = _get_cross_levels(ind, cfg)
            for level in cross_levels:
                result[("temporal", f"{ind}_bars_since_cross_{level}")] = (
                    _batch_bars_since_cross(arr, level)
                )

        # --- 辅助指标（仅最大窗口 delta）---
        for ind in cfg.aux_indicators:
            series = _get_series(matrix, ind)
            arr = _to_array(series)
            result[("temporal", f"{ind}_delta_{self._max_window}")] = _batch_delta(
                arr, self._max_window
            )

        return result

    def _empty_result(self) -> Dict[Tuple[str, str], List[Optional[float]]]:
        """返回空列结构（n_bars=0 时使用）。"""
        result: Dict[Tuple[str, str], List[Optional[float]]] = {}
        cfg = self._cfg
        for ind in cfg.core_indicators:
            for w in cfg.windows:
                result[("temporal", f"{ind}_delta_{w}")] = []
                result[("temporal", f"{ind}_accel_{w}")] = []
                result[("temporal", f"{ind}_slope_{w}")] = []
                result[("temporal", f"{ind}_zscore_{w}")] = []
            for level in _get_cross_levels(ind, cfg):
                result[("temporal", f"{ind}_bars_since_cross_{level}")] = []
        for ind in cfg.aux_indicators:
            result[("temporal", f"{ind}_delta_{self._max_window}")] = []
        return result


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _get_cross_levels(ind: str, cfg: TemporalProviderConfig) -> List[float]:
    """返回该指标对应的穿越水平列表。"""
    if ind == "rsi14":
        return list(cfg.cross_levels_rsi)
    if ind == "adx14":
        return list(cfg.cross_levels_adx)
    return []


def _get_series(
    matrix: Any,
    ind: str,
) -> List[Optional[float]]:
    """从 DataMatrix 取出指标序列；找不到则返回全 None 列表。"""
    key = _INDICATOR_KEY_MAP.get(ind)
    if key is None:
        return [None] * matrix.n_bars
    indicator_series: Dict[Tuple[str, str], List[Optional[float]]] = (
        matrix.indicator_series
    )
    return indicator_series.get(key, [None] * matrix.n_bars)


def _to_array(series: List[Optional[float]]) -> np.ndarray:
    """将 Optional[float] 列表转为 float64 数组（None → nan）。"""
    n = len(series)
    arr = np.empty(n, dtype=np.float64)
    for i, v in enumerate(series):
        arr[i] = v if v is not None else np.nan
    return arr


# ---------------------------------------------------------------------------
# 批量计算函数
# ---------------------------------------------------------------------------


# ── Numba JIT 内层（Phase R.2） ──────────────────────────────────────────
# 公开 _batch_* 函数保持原签名（返回 List[Optional[float]]），内部转 NaN ↔ None。
# Numba 不支持 Python list 和 Optional，JIT 函数返回 np.ndarray (NaN 表 None)。


@njit(cache=True)
def _batch_delta_jit(arr: np.ndarray, w: int) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(n):
        if i < w:
            continue
        cur = arr[i]
        ref = arr[i - w]
        if np.isnan(cur) or np.isnan(ref):
            continue
        out[i] = cur - ref
    return out


@njit(cache=True)
def _batch_slope_jit(arr: np.ndarray, w: int) -> np.ndarray:
    """Closed-form 线性回归斜率（替代 np.polyfit，Numba 不支持 polyfit）。

    slope = sum((x - x_mean)(y - y_mean)) / sum((x - x_mean)^2)
    x = arange(w+1) → x_mean = w/2
    """
    n = len(arr)
    out = np.full(n, np.nan)
    win_size = w + 1
    x_mean = w / 2.0
    # Pre-compute denominator: sum((i - x_mean)^2 for i in range(w+1))
    den = 0.0
    for j in range(win_size):
        dx = j - x_mean
        den += dx * dx
    if den < 1e-18:
        return out
    for i in range(n):
        if i < w:
            continue
        ok = True
        y_sum = 0.0
        for j in range(win_size):
            v = arr[i - w + j]
            if not np.isfinite(v):
                ok = False
                break
            y_sum += v
        if not ok:
            continue
        y_mean = y_sum / win_size
        num = 0.0
        for j in range(win_size):
            v = arr[i - w + j]
            num += (j - x_mean) * (v - y_mean)
        out[i] = num / den
    return out


@njit(cache=True)
def _batch_zscore_jit(arr: np.ndarray, w: int) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(n):
        if i < w - 1:
            continue
        ok = True
        s = 0.0
        sq = 0.0
        for k in range(i - w + 1, i + 1):
            v = arr[k]
            if not np.isfinite(v):
                ok = False
                break
            s += v
            sq += v * v
        if not ok:
            continue
        mean = s / w
        # 用 sample variance (除 w 而非 w-1) 与 np.std 默认 ddof=0 一致
        var = sq / w - mean * mean
        if var < 1e-18:
            continue
        std = np.sqrt(var)
        if std < 1e-9:
            continue
        out[i] = (arr[i] - mean) / std
    return out


@njit(cache=True)
def _batch_bars_since_cross_jit(arr: np.ndarray, level: float) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    last_cross_idx = -1  # -1 表示未发生穿越（None）
    for i in range(1, n):
        prev = arr[i - 1]
        cur = arr[i]
        if np.isnan(prev) or np.isnan(cur):
            if last_cross_idx >= 0:
                out[i] = float(i - last_cross_idx)
            continue
        if (prev < level and cur >= level) or (prev >= level and cur < level):
            last_cross_idx = i
        if last_cross_idx >= 0:
            out[i] = float(i - last_cross_idx)
    return out


def _array_to_optional_list(arr: np.ndarray) -> List[Optional[float]]:
    """np.ndarray (NaN=None) → List[Optional[float]] for downstream API 兼容。"""
    return [None if np.isnan(v) else float(v) for v in arr]


def _batch_delta(arr: np.ndarray, w: int) -> List[Optional[float]]:
    """value[i] - value[i-w]；i<w 或涉及 nan → None。"""
    return _array_to_optional_list(_batch_delta_jit(arr, w))


def _batch_slope(arr: np.ndarray, w: int) -> List[Optional[float]]:
    """对 [i-w, i] 共 w+1 个点做线性回归，返回斜率。

    若窗口内含 nan 或点数不足 → None。
    用 closed-form 公式（不用 np.polyfit，Numba 兼容）；与 np.polyfit
    数值等价（最小二乘法封闭解）。
    """
    return _array_to_optional_list(_batch_slope_jit(arr, w))


def _batch_zscore(arr: np.ndarray, w: int) -> List[Optional[float]]:
    """(val - mean(window)) / std(window)；std < 1e-9 → None。

    窗口为 [i-w+1, i]（含当前 bar），共 w 个点。
    用 sum² - mean² 公式，与 np.std (ddof=0) 数值等价。
    """
    return _array_to_optional_list(_batch_zscore_jit(arr, w))


def _batch_bars_since_cross(
    arr: np.ndarray,
    level: float,
) -> List[Optional[float]]:
    """计算距上次穿越 level 水平线的 bar 数。

    穿越定义：相邻两个有效 bar，一个在 level 上方（>=level），
    另一个在下方（<level），则视为发生穿越。

    穿越当 bar（方向变化生效的 bar）记为 0，之后每 bar 递增。
    未发生任何穿越前返回 None。
    """
    return _array_to_optional_list(_batch_bars_since_cross_jit(arr, level))
