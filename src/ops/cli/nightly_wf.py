"""Nightly WF CLI：每晚跑全策略 × 多 TF 回测 + 同比回归检测。

使用：
    # 初次全量（无历史基线）
    python -m src.ops.cli.nightly_wf --environment demo \\
        --tf M15,M30,H1 --start 2023-01-01 --end 2026-03-30 --workers 3

    # 日常（对比前一次结果自动检测回归）
    python -m src.ops.cli.nightly_wf --environment demo \\
        --tf M15,M30,H1 --start 2023-01-01 --end 2026-03-30 --workers 3 \\
        --previous data/nightly_wf/latest.json

退出码：
    0 = 所有组合正常运行 + 无显著回归
    1 = 检测到回归 或 部分失败
    2 = 基础设施错误
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _load_previous_report(path: str):
    """加载前一次 nightly 报告 JSON。不存在返回 None。"""
    from src.research.nightly.contracts import (
        NightlyWFConfig,
        NightlyWFReport,
        StrategyMetrics,
    )

    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 反序列化为契约（只重建 metrics 和 config，其他字段对对比足够）
    cfg = NightlyWFConfig(
        symbol=data["symbol"],
        timeframes=tuple(data["timeframes"]),
        start_date=data["start_date"],
        end_date=data["end_date"],
        strategies=tuple(data["strategies"]),
        sharpe_regression_threshold=0.20,
        dd_regression_threshold=0.15,
        workers=int(data.get("workers", 1)),
        output_dir="",
    )
    metrics = tuple(
        StrategyMetrics(
            strategy=m["strategy"],
            timeframe=m["timeframe"],
            trades=int(m["trades"]),
            win_rate=float(m["win_rate"]),
            pnl=float(m["pnl"]),
            profit_factor=float(m["profit_factor"]),
            sharpe=float(m["sharpe"]),
            sortino=float(m["sortino"]),
            max_dd=float(m["max_dd"]),
            expectancy=float(m["expectancy"]),
            avg_bars_held=float(m["avg_bars_held"]),
        )
        for m in data["metrics"]
    )
    return NightlyWFReport(
        generated_at=datetime.fromisoformat(data["generated_at"]),
        config=cfg,
        metrics=metrics,
        runtime_seconds=float(data.get("runtime_seconds", 0)),
    )


def _resolve_default_strategies() -> List[str]:
    """从 signal_config 自动解析所有已注册且 live-executable 的策略名。

    契约：live deployment 意味着"至少应该被 WF 跟踪"。
    """
    from src.backtesting.component_factory import _load_signal_config_snapshot

    signal_config = _load_signal_config_snapshot()
    names: List[str] = []
    for strategy_name, deployment in signal_config.strategy_deployments.items():
        if deployment.allows_runtime_evaluation():
            names.append(strategy_name)
    if not names:
        raise ValueError(
            "No strategies found in signal_config.strategy_deployments "
            "with runtime_evaluation enabled"
        )
    return sorted(names)


def _format_report(report) -> str:
    """控制台打印格式。"""
    lines: List[str] = []
    lines.append(f"\n{'=' * 72}")
    lines.append(f"Nightly WF Report — {report.generated_at.isoformat()}")
    lines.append(f"{'=' * 72}")
    lines.append(
        f"Symbol: {report.config.symbol}  "
        f"TFs: {','.join(report.config.timeframes)}  "
        f"Date: {report.config.start_date} → {report.config.end_date}  "
        f"Runtime: {report.runtime_seconds:.1f}s"
    )
    lines.append("")

    # Metrics 表
    lines.append(f"{'Strategy':<28} {'TF':<5} {'Trades':>6} {'Sharpe':>8} "
                 f"{'MaxDD%':>8} {'PnL':>10} {'WinRate':>8}")
    lines.append("-" * 72)
    for m in sorted(report.metrics, key=lambda x: (-x.sharpe, x.strategy)):
        lines.append(
            f"{m.strategy:<28} {m.timeframe:<5} {m.trades:>6} "
            f"{m.sharpe:>8.2f} {m.max_dd * 100:>7.2f}% "
            f"{m.pnl:>10.2f} {m.win_rate * 100:>7.1f}%"
        )

    # Regression
    regressions = [r for r in report.regressions if r.severity == "regression"]
    improvements = [r for r in report.regressions if r.severity == "improvement"]
    if report.regressions:
        lines.append("")
        lines.append(f"Regressions (relative to previous run): "
                     f"{len(regressions)} bad, {len(improvements)} good")
        for r in regressions:
            lines.append(
                f"  [REGRESSION] {r.strategy}/{r.timeframe} {r.metric}: "
                f"{r.previous:.3f} → {r.current:.3f} ({r.change_ratio * 100:+.1f}%)"
            )
        for r in improvements:
            lines.append(
                f"  [improved]   {r.strategy}/{r.timeframe} {r.metric}: "
                f"{r.previous:.3f} → {r.current:.3f} ({r.change_ratio * 100:+.1f}%)"
            )

    if report.failed_runs:
        lines.append("")
        lines.append(f"Failed runs ({len(report.failed_runs)}):")
        for f in report.failed_runs:
            lines.append(f"  - {f}")

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nightly WF: strategies × TFs backtest + regression detection"
    )
    parser.add_argument(
        "--environment", choices=["live", "demo"], required=True,
    )
    parser.add_argument(
        "--tf", required=True, help="Timeframe(s), comma-separated: M15,M30,H1",
    )
    parser.add_argument("--start", required=True, help="ISO date")
    parser.add_argument("--end", required=True, help="ISO date")
    parser.add_argument(
        "--strategies", default="",
        help="Comma-separated strategies. Default: all live-executable.",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--output-dir", default="data/nightly_wf",
        help="Directory to persist reports",
    )
    parser.add_argument(
        "--previous", default="",
        help="Path to previous report JSON for regression comparison",
    )
    parser.add_argument(
        "--sharpe-regression-threshold", type=float, default=0.20,
        help="Relative Sharpe drop threshold (default 0.20 = 20%)",
    )
    parser.add_argument(
        "--dd-regression-threshold", type=float, default=0.15,
        help="Relative MaxDD worsening threshold (default 0.15 = 15%)",
    )
    args = parser.parse_args()

    from src.config.instance_context import set_current_environment
    set_current_environment(args.environment)

    from src.research.nightly import (
        NightlyWFConfig,
        NightlyWFReport,
        compare_reports,
        run_nightly_wf,
    )

    tfs = tuple(t.strip().upper() for t in args.tf.split(",") if t.strip())
    if args.strategies:
        strategies = tuple(s.strip() for s in args.strategies.split(",") if s.strip())
    else:
        strategies = tuple(_resolve_default_strategies())

    cfg = NightlyWFConfig(
        symbol="XAUUSD",
        timeframes=tfs,
        start_date=args.start,
        end_date=args.end,
        strategies=strategies,
        sharpe_regression_threshold=args.sharpe_regression_threshold,
        dd_regression_threshold=args.dd_regression_threshold,
        workers=args.workers,
        output_dir=args.output_dir,
    )

    # 跑回测
    report = run_nightly_wf(cfg)

    # 加载前次报告做回归检测
    previous_path: Optional[str] = args.previous or None
    if previous_path:
        prev = _load_previous_report(previous_path)
        if prev is not None:
            report = NightlyWFReport(
                generated_at=report.generated_at,
                config=report.config,
                metrics=report.metrics,
                regressions=compare_reports(
                    current=report, previous=prev, config=cfg,
                ),
                previous_report_path=previous_path,
                runtime_seconds=report.runtime_seconds,
                failed_runs=report.failed_runs,
            )

    # 落盘
    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = report.generated_at.strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(args.output_dir, f"{timestamp}.json")
    latest_path = os.path.join(args.output_dir, "latest.json")
    payload = report.to_dict()
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(_format_report(report))
    print(f"Report saved: {report_path}")
    print(f"Latest symlink: {latest_path}")

    # Exit code
    if report.failed_runs:
        return 1
    if report.has_regression():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
