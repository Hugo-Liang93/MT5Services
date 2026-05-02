from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.research.entry_meta.labels import EntryMetaLabelBuilder, EntryMetaLabelSet


@dataclass(frozen=True)
class EntryMetaDataset:
    trades: list[dict[str, Any]]
    bar_indices: list[int]
    labels: EntryMetaLabelSet
    train_indices: list[int]
    test_indices: list[int]
    unmatched_trades: list[dict[str, Any]]

    @property
    def label_summary(self) -> dict[str, int]:
        return self.labels.summary


class EntryMetaDatasetBuilder:
    def __init__(self, label_builder: EntryMetaLabelBuilder | None = None) -> None:
        self._label_builder = label_builder or EntryMetaLabelBuilder()

    def build(
        self,
        matrix: Any,
        raw_trades: list[dict[str, Any]],
    ) -> EntryMetaDataset:
        bar_time_to_index = {
            normalized: index
            for index, normalized in enumerate(
                _normalize_timestamp(item) for item in _matrix_bar_times(matrix)
            )
            if normalized is not None
        }

        trades: list[dict[str, Any]] = []
        bar_indices: list[int] = []
        unmatched_trades: list[dict[str, Any]] = []

        for raw_trade in raw_trades:
            trade = dict(raw_trade)
            entry_time = _normalize_timestamp(trade.get("entry_time"))
            bar_index = bar_time_to_index.get(entry_time)
            if entry_time is None or bar_index is None:
                unmatched_trades.append(trade)
                continue
            trades.append(trade)
            bar_indices.append(bar_index)

        train_bar_indices = set(matrix.train_slice())
        test_bar_indices = set(matrix.test_slice())
        train_indices = [
            sample_index
            for sample_index, bar_index in enumerate(bar_indices)
            if bar_index in train_bar_indices
        ]
        test_indices = [
            sample_index
            for sample_index, bar_index in enumerate(bar_indices)
            if bar_index in test_bar_indices
        ]

        return EntryMetaDataset(
            trades=trades,
            bar_indices=bar_indices,
            labels=self._label_builder.build(trades),
            train_indices=train_indices,
            test_indices=test_indices,
            unmatched_trades=unmatched_trades,
        )


def _matrix_bar_times(matrix: Any) -> list[Any]:
    if not hasattr(matrix, "bar_times"):
        raise ValueError("matrix must expose bar_times")
    return list(matrix.bar_times)


def _normalize_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            timestamp = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)
