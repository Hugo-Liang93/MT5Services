"""回测运行器 — 本地直接执行回测并输出结构化摘要。

用法：
    python tools/backtest_runner.py --tf H1 --start 2025-12-30 --end 2026-03-30
    python tools/backtest_runner.py --tf H1,M30,M15 --start 2025-12-30 --end 2026-03-30
    python tools/backtest_runner.py --tf M15 --template regime_detail
    python tools/backtest_runner.py --tf H1 --template custom --custom-template '...'

摘要模板：
    default       — 总体指标 + 退出原因 + 策略排名 + regime 分布
    strategy_only — 仅策略排名
    regime_detail — regime × strategy 交叉分析
    exit_analysis — 退出原因深度分析（含 SL/TP 分布）
    minimal       — 仅核心指标（1 行）
    custom        — 通过 --custom-template 传入 Python format string

所有日志静默，仅输出摘要到 stdout。供 Claude Code 通过单次 Bash 调用获取结果。
"""
from __future__ import annotations

import argparse
import os
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings

warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _run_single(
    tf: str,
    start: str,
    end: str,
    *,
    min_confidence: float = 0.55,
    warmup_bars: int = 200,
    filters_enabled: bool = True,
    sessions: str = "london,newyork",
) -> dict:
    """执行单个 TF 回测，返回结构化结果 dict。"""
    from src.backtesting.component_factory import build_backtest_components
    from src.backtesting.engine import BacktestEngine
    from src.backtesting.models import BacktestConfig

    config = BacktestConfig(
        symbol="XAUUSD",
        timeframe=tf,
        start_time=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        initial_balance=10000.0,
        min_confidence=min_confidence,
        warmup_bars=warmup_bars,
        filters_enabled=filters_enabled,
        filter_session_enabled=filters_enabled,
        filter_allowed_sessions=sessions,
        # 与实盘 signal.ini 对齐
        trailing_tp_enabled=True,
        trailing_tp_activation_atr=1.2,
        trailing_tp_trail_atr=0.6,
        indicator_exit_enabled=True,
    )
    components = build_backtest_components()
    engine = BacktestEngine(
        config=config,
        data_loader=components["data_loader"],
        signal_module=components["signal_module"],
        indicator_pipeline=components["pipeline"],
        regime_detector=components["regime_detector"],
        voting_engine=components.get("voting_engine"),
    )
    result = engine.run()
    m = result.metrics
    trades = result.trades

    # 构建结构化摘要
    exit_stats: Dict[str, Dict[str, Any]] = {}
    strat_stats: Dict[str, Dict[str, Any]] = {}
    regime_stats: Dict[str, Dict[str, Any]] = {}
    regime_strategy: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for t in trades:
        pnl = getattr(t, "pnl", 0) or 0
        strategy = getattr(t, "strategy", "?")
        regime = getattr(t, "regime", "unknown")
        exit_reason = getattr(t, "close_reason", getattr(t, "exit_reason", "unknown"))
        bars_held = getattr(t, "bars_held", 0) or 0

        # exit
        if exit_reason not in exit_stats:
            exit_stats[exit_reason] = {"n": 0, "pnl": 0.0, "bars_held_sum": 0}
        exit_stats[exit_reason]["n"] += 1
        exit_stats[exit_reason]["pnl"] += pnl
        exit_stats[exit_reason]["bars_held_sum"] += bars_held

        # strategy
        if strategy not in strat_stats:
            strat_stats[strategy] = {"n": 0, "w": 0, "pnl": 0.0}
        strat_stats[strategy]["n"] += 1
        strat_stats[strategy]["pnl"] += pnl
        if pnl > 0:
            strat_stats[strategy]["w"] += 1

        # regime
        if regime not in regime_stats:
            regime_stats[regime] = {"n": 0, "w": 0, "pnl": 0.0}
        regime_stats[regime]["n"] += 1
        regime_stats[regime]["pnl"] += pnl
        if pnl > 0:
            regime_stats[regime]["w"] += 1

        # regime × strategy
        if regime not in regime_strategy:
            regime_strategy[regime] = {}
        if strategy not in regime_strategy[regime]:
            regime_strategy[regime][strategy] = {"n": 0, "w": 0, "pnl": 0.0}
        regime_strategy[regime][strategy]["n"] += 1
        regime_strategy[regime][strategy]["pnl"] += pnl
        if pnl > 0:
            regime_strategy[regime][strategy]["w"] += 1

    return {
        "tf": tf,
        "start": start,
        "end": end,
        "metrics": {
            "trades": m.total_trades,
            "win_rate": round(m.win_rate * 100, 1),
            "pnl": round(m.total_pnl, 2),
            "pf": round(m.profit_factor, 3),
            "sharpe": round(m.sharpe_ratio, 3),
            "max_dd": round(m.max_drawdown * 100, 2),
            "avg_win": round(m.avg_win, 2),
            "avg_loss": round(m.avg_loss, 2),
            "expectancy": round(m.expectancy, 2),
            "win_loss_ratio": round(m.avg_win / m.avg_loss, 2) if m.avg_loss > 0 else 0,
        },
        "exit_stats": exit_stats,
        "strategy_stats": strat_stats,
        "regime_stats": regime_stats,
        "regime_strategy": regime_strategy,
    }


