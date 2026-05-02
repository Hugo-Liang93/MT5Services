from __future__ import annotations

from typing import Any


def evaluate_entry_meta_quality(
    label_summary: dict[str, int],
    metrics: dict[str, Any],
    *,
    min_samples: int = 40,
    min_oos_samples: int = 10,
    min_class_samples: int = 10,
) -> dict[str, str]:
    take_entry = int(label_summary.get("take_entry", 0))
    block_entry = int(label_summary.get("block_entry", 0))
    total_samples = take_entry + block_entry
    oos_samples = int(metrics.get("oos_samples", 0))

    if total_samples < int(min_samples):
        return {"status": "refit", "reason": "insufficient_samples"}
    if oos_samples < int(min_oos_samples):
        return {"status": "refit", "reason": "insufficient_oos_samples"}
    if take_entry < int(min_class_samples) or block_entry < int(min_class_samples):
        return {"status": "refit", "reason": "insufficient_class_samples"}
    return {"status": "accepted", "reason": "accepted"}
