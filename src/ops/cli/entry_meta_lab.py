from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.utils.timezone import parse_iso_to_utc


def main() -> None:
    parser = argparse.ArgumentParser(description="Entry Meta Research Lab")
    parser.add_argument("--environment", choices=["live", "demo"], required=True)
    parser.add_argument("--baseline", required=True, help="Baseline backtest JSON path")
    parser.add_argument("--tf", required=True, help="Timeframe, for example H1")
    parser.add_argument("--start", required=True, help="Start time/date ISO format")
    parser.add_argument("--end", required=True, help="End time/date ISO format")
    parser.add_argument("--backend", choices=["cpu", "gpu"], default="cpu")
    parser.add_argument("--artifact-dir", required=True, help="Artifact output directory")
    parser.add_argument("--json-output", default=None, help="Write lab result JSON")
    parser.add_argument("--symbol", default="XAUUSD", help="Symbol, default XAUUSD")
    parser.add_argument("--model-id", default=None, help="Optional stable model id")
    parser.add_argument(
        "--no-auto-backfill",
        action="store_true",
        help="Disable automatic MT5 backfill when requested OHLC coverage is missing",
    )
    args = parser.parse_args()

    from src.config.instance_context import set_current_environment
    from src.ops.cli._coverage import ensure_ohlc_data_coverage
    from src.backtesting.component_factory import build_research_data_deps
    from src.research.core.backends import resolve_backend
    from src.research.core.config import load_research_config
    from src.research.entry_meta.lab import EntryMetaLab

    set_current_environment(args.environment)
    backend = resolve_backend(args.backend)
    backend.assert_available()

    start_time = parse_iso_to_utc(args.start)
    end_time = parse_iso_to_utc(args.end)
    timeframe = args.tf.strip().upper()
    ensure_ohlc_data_coverage(
        symbol=args.symbol,
        timeframes=[timeframe],
        start=start_time,
        end=end_time,
        auto_backfill=not args.no_auto_backfill,
    )

    with build_research_data_deps() as deps:
        result = EntryMetaLab(
            config=load_research_config(),
            deps=deps,
        ).run(
            baseline_path=Path(args.baseline),
            symbol=args.symbol,
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
            backend_name=backend.name,
            artifact_dir=Path(args.artifact_dir),
            model_id=args.model_id,
        )

    payload: dict[str, Any] = result.to_dict()
    if args.json_output:
        output_path = Path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n",
            encoding="utf-8",
        )
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
