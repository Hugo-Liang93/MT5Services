"""挖掘层 Walk-Forward 稳定性验证（CLI 适配层）。

业务语义（窗口切分 / 跨窗口规则聚合 / 稳定性筛选）已收口到
`src.research.orchestration.walk_forward`；本 CLI 只负责参数解析、
provider/child_tf 覆盖、coverage 检查、I/O（stdout 渲染 + JSON / 持久化）。

区别于 `walkforward_runner.py`（回测层 IS/OOS 评估）：本工具在挖掘阶段把
时间范围切成 N 个连续非重叠窗口，**每个窗口独立调用 mine_rules**，然后
统计每条规则（按 condition key）的跨窗口出现次数 + 平均 train/test hit rate。

用法：
    python -m src.ops.cli.mining_walk_forward \\
        --environment live --tf H1 \\
        --start 2024-04-01 --end 2026-03-30 --splits 6 \\
        --providers all --child-tf M5 \\
        --json-output data/research/wf.json --persist --experiment xxx

输出：
    - 每个窗口的 mined rules 数量
    - Stable Rules 清单（至少在 M 个窗口出现）+ 各窗口 hit rate
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

import warnings

warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from src.utils.timezone import parse_iso_to_utc

_ALL_PROVIDERS = [
    "temporal",
    "microstructure",
    "cross_tf",
    "regime_transition",
    "session_event",
    "intrabar",
]


def _mine_window(
    tf: str,
    window_start: datetime,
    window_end: datetime,
    *,
    child_tf: str = "",
    providers: Optional[List[str]] = None,
) -> Any:
    """在单个窗口跑挖掘，返回 MiningResult（含 mined_rules）。"""
    from dataclasses import replace as _replace

    from src.backtesting.component_factory import build_research_data_deps
    from src.research.core import load_research_config
    from src.research.orchestration import MiningRunner

    config = load_research_config()

    if providers is not None:
        if providers == ["all"]:
            enabled = _ALL_PROVIDERS
        else:
            enabled = providers
        fp = config.feature_providers
        overrides: Dict[str, bool] = {}
        for name in _ALL_PROVIDERS:
            overrides[f"{name}_enabled"] = name in enabled
        config = _replace(config, feature_providers=_replace(fp, **overrides))

    # §0di P2: walk-forward 按 split 多次调用本函数，旧实现无 cleanup 导致
    # 连接池泄漏线性累积；context manager 确保每个窗口退出前 writer / pipeline
    # 都关闭。
    with build_research_data_deps() as deps:
        runner = MiningRunner(config=config, deps=deps)
        return runner.run(
            symbol="XAUUSD",
            timeframe=tf,
            start_time=window_start,
            end_time=window_end,
            child_tf=child_tf,
        )


def _render(
    tf: str,
    windows: List[Tuple[datetime, datetime]],
    per_window_rule_counts: List[int],
    stable: List[Dict[str, Any]],
    min_consistency: float,
) -> str:
    n_splits = len(windows)
    min_appearances = max(2, math.ceil(n_splits * min_consistency))

    lines: List[str] = []
    lines.append("\n" + "=" * 80)
    lines.append(
        f" XAUUSD/{tf} Walk-Forward Mining ({n_splits} splits, "
        f"min_consistency={min_consistency:.0%} → min_appear={min_appearances})"
    )
    lines.append("=" * 80)

    lines.append("\n--- Per-Window Summary ---")
    for i, (ws, we) in enumerate(windows):
        lines.append(
            f"  Window {i + 1}: {ws.date()} ~ {we.date()} | "
            f"{per_window_rule_counts[i]} rules mined"
        )

    lines.append(
        f"\n--- Stable Rules ({len(stable)} appear in ≥ {min_appearances}/{n_splits} windows) ---"
    )
    if not stable:
        lines.append(
            "  无稳健规则。所有挖出的规则均为单窗口样本波动，不应进 Demo Validation。"
        )
        lines.append("")
        return "\n".join(lines)

    for item in stable:
        app_count = item["appearance_count"]
        train_avg = item["avg_train_hit_rate"]
        test_avg = item["avg_test_hit_rate"]
        barrier_avg = item["avg_barrier_top_hit_rate"]
        n_avg = item["avg_train_n_samples"]
        lines.append("")
        lines.append(
            f"  [{item['direction'].upper()}] appears {app_count}/{n_splits} "
            f"| train_avg={train_avg * 100:.1f}% "
            f"| test_avg={test_avg * 100:.1f}% "
            f"| barrier_avg={barrier_avg * 100:.1f}% "
            f"| avg_n={n_avg:.0f}"
        )
        lines.append(f"    Rule: {item['key']}")
        appear = item["appearances"]
        lines.append(f"    Window indices: {appear}")

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    from src.config.instance_context import set_current_environment
    from src.ops.cli._coverage import ensure_ohlc_data_coverage
    from src.ops.cli._persistence import add_persist_arguments
    from src.research.orchestration.walk_forward import run_mining_walk_forward

    parser = argparse.ArgumentParser(
        description="Mining-layer walk-forward stability validation"
    )
    parser.add_argument("--environment", choices=["live", "demo"], required=True)
    parser.add_argument("--tf", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--splits",
        type=int,
        default=6,
        help="窗口数（默认 6）；连续非重叠切分",
    )
    parser.add_argument(
        "--min-consistency",
        type=float,
        default=0.60,
        help="稳健阈值：规则至少在该比例的窗口出现（默认 0.60）",
    )
    parser.add_argument(
        "--providers",
        type=str,
        default=None,
        help=(
            "Feature Providers to enable (comma-separated). "
            "'all' for all providers. Default: from config."
        ),
    )
    parser.add_argument(
        "--child-tf",
        default="",
        help="Child TF for intrabar features (e.g. M5 for H1 parent). Empty = disabled.",
    )
    parser.add_argument(
        "--no-auto-backfill",
        action="store_true",
        help="Disable automatic MT5 backfill when requested OHLC coverage is missing",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        help="Write walk-forward payload to JSON file.",
    )
    add_persist_arguments(parser)
    args = parser.parse_args()
    set_current_environment(args.environment)

    providers_list: Optional[List[str]] = None
    if args.providers is not None:
        providers_list = [p.strip() for p in args.providers.split(",") if p.strip()]

    start_dt = parse_iso_to_utc(args.start)
    end_dt = parse_iso_to_utc(args.end)

    coverage = ensure_ohlc_data_coverage(
        symbol="XAUUSD",
        timeframes=[args.tf],
        start=start_dt,
        end=end_dt,
        auto_backfill=not args.no_auto_backfill,
    )

    print(
        f"Splitting {args.start} ~ {args.end} into {args.splits} windows "
        f"({(end_dt - start_dt).days / args.splits:.1f} days each)",
        flush=True,
    )

    raw_results: Dict[str, Any] = {}
    per_window_summaries: List[Tuple[datetime, datetime, int]] = []

    def mine_window_with_capture(window_start: datetime, window_end: datetime) -> Any:
        idx = len(per_window_summaries) + 1
        print(
            f"  Window {idx}/{args.splits}: {window_start.date()} ~ {window_end.date()} ...",
            flush=True,
        )
        try:
            result = _mine_window(
                args.tf,
                window_start,
                window_end,
                child_tf=args.child_tf,
                providers=providers_list,
            )
            rules = list(result.mined_rules or [])
            per_window_summaries.append((window_start, window_end, len(rules)))
            run_id = getattr(result, "run_id", None)
            if run_id:
                raw_results[f"{args.tf}-w{idx}-{run_id[:8]}"] = result
            return result
        except Exception as exc:  # noqa: BLE001 — CLI 边界，单窗口失败不阻断后续窗口
            print(f"    FAILED: {exc}", flush=True)

            class _EmptyResult:
                mined_rules: list = []

            per_window_summaries.append((window_start, window_end, 0))
            return _EmptyResult()

    wf_result = run_mining_walk_forward(
        timeframe=args.tf,
        start=start_dt,
        end=end_dt,
        splits=args.splits,
        min_consistency=args.min_consistency,
        mine_window=mine_window_with_capture,
    )

    windows_for_render = [(ws, we) for ws, we, _ in per_window_summaries]
    rule_counts = [count for _, _, count in per_window_summaries]
    print(
        _render(
            args.tf,
            windows_for_render,
            rule_counts,
            wf_result.stable,
            args.min_consistency,
        )
    )

    if args.persist and raw_results:
        from src.ops.cli._persistence import persist_mining_results

        saved = persist_mining_results(
            raw_results,
            environment=args.environment,
            experiment_id=args.experiment,
        )
        print(
            f"[persist] saved {saved}/{len(raw_results)} mining_runs to DB "
            f"(env={args.environment}, experiment={args.experiment or '-'})"
        )

    if args.json_output:
        import json

        output_path = Path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = wf_result.to_dict()
        payload["coverage"] = {tf: info.to_dict() for tf, info in coverage.items()}
        with output_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
