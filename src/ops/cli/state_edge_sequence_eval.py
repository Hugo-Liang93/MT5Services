"""State Edge sequence evaluation pipeline.

Research-only orchestration:
1. train State Edge sequence artifact
2. run baseline backtest
3. run shadow overlay
4. run filter threshold grid
5. emit accepted/refit/rejected report
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

from src.utils.timezone import parse_iso_to_utc


def _parse_threshold_grid(raw: str) -> list[float]:
    values: list[float] = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        value = float(item)
        if value < 0.0 or value > 1.0:
            raise ValueError(f"threshold must be within [0,1]: {value}")
        values.append(value)
    return values or [0.50]


def _parse_state_edge_directions(raw: str) -> list[str]:
    values = [part.strip().lower() for part in str(raw or "").split(",") if part.strip()]
    return values or ["buy", "sell"]


def _train_artifact(
    *,
    tf: str,
    start: str,
    end: str,
    backend: str,
    model_kind: str,
    sequence_window: int | None,
    epochs: int | None,
    batch_size: int | None,
    artifact_dir: str,
) -> dict[str, Any]:
    from src.ops.cli.state_edge_lab import _run_single

    return _run_single(
        tf=tf,
        start=start,
        end=end,
        backend=backend,
        artifact_dir=artifact_dir,
        model_kind=model_kind,
        sequence_window=sequence_window,
        epochs=epochs,
        batch_size=batch_size,
    )


def _run_backtest(
    *,
    tf: str,
    start: str,
    end: str,
    include_demo_validation: bool,
    artifact_path: str | None = None,
    mode: str = "shadow",
    threshold: float = 0.50,
    state_edge_directions: list[str] | None = None,
) -> dict[str, Any]:
    from src.ops.cli.backtest_runner import _run_single

    overrides: dict[str, Any] = {}
    if include_demo_validation:
        overrides["include_demo_validation"] = True
    result = _run_single(
        tf,
        start,
        end,
        config_overrides=overrides,
        state_edge_artifact=artifact_path,
        state_edge_mode=mode,
        state_edge_threshold=threshold,
        state_edge_directions=state_edge_directions,
    )
    result.pop("_raw_result", None)
    if artifact_path is not None:
        result["threshold"] = threshold
    return result


def _build_payload(
    *,
    environment: str,
    backend: str,
    model_kind: str,
    thresholds: list[float],
    state_edge_directions: list[str],
    backend_readiness: dict[str, Any],
    coverage: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "symbol": "XAUUSD",
        "environment": environment,
        "backend": backend,
        "model_kind": model_kind,
        "threshold_grid": thresholds,
        "state_edge_directions": state_edge_directions,
        "backend_readiness": backend_readiness,
        "coverage": coverage,
        "results": results,
    }


def _write_json_output(path: str | Path, payload: dict[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    tmp_path.replace(output_path)


def _evaluate_tf(
    *,
    tf: str,
    start: str,
    end: str,
    backend: str,
    model_kind: str,
    sequence_window: int | None,
    epochs: int | None,
    batch_size: int | None,
    artifact_dir: str,
    thresholds: list[float],
    include_demo_validation: bool,
    state_edge_directions: list[str] | None = None,
    min_oos_samples: int = 30,
    min_top_bucket_samples: int = 5,
    min_label_class_samples: int = 10,
    artifact_quality_only: bool = False,
    skip_artifact_quality_gate: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from src.research.state_edge.evaluation import build_threshold_grid_report
    from src.research.state_edge.quality import evaluate_artifact_quality

    result: dict[str, Any] = {
        "tf": tf,
        "stage": "starting",
        "filters": [],
    }

    def emit(stage: str) -> None:
        result["stage"] = stage
        if on_progress is not None:
            on_progress(dict(result))

    artifact_result = _train_artifact(
        tf=tf,
        start=start,
        end=end,
        backend=backend,
        model_kind=model_kind,
        sequence_window=sequence_window,
        epochs=epochs,
        batch_size=batch_size,
        artifact_dir=artifact_dir,
    )
    artifact_path = str(artifact_result["artifact_path"])
    result["artifact"] = artifact_result
    emit("artifact")

    if not skip_artifact_quality_gate:
        quality = evaluate_artifact_quality(
            artifact_result,
            min_oos_samples=min_oos_samples,
            min_top_bucket_samples=min_top_bucket_samples,
            min_label_class_samples=min_label_class_samples,
        )
        result["artifact_quality"] = quality.to_dict()
        result["status"] = quality.status
        emit("artifact_quality")
        if artifact_quality_only or not quality.should_run_backtest:
            return result

    baseline = _run_backtest(
        tf=tf,
        start=start,
        end=end,
        include_demo_validation=include_demo_validation,
    )
    result["baseline"] = baseline
    emit("baseline")

    shadow = _run_backtest(
        tf=tf,
        start=start,
        end=end,
        include_demo_validation=include_demo_validation,
        artifact_path=artifact_path,
        mode="shadow",
        threshold=thresholds[0],
        state_edge_directions=state_edge_directions,
    )
    result["shadow"] = shadow
    emit("shadow")

    filters: list[dict[str, Any]] = []
    for threshold in thresholds:
        filter_result = _run_backtest(
            tf=tf,
            start=start,
            end=end,
            include_demo_validation=include_demo_validation,
            artifact_path=artifact_path,
            mode="filter",
            threshold=threshold,
            state_edge_directions=state_edge_directions,
        )
        filters.append(filter_result)
        result["filters"] = filters
        emit(f"filter:{threshold:g}")

    report = build_threshold_grid_report(baseline=baseline, overlays=filters)
    result["threshold_report"] = report.to_dict()
    result["status"] = report.status
    emit("threshold_report")
    return result


def main() -> None:
    from src.config.instance_context import set_current_environment
    from src.ops.cli._coverage import ensure_ohlc_data_coverage
    from src.research.core import load_research_config
    from src.research.state_edge.backends import resolve_backend

    parser = argparse.ArgumentParser(description="State Edge sequence evaluation")
    parser.add_argument("--environment", choices=["live", "demo"], required=True)
    parser.add_argument("--tf", required=True, help="Comma-separated TF list")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--backend", choices=["cpu", "gpu"], default="gpu")
    parser.add_argument(
        "--model-kind",
        choices=["sequence_mlp", "sequence_tcn", "tabular", "hist_gradient_boosting"],
        default="sequence_mlp",
    )
    parser.add_argument("--sequence-window", type=int, default=None)
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument(
        "--threshold-grid",
        default="0.45,0.50,0.55,0.60,0.65,0.70",
    )
    parser.add_argument("--artifact-dir", default="artifacts/state_edge_sequence_eval")
    parser.add_argument("--json-output", required=True)
    parser.add_argument("--include-demo-validation", action="store_true")
    parser.add_argument(
        "--state-edge-directions",
        default="buy,sell",
        help="Comma-separated directions for filter mode: buy,sell,buy or sell",
    )
    parser.add_argument(
        "--artifact-quality-only",
        action="store_true",
        help="Train artifact and run quality gate only; do not run overlay backtests",
    )
    parser.add_argument(
        "--skip-artifact-quality-gate",
        action="store_true",
        help="Force overlay backtests even when artifact quality gate fails",
    )
    parser.add_argument("--no-auto-backfill", action="store_true")
    args = parser.parse_args()

    set_current_environment(args.environment)
    config = load_research_config()
    backend = resolve_backend(args.backend)
    backend.assert_available()
    backend_readiness = backend.readiness_benchmark(
        rows=config.gpu_backend.readiness_benchmark_rows
    )

    timeframes = [tf.strip().upper() for tf in args.tf.split(",") if tf.strip()]
    thresholds = _parse_threshold_grid(args.threshold_grid)
    state_edge_directions = _parse_state_edge_directions(args.state_edge_directions)
    coverage = ensure_ohlc_data_coverage(
        symbol="XAUUSD",
        timeframes=timeframes,
        start=parse_iso_to_utc(args.start),
        end=parse_iso_to_utc(args.end),
        auto_backfill=not args.no_auto_backfill,
    )

    coverage_payload = {tf: info.to_dict() for tf, info in coverage.items()}
    results: list[dict[str, Any]] = []

    def write_progress(partial: dict[str, Any]) -> None:
        payload = _build_payload(
            environment=args.environment,
            backend=args.backend,
            model_kind=args.model_kind,
            thresholds=thresholds,
            state_edge_directions=state_edge_directions,
            backend_readiness=backend_readiness,
            coverage=coverage_payload,
            results=results + [partial],
        )
        _write_json_output(args.json_output, payload)

    for tf in timeframes:
        sys.stderr.write(f"Evaluating State Edge sequence {tf}...\n")
        sys.stderr.flush()
        result = _evaluate_tf(
            tf=tf,
            start=args.start,
            end=args.end,
            backend=args.backend,
            model_kind=args.model_kind,
            sequence_window=args.sequence_window,
            epochs=args.epochs,
            batch_size=args.batch_size,
            artifact_dir=args.artifact_dir,
            thresholds=thresholds,
            include_demo_validation=args.include_demo_validation,
            state_edge_directions=state_edge_directions,
            min_oos_samples=config.state_edge_model.min_oos_samples,
            min_top_bucket_samples=config.state_edge_model.min_top_bucket_samples,
            min_label_class_samples=config.state_edge_model.min_label_class_samples,
            artifact_quality_only=args.artifact_quality_only,
            skip_artifact_quality_gate=args.skip_artifact_quality_gate,
            on_progress=write_progress,
        )
        results.append(result)

    payload = _build_payload(
        environment=args.environment,
        backend=args.backend,
        model_kind=args.model_kind,
        thresholds=thresholds,
        state_edge_directions=state_edge_directions,
        backend_readiness=backend_readiness,
        coverage=coverage_payload,
        results=results,
    )
    _write_json_output(args.json_output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
