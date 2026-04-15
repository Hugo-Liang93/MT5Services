"""Chandelier aggression 扫描器 — 对单策略扫 α ∈ {0.30, 0.45, 0.55, 0.70, 0.85}。

B-5 工具：替代失效的 sltp_grid_search（路径 broken），专注 Chandelier aggression
单参数扫描，因为当前策略都用 aggression α 驱动 (trail / lock_ratio / breakeven_r 联动)。

用法：
    python -m src.ops.cli.aggression_scan \\
        --environment live --tf H1 \\
        --strategy structured_squeeze_breakout_buy \\
        --start 2024-04-01 --end 2026-03-30

原理：
    通过 monkey-patch 策略类的 `_aggression` 类变量跑多次回测，
    不修改源文件、不污染 signal.local.ini。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import warnings

warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from datetime import datetime, timezone
from typing import Any, Dict, List


_STRATEGY_CLASS_MAP = {
    # name → (module, class_name)
    "structured_squeeze_breakout_buy": (
        "src.signals.strategies.structured.squeeze_breakout_buy",
        "StructuredSqueezeBreakoutBuy",
    ),
    "structured_trend_continuation": (
        "src.signals.strategies.structured.trend_continuation",
        "StructuredTrendContinuation",
    ),
    "structured_sweep_reversal": (
        "src.signals.strategies.structured.sweep_reversal",
        "StructuredSweepReversal",
    ),
    "structured_breakout_follow": (
        "src.signals.strategies.structured.breakout_follow",
        "StructuredBreakoutFollow",
    ),
    "structured_range_reversion": (
        "src.signals.strategies.structured.range_reversion",
        "StructuredRangeReversion",
    ),
    "structured_trendline_touch": (
        "src.signals.strategies.structured.trendline_touch",
        "StructuredTrendlineTouch",
    ),
}


def _run_with_aggression(
    *,
    tf: str,
    start: str,
    end: str,
    strategy: str,
    alpha: float,
) -> Dict[str, Any]:
    module_name, class_name = _STRATEGY_CLASS_MAP[strategy]
    import importlib

    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    original_alpha = cls._aggression

    try:
        cls._aggression = alpha
        # 清 component_factory 缓存确保策略实例重建
        from src.backtesting import component_factory as cf

        for attr in ("_cached_signal_config", "_cached_components"):
            if hasattr(cf, attr):
                delattr(cf, attr) if not callable(getattr(cf, attr)) else None

        from src.ops.cli.backtest_runner import _run_single

        data = _run_single(
            tf,
            start,
            end,
            strategy_names=[strategy],
        )
        return data
    finally:
        cls._aggression = original_alpha


def _summarize(data: Dict[str, Any]) -> Dict[str, Any]:
    m = data.get("metrics", {}) or {}
    return {
        "total_trades": int(m.get("trades", 0) or 0),
        "win_rate_pct": float(m.get("win_rate", 0.0) or 0.0),  # 已是 %
        "pnl": float(m.get("pnl", 0.0) or 0.0),
        "profit_factor": float(m.get("pf", 0.0) or 0.0),
        "sharpe": float(m.get("sharpe", 0.0) or 0.0),
        "max_dd_pct": float(m.get("max_dd", 0.0) or 0.0),
        "avg_bars_held": float(m.get("avg_bars_held", 0.0) or 0.0),
    }


def main() -> None:
    from src.config.instance_context import set_current_environment

    parser = argparse.ArgumentParser(description="Chandelier aggression α scan")
    parser.add_argument("--environment", choices=["live", "demo"], required=True)
    parser.add_argument("--tf", required=True)
    parser.add_argument(
        "--strategy",
        required=True,
        help=f"Strategy name; must be one of: {', '.join(_STRATEGY_CLASS_MAP.keys())}",
    )
    parser.add_argument("--start", default="2024-04-01")
    parser.add_argument("--end", default="2026-03-30")
    parser.add_argument(
        "--alphas",
        default="0.30,0.45,0.55,0.70,0.85",
        help="α values to scan (comma-separated)",
    )
    args = parser.parse_args()

    if args.strategy not in _STRATEGY_CLASS_MAP:
        print(f"Unknown strategy: {args.strategy}")
        print(f"Known: {', '.join(_STRATEGY_CLASS_MAP.keys())}")
        sys.exit(1)

    set_current_environment(args.environment)

    alphas = [float(a.strip()) for a in args.alphas.split(",") if a.strip()]

    print(f"\n{'='*80}")
    print(
        f"Aggression α scan: {args.strategy} on {args.tf} "
        f"({args.start} ~ {args.end})"
    )
    print(f"Alphas: {alphas}")
    print(f"{'='*80}\n")

    header = f"{'α':>5} {'trades':>8} {'WR':>7} {'PnL':>9} {'PF':>6} {'Sharpe':>8} {'MaxDD%':>8}"
    print(header)
    print("-" * len(header))

    results: List[Dict[str, Any]] = []
    for alpha in alphas:
        try:
            data = _run_with_aggression(
                tf=args.tf,
                start=args.start,
                end=args.end,
                strategy=args.strategy,
                alpha=alpha,
            )
            summary = _summarize(data)
            summary["alpha"] = alpha
            results.append(summary)
            print(
                f"{alpha:>5.2f} "
                f"{summary['total_trades']:>8} "
                f"{summary['win_rate_pct']:>6.1f}% "
                f"{summary['pnl']:>+9.2f} "
                f"{summary['profit_factor']:>6.2f} "
                f"{summary['sharpe']:>+8.3f} "
                f"{summary['max_dd_pct']:>7.2f}%"
            )
        except Exception as exc:
            print(f"{alpha:>5.2f} FAILED: {exc}")

    if results:
        # 只考虑有足够样本（≥5 笔）的组合
        viable = [r for r in results if r["total_trades"] >= 5]
        if viable:
            best = max(viable, key=lambda r: r["profit_factor"])
            print()
            print(
                f"Best by PF (n ≥ 5): α={best['alpha']} "
                f"(PF={best['profit_factor']:.2f}, "
                f"WR={best['win_rate_pct']:.1f}%, "
                f"n={best['total_trades']}, "
                f"Sharpe={best['sharpe']:.3f}, "
                f"MaxDD={best['max_dd_pct']:.2f}%)"
            )
        else:
            print("\n无可用结果（所有 α 均产生 <5 笔交易）")


if __name__ == "__main__":
    main()
