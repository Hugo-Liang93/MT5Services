"""src/research/features/microstructure/provider.py

MicrostructureFeatureProvider — K 线微结构特征集。

计算以下特征（均挂在 group="microstructure"）：

  迁移自 engineer.py 的批量特征（8 项）：
    close_in_range        — (close-low)/(high-low)，range<1e-9 时返回 0.5
    body_ratio            — |close-open|/(high-low)
    upper_wick_ratio      — (high-max(open,close))/(high-low)
    lower_wick_ratio      — (min(open,close)-low)/(high-low)
    range_expansion       — (high-low)/ATR14，需 ("atr14","atr")
    oc_imbalance          — (close-open)/(high-low)
    close_to_close_3      — (close[i]-close[i-3])/close[i-3]
    consecutive_same_color — 带符号连续同色 bar 计数（上限 10）

  新增特征（13 项，{w}=lookback）：
    consecutive_up        — 连续 close>close[-1] 的 bar 计数（下跌时重置为 0）
    consecutive_down      — 连续 close<close[-1] 的 bar 计数（上涨时重置为 0）
    vol_price_accord_{w}  — Σ sign(close_delta)×sign(vol_delta)/w
    volatility_ratio_{w}  — ATR[i]/mean(ATR[i-w:i])，需 ("atr14","atr")
    bb_width_change_{w}   — bb_width[i]/bb_width[i-w]，需 ("bb20","bb_width")
    avg_body_ratio_{w}    — body_ratio 过去 w bar 均值
    upper_shadow_ratio_{w} — upper_wick_ratio 过去 w bar 均值
    lower_shadow_ratio_{w} — lower_wick_ratio 过去 w bar 均值
    hh_count_{w}          — 过去 w bar 中 high[i]>high[i-1] 的次数
    ll_count_{w}          — 过去 w bar 中 low[i]<low[i-1] 的次数
    gap_ratio             — (open[i]-close[i-1])/ATR，需 ("atr14","atr")
    range_position_{w}    — (close-min(low,w))/(max(high,w)-min(low,w))
    volume_surge          — volume[i]/mean(volume[i-w:i])

性能（Phase R.2，2026-04-22 实施）：
  12 个 per-bar 循环函数（consecutive_*, vol_price_accord, volatility_ratio,
  bb_width_change, rolling_mean, hh_count, ll_count, gap_ratio,
  range_position, volume_surge）改为 Numba `@njit` 内层。
  公开 API 签名/语义不变；3 个已纯 numpy 向量化的函数（close_in_range /
  oc_imbalance / close_to_close_3）保持原样以避免无谓的 JIT overhead。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.research.core.config import MicrostructureProviderConfig
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


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_GROUP = "microstructure"
_ATR_KEY: Tuple[str, str] = ("atr14", "atr")
_BB_WIDTH_KEY: Tuple[str, str] = ("bb20", "bb_width")


class MicrostructureFeatureProvider:
    """K 线微结构特征计算器。

    满足 FeatureProvider Protocol。
    所有计算均基于 numpy 向量化，仅窗口滚动类特征在外层 Python 循环。
    """

    def __init__(
        self,
        config: Optional[MicrostructureProviderConfig] = None,
    ) -> None:
        self._cfg = config or MicrostructureProviderConfig()
        self._w = self._cfg.lookback

    # ------------------------------------------------------------------
    # FeatureProvider Protocol
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return _GROUP

    @property
    def feature_count(self) -> int:
        # 固定特征 8 项 + 窗口特征 13 项
        return 8 + 13

    def required_columns(self) -> List[Tuple[str, str]]:
        """声明所需的 indicator_series 列。"""
        return [_ATR_KEY, _BB_WIDTH_KEY]

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        """无需跨 TF 额外数据。"""
        return None

    def role_mapping(self) -> Dict[str, FeatureRole]:
        w = self._w
        mapping: Dict[str, FeatureRole] = {
            # WHERE
            "close_in_range": FeatureRole.WHERE,
            "upper_wick_ratio": FeatureRole.WHERE,
            "lower_wick_ratio": FeatureRole.WHERE,
            # WHEN
            "body_ratio": FeatureRole.WHEN,
            "range_expansion": FeatureRole.WHEN,
            "close_to_close_3": FeatureRole.WHEN,
            "gap_ratio": FeatureRole.WHEN,
            # WHY
            "oc_imbalance": FeatureRole.WHY,
            "consecutive_same_color": FeatureRole.WHY,
            "consecutive_up": FeatureRole.WHY,
            "consecutive_down": FeatureRole.WHY,
            # 窗口特征
            f"vol_price_accord_{w}": FeatureRole.VOLUME,
            f"volatility_ratio_{w}": FeatureRole.WHEN,
            f"bb_width_change_{w}": FeatureRole.WHEN,
            f"avg_body_ratio_{w}": FeatureRole.WHEN,
            f"upper_shadow_ratio_{w}": FeatureRole.WHERE,
            f"lower_shadow_ratio_{w}": FeatureRole.WHERE,
            f"hh_count_{w}": FeatureRole.WHY,
            f"ll_count_{w}": FeatureRole.WHY,
            f"range_position_{w}": FeatureRole.WHERE,
            "volume_surge": FeatureRole.VOLUME,
        }
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

        # --- 一次性转换为 numpy 数组 ---
        opens = np.asarray(matrix.opens, dtype=np.float64)
        highs = np.asarray(matrix.highs, dtype=np.float64)
        lows = np.asarray(matrix.lows, dtype=np.float64)
        closes = np.asarray(matrix.closes, dtype=np.float64)
        volumes = np.asarray(matrix.volumes, dtype=np.float64)

        # --- 中间变量（供多个特征复用）---
        rng = highs - lows
        safe_rng = np.where(rng < 1e-9, 1.0, rng)
        body_abs = np.abs(closes - opens)
        body_ratio_arr = np.where(rng < 1e-9, 0.0, body_abs / safe_rng)
        body_top = np.maximum(opens, closes)
        body_bot = np.minimum(opens, closes)
        upper_wick_arr = np.where(rng < 1e-9, 0.0, (highs - body_top) / safe_rng)
        lower_wick_arr = np.where(rng < 1e-9, 0.0, (body_bot - lows) / safe_rng)

        # --- ATR 序列（供多个特征复用）---
        atr_series = matrix.indicator_series.get(_ATR_KEY)
        atr_arr: Optional[np.ndarray] = None
        if atr_series is not None:
            atr_arr = np.asarray(
                [v if v is not None else float("nan") for v in atr_series],
                dtype=np.float64,
            )

        w = self._w
        result: Dict[Tuple[str, str], List[Optional[float]]] = {}

        # --- 固定特征 8 项 ---
        result[(_GROUP, "close_in_range")] = _batch_close_in_range(
            highs, lows, closes, rng
        )
        result[(_GROUP, "body_ratio")] = body_ratio_arr.tolist()
        result[(_GROUP, "upper_wick_ratio")] = upper_wick_arr.tolist()
        result[(_GROUP, "lower_wick_ratio")] = lower_wick_arr.tolist()
        result[(_GROUP, "range_expansion")] = _batch_range_expansion(
            highs, lows, rng, atr_arr
        )
        result[(_GROUP, "oc_imbalance")] = _batch_oc_imbalance(opens, closes, rng)
        result[(_GROUP, "close_to_close_3")] = _batch_close_to_close_3(closes, n)
        result[(_GROUP, "consecutive_same_color")] = _batch_consecutive_same_color(
            opens, closes, n
        )

        # --- 新增特征 13 项 ---
        result[(_GROUP, "consecutive_up")] = _batch_consecutive_up(closes, n)
        result[(_GROUP, "consecutive_down")] = _batch_consecutive_down(closes, n)
        result[(_GROUP, f"vol_price_accord_{w}")] = _batch_vol_price_accord(
            closes, volumes, n, w
        )
        result[(_GROUP, f"volatility_ratio_{w}")] = _batch_volatility_ratio(
            atr_arr, n, w
        )
        result[(_GROUP, f"bb_width_change_{w}")] = _batch_bb_width_change(
            matrix.indicator_series.get(_BB_WIDTH_KEY), n, w
        )
        result[(_GROUP, f"avg_body_ratio_{w}")] = _rolling_mean(body_ratio_arr, n, w)
        result[(_GROUP, f"upper_shadow_ratio_{w}")] = _rolling_mean(
            upper_wick_arr, n, w
        )
        result[(_GROUP, f"lower_shadow_ratio_{w}")] = _rolling_mean(
            lower_wick_arr, n, w
        )
        result[(_GROUP, f"hh_count_{w}")] = _batch_hh_count(highs, n, w)
        result[(_GROUP, f"ll_count_{w}")] = _batch_ll_count(lows, n, w)
        result[(_GROUP, "gap_ratio")] = _batch_gap_ratio(opens, closes, atr_arr, n)
        result[(_GROUP, f"range_position_{w}")] = _batch_range_position(
            highs, lows, closes, n, w
        )
        result[(_GROUP, "volume_surge")] = _batch_volume_surge(volumes, n, w)

        return result

    def _empty_result(self) -> Dict[Tuple[str, str], List[Optional[float]]]:
        """返回所有列均为空列表的结果。"""
        w = self._w
        keys = [
            "close_in_range",
            "body_ratio",
            "upper_wick_ratio",
            "lower_wick_ratio",
            "range_expansion",
            "oc_imbalance",
            "close_to_close_3",
            "consecutive_same_color",
            "consecutive_up",
            "consecutive_down",
            f"vol_price_accord_{w}",
            f"volatility_ratio_{w}",
            f"bb_width_change_{w}",
            f"avg_body_ratio_{w}",
            f"upper_shadow_ratio_{w}",
            f"lower_shadow_ratio_{w}",
            f"hh_count_{w}",
            f"ll_count_{w}",
            "gap_ratio",
            f"range_position_{w}",
            "volume_surge",
        ]
        return {(_GROUP, k): [] for k in keys}


# ---------------------------------------------------------------------------
# 内部批量计算函数（numpy 向量化）
# ---------------------------------------------------------------------------


def _batch_close_in_range(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    rng: np.ndarray,
) -> List[Optional[float]]:
    """(close-low)/(high-low)，range<1e-9 时返回 0.5。

    迁移自 engineer.py _batch_close_in_range。
    """
    out = np.where(rng < 1e-9, 0.5, (closes - lows) / np.where(rng < 1e-9, 1.0, rng))
    return [float(v) for v in out]


def _batch_oc_imbalance(
    opens: np.ndarray,
    closes: np.ndarray,
    rng: np.ndarray,
) -> List[Optional[float]]:
    """(close-open)/(high-low)，range<1e-9 时返回 0.0。

    迁移自 engineer.py _batch_oc_imbalance。
    """
    out = np.where(rng < 1e-9, 0.0, (closes - opens) / np.where(rng < 1e-9, 1.0, rng))
    return [float(v) for v in out]


def _batch_range_expansion(
    highs: np.ndarray,
    lows: np.ndarray,
    rng: np.ndarray,
    atr_arr: Optional[np.ndarray],
) -> List[Optional[float]]:
    """(high-low)/ATR14。ATR 缺失或 < 1e-9 的 bar 输出 None。

    迁移自 engineer.py _batch_range_expansion。
    """
    if atr_arr is None:
        return [None] * len(highs)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = rng / atr_arr
    out: List[Optional[float]] = []
    for v, a in zip(ratio, atr_arr):
        if not np.isfinite(v) or a < 1e-9:
            out.append(None)
        else:
            out.append(float(v))
    return out


def _batch_close_to_close_3(closes: np.ndarray, n: int) -> List[Optional[float]]:
    """(close[i]-close[i-3])/close[i-3]。

    迁移自 engineer.py _batch_close_to_close_3。
    """
    out: List[Optional[float]] = [None] * n
    if n <= 3:
        return out
    prev = closes[:-3]
    cur = closes[3:]
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(np.abs(prev) < 1e-9, np.nan, (cur - prev) / prev)
    for i, v in enumerate(ratio, start=3):
        out[i] = None if not np.isfinite(v) else float(v)
    return out


# ── Numba JIT 内层（Phase R.2） ──────────────────────────────────────────
# JIT 函数返回 np.ndarray (NaN 表 None)，公开 _batch_* 函数转换为
# List[Optional[float]] 以兼容下游（DataMatrix.indicator_series）。


@njit(cache=True)
def _consecutive_same_color_jit(
    opens: np.ndarray, closes: np.ndarray, n: int
) -> np.ndarray:
    out = np.zeros(n, dtype=np.float64)
    signs = np.zeros(n, dtype=np.int64)
    for i in range(n):
        if closes[i] > opens[i]:
            signs[i] = 1
        elif closes[i] < opens[i]:
            signs[i] = -1
    for i in range(n):
        s = signs[i]
        if s == 0:
            out[i] = 0.0
            continue
        run = 1
        # 与原版一致：j 范围为 (i-10, i)，最多回看 9 根
        lower = i - 10
        if lower < -1:
            lower = -1
        j = i - 1
        while j > lower:
            sj = signs[j]
            if sj == 0:
                break
            if sj == s:
                run += 1
            else:
                break
            j -= 1
        out[i] = float(run * s)
    return out


@njit(cache=True)
def _consecutive_up_jit(closes: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + 1.0
        else:
            out[i] = 0.0
    return out


@njit(cache=True)
def _consecutive_down_jit(closes: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        if closes[i] < closes[i - 1]:
            out[i] = out[i - 1] + 1.0
        else:
            out[i] = 0.0
    return out


@njit(cache=True)
def _vol_price_accord_jit(
    closes: np.ndarray, volumes: np.ndarray, n: int, w: int
) -> np.ndarray:
    """sign(close_delta) × sign(vol_delta) 在窗口 [i-w+1, i] 上求均值。

    与原版语义一致：sign_cd[0]=sign_vd[0]=0；窗口 < w bars 时返回 NaN。
    """
    out = np.full(n, np.nan)
    sign_cd = np.zeros(n, dtype=np.float64)
    sign_vd = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        cd = closes[i] - closes[i - 1]
        if cd > 0.0:
            sign_cd[i] = 1.0
        elif cd < 0.0:
            sign_cd[i] = -1.0
        vd = volumes[i] - volumes[i - 1]
        if vd > 0.0:
            sign_vd[i] = 1.0
        elif vd < 0.0:
            sign_vd[i] = -1.0

    for i in range(w, n):
        s = 0.0
        for k in range(i - w + 1, i + 1):
            s += sign_cd[k] * sign_vd[k]
        out[i] = s / w
    return out


@njit(cache=True)
def _volatility_ratio_jit(atr_arr: np.ndarray, n: int, w: int) -> np.ndarray:
    """ATR[i]/mean(ATR[i-w:i])。任一非有限或均值<1e-9 → NaN。"""
    out = np.full(n, np.nan)
    for i in range(w, n):
        cur = atr_arr[i]
        if not np.isfinite(cur):
            continue
        s = 0.0
        ok = True
        for k in range(i - w, i):
            v = atr_arr[k]
            if not np.isfinite(v):
                ok = False
                break
            s += v
        if not ok:
            continue
        mean_w = s / w
        if mean_w < 1e-9:
            continue
        out[i] = cur / mean_w
    return out


@njit(cache=True)
def _bb_width_change_jit(arr: np.ndarray, n: int, w: int) -> np.ndarray:
    """arr[i]/arr[i-w]；任一 NaN 或 |ref|<1e-9 → NaN。"""
    out = np.full(n, np.nan)
    for i in range(w, n):
        cur = arr[i]
        ref = arr[i - w]
        if np.isnan(cur) or np.isnan(ref):
            continue
        if abs(ref) < 1e-9:
            continue
        out[i] = cur / ref
    return out


@njit(cache=True)
def _rolling_mean_jit(arr: np.ndarray, n: int, w: int) -> np.ndarray:
    """对 arr 计算窗口 [i-w+1, i] 滑动均值；i<w-1 → NaN。

    注意：与原版 `np.mean` 一致，不剔除 NaN（含 NaN → 输出 NaN）。
    """
    out = np.full(n, np.nan)
    for i in range(w - 1, n):
        s = 0.0
        for k in range(i - w + 1, i + 1):
            s += arr[k]
        out[i] = s / w
    return out


@njit(cache=True)
def _hh_count_jit(highs: np.ndarray, n: int, w: int) -> np.ndarray:
    """过去 w bar 中 high[j]>high[j-1] 的次数；i<w → NaN。"""
    out = np.full(n, np.nan)
    for i in range(w, n):
        c = 0
        for j in range(i - w + 1, i + 1):
            if highs[j] > highs[j - 1]:
                c += 1
        out[i] = float(c)
    return out


@njit(cache=True)
def _ll_count_jit(lows: np.ndarray, n: int, w: int) -> np.ndarray:
    """过去 w bar 中 low[j]<low[j-1] 的次数；i<w → NaN。"""
    out = np.full(n, np.nan)
    for i in range(w, n):
        c = 0
        for j in range(i - w + 1, i + 1):
            if lows[j] < lows[j - 1]:
                c += 1
        out[i] = float(c)
    return out


@njit(cache=True)
def _gap_ratio_jit(
    opens: np.ndarray, closes: np.ndarray, atr_arr: np.ndarray, n: int
) -> np.ndarray:
    """(open[i]-close[i-1])/ATR[i]；ATR 非有限或<1e-9 → NaN。"""
    out = np.full(n, np.nan)
    for i in range(1, n):
        a = atr_arr[i]
        if not np.isfinite(a) or a < 1e-9:
            continue
        out[i] = (opens[i] - closes[i - 1]) / a
    return out


@njit(cache=True)
def _range_position_jit(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, n: int, w: int
) -> np.ndarray:
    """(close - min_low) / (max_high - min_low)；spread<1e-9 → 0.5；i<w-1 → NaN。"""
    out = np.full(n, np.nan)
    for i in range(w - 1, n):
        h_max = highs[i - w + 1]
        l_min = lows[i - w + 1]
        for k in range(i - w + 2, i + 1):
            hv = highs[k]
            lv = lows[k]
            if hv > h_max:
                h_max = hv
            if lv < l_min:
                l_min = lv
        spread = h_max - l_min
        if spread < 1e-9:
            out[i] = 0.5
        else:
            out[i] = (closes[i] - l_min) / spread
    return out


@njit(cache=True)
def _volume_surge_jit(volumes: np.ndarray, n: int, w: int) -> np.ndarray:
    """volume[i]/mean(volume[i-w:i])；mean<1e-9 → NaN；i<w → NaN。"""
    out = np.full(n, np.nan)
    for i in range(w, n):
        s = 0.0
        for k in range(i - w, i):
            s += volumes[k]
        mean_w = s / w
        if mean_w < 1e-9:
            continue
        out[i] = volumes[i] / mean_w
    return out


# ── Helpers ────────────────────────────────────────────────────────────────


def _array_to_optional_list(arr: np.ndarray) -> List[Optional[float]]:
    """np.ndarray (NaN=None) → List[Optional[float]] for downstream API 兼容。"""
    return [None if np.isnan(v) else float(v) for v in arr]


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


# ── 公开 _batch_* 函数（保持原签名）──────────────────────────────────────


def _batch_consecutive_same_color(
    opens: np.ndarray, closes: np.ndarray, n: int
) -> List[Optional[float]]:
    """带符号连续同色 bar 数（上限 10）。"""
    arr = _consecutive_same_color_jit(opens, closes, n)
    return [float(v) for v in arr]


def _batch_consecutive_up(closes: np.ndarray, n: int) -> List[Optional[float]]:
    """连续 close>close[-1] 的 bar 计数，第一个 bar 为 0，下跌时重置为 0。"""
    arr = _consecutive_up_jit(closes, n)
    return [float(v) for v in arr]


def _batch_consecutive_down(closes: np.ndarray, n: int) -> List[Optional[float]]:
    """连续 close<close[-1] 的 bar 计数，第一个 bar 为 0，上涨时重置为 0。"""
    arr = _consecutive_down_jit(closes, n)
    return [float(v) for v in arr]


def _batch_vol_price_accord(
    closes: np.ndarray, volumes: np.ndarray, n: int, w: int
) -> List[Optional[float]]:
    """Σ sign(close_delta)×sign(vol_delta)/w，窗口不足时返回 None。"""
    return _array_to_optional_list(_vol_price_accord_jit(closes, volumes, n, w))


def _batch_volatility_ratio(
    atr_arr: Optional[np.ndarray], n: int, w: int
) -> List[Optional[float]]:
    """ATR[i]/mean(ATR[i-w:i])，窗口不足或 ATR 缺失时返回 None。"""
    if atr_arr is None:
        return [None] * n
    return _array_to_optional_list(_volatility_ratio_jit(atr_arr, n, w))


def _batch_bb_width_change(
    bb_series: Optional[List[Optional[float]]], n: int, w: int
) -> List[Optional[float]]:
    """bb_width[i]/bb_width[i-w]，缺失时返回 None。"""
    if bb_series is None:
        return [None] * n
    arr = _optional_list_to_array(bb_series, n)
    return _array_to_optional_list(_bb_width_change_jit(arr, n, w))


def _rolling_mean(arr: np.ndarray, n: int, w: int) -> List[Optional[float]]:
    """对 arr 计算过去 w bar 的滚动均值（含当前 bar）。窗口不足时返回 None。"""
    return _array_to_optional_list(_rolling_mean_jit(arr, n, w))


def _batch_hh_count(highs: np.ndarray, n: int, w: int) -> List[Optional[float]]:
    """过去 w bar 中 high[i]>high[i-1] 的次数（含当前 bar）。窗口不足时返回 None。"""
    return _array_to_optional_list(_hh_count_jit(highs, n, w))


def _batch_ll_count(lows: np.ndarray, n: int, w: int) -> List[Optional[float]]:
    """过去 w bar 中 low[i]<low[i-1] 的次数（含当前 bar）。窗口不足时返回 None。"""
    return _array_to_optional_list(_ll_count_jit(lows, n, w))


def _batch_gap_ratio(
    opens: np.ndarray,
    closes: np.ndarray,
    atr_arr: Optional[np.ndarray],
    n: int,
) -> List[Optional[float]]:
    """(open[i]-close[i-1])/ATR[i]，ATR 缺失或无前值时返回 None。"""
    if atr_arr is None:
        return [None] * n
    return _array_to_optional_list(_gap_ratio_jit(opens, closes, atr_arr, n))


def _batch_range_position(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    n: int,
    w: int,
) -> List[Optional[float]]:
    """(close-min(low,w))/(max(high,w)-min(low,w))。窗口不足时返回 None。"""
    return _array_to_optional_list(_range_position_jit(highs, lows, closes, n, w))


def _batch_volume_surge(volumes: np.ndarray, n: int, w: int) -> List[Optional[float]]:
    """volume[i]/mean(volume[i-w:i])，窗口不足时返回 None。"""
    return _array_to_optional_list(_volume_surge_jit(volumes, n, w))
