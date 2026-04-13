"""出场机制 A/B 对照实验。

并行跑多组配置，对比 Trailing TP 参数和出场机制对 PnL 的影响。

用法：
    python -m src.ops.cli.exit_experiment M30
    python -m src.ops.cli.exit_experiment H1 --start 2025-12-30 --end 2026-03-30
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from datetime import datetime, timezone
from typing import Any, Dict

from src.backtesting.component_factory import (
    _load_signal_config_snapshot,
    build_backtest_components,
)
from src.backtesting.config import get_backtest_defaults
from src.backtesting.engine import BacktestEngine
from src.backtesting.models import BacktestConfig


def _run(
    tf: str,
    start: str,
    end: str,
    *,
    label: str,
    trailing_tp: bool,
    tp_activation: float = 1.5,
    tp_trail: float = 0.8,
) -> Dict[str, Any]:
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

    overrides = {
        "trailing_tp_enabled": trailing_tp,
        "trailing_tp_activation_atr": tp_activation,
        "trailing_tp_trail_atr": tp_trail,
    }
    merged = dict(defaults)
    merged.update(overrides)

    config = BacktestConfig.from_flat(
        symbol="XAUUSD",
        timeframe=tf,
        start_time=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        strategy_sessions=strategy_sessions,
        strategy_timeframes=strategy_timeframes,
        **merged,
    )
    components = build_backtest_components()
    engine = BacktestEngine(
        config=config,
        data_loader=components["data_loader"],
        signal_module=components["signal_module"],
        indicator_pipeline=components["pipeline"],
        regime_detector=components["regime_detector"],
        performance_tracker=components.get("performance_tracker"),
    )
    result = engine.run()
    m = result.metrics
    trades = result.trades

    exit_counts: Dict[str, int] = {}
    for t in trades:
        reason = getattr(t, "close_reason", getattr(t, "exit_reason", "unknown"))
        exit_counts[reason] = exit_counts.get(reason, 0) + 1

    return {
        "label": label,
        "trades": m.total_trades,
        "wr": m.win_rate,
        "pnl": m.total_pnl,
        "pf": m.profit_factor,
        "sharpe": m.sharpe_ratio,
        "avg_win": m.avg_win,
        "avg_loss": m.avg_loss,
        "wl_ratio": round(m.avg_win / m.avg_loss, 3) if m.avg_loss > 0 else 0,
        "max_dd": m.max_drawdown,
        "exits": exit_counts,
    }


def main() -> None:
    from src.config.instance_context import set_current_environment

    parser = argparse.ArgumentParser(description="Exit mechanism A/B experiment")
    parser.add_argument(
        "--environment",
        choices=["live", "demo"],
        required=True,
        help="显式指定实验使用哪个环境数据库",
    )
    parser.add_argument("tf", nargs="?", default="M30", help="Timeframe (default M30)")
    parser.add_argument("--start", default="2025-12-30")
    parser.add_argument("--end", default="2026-03-30")
    args = parser.parse_args()
    set_current_environment(args.environment)

    tf = args.tf.upper()
    start = args.start
    end = args.end

    experiments = [
        {"label": "A: trailing TP on (baseline)", "trailing_tp": True},
        {"label": "B: trailing TP off (pure SL/TP)", "trailing_tp": False},
        {
            "label": "C: wider TP (act=2.0 trail=1.0)",
            "trailing_tp": True,
            "tp_activation": 2.0,
            "tp_trail": 1.0,
        },
        {
            "label": "D: tighter TP (act=1.0 trail=0.5)",
            "trailing_tp": True,
            "tp_activation": 1.0,
            "tp_trail": 0.5,
        },
        {
            "label": "E: aggressive trail (act=1.2 trail=0.4)",
            "trailing_tp": True,
            "tp_activation": 1.2,
            "tp_trail": 0.4,
        },
    ]

    print(f"\n{'='*80}")
    print(f" Exit Mechanism A/B Experiment — {tf} XAUUSD ({start} ~ {end})")
    print(f"{'='*80}")
    print(
        f"  {'Label':<40} {'Trd':>4} {'WR%':>6} {'PnL':>10} {'PF':>7} "
        f"{'Sharpe':>7} {'AvgW':>8} {'AvgL':>8} {'W/L':>6} {'DD%':>6}"
    )
    print("-" * 110)

    for exp in experiments:
        label = exp["label"]
        sys.stderr.write(f"Running {label}...\n")
        sys.stderr.flush()
        r = _run(tf, start, end, **exp)
        exits_str = ", ".join(f"{k}:{v}" for k, v in sorted(r["exits"].items()))
        print(
            f"  {r['label']:<40} {r['trades']:>4} {r['wr']:>5.1%} {r['pnl']:>+10.2f} {r['pf']:>7.3f} "
            f"{r['sharpe']:>7.3f} {r['avg_win']:>8.2f} {r['avg_loss']:>8.2f} {r['wl_ratio']:>6.3f} {r['max_dd']:>5.2%}"
        )
        print(f"    Exits: {exits_str}")

    print()


if __name__ == "__main__":
    main()
