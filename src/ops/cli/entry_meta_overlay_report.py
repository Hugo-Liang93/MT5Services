from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from src.research.entry_meta.evaluation import (
    EntryMetaOverlayReport,
    build_entry_meta_overlay_report,
)


def build_report_from_paths(
    baseline_path: str | Path,
    shadow_path: str | Path | None,
    filter_paths: Iterable[str | Path],
    *,
    max_dd_worsen_ratio: float = 0.10,
    min_trades: int = 10,
) -> EntryMetaOverlayReport:
    baseline = _load_single_result_with_raw_trades(
        Path(baseline_path),
        role="baseline",
    )
    shadow = (
        _load_single_result_with_raw_trades(Path(shadow_path), role="shadow")
        if shadow_path
        else None
    )
    filters: list[dict[str, Any]] = []
    for path in filter_paths:
        filters.extend(_load_results_with_raw_trades(Path(path)))
    return build_entry_meta_overlay_report(
        baseline,
        shadow,
        filters,
        max_dd_worsen_ratio=max_dd_worsen_ratio,
        min_trades=min_trades,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Entry Meta overlay report")
    parser.add_argument("--baseline", required=True, help="Baseline backtest JSON path")
    parser.add_argument("--shadow", default=None, help="Shadow backtest JSON path")
    parser.add_argument(
        "--filters",
        nargs="+",
        required=True,
        help="Filter backtest JSON path(s)",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        help="Write report JSON to this path instead of stdout",
    )
    parser.add_argument(
        "--max-dd-worsen-ratio",
        type=float,
        default=0.10,
        help="Allowed max drawdown worsen ratio, default 0.10",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=10,
        help="Minimum filter trades before making accept/reject decisions",
    )
    args = parser.parse_args()

    payload = build_report_from_paths(
        args.baseline,
        args.shadow,
        args.filters,
        max_dd_worsen_ratio=args.max_dd_worsen_ratio,
        min_trades=args.min_trades,
    ).to_dict()
    text = f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    if args.json_output:
        output_path = Path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _load_single_result_with_raw_trades(path: Path, *, role: str) -> dict[str, Any]:
    results = _load_results_with_raw_trades(path)
    if len(results) != 1:
        raise ValueError(
            f"Entry Meta overlay {role} input must contain a single result; "
            f"got {len(results)} results in {path}"
        )
    return results[0]


def _load_results_with_raw_trades(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "results" in payload:
        return _from_backtest_runner_payload(payload)
    if isinstance(payload, dict) and "metrics" in payload:
        result = deepcopy(payload)
        if "raw_trades" not in result and "trades" in result:
            result["raw_trades"] = list(result["trades"] or [])
        return [result]
    raise ValueError(f"Unsupported Entry Meta overlay input JSON: {path}")


def _from_backtest_runner_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = payload.get("results") or []
    if not results:
        raise ValueError("Backtest runner JSON does not contain results")
    raw_results = payload.get("raw_results") or []
    normalized: list[dict[str, Any]] = []
    for index, raw_result in enumerate(results):
        result = deepcopy(raw_result)
        if index < len(raw_results):
            raw_payload = raw_results[index] or {}
            result["raw_trades"] = list(raw_payload.get("trades") or [])
        elif "trades" in result:
            result["raw_trades"] = list(result.get("trades") or [])
        else:
            result.setdefault("raw_trades", [])
        normalized.append(result)
    return normalized


if __name__ == "__main__":
    main()
