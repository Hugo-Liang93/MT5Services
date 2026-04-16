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
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.research.core.config import MicrostructureProviderConfig
from src.research.features.protocol import FeatureProvider, FeatureRole, ProviderDataRequirement

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
        result[(_GROUP, "close_in_range")] = _batch_close_in_range(highs, lows, closes, rng)
        result[(_GROUP, "body_ratio")] = body_ratio_arr.tolist()
        result[(_GROUP, "upper_wick_ratio")] = upper_wick_arr.tolist()
        result[(_GROUP, "lower_wick_ratio")] = lower_wick_arr.tolist()
        result[(_GROUP, "range_expansion")] = _batch_range_expansion(highs, lows, rng, atr_arr)
        result[(_GROUP, "oc_imbalance")] = _batch_oc_imbalance(opens, closes, rng)
        result[(_GROUP, "close_to_close_3")] = _batch_close_to_close_3(closes, n)
        result[(_GROUP, "consecutive_same_color")] = _batch_consecutive_same_color(opens, closes, n)

        # --- 新增特征 13 项 ---
        result[(_GROUP, "consecutive_up")] = _batch_consecutive_up(closes, n)
        result[(_GROUP, "consecutive_down")] = _batch_consecutive_down(closes, n)
        result[(_GROUP, f"vol_price_accord_{w}")] = _batch_vol_price_accord(closes, volumes, n, w)
        result[(_GROUP, f"volatility_ratio_{w}")] = _batch_volatility_ratio(atr_arr, n, w)
        result[(_GROUP, f"bb_width_change_{w}")] = _batch_bb_width_change(
            matrix.indicator_series.get(_BB_WIDTH_KEY), n, w
        )
        result[(_GROUP, f"avg_body_ratio_{w}")] = _rolling_mean(body_ratio_arr, n, w)
        result[(_GROUP, f"upper_shadow_ratio_{w}")] = _rolling_mean(upper_wick_arr, n, w)
        result[(_GROUP, f"lower_shadow_ratio_{w}")] = _rolling_mean(lower_wick_arr, n, w)
        result[(_GROUP, f"hh_count_{w}")] = _batch_hh_count(highs, n, w)
        result[(_GROUP, f"ll_count_{w}")] = _batch_ll_count(lows, n, w)
        result[(_GROUP, "gap_ratio")] = _batch_gap_ratio(opens, closes, atr_arr, n)
        result[(_GROUP, f"range_position_{w}")] = _batch_range_position(highs, lows, closes, n, w)
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


def _batch_consecutive_same_color(
    opens: np.ndarray, closes: np.ndarray, n: int
) -> List[Optional[float]]:
    """带符号连续同色 bar 数（上限 10）。

    迁移自 engineer.py _batch_consecutive_same_color。
    """
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


def _batch_consecutive_up(closes: np.ndarray, n: int) -> List[Optional[float]]:
    """连续 close>close[-1] 的 bar 计数，第一个 bar 为 0，下跌时重置为 0。"""
    out = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + 1.0
        else:
            out[i] = 0.0
    return [float(v) for v in out]


def _batch_consecutive_down(closes: np.ndarray, n: int) -> List[Optional[float]]:
    """连续 close<close[-1] 的 bar 计数，第一个 bar 为 0，上涨时重置为 0。"""
    out = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        if closes[i] < closes[i - 1]:
            out[i] = out[i - 1] + 1.0
        else:
            out[i] = 0.0
    return [float(v) for v in out]


def _batch_vol_price_accord(
    closes: np.ndarray, volumes: np.ndarray, n: int, w: int
) -> List[Optional[float]]:
    """Σ sign(close_delta)×sign(vol_delta)/w，窗口不足时返回 None。

    计算方式：取过去 w 个 bar（含当前 bar），每个 bar 计算其与前一 bar 的
    close_delta 和 vol_delta 的符号乘积，然后求均值。
    需要 i >= w 才有足够的 bar（i-w+1 到 i，共 w 个 bar，每个各自与前一 bar 对比）。
    """
    out: List[Optional[float]] = [None] * n
    # 预先计算 sign(close_delta) 和 sign(vol_delta)
    # sign_cd[i] = sign(closes[i] - closes[i-1])，i 从 1 开始
    sign_cd = np.zeros(n, dtype=np.float64)
    sign_vd = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        sign_cd[i] = float(np.sign(closes[i] - closes[i - 1]))
        sign_vd[i] = float(np.sign(volumes[i] - volumes[i - 1]))

    accord = sign_cd * sign_vd  # 元素乘积

    for i in range(w, n):
        # 取 i-w+1 到 i 的 accord（每个 bar 都是相对前一 bar 的比较，i-w+1 >= 1）
        # 如果 i-w+1 < 1，需要保证 i >= w（已由循环起点保证 i >= w >= 1）
        window = accord[i - w + 1 : i + 1]
        out[i] = float(np.sum(window) / w)
    return out


