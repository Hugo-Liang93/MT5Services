"""Aggression 网格搜索 — 搜索每个策略 × TF 的最优 Chandelier Exit α。

用法：
    python -m src.ops.cli.aggression_search --tf H1
    python -m src.ops.cli.aggression_search --tf M30,H1
    python -m src.ops.cli.aggression_search --tf H1 --strategies structured_trend_continuation
    python -m src.ops.cli.aggression_search --tf H1 --step 0.05

对每个 (策略, TF) 组合，扫描 aggression 从 0.05 到 0.95，
输出各值的 PnL / WR / PF / 交易数，标注最优值。

每个 combo 独立运行完整回测（~17s/次），简单可靠。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import warnings

warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── 默认 aggression 值 ──────────────────────────────────────────────

_DEFAULTS: Dict[str, float] = {
    "structured_trend_continuation": 0.80,
    "structured_trend_h4": 0.80,
    "structured_breakout_follow": 0.85,
    "structured_trendline_touch": 0.70,
    "structured_session_breakout": 0.60,
    "structured_sweep_reversal": 0.20,
    "structured_range_reversion": 0.15,
    "structured_lowbar_entry": 0.15,
}


def _get_active_strategies(tf: str) -> List[str]:
    """返回在指定 TF 上活跃的结构化策略名称列表。"""
    from src.backtesting.component_factory import _load_signal_config_snapshot

    signal_config = _load_signal_config_snapshot()
    active = []
    for name, tf_list in getattr(signal_config, "strategy_timeframes", {}).items():
        if not name.startswith("structured_"):
            continue
        tfs = list(tf_list) if not isinstance(tf_list, list) else tf_list
        if tf.upper() in [t.upper() for t in tfs]:
            active.append(name)
    return sorted(active)


def _run_single(
    tf: str,
    start: str,
    end: str,
    strategy_params_per_tf: Optional[Dict[str, Dict[str, Any]]] = None,
) -> dict:
    """执行单个回测，返回交易列表和总体指标。

    使用 strategy_params_per_tf 确保覆盖优先级正确（per-TF > global > default）。
    """
    from src.backtesting.component_factory import (
        _load_signal_config_snapshot,
        build_backtest_components,
    )
    from src.backtesting.config import get_backtest_defaults
    from src.backtesting.engine import BacktestEngine
    from src.backtesting.models import BacktestConfig

    _OPTIMIZER_ONLY_KEYS = {"search_mode", "max_combinations", "sort_metric"}
    defaults = {
        k: v for k, v in get_backtest_defaults().items() if k not in _OPTIMIZER_ONLY_KEYS
    }
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

    config = BacktestConfig.from_flat(
        symbol="XAUUSD",
        timeframe=tf,
        start_time=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        strategy_sessions=strategy_sessions,
        strategy_timeframes=strategy_timeframes,
        **merged,
    )

    components = build_backtest_components(
        strategy_params_per_tf=strategy_params_per_tf,
    )
    engine = BacktestEngine(
        config=config,
        data_loader=components["data_loader"],
        signal_module=components["signal_module"],
        indicator_pipeline=components["pipeline"],
        regime_detector=components["regime_detector"],
        performance_tracker=components.get("performance_tracker"),
    )
    result = engine.run()
    return {"trades": result.trades, "metrics": result.metrics}


def _extract_strategy_stats(trades: list, strategy_name: str) -> Dict[str, Any]:
    """从交易列表中提取指定策略的统计。"""
    strat_trades = [
        t for t in trades if getattr(t, "strategy", "") == strategy_name
    ]
    if not strat_trades:
        return {"n": 0, "w": 0, "wr": 0.0, "pnl": 0.0, "pf": 0.0, "avg_pnl": 0.0}

    n = len(strat_trades)
    wins = [t for t in strat_trades if (getattr(t, "pnl", 0) or 0) > 0]
    losses = [t for t in strat_trades if (getattr(t, "pnl", 0) or 0) <= 0]
    w = len(wins)
    total_pnl = sum(getattr(t, "pnl", 0) or 0 for t in strat_trades)
    gross_profit = sum(getattr(t, "pnl", 0) or 0 for t in wins) if wins else 0
    gross_loss = abs(sum(getattr(t, "pnl", 0) or 0 for t in losses)) if losses else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

    return {
        "n": n,
        "w": w,
        "wr": w / n * 100 if n > 0 else 0.0,
        "pnl": round(total_pnl, 2),
        "pf": round(pf, 3),
        "avg_pnl": round(total_pnl / n, 2) if n > 0 else 0.0,
    }


def run_aggression_search(
    tf: str,
    start: str,
    end: str,
    strategies: Optional[List[str]] = None,
    step: float = 0.10,
) -> Dict[str, List[Dict[str, Any]]]:
    """执行 aggression 网格搜索。"""
    active = strategies or _get_active_strategies(tf)
    if not active:
        sys.stderr.write(f"No active structured strategies for {tf}\n")
        return {}

    aggression_values = []
    v = step
    while v <= 0.95 + 1e-9:
        aggression_values.append(round(v, 2))
        v += step

    total_runs = len(active) * len(aggression_values)
    sys.stderr.write(
        f"\n=== {tf} Aggression Search ===\n"
        f"Strategies: {', '.join(active)}\n"
        f"Aggression range: {aggression_values[0]}~{aggression_values[-1]} (step={step})\n"
        f"Total runs: {total_runs} (est. ~{total_runs * 17 // 60}min)\n\n"
    )

    results: Dict[str, List[Dict[str, Any]]] = {}
    run_count = 0
    t0 = time.time()

    for strat_name in active:
        default_aggr = _DEFAULTS.get(strat_name, 0.50)
        strat_results: List[Dict[str, Any]] = []

        for aggr in aggression_values:
            run_count += 1
            elapsed = time.time() - t0
            eta = elapsed / run_count * (total_runs - run_count) if run_count > 1 else 0
            sys.stderr.write(
                f"  [{run_count}/{total_runs}] {strat_name} a={aggr:.2f}"
                f"  ({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining)\n"
            )
            sys.stderr.flush()

            per_tf = {tf: {f"{strat_name}__aggression": aggr}}
            result = _run_single(tf, start, end, strategy_params_per_tf=per_tf)

            stats = _extract_strategy_stats(result["trades"], strat_name)
            stats["aggression"] = aggr
            stats["is_default"] = abs(aggr - default_aggr) < 0.01
            strat_results.append(stats)

        results[strat_name] = strat_results

    elapsed = time.time() - t0
    sys.stderr.write(f"\nCompleted {total_runs} runs in {elapsed:.0f}s\n")
    return results


def _render_results(
    tf: str, results: Dict[str, List[Dict[str, Any]]], start: str, end: str
) -> str:
    """渲染搜索结果表格。"""
    lines = [
        f"\n{'='*80}",
        f" AGGRESSION GRID SEARCH: {tf} XAUUSD ({start} ~ {end})",
        f"{'='*80}",
    ]

    for strat_name, strat_results in results.items():
        if not strat_results:
            continue

        default_aggr = _DEFAULTS.get(strat_name, 0.50)
        best = max(strat_results, key=lambda x: x["pnl"])
        valid = [r for r in strat_results if r["n"] >= 3]
        best_pf = max(valid, key=lambda x: x["pf"]) if valid else best

        lines.append(f"\n--- {strat_name} (default a={default_aggr:.2f}) ---")
        lines.append(
            f"  {'a':>5}  {'Trd':>4} {'Win':>4} {'WR%':>6} {'PnL':>10} "
            f"{'PF':>7} {'AvgPnL':>8}  Note"
        )
        lines.append(f"  {'-'*62}")

        for r in strat_results:
            note = ""
            if r["is_default"]:
                note += " [DEFAULT]"
            if r["aggression"] == best["aggression"]:
                note += " *BEST-PnL"
            if valid and r["aggression"] == best_pf["aggression"] and best_pf["aggression"] != best["aggression"]:
                note += " *BEST-PF"

            lines.append(
                f"  {r['aggression']:>5.2f}  {r['n']:>4} {r['w']:>4} "
                f"{r['wr']:>5.1f}% {r['pnl']:>+10.2f} {r['pf']:>7.3f} "
                f"{r['avg_pnl']:>+8.2f}{note}"
            )

        # 推荐摘要
        default_pnl = next(
            (r["pnl"] for r in strat_results if r["is_default"]), 0
        )
        if best["pnl"] > 0:
            improvement = best["pnl"] - default_pnl
            lines.append(
                f"  -> Best a={best['aggression']:.2f} "
                f"(PnL:{best['pnl']:+.2f}, vs default {improvement:+.2f})"
            )
        else:
            lines.append(f"  -> All aggression values unprofitable, consider freezing")

    # 总结
    lines.append(f"\n{'='*80}")
    lines.append(" SUMMARY: Recommended aggression overrides")
    lines.append(f"{'='*80}")
    lines.append(f"  {'Strategy':<35} {'Default':>7} {'Best a':>7} {'D PnL':>10}")
    lines.append(f"  {'-'*65}")

    for strat_name, strat_results in results.items():
        if not strat_results:
            continue
        default_aggr = _DEFAULTS.get(strat_name, 0.50)
        default_pnl = next(
            (r["pnl"] for r in strat_results if r["is_default"]), 0
        )
        best = max(strat_results, key=lambda x: x["pnl"])
        delta = best["pnl"] - default_pnl
        marker = " OK" if delta > 0 else ""
        lines.append(
            f"  {strat_name:<35} {default_aggr:>7.2f} {best['aggression']:>7.2f} "
            f"{delta:>+10.2f}{marker}"
        )

    return "\n".join(lines)


def main() -> None:
    from src.config.instance_context import set_current_environment

    parser = argparse.ArgumentParser(description="Aggression grid search")
    parser.add_argument(
        "--environment",
        choices=["live", "demo"],
        required=True,
        help="显式指定使用哪个环境数据库",
    )
    parser.add_argument(
        "--tf", required=True, help="Timeframe(s), comma-separated: H1,M30"
    )
    parser.add_argument("--start", default="2025-12-30")
    parser.add_argument("--end", default="2026-03-30")
    parser.add_argument(
        "--step", type=float, default=0.10, help="Aggression step size (default 0.10)"
    )
    parser.add_argument(
        "--strategies",
        default=None,
        help="Comma-separated strategy names (default: all active)",
    )
    args = parser.parse_args()
    set_current_environment(args.environment)

    timeframes = [t.strip().upper() for t in args.tf.split(",")]
    strategies = (
        [s.strip() for s in args.strategies.split(",")]
        if args.strategies
        else None
    )

    for tf in timeframes:
        results = run_aggression_search(
            tf, args.start, args.end, strategies=strategies, step=args.step
        )
        print(_render_results(tf, results, args.start, args.end))

    sys.stderr.write("\nDone.\n")


if __name__ == "__main__":
    main()
