from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.research.entry_meta.dataset import EntryMetaDataset, _normalize_timestamp


FORBIDDEN_TOKENS = [
    "forward",
    "future",
    "barrier",
    "outcome",
    "label",
    "pnl",
    "exit",
]

ENTRY_FEATURE_KEYS = [
    "entry.confidence",
    "entry.direction.buy",
    "entry.direction.sell",
    "entry.price",
    "entry.strategy_code",
]


@dataclass(frozen=True)
class EntryMetaFeatureMatrix:
    rows: np.ndarray
    feature_keys: list[str]
    bar_times: list[str]
    train_indices: list[int]
    test_indices: list[int]
    manifest: dict[str, Any]


class EntryMetaFeatureBuilder:
    def build(self, matrix: Any, dataset: EntryMetaDataset) -> EntryMetaFeatureMatrix:
        visible_indicator_keys = self._visible_indicator_keys(matrix)
        feature_keys = [
            *ENTRY_FEATURE_KEYS,
            *[
                f"indicator.{indicator}.{field}"
                for indicator, field in visible_indicator_keys
            ],
            "matrix.regime_code",
            "matrix.session_code",
        ]

        rows = [
            self._build_row(matrix, trade, bar_index, visible_indicator_keys)
            for trade, bar_index in zip(dataset.trades, dataset.bar_indices)
        ]
        row_array = np.array(rows, dtype=float)
        if not rows:
            row_array = np.empty((0, len(feature_keys)), dtype=float)

        return EntryMetaFeatureMatrix(
            rows=row_array,
            feature_keys=feature_keys,
            bar_times=[
                _format_bar_time(matrix.bar_times[bar_index])
                for bar_index in dataset.bar_indices
            ],
            train_indices=list(dataset.train_indices),
            test_indices=list(dataset.test_indices),
            manifest={
                "source": "entry_meta",
                "forbidden_tokens": list(FORBIDDEN_TOKENS),
                "n_features": len(feature_keys),
            },
        )

    def _visible_indicator_keys(self, matrix: Any) -> list[tuple[str, str]]:
        keys = []
        for indicator, field in getattr(matrix, "indicator_series", {}).keys():
            key_text = f"{indicator}.{field}".lower()
            if any(token in key_text for token in FORBIDDEN_TOKENS):
                continue
            keys.append((str(indicator), str(field)))
        return sorted(keys, key=lambda item: (item[0], item[1]))

    def _build_row(
        self,
        matrix: Any,
        trade: dict[str, Any],
        bar_index: int,
        visible_indicator_keys: list[tuple[str, str]],
    ) -> list[float]:
        entry = trade.get("entry")
        entry_values = entry if isinstance(entry, dict) else trade
        row = [
            _to_float(entry_values.get("confidence")),
            1.0 if str(entry_values.get("direction", "")).lower() == "buy" else 0.0,
            1.0 if str(entry_values.get("direction", "")).lower() == "sell" else 0.0,
            _to_float(entry_values.get("price")),
            _to_float(entry_values.get("strategy_code")),
        ]
        indicator_series = getattr(matrix, "indicator_series", {})
        for key in visible_indicator_keys:
            row.append(_to_float(_series_value(indicator_series.get(key, []), bar_index)))
        row.append(_to_float(_series_value(getattr(matrix, "regime_code", []), bar_index)))
        row.append(_to_float(_series_value(getattr(matrix, "session_code", []), bar_index)))
        return row


def _series_value(series: Any, index: int) -> Any:
    try:
        return series[index]
    except (IndexError, TypeError, KeyError):
        return None


def _to_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(result) or math.isinf(result):
        return 0.0
    return result


def _format_bar_time(value: Any) -> str:
    timestamp = _normalize_timestamp(value)
    if timestamp is None:
        return ""
    return timestamp.isoformat()
