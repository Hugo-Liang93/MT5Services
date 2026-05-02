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
        strategy_codes = _stable_codes(
            str(trade.get("strategy", "")) for trade in dataset.trades
        )
        regime_names = [_semantic_name(item) for item in getattr(matrix, "regimes", [])]
        regime_codes = _stable_codes(regime_names)
        session_names = [str(item) for item in getattr(matrix, "sessions", [])]
        session_codes = _stable_codes(session_names)
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
            self._build_row(
                matrix,
                trade,
                bar_index,
                visible_indicator_keys,
                strategy_codes,
                regime_names,
                regime_codes,
                session_names,
                session_codes,
            )
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
        strategy_codes: dict[str, float],
        regime_names: list[str],
        regime_codes: dict[str, float],
        session_names: list[str],
        session_codes: dict[str, float],
    ) -> list[float]:
        strategy_name = str(trade.get("strategy", ""))
        regime_name = _series_value(regime_names, bar_index)
        session_name = _series_value(session_names, bar_index)
        row = [
            _to_float(trade.get("confidence")),
            1.0 if str(trade.get("direction", "")).lower() == "buy" else 0.0,
            1.0 if str(trade.get("direction", "")).lower() == "sell" else 0.0,
            _to_float(trade.get("entry_price")),
            strategy_codes.get(strategy_name, 0.0),
        ]
        indicator_series = getattr(matrix, "indicator_series", {})
        for key in visible_indicator_keys:
            row.append(_to_float(_series_value(indicator_series.get(key, []), bar_index)))
        row.append(regime_codes.get(regime_name, 0.0))
        row.append(session_codes.get(session_name, 0.0))
        return row


def _stable_codes(values: Any) -> dict[str, float]:
    names = [str(value) for value in values]
    return {name: float(index) for index, name in enumerate(sorted(set(names)))}


def _semantic_name(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    if hasattr(value, "name"):
        return str(value.name)
    return str(value)


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
