from __future__ import annotations

from src.research.entry_meta.quality import evaluate_entry_meta_quality


def test_quality_accepts_when_samples_oos_and_classes_meet_thresholds() -> None:
    result = evaluate_entry_meta_quality(
        {"take_entry": 20, "block_entry": 20},
        {"oos_samples": 10},
        min_samples=40,
        min_oos_samples=10,
        min_class_samples=10,
    )

    assert result == {"status": "accepted", "reason": "accepted"}


def test_quality_refits_when_total_samples_are_insufficient() -> None:
    result = evaluate_entry_meta_quality(
        {"take_entry": 19, "block_entry": 20},
        {"oos_samples": 10},
        min_samples=40,
        min_oos_samples=10,
        min_class_samples=10,
    )

    assert result == {"status": "refit", "reason": "insufficient_samples"}


def test_quality_refits_when_oos_samples_are_insufficient() -> None:
    result = evaluate_entry_meta_quality(
        {"take_entry": 20, "block_entry": 20},
        {"oos_samples": 9},
        min_samples=40,
        min_oos_samples=10,
        min_class_samples=10,
    )

    assert result == {"status": "refit", "reason": "insufficient_oos_samples"}


def test_quality_refits_when_any_class_sample_count_is_insufficient() -> None:
    result = evaluate_entry_meta_quality(
        {"take_entry": 31, "block_entry": 9},
        {"oos_samples": 10},
        min_samples=40,
        min_oos_samples=10,
        min_class_samples=10,
    )

    assert result == {"status": "refit", "reason": "insufficient_class_samples"}
