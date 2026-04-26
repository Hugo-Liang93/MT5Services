"""Walk-Forward 前推验证工具 — 多窗口训练-测试评估策略稳健性。

用法：
    python -m src.ops.cli.walkforward_runner --tf H1 --splits 5
    python -m src.ops.cli.walkforward_runner --tf M30 --start 2025-09-01 --end 2026-03-30 --splits 6
    python -m src.ops.cli.walkforward_runner --tf H1 --train-ratio 0.75 --metric sharpe_ratio

输出：
    - 每个窗口的 IS/OOS 指标对比
    - 汇总 OOS 指标
    - 过拟合比率 (IS_sharpe / OOS_sharpe)
    - 一致性率 (OOS 盈利窗口占比)
    - 参数稳定性评估
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

import warnings

warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from datetime import datetime, timezone
from src.utils.timezone import parse_iso_to_utc
from typing import Any, Dict, List, Optional


def _run_walkforward(
    tf: str,
    start: str,
    end: str,
    *,
    n_splits: int = 5,
    train_ratio: float = 0.70,
    anchored: bool = False,
    metric: str = "sharpe_ratio",
    strategy_names: Optional[List[str]] = None,
) -> Any:
    """执行 Walk-Forward 验证。"""
    from src.backtesting.component_factory import (
        _load_signal_config_snapshot,
        build_backtest_components,
    )
    from src.backtesting.config import get_backtest_defaults
    from src.backtesting.models import BacktestConfig
    from src.backtesting.optimization.walk_forward import (
        WalkForwardConfig,
        WalkForwardValidator,
    )

    _OPTIMIZER_ONLY = {"search_mode", "max_combinations", "sort_metric"}
    defaults = {k: v for k, v in get_backtest_defaults().items() if k not in _OPTIMIZER_ONLY}
    signal_config = _load_signal_config_snapshot()

    strategy_sessions: dict = {}
    for name, sess_list in getattr(signal_config, "strategy_sessions", {}).items():
        if sess_list:
            strategy_sessions[name] = (
                list(sess_list) if not isinstance(sess_list, list) else sess_list
            )
    strategy_timeframes: dict = {}
    for name, tf_list in getattr(signal_config, "strategy_timeframes", {}).items():
        if tf_list:
            strategy_timeframes[name] = (
                list(tf_list) if not isinstance(tf_list, list) else tf_list
            )

    merged = dict(defaults)
    merged["trailing_tp_enabled"] = bool(
        getattr(signal_config, "trailing_tp_enabled", True)
    )
    merged["trailing_tp_activation_atr"] = float(
        getattr(signal_config, "trailing_tp_activation_atr", 1.2)
    )
    merged["trailing_tp_trail_atr"] = float(
        getattr(signal_config, "trailing_tp_trail_atr", 0.6)
    )

    base_config = BacktestConfig.from_flat(
        symbol="XAUUSD",
        timeframe=tf,
        start_time=parse_iso_to_utc(start),
        end_time=parse_iso_to_utc(end),
        strategy_sessions=strategy_sessions,
        strategy_timeframes=strategy_timeframes,
        **merged,
    )

    wf_config = WalkForwardConfig(
        total_start_time=parse_iso_to_utc(start),
        total_end_time=parse_iso_to_utc(end),
        base_config=base_config,
        train_ratio=train_ratio,
        n_splits=n_splits,
        anchored=anchored,
        optimization_metric=metric,
    )

    components = build_backtest_components(strategy_names=strategy_names)

    def signal_module_factory(params: Dict[str, Any]):
        """重新构建 SignalModule 带参数覆盖。"""
        c = build_backtest_components(
            strategy_params=params,
            strategy_names=strategy_names,
        )
        return c["signal_module"]

    validator = WalkForwardValidator(
        config=wf_config,
        data_loader=components["data_loader"],
        signal_module_factory=signal_module_factory,
        indicator_pipeline=components["pipeline"],
        regime_detector=components["regime_detector"],
    )

    def progress_cb(split_idx: int, total: int, phase: str) -> None:
        sys.stderr.write(f"  [{split_idx}/{total}] {phase}\n")
        sys.stderr.flush()

    return validator.run(progress_callback=progress_cb)


def _render_result(result: Any, tf: str) -> str:
    """渲染 Walk-Forward 结果为文本。"""
    lines = [
        f"\n{'='*70}",
        f" Walk-Forward Validation — {tf} XAUUSD",
        f" Splits: {len(result.splits)}  "
        f"Train Ratio: {result.config.train_ratio:.0%}  "
        f"Mode: {'Anchored' if result.config.anchored else 'Rolling'}",
        f"{'='*70}",
    ]

    # 每个窗口详情
    lines.append(
        f"\n  {'Split':<6} {'Period':<25} {'IS Sharpe':>10} {'OOS Sharpe':>11} "
        f"{'OOS PnL':>10} {'OOS WR%':>8} {'OOS Trd':>8}"
    )
    lines.append("-" * 85)

    for i, split in enumerate(result.splits):
        is_m = split.in_sample_result.metrics
        oos_m = split.out_of_sample_result.metrics
        period = (
            f"{split.out_of_sample_result.config.start_time.strftime('%m-%d')}"
            f"~{split.out_of_sample_result.config.end_time.strftime('%m-%d')}"
        )
        oos_pnl_marker = "+" if oos_m.total_pnl > 0 else ""
        lines.append(
            f"  {i+1:<6} {period:<25} {is_m.sharpe_ratio:>10.3f} {oos_m.sharpe_ratio:>11.3f} "
            f"{oos_pnl_marker}{oos_m.total_pnl:>9.2f} {oos_m.win_rate:>7.1%} {oos_m.total_trades:>8}"
        )

    # 汇总
    agg = result.aggregate_metrics
    lines.append(f"\n--- Aggregate OOS Results ---")
    lines.append(
        f"  Trades:{agg.total_trades}  WR:{agg.win_rate:.1%}  PnL:{agg.total_pnl:+.2f}"
        f"  PF:{agg.profit_factor:.3f}  Sharpe:{agg.sharpe_ratio:.3f}"
        f"  Sortino:{agg.sortino_ratio:.3f}  MaxDD:{agg.max_drawdown:.2%}"
    )

    # 关键指标
    lines.append(f"\n--- Robustness Assessment ---")
    of_ratio = result.overfitting_ratio
    of_status = "OK" if of_ratio < 1.5 else ("WARNING" if of_ratio < 2.0 else "DANGER")
    lines.append(f"  Overfitting Ratio: {of_ratio:.2f}  [{of_status}]  (IS/OOS, <1.5=good, >2.0=overfitting)")

    cons_rate = result.consistency_rate
    cons_status = "OK" if cons_rate >= 0.60 else "WARNING"
    lines.append(f"  Consistency Rate:  {cons_rate:.1%}  [{cons_status}]  (OOS profitable windows, >=60%=good)")

    # 判定
    if of_ratio >= 2.0:
        lines.append(f"\n  *** OVERFITTING DETECTED — parameters are not robust ***")
        lines.append(f"  Strategy is 'trained to history' and likely to fail in production.")
    elif cons_rate < 0.50:
        lines.append(f"\n  *** INCONSISTENT — parameters unstable across time ***")
        lines.append(f"  Less than half of OOS windows are profitable.")
    elif of_ratio < 1.5 and cons_rate >= 0.60:
        lines.append(f"\n  Strategy appears ROBUST — safe to proceed to Demo Validation (deployment.status=demo_validation).")

    return "\n".join(lines)


def main() -> None:
    from src.config.instance_context import set_current_environment

    parser = argparse.ArgumentParser(description="Walk-Forward validation runner")
    parser.add_argument(
        "--environment",
        choices=["live", "demo"],
        required=True,
        help="显式指定 WF 使用哪个环境数据库",
    )
    parser.add_argument("--tf", required=True, help="Timeframe")
    parser.add_argument("--start", default="2025-09-01", help="Start date (longer for WF)")
    parser.add_argument("--end", default="2026-03-30", help="End date")
    parser.add_argument("--splits", type=int, default=5, help="Number of splits")
    parser.add_argument("--train-ratio", type=float, default=0.70, help="Train ratio")
    parser.add_argument("--anchored", action="store_true", help="Use anchored (expanding) windows")
    parser.add_argument("--metric", default="sharpe_ratio", help="Optimization metric")
    parser.add_argument(
        "--strategies",
        default=None,
        help="Only run these registered strategies, comma-separated",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        help="Write walk-forward summary to JSON file",
    )
    parser.add_argument(
        "--no-auto-backfill",
        action="store_true",
        help="Disable automatic MT5 backfill when requested OHLC coverage is missing",
    )
    args = parser.parse_args()
    set_current_environment(args.environment)

    tf = args.tf.strip().upper()
    strategy_names = (
        [name.strip() for name in args.strategies.split(",") if name.strip()]
        if args.strategies
        else None
    )
    from src.ops.cli._coverage import ensure_ohlc_data_coverage

    coverage = ensure_ohlc_data_coverage(
        symbol="XAUUSD",
        timeframes=[tf],
        start=parse_iso_to_utc(args.start),
        end=parse_iso_to_utc(args.end),
        auto_backfill=not args.no_auto_backfill,
    )
    sys.stderr.write(f"Walk-Forward: {tf} ({args.start} ~ {args.end}), {args.splits} splits\n")

    result = _run_walkforward(
        tf,
        args.start,
        args.end,
        n_splits=args.splits,
        train_ratio=args.train_ratio,
        anchored=args.anchored,
        metric=args.metric,
        strategy_names=strategy_names,
    )

    print(_render_result(result, tf))
    if args.json_output:
        import json

        output_path = Path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "symbol": "XAUUSD",
            "timeframe": tf,
            "strategies": strategy_names or [],
            "coverage": {name: info.to_dict() for name, info in coverage.items()},
            "summary": {
                "aggregate_metrics": {
                    "total_trades": result.aggregate_metrics.total_trades,
                    "win_rate": result.aggregate_metrics.win_rate,
                    "profit_factor": result.aggregate_metrics.profit_factor,
                    "sharpe_ratio": result.aggregate_metrics.sharpe_ratio,
                    "sortino_ratio": result.aggregate_metrics.sortino_ratio,
                    "max_drawdown": result.aggregate_metrics.max_drawdown,
                    "total_pnl": result.aggregate_metrics.total_pnl,
                },
                "overfitting_ratio": result.overfitting_ratio,
                "consistency_rate": result.consistency_rate,
                "n_splits": len(result.splits),
            },
            "splits": [
                {
                    "split_index": split.split_index,
                    "train_start": split.train_start.isoformat(),
                    "train_end": split.train_end.isoformat(),
                    "test_start": split.test_start.isoformat(),
                    "test_end": split.test_end.isoformat(),
                    "best_params": dict(split.best_params),
                    "in_sample": {
                        "total_trades": split.in_sample_result.metrics.total_trades,
                        "win_rate": split.in_sample_result.metrics.win_rate,
                        "profit_factor": split.in_sample_result.metrics.profit_factor,
                        "sharpe_ratio": split.in_sample_result.metrics.sharpe_ratio,
                        "max_drawdown": split.in_sample_result.metrics.max_drawdown,
                        "total_pnl": split.in_sample_result.metrics.total_pnl,
                    },
                    "out_of_sample": {
                        "total_trades": split.out_of_sample_result.metrics.total_trades,
                        "win_rate": split.out_of_sample_result.metrics.win_rate,
                        "profit_factor": split.out_of_sample_result.metrics.profit_factor,
                        "sharpe_ratio": split.out_of_sample_result.metrics.sharpe_ratio,
                        "max_drawdown": split.out_of_sample_result.metrics.max_drawdown,
                        "total_pnl": split.out_of_sample_result.metrics.total_pnl,
                    },
                }
                for split in result.splits
            ],
        }
        with output_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    sys.stderr.write("Done.\n")


if __name__ == "__main__":
    main()
