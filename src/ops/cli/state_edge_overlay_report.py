"""State Edge overlay report builder.

Research-only CLI: compare baseline/shadow/filter backtest JSON payloads and
emit a structured accepted/refit/rejected report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))


def build_report_from_paths(
    *,
    baseline_path: str | Path,
    shadow_path: str | Path | None,
    filter_paths: list[str | Path],
    max_dd_worsen_ratio: float = 0.10,
    min_trades: int = 10,
) -> dict[str, Any]:
    from src.research.state_edge.evaluation import build_overlay_validation_report

    baseline = _load_single_result(baseline_path, role="baseline")
    shadow = (
        _load_single_result(shadow_path, role="shadow")
        if shadow_path is not None
        else None
    )
    filters: list[dict[str, Any]] = []
    for path in filter_paths:
        filters.extend(_load_results(path))
    if not filters:
        raise ValueError("at least one filter result is required")

    report = build_overlay_validation_report(
        baseline=baseline,
        shadow=shadow,
        filters=filters,
        max_dd_worsen_ratio=max_dd_worsen_ratio,
        min_trades=min_trades,
    )
    filter_file_paths = [str(Path(path)) for path in filter_paths]
    return {
        "status": report.status,
        "input_files": {
            "baseline": str(Path(baseline_path)),
            "shadow": str(Path(shadow_path)) if shadow_path is not None else None,
            "filters": filter_file_paths,
        },
        "overlay_report": report.to_dict(),
    }


def _load_single_result(path: str | Path, *, role: str) -> dict[str, Any]:
    results = _load_results(path)
    if len(results) != 1:
        raise ValueError(f"{role} file must contain exactly one result: {path}")
    return results[0]


def _load_results(path: str | Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if isinstance(payload.get("results"), list):
        raw_results = payload.get("raw_results")
        return [
            _attach_raw_result(dict(item), raw_results, index)
            for index, item in enumerate(payload["results"])
        ]
    if isinstance(payload.get("metrics"), dict):
        result = dict(payload)
        trades = payload.get("trades")
        if isinstance(trades, list):
            result["raw_trades"] = [
                dict(item) for item in trades if isinstance(item, dict)
            ]
        return [result]
    raise ValueError(f"backtest JSON has no results or metrics payload: {path}")


def _attach_raw_result(
    result: dict[str, Any],
    raw_results: Any,
    index: int,
) -> dict[str, Any]:
    if not isinstance(raw_results, list) or index >= len(raw_results):
        return result
    raw_result = raw_results[index]
    if not isinstance(raw_result, dict):
        return result
    raw_trades = raw_result.get("trades")
    if isinstance(raw_trades, list):
        result["raw_trades"] = [
            dict(item) for item in raw_trades if isinstance(item, dict)
        ]
    return result


def _read_json(path: str | Path) -> dict[str, Any]:
    raw_path = Path(path)
    with raw_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    tmp_path.replace(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="State Edge overlay report")
    parser.add_argument("--baseline", required=True, help="Baseline backtest JSON")
    parser.add_argument("--shadow", default=None, help="Shadow overlay backtest JSON")
    parser.add_argument(
        "--filters",
        nargs="+",
        required=True,
        help="Filter overlay backtest JSON file(s)",
    )
    parser.add_argument("--json-output", required=True)
    parser.add_argument("--max-dd-worsen-ratio", type=float, default=0.10)
    parser.add_argument("--min-trades", type=int, default=10)
    args = parser.parse_args()

    payload = build_report_from_paths(
        baseline_path=args.baseline,
        shadow_path=args.shadow,
        filter_paths=list(args.filters),
        max_dd_worsen_ratio=args.max_dd_worsen_ratio,
        min_trades=args.min_trades,
    )
    _write_json(args.json_output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
