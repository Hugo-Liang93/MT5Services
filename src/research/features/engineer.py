"""Feature Engineering 框架 — 从原始指标派生跨指标/时序/结构特征。

可插拔架构：每个派生特征是一个 FeatureDefinition，注册到 FeatureEngineer。
DataMatrix 是 frozen，enrich() 通过 dataclasses.replace() 创建新实例。

所有内置特征均为无量纲，兼容 rule_mining 的 _DIMENSIONLESS_FIELDS。
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.data_matrix import DataMatrix

# 特征计算函数类型（两种，二选一，由 FeatureDefinition 决定走哪条）：
#   - FeatureFunc: (matrix, bar_index) -> Optional[float]  —— 逐 bar 解释循环
#   - BatchFeatureFunc: (matrix) -> List[Optional[float]]  —— 向量化批量计算
# FeatureEngineer.enrich() 优先使用 batch_func（若提供），显著降低特征计算耗时。
FeatureFunc = Callable[[DataMatrix, int], Optional[float]]
BatchFeatureFunc = Callable[[DataMatrix], List[Optional[float]]]


@dataclass(frozen=True)
class FeatureDefinition:
    """派生特征定义。

    Attributes:
        func: 必填，逐 bar 计算函数（用于运行时单 bar 计算或未提供 batch 时的回退）
        batch_func: 可选，向量化批量计算（同一特征的等价实现，输出语义必须与 func 一致）
    """

    name: str  # e.g. "di_spread"
    group: str  # indicator_series 中的 key group, e.g. "derived"
    func: FeatureFunc
    dependencies: Tuple[Tuple[str, str], ...]  # 需要的 indicator_series key
    batch_func: Optional[BatchFeatureFunc] = None
    is_dimensionless: bool = True
    formula_summary: str = ""
    source_inputs: Tuple[str, ...] = ()
    runtime_state_inputs: Tuple[str, ...] = ()
    live_computable: bool = True
    compute_scope: str = "bar_close"
    bounded_lookback: bool = True
    strategy_roles: Tuple[str, ...] = ()
    promotion_target_default: str = "research_only"
    no_lookahead: bool = True
    interpretable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "dependencies": [f"{ind}.{field}" for ind, field in self.dependencies],
            "is_dimensionless": self.is_dimensionless,
            "formula_summary": self.formula_summary,
            "source_inputs": list(self.source_inputs),
            "runtime_state_inputs": list(self.runtime_state_inputs),
            "live_computable": self.live_computable,
            "compute_scope": self.compute_scope,
            "bounded_lookback": self.bounded_lookback,
            "strategy_roles": list(self.strategy_roles),
            "promotion_target_default": self.promotion_target_default,
            "no_lookahead": self.no_lookahead,
            "interpretable": self.interpretable,
        }


class FeatureEngineer:
    """派生特征注册和批量计算。"""

    def __init__(self) -> None:
        self._features: Dict[str, FeatureDefinition] = {}

    def register(self, defn: FeatureDefinition) -> None:
        self._features[defn.name] = defn

    def available_features(self) -> List[str]:
        return list(self._features.keys())

    def definition(self, name: str) -> Optional[FeatureDefinition]:
        return self._features.get(name)

    def definitions(self) -> Dict[str, FeatureDefinition]:
        return dict(self._features)

    def inventory(self) -> Dict[str, Any]:
        return {
            "active_features": {
                name: defn.to_dict() for name, defn in sorted(self._features.items())
            },
            "promoted_indicator_precedents": list(PROMOTED_INDICATOR_PRECEDENTS),
        }

    def enrich(
        self,
        matrix: DataMatrix,
        *,
        feature_names: Optional[List[str]] = None,
    ) -> DataMatrix:
        """计算派生特征并返回增强的 DataMatrix。

        Args:
            matrix: 原始 DataMatrix（不修改）
            feature_names: 要计算的特征列表（None = 全部已注册）

        Returns:
            包含额外 indicator_series 条目的新 DataMatrix
        """
        targets = feature_names or list(self._features.keys())
        new_series = dict(matrix.indicator_series)

        for fname in targets:
            defn = self._features.get(fname)
            if defn is None:
                continue
            # 检查依赖是否存在
            if not all(dep in matrix.indicator_series for dep in defn.dependencies):
                continue
            # Intrabar 特征需要 child_bars 数据，缺失时跳过（避免全 None 无效计算）
            if defn.group == "derived_intrabar" and not matrix.child_bars:
                continue
            # 优先使用向量化批量实现；否则退化到逐 bar 解释循环
            if defn.batch_func is not None:
                series = defn.batch_func(matrix)
                if len(series) != matrix.n_bars:
                    raise ValueError(
                        f"batch_func for '{defn.name}' returned {len(series)} "
                        f"values, expected {matrix.n_bars}"
                    )
            else:
                series = []
                for i in range(matrix.n_bars):
                    try:
                        val = defn.func(matrix, i)
                    except Exception:
                        val = None
                    series.append(val)
            new_series[(defn.group, defn.name)] = series

        return dataclasses.replace(matrix, indicator_series=new_series)


# ── 内置派生特征 ──────────────────────────────────────────────


def _get(matrix: DataMatrix, ind: str, field: str, i: int) -> Optional[float]:
    """安全获取 indicator_series 值。"""
    series = matrix.indicator_series.get((ind, field))
    if series is None or i >= len(series):
        return None
    return series[i]


def _momentum_consensus(matrix: DataMatrix, i: int) -> Optional[float]:
    """动量一致性：(sign(MACD hist) + sign(RSI-50) + sign(StochK-50)) / 3。值域 [-1, 1]。"""
    hist = _get(matrix, "macd", "hist", i)
    rsi = _get(matrix, "rsi14", "rsi", i)
    stoch = _get(matrix, "stoch_rsi14", "stoch_rsi_k", i)
    if hist is None or rsi is None or stoch is None:
        return None
    s = (
        (1.0 if hist > 0 else -1.0 if hist < 0 else 0.0)
        + (1.0 if rsi > 50 else -1.0 if rsi < 50 else 0.0)
        + (1.0 if stoch > 50 else -1.0 if stoch < 50 else 0.0)
    ) / 3.0
    return s


def _regime_entropy(matrix: DataMatrix, i: int) -> Optional[float]:
    """Regime 概率熵：-Σ(p·ln(p))。值越高 = regime 越不确定。"""
    soft = matrix.soft_regimes[i]
    if soft is None:
        return None
    probs = [p for p in soft.values() if p > 0]
    if not probs:
        return None
    return -sum(p * math.log(p) for p in probs)


def _close_in_range(matrix: DataMatrix, i: int) -> Optional[float]:
    """收盘在 bar 区间的位置（OHLC order-flow 代理）。

    1.0 = 强势收盘（买方主导）；0.0 = 弱势收盘（卖方主导）；0.5 = 中性。
    XAUUSD 日内交易的关键信号之一——连续多根高位收盘常预示延续。
    """
    h = matrix.highs[i]
    l = matrix.lows[i]
    c = matrix.closes[i]
    rng = h - l
    if rng < 1e-9:
        return 0.5
    return (c - l) / rng


def _body_ratio(matrix: DataMatrix, i: int) -> Optional[float]:
    """实体/区间比：|close-open|/(high-low)。0=十字星（犹豫）；1=全实体（方向坚决）。"""
    h = matrix.highs[i]
    l = matrix.lows[i]
    o = matrix.opens[i]
    c = matrix.closes[i]
    rng = h - l
    if rng < 1e-9:
        return 0.0
    return abs(c - o) / rng


def _upper_wick_ratio(matrix: DataMatrix, i: int) -> Optional[float]:
    """上影线占比：卖压代理。高上影 + 高收盘位 = 上方供给密集。"""
    h = matrix.highs[i]
    l = matrix.lows[i]
    o = matrix.opens[i]
    c = matrix.closes[i]
    rng = h - l
    if rng < 1e-9:
        return 0.0
    return (h - max(o, c)) / rng


def _lower_wick_ratio(matrix: DataMatrix, i: int) -> Optional[float]:
    """下影线占比：买压代理。"""
    h = matrix.highs[i]
    l = matrix.lows[i]
    o = matrix.opens[i]
    c = matrix.closes[i]
    rng = h - l
    if rng < 1e-9:
        return 0.0
    return (min(o, c) - l) / rng


def _range_expansion(matrix: DataMatrix, i: int) -> Optional[float]:
    """Range/ATR14 比值：当前 bar 真实范围相对平均的倍数。>1.5 = 扩张，<0.5 = 压缩。"""
    atr = _get(matrix, "atr14", "atr", i)
    if atr is None or atr < 1e-9:
        return None
    rng = matrix.highs[i] - matrix.lows[i]
    return rng / atr


def _oc_imbalance(matrix: DataMatrix, i: int) -> Optional[float]:
    """(close-open)/(high-low)：方向强度的区间归一化。∈ [-1, 1]。"""
    h = matrix.highs[i]
    l = matrix.lows[i]
    o = matrix.opens[i]
    c = matrix.closes[i]
    rng = h - l
    if rng < 1e-9:
        return 0.0
    return (c - o) / rng


def _session_phase(matrix: DataMatrix, i: int) -> Optional[float]:
    """UTC 时段编码：0=亚盘 / 1=欧盘 / 2=美盘 / 3=尾盘。供 × 指标交互使用。"""
    t = matrix.bar_times[i]
    h = t.hour
    if 0 <= h < 7:
        return 0.0
    if 7 <= h < 12:
        return 1.0
    if 12 <= h < 21:
        return 2.0
    return 3.0


def _london_session(matrix: DataMatrix, i: int) -> Optional[float]:
    """London 时段 0/1 flag（欧盘 07-16 UTC）。"""
    h = matrix.bar_times[i].hour
    return 1.0 if 7 <= h < 16 else 0.0


def _ny_session(matrix: DataMatrix, i: int) -> Optional[float]:
    """NY 时段 0/1 flag（美盘 12-21 UTC）。"""
    h = matrix.bar_times[i].hour
    return 1.0 if 12 <= h < 21 else 0.0


def _day_progress(matrix: DataMatrix, i: int) -> Optional[float]:
    """UTC 日内进度 [0, 1]。用于捕捉日内时间衰减/累积效应。"""
    t = matrix.bar_times[i]
    return (t.hour * 3600 + t.minute * 60 + t.second) / 86400.0


def _close_to_close_3(matrix: DataMatrix, i: int) -> Optional[float]:
    """3 bar 涨跌百分比：(close[i] - close[i-3]) / close[i-3]。短期动量。"""
    if i < 3:
        return None
    prev = matrix.closes[i - 3]
    if prev < 1e-9:
        return None
    return (matrix.closes[i] - prev) / prev


def _bars_to_next_high_impact_event(matrix: DataMatrix, i: int) -> Optional[float]:
    """距离下一个高影响事件的 bar 数（timeframe 粒度粗略换算）。

    找不到下一个事件 → None；找到则返回"到事件的分钟数 / 每 bar 分钟数"。
    XAUUSD 在非农/议息前 30 分钟波动率结构性上升——此特征可捕获事件前衰减。
    """
    events = matrix.high_impact_event_times
    if not events:
        return None
    bar_time = matrix.bar_times[i]
    tf_minutes = _tf_minutes(matrix.timeframe)
    if tf_minutes <= 0:
        return None
    for ev in events:
        if ev > bar_time:
            delta_minutes = (ev - bar_time).total_seconds() / 60.0
            return delta_minutes / tf_minutes
    return None


def _bars_since_last_high_impact_event(matrix: DataMatrix, i: int) -> Optional[float]:
    """距离上一个高影响事件的 bar 数。"""
    events = matrix.high_impact_event_times
    if not events:
        return None
    bar_time = matrix.bar_times[i]
    tf_minutes = _tf_minutes(matrix.timeframe)
    if tf_minutes <= 0:
        return None
    last: Optional[Any] = None
    for ev in events:
        if ev <= bar_time:
            last = ev
        else:
            break
    if last is None:
        return None
    delta_minutes = (bar_time - last).total_seconds() / 60.0
    return delta_minutes / tf_minutes


def _in_news_window(matrix: DataMatrix, i: int) -> Optional[float]:
    """当前 bar 是否在 ±30 分钟新闻窗口内：1.0=在窗口，0.0=不在。

    硬编码 ±30 min——XAUUSD 上该窗口内 spread 扩张和滑点风险最高。
    """
    events = matrix.high_impact_event_times
    if not events:
        return 0.0
    bar_time = matrix.bar_times[i]
    for ev in events:
        delta_sec = abs((ev - bar_time).total_seconds())
        if delta_sec <= 30 * 60:
            return 1.0
        if ev > bar_time and (ev - bar_time).total_seconds() > 30 * 60:
            break
    return 0.0


def _tf_minutes(tf: str) -> int:
    """时间框架字符串 → 分钟数（简化版）。"""
    tf = (tf or "").strip().upper()
    table = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
    return table.get(tf, 0)


def _consecutive_same_color(matrix: DataMatrix, i: int) -> Optional[float]:
    """连续同色 bar 数（带符号）：+N = N 根连阳，-N = N 根连阴。"""
    o = matrix.opens[i]
    c = matrix.closes[i]
    if abs(c - o) < 1e-9:
        return 0.0
    cur_up = c > o
    count = 1
    for j in range(i - 1, max(-1, i - 10), -1):
        oj = matrix.opens[j]
        cj = matrix.closes[j]
        if abs(cj - oj) < 1e-9:
            break
        if (cj > oj) == cur_up:
            count += 1
        else:
            break
    return float(count if cur_up else -count)


# ── 向量化 batch 实现（NumPy）：与上面 per-bar 函数行为一致，仅性能优化 ────


def _series_to_float_array(values: Optional[List[Optional[float]]], n: int):
    """将可能含 None 的 indicator_series 转为 ndarray（None → nan）。

    series 为 None 时返回全 nan 数组（长度 n）。
    """
    import numpy as np

    if values is None:
        return np.full(n, np.nan, dtype=np.float64)
    return np.asarray(
        [v if v is not None else float("nan") for v in values],
        dtype=np.float64,
    )


def _batch_momentum_consensus(matrix: DataMatrix) -> List[Optional[float]]:
    """(sign(macd.hist) + sign(rsi-50) + sign(stoch-50)) / 3，任一缺失 → None。"""
    import numpy as np

    n = matrix.n_bars
    hist = _series_to_float_array(matrix.indicator_series.get(("macd", "hist")), n)
    rsi = _series_to_float_array(matrix.indicator_series.get(("rsi14", "rsi")), n)
    stoch = _series_to_float_array(
        matrix.indicator_series.get(("stoch_rsi14", "stoch_rsi_k")),
        n,
    )
    valid = np.isfinite(hist) & np.isfinite(rsi) & np.isfinite(stoch)
    score = (np.sign(hist) + np.sign(rsi - 50.0) + np.sign(stoch - 50.0)) / 3.0
    out: List[Optional[float]] = []
    for v, ok in zip(score, valid):
        out.append(float(v) if ok else None)
    return out


def _batch_close_in_range(matrix: DataMatrix) -> List[Optional[float]]:
    import numpy as np

    highs = np.asarray(matrix.highs, dtype=np.float64)
    lows = np.asarray(matrix.lows, dtype=np.float64)
    closes = np.asarray(matrix.closes, dtype=np.float64)
    rng = highs - lows
    out = np.where(rng < 1e-9, 0.5, (closes - lows) / np.where(rng < 1e-9, 1.0, rng))
    return out.tolist()


def _batch_body_ratio(matrix: DataMatrix) -> List[Optional[float]]:
    import numpy as np

    highs = np.asarray(matrix.highs, dtype=np.float64)
    lows = np.asarray(matrix.lows, dtype=np.float64)
    opens = np.asarray(matrix.opens, dtype=np.float64)
    closes = np.asarray(matrix.closes, dtype=np.float64)
    rng = highs - lows
    out = np.where(
        rng < 1e-9, 0.0, np.abs(closes - opens) / np.where(rng < 1e-9, 1.0, rng)
    )
    return out.tolist()


def _batch_upper_wick_ratio(matrix: DataMatrix) -> List[Optional[float]]:
    import numpy as np

    highs = np.asarray(matrix.highs, dtype=np.float64)
    lows = np.asarray(matrix.lows, dtype=np.float64)
    opens = np.asarray(matrix.opens, dtype=np.float64)
    closes = np.asarray(matrix.closes, dtype=np.float64)
    rng = highs - lows
    body_top = np.maximum(opens, closes)
    out = np.where(rng < 1e-9, 0.0, (highs - body_top) / np.where(rng < 1e-9, 1.0, rng))
    return out.tolist()


def _batch_lower_wick_ratio(matrix: DataMatrix) -> List[Optional[float]]:
    import numpy as np

    highs = np.asarray(matrix.highs, dtype=np.float64)
    lows = np.asarray(matrix.lows, dtype=np.float64)
    opens = np.asarray(matrix.opens, dtype=np.float64)
    closes = np.asarray(matrix.closes, dtype=np.float64)
    rng = highs - lows
    body_bot = np.minimum(opens, closes)
    out = np.where(rng < 1e-9, 0.0, (body_bot - lows) / np.where(rng < 1e-9, 1.0, rng))
    return out.tolist()


def _batch_oc_imbalance(matrix: DataMatrix) -> List[Optional[float]]:
    import numpy as np

    highs = np.asarray(matrix.highs, dtype=np.float64)
    lows = np.asarray(matrix.lows, dtype=np.float64)
    opens = np.asarray(matrix.opens, dtype=np.float64)
    closes = np.asarray(matrix.closes, dtype=np.float64)
    rng = highs - lows
    out = np.where(rng < 1e-9, 0.0, (closes - opens) / np.where(rng < 1e-9, 1.0, rng))
    return out.tolist()


def _batch_range_expansion(matrix: DataMatrix) -> List[Optional[float]]:
    """range / atr14。atr 缺失或 < 1e-9 的 bar 输出 None。"""
    import numpy as np

    highs = np.asarray(matrix.highs, dtype=np.float64)
    lows = np.asarray(matrix.lows, dtype=np.float64)
    atr_series = matrix.indicator_series.get(("atr14", "atr"))
    if atr_series is None:
        return [None] * matrix.n_bars
    # atr 可能含 None，先转为 ndarray（None → nan），逐项判断
    atr_arr = np.asarray(
        [v if v is not None else float("nan") for v in atr_series],
        dtype=np.float64,
    )
    rng = highs - lows
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = rng / atr_arr
    out: List[Optional[float]] = []
    for v, a in zip(ratio, atr_arr):
        if not np.isfinite(v) or a < 1e-9:
            out.append(None)
        else:
            out.append(float(v))
    return out


def _batch_close_to_close_3(matrix: DataMatrix) -> List[Optional[float]]:
    import numpy as np

    closes = np.asarray(matrix.closes, dtype=np.float64)
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n <= 3:
        return out
    prev = closes[:-3]
    cur = closes[3:]
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(prev < 1e-9, np.nan, (cur - prev) / prev)
    for i, v in enumerate(ratio, start=3):
        out[i] = None if not np.isfinite(v) else float(v)
    return out


def _batch_regime_entropy(matrix: DataMatrix) -> List[Optional[float]]:
    """-Σ(p·ln(p))：NumPy inner product 替代 Python math.log 逐元素。

    soft_regimes 是每 bar 独立字典（结构不统一），仍需外层 Python 循环，
    但内部熵计算向量化后对常见 3-5 档 regime 已近乎常数级开销。
    """
    import numpy as np

    out: List[Optional[float]] = []
    for sr in matrix.soft_regimes:
        if sr is None:
            out.append(None)
            continue
        probs = [p for p in sr.values() if p > 0]
        if not probs:
            out.append(None)
            continue
        arr = np.asarray(probs, dtype=np.float64)
        out.append(float(-(arr * np.log(arr)).sum()))
    return out


def _batch_bars_in_regime(matrix: DataMatrix) -> List[Optional[float]]:
    """每个 bar 的当前 regime 连续持续 bar 数。向量化：用 np.cumsum 配合 reset 点。"""
    import numpy as np

    n = matrix.n_bars
    regimes = matrix.regimes
    if n == 0:
        return []
    counts = np.ones(n, dtype=np.float64)
    for j in range(1, n):
        if regimes[j] == regimes[j - 1]:
            counts[j] = counts[j - 1] + 1.0
        else:
            counts[j] = 1.0
    return counts.tolist()


def _batch_session_phase(matrix: DataMatrix) -> List[Optional[float]]:
    """UTC 时段编码：0=亚盘 / 1=欧盘 / 2=美盘 / 3=尾盘（与 per-bar 版一致）。"""
    import numpy as np

    hours = np.asarray([t.hour for t in matrix.bar_times], dtype=np.int64)
    out = np.where(
        hours < 7,
        0.0,
        np.where(hours < 12, 1.0, np.where(hours < 21, 2.0, 3.0)),
    )
    return out.tolist()


def _batch_london_session(matrix: DataMatrix) -> List[Optional[float]]:
    import numpy as np

    hours = np.asarray([t.hour for t in matrix.bar_times], dtype=np.int64)
    out = ((hours >= 7) & (hours < 16)).astype(np.float64)
    return out.tolist()


def _batch_ny_session(matrix: DataMatrix) -> List[Optional[float]]:
    import numpy as np

    hours = np.asarray([t.hour for t in matrix.bar_times], dtype=np.int64)
    out = ((hours >= 12) & (hours < 21)).astype(np.float64)
    return out.tolist()


def _batch_day_progress(matrix: DataMatrix) -> List[Optional[float]]:
    """UTC 日内进度 [0, 1]：(h*3600+m*60+s)/86400。"""
    import numpy as np

    secs = np.asarray(
        [t.hour * 3600 + t.minute * 60 + t.second for t in matrix.bar_times],
        dtype=np.float64,
    )
    return (secs / 86400.0).tolist()


def _batch_consecutive_same_color(matrix: DataMatrix) -> List[Optional[float]]:
    """连续同色 bar 数（带符号），回看上限 10。向量化：基于 sign 数组的累积计数。

    说明：per-bar 版上限 10 内回看，向量化用递推（O(n)），等价输出。
    """
    import numpy as np

    opens = np.asarray(matrix.opens, dtype=np.float64)
    closes = np.asarray(matrix.closes, dtype=np.float64)
    n = len(opens)
    out = np.zeros(n, dtype=np.float64)
    signs = np.where(closes > opens, 1, np.where(closes < opens, -1, 0))

    # 无符号（doji）单独处理：返回 0
    # 同号累计：run_len 向量化递推
    for i in range(n):
        if signs[i] == 0:
            out[i] = 0.0
            continue
        run = 1
        # 回看最多 10 根或直到方向变
        for j in range(i - 1, max(-1, i - 10), -1):
            if signs[j] == 0:
                break
            if signs[j] == signs[i]:
                run += 1
            else:
                break
        out[i] = float(run * signs[i])
    return out.tolist()


def _batch_bars_to_next_high_impact_event(matrix: DataMatrix) -> List[Optional[float]]:
    """每个 bar 到下一个高影响事件的 tf-bar 数。

    向量化：对已排序 event_times 用 np.searchsorted 找"右边第一个 > bar_time"。
    """
    import numpy as np

    events = matrix.high_impact_event_times
    if not events:
        return [None] * matrix.n_bars
    tf_minutes = _tf_minutes(matrix.timeframe)
    if tf_minutes <= 0:
        return [None] * matrix.n_bars

    bar_ts = np.asarray([t.timestamp() for t in matrix.bar_times], dtype=np.float64)
    ev_ts = np.asarray([e.timestamp() for e in events], dtype=np.float64)
    # 对每个 bar_ts 找第一个 > bar_ts 的事件位置（side='right'）
    idx = np.searchsorted(ev_ts, bar_ts, side="right")
    out: List[Optional[float]] = []
    tf_seconds = tf_minutes * 60.0
    n_events = len(ev_ts)
    for i, pos in enumerate(idx):
        if pos >= n_events:
            out.append(None)
        else:
            delta = (ev_ts[pos] - bar_ts[i]) / tf_seconds
            out.append(float(delta))
    return out


def _batch_bars_since_last_high_impact_event(
    matrix: DataMatrix,
) -> List[Optional[float]]:
    """每个 bar 距上一个高影响事件的 tf-bar 数。"""
    import numpy as np

    events = matrix.high_impact_event_times
    if not events:
        return [None] * matrix.n_bars
    tf_minutes = _tf_minutes(matrix.timeframe)
    if tf_minutes <= 0:
        return [None] * matrix.n_bars

    bar_ts = np.asarray([t.timestamp() for t in matrix.bar_times], dtype=np.float64)
    ev_ts = np.asarray([e.timestamp() for e in events], dtype=np.float64)
    # side='right' → 第一个 > bar_ts 的位置；减 1 即最近的 <= bar_ts
    idx = np.searchsorted(ev_ts, bar_ts, side="right") - 1
    out: List[Optional[float]] = []
    tf_seconds = tf_minutes * 60.0
    for i, pos in enumerate(idx):
        if pos < 0:
            out.append(None)
        else:
            delta = (bar_ts[i] - ev_ts[pos]) / tf_seconds
            out.append(float(delta))
    return out


def _batch_in_news_window(matrix: DataMatrix) -> List[Optional[float]]:
    """|bar_time - 最近事件| ≤ 30 分钟 → 1.0，否则 0.0。"""
    import numpy as np

    events = matrix.high_impact_event_times
    n = matrix.n_bars
    if not events:
        return [0.0] * n

    bar_ts = np.asarray([t.timestamp() for t in matrix.bar_times], dtype=np.float64)
    ev_ts = np.asarray([e.timestamp() for e in events], dtype=np.float64)
    # 左右各找一个事件，取绝对距离较小者
    right_idx = np.searchsorted(ev_ts, bar_ts, side="right")
    left_idx = right_idx - 1

    out = np.zeros(n, dtype=np.float64)
    threshold = 30 * 60.0
    n_events = len(ev_ts)
    for i in range(n):
        best = float("inf")
        if left_idx[i] >= 0:
            best = min(best, abs(bar_ts[i] - ev_ts[left_idx[i]]))
        if right_idx[i] < n_events:
            best = min(best, abs(ev_ts[right_idx[i]] - bar_ts[i]))
        if best <= threshold:
            out[i] = 1.0
    return out.tolist()


def _bars_in_regime(matrix: DataMatrix, i: int) -> Optional[float]:
    """当前 regime 持续 bar 数（O(1) per call，利用上一个 bar 的结果）。

    注意：FeatureEngineer 按 i=0,1,2... 顺序调用，所以可以用全局缓存。
    """
    # 使用函数属性缓存上次结果
    cache = getattr(_bars_in_regime, "_cache", None)
    if cache is None or cache.get("_matrix_id") != id(matrix):
        # 首次调用或矩阵变了：O(n) 预计算全部
        n = matrix.n_bars
        counts = [1.0] * n
        for j in range(1, n):
            if matrix.regimes[j] == matrix.regimes[j - 1]:
                counts[j] = counts[j - 1] + 1.0
        _bars_in_regime._cache = {"_matrix_id": id(matrix), "counts": counts}  # type: ignore[attr-defined]
        cache = _bars_in_regime._cache  # type: ignore[attr-defined]
    return cache["counts"][i] if i < len(cache["counts"]) else None


# ── 特征注册表 ────────────────────────────────────────────────


# 注意：di_spread, squeeze_score, vwap_gap_atr, rsi_accel 已提升为正式指标
# （src/indicators/core/composite.py），不再作为研究特征。
# 其余条目保留在 research feature registry 中，用于继续做候选发现与晋升审计。


# ── Intrabar 派生特征函数 ──────────────────────────────────────────────
# 需要 DataMatrix.child_bars 数据（build_data_matrix(child_tf=...) 加载）。
# 未加载子 TF 数据时返回 None（安全降级）。


def _child_bar_consensus(matrix: DataMatrix, i: int) -> Optional[float]:
    """子 TF 同色比例：子 bars 中与父 bar 同色（涨/跌）的占比。"""
    children = matrix.child_bars.get(i)
    if not children or len(children) < 2:
        return None
    parent_bullish = matrix.closes[i] > matrix.opens[i]
    same = sum(1 for b in children if (b.close > b.open) == parent_bullish)
    return same / len(children)


def _batch_child_bar_consensus(matrix: DataMatrix) -> List[Optional[float]]:
    """子 TF 同色比例（batch）。"""
    out: List[Optional[float]] = []
    for i in range(matrix.n_bars):
        children = matrix.child_bars.get(i)
        if not children or len(children) < 2:
            out.append(None)
            continue
        parent_bullish = matrix.closes[i] > matrix.opens[i]
        same = sum(1 for b in children if (b.close > b.open) == parent_bullish)
        out.append(same / len(children))
    return out


def _child_range_acceleration(matrix: DataMatrix, i: int) -> Optional[float]:
    """前半段 vs 后半段 range 扩张比：second_half_avg_range / first_half_avg_range - 1。"""
    children = matrix.child_bars.get(i)
    if not children or len(children) < 4:
        return None
    mid = len(children) // 2
    first_half = children[:mid]
    second_half = children[mid:]
    avg_first = sum(b.high - b.low for b in first_half) / len(first_half)
    avg_second = sum(b.high - b.low for b in second_half) / len(second_half)
    if avg_first < 1e-9:
        return None
    return avg_second / avg_first - 1.0


def _batch_child_range_acceleration(matrix: DataMatrix) -> List[Optional[float]]:
    """前半段 vs 后半段 range 扩张比（batch）。"""
    out: List[Optional[float]] = []
    for i in range(matrix.n_bars):
        children = matrix.child_bars.get(i)
        if not children or len(children) < 4:
            out.append(None)
            continue
        mid = len(children) // 2
        first_half = children[:mid]
        second_half = children[mid:]
        avg_first = sum(b.high - b.low for b in first_half) / len(first_half)
        avg_second = sum(b.high - b.low for b in second_half) / len(second_half)
        if avg_first < 1e-9:
            out.append(None)
        else:
            out.append(avg_second / avg_first - 1.0)
    return out


def _intrabar_momentum_shift(matrix: DataMatrix, i: int) -> Optional[float]:
    """子 TF close-to-close 方向切换比：sign_changes / (n_children - 1)。"""
    children = matrix.child_bars.get(i)
    if not children or len(children) < 3:
        return None
    changes = 0
    prev_dir = 0
    for j in range(1, len(children)):
        diff = children[j].close - children[j - 1].close
        cur_dir = 1 if diff > 0 else (-1 if diff < 0 else 0)
        if cur_dir != 0 and prev_dir != 0 and cur_dir != prev_dir:
            changes += 1
        if cur_dir != 0:
            prev_dir = cur_dir
    return changes / (len(children) - 1)


def _batch_intrabar_momentum_shift(matrix: DataMatrix) -> List[Optional[float]]:
    """子 TF 动量方向切换比（batch）。"""
    out: List[Optional[float]] = []
    for i in range(matrix.n_bars):
        out.append(_intrabar_momentum_shift(matrix, i))
    return out


def _child_volume_front_weight(matrix: DataMatrix, i: int) -> Optional[float]:
    """前半 volume / 总 volume。"""
    children = matrix.child_bars.get(i)
    if not children or len(children) < 4:
        return None
    mid = len(children) // 2
    total = sum(b.volume for b in children)
    if total < 1e-9:
        return None
    front = sum(b.volume for b in children[:mid])
    return front / total


def _batch_child_volume_front_weight(matrix: DataMatrix) -> List[Optional[float]]:
    """前半 volume / 总 volume（batch）。"""
    out: List[Optional[float]] = []
    for i in range(matrix.n_bars):
        out.append(_child_volume_front_weight(matrix, i))
    return out


def _child_bar_count_ratio(matrix: DataMatrix, i: int) -> Optional[float]:
    """实际子 bar 数 / 期望子 bar 数（gap 检测）。缺少期望值时返回 None。"""
    children = matrix.child_bars.get(i)
    if children is None:
        return None
    if not matrix.child_tf:
        return None
    from src.utils.common import timeframe_seconds

    parent_secs = timeframe_seconds(matrix.timeframe)
    child_secs = timeframe_seconds(matrix.child_tf)
    if child_secs <= 0 or parent_secs <= 0:
        return None
    expected = parent_secs // child_secs
    if expected <= 0:
        return None
    return len(children) / expected


def _batch_child_bar_count_ratio(matrix: DataMatrix) -> List[Optional[float]]:
    """子 bar 数量比（batch）。"""
    if not matrix.child_tf:
        return [None] * matrix.n_bars
    from src.utils.common import timeframe_seconds

    parent_secs = timeframe_seconds(matrix.timeframe)
    child_secs = timeframe_seconds(matrix.child_tf)
    if child_secs <= 0 or parent_secs <= 0:
        return [None] * matrix.n_bars
    expected = parent_secs // child_secs
    if expected <= 0:
        return [None] * matrix.n_bars

    out: List[Optional[float]] = []
    for i in range(matrix.n_bars):
        children = matrix.child_bars.get(i)
        if children is None:
            out.append(None)
        else:
            out.append(len(children) / expected)
    return out


_BUILTIN_FEATURES: List[FeatureDefinition] = [
    FeatureDefinition(
        name="momentum_consensus",
        group="derived",
        func=_momentum_consensus,
        batch_func=_batch_momentum_consensus,
        dependencies=(
            ("macd", "hist"),
            ("rsi14", "rsi"),
            ("stoch_rsi14", "stoch_rsi_k"),
        ),
        formula_summary=(
            "(sign(macd.hist) + sign(rsi14.rsi-50) + "
            "sign(stoch_rsi14.stoch_rsi_k-50)) / 3"
        ),
        source_inputs=("macd.hist", "rsi14.rsi", "stoch_rsi14.stoch_rsi_k"),
        runtime_state_inputs=(),
        live_computable=True,
        compute_scope="bar_close",
        bounded_lookback=True,
        strategy_roles=("why", "when"),
        promotion_target_default="indicator_and_strategy_candidate",
    ),
    FeatureDefinition(
        name="regime_entropy",
        group="derived",
        func=_regime_entropy,
        batch_func=_batch_regime_entropy,
        dependencies=(),  # 使用 soft_regimes，不是 indicator_series
        formula_summary="-Σ(p·ln(p)) from soft regime probabilities",
        source_inputs=(),
        runtime_state_inputs=("soft_regimes",),
        live_computable=False,
        compute_scope="runtime_state",
        bounded_lookback=True,
        strategy_roles=("why",),
        promotion_target_default="research_only",
    ),
    FeatureDefinition(
        name="bars_in_regime",
        group="derived",
        func=_bars_in_regime,
        batch_func=_batch_bars_in_regime,
        dependencies=(),  # 使用 regimes 列表
        formula_summary="count consecutive bars staying in the current hard regime",
        source_inputs=(),
        runtime_state_inputs=("regimes",),
        live_computable=False,
        compute_scope="runtime_state",
        bounded_lookback=True,
        strategy_roles=("when", "where"),
        promotion_target_default="strategy_helper",
    ),
    # ── Bar 内 order-flow 代理（XAUUSD 日内关键）─────────────────
    FeatureDefinition(
        name="close_in_range",
        group="derived_flow",
        func=_close_in_range,
        batch_func=_batch_close_in_range,
        dependencies=(),
        formula_summary="(close - low) / (high - low)",
        source_inputs=("bar.high", "bar.low", "bar.close"),
        compute_scope="bar_close",
        strategy_roles=("why", "when"),
    ),
    FeatureDefinition(
        name="body_ratio",
        group="derived_flow",
        func=_body_ratio,
        batch_func=_batch_body_ratio,
        dependencies=(),
        formula_summary="|close - open| / (high - low)",
        source_inputs=("bar.open", "bar.high", "bar.low", "bar.close"),
        compute_scope="bar_close",
        strategy_roles=("when",),
    ),
    FeatureDefinition(
        name="upper_wick_ratio",
        group="derived_flow",
        func=_upper_wick_ratio,
        batch_func=_batch_upper_wick_ratio,
        dependencies=(),
        formula_summary="(high - max(open,close)) / (high - low)",
        source_inputs=("bar.open", "bar.high", "bar.low", "bar.close"),
        compute_scope="bar_close",
        strategy_roles=("why", "where"),
    ),
    FeatureDefinition(
        name="lower_wick_ratio",
        group="derived_flow",
        func=_lower_wick_ratio,
        batch_func=_batch_lower_wick_ratio,
        dependencies=(),
        formula_summary="(min(open,close) - low) / (high - low)",
        source_inputs=("bar.open", "bar.high", "bar.low", "bar.close"),
        compute_scope="bar_close",
        strategy_roles=("why", "where"),
    ),
    FeatureDefinition(
        name="range_expansion",
        group="derived_flow",
        func=_range_expansion,
        batch_func=_batch_range_expansion,
        dependencies=(("atr14", "atr"),),
        formula_summary="(high - low) / atr14",
        source_inputs=("bar.high", "bar.low", "atr14.atr"),
        compute_scope="bar_close",
        strategy_roles=("when",),
    ),
    FeatureDefinition(
        name="oc_imbalance",
        group="derived_flow",
        func=_oc_imbalance,
        batch_func=_batch_oc_imbalance,
        dependencies=(),
        formula_summary="(close - open) / (high - low)",
        source_inputs=("bar.open", "bar.high", "bar.low", "bar.close"),
        compute_scope="bar_close",
        strategy_roles=("why",),
    ),
    # ── 时段编码（供 × 指标交互使用）─────────────────────────────
    FeatureDefinition(
        name="session_phase",
        group="derived_time",
        func=_session_phase,
        batch_func=_batch_session_phase,
        dependencies=(),
        formula_summary="0=Asia / 1=London / 2=NY / 3=Close",
        source_inputs=("bar.time",),
        compute_scope="bar_close",
        strategy_roles=("when",),
    ),
    FeatureDefinition(
        name="london_session",
        group="derived_time",
        func=_london_session,
        batch_func=_batch_london_session,
        dependencies=(),
        formula_summary="flag: bar_time.hour ∈ [7, 16) UTC",
        source_inputs=("bar.time",),
        compute_scope="bar_close",
        strategy_roles=("when",),
    ),
    FeatureDefinition(
        name="ny_session",
        group="derived_time",
        func=_ny_session,
        batch_func=_batch_ny_session,
        dependencies=(),
        formula_summary="flag: bar_time.hour ∈ [12, 21) UTC",
        source_inputs=("bar.time",),
        compute_scope="bar_close",
        strategy_roles=("when",),
    ),
    FeatureDefinition(
        name="day_progress",
        group="derived_time",
        func=_day_progress,
        batch_func=_batch_day_progress,
        dependencies=(),
        formula_summary="UTC intraday progress ∈ [0, 1]",
        source_inputs=("bar.time",),
        compute_scope="bar_close",
        strategy_roles=("when",),
    ),
    # ── 经济事件距离（XAUUSD 对非农/议息极度敏感）────────────────
    FeatureDefinition(
        name="bars_to_next_high_impact_event",
        group="derived_event",
        func=_bars_to_next_high_impact_event,
        batch_func=_batch_bars_to_next_high_impact_event,
        dependencies=(),
        formula_summary="minutes to next high-impact event / tf_minutes",
        source_inputs=("bar.time",),
        runtime_state_inputs=("high_impact_event_times",),
        compute_scope="bar_close",
        strategy_roles=("when", "why"),
    ),
    FeatureDefinition(
        name="bars_since_last_high_impact_event",
        group="derived_event",
        func=_bars_since_last_high_impact_event,
        batch_func=_batch_bars_since_last_high_impact_event,
        dependencies=(),
        formula_summary="minutes since last high-impact event / tf_minutes",
        source_inputs=("bar.time",),
        runtime_state_inputs=("high_impact_event_times",),
        compute_scope="bar_close",
        strategy_roles=("when",),
    ),
    FeatureDefinition(
        name="in_news_window",
        group="derived_event",
        func=_in_news_window,
        batch_func=_batch_in_news_window,
        dependencies=(),
        formula_summary="flag: |bar_time - event| <= 30 minutes",
        source_inputs=("bar.time",),
        runtime_state_inputs=("high_impact_event_times",),
        compute_scope="bar_close",
        strategy_roles=("when",),
    ),
    # ── 多 bar 结构 ──────────────────────────────────────────────
    FeatureDefinition(
        name="close_to_close_3",
        group="derived_structure",
        func=_close_to_close_3,
        batch_func=_batch_close_to_close_3,
        dependencies=(),
        formula_summary="(close[i] - close[i-3]) / close[i-3]",
        source_inputs=("bar.close",),
        compute_scope="bar_close",
        strategy_roles=("why",),
    ),
    FeatureDefinition(
        name="consecutive_same_color",
        group="derived_structure",
        func=_consecutive_same_color,
        batch_func=_batch_consecutive_same_color,
        dependencies=(),
        formula_summary="signed count of consecutive same-color bars (capped at 10)",
        source_inputs=("bar.open", "bar.close"),
        compute_scope="bar_close",
        strategy_roles=("why",),
    ),
    # ── Intrabar 派生特征（需 child_bars 数据） ─────────────────────
    FeatureDefinition(
        name="child_bar_consensus",
        group="derived_intrabar",
        func=_child_bar_consensus,
        batch_func=_batch_child_bar_consensus,
        dependencies=(),
        formula_summary="proportion of child bars with same color as parent bar",
        source_inputs=("child_bars", "bar.open", "bar.close"),
        runtime_state_inputs=("child_bars",),
        compute_scope="bar_close",
        live_computable=False,
        strategy_roles=("why",),
    ),
    FeatureDefinition(
        name="child_range_acceleration",
        group="derived_intrabar",
        func=_child_range_acceleration,
        batch_func=_batch_child_range_acceleration,
        dependencies=(),
        formula_summary="second_half_avg_range / first_half_avg_range - 1",
        source_inputs=("child_bars",),
        runtime_state_inputs=("child_bars",),
        compute_scope="bar_close",
        live_computable=False,
        strategy_roles=("when",),
    ),
    FeatureDefinition(
        name="intrabar_momentum_shift",
        group="derived_intrabar",
        func=_intrabar_momentum_shift,
        batch_func=_batch_intrabar_momentum_shift,
        dependencies=(),
        formula_summary="sign changes in child bar close-to-close / total child bars",
        source_inputs=("child_bars",),
        runtime_state_inputs=("child_bars",),
        compute_scope="bar_close",
        live_computable=False,
        strategy_roles=("why",),
    ),
    FeatureDefinition(
        name="child_volume_front_weight",
        group="derived_intrabar",
        func=_child_volume_front_weight,
        batch_func=_batch_child_volume_front_weight,
        dependencies=(),
        formula_summary="first_half_volume / total_volume",
        source_inputs=("child_bars",),
        runtime_state_inputs=("child_bars",),
        compute_scope="bar_close",
        live_computable=False,
        strategy_roles=("when",),
    ),
    FeatureDefinition(
        name="child_bar_count_ratio",
        group="derived_intrabar",
        func=_child_bar_count_ratio,
        batch_func=_batch_child_bar_count_ratio,
        dependencies=(),
        formula_summary="actual_child_bars / expected_child_bars (gap detection)",
        source_inputs=("child_bars",),
        runtime_state_inputs=("child_bars",),
        compute_scope="bar_close",
        live_computable=False,
        strategy_roles=("where",),
    ),
]


PROMOTED_INDICATOR_PRECEDENTS: Tuple[Dict[str, Any], ...] = (
    {
        "feature_name": "di_spread",
        "promoted_indicator_name": "di_spread14",
        "status": "promoted_indicator",
        "note": "已晋升为共享趋势方向复合指标",
    },
    {
        "feature_name": "squeeze_score",
        "promoted_indicator_name": "squeeze20",
        "status": "promoted_indicator",
        "note": "已晋升为共享波动率挤压指标",
    },
    {
        "feature_name": "vwap_gap_atr",
        "promoted_indicator_name": "vwap_dev30",
        "status": "promoted_indicator",
        "note": "已晋升为共享均值偏离指标",
    },
    {
        "feature_name": "rsi_accel",
        "promoted_indicator_name": "momentum_accel14",
        "status": "promoted_indicator",
        "note": "已晋升为共享动量加速度指标",
    },
    {
        "feature_name": "momentum_consensus",
        "promoted_indicator_name": "momentum_consensus14",
        "status": "promoted_indicator",
        "note": "首轮 feature promotion 交付的新共享动量一致性指标",
    },
)


def build_default_engineer() -> FeatureEngineer:
    """创建包含所有内置派生特征的 FeatureEngineer。

    注：不包含 volume 派生特征——MT5 tick volume 是 tick 计数而非真实成交量，
    对外汇/黄金 CFD 尤其不可信（各家 broker tick 聚合逻辑不同）。
    需要真实 volume 请接入外部数据源（CME Gold Futures / LBMA / 第三方聚合）。
    """
    eng = FeatureEngineer()
    for defn in _BUILTIN_FEATURES:
        eng.register(defn)
    return eng


def get_feature_definition(name: str) -> Optional[FeatureDefinition]:
    return build_default_engineer().definition(name)


def get_feature_inventory() -> Dict[str, Any]:
    return build_default_engineer().inventory()