def _batch_volatility_ratio(
    atr_arr: Optional[np.ndarray], n: int, w: int
) -> List[Optional[float]]:
    """ATR[i]/mean(ATR[i-w:i])，窗口不足或 ATR 缺失时返回 None。"""
    if atr_arr is None:
        return [None] * n
    out: List[Optional[float]] = [None] * n
    for i in range(w, n):
        cur = atr_arr[i]
        window = atr_arr[i - w : i]
        if not np.isfinite(cur) or not np.all(np.isfinite(window)):
            out[i] = None
            continue
        mean_w = float(np.mean(window))
        if mean_w < 1e-9:
            out[i] = None
        else:
            out[i] = float(cur / mean_w)
    return out


def _batch_bb_width_change(
    bb_series: Optional[List[Optional[float]]], n: int, w: int
) -> List[Optional[float]]:
    """bb_width[i]/bb_width[i-w]，缺失时返回 None。"""
    if bb_series is None:
        return [None] * n
    out: List[Optional[float]] = [None] * n
    for i in range(w, n):
        cur = bb_series[i]
        ref = bb_series[i - w]
        if cur is None or ref is None:
            out[i] = None
        elif abs(ref) < 1e-9:
            out[i] = None
        else:
            out[i] = float(cur / ref)
    return out


def _rolling_mean(arr: np.ndarray, n: int, w: int) -> List[Optional[float]]:
    """对 arr 计算过去 w bar 的滚动均值（含当前 bar）。窗口不足时返回 None。"""
    out: List[Optional[float]] = [None] * n
    for i in range(w - 1, n):
        window = arr[i - w + 1 : i + 1]
        out[i] = float(np.mean(window))
    return out


def _batch_hh_count(highs: np.ndarray, n: int, w: int) -> List[Optional[float]]:
    """过去 w bar 中 high[i]>high[i-1] 的次数（含当前 bar）。窗口不足时返回 None。

    注意："过去 w bar" 的每个 bar[j] 与 bar[j-1] 做比较，
    需要 i-w+1 >= 1，即 i >= w。
    """
    out: List[Optional[float]] = [None] * n
    for i in range(w, n):
        count = 0
        for j in range(i - w + 1, i + 1):
            if highs[j] > highs[j - 1]:
                count += 1
        out[i] = float(count)
    return out


def _batch_ll_count(lows: np.ndarray, n: int, w: int) -> List[Optional[float]]:
    """过去 w bar 中 low[i]<low[i-1] 的次数（含当前 bar）。窗口不足时返回 None。"""
    out: List[Optional[float]] = [None] * n
    for i in range(w, n):
        count = 0
        for j in range(i - w + 1, i + 1):
            if lows[j] < lows[j - 1]:
                count += 1
        out[i] = float(count)
    return out


def _batch_gap_ratio(
    opens: np.ndarray,
    closes: np.ndarray,
    atr_arr: Optional[np.ndarray],
    n: int,
) -> List[Optional[float]]:
    """(open[i]-close[i-1])/ATR[i]，ATR 缺失或无前值时返回 None。"""
    if atr_arr is None:
        return [None] * n
    out: List[Optional[float]] = [None] * n
    for i in range(1, n):
        atr_val = atr_arr[i]
        if not np.isfinite(atr_val) or atr_val < 1e-9:
            out[i] = None
        else:
            out[i] = float((opens[i] - closes[i - 1]) / atr_val)
    return out


def _batch_range_position(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    n: int,
    w: int,
) -> List[Optional[float]]:
    """(close-min(low,w))/(max(high,w)-min(low,w))。

    窗口 w 取 [i-w+1, i] 的 high/low 极值；窗口不足时返回 None。
    """
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


def _batch_volume_surge(
    volumes: np.ndarray, n: int, w: int
) -> List[Optional[float]]:
    """volume[i]/mean(volume[i-w:i])，窗口不足时返回 None。

    mean 取 [i-w, i-1]（不含当前 bar），避免自参考。
    """
    out: List[Optional[float]] = [None] * n
    for i in range(w, n):
        window = volumes[i - w : i]
        mean_w = float(np.mean(window))
        if mean_w < 1e-9:
            out[i] = None
        else:
            out[i] = float(volumes[i] / mean_w)
    return out
