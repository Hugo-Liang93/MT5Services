from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.research.entry_meta.dataset import EntryMetaDataset, normalize_timestamp


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
    def __init__(
        self,
        category_mappings: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self._frozen_category_mappings = (
            {
                category: {str(name): float(code) for name, code in mapping.items()}
                for category, mapping in category_mappings.items()
            }
            if category_mappings is not None
            else None
        )

    def build(self, matrix: Any, dataset: EntryMetaDataset) -> EntryMetaFeatureMatrix:
        matrix_contract = _validate_matrix_contract(matrix)
        visible_indicator_keys = self._visible_indicator_keys(matrix)
        trade_features = [_validate_trade(trade, index) for index, trade in enumerate(dataset.trades)]
        regime_names = [_semantic_name(item) for item in matrix_contract["regimes"]]
        session_names = [str(item) for item in matrix_contract["sessions"]]
        category_mappings = self._category_mappings(
            strategies=[item["strategy"] for item in trade_features],
            regimes=regime_names,
            sessions=session_names,
        )
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
                trade_features[sample_index],
                sample_index,
                bar_index,
                visible_indicator_keys,
                regime_names,
                session_names,
                category_mappings,
            )
            for sample_index, bar_index in enumerate(dataset.bar_indices)
        ]
        row_array = np.array(rows, dtype=float)
        if not rows:
            row_array = np.empty((0, len(feature_keys)), dtype=float)

        return EntryMetaFeatureMatrix(
            rows=row_array,
            feature_keys=feature_keys,
            bar_times=[
                _format_bar_time(matrix_contract["bar_times"][bar_index])
                for bar_index in dataset.bar_indices
            ],
            train_indices=list(dataset.train_indices),
            test_indices=list(dataset.test_indices),
            manifest={
                "source": "entry_meta",
                "forbidden_tokens": list(FORBIDDEN_TOKENS),
                "n_features": len(feature_keys),
                "category_mappings": category_mappings,
            },
        )

    def _visible_indicator_keys(self, matrix: Any) -> list[tuple[str, str]]:
        keys = []
        for indicator, field in matrix.indicator_series.keys():
            key_text = f"{indicator}.{field}".lower()
            if any(token in key_text for token in FORBIDDEN_TOKENS):
                continue
            keys.append((str(indicator), str(field)))
        return sorted(keys, key=lambda item: (item[0], item[1]))

    def _build_row(
        self,
        matrix: Any,
        trade: dict[str, Any],
        sample_index: int,
        bar_index: int,
        visible_indicator_keys: list[tuple[str, str]],
        regime_names: list[str],
        session_names: list[str],
        category_mappings: dict[str, dict[str, float]],
    ) -> list[float]:
        regime_name = _series_value(regime_names, bar_index)
        session_name = _series_value(session_names, bar_index)
        row = [
            trade["confidence"],
            1.0 if trade["direction"] == "buy" else 0.0,
            1.0 if trade["direction"] == "sell" else 0.0,
            trade["entry_price"],
            _category_code(
                category_mappings,
                "strategy",
                trade["strategy"],
                f"sample {sample_index} field strategy",
            ),
        ]
        indicator_series = matrix.indicator_series
        for key in visible_indicator_keys:
            row.append(_to_float(_series_value(indicator_series.get(key, []), bar_index)))
        row.append(
            _category_code(
                category_mappings,
                "regime",
                regime_name,
                f"sample {sample_index} field regime",
            )
        )
        row.append(
            _category_code(
                category_mappings,
                "session",
                session_name,
                f"sample {sample_index} field session",
            )
        )
        return row

    def _category_mappings(
        self,
        *,
        strategies: list[str],
        regimes: list[str],
        sessions: list[str],
    ) -> dict[str, dict[str, float]]:
        if self._frozen_category_mappings is not None:
            return {
                "strategy": dict(self._frozen_category_mappings.get("strategy", {})),
                "regime": dict(self._frozen_category_mappings.get("regime", {})),
                "session": dict(self._frozen_category_mappings.get("session", {})),
            }
        return {
            "strategy": _stable_codes(strategies),
            "regime": _stable_codes(regimes),
            "session": _stable_codes(sessions),
        }


def _validate_matrix_contract(matrix: Any) -> dict[str, Any]:
    required_fields = ["bar_times", "indicator_series", "regimes", "sessions"]
    for field in required_fields:
        if not hasattr(matrix, field):
            raise ValueError(f"matrix missing required field {field}")

    bar_times = list(matrix.bar_times)
    n_bars = len(bar_times)
    if hasattr(matrix, "n_bars") and int(matrix.n_bars) != n_bars:
        raise ValueError(
            f"matrix n_bars {matrix.n_bars} must match len(bar_times) {n_bars}"
        )

    indicator_series = matrix.indicator_series
    if not isinstance(indicator_series, dict):
        raise ValueError("matrix field indicator_series must be a dict")
    for key, values in indicator_series.items():
        indicator, field = key
        if len(values) != n_bars:
            raise ValueError(
                "matrix indicator_series "
                f"{indicator}.{field} length {len(values)} must match n_bars {n_bars}"
            )

    regimes = list(matrix.regimes)
    if len(regimes) != n_bars:
        raise ValueError(
            f"matrix regimes length {len(regimes)} must match n_bars {n_bars}"
        )
    sessions = list(matrix.sessions)
    if len(sessions) != n_bars:
        raise ValueError(
            f"matrix sessions length {len(sessions)} must match n_bars {n_bars}"
        )

    return {
        "bar_times": bar_times,
        "indicator_series": indicator_series,
        "regimes": regimes,
        "sessions": sessions,
        "n_bars": n_bars,
    }


def _validate_trade(trade: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "confidence": _required_finite_float(trade, "confidence", index),
        "direction": _required_direction(trade, index),
        "entry_price": _required_positive_float(trade, "entry_price", index),
        "strategy": _required_non_empty_string(trade, "strategy", index),
    }


def _required_finite_float(trade: dict[str, Any], field: str, index: int) -> float:
    if field not in trade:
        raise ValueError(f"sample {index} missing required field {field}")
    try:
        value = float(trade[field])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"sample {index} field {field} must be a finite float") from exc
    if not math.isfinite(value):
        raise ValueError(f"sample {index} field {field} must be a finite float")
    return value


def _required_positive_float(trade: dict[str, Any], field: str, index: int) -> float:
    value = _required_finite_float(trade, field, index)
    if value <= 0.0:
        raise ValueError(f"sample {index} field {field} must be > 0")
    return value


def _required_direction(trade: dict[str, Any], index: int) -> str:
    direction = _required_non_empty_string(trade, "direction", index).lower()
    if direction not in {"buy", "sell"}:
        raise ValueError("sample {index} field direction must be buy or sell".format(index=index))
    return direction


def _required_non_empty_string(trade: dict[str, Any], field: str, index: int) -> str:
    if field not in trade:
        raise ValueError(f"sample {index} missing required field {field}")
    value = trade[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"sample {index} field {field} must be a non-empty string")
    return value.strip()


def _stable_codes(values: Any) -> dict[str, float]:
    names = [str(value) for value in values]
    return {name: float(index) for index, name in enumerate(sorted(set(names)))}


def _category_code(
    mappings: dict[str, dict[str, float]],
    category: str,
    name: Any,
    context: str,
) -> float:
    key = str(name)
    mapping = mappings.get(category, {})
    if key not in mapping:
        raise ValueError(f"{context} has unknown {category} category {key}")
    return mapping[key]


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
    timestamp = normalize_timestamp(value)
    if timestamp is None:
        return ""
    return timestamp.isoformat()
