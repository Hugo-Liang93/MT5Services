from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.research.core.data_matrix import DataMatrix

_FORBIDDEN_TOKENS = ("forward", "future", "barrier", "outcome", "label")


@dataclass(frozen=True)
class StateEdgeFeatureMatrix:
    feature_keys: list[str]
    rows: np.ndarray
    bar_times: list[str]
    train_indices: list[int]
    test_indices: list[int]
    manifest: dict[str, Any]


class StateEdgeFeatureBuilder:
    """构造 State Edge 模型特征矩阵。

    只允许当前/历史可见字段：indicator_series、hard/soft regime、session。
    显式排除 forward return、barrier outcome、future、label 等泄漏字段。
    """

    def build(self, matrix: DataMatrix) -> StateEdgeFeatureMatrix:
        columns: list[tuple[str, list[float]]] = []

        for indicator, field in sorted(matrix.indicator_series.keys()):
            key = f"indicator.{indicator}.{field}"
            if self._is_forbidden(key):
                continue
            raw_values = matrix.indicator_series[(indicator, field)]
            columns.append((key, [self._to_float(v) for v in raw_values]))

        regimes = [self._regime_name(r) for r in matrix.regimes]
        regime_codes = {name: float(idx) for idx, name in enumerate(sorted(set(regimes)))}
        columns.append(
            (
                "regime.hard_code",
                [regime_codes.get(name, 0.0) for name in regimes],
            )
        )

        soft_keys = sorted(
            {
                str(name)
                for item in matrix.soft_regimes
                if isinstance(item, dict)
                for name in item.keys()
            }
        )
        for name in soft_keys:
            columns.append(
                (
                    f"soft_regime.{name}",
                    [
                        self._to_float(item.get(name, 0.0)) if isinstance(item, dict) else 0.0
                        for item in matrix.soft_regimes
                    ],
                )
            )

        sessions = list(matrix.sessions) if matrix.sessions else ["unknown"] * matrix.n_bars
        if len(sessions) < matrix.n_bars:
            sessions = sessions + ["unknown"] * (matrix.n_bars - len(sessions))
        session_keys = sorted(set(str(s) for s in sessions[: matrix.n_bars]))
        for name in session_keys:
            columns.append(
                (
                    f"session.{name}",
                    [1.0 if str(sessions[i]) == name else 0.0 for i in range(matrix.n_bars)],
                )
            )

        feature_keys = [key for key, _ in columns]
        if columns:
            rows = np.asarray([values for _, values in columns], dtype=float).T
        else:
            rows = np.zeros((matrix.n_bars, 0), dtype=float)
        rows = np.nan_to_num(rows, nan=0.0, posinf=0.0, neginf=0.0)

        return StateEdgeFeatureMatrix(
            feature_keys=feature_keys,
            rows=rows,
            bar_times=[t.isoformat() for t in matrix.bar_times],
            train_indices=list(matrix.train_slice()),
            test_indices=list(matrix.test_slice()),
            manifest={
                "source": "DataMatrix.indicator_series+regime+session",
                "forbidden_tokens": list(_FORBIDDEN_TOKENS),
                "n_features": len(feature_keys),
            },
        )

    @staticmethod
    def _is_forbidden(key: str) -> bool:
        lowered = key.lower()
        return any(token in lowered for token in _FORBIDDEN_TOKENS)

    @staticmethod
    def _regime_name(value: Any) -> str:
        if hasattr(value, "value"):
            return str(value.value)
        if hasattr(value, "name"):
            return str(value.name)
        return str(value)

    @staticmethod
    def _to_float(value: Any) -> float:
        if value is None:
            return 0.0
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(result):
            return 0.0
        return result
