"""src/research/features/intrabar/provider.py

IntrabarProvider — 子 TF 内 bar 结构特征集。

计算以下特征（均挂在 group="intrabar"）：

  子 bar 特征（5 项），需要 matrix.child_bars 非空，否则返回空 dict：
    child_bar_consensus        — 子 bars 与父 bar 同色的比例             Role: WHY
    child_range_acceleration   — 后半/前半段平均 range 比 - 1            Role: WHEN
    intrabar_momentum_shift    — 方向切换次数 / (n_children-1)           Role: WHY
    child_volume_front_weight  — 前半 volume / 总 volume                 Role: WHEN
    child_bar_count_ratio      — 实际子 bar 数 / 期望子 bar 数           Role: WHERE

条件计算：若 matrix.child_bars 为空（{}），compute() 返回空 dict（{}），
不向 DataMatrix 写入任何列，节省内存与计算开销。

迁移自 engineer.py _batch_child_bar_consensus / _batch_child_range_acceleration /
_batch_intrabar_momentum_shift / _batch_child_volume_front_weight /
_batch_child_bar_count_ratio（lines 765-900）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.research.features.protocol import FeatureProvider, FeatureRole, ProviderDataRequirement

_GROUP = "intrabar"

_FEATURE_NAMES: Tuple[str, ...] = (
    "child_bar_consensus",
    "child_range_acceleration",
    "intrabar_momentum_shift",
    "child_volume_front_weight",
    "child_bar_count_ratio",
)


class IntrabarProvider:
    """子 TF 内 bar 结构特征计算器。

    满足 FeatureProvider Protocol。

    条件行为：若 matrix.child_bars 为空（{}），compute() 返回空 dict，
    不产生任何特征列。
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
        return {
            "child_bar_consensus": FeatureRole.WHY,
            "child_range_acceleration": FeatureRole.WHEN,
            "intrabar_momentum_shift": FeatureRole.WHY,
            "child_volume_front_weight": FeatureRole.WHEN,
            "child_bar_count_ratio": FeatureRole.WHERE,
        }

    def compute(
        self,
        matrix: Any,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        """执行全量特征计算。

        若 matrix.child_bars 为空，返回空 dict（不产生任何列）。
        """
        child_bars: Dict[int, List[Any]] = matrix.child_bars
        if not child_bars:
            return {}

        n: int = matrix.n_bars
        result: Dict[Tuple[str, str], List[Optional[float]]] = {}

        result[(_GROUP, "child_bar_consensus")] = _batch_child_bar_consensus(matrix, n)
        result[(_GROUP, "child_range_acceleration")] = (
            _batch_child_range_acceleration(matrix, n)
        )
        result[(_GROUP, "intrabar_momentum_shift")] = (
            _batch_intrabar_momentum_shift(matrix, n)
        )
        result[(_GROUP, "child_volume_front_weight")] = (
            _batch_child_volume_front_weight(matrix, n)
        )
        result[(_GROUP, "child_bar_count_ratio")] = (
            _batch_child_bar_count_ratio(matrix, n)
        )

        return result


# ---------------------------------------------------------------------------
# 内部批量计算函数（迁移自 engineer.py）
# ---------------------------------------------------------------------------


def _batch_child_bar_consensus(matrix: Any, n: int) -> List[Optional[float]]:
    """子 TF 同色比例（batch）。

    迁移自 engineer.py _batch_child_bar_consensus（lines 765-776）。
    """
    out: List[Optional[float]] = []
    for i in range(n):
        children = matrix.child_bars.get(i)
        if not children or len(children) < 2:
            out.append(None)
            continue
        parent_bullish = matrix.closes[i] > matrix.opens[i]
        same = sum(1 for b in children if (b.close > b.open) == parent_bullish)
        out.append(same / len(children))
    return out


def _batch_child_range_acceleration(matrix: Any, n: int) -> List[Optional[float]]:
    """前半段 vs 后半段 range 扩张比：second_half_avg_range / first_half_avg_range - 1。

    迁移自 engineer.py _batch_child_range_acceleration（lines 794-811）。
    子 bar 数 < 4 → None；前半平均 range < 1e-9 → None。
    """
    out: List[Optional[float]] = []
    for i in range(n):
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


def _batch_intrabar_momentum_shift(matrix: Any, n: int) -> List[Optional[float]]:
    """子 TF close-to-close 方向切换比：sign_changes / (n_children - 1)。

    迁移自 engineer.py _batch_intrabar_momentum_shift（lines 831-836），
    内部逻辑复用 _intrabar_momentum_shift per-bar 版本（lines 814-828）。
    子 bar 数 < 3 → None。
    """
    out: List[Optional[float]] = []
    for i in range(n):
        out.append(_intrabar_momentum_shift_single(matrix.child_bars.get(i)))
    return out


def _intrabar_momentum_shift_single(
    children: Optional[List[Any]],
) -> Optional[float]:
    """单 bar 的方向切换比计算。"""
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


def _batch_child_volume_front_weight(matrix: Any, n: int) -> List[Optional[float]]:
    """前半 volume / 总 volume。

    迁移自 engineer.py _batch_child_volume_front_weight（lines 852-857）。
    子 bar 数 < 4 → None；总 volume < 1e-9 → None。
    """
    out: List[Optional[float]] = []
    for i in range(n):
        children = matrix.child_bars.get(i)
        if not children or len(children) < 4:
            out.append(None)
            continue
        mid = len(children) // 2
        total = sum(b.volume for b in children)
        if total < 1e-9:
            out.append(None)
        else:
            front = sum(b.volume for b in children[:mid])
            out.append(front / total)
    return out


def _batch_child_bar_count_ratio(matrix: Any, n: int) -> List[Optional[float]]:
    """子 bar 数量比（batch）：实际子 bar 数 / 期望子 bar 数。

    迁移自 engineer.py _batch_child_bar_count_ratio（lines 879-900）。
    child_tf 为空、或 timeframe_seconds 无法解析 → None。
    """
    child_tf: str = matrix.child_tf
    if not child_tf:
        return [None] * n

    from src.utils.common import timeframe_seconds

    parent_secs = timeframe_seconds(matrix.timeframe)
    child_secs = timeframe_seconds(child_tf)
    if child_secs <= 0 or parent_secs <= 0:
        return [None] * n
    expected = parent_secs // child_secs
    if expected <= 0:
        return [None] * n

    out: List[Optional[float]] = []
    for i in range(n):
        children = matrix.child_bars.get(i)
        if children is None:
            out.append(None)
        else:
            out.append(len(children) / expected)
    return out
