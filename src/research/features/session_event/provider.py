"""src/research/features/session_event/provider.py

SessionEventProvider — 时间窗口与经济事件特征集。

计算以下特征（均挂在 group="session_event"）：

  时间特征（4 项）：
    session_phase          — 0=亚盘(0-7h)/1=欧盘(7-12h)/2=美盘(12-21h)/3=尾盘(21-24h)
    london_session         — 1.0 if hour in [7,16), else 0.0
    ny_session             — 1.0 if hour in [12,21), else 0.0
    day_progress           — (h*3600+m*60+s)/86400

  事件特征（3 项）：
    bars_to_next_high_impact_event   — 到下一个高影响事件的 tf-bar 数
    bars_since_last_high_impact_event — 距上一个高影响事件的 tf-bar 数
    in_news_window         — 1.0 if |bar_time - 最近事件| ≤ 30min

所有特征 Role: WHEN。

迁移自 engineer.py _batch_session_phase / _batch_london_session /
_batch_ny_session / _batch_day_progress / _batch_bars_to_next_high_impact_event /
_batch_bars_since_last_high_impact_event / _batch_in_news_window。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.research.features.protocol import FeatureProvider, FeatureRole, ProviderDataRequirement

_GROUP = "session_event"

# 7 个固定特征
_FEATURE_NAMES: Tuple[str, ...] = (
    "session_phase",
    "london_session",
    "ny_session",
    "day_progress",
    "bars_to_next_high_impact_event",
    "bars_since_last_high_impact_event",
    "in_news_window",
)


class SessionEventProvider:
    """时间窗口与经济事件特征计算器。

    满足 FeatureProvider Protocol。
    所有计算均基于 DataMatrix.bar_times 和 high_impact_event_times。
    无需 indicator_series 中的任何列。
    """

    # ------------------------------------------------------------------
    # FeatureProvider Protocol
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return _GROUP

    @property
    def feature_count(self) -> int:
        return len(_FEATURE_NAMES)

    def required_columns(self) -> List[Tuple[str, str]]:
        """不依赖任何 indicator_series 列。"""
        return []

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        """无需跨 TF 额外数据。"""
        return None

    def role_mapping(self) -> Dict[str, FeatureRole]:
        """所有特征角色均为 WHEN。"""
        return {name: FeatureRole.WHEN for name in _FEATURE_NAMES}

    def compute(
        self,
        matrix: Any,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        """执行全量特征计算。"""
        n: int = matrix.n_bars
        if n == 0:
            return {(_GROUP, name): [] for name in _FEATURE_NAMES}

        result: Dict[Tuple[str, str], List[Optional[float]]] = {}

        result[(_GROUP, "session_phase")] = _batch_session_phase(matrix)
        result[(_GROUP, "london_session")] = _batch_london_session(matrix)
        result[(_GROUP, "ny_session")] = _batch_ny_session(matrix)
        result[(_GROUP, "day_progress")] = _batch_day_progress(matrix)
        result[(_GROUP, "bars_to_next_high_impact_event")] = (
            _batch_bars_to_next_high_impact_event(matrix)
        )
        result[(_GROUP, "bars_since_last_high_impact_event")] = (
            _batch_bars_since_last_high_impact_event(matrix)
        )
        result[(_GROUP, "in_news_window")] = _batch_in_news_window(matrix)

        return result


# ---------------------------------------------------------------------------
# 内部批量计算函数（迁移自 engineer.py）
# ---------------------------------------------------------------------------


def _tf_minutes(tf: str) -> int:
    """时间框架字符串 → 分钟数（简化版）。"""
    tf = (tf or "").strip().upper()
    table = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
    return table.get(tf, 0)


def _batch_session_phase(matrix: Any) -> List[Optional[float]]:
    """UTC 时段编码：0=亚盘 / 1=欧盘 / 2=美盘 / 3=尾盘。

    迁移自 engineer.py _batch_session_phase（lines 564-574）。
    """
    hours = np.asarray([t.hour for t in matrix.bar_times], dtype=np.int64)
    out = np.where(
        hours < 7,
        0.0,
        np.where(hours < 12, 1.0, np.where(hours < 21, 2.0, 3.0)),
    )
    return [float(v) for v in out]


def _batch_london_session(matrix: Any) -> List[Optional[float]]:
    """伦敦盘标志：1.0 if hour in [7, 16), else 0.0。

    迁移自 engineer.py _batch_london_session（lines 577-582）。
    """
    hours = np.asarray([t.hour for t in matrix.bar_times], dtype=np.int64)
    out = ((hours >= 7) & (hours < 16)).astype(np.float64)
    return [float(v) for v in out]


def _batch_ny_session(matrix: Any) -> List[Optional[float]]:
    """纽约盘标志：1.0 if hour in [12, 21), else 0.0。

    迁移自 engineer.py _batch_ny_session（lines 585-590）。
    """
    hours = np.asarray([t.hour for t in matrix.bar_times], dtype=np.int64)
    out = ((hours >= 12) & (hours < 21)).astype(np.float64)
    return [float(v) for v in out]


def _batch_day_progress(matrix: Any) -> List[Optional[float]]:
    """UTC 日内进度 [0, 1]：(h*3600+m*60+s)/86400。

    迁移自 engineer.py _batch_day_progress（lines 593-601）。
    """
    secs = np.asarray(
        [t.hour * 3600 + t.minute * 60 + t.second for t in matrix.bar_times],
        dtype=np.float64,
    )
    return [float(v) for v in secs / 86400.0]


def _batch_bars_to_next_high_impact_event(matrix: Any) -> List[Optional[float]]:
    """每个 bar 到下一个高影响事件的 tf-bar 数。

    迁移自 engineer.py _batch_bars_to_next_high_impact_event（lines 636-663）。
    无事件或 tf_minutes <= 0 时返回全 None。
    """
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


def _batch_bars_since_last_high_impact_event(matrix: Any) -> List[Optional[float]]:
    """每个 bar 距上一个高影响事件的 tf-bar 数。

    迁移自 engineer.py _batch_bars_since_last_high_impact_event（lines 666-691）。
    无事件或 tf_minutes <= 0 时返回全 None。
    """
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


def _batch_in_news_window(matrix: Any) -> List[Optional[float]]:
    """|bar_time - 最近事件| ≤ 30 分钟 → 1.0，否则 0.0。

    迁移自 engineer.py _batch_in_news_window（lines 694-720）。
    无事件时所有 bar 返回 0.0。
    """
    events = matrix.high_impact_event_times
    n = matrix.n_bars
    if not events:
        return [0.0] * n

    bar_ts = np.asarray([t.timestamp() for t in matrix.bar_times], dtype=np.float64)
    ev_ts = np.asarray([e.timestamp() for e in events], dtype=np.float64)
    # 左右各找一个事件，取绝对距离较小者
    right_idx = np.searchsorted(ev_ts, bar_ts, side="right")
    left_idx = right_idx - 1

    result_arr = np.zeros(n, dtype=np.float64)
    threshold = 30 * 60.0
    n_events = len(ev_ts)
    for i in range(n):
        best = float("inf")
        if left_idx[i] >= 0:
            best = min(best, abs(bar_ts[i] - ev_ts[left_idx[i]]))
        if right_idx[i] < n_events:
            best = min(best, abs(ev_ts[right_idx[i]] - bar_ts[i]))
        if best <= threshold:
            result_arr[i] = 1.0
    return [float(v) for v in result_arr]