# ── 摘要模板渲染 ────────────────────────────────────────────────────


def _render_default(data: dict) -> str:
    m = data["metrics"]
    lines = [
        f"\n{'='*60}",
        f" {data['tf']} XAUUSD ({data['start']} ~ {data['end']})",
        f"{'='*60}",
        f"Trades:{m['trades']}  WR:{m['win_rate']}%  PnL:{m['pnl']}  PF:{m['pf']}  Sharpe:{m['sharpe']}",
        f"MaxDD:{m['max_dd']}%  AvgWin:{m['avg_win']}  AvgLoss:{m['avg_loss']}  W/L Ratio:{m['win_loss_ratio']}  Exp:{m['expectancy']}",
        "--- Exit Reasons ---",
    ]
    for reason, d in sorted(data["exit_stats"].items(), key=lambda x: x[1]["n"], reverse=True):
        avg_bars = d["bars_held_sum"] / d["n"] if d["n"] > 0 else 0
        lines.append(f"  {reason:<20} {d['n']:>4}  PnL:{d['pnl']:>+10.2f}  AvgBars:{avg_bars:.1f}")
    lines.append("--- Per-Strategy (by PnL) ---")
    lines.append(f"  {'Strategy':<28} {'Trd':>4} {'Win':>4} {'WR%':>6} {'PnL':>10}")
    for name, s in sorted(data["strategy_stats"].items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = s["w"] / s["n"] * 100 if s["n"] > 0 else 0
        lines.append(f"  {name:<28} {s['n']:>4} {s['w']:>4} {wr:>5.1f}% {s['pnl']:>+10.2f}")
    lines.append("--- Regime ---")
    for r, d in sorted(data["regime_stats"].items(), key=lambda x: x[1]["n"], reverse=True):
        wr = d["w"] / d["n"] * 100 if d["n"] > 0 else 0
        lines.append(f"  {r:<15} {d['n']:>4}  WR:{wr:>5.1f}%  PnL:{d['pnl']:>+10.2f}")
    return "\n".join(lines)


def _render_minimal(data: dict) -> str:
    m = data["metrics"]
    return (
        f"{data['tf']}: {m['trades']}trades WR:{m['win_rate']}% PnL:{m['pnl']} "
        f"PF:{m['pf']} Sharpe:{m['sharpe']} DD:{m['max_dd']}% W/L:{m['win_loss_ratio']}"
    )


def _render_strategy_only(data: dict) -> str:
    m = data["metrics"]
    lines = [
        f"\n{data['tf']} — {m['trades']} trades, WR:{m['win_rate']}%, PnL:{m['pnl']}, PF:{m['pf']}",
        f"  {'Strategy':<28} {'Trd':>4} {'Win':>4} {'WR%':>6} {'PnL':>10}",
    ]
    for name, s in sorted(data["strategy_stats"].items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = s["w"] / s["n"] * 100 if s["n"] > 0 else 0
        lines.append(f"  {name:<28} {s['n']:>4} {s['w']:>4} {wr:>5.1f}% {s['pnl']:>+10.2f}")
    return "\n".join(lines)


def _render_regime_detail(data: dict) -> str:
    m = data["metrics"]
    lines = [
        f"\n{data['tf']} — {m['trades']} trades, PnL:{m['pnl']}, PF:{m['pf']}",
    ]
    for regime, strategies in sorted(data["regime_strategy"].items()):
        r_data = data["regime_stats"].get(regime, {})
        r_wr = r_data["w"] / r_data["n"] * 100 if r_data.get("n", 0) > 0 else 0
        lines.append(f"  [{regime}] {r_data.get('n',0)} trades  WR:{r_wr:.1f}%  PnL:{r_data.get('pnl',0):>+.2f}")
        for sname, sd in sorted(strategies.items(), key=lambda x: x[1]["pnl"], reverse=True):
            swr = sd["w"] / sd["n"] * 100 if sd["n"] > 0 else 0
            lines.append(f"    {sname:<26} {sd['n']:>3} {swr:>5.1f}% {sd['pnl']:>+10.2f}")
    return "\n".join(lines)


def _render_exit_analysis(data: dict) -> str:
    m = data["metrics"]
    lines = [
        f"\n{data['tf']} — {m['trades']} trades, PnL:{m['pnl']}",
        f"  AvgWin:{m['avg_win']}  AvgLoss:{m['avg_loss']}  W/L Ratio:{m['win_loss_ratio']}",
        "--- Exit Reason Detail ---",
    ]
    total = sum(d["n"] for d in data["exit_stats"].values())
    for reason, d in sorted(data["exit_stats"].items(), key=lambda x: x[1]["n"], reverse=True):
        pct = d["n"] / total * 100 if total > 0 else 0
        avg_bars = d["bars_held_sum"] / d["n"] if d["n"] > 0 else 0
        avg_pnl = d["pnl"] / d["n"] if d["n"] > 0 else 0
        lines.append(
            f"  {reason:<20} {d['n']:>4} ({pct:>5.1f}%)  "
            f"TotalPnL:{d['pnl']:>+10.2f}  AvgPnL:{avg_pnl:>+8.2f}  AvgBars:{avg_bars:.1f}"
        )
    return "\n".join(lines)


TEMPLATES = {
    "default": _render_default,
    "minimal": _render_minimal,
    "strategy_only": _render_strategy_only,
    "regime_detail": _render_regime_detail,
    "exit_analysis": _render_exit_analysis,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5Services backtest runner")
    parser.add_argument("--tf", required=True, help="Timeframe(s), comma-separated: H1,M30,M15")
    parser.add_argument("--start", default="2025-12-30", help="Start date ISO format")
    parser.add_argument("--end", default="2026-03-30", help="End date ISO format")
    parser.add_argument("--template", default="default", choices=list(TEMPLATES.keys()) + ["custom"])
    parser.add_argument("--custom-template", default=None, help="Custom Python format string")
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--no-filters", action="store_true")
    parser.add_argument("--sessions", default="london,newyork")
    args = parser.parse_args()

    timeframes = [t.strip().upper() for t in args.tf.split(",")]
    render_fn = TEMPLATES.get(args.template, _render_default)

    for tf in timeframes:
        sys.stderr.write(f"Running {tf}...\n")
        sys.stderr.flush()
        data = _run_single(
            tf,
            args.start,
            args.end,
            min_confidence=args.min_confidence,
            warmup_bars=args.warmup,
            filters_enabled=not args.no_filters,
            sessions=args.sessions,
        )
        if args.template == "custom" and args.custom_template:
            print(args.custom_template.format(**data["metrics"], tf=data["tf"]))
        else:
            print(render_fn(data))

    sys.stderr.write("Done.\n")


if __name__ == "__main__":
    main()
