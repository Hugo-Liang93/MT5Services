from __future__ import annotations

from src.research.entry_meta.labels import EntryMetaLabelBuilder


def test_labels_mark_positive_pnl_as_take_and_non_positive_as_block() -> None:
    trades = [
        {"pnl": 25.0, "pnl_pct": 1.2},
        {"pnl": 0.0, "pnl_pct": 0.0},
        {"pnl": -10.0, "pnl_pct": -0.5},
    ]

    labels = EntryMetaLabelBuilder().build(trades)

    assert labels.labels == [1, 0, 0]
    assert labels.summary == {"take_entry": 1, "block_entry": 2}


def test_sample_weights_protect_large_winners_and_large_losers() -> None:
    trades = [
        {"pnl": 5.0, "pnl_pct": 0.2},
        {"pnl": 100.0, "pnl_pct": 5.0},
        {"pnl": -80.0, "pnl_pct": -4.0},
    ]

    labels = EntryMetaLabelBuilder(max_weight=5.0).build(trades)

    assert labels.sample_weights[0] == 1.05
    assert labels.sample_weights[1] == 2.0
    assert labels.sample_weights[2] == 1.8


def test_sample_weights_are_capped_at_max_weight() -> None:
    trades = [
        {"pnl": 10_000.0, "pnl_pct": 500.0},
        {"pnl": -10_000.0, "pnl_pct": -500.0},
    ]

    labels = EntryMetaLabelBuilder(max_weight=3.0).build(trades)

    assert labels.sample_weights == [3.0, 3.0]
    assert labels.weight_summary == {"min": 3.0, "max": 3.0, "mean": 3.0}
