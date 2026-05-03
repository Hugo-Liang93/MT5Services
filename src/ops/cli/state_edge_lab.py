"""State Edge Research Lab CLI.

离线训练 long/short/no-trade 市场状态概率模型。第一阶段仅输出 research artifact，
可被 backtest overlay 读取，不接入 demo/live runtime。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

from src.utils.timezone import parse_iso_to_utc


def _run_single(
    *,
    tf: str,
    start: str,
    end: str,
    backend: str,
    artifact_dir: str,
    model_kind: str,
    sequence_window: int | None,
    epochs: int | None,
    batch_size: int | None,
) -> dict[str, Any]:
    from src.backtesting.component_factory import build_research_data_deps
    from src.research.core import load_research_config
    from src.research.state_edge.lab import StateEdgeLab

    config = load_research_config()
    with build_research_data_deps() as deps:
        lab = StateEdgeLab(
            deps=deps,
            config=config,
            backend_name=backend,
            artifact_dir=artifact_dir,
            model_kind=model_kind,
            sequence_window=sequence_window,
            epochs=epochs,
            batch_size=batch_size,
        )
        result = lab.run(
            symbol="XAUUSD",
            timeframe=tf,
            start_time=parse_iso_to_utc(start),
            end_time=parse_iso_to_utc(end),
        )
    payload = result.to_dict()
    payload["start"] = start
    payload["end"] = end
    return payload


def main() -> None:
    from src.config.instance_context import set_current_environment

    parser = argparse.ArgumentParser(description="State Edge GPU Research Lab")
    parser.add_argument(
        "--environment",
        choices=["live", "demo"],
        required=True,
        help="显式指定读取哪个环境数据库",
    )
    parser.add_argument(
        "--tf",
        required=True,
        help="Timeframe(s), comma-separated: H4,H1,M30,M15",
    )
    parser.add_argument("--start", required=True, help="Start date ISO format")
    parser.add_argument("--end", required=True, help="End date ISO format")
    parser.add_argument(
        "--backend",
        choices=["cpu", "gpu"],
        default="cpu",
        help="Compute backend. gpu 模式缺 CUDA 栈会 fail-fast，不静默回退。",
    )
    parser.add_argument(
        "--artifact-dir",
        default="artifacts/state_edge",
        help="Directory for per-TF state edge artifacts",
    )
    parser.add_argument(
        "--model-kind",
        choices=["tabular", "hist_gradient_boosting", "sequence_tcn", "sequence_mlp"],
        default=None,
        help="State Edge model kind. sequence_mlp uses OHLC/feature windows.",
    )
    parser.add_argument("--sequence-window", type=int, default=None)
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument(
        "--json-output",
        default=None,
        help="Write structured lab report to JSON file",
    )
    parser.add_argument(
        "--no-auto-backfill",
        action="store_true",
        help="Disable automatic MT5 backfill when requested OHLC coverage is missing",
    )
    args = parser.parse_args()

    set_current_environment(args.environment)
    from src.research.state_edge.backends import resolve_backend

    backend = resolve_backend(args.backend)
    backend.assert_available()
    from src.research.core import load_research_config

    config = load_research_config()
    model_kind = args.model_kind or config.state_edge_model.model_kind
    backend_readiness = backend.readiness_benchmark(
        rows=config.gpu_backend.readiness_benchmark_rows
    )
    timeframes = [tf.strip().upper() for tf in args.tf.split(",") if tf.strip()]
    unsupported = [tf for tf in timeframes if tf not in {"H4", "H1", "M30", "M15"}]
    if unsupported:
        raise ValueError(f"Unsupported State Edge timeframe(s): {unsupported}")

    from src.ops.cli._coverage import ensure_ohlc_data_coverage

    coverage = ensure_ohlc_data_coverage(
        symbol="XAUUSD",
        timeframes=timeframes,
        start=parse_iso_to_utc(args.start),
        end=parse_iso_to_utc(args.end),
        auto_backfill=not args.no_auto_backfill,
    )

    results: list[dict[str, Any]] = []
    for tf in timeframes:
        sys.stderr.write(f"State Edge Lab running {tf} ({args.backend})...\n")
        sys.stderr.flush()
        results.append(
            _run_single(
                tf=tf,
                start=args.start,
                end=args.end,
                backend=args.backend,
                artifact_dir=args.artifact_dir,
                model_kind=model_kind,
                sequence_window=args.sequence_window,
                epochs=args.epochs,
                batch_size=args.batch_size,
            )
        )

    payload = {
        "symbol": "XAUUSD",
        "environment": args.environment,
        "backend": args.backend,
        "model_kind": model_kind,
        "backend_readiness": backend_readiness,
        "coverage": {tf: info.to_dict() for tf, info in coverage.items()},
        "results": results,
    }
    if args.json_output:
        output_path = Path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
