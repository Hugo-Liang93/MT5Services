from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from src.research.core.data_matrix import DataMatrix
from src.research.state_edge.features import StateEdgeFeatureMatrix
from src.research.state_edge.labels import StateEdgeLabelSet


@dataclass(frozen=True)
class SequenceWindowMatrix:
    windows: np.ndarray
    target_indices: list[int]
    train_sample_indices: list[int]
    test_sample_indices: list[int]
    feature_keys: list[str]
    labels: list[int]
    manifest: dict[str, Any]


class SequenceWindowBuilder:
    """把逐 bar 特征转换为只含历史信息的固定长度 K 线序列窗口。"""

    def __init__(self, *, window: int = 64) -> None:
        if window < 2:
            raise ValueError("sequence window must be >= 2")
        self._window = int(window)

    def build(
        self,
        matrix: DataMatrix,
        features: StateEdgeFeatureMatrix,
        labels: StateEdgeLabelSet,
    ) -> SequenceWindowMatrix:
        candle_keys, candle_rows = self._build_candle_shape_rows(matrix)
        all_rows = np.hstack([features.rows, candle_rows])
        feature_keys = list(features.feature_keys) + candle_keys

        windows: list[np.ndarray] = []
        target_indices: list[int] = []
        y: list[int] = []
        train_sample_indices: list[int] = []
        test_sample_indices: list[int] = []

        for target_idx in range(self._window - 1, matrix.n_bars):
            sample_idx = len(windows)
            start_idx = target_idx - self._window + 1
            windows.append(all_rows[start_idx : target_idx + 1])
            target_indices.append(target_idx)
            y.append(_label_to_int(labels.labels[target_idx]))
            if target_idx < matrix.train_end_idx:
                train_sample_indices.append(sample_idx)
            elif target_idx >= matrix.split_idx:
                test_sample_indices.append(sample_idx)

        window_array = (
            np.asarray(windows, dtype=np.float32)
            if windows
            else np.zeros((0, self._window, len(feature_keys)), dtype=np.float32)
        )
        window_array = np.nan_to_num(window_array, nan=0.0, posinf=0.0, neginf=0.0)
        return SequenceWindowMatrix(
            windows=window_array,
            target_indices=target_indices,
            train_sample_indices=train_sample_indices,
            test_sample_indices=test_sample_indices,
            feature_keys=feature_keys,
            labels=y,
            manifest={
                "kind": "sequence_window",
                "window": self._window,
                "source": "StateEdgeFeatureMatrix+candle_shape",
                "n_features": len(feature_keys),
            },
        )

    @staticmethod
    def _build_candle_shape_rows(matrix: DataMatrix) -> tuple[list[str], np.ndarray]:
        keys = [
            "candle.log_return",
            "candle.body_ratio",
            "candle.upper_wick_ratio",
            "candle.lower_wick_ratio",
            "candle.close_location",
            "candle.range_pct",
        ]
        rows: list[list[float]] = []
        prev_close = None
        for idx in range(matrix.n_bars):
            open_price = float(matrix.opens[idx])
            high = float(matrix.highs[idx])
            low = float(matrix.lows[idx])
            close = float(matrix.closes[idx])
            span = max(high - low, 1e-12)
            base = max(abs(close), 1e-12)
            if prev_close is None or prev_close <= 0 or close <= 0:
                log_return = 0.0
            else:
                log_return = float(np.log(close / prev_close))
            body_ratio = (close - open_price) / span
            upper_wick_ratio = (high - max(open_price, close)) / span
            lower_wick_ratio = (min(open_price, close) - low) / span
            close_location = (close - low) / span
            range_pct = span / base
            rows.append(
                [
                    log_return,
                    body_ratio,
                    upper_wick_ratio,
                    lower_wick_ratio,
                    close_location,
                    range_pct,
                ]
            )
            prev_close = close
        return keys, np.asarray(rows, dtype=np.float32)


def _label_to_int(label: Any) -> int:
    value = str(getattr(label, "value", label))
    if value == "long":
        return 0
    if value == "short":
        return 1
    return 2
