from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.research.core.barrier import BarrierOutcome
from src.research.core.data_matrix import DataMatrix


class StateEdgeClass(str, Enum):
    LONG = "long"
    SHORT = "short"
    NO_TRADE = "no_trade"


@dataclass(frozen=True)
class StateEdgeLabelSet:
    labels: list[StateEdgeClass]
    long_best_return: list[Optional[float]]
    short_best_return: list[Optional[float]]
    best_long_barrier: list[Optional[tuple]]
    best_short_barrier: list[Optional[tuple]]
    summary: dict[str, int]


class StateEdgeLabelBuilder:
    """从 DataMatrix 的 triple-barrier outcome 构造三分类标签。

    标签只依赖已扣交易成本的 `BarrierOutcome.return_pct`：
    - best long > 0 且不低于 best short -> long
    - best short > 0 且高于 best long -> short
    - 否则 -> no_trade
    """

    def build(self, matrix: DataMatrix) -> StateEdgeLabelSet:
        labels: list[StateEdgeClass] = []
        long_best_return: list[Optional[float]] = []
        short_best_return: list[Optional[float]] = []
        best_long_barrier: list[Optional[tuple]] = []
        best_short_barrier: list[Optional[tuple]] = []

        for idx in range(matrix.n_bars):
            long_ret, long_key = self._best_return_at(matrix.barrier_returns_long, idx)
            short_ret, short_key = self._best_return_at(
                matrix.barrier_returns_short, idx
            )
            long_best_return.append(long_ret)
            short_best_return.append(short_ret)
            best_long_barrier.append(long_key)
            best_short_barrier.append(short_key)

            if (
                long_ret is not None
                and long_ret > 0
                and (short_ret is None or long_ret >= short_ret)
            ):
                labels.append(StateEdgeClass.LONG)
            elif (
                short_ret is not None
                and short_ret > 0
                and (long_ret is None or short_ret > long_ret)
            ):
                labels.append(StateEdgeClass.SHORT)
            else:
                labels.append(StateEdgeClass.NO_TRADE)

        counts = Counter(label.value for label in labels)
        summary = {
            label.value: int(counts.get(label.value, 0)) for label in StateEdgeClass
        }
        return StateEdgeLabelSet(
            labels=labels,
            long_best_return=long_best_return,
            short_best_return=short_best_return,
            best_long_barrier=best_long_barrier,
            best_short_barrier=best_short_barrier,
            summary=summary,
        )

    @staticmethod
    def _best_return_at(
        returns_by_barrier: dict[tuple, list[Optional[BarrierOutcome]]],
        idx: int,
    ) -> tuple[Optional[float], Optional[tuple]]:
        best_value: Optional[float] = None
        best_key: Optional[tuple] = None
        for key, outcomes in returns_by_barrier.items():
            if idx >= len(outcomes):
                continue
            outcome = outcomes[idx]
            if outcome is None:
                continue
            value = float(outcome.return_pct)
            if best_value is None or value > best_value:
                best_value = value
                best_key = key
        return best_value, best_key
