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
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.research.features.protocol import FeatureProvider, FeatureRole, ProviderDataRequirement
from src.research.core.config import CrossTFProviderConfig

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
        parent_indicators: Dict[str, List[float]] = extra_data.get("parent_indicators", {})

        if not parent_bar_times or not parent_indicators:
            return {}

        # --- 时间对齐：子 TF → 父 TF 前向填充 ---
        child_ts = np.array([t.timestamp() for t in matrix.bar_times], dtype=np.float64)
        parent_ts = np.array([t.timestamp() for t in parent_bar_times], dtype=np.float64)
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
        result[("cross_tf", "parent_rsi_delta_5")] = _compute_delta(aligned_rsi, _DELTA_WINDOW, n)

        # --- 8. parent_adx_delta_5 ---
        result[("cross_tf", "parent_adx_delta_5")] = _compute_delta(aligned_adx, _DELTA_WINDOW, n)

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
    child_trend_list = ind_series.get(_CHILD_TREND_KEY)

    out: List[Optional[float]] = []
    for i in range(n):
        # 父趋势
        p_val = aligned_parent_trend[i]
        if not np.isfinite(p_val) or p_val == 0.0:
            out.append(None)
            continue

        # 子趋势
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

    # 获取 close 序列（尝试 matrix.closes，否则全 None）
    closes: Optional[List[Optional[float]]] = getattr(matrix, "closes", None)

    # 获取 ATR 序列
    ind_series: Dict[Tuple[str, str], List[Optional[float]]] = matrix.indicator_series
    atr_list = ind_series.get(_CHILD_ATR_KEY)

    out: List[Optional[float]] = []
    for i in range(n):
        # close
        if closes is None or i >= len(closes):
            out.append(None)
            continue
        close_raw = closes[i]
        if close_raw is None:
            out.append(None)
            continue

        # EMA
        ema_val = aligned_ema[i]
        if not np.isfinite(ema_val):
            out.append(None)
            continue

        # ATR
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
