from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.research.entry_meta.dataset import EntryMetaDatasetBuilder


def _matrix() -> SimpleNamespace:
    return SimpleNamespace(
        bar_times=[
            datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc),
        ],
        train_slice=lambda: range(0, 2),
        test_slice=lambda: range(3, 4),
    )


def test_matches_raw_trade_entry_time_to_bar_times_index() -> None:
    trades = [{"entry_time": datetime(2026, 1, 1, 0, 5), "pnl": 10.0}]

    dataset = EntryMetaDatasetBuilder().build(_matrix(), trades)

    assert dataset.bar_indices == [1]
    assert dataset.trades == trades
    assert dataset.labels.labels == [1]
    assert dataset.label_summary == {"take_entry": 1, "block_entry": 0}


def test_train_test_indices_are_dataset_sample_indices_not_bar_indices() -> None:
    trades = [
        {"entry_time": datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc), "pnl": 1.0},
        {"entry_time": datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc), "pnl": -1.0},
    ]

    dataset = EntryMetaDatasetBuilder().build(_matrix(), trades)

    assert dataset.bar_indices == [1, 3]
    assert dataset.train_indices == [0]
    assert dataset.test_indices == [1]


def test_unmatched_trade_is_recorded_and_not_labeled() -> None:
    matched = {"entry_time": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), "pnl": 5.0}
    unmatched = {"entry_time": "not-a-time", "pnl": -100.0}

    dataset = EntryMetaDatasetBuilder().build(_matrix(), [matched, unmatched])

    assert dataset.trades == [matched]
    assert dataset.unmatched_trades == [unmatched]
    assert dataset.labels.labels == [1]


def test_iso_z_entry_time_matches_utc_datetime_bar_time() -> None:
    trades = [{"entry_time": "2026-01-01T00:10:00Z", "pnl": -2.0}]

    dataset = EntryMetaDatasetBuilder().build(_matrix(), trades)

    assert dataset.bar_indices == [2]
    assert dataset.labels.labels == [0]
