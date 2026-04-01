"""出场机制 A/B 对照实验。

并行跑 4 组配置，对比 Trailing TP / Indicator Exit 对 PnL 的影响。
"""
from __future__ import annotations

import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from datetime import datetime, timezone
from typing import Any, Dict

from src.backtesting.component_factory import (
    _load_signal_config_snapshot,
    build_backtest_components,
)
from src.backtesting.engine import BacktestEngine
from src.backtesting.models import BacktestConfig


def _run(tf: str, start: str, end: str, *, label: str,
         trailing_tp: bool, indicator_exit: bool,
         tp_activation: float = 1.2, tp_trail: float = 0.6) -> Dict[str, Any]:
    signal_config = _load_signal_config_snapshot()
    strategy_sessions: dict = {}
    for name, sess_list in getattr(signal_config, "strategy_sessions", {}).items():
        if sess_list:
            strategy_sessions[name] = list(sess_list) if not isinstance(sess_list, list) else sess_list
    strategy_timeframes: dict = {}
    for name, tf_list in getattr(signal_config, "strategy_timeframes", {}).items():
        if tf_list:
            strategy_timeframes[name] = list(tf_list) if not isinstance(tf_list, list) else tf_list

    config = BacktestConfig(
        symbol="XAUUSD",
        timeframe=tf,
        start_time=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        initial_balance=10000.0,
        min_confidence=0.55,
        warmup_bars=200,
        filters_enabled=True,
        filter_session_enabled=True,
        filter_allowed_sessions="london,newyork",
        strategy_sessions=strategy_sessions,
        strategy_timeframes=strategy_timeframes,
        trailing_tp_enabled=trailing_tp,
        trailing_tp_activation_atr=tp_activation,
        trailing_tp_trail_atr=tp_trail,
        indicator_exit_enabled=indicator_exit,
    )
    components = build_backtest_components()
    engine = BacktestEngine(
        config=config,
        data_loader=components["data_loader"],
        signal_module=components["signal_module"],
        indicator_pipeline=components["pipeline"],
        regime_detector=components["regime_detector"],
        voting_engine=components.get("voting_engine"),
        voting_group_engines=components.get("voting_group_engines"),
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
        "avg_win": m.avg_win,
        "avg_loss": m.avg_loss,
        "wl_ratio": round(m.avg_win / m.avg_loss, 3) if m.avg_loss > 0 else 0,
        "max_dd": m.max_drawdown,
        "exits": exit_counts,
    }


if __name__ == "__main__":
    tf = sys.argv[1] if len(sys.argv) > 1 else "M30"
    start = "2025-12-30"
    end = "2026-03-30"

    experiments = [
        {"label": "A: baseline (TP+IE on)",      "trailing_tp": True,  "indicator_exit": True},
        {"label": "B: no trailing TP",            "trailing_tp": False, "indicator_exit": True},
        {"label": "C: no indicator exit",         "trailing_tp": True,  "indicator_exit": False},
        {"label": "D: both off (pure SL/TP)",     "trailing_tp": False, "indicator_exit": False},
        {"label": "E: wider TP (act=2.0 trail=1.0)", "trailing_tp": True, "indicator_exit": True,
         "tp_activation": 2.0, "tp_trail": 1.0},
    ]

    print(f"\n{'='*80}")
    print(f" Exit Mechanism A/B Experiment — {tf} XAUUSD ({start} ~ {end})")
    print(f"{'='*80}\n")

    for exp in experiments:
        label = exp["label"]
        print(f"Running {label}...", end=" ", flush=True)
        r = _run(tf, start, end, **exp)
        exits_str = ", ".join(f"{k}:{v}" for k, v in sorted(r["exits"].items()))
        print(
            f"Done.\n"
            f"  {r['trades']} trades  WR:{r['wr']:.1%}  PnL:{r['pnl']:+.2f}  PF:{r['pf']:.3f}\n"
            f"  AvgWin:{r['avg_win']:.2f}  AvgLoss:{r['avg_loss']:.2f}  W/L:{r['wl_ratio']:.3f}  MaxDD:{r['max_dd']:.2%}\n"
            f"  Exits: {exits_str}\n"
        )
