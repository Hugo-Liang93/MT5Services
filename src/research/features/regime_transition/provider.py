"""src/research/features/regime_transition/provider.py

RegimeTransitionFeatureProvider — Regime 状态转换特征集。

计算以下特征（均挂在 group="regime_transition"）：
  固定特征（12 项）：
    regime_entropy        — -Σ(p·ln(p))，soft_regime 的概率熵
    bars_in_regime        — 当前 regime 连续持续 bar 数
    bars_since_change     — 距上次 regime 变化的 bar 数（变化当 bar = 0）
    prev_regime           — 上一个 regime 的编码（trending=0,ranging=1,breakout=2,uncertain=3）
    duration_vs_avg       — 当前 regime 持续 / 该 regime 历史平均持续
    dominant_strength     — max(soft_probs) per bar

  窗口特征（依 prob_delta_window=w 展开）：
    transitions_{w}       — 过去 w bar 内 regime 变化次数
    trending_prob_delta_{w}
    ranging_prob_delta_{w}
    breakout_prob_delta_{w}
    entropy_delta_{w}

迁移自 engineer.py 中的 _batch_regime_entropy / _batch_bars_in_regime（Batch 版）。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.research.features.protocol import FeatureProvider, FeatureRole, ProviderDataRequirement
from src.research.core.config import RegimeTransitionProviderConfig
from src.signals.evaluation.regime import RegimeType

# regime → float 编码
_REGIME_CODE: Dict[RegimeType, float] = {
    RegimeType.TRENDING: 0.0,
    RegimeType.RANGING: 1.0,
    RegimeType.BREAKOUT: 2.0,
    RegimeType.UNCERTAIN: 3.0,
}

# 软概率键名（固定顺序）
_SOFT_KEYS = ("trending", "ranging", "breakout")


class RegimeTransitionFeatureProvider:
    """Regime 状态转换特征计算器。

    满足 FeatureProvider Protocol。
    """

    def __init__(
        self,
        config: Optional[RegimeTransitionProviderConfig] = None,
    ) -> None:
        self._cfg = config or RegimeTransitionProviderConfig()
        # 确定窗口列表：history_window + prob_delta_window
        self._w = self._cfg.prob_delta_window  # 主窗口，用于 prob_delta / entropy_delta / transitions

    # ------------------------------------------------------------------
    # FeatureProvider Protocol
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "regime_transition"

    @property
    def feature_count(self) -> int:
        # 固定 6 + 窗口展开 5（transitions/3 prob_delta/entropy_delta）
        return 6 + 5

    def required_columns(self) -> List[Tuple[str, str]]:
        """不依赖任何 indicator_series 列。"""
        return []

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        """无需跨 TF 额外数据。"""
        return None

    def role_mapping(self) -> Dict[str, FeatureRole]:
        w = self._w
        mapping: Dict[str, FeatureRole] = {
            "regime_entropy": FeatureRole.WHEN,
            "bars_in_regime": FeatureRole.WHEN,
            "bars_since_change": FeatureRole.WHEN,
            "prev_regime": FeatureRole.WHY,
            "duration_vs_avg": FeatureRole.WHEN,
            "dominant_strength": FeatureRole.WHY,
            f"transitions_{w}": FeatureRole.WHEN,
            f"trending_prob_delta_{w}": FeatureRole.WHY,
            f"ranging_prob_delta_{w}": FeatureRole.WHY,
            f"breakout_prob_delta_{w}": FeatureRole.WHY,
            f"entropy_delta_{w}": FeatureRole.WHEN,
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

        regimes: List[RegimeType] = matrix.regimes
        soft_regimes: List[Optional[Dict[str, float]]] = matrix.soft_regimes
        w = self._w

        # --- 预计算 entropy 序列（供 entropy_delta 复用）---
        entropy_series = _batch_regime_entropy(soft_regimes)

        result: Dict[Tuple[str, str], List[Optional[float]]] = {}

        # 1. regime_entropy
        result[("regime_transition", "regime_entropy")] = entropy_series

        # 2. bars_in_regime
        result[("regime_transition", "bars_in_regime")] = _batch_bars_in_regime(regimes, n)

        # 3. bars_since_change
        result[("regime_transition", "bars_since_change")] = _batch_bars_since_change(regimes, n)

        # 4. prev_regime
        result[("regime_transition", "prev_regime")] = _batch_prev_regime(regimes, n)

        # 5. duration_vs_avg
        result[("regime_transition", "duration_vs_avg")] = _batch_duration_vs_avg(regimes, n)

        # 6. dominant_strength
        result[("regime_transition", "dominant_strength")] = _batch_dominant_strength(soft_regimes)

        # 7. transitions_{w}
        result[("regime_transition", f"transitions_{w}")] = _batch_transitions(regimes, n, w)

        # 8-10. prob_delta_{w} for each soft key
        for key in _SOFT_KEYS:
            result[("regime_transition", f"{key}_prob_delta_{w}")] = (
                _batch_prob_delta(soft_regimes, key, w)
            )

        # 11. entropy_delta_{w}
        result[("regime_transition", f"entropy_delta_{w}")] = (
            _batch_series_delta(entropy_series, w)
        )

        return result

    def _empty_result(self) -> Dict[Tuple[str, str], List[Optional[float]]]:
        w = self._w
        keys = [
            "regime_entropy",
            "bars_in_regime",
            "bars_since_change",
            "prev_regime",
            "duration_vs_avg",
            "dominant_strength",
            f"transitions_{w}",
            f"trending_prob_delta_{w}",
            f"ranging_prob_delta_{w}",
            f"breakout_prob_delta_{w}",
            f"entropy_delta_{w}",
        ]
        return {("regime_transition", k): [] for k in keys}


# ---------------------------------------------------------------------------
# 内部批量计算函数
# ---------------------------------------------------------------------------


def _batch_regime_entropy(
    soft_regimes: List[Optional[Dict[str, float]]],
) -> List[Optional[float]]:
    """-Σ(p·ln(p))，使用 numpy inner product 替代 Python math.log 逐元素。

    迁移自 engineer.py _batch_regime_entropy（保留原实现策略）。
    """
    out: List[Optional[float]] = []
    for sr in soft_regimes:
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


def _batch_bars_in_regime(
    regimes: List[RegimeType],
    n: int,
) -> List[Optional[float]]:
    """每 bar 当前 regime 连续持续 bar 数。

    迁移自 engineer.py _batch_bars_in_regime。
    """
    if n == 0:
        return []
    counts = np.ones(n, dtype=np.float64)
    for j in range(1, n):
        if regimes[j] == regimes[j - 1]:
            counts[j] = counts[j - 1] + 1.0
        else:
            counts[j] = 1.0
    # 返回 List[Optional[float]]（值均非 None）
    return [float(v) for v in counts]


def _batch_bars_since_change(
    regimes: List[RegimeType],
    n: int,
) -> List[Optional[float]]:
    """距上次 regime 变化的 bar 数。第一个 bar 和每次变化当 bar = 0。"""
    if n == 0:
        return []
    out: List[Optional[float]] = [0.0]
    for j in range(1, n):
        if regimes[j] != regimes[j - 1]:
            out.append(0.0)
        else:
            prev = out[j - 1]
            out.append((prev or 0.0) + 1.0)
    return out


def _batch_prev_regime(
    regimes: List[RegimeType],
    n: int,
) -> List[Optional[float]]:
    """上一个 regime 编码；发生变化前（含第一段内）返回 None。

    语义：记录"上一段完整 regime"的编码。在第一次变化发生之前，
    所有 bar 返回 None（没有历史 regime）。第一次变化发生时，
    从该 bar 开始记录变化前的 regime 编码，直到下次变化。
    """
    if n == 0:
        return []
    out: List[Optional[float]] = [None] * n
    prev_code: Optional[float] = None
    for j in range(1, n):
        if regimes[j] != regimes[j - 1]:
            # 发生变化：更新 prev_code 为上一段的 regime
            prev_code = _REGIME_CODE.get(regimes[j - 1])
        out[j] = prev_code
    return out


def _batch_duration_vs_avg(
    regimes: List[RegimeType],
    n: int,
) -> List[Optional[float]]:
    """当前 regime 持续 / 该 regime 历史平均持续（只用已完成的 run）。

    首个 bar 或尚无历史数据的 regime → None。
    """
    if n == 0:
        return []

    # 先计算 bars_in_regime（已有函数）
    bars_in = _batch_bars_in_regime(regimes, n)

    # 收集历史完整 run 长度（每次发生 regime 变化时，上一段完整 run 结束）
    # histories[regime_type] = list of completed run lengths
    histories: Dict[RegimeType, List[float]] = {rt: [] for rt in RegimeType}
    avgs: Dict[RegimeType, Optional[float]] = {rt: None for rt in RegimeType}

    out: List[Optional[float]] = []
    for j in range(n):
        current = regimes[j]
        cur_dur_raw = bars_in[j]
        cur_dur = float(cur_dur_raw) if cur_dur_raw is not None else 1.0

        # 当前 regime 的历史均值
        avg = avgs[current]
        if avg is None:
            out.append(None)
        else:
            out.append(cur_dur / avg)

        # 检测 regime 变化（j→j+1），将当前完整 run 记入历史
        if j + 1 < n and regimes[j + 1] != current:
            # 当前 run 结束：持续长度 = cur_dur
            histories[current].append(cur_dur)
            # 更新该 regime 的历史均值
            avgs[current] = sum(histories[current]) / len(histories[current])

    return out


def _batch_dominant_strength(
    soft_regimes: List[Optional[Dict[str, float]]],
) -> List[Optional[float]]:
    """max(soft_probs) per bar。"""
    out: List[Optional[float]] = []
    for sr in soft_regimes:
        if sr is None:
            out.append(None)
        else:
            vals = list(sr.values())
            out.append(max(vals) if vals else None)
    return out


def _batch_transitions(
    regimes: List[RegimeType],
    n: int,
    w: int,
) -> List[Optional[float]]:
    """过去 w bar 内（含当前 bar）的 regime 变化次数。"""
    if n == 0:
        return []
    # change[j] = 1 if regimes[j] != regimes[j-1], else 0; change[0] = 0
    changes = np.zeros(n, dtype=np.float64)
    for j in range(1, n):
        if regimes[j] != regimes[j - 1]:
            changes[j] = 1.0

    # 滚动求和（窗口 w）
    out: List[Optional[float]] = []
    cumsum = np.cumsum(changes)
    for j in range(n):
        start = max(0, j - w + 1)
        if start == 0:
            total = cumsum[j]
        else:
            total = cumsum[j] - cumsum[start - 1]
        out.append(float(total))
    return out


def _batch_prob_delta(
    soft_regimes: List[Optional[Dict[str, float]]],
    key: str,
    w: int,
) -> List[Optional[float]]:
    """soft_regimes[key] 在过去 w bar 内的变化量（当前 - w bar 前）。"""
    n = len(soft_regimes)
    if n == 0:
        return []

    # 先提取每 bar 的单个概率（可能 None）
    probs: List[Optional[float]] = []
    for sr in soft_regimes:
        if sr is None:
            probs.append(None)
        else:
            probs.append(sr.get(key))

    out: List[Optional[float]] = []
    for j in range(n):
        ref_idx = j - w
        if ref_idx < 0:
            out.append(None)
            continue
        cur = probs[j]
        ref = probs[ref_idx]
        if cur is None or ref is None:
            out.append(None)
        else:
            out.append(cur - ref)
    return out


def _batch_series_delta(
    series: List[Optional[float]],
    w: int,
) -> List[Optional[float]]:
    """任意 Optional[float] 序列在 w bar 内的变化量（当前 - w bar 前）。"""
    n = len(series)
    out: List[Optional[float]] = []
    for j in range(n):
        ref_idx = j - w
        if ref_idx < 0:
            out.append(None)
            continue
        cur = series[j]
        ref = series[ref_idx]
        if cur is None or ref is None:
            out.append(None)
        else:
            out.append(cur - ref)
    return out
